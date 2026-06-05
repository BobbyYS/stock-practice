"""
sync/export_sql.py — 離線資料匯出腳本（無需 TiDB 連線）

功能：
  1. 從 yfinance 下載 WATCHLIST 所有股票的 OHLCV
  2. 從 TWSE / TPEX 下載三大法人買賣超（可選）
  3. 產生 MySQL 相容的 INSERT SQL 檔案，直接貼入 TiDB SQL Editor 執行

使用方式：
  python sync/export_sql.py                    # 近1年 OHLCV（預設）
  python sync/export_sql.py --years 5          # 近5年
  python sync/export_sql.py --with-chips       # 含三大法人（耗時較長）
  python sync/export_sql.py --output my.sql    # 自訂輸出檔名

執行完成後：
  → 開啟 TiDB Cloud SQL Editor
  → 全選 output/insert_data.sql 內容貼上執行
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


# ------------------------------------------------------------------
# WATCHLIST — 請依實際股池更新
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
    parser = argparse.ArgumentParser(description="離線產生 TiDB INSERT SQL 檔")
    parser.add_argument("--years", type=int, default=1, help="回補年數（預設 1）")
    parser.add_argument("--with-chips", action="store_true", help="含三大法人資料")
    parser.add_argument("--output", default="insert_data.sql", help="輸出檔名")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / args.output

    end_date = date.today()
    start_date = end_date - timedelta(days=365 * args.years)
    log.info(f"區間：{start_date} ~ {end_date}（{args.years} 年，{len(WATCHLIST)} 檔）")

    lines: list[str] = []
    lines.append(f"-- 自動產生：{date.today()}，區間 {start_date} ~ {end_date}")
    lines.append(f"-- 股票數量：{len(WATCHLIST)} 檔")
    lines.append("USE stock_practice_db;")
    lines.append("")

    # stock_info
    log.info("=== 產生 stock_info INSERT ===")
    lines += _gen_stock_info()

    # stock_daily_data OHLCV
    log.info("=== 下載 OHLCV（yfinance 批量）===")
    ohlcv_map = _download_ohlcv(start_date, end_date)

    # 三大法人（可選）
    chips_map: dict[date, dict[str, dict]] = {}
    if args.with_chips:
        log.info("=== 下載三大法人（逐日，耗時較長）===")
        chips_map = _download_chips_all(start_date, end_date)

    log.info("=== 產生 stock_daily_data INSERT ===")
    lines += _gen_daily_data(ohlcv_map, chips_map)

    # 寫檔
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"✅ 產生完成：{out_path}")
    log.info(f"   共 {len(lines)} 行")
    log.info("   → 開啟 TiDB Cloud SQL Editor，全選貼上執行")


# ------------------------------------------------------------------
# stock_info INSERT
# ------------------------------------------------------------------

def _gen_stock_info() -> list[str]:
    lines = ["-- =========================================="]
    lines.append("-- stock_info")
    lines.append("-- ==========================================")
    for code, (_, name, market) in WATCHLIST.items():
        safe_name = name.replace("'", "''")
        lines.append(
            f"INSERT INTO stock_info (stock_code, stock_name, market_type) "
            f"VALUES ('{code}', '{safe_name}', '{market}') "
            f"ON DUPLICATE KEY UPDATE stock_name = VALUES(stock_name);"
        )
    lines.append("")
    log.info(f"stock_info：{len(WATCHLIST)} 筆")
    return lines


# ------------------------------------------------------------------
# yfinance 批量下載 OHLCV
# ------------------------------------------------------------------

def _download_ohlcv(start_date: date, end_date: date) -> dict[str, pd.DataFrame]:
    tickers = [v[0] for v in WATCHLIST.values()]
    raw = yf.download(
        tickers=tickers,
        start=str(start_date),
        end=str(end_date + timedelta(days=1)),
        auto_adjust=True,
        progress=True,
        group_by="ticker",
    )

    result: dict[str, pd.DataFrame] = {}
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
            result[code] = df
            log.info(f"  {code}：{len(df)} 筆")
        except Exception as e:
            log.warning(f"  {code} 失敗：{e}")

    return result


# ------------------------------------------------------------------
# 三大法人下載（可選）
# ------------------------------------------------------------------

def _download_chips_all(start_date: date, end_date: date) -> dict[date, dict[str, dict]]:
    """回傳 {trade_date: {stock_code: {foreign_buy, investment_buy, dealer_buy}}}"""
    result: dict[date, dict[str, dict]] = {}
    current = start_date
    while current <= end_date:
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue
        chips = {**_fetch_chips_twse(current), **_fetch_chips_tpex(current)}
        if chips:
            result[current] = chips
        time.sleep(0.5)
        current += timedelta(days=1)
    return result


def _fetch_chips_twse(target_date: date) -> dict[str, dict]:
    date_str = target_date.strftime("%Y%m%d")
    url = (f"https://www.twse.com.tw/fund/T86"
           f"?response=json&date={date_str}&selectType=ALLBUT0999")
    try:
        data = requests.get(url, timeout=15).json()
        if data.get("stat") != "OK":
            return {}
        result = {}
        for row in data.get("data", []):
            code = f"{row[0].strip()}.TW"
            if code not in WATCHLIST:
                continue
            def p(s): return int(str(s).replace(",", "").replace("+", "") or "0")
            result[code] = {
                "foreign_buy": p(row[4]),
                "investment_buy": p(row[7]),
                "dealer_buy": p(row[13]) if len(row) > 13 else p(row[10]),
            }
        return result
    except Exception as e:
        log.debug(f"TWSE {target_date}：{e}")
        return {}


def _fetch_chips_tpex(target_date: date) -> dict[str, dict]:
    roc_year = target_date.year - 1911
    date_str = f"{roc_year}/{target_date.month:02d}/{target_date.day:02d}"
    url = ("https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_print.php"
           f"?l=zh-tw&se=AL&t=D&d={date_str}&_=1")
    try:
        data = requests.get(url, timeout=15,
                            headers={"Referer": "https://www.tpex.org.tw/"}).json()
        result = {}
        for row in data.get("aaData", []):
            code = f"{row[0].strip()}.TWO"
            if code not in WATCHLIST:
                continue
            def p(s): return int(str(s).replace(",", "").replace("+", "").strip() or "0")
            result[code] = {
                "foreign_buy": p(row[4]),
                "investment_buy": p(row[7]),
                "dealer_buy": p(row[10]) if len(row) > 10 else 0,
            }
        return result
    except Exception as e:
        log.debug(f"TPEX {target_date}：{e}")
        return {}


# ------------------------------------------------------------------
# 產生 stock_daily_data INSERT SQL
# ------------------------------------------------------------------

def _gen_daily_data(
    ohlcv_map: dict[str, pd.DataFrame],
    chips_map: dict[date, dict[str, dict]],
) -> list[str]:
    lines = ["-- =========================================="]
    lines.append("-- stock_daily_data")
    lines.append("-- ==========================================")
    total = 0
    for code, df in ohlcv_map.items():
        for row in df.itertuples(index=False):
            chip = chips_map.get(row.trade_date, {}).get(code, {})
            fb = chip.get("foreign_buy", 0)
            ib = chip.get("investment_buy", 0)
            db_ = chip.get("dealer_buy", 0)
            lines.append(
                f"INSERT INTO stock_daily_data "
                f"(stock_code, trade_date, open_price, high_price, low_price, "
                f"close_price, volume, foreign_buy, investment_buy, dealer_buy) "
                f"VALUES "
                f"('{code}', '{row.trade_date}', {row.open_price:.2f}, "
                f"{row.high_price:.2f}, {row.low_price:.2f}, {row.close_price:.2f}, "
                f"{int(row.volume)}, {fb}, {ib}, {db_}) "
                f"ON DUPLICATE KEY UPDATE "
                f"open_price=VALUES(open_price), high_price=VALUES(high_price), "
                f"low_price=VALUES(low_price), close_price=VALUES(close_price), "
                f"volume=VALUES(volume), foreign_buy=VALUES(foreign_buy), "
                f"investment_buy=VALUES(investment_buy), dealer_buy=VALUES(dealer_buy);"
            )
            total += 1
    lines.append("")
    log.info(f"stock_daily_data：{total} 筆")
    return lines


if __name__ == "__main__":
    main()
