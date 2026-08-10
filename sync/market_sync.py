"""
sync/market_sync.py — 單日「全市場」OHLCV 抓取與缺漏補齊

官方端點（皆認日期，可取任意歷史交易日的全市場收盤行情）：
  - 上市：https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999
  - 上櫃：https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date=YYYY/MM/DD&type=EW

用途：
  - 全市場單日同步 / 歷史回補（一次呼叫＝一天全市場）
  - 選股前自動補齊「缺漏交易日」（ensure_history）

寫入：
  - stock_daily_data（只更新 OHLCV/volume，不會覆寫既有三大法人欄位）
  - stock_info（代號/名稱/市場別，確保 daily_strategy_results 的 FK 成立）

注意：
  - 只收 4 位數字代號（一般股票 + 如 0050 等），排除債券/權證類雜項。
  - volume 以「張」儲存（成交股數 / 1000），與既有資料一致。
"""
from __future__ import annotations

import re
import sys
import time
import logging
from datetime import date, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

log = logging.getLogger(__name__)

TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
API_DELAY = 1.0
_CODE_RE = re.compile(r"^\d{4}$")          # 僅 4 位數字代號
# 分開設上市/上櫃各自的門檻（而不是用合併總數判斷）：TWSE 上市本身就有 1000+ 檔，
# 若只用「合併總數 >= 500」判斷完整性，會發生「上市抓到、上櫃整個抓失敗」時，
# 合併總數依然輕鬆超過 500，被誤判成「這天已經是全市場資料」，之後 ensure_history()
# 就永遠不會重試那一天，上櫃資料就永久性地少了一半市場。
# 2026-08-07 就是實際踩到的案例：上市 1089 檔、上櫃 0 檔，合併 1089 >= 500 被判定完整。
COMPLETE_THRESHOLD_TW = 500                 # 上市單日 >= 此股票數視為「已完整抓取」
COMPLETE_THRESHOLD_TWO = 300                 # 上櫃單日 >= 此股票數視為「已完整抓取」


def _num(s):
    """把帶逗號/空白的數字字串轉 float；'--'、'除息'、空值等 → None。"""
    try:
        v = str(s).replace(",", "").strip()
        return float(v)
    except (ValueError, TypeError, AttributeError):
        return None


def _pick_table(tables: list, must_have: list[str]):
    """從 TWSE/TPEX 的 tables 陣列挑出欄位含 must_have 全部關鍵字的表。"""
    for t in tables:
        fields = t.get("fields") or []
        if all(any(k in f for f in fields) for k in must_have):
            return t
    return None


# ------------------------------------------------------------------
# 單日全市場抓取
# ------------------------------------------------------------------

