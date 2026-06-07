"""
sync/backfill.py — 歷史資料5年回補腳本

功能：
  1. 從 TWSE / TPEX 官方 API 一次拉取所有 stock_info 股票的 N 年 OHLCV（按月、逐股下載）
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
# WATCHLIST — 預設基本股票（用於資料庫初始化）
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
}


# ------------------------------------------------------------------
# 輔助函式
# ------------------------------------------------------------------

def _load_stock_info_dict() -> dict[str, tuple[str, str, str]]:
    """
    從資料庫的 stock_info 表中載入所有股票，
    並轉換為 dict[stock_code, (yfinance_ticker, stock_name, market_type)]
    """
    df = db.query_df("SELECT stock_code, stock_name, market_type FROM stock_info")
    stocks = {}
    for _, row in df.iterrows():
        code = row["stock_code"]
        name = row["stock_name"]
        market = row["market_type"]
        # yfinance 對上市/上櫃都使用 .TW 後綴
        yfinance_ticker = code.replace(".TWO", ".TW")
        stocks[code] = (yfinance_ticker, name, market)
    return stocks


def _get_months_in_range(start_date: date, end_date: date) -> list[tuple[int, int]]:
    """回傳 start_date 到 end_date 之間的所有 (year, month) 列表"""
    months = []
    current = start_date
    while current <= end_date:
        year_month = (current.year, current.month)
        if year_month not in months:
            months.append(year_month)
        # 移至下個月第一天
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def _parse_roc_date_slash(date_str: str) -> date | None:
    """解析民國斜線日期字串，例如 113/05/02 -> date(2024, 5, 2)"""
    try:
        parts = date_str.strip().split('/')
        if len(parts) != 3:
            return None
        year = int(parts[0]) + 1911
        month = int(parts[1])
        day = int(parts[2])
        return date(year, month, day)
    except Exception:
        return None


def _safe_float(val) -> float:
    if val is None:
        return 0.0
    s = str(val).replace(",", "").strip()
    if s in ["--", "", "None"]:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fetch_twse_historical_ohlcv(stock_no: str, year: int, month: int) -> tuple[pd.DataFrame, bool]:
    """從 TWSE 官網 API 拉取單個股單月的歷史 K 線"""
    date_str = f"{year}{month:02d}01"
    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date_str}&stockNo={stock_no}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    max_retries = 3
    retry_delay = 10
    
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                stat = data.get("stat", "")
                
                # Check for unlisted stock
                if "很抱歉" in stat or "沒有符合條件" in stat or "查詢無資料" in stat or "無資料" in stat:
                    log.info(f"TWSE {stock_no} 於 {year}/{month} 確定無資料 (可能未上市)")
                    return pd.DataFrame(), True
                    
                if stat != "OK" or "data" not in data:
                    log.warning(f"TWSE API 回傳異常：{stat} ({stock_no}, {year}/{month}). Attempt {attempt+1}/{max_retries}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    return pd.DataFrame(), False
                    
                rows = []
                for row in data["data"]:
                    dt = _parse_roc_date_slash(row[0])
                    if dt is None:
                        continue
                    op = _safe_float(row[3])
                    hp = _safe_float(row[4])
                    lp = _safe_float(row[5])
                    cp = _safe_float(row[6])
                    vol = int(round(_safe_float(row[1]) / 1000))
                    
                    rows.append({
                        "trade_date": dt,
                        "open_price": op,
                        "high_price": hp,
                        "low_price": lp,
                        "close_price": cp,
                        "volume": vol
                    })
                return pd.DataFrame(rows), False
                
            elif r.status_code in [403, 429, 503]:
                log.warning(f"TWSE API 被限流/阻擋：{r.status_code} ({stock_no}, {year}/{month}). Attempt {attempt+1}/{max_retries}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
                    continue
            else:
                log.warning(f"TWSE API 回傳狀態碼異常：{r.status_code} ({stock_no}, {year}/{month})")
                return pd.DataFrame(), False
        except Exception as e:
            log.error(f"下載 TWSE {stock_no} 歷史資料失敗 ({year}/{month}) Attempt {attempt+1}/{max_retries}：{e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
                
    return pd.DataFrame(), False


def _fetch_tpex_historical_ohlcv(stock_no: str, year: int, month: int) -> tuple[pd.DataFrame, bool]:
    """從 TPEX 官網 POST API 拉取單個股單月的歷史 K 線"""
    url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
    date_str = f"{year}/{month:02d}/01"
    payload = {
        "code": stock_no,
        "date": date_str,
        "response": "json"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/stock-pricing.html"
    }
    
    max_retries = 3
    retry_delay = 10
    
    for attempt in range(max_retries):
        try:
            r = requests.post(url, data=payload, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                stat = data.get("stat", "")
                
                tables = data.get("tables", [])
                is_empty = False
                if len(tables) == 0:
                    is_empty = True
                elif tables[0].get("totalCount", 0) == 0:
                    is_empty = True
                elif not tables[0].get("data"):
                    is_empty = True
                    
                if is_empty and stat == "ok":
                    log.info(f"TPEX {stock_no} 於 {year}/{month} 確定無資料 (可能未上櫃)")
                    return pd.DataFrame(), True
                    
                if stat != "ok" or len(tables) == 0:
                    log.warning(f"TPEX API 回傳異常：{stat} ({stock_no}, {year}/{month}). Attempt {attempt+1}/{max_retries}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    return pd.DataFrame(), False
                    
                table = tables[0]
                rows = []
                for row in table.get("data", []):
                    dt = _parse_roc_date_slash(row[0])
                    if dt is None:
                        continue
                    op = _safe_float(row[3])
                    hp = _safe_float(row[4])
                    lp = _safe_float(row[5])
                    cp = _safe_float(row[6])
                    vol = int(round(_safe_float(row[1])))
                    
                    rows.append({
                        "trade_date": dt,
                        "open_price": op,
                        "high_price": hp,
                        "low_price": lp,
                        "close_price": cp,
                        "volume": vol
                    })
                return pd.DataFrame(rows), False
                
            elif r.status_code in [403, 429, 503]:
                log.warning(f"TPEX API 被限流/阻擋：{r.status_code} ({stock_no}, {year}/{month}). Attempt {attempt+1}/{max_retries}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
                    continue
            else:
                log.warning(f"TPEX API 回傳狀態碼異常：{r.status_code} ({stock_no}, {year}/{month})")
                return pd.DataFrame(), False
        except Exception as e:
            log.error(f"下載 TPEX {stock_no} 歷史資料失敗 ({year}/{month}) Attempt {attempt+1}/{max_retries}：{e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
                
    return pd.DataFrame(), False


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

    # Step 1: 確保 stock_info 存在基本監控股票
    _upsert_stock_info()

    # 從資料庫中讀取所有股票資訊 (以同步 stock_info 內所有的股票資訊)
    stocks = _load_stock_info_dict()

    log.info(f"回補區間：{start_date} ~ {end_date}（{len(stocks)} 檔股票）")

    if not args.eps_only:
        # Step 2: 批量下載 OHLCV（官方 API 逐月/逐股）
        log.info("=== STEP 2: 下載 OHLCV（官方 API 逐月/逐股）===")
        _bulk_upsert_ohlcv(start_date, end_date, stocks)

        # Step 3: 三大法人（可選）
        if args.with_chips:
            log.info("=== STEP 3: 下載三大法人（逐日，耗時較長）===")
            _bulk_upsert_chips(start_date, end_date, stocks)
        else:
            log.info("=== STEP 3: 跳過三大法人（未加 --with-chips）===")

    # Step 4: EPS
    log.info("=== STEP 4: 下載 EPS 季報（FinMind）===")
    _bulk_upsert_eps(start_date, stocks)

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
# Step 2: 批量 OHLCV upsert（官方 API 逐月/逐股）
# ------------------------------------------------------------------

def _load_ipo_dates() -> dict[str, date | None]:
    """載入所有股票的 ipo_date"""
    df = db.query_df("SELECT stock_code, ipo_date FROM stock_info")
    result = {}
    for _, row in df.iterrows():
        code = row["stock_code"]
        ipo = row["ipo_date"]
        if ipo is not None and not pd.isna(ipo):
            result[code] = pd.to_datetime(ipo).date()
        else:
            result[code] = None
    return result


def _update_stock_ipo_date(stock_code: str, ipo_date: date):
    db.execute(
        "UPDATE stock_info SET ipo_date = :ipo WHERE stock_code = :code",
        {"ipo": str(ipo_date), "code": stock_code}
    )


def _load_all_existing_months() -> dict[str, dict[tuple[int, int], tuple[date, int]]]:
    sql = """
        SELECT stock_code, YEAR(trade_date) as y, MONTH(trade_date) as m, 
               MAX(trade_date) as max_d, COUNT(*) as cnt
        FROM stock_daily_data 
        GROUP BY stock_code, y, m
    """
    df = db.query_df(sql)
    result = {}
    for _, row in df.iterrows():
        code = row["stock_code"]
        y = int(row["y"])
        m = int(row["m"])
        max_d = pd.to_datetime(row["max_d"]).date()
        cnt = int(row["cnt"])
        if code not in result:
            result[code] = {}
        result[code][(y, m)] = (max_d, cnt)
    return result


def _is_before_ipo(year: int, month: int, ipo_date: date | None) -> bool:
    if ipo_date is None:
        return False
    if month == 12:
        next_y, next_m = year + 1, 1
    else:
        next_y, next_m = year, month + 1
    next_month_start = date(next_y, next_m, 1)
    return next_month_start <= ipo_date


def _should_skip_month(stock_code: str, year: int, month: int, existing_months: dict[str, dict[tuple[int, int], tuple[date, int]]]) -> bool:
    today = date.today()
    # 1. 不要跳過當前月份，因為每天都有新資料
    if year == today.year and month == today.month:
        return False
        
    stock_data = existing_months.get(stock_code, {})
    if (year, month) not in stock_data:
        return False
        
    max_d, cnt = stock_data[(year, month)]
    
    # 2. 計算該月份的最後一天
    import calendar
    _, last_day_num = calendar.monthrange(year, month)
    last_day_of_month = date(year, month, last_day_num)
    
    # 3. 如果該月份最後一筆交易日距離月底在 7 天之內（如 29 號對 31 號），或該月份資料筆數已大於等於 15 筆，視為完整，可跳過
    if (last_day_of_month - max_d).days <= 7 or cnt >= 15:
        return True
        
    return False


def _sync_ohlcv_core(start_date: date, end_date: date, stocks: dict[str, tuple[str, str, str]], cb=None) -> int:
    months = _get_months_in_range(start_date, end_date)
    # Reverse it to go backward in time
    months.reverse()
    
    total_steps = len(months) * len(stocks)
    current_step = 0
    total_rows = 0
    
    # Load ipo_dates and existing_months
    ipo_dates = _load_ipo_dates()
    existing_months = _load_all_existing_months()
    unlisted_stocks = set()
    
    if cb:
        cb("💡 採用『由新到舊』逆序回補策略，可自動偵測新上市股票的掛牌邊界，避免無謂的歷史 API 請求。")
        
    for year, month in months:
        if cb:
            cb(f"📅 處理月份：{year}/{month:02d}")
        else:
            log.info(f"--- 處理月份 {year}/{month:02d} ---")
            
        for code, (_, name, market) in stocks.items():
            stock_no = code.split('.')[0]
            current_step += 1
            progress_val = float(current_step) / total_steps
            status_msg = f"下載 K 線：{code} ({name}) - {year}/{month:02d} (進度 {current_step}/{total_steps})"
            
            # Check if dynamically unlisted in this run
            if code in unlisted_stocks:
                msg = f"  [Skip] {code} ({name}) 在此月份前尚未上市/上櫃 (本趟已偵測)"
                if cb:
                    cb(msg, progress_val, status_msg)
                else:
                    log.info(msg)
                continue
                
            # Check if before DB ipo_date
            ipo_date = ipo_dates.get(code)
            if _is_before_ipo(year, month, ipo_date):
                msg = f"  [Skip] {code} ({name}) 在此月份前尚未上市/上櫃 (資料庫 ipo_date: {ipo_date})"
                if cb:
                    cb(msg, progress_val, status_msg)
                else:
                    log.info(msg)
                continue
                
            # Check if month already exists in DB
            if _should_skip_month(code, year, month, existing_months):
                msg = f"  [Skip] {code} ({name}) 該月份已存在資料庫"
                if cb:
                    cb(msg, progress_val, status_msg)
                else:
                    log.info(msg)
                continue
                
            if cb:
                cb(f"  正在下載 {code} ({name})...", progress_val, status_msg)
            else:
                log.info(f"  同步 {code} ({name}) ...")
                
            # Fetch data
            is_unlisted = False
            df = pd.DataFrame()
            if market == "上市" or code.endswith(".TW"):
                df, is_unlisted = _fetch_twse_historical_ohlcv(stock_no, year, month)
            elif market == "上櫃" or code.endswith(".TWO"):
                df, is_unlisted = _fetch_tpex_historical_ohlcv(stock_no, year, month)
            else:
                msg = f"  ⚠️ {code} 市場類型不明 ({market})，跳過"
                if cb:
                    cb(msg, progress_val, status_msg)
                else:
                    log.warning(msg)
                continue
                
            if is_unlisted:
                unlisted_stocks.add(code)
                if month == 12:
                    next_y, next_m = year + 1, 1
                else:
                    next_y, next_m = year, month + 1
                next_month_start = date(next_y, next_m, 1)
                
                if ipo_date is None:
                    _update_stock_ipo_date(code, next_month_start)
                    ipo_dates[code] = next_month_start
                    
                msg = f"    🚫 確定尚未掛牌，設定上市/上櫃日期起點為 {next_month_start}，後續月份將自動跳過"
                if cb:
                    cb(msg, progress_val, status_msg)
                else:
                    log.info(msg)
                    
            elif not df.empty:
                df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
                if not df.empty:
                    rows = _upsert_ohlcv_rows(code, df)
                    total_rows += rows
                    
                    msg = f"    ✅ 成功寫入 {rows} 筆"
                    if cb:
                        cb(msg, progress_val, status_msg)
                    else:
                        log.info(msg)
                else:
                    msg = "    該月份在設定區間內無交易資料"
                    if cb:
                        cb(msg, progress_val, status_msg)
                    else:
                        log.info(msg)
            else:
                msg = "    ⚠️ 無法取得該月份資料 (可能非交易月份或連線失敗)"
                if cb:
                    cb(msg, progress_val, status_msg)
                else:
                    log.warning(msg)
                    
            time.sleep(3)
            
    return total_rows


def _bulk_upsert_ohlcv(start_date: date, end_date: date, stocks: dict[str, tuple[str, str, str]]):
    def cli_cb(msg: str, progress_val=None, status_msg=None):
        log.info(msg)
    _sync_ohlcv_core(start_date, end_date, stocks, cb=cli_cb)


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

def _bulk_upsert_chips(start_date: date, end_date: date, stocks: dict[str, tuple[str, str, str]]):
    """逐日呼叫 TWSE / TPEX API，回補三大法人買賣超。"""
    import sync_daily
    # 動態將 sync_daily.WATCHLIST 替換成我們從 stock_info 讀取出來的所有股票
    sync_daily.WATCHLIST = stocks

    current = start_date
    processed = 0
    while current <= end_date:
        if current.weekday() >= 5:  # 跳過週末
            current += timedelta(days=1)
            continue

        chips_listed = sync_daily._fetch_chips_twse(current)
        chips_otc = sync_daily._fetch_chips_tpex(current)
        chips = {**chips_listed, **chips_otc}

        if chips:
            _update_chips_for_date(current, chips, stocks)
            processed += 1

        time.sleep(0.5)
        current += timedelta(days=1)

    log.info(f"三大法人回補完畢，共處理 {processed} 個交易日")


def _update_chips_for_date(target_date: date, chips: dict, stocks: dict[str, tuple[str, str, str]]):
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
            if code in stocks:
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

def _bulk_upsert_eps(start_date: date, stocks: dict[str, tuple[str, str, str]]):
    """
    從 TWSE / TPEX OpenAPI 拉取最新 EPS，寫入 stock_eps。
    """
    import sync_daily
    sync_daily.WATCHLIST = stocks
    log.info("說明：TWSE/TPEX OpenAPI 僅提供最新季度的 EPS 資料，歷史回補不支援多年 EPS 自動拉取")
    sync_daily._sync_eps_daily()


if __name__ == "__main__":
    main()
