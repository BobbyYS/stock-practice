"""
sync/backfill.py — 歷史資料5年回補腳本

功能：
  1. 從 yfinance 一次拉取所有 WATCHLIST 股票的 N 年 OHLCV（單次批量下載，效率高）
  2. 從 TWSE / TPEX 逐日拉取三大法人買賣超（可選，耗時較長）
  3. 從 FinMind 拉取近 N 年 EPS 季報（需設定 API Token）
  4. 全部以 Upsert 方式寫入 TiDB（可重複執行，不會重複資料）

使用方式（本地）：
  python sync/backfill.py                    # 回補近5年 OHLCV + EPS（預設）
  python sync/backfill.py --years 3          # 回補近3年
  python sync/backfill.py --with-chips       # 額外回補三大法人（需時較長，約30~60分鐘）
  python sync/backfill.py --eps-only         # 僅回補 EPS

注意：
  - 三大法人資料因 TWSE/TPEX 為逐日 API，5年約需 1250 次呼叫（啟用 --with-chips 才執行）
  - 執行前請確認 .streamlit/secrets.toml 已填入 TiDB 連線資訊
  - EPS 需在 secrets.toml [finmind] 區塊填入 token
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
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault(
    "STREAMLIT_SECRETS_TOML",
    str(Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"),
)

import streamlit as st  # noqa: E402
import db               # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# WATCHLIST — 與 sync_daily.py 保持一致，請依實際股池更新
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
# 主程式
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="歷史資料回補")
    # 指定西元年範圍（優先）
    parser.add_argument("--year", type=int, default=None,
                        help="同步指定單一年份，如 --year 2020")
    parser.add_argument("--from-year", type=int, default=None,
                        help="同步起始年份，如 --from-year 2015")
    parser.add_argument("--to-year", type=int, default=None,
                        help="同步結束年份（含），如 --to-year 2020")
    # 舊有的「往回幾年」模式（備用）
    parser.add_argument("--years", type=int, default=None,
                        help="往回回補幾年（未指定 --year / --from-year 時生效，預設 1）")
    parser.add_argument("--with-chips", action="store_true",
                        help="額外回補三大法人逐日資料（耗時較長）")
    parser.add_argument("--eps-only", action="store_true",
                        help="僅回補 EPS 季報，跳過 OHLCV")
    args = parser.parse_args()

    today = date.today()

    # 決定 start_date / end_date
    if args.year:
        # 單一年份：該年 1/1 ~ 12/31（若為今年則截至今天）
        start_date = date(args.year, 1, 1)
        end_date   = date(args.year, 12, 31) if args.year < today.year else today
    elif args.from_year or args.to_year:
        from_y = args.from_year or today.year
        to_y   = args.to_year   or today.year
        start_date = date(from_y, 1, 1)
        end_date   = date(to_y, 12, 31) if to_y < today.year else today
    else:
        # fallback：往回 N 年（預設 1 年）
        n = args.years or 1
        end_date   = today
        start_date = date(today.year - n, today.month, today.day)

    log.info(f"回補區間：{start_date} ~ {end_date}（{len(WATCHLIST)} 檔股票）")

    # Step 1: 確保 stock_info 存在
    _upsert_stock_info()

    if not args.eps_only:
        # Step 2: 批量下載 OHLCV（yfinance 單次批量呼叫）
        log.info("=== STEP 2: 下載 OHLCV（yfinance 批量）===")
        _bulk_upsert_ohlcv(start_date, end_date)

        # Step 3: 三大法人（可選）
        if args.with_chips:
            log.info("=== STEP 3: 下載三大法人（逐日，耗時較長）===")
            _bulk_upsert_chips(start_date, end_date)
        else:
            log.info("=== STEP 3: 跳過三大法人（未加 --with-chips）===")

    # Step 4: EPS
    log.info("=== STEP 4: 下載 EPS 季報（FinMind）===")
    _bulk_upsert_eps(start_date)

    log.info("✅ 回補完畢")


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
# Step 2: 批量 OHLCV upsert（yfinance 單次下載整段區間）
# ------------------------------------------------------------------

def _bulk_upsert_ohlcv(start_date: date, end_date: date):
    tickers = [v[0] for v in WATCHLIST.values()]
    start_str = str(start_date)
    end_str = str(end_date + timedelta(days=1))  # yfinance end 為不含

    log.info(f"yfinance 下載 {len(tickers)} 檔，{start_str} ~ {end_str}")
    raw = yf.download(
        tickers=tickers,
        start=start_str,
        end=end_str,
        auto_adjust=True,
        progress=True,
        group_by="ticker",
    )

    total_rows = 0
    for code, (ticker, _, _) in WATCHLIST.items():
        try:
            df = raw[ticker].copy() if len(tickers) > 1 else raw.copy()
            df = df.dropna(subset=["Close"]).reset_index()
            df = df.rename(columns={
                "Date": "trade_date",
                "Open": "open_price",
                "High": "high_price",
                "Low": "low_price",
                "Close": "close_price",
                "Volume": "volume",
            })
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            df["volume"] = (df["volume"] / 1000).round(0).astype("int64")
            df = df[["trade_date", "open_price", "high_price",
                     "low_price", "close_price", "volume"]]

            rows = _upsert_ohlcv_rows(code, df)
            total_rows += rows
            log.info(f"  {code}：{rows} 筆 upsert 完成")

        except Exception as e:
            log.warning(f"  {code} OHLCV 處理失敗：{e}")

    log.info(f"OHLCV 回補完畢，共 {total_rows} 筆")


def _upsert_ohlcv_rows(stock_code: str, df: pd.DataFrame) -> int:
    """批量 upsert 一檔股票的 OHLCV，回傳寫入筆數。"""
    if df.empty:
        return 0

    engine = db.get_engine()
    sql = text("""
        INSERT INTO stock_daily_data
            (stock_code, trade_date, open_price, high_price, low_price,
             close_price, volume)
        VALUES
            (:code, :dt, :op, :hp, :lp, :cp, :vol)
        ON DUPLICATE KEY UPDATE
            open_price  = VALUES(open_price),
            high_price  = VALUES(high_price),
            low_price   = VALUES(low_price),
            close_price = VALUES(close_price),
            volume      = VALUES(volume)
    """)

    params = [
        {
            "code": stock_code,
            "dt": str(row.trade_date),
            "op": float(row.open_price),
            "hp": float(row.high_price),
            "lp": float(row.low_price),
            "cp": float(row.close_price),
            "vol": int(row.volume),
        }
        for row in df.itertuples(index=False)
    ]

    with engine.begin() as conn:
        conn.execute(sql, params)

    return len(params)


# ------------------------------------------------------------------
# Step 3: 三大法人逐日回補（可選，與 sync_daily.py 共用邏輯）
# ------------------------------------------------------------------

def _bulk_upsert_chips(start_date: date, end_date: date):
    """逐日呼叫 TWSE / TPEX API，回補三大法人買賣超。"""
    from sync_daily import _fetch_chips_twse, _fetch_chips_tpex

    current = start_date
    processed = 0
    while current <= end_date:
        if current.weekday() >= 5:  # 跳過週末
            current += timedelta(days=1)
            continue

        chips_listed = _fetch_chips_twse(current)
        chips_otc = _fetch_chips_tpex(current)
        chips = {**chips_listed, **chips_otc}

        if chips:
            _update_chips_for_date(current, chips)
            processed += 1

        time.sleep(0.5)
        current += timedelta(days=1)

    log.info(f"三大法人回補完畢，共處理 {processed} 個交易日")


def _update_chips_for_date(target_date: date, chips: dict):
    """將三大法人買賣超更新至已存在的 OHLCV 紀錄。"""
    engine = db.get_engine()
    sql = text("""
        UPDATE stock_daily_data
        SET foreign_buy    = :fb,
            investment_buy = :ib,
            dealer_buy     = :db
        WHERE stock_code = :code AND trade_date = :dt
    """)
    with engine.begin() as conn:
        for code, chip in chips.items():
            if code in WATCHLIST:
                conn.execute(sql, {
                    "code": code,
                    "dt": str(target_date),
                    "fb": chip.get("foreign_buy", 0),
                    "ib": chip.get("investment_buy", 0),
                    "db": chip.get("dealer_buy", 0),
                })


# ------------------------------------------------------------------
# Step 4: EPS 季報（FinMind API）
# ------------------------------------------------------------------

def _bulk_upsert_eps(start_date: date):
    """
    從 FinMind TaiwanStockFinancialStatements 拉取 EPS，寫入 stock_eps。
    需在 secrets.toml [finmind] 區塊設定 token。
    """
    try:
        token = st.secrets["finmind"]["token"]
    except Exception:
        log.warning("未設定 FinMind token（secrets.toml [finmind] token），跳過 EPS 同步。")
        log.warning("請在 .streamlit/secrets.toml 新增：\n[finmind]\ntoken = \"你的token\"")
        return

    start_str = str(start_date)
    total_rows = 0

    for code, (_, _, _) in WATCHLIST.items():
        # FinMind 使用不含後綴的代碼（2330.TW → 2330）
        fin_code = code.split(".")[0]
        rows = _fetch_and_upsert_eps(fin_code, code, start_str, token)
        total_rows += rows
        log.info(f"  {code} EPS：{rows} 筆 upsert")
        time.sleep(0.3)  # FinMind 免費版限流

    log.info(f"EPS 回補完畢，共 {total_rows} 筆")


def _fetch_and_upsert_eps(fin_code: str, stock_code: str,
                           start_date: str, token: str) -> int:
    """呼叫 FinMind API，解析並 upsert EPS 至 stock_eps。"""
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockFinancialStatements",
        "data_id": fin_code,
        "start_date": start_date,
        "token": token,
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"  {stock_code} FinMind 呼叫失敗：{e}")
        return 0

    records = data.get("data", [])
    if not records:
        return 0

    # FinMind 財報欄位：date(YYYY-MM-DD), type, value
    # type = "EPS" 為每股盈餘
    eps_rows = [r for r in records if r.get("type") == "EPS"]
    if not eps_rows:
        return 0

    engine = db.get_engine()
    sql = text("""
        INSERT INTO stock_eps
            (stock_code, fiscal_year, fiscal_quarter, eps, announced_date)
        VALUES
            (:code, :fy, :fq, :eps, :announced)
        ON DUPLICATE KEY UPDATE
            eps            = VALUES(eps),
            announced_date = VALUES(announced_date)
    """)

    params_list = []
    for r in eps_rows:
        try:
            announced = r["date"]  # YYYY-MM-DD
            y, m, _ = announced.split("-")
            year = int(y)
            month = int(m)
            # 財報公告月份推算季度：1~3月=Q4上年, 4~5月=Q1, 8~9月=Q2, 11~12月=Q3
            quarter_map = {1: 4, 2: 4, 3: 4, 4: 1, 5: 1, 8: 2, 9: 2, 11: 3, 12: 3}
            fiscal_quarter = quarter_map.get(month, 0)
            if fiscal_quarter == 0:
                continue
            fiscal_year = year - 1 if fiscal_quarter == 4 else year

            params_list.append({
                "code": stock_code,
                "fy": fiscal_year,
                "fq": fiscal_quarter,
                "eps": float(r["value"]),
                "announced": announced,
            })
        except Exception:
            continue

    if not params_list:
        return 0

    with engine.begin() as conn:
        conn.execute(sql, params_list)

    return len(params_list)


if __name__ == "__main__":
    main()
