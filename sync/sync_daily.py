"""
sync/sync_daily.py — 每日 ETL 數據同步腳本

功能：
  1. 從 TWSE / TPEX 官方 OpenAPI 拉取當日 OHLCV
  2. 歷史補資料模式：使用 TWSE STOCK_DAY / TPEX st43 逐股逐月拉取
  3. 從 TWSE / TPEX 官方 API 拉取三大法人買賣超
  4. Upsert 至 TiDB Cloud：stock_info + stock_daily_data

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

WATCHLIST: dict[str, tuple[str, str]] = {
    # stock_code: (stock_name, market_type)
    # 上市（.TW）→ TWSE；上櫃（.TWO）→ TPEX
    "2330.TW":  ("台積電",   "上市"),
    "2317.TW":  ("鴻海",     "上市"),
    "2454.TW":  ("聯發科",   "上市"),
    "6449.TW":  ("鈺邦",     "上市"),   # 已確認上市，非上櫃
    "3035.TW":  ("智原",     "上市"),
    "4958.TW":  ("臻鼎-KY",  "上市"),   # 已確認上市，非上櫃
    # 上櫃股範例：
    # "8043.TWO": ("宏正", "上櫃"),
}


# ------------------------------------------------------------------
# API 共用設定：rate limiting 與 headers
# ------------------------------------------------------------------

API_DELAY = 2.0  # 每次 API 呼叫間隔秒數，避免觸發限流

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _api_get(url: str, **kwargs) -> requests.Response:
    """統一 API 呼叫，自動附加 User-Agent 並在呼叫前等待，避免觸發限流。"""
    time.sleep(API_DELAY)
    kw: dict = {"timeout": 15, "headers": HEADERS}
    kw.update(kwargs)
    return requests.get(url, **kw)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="台股每日 ETL 同步")
    parser.add_argument("--date", default=None, help="同步日期 YYYY-MM-DD，預設昨日")
    parser.add_argument("--backfill-days", type=int, default=0,
                        help="補歷史資料天數（0 = 僅同步單日）")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()

    if args.backfill_days > 0:
        # 補歷史：從 target_date 往回 backfill_days 天
        dates = [target_date - timedelta(days=i) for i in range(args.backfill_days - 1, -1, -1)]
    elif args.date:
        # 指定單一日期
        dates = [target_date]
    else:
        # 預設（盤後排程）：同步「今天 + 前一個交易日」
        # 理由：可能在盤中先跑過一次（當日資料尚未定案），盤後再跑時會一併更正
        #       前一日，確保今天與昨天的資料都是最新、完整的。
        dates = []
        d = target_date
        while len(dates) < 2:
            if d.weekday() < 5:
                dates.append(d)
            d -= timedelta(days=1)

    # 過濾週末並去重排序
    dates = sorted({d for d in dates if d.weekday() < 5})

    if not dates:
        log.info("沒有需要同步的交易日（可能都是週末）。")
        return

    log.info(f"準備同步 {len(dates)} 個交易日 {dates[0]}~{dates[-1]}，共 {len(WATCHLIST)} 檔股票")

    # Step 1: 確保所有股票已在 stock_info
    _upsert_stock_info()

    # Step 2: 拉取 OHLCV
    #   - 純歷史補資料（backfill 或指定較舊日期）→ 只用歷史 API
    #   - 一般每日同步 → 先用歷史 API 取得日期區間內已公布資料（含前一交易日），
    #     再用 OpenAPI 覆蓋「最新交易日」。OpenAPI 只會回最新一日，盤後跑 = 今天，
    #     盤中跑 = 前一交易日；兩種情況都能確保抓到最新可得的資料。
    is_backfill_only = args.backfill_days > 0 or (
        args.date and target_date < date.today() - timedelta(days=3)
    )
    if is_backfill_only:
        log.info("=== STEP 2: OHLCV from TWSE/TPEX 官方歷史 API ===")
        ohlcv_map = _fetch_ohlcv_historical(dates)
    else:
        log.info("=== STEP 2: OHLCV（歷史 API + OpenAPI 覆蓋最新交易日）===")
        ohlcv_map = _fetch_ohlcv_historical(dates)
        date_set = set(dates)
        openapi_map = _fetch_ohlcv_openapi(target_date)
        for code, latest_df in openapi_map.items():
            # OpenAPI 只回最新一日，確認它落在要同步的日期清單內才合併
            latest_df = latest_df[latest_df["trade_date"].isin(date_set)]
            if latest_df.empty:
                continue
            if code in ohlcv_map and not ohlcv_map[code].empty:
                merged = pd.concat([ohlcv_map[code], latest_df])
                ohlcv_map[code] = merged.drop_duplicates("trade_date", keep="last")
            else:
                ohlcv_map[code] = latest_df

    # Step 3: 逐日拉取三大法人並合併寫入
    for d in dates:
        log.info(f"--- 同步 {d} ---")
        chips_listed = _fetch_chips_twse(d)
        chips_otc = _fetch_chips_tpex(d)
        chips = {**chips_listed, **chips_otc}
        _upsert_daily_data(d, ohlcv_map, chips)
        time.sleep(0.5)  # 避免 API 過快觸發限流

    # Step 4: 順帶同步最新一季 EPS（有資料才寫，無資料則跳過，不影響 OHLCV）
    _sync_eps_daily()

    log.info("✅ ETL 同步完畢")


# ------------------------------------------------------------------
# Step 1: Upsert stock_info
# ------------------------------------------------------------------

def _upsert_stock_info():
    for code, (name, market) in WATCHLIST.items():
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
# Step 2a: TWSE / TPEX OpenAPI 拉取今日 OHLCV（官方端點）
# ------------------------------------------------------------------

def _fetch_ohlcv_openapi(target_date: date) -> dict[str, pd.DataFrame]:
    """
    從 TWSE / TPEX OpenAPI 拉取最新交易日 OHLCV。
    - 上市（.TW）  → https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
    - 上櫃（.TWO） → https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
    OpenAPI 只提供最新交易日，不支援日期查詢。
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result: dict[str, pd.DataFrame] = {}

    # --- 上市（TWSE）---
    try:
        r = _api_get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
        twse_rows = {row["Code"]: row for row in r.json()}
        for code in WATCHLIST:
            if not code.endswith(".TW"):
                continue
            raw_code = code.replace(".TW", "")
            row = twse_rows.get(raw_code)
            if not row:
                continue
            def _f(s):
                return float(str(s).replace(",", "") or "0")
            trade_date = _parse_roc_date(row["Date"])
            if not trade_date:
                continue
            df = pd.DataFrame([{
                "trade_date":  trade_date,
                "open_price":  _f(row["OpeningPrice"]),
                "high_price":  _f(row["HighestPrice"]),
                "low_price":   _f(row["LowestPrice"]),
                "close_price": _f(row["ClosingPrice"]),
                "volume":      int(_f(row["TradeVolume"]) / 1000),  # 股→張
            }])
            result[code] = df
        log.info(f"TWSE OpenAPI：取得 {sum(1 for c in result if c.endswith('.TW'))} 檔上市 OHLCV")
    except Exception as e:
        log.warning(f"TWSE OpenAPI OHLCV 失敗：{e}")

    # --- 上櫃（TPEX）---
    otc_codes = [c for c in WATCHLIST if c.endswith(".TWO")]
    if otc_codes:
        try:
            r = _api_get(
                "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                verify=False,
            )
            tpex_rows = {row.get("SecuritiesCompanyCode", "").strip(): row for row in r.json()}
            for code in otc_codes:
                raw_code = code.replace(".TWO", "")
                row = tpex_rows.get(raw_code)
                if not row:
                    continue
                def _f2(s):
                    return float(str(s).replace(",", "") or "0")
                trade_date = _parse_roc_date(row.get("Date", ""))
                if not trade_date:
                    continue
                df = pd.DataFrame([{
                    "trade_date":  trade_date,
                    "open_price":  _f2(row.get("Open", 0)),
                    "high_price":  _f2(row.get("High", 0)),
                    "low_price":   _f2(row.get("Low", 0)),
                    "close_price": _f2(row.get("Close", 0)),
                    "volume":      int(_f2(row.get("TradingShares", 0)) / 1000),
                }])
                result[code] = df
            log.info(f"TPEX OpenAPI：取得 {sum(1 for c in result if c.endswith('.TWO'))} 檔上櫃 OHLCV")
        except Exception as e:
            log.warning(f"TPEX OpenAPI OHLCV 失敗：{e}")

    return result


