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
COMPLETE_THRESHOLD = 500                    # 單日 >= 此股票數視為「全市場已存在」


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

def _upsert_info(data: dict[str, dict]):
    for code, row in data.items():
        market = "上櫃" if code.endswith(".TWO") else "上市"
        db.execute(
            """
            INSERT INTO stock_info (stock_code, stock_name, market_type)
            VALUES (:code, :name, :market)
            ON DUPLICATE KEY UPDATE stock_name = VALUES(stock_name)
            """,
            {"code": code, "name": row["name"] or code, "market": market},
        )


def _upsert_ohlcv(d: date, data: dict[str, dict]):
    """只更新 OHLCV/volume；ON DUPLICATE 不動三大法人欄位（避免覆寫成 0）。"""
    for code, row in data.items():
        db.execute(
            """
            INSERT INTO stock_daily_data
                (stock_code, trade_date, open_price, high_price, low_price,
                 close_price, volume, foreign_buy, investment_buy, dealer_buy)
            VALUES (:code, :dt, :op, :hp, :lp, :cp, :vol, 0, 0, 0)
            ON DUPLICATE KEY UPDATE
                open_price  = VALUES(open_price),
                high_price  = VALUES(high_price),
                low_price   = VALUES(low_price),
                close_price = VALUES(close_price),
                volume      = VALUES(volume)
            """,
            {"code": code, "dt": str(d), "op": row["open"], "hp": row["high"],
             "lp": row["low"], "cp": row["close"], "vol": row["volume"]},
        )


def sync_day(d: date) -> int:
    """抓取並寫入單一交易日全市場。回傳寫入股票數（0 = 非交易日）。"""
    data = fetch_market_day(d)
    if not data:
        return 0
    _upsert_info(data)
    _upsert_ohlcv(d, data)
    return len(data)


# ------------------------------------------------------------------
# 缺漏補齊：從 end_date 往回補足 n 個交易日（已有全市場資料的日子跳過）
# ------------------------------------------------------------------

def _complete_dates() -> set:
    """已具備『全市場』資料的交易日集合（單日股票數 >= 門檻）。"""
    df = db.query_df(
        """
        SELECT trade_date FROM stock_daily_data
        GROUP BY trade_date HAVING COUNT(*) >= :thr
        """,
        {"thr": COMPLETE_THRESHOLD},
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