def fetch_twse_day(d: date) -> dict[str, dict]:
    """上市單日全市場。回傳 {code.TW: {open,high,low,close,volume,name}}。"""
    params = {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"}
    try:
        r = requests.get(TWSE_URL, params=params, headers=HEADERS, timeout=20)
        j = r.json()
    except Exception as e:
        log.warning(f"TWSE MI_INDEX {d} 失敗：{e}")
        return {}
    if j.get("stat") != "OK":
        return {}
    table = _pick_table(j.get("tables") or [], ["證券代號", "收盤價"])
    if not table:
        return {}
    # 欄位順序：0代號 1名稱 2成交股數 3筆數 4金額 5開 6高 7低 8收 ...
    out: dict[str, dict] = {}
    for row in table.get("data") or []:
        code = str(row[0]).strip()
        if not _CODE_RE.match(code):
            continue
        o, h, l, c = _num(row[5]), _num(row[6]), _num(row[7]), _num(row[8])
        vol = _num(row[2])
        if c is None or o is None:
            continue
        out[f"{code}.TW"] = {
            "name": str(row[1]).strip(), "open": o, "high": h, "low": l,
            "close": c, "volume": int((vol or 0) / 1000),
        }
    return out


def fetch_tpex_day(d: date) -> dict[str, dict]:
    """上櫃單日全市場。回傳 {code.TWO: {...}}。"""
    roc = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
    params = {"date": roc, "type": "EW", "response": "json"}
    try:
        r = requests.get(TPEX_URL, params=params, headers=HEADERS, timeout=25, verify=False)
        j = r.json()
    except Exception as e:
        log.warning(f"TPEX dailyQuotes {d} 失敗：{e}")
        return {}
    table = _pick_table(j.get("tables") or [], ["代號", "收盤"])
    if not table:
        return {}
    # 欄位：0代號 1名稱 2收盤 3漲跌 4開盤 5最高 6最低 7均價 8成交股數 ...
    out: dict[str, dict] = {}
    for row in table.get("data") or []:
        code = str(row[0]).strip()
        if not _CODE_RE.match(code):
            continue
        c, o, h, l = _num(row[2]), _num(row[4]), _num(row[5]), _num(row[6])
        vol = _num(row[8])
        if c is None or o is None:
            continue
        out[f"{code}.TWO"] = {
            "name": str(row[1]).strip(), "open": o, "high": h, "low": l,
            "close": c, "volume": int((vol or 0) / 1000),
        }
    return out


def fetch_market_day(d: date) -> dict[str, dict]:
    """合併上市+上櫃單日全市場；非交易日（兩邊皆空）回傳 {}。"""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    data = fetch_twse_day(d)
    time.sleep(API_DELAY)
    data.update(fetch_tpex_day(d))
    time.sleep(API_DELAY)
    return data


# ------------------------------------------------------------------
# 寫入
# ------------------------------------------------------------------

_UPSERT_CHUNK = 300   # 每次 INSERT 包多少列：把「每檔股票一次連線」降成每幾百檔一次


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _upsert_info(data: dict[str, dict]):
    items = list(data.items())
    for chunk in _chunked(items, _UPSERT_CHUNK):
        values_sql, params = [], {}
        for j, (code, row) in enumerate(chunk):
            market = "上櫃" if code.endswith(".TWO") else "上市"
            values_sql.append(f"(:code{j}, :name{j}, :market{j})")
            params[f"code{j}"] = code
            params[f"name{j}"] = row["name"] or code
            params[f"market{j}"] = market
        sql = (
            "INSERT INTO stock_info (stock_code, stock_name, market_type) "
            f"VALUES {', '.join(values_sql)} "
            "ON DUPLICATE KEY UPDATE stock_name = VALUES(stock_name)"
        )
        db.execute(sql, params)


def _upsert_ohlcv(d: date, data: dict[str, dict]):
    """只更新 OHLCV/volume；ON DUPLICATE 不動三大法人欄位（避免覆寫成 0）。
    批次組成單一多列 INSERT，把每檔股票各一次連線降成每 _UPSERT_CHUNK 檔一次，
    大幅減少對 TiDB Cloud 的網路往返次數（原本逐列寫入單一天要花數分鐘以上）。"""
    items = list(data.items())
    for chunk in _chunked(items, _UPSERT_CHUNK):
        values_sql, params = [], {}
        for j, (code, row) in enumerate(chunk):
            values_sql.append(f"(:code{j}, :dt{j}, :op{j}, :hp{j}, :lp{j}, :cp{j}, :vol{j}, 0, 0, 0)")
            params[f"code{j}"] = code
            params[f"dt{j}"] = str(d)
            params[f"op{j}"] = row["open"]
            params[f"hp{j}"] = row["high"]
            params[f"lp{j}"] = row["low"]
            params[f"cp{j}"] = row["close"]
            params[f"vol{j}"] = row["volume"]
        sql = (
            "INSERT INTO stock_daily_data "
            "(stock_code, trade_date, open_price, high_price, low_price, "
            " close_price, volume, foreign_buy, investment_buy, dealer_buy) "
            f"VALUES {', '.join(values_sql)} "
            "ON DUPLICATE KEY UPDATE "
            "open_price = VALUES(open_price), high_price = VALUES(high_price), "
            "low_price = VALUES(low_price), close_price = VALUES(close_price), "
            "volume = VALUES(volume)"
        )
        db.execute(sql, params)


def sync_day(d: date) -> int:
    """抓取並寫入單一交易日全市場。回傳寫入股票數（0 = 非交易日）。"""
    data = fetch_market_day(d)
    if not data:
        return 0
    n_tw = sum(1 for c in data if c.endswith(".TW"))
    n_two = sum(1 for c in data if c.endswith(".TWO"))
    if n_tw < COMPLETE_THRESHOLD_TW:
        log.warning(f"⚠️ {d} 上市(TWSE)只抓到 {n_tw} 檔，疑似該交易所端點失敗或資料不完整")
    if n_two < COMPLETE_THRESHOLD_TWO:
        log.warning(f"⚠️ {d} 上櫃(TPEX)只抓到 {n_two} 檔，疑似該交易所端點失敗或資料不完整")
    _upsert_info(data)
    _upsert_ohlcv(d, data)
    _detect_price_anomalies(d)
    return len(data)


# ------------------------------------------------------------------
# 公司行動（減資/分割/合併）異常偵測：只記錄警告，不自動修改資料
#
# 沒有可靠的官方 API 能回溯查詢任意歷史日期的減資/分割事件（已實測 TWSE
# 除權除息表、減資恢復買賣參考價兩支 API，前者不涵蓋減資、後者不支援指定
# 歷史日期查詢），因此改用「單日價格斷層偵測 + 人工確認寫入 stock_corp_actions」
# 的半自動流程：偵測到就記 log，人工確認屬實後手動補一筆調整係數。
# ------------------------------------------------------------------

ANOMALY_PCT_THRESHOLD = 0.30   # 單日收盤變動 >= 30% 視為疑似公司行動


def _detect_price_anomalies(d: date):
    """比對 d 這天所有股票的收盤價，與其「前一個既有交易日」（可能因停牌隔了好幾週）收盤價，
    抓出疑似減資/分割/合併造成的價格斷層。"""
    window_start = d - timedelta(days=120)   # 涵蓋常見的減資/分割停牌期（通常數週）
    df = db.query_df(
        """
        SELECT stock_code, trade_date, close_price, prev_close, prev_date FROM (
            SELECT stock_code, trade_date, close_price,
                   LAG(close_price) OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_close,
                   LAG(trade_date) OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_date
            FROM stock_daily_data
            WHERE trade_date BETWEEN :start AND :d
        ) t
        WHERE trade_date = :d AND prev_close IS NOT NULL
        """,
        {"start": str(window_start), "d": str(d)},
    )
    if df.empty:
        return

    close = df["close_price"].astype(float)
    prev = df["prev_close"].astype(float)
    pct_chg = (close - prev) / prev
    flagged = df[pct_chg.abs() >= ANOMALY_PCT_THRESHOLD].copy()
    if flagged.empty:
        return

    for _, row in flagged.iterrows():
        c, p = float(row["close_price"]), float(row["prev_close"])
        factor = c / p
        log.warning(
            f"⚠️ 疑似公司行動（減資/分割/合併）：{row['stock_code']} "
            f"{row['prev_date']} 收盤 {p} -> {row['trade_date']} 收盤 {c} "
            f"（變動 {(c-p)/p*100:+.1f}%，估計調整係數 {factor:.6f}）。"
            f"若確認屬實，請手動寫入 stock_corp_actions（ex_date={row['trade_date']}, adjust_factor={factor:.6f}）。"
        )


# ------------------------------------------------------------------
# 缺漏補齊：從 end_date 往回補足 n 個交易日（已有全市場資料的日子跳過）
# ------------------------------------------------------------------

def _complete_dates() -> set:
    """已具備『全市場』資料的交易日集合：上市、上櫃各自的股票數都要達到門檻，
    避免其中一個交易所抓失敗時，被另一邊撐大的合併總數誤判成「已完整」。"""
    df = db.query_df(
        """
        SELECT trade_date FROM stock_daily_data
        GROUP BY trade_date
        HAVING SUM(CASE WHEN stock_code LIKE '%.TW' THEN 1 ELSE 0 END) >= :thr_tw
           AND SUM(CASE WHEN stock_code LIKE '%.TWO' THEN 1 ELSE 0 END) >= :thr_two
        """,
        {"thr_tw": COMPLETE_THRESHOLD_TW, "thr_two": COMPLETE_THRESHOLD_TWO},
    )
    out = set()
    for v in df["trade_date"].tolist():
        out.add(v if isinstance(v, date) else date.fromisoformat(str(v)))
    return out


def ensure_history(end_date: date, n_trading_days: int) -> int:
    """
    從 end_date 往回，確保有 n_trading_days 個『全市場』交易日資料。
      - 該日已是全市場資料 → 計入、跳過
      - 該日缺漏（無資料或僅部分股票）→ 抓全市場寫入；抓到資料才算交易日，
        抓不到（假日）則跳過不計
    回傳：本次實際補進的交易日數。
    """
    complete = _complete_dates()
    collected = filled = 0
    d = end_date
    guard = 0
    max_guard = n_trading_days * 3 + 40   # 約略涵蓋假日/週末，避免無限迴圈
    while collected < n_trading_days and guard < max_guard:
        guard += 1
        if d.weekday() < 5:                       # 只看平日
            if d in complete:
                collected += 1
            else:
                n = sync_day(d)
                if n > 0:                         # 有資料 = 交易日
                    collected += 1
                    filled += 1
                    log.info(f"  補齊 {d}：全市場 {n} 檔")
                # n == 0 視為假日，略過不計
        d -= timedelta(days=1)
    if filled:
        log.info(f"缺漏補齊完成：新增 {filled} 個交易日（目標 {n_trading_days} 日）")
    return filled