def _parse_roc_date(roc_str: str):
    """將民國日期字串（1150604）轉換為 date 物件。"""
    try:
        s = str(roc_str).strip()
        if len(s) == 7:                          # 1150604
            year = int(s[:3]) + 1911
            month = int(s[3:5])
            day = int(s[5:7])
            return date(year, month, day)
    except Exception:
        pass
    return None


# ------------------------------------------------------------------
# Step 2b: 官方歷史 OHLCV（TWSE STOCK_DAY / TPEX st43，按股票逐月拉取）
# ------------------------------------------------------------------

def _fetch_ohlcv_historical(dates: list[date]) -> dict[str, pd.DataFrame]:
    """
    使用官方 API 拉取歷史 OHLCV，每次呼叫 = 1 支股票 × 1 個月份。
    - 上市（.TW） → TWSE rwd/zh/afterTrading/STOCK_DAY
    - 上櫃（.TWO）→ TPEX web/stock/aftertrading/daily_trading_info/st43_print.php
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if not dates:
        return {}

    date_set = set(dates)
    start_date, end_date = min(dates), max(dates)

    # 計算需要的月份列表
    months: list[tuple[int, int]] = []
    d = start_date.replace(day=1)
    end_month = end_date.replace(day=1)
    while d <= end_month:
        months.append((d.year, d.month))
        d = d.replace(month=d.month + 1) if d.month < 12 else d.replace(year=d.year + 1, month=1)

    def _parse_price(s) -> float:
        return float(str(s).replace(",", "").strip() or "0")

    result: dict[str, pd.DataFrame] = {}

    for code in WATCHLIST:
        raw_code = code.replace(".TW", "").replace(".TWO", "")
        all_rows: list[dict] = []

        if code.endswith(".TW"):
            for year, month in months:
                url = (
                    f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
                    f"?stockNo={raw_code}&date={year}{month:02d}01&response=json"
                )
                try:
                    r = _api_get(url)
                    data = r.json()
                    if data.get("stat") != "OK" or not data.get("data"):
                        log.debug(f"TWSE STOCK_DAY {raw_code} {year}/{month:02d}：無資料")
                        continue
                    for row in data["data"]:
                        parts = row[0].split("/")
                        if len(parts) != 3:
                            continue
                        td = date(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))
                        if td not in date_set:
                            continue
                        all_rows.append({
                            "trade_date":  td,
                            "open_price":  _parse_price(row[3]),
                            "high_price":  _parse_price(row[4]),
                            "low_price":   _parse_price(row[5]),
                            "close_price": _parse_price(row[6]),
                            "volume":      int(_parse_price(row[1]) / 1000),  # 股→張
                        })
                except Exception as e:
                    log.warning(f"TWSE STOCK_DAY {raw_code} {year}/{month:02d} 失敗：{e}")

        elif code.endswith(".TWO"):
            for year, month in months:
                roc_year = year - 1911
                url = (
                    f"https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info"
                    f"/st43_print.php?l=zh-tw&d={roc_year}/{month:02d}&stkno={raw_code}&s=0,asc,0"
                )
                try:
                    r = _api_get(url, verify=False)
                    if not r.text.strip():
                        log.debug(f"TPEX st43 {raw_code} {year}/{month:02d}：空回應")
                        continue
                    data = r.json()
                    for row in data.get("aaData", []):
                        parts = row[0].split("/")
                        if len(parts) != 3:
                            continue
                        td = date(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))
                        if td not in date_set:
                            continue
                        # st43 欄位：[0]日期, [1]成交股數, [2]成交金額, [3]開盤, [4]最高, [5]最低, [6]收盤
                        all_rows.append({
                            "trade_date":  td,
                            "open_price":  _parse_price(row[3]),
                            "high_price":  _parse_price(row[4]),
                            "low_price":   _parse_price(row[5]),
                            "close_price": _parse_price(row[6]),
                            "volume":      int(_parse_price(row[1]) / 1000),  # 股→張
                        })
                except Exception as e:
                    log.warning(f"TPEX st43 {raw_code} {year}/{month:02d} 失敗：{e}")

        if all_rows:
            result[code] = pd.DataFrame(all_rows)
            log.info(f"{code} 歷史 OHLCV：{len(all_rows)} 筆")
        else:
            log.warning(f"{code} 歷史 OHLCV：無資料")

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
        "https://www.twse.com.tw/rwd/zh/fund/T86"
        f"?response=json&date={date_str}&selectType=ALLBUT0999"
    )
    try:
        resp = _api_get(url)
        data = resp.json()
    except Exception as e:
        log.warning(f"TWSE 三大法人 API 失敗 ({target_date})：{e}")
        return {}

    if data.get("stat") != "OK" or "data" not in data:
        log.warning(f"TWSE 三大法人 API 無資料 ({target_date})")
        return {}

    # 欄位位置曾經改版（舊版16欄「外資買賣超股數」單一欄；新版19欄拆成
    # 「外陸資買賣超股數(不含外資自營商)」+「外資自營商買賣超股數」兩欄需相加）。
    # 依欄位「名稱」動態比對，不寫死位置，兩種格式都能正確解析。
    fields = data.get("fields") or []

    def parse_int(s: str) -> int:
        return int(str(s).replace(",", "").replace("+", "") or "0")

    foreign_idxs = [
        i for i, f in enumerate(fields)
        if "買賣超股數" in f and "外" in f and f != "自營商買賣超股數"
        and "自行買賣" not in f and "避險" not in f
    ]
    invest_idx = fields.index("投信買賣超股數") if "投信買賣超股數" in fields else None
    dealer_idx = fields.index("自營商買賣超股數") if "自營商買賣超股數" in fields else None

    if not foreign_idxs or invest_idx is None or dealer_idx is None:
        log.warning(f"TWSE 三大法人欄位比對失敗 ({target_date})：fields={fields}")
        return {}

    result: dict[str, dict] = {}
    for row in data["data"]:
        code_raw = row[0].strip()
        # 轉換為 .TW 格式
        code = f"{code_raw}.TW"
        if code not in WATCHLIST:
            continue
        try:
            foreign_net = sum(parse_int(row[i]) for i in foreign_idxs)
            invest_net = parse_int(row[invest_idx])
            dealer_net = parse_int(row[dealer_idx])

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
    拉取上櫃股三大法人資料。
    - 當日資料：使用 TPEX OpenAPI（穩定，無 SSL 問題）
    - 歷史資料：使用舊版 Web API（帶日期參數）
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if target_date >= date.today():
        return _fetch_chips_tpex_openapi(target_date)
    else:
        return _fetch_chips_tpex_legacy(target_date)


def _fetch_chips_tpex_openapi(target_date: date) -> dict[str, dict]:
    """TPEX OpenAPI — 只返回最新交易日資料，無需日期參數，無 SSL 問題。"""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
    try:
        resp = _api_get(url, verify=False)
        rows = resp.json()
    except Exception as e:
        log.warning(f"TPEX OpenAPI 失敗 ({target_date})：{e}")
        return {}

    FK = "ForeignInvestorsIncludeMainlandAreaInvestors-Difference"
    IK = "SecuritiesInvestmentTrustCompanies-Difference"
    DK = "Dealers-Difference"

    def p(s) -> int:
        try:
            return int(str(s).replace(",", "").replace("+", "").strip() or "0")
        except ValueError:
            return 0

    result: dict[str, dict] = {}
    for row in rows:
        code = f"{row.get('SecuritiesCompanyCode', '').strip()}.TWO"
        if code not in WATCHLIST:
            continue
        result[code] = {
            "foreign_buy":    p(row.get(FK, 0)),
            "investment_buy": p(row.get(IK, 0)),
            "dealer_buy":     p(row.get(DK, 0)),
        }

    log.info(f"TPEX OpenAPI：成功解析 {len(result)} 筆（{target_date}）")
    return result


def _fetch_chips_tpex_legacy(target_date: date) -> dict[str, dict]:
    """
    TPEX 歷史三大法人 API，用於 backfill。

    注意：舊版 3itrade_print.php 端點已於 TPEX 網站改版後失效（回傳 302 導向
    /errors）。改用新版 insti/dailyTrade 端點。此端點欄位無具名分組（皆為
    「買進股數/賣出股數/買賣超股數」重複標籤），只能依「欄位總數」判斷新舊
    版本區塊位置：
      - 16 欄（5大類）：舊版，foreign=idx4, invest=idx7, dealer=idx8
      - 24 欄（7大類）：新版，foreign=idx4, invest=idx13, dealer=idx22
    以上位置皆已用 TPEX OpenAPI（有具名欄位，但只回最新一日）交叉比對驗證正確。
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    roc_year = target_date.year - 1911
    date_str = f"{roc_year}/{target_date.month:02d}/{target_date.day:02d}"
    url = (
        "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
        f"?date={date_str}&type=Daily&response=json"
    )
    try:
        resp = _api_get(url, verify=False)
        if not resp.text.strip():
            log.debug(f"TPEX {target_date}：空回應（可能假日或停市）")
            return {}
        data = resp.json()
    except Exception as e:
        log.warning(f"TPEX 三大法人 API 失敗 ({target_date})：{e}")
        return {}

    table = next((t for t in data.get("tables", []) if t.get("fields")), None)
    if not table or not table.get("data"):
        log.debug(f"TPEX {target_date}：無資料（可能假日或停市）")
        return {}

    n_fields = len(table["fields"])
    if n_fields == 16:
        f_idx, i_idx, d_idx = 4, 7, 8
    elif n_fields == 24:
        f_idx, i_idx, d_idx = 4, 13, 22
    else:
        log.warning(f"TPEX {target_date}：未知欄位數 {n_fields}，略過")
        return {}

    result: dict[str, dict] = {}
    for row in table["data"]:
        code = f"{row[0].strip()}.TWO"
        if code not in WATCHLIST:
            continue
        try:
            def p(s) -> int:
                return int(str(s).replace(",", "").replace("+", "").strip() or "0")
            result[code] = {
                "foreign_buy":    p(row[f_idx]),
                "investment_buy": p(row[i_idx]),
                "dealer_buy":     p(row[d_idx]),
            }
        except (ValueError, IndexError) as e:
            log.debug(f"解析 TPEX {code} 失敗：{e}")

    log.info(f"TPEX legacy：成功解析 {len(result)} 筆（{target_date}）")
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


# ------------------------------------------------------------------
# Step 4: 每日順帶同步最新一季 EPS（委派給 sync_eps，僅最新一季）
# ------------------------------------------------------------------

def _sync_eps_daily():
    """
    每日 ETL 尾端順帶更新最新一季 EPS：
      - 有資料 → upsert（update-or-insert）到 stock_eps（並補全 stock_info）
      - 無資料 / API 失敗 → 記錄並跳過，不影響上方 OHLCV 同步
    EPS 為季資料，多數日子抓到的是同一季（冪等覆寫），季報公告後會自動更新為新季。
    """
    try:
        # 確保同層的 sync_eps 可被 import
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sync_eps  # noqa: E402

        rows = sync_eps.fetch_all_eps()
        if not rows:
            log.info("EPS：本次無資料，跳過")
            return
        sync_eps._upsert_stock_info(rows)
        sync_eps._upsert_eps(rows)
    except Exception as e:
        log.warning(f"EPS 同步失敗（不影響 OHLCV）：{e}")


if __name__ == "__main__":
    main()
