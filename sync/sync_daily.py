"""
sync/sync_daily.py — 每日 ETL 數據同步腳本

功能：
  1. 從 yfinance 批量拉取台股 OHLCV（支援 .TW / .TWO 後綴）
  2. 從 TWSE / TPEX 官方 API 拉取三大法人買賣超
  3. Upsert 至 TiDB Cloud：stock_info + stock_daily_data

使用方式（本地）：
  1. 確保 .streamlit/secrets.toml 已填入 TiDB 連線資訊
  2. pip install -r requirements.txt
  3. python sync/sync_daily.py --date 2026-06-04
     （不帶 --date 預設同步昨日）

GitHub Actions 排程（建議）：
  - 在 .github/workflows/sync.yml 設定 cron: '30 7 * * 1-5'（台北時間 15:30 盤後）
"""
from __future__ import annotations

import argparse
import sys
import os
import time
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# 讓 sync 腳本可以載入 db.py（加入專案根目錄至 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 在非 Streamlit 環境下模擬 st.secrets（從 toml 直接讀取）
os.environ.setdefault("STREAMLIT_SECRETS_TOML",
    str(Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"))

import streamlit as st                 # noqa: E402  (需在 secrets 設定後才 import)
import db                              # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 監控清單（請根據實際操盤股池更新）
# ------------------------------------------------------------------

WATCHLIST: dict[str, tuple[str, str, str]] = {
    # stock_code  : (yfinance_ticker,  stock_name,    market_type)
    # 注意：yfinance 對上市和上櫃都使用 .TW 後綴；
    #       stock_code 保留 .TWO（用於 TPEX 三大法人 API 比對）
    "2330.TW":  ("2330.TW",  "台積電",  "上市"),
    "2317.TW":  ("2317.TW",  "鴻海",    "上市"),
    "2454.TW":  ("2454.TW",  "聯發科",  "上市"),
    "6449.TWO": ("6449.TW",  "鈺邦",    "上櫃"),   # yfinance ticker 用 .TW
    "3035.TW":  ("3035.TW",  "智原",    "上市"),
    "4958.TWO": ("4958.TW",  "臻鼎-KY", "上櫃"),   # yfinance ticker 用 .TW
    # ↑ 在此新增更多股票（上櫃股 yfinance ticker 請填 .TW，stock_code 填 .TWO）
}


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="台股每日 ETL 同步")
    parser.add_argument("--date", default=None, help="同步日期 YYYY-MM-DD，預設昨日")
    parser.add_argument("--backfill-days", type=int, default=0,
                        help="補歷史資料天數（0 = 僅同步單日）")
    args = parser.parse_args()

    target_date = (
        date.fromisoformat(args.date) if args.date
        else date.today() - timedelta(days=1)
    )

    if args.backfill_days > 0:
        dates = [target_date - timedelta(days=i) for i in range(args.backfill_days - 1, -1, -1)]
    else:
        dates = [target_date]

    # 過濾週末
    dates = [d for d in dates if d.weekday() < 5]

    if not dates:
        log.info("沒有需要同步的交易日（可能都是週末）。")
        return

    log.info(f"準備同步 {len(dates)} 個交易日，共 {len(WATCHLIST)} 檔股票")

    # Step 1: 確保所有股票已在 stock_info
    _upsert_stock_info()

    # Step 2: 批量拉取 yfinance OHLCV
    start_str = str(min(dates))
    end_str = str(max(dates) + timedelta(days=1))
    ohlcv_map = _fetch_ohlcv_batch(start_str, end_str)

    # Step 3: 逐日拉取三大法人並合併寫入
    for d in dates:
        log.info(f"--- 同步 {d} ---")
        chips_listed = _fetch_chips_twse(d)
        chips_otc = _fetch_chips_tpex(d)
        chips = {**chips_listed, **chips_otc}
        _upsert_daily_data(d, ohlcv_map, chips)
        time.sleep(0.5)  # 避免 API 過快觸發限流

    log.info("✅ ETL 同步完畢")


# ------------------------------------------------------------------
# Step 1: Upsert stock_info
# ------------------------------------------------------------------

def _upsert_stock_info():
    for code, (_, name, market) in WATCHLIST.items():
        db.execute(
            """
            INSERT INTO stock_info (stock_code, stock_name, market_type)
            VALUES (:code, :name, :market)
            ON DUPLICATE KEY UPDATE stock_name = VALUES(stock_name)
            """,
            {"code": code, "name": name, "market": market},
        )
    log.info(f"stock_info upsert 完成（{len(WATCHLIST)} 筆）")


# ------------------------------------------------------------------
# Step 2: yfinance 批量拉取 OHLCV
# ------------------------------------------------------------------

def _fetch_ohlcv_batch(start: str, end: str) -> dict[str, pd.DataFrame]:
    """
    回傳 {stock_code: DataFrame(trade_date, open_price, high_price, low_price, close_price, volume)}
    """
    tickers = [v[0] for v in WATCHLIST.values()]
    log.info(f"yfinance 拉取 {len(tickers)} 檔，區間 {start} ~ {end}")

    raw = yf.download(
        tickers=tickers,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
    )

    result: dict[str, pd.DataFrame] = {}
    for code, (ticker, _, _) in WATCHLIST.items():
        try:
            if len(tickers) == 1:
                df = raw.copy()
            else:
                df = raw[ticker].copy()

            df = df.dropna(subset=["Close"])
            df = df.reset_index()
            df = df.rename(columns={
                "Date": "trade_date",
                "Open": "open_price",
                "High": "high_price",
                "Low": "low_price",
                "Close": "close_price",
                "Volume": "volume",
            })
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            df = df[["trade_date", "open_price", "high_price", "low_price", "close_price", "volume"]]
            # 台股 volume 單位轉換：yfinance 回傳股數，除以 1000 換算為張
            df["volume"] = (df["volume"] / 1000).round(0).astype("int64")
            result[code] = df
        except Exception as e:
            log.warning(f"{code} OHLCV 拉取失敗：{e}")

    return result


# ------------------------------------------------------------------
# Step 3a: TWSE 三大法人（上市）
# ------------------------------------------------------------------

def _fetch_chips_twse(target_date: date) -> dict[str, dict]:
    """
    回傳 {stock_code: {"foreign_buy": int, "investment_buy": int, "dealer_buy": int}}
    """
    date_str = target_date.strftime("%Y%m%d")
    url = (
        "https://www.twse.com.tw/fund/T86"
        f"?response=json&date={date_str}&selectType=ALLBUT0999"
    )
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
    except Exception as e:
        log.warning(f"TWSE 三大法人 API 失敗 ({target_date})：{e}")
        return {}

    if data.get("stat") != "OK" or "data" not in data:
        log.warning(f"TWSE 三大法人 API 無資料 ({target_date})")
        return {}

    result: dict[str, dict] = {}
    for row in data["data"]:
        code_raw = row[0].strip()
        # 轉換為 .TW 格式
        code = f"{code_raw}.TW"
        if code not in WATCHLIST:
            continue
        try:
            # 欄位：[0]代號 [1]名稱 [2]外資買 [3]外資賣 [4]外資淨
            # [5]投信買 [6]投信賣 [7]投信淨 [8]自營商(自) [9] ... [13]自營商合計淨
            def parse_int(s: str) -> int:
                return int(s.replace(",", "").replace("+", ""))

            foreign_net = parse_int(row[4])
            invest_net = parse_int(row[7])
            # 自營商合計淨（含避險）= index 13
            dealer_net = parse_int(row[13]) if len(row) > 13 else parse_int(row[10])

            result[code] = {
                "foreign_buy": foreign_net,
                "investment_buy": invest_net,
                "dealer_buy": dealer_net,
            }
        except (ValueError, IndexError) as e:
            log.debug(f"解析 {code} 失敗：{e}")

    log.info(f"TWSE 三大法人：成功解析 {len(result)} 筆（{target_date}）")
    return result


# ------------------------------------------------------------------
# Step 3b: TPEX 三大法人（上櫃）
# ------------------------------------------------------------------

def _fetch_chips_tpex(target_date: date) -> dict[str, dict]:
    """
    使用 TPEX JSON API 拉取上櫃股三大法人資料。
    TPEX 憑證有已知問題（Missing Subject Key Identifier），使用 verify=False。
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    roc_year = target_date.year - 1911
    date_str = f"{roc_year}/{target_date.month:02d}/{target_date.day:02d}"
    import time as _time
    ts = int(_time.time() * 1000)
    url = (
        "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_print.php"
        f"?l=zh-tw&se=AL&t=D&d={date_str}&_={ts}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Referer": "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade.php?l=zh-tw",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        resp = requests.get(url, timeout=15, headers=headers, verify=False)
        if not resp.text.strip():
            log.debug(f"TPEX {target_date}：空回應（可能假日或停市）")
            return {}
        data = resp.json()
    except Exception as e:
        log.warning(f"TPEX 三大法人 API 失敗 ({target_date})：{e}")
        return {}

    rows = data.get("aaData", [])
    result: dict[str, dict] = {}
    for row in rows:
        code_raw = row[0].strip()
        code = f"{code_raw}.TWO"
        if code not in WATCHLIST:
            continue
        try:
            def parse_int(s) -> int:
                return int(str(s).replace(",", "").replace("+", "").strip() or "0")

            # TPEX 欄位（大約）：[0]代號 [1]名稱
            # [2]外資買 [3]外資賣 [4]外資淨
            # [5]投信買 [6]投信賣 [7]投信淨
            # [8]自營買 [9]自營賣 [10]自營淨
            foreign_net = parse_int(row[4])
            invest_net = parse_int(row[7])
            dealer_net = parse_int(row[10]) if len(row) > 10 else 0

            result[code] = {
                "foreign_buy": foreign_net,
                "investment_buy": invest_net,
                "dealer_buy": dealer_net,
            }
        except (ValueError, IndexError) as e:
            log.debug(f"解析 TPEX {code} 失敗：{e}")

    log.info(f"TPEX 三大法人：成功解析 {len(result)} 筆（{target_date}）")
    return result


# ------------------------------------------------------------------
# Step 4: Upsert stock_daily_data
# ------------------------------------------------------------------

def _upsert_daily_data(
    target_date: date,
    ohlcv_map: dict[str, pd.DataFrame],
    chips: dict[str, dict],
):
    upsert_count = 0
    for code in WATCHLIST:
        df = ohlcv_map.get(code)
        if df is None or df.empty:
            continue

        day_df = df[df["trade_date"] == target_date]
        if day_df.empty:
            log.debug(f"{code} {target_date} 無 OHLCV（可能是假日或停牌）")
            continue

        row = day_df.iloc[0]
        chip = chips.get(code, {})

        db.execute(
            """
            INSERT INTO stock_daily_data
                (stock_code, trade_date, open_price, high_price, low_price,
                 close_price, volume, foreign_buy, investment_buy, dealer_buy)
            VALUES
                (:code, :dt, :op, :hp, :lp, :cp, :vol, :fb, :ib, :db)
            ON DUPLICATE KEY UPDATE
                open_price     = VALUES(open_price),
                high_price     = VALUES(high_price),
                low_price      = VALUES(low_price),
                close_price    = VALUES(close_price),
                volume         = VALUES(volume),
                foreign_buy    = VALUES(foreign_buy),
                investment_buy = VALUES(investment_buy),
                dealer_buy     = VALUES(dealer_buy)
            """,
            {
                "code": code,
                "dt": str(target_date),
                "op": float(row["open_price"]),
                "hp": float(row["high_price"]),
                "lp": float(row["low_price"]),
                "cp": float(row["close_price"]),
                "vol": int(row["volume"]),
                "fb": chip.get("foreign_buy", 0),
                "ib": chip.get("investment_buy", 0),
                "db": chip.get("dealer_buy", 0),
            },
        )
        upsert_count += 1

    log.info(f"stock_daily_data upsert：{upsert_count} 筆（{target_date}）")


if __name__ == "__main__":
    main()
