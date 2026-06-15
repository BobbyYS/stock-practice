"""
sync/sync_intraday.py — 盤中即時報價同步腳本

功能：
  1. 從 TWSE / TPEX MIS 即時報價 API（mis.twse.com.tw）拉取監控清單當下快照
  2. Upsert 至 TiDB Cloud：stock_intraday_quotes（每檔僅保留最新一筆，每次覆蓋）

與 sync_daily.py 的差異：
  - sync_daily 抓「盤後定案」的每日 K 線 / 三大法人 → stock_daily_data
  - sync_intraday 抓「盤中即時」快照（現價、開高低、累積量、最佳買賣價）→ stock_intraday_quotes
  外部策略引擎 / Streamlit 盤中分析直接讀 stock_intraday_quotes 即可。

使用方式（本地）：
  python sync/sync_intraday.py

GitHub Actions 排程：
  見 .github/workflows/intraday.yml（台北 09:00–13:30 每 5 分鐘）。

注意：
  MIS 即時報價公開端點約有 15~20 秒延遲，且非交易時段會回報「最近一次」快照。
"""
from __future__ import annotations

import sys
import os
import time
import logging
from datetime import date
from pathlib import Path

import requests

# 讓腳本可載入 db.py（專案根目錄）與 sync_daily.py（同層，共用 WATCHLIST）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SYNC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_SYNC_DIR))

# 在非 Streamlit 環境下模擬 st.secrets（從 toml 直接讀取）
os.environ.setdefault(
    "STREAMLIT_SECRETS_TOML",
    str(_PROJECT_ROOT / ".streamlit" / "secrets.toml"),
)

import streamlit as st          # noqa: E402  (需在 secrets 設定後才 import)
import db                       # noqa: E402
from sync_daily import WATCHLIST  # noqa: E402  (單一來源的監控清單)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# MIS 即時報價 API 設定
# ------------------------------------------------------------------

MIS_BASE = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_WARMUP = "https://mis.twse.com.tw/stock/fibest.jsp?lang=zh_tw"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# MIS 單次請求建議 channel 數上限（避免被拒），保守批次處理。
BATCH_SIZE = 50


def _to_channel(code: str) -> str:
    """將 WATCHLIST 代碼轉為 MIS channel：上市→tse_、上櫃→otc_，後綴一律 .tw。"""
    raw = code.replace(".TWO", "").replace(".TW", "")
    prefix = "otc" if code.endswith(".TWO") else "tse"
    return f"{prefix}_{raw}.tw"


def _channel_to_code(item: dict) -> str | None:
    """從 MIS 回傳項目還原成 WATCHLIST 代碼（.TW / .TWO）。"""
    raw = str(item.get("c", "")).strip()
    ex = str(item.get("ex", "")).strip()  # 'tse' or 'otc'
    if not raw:
        return None
    suffix = ".TWO" if ex == "otc" else ".TW"
    return f"{raw}{suffix}"


def _num(s) -> float | None:
    """把 MIS 字串數值轉 float；'-'、''、'0.0000' 以外的非法值回 None。"""
    try:
        v = float(str(s).replace(",", "").strip())
        return v
    except (ValueError, TypeError, AttributeError):
        return None


def _first_quote(packed: str) -> float | None:
    """MIS 的最佳五檔以底線分隔（如 '590.0_589.0_...'），取第一檔。"""
    if not packed:
        return None
    return _num(packed.split("_")[0])


# ------------------------------------------------------------------
# 抓取即時報價
# ------------------------------------------------------------------

def fetch_realtime_quotes(codes: list[str]) -> dict[str, dict]:
    """
    回傳 {stock_code: {last_price, open_price, high_price, low_price, prev_close,
                       change_amount, change_pct, volume, bid_price, ask_price,
                       quote_time, trade_date}}
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # 暖機：先 GET 一次取得 cookie，提升 API 成功率（best-effort）
    try:
        session.get(MIS_WARMUP, timeout=10)
    except Exception as e:
        log.debug(f"MIS 暖機請求失敗（忽略）：{e}")

    result: dict[str, dict] = {}

    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        ex_ch = "|".join(_to_channel(c) for c in batch)
        params = {"ex_ch": ex_ch, "json": "1", "delay": "0"}
        try:
            resp = session.get(MIS_BASE, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            log.warning(f"MIS API 失敗（batch {i // BATCH_SIZE}）：{e}")
            continue

        for item in data.get("msgArray", []):
            code = _channel_to_code(item)
            if not code or code not in WATCHLIST:
                continue

            last = _num(item.get("z"))
            if last is None:
                # 無即時成交（盤前/無撮合）→ 以最佳買價、再退而求其次以開盤價回補
                last = _first_quote(item.get("b", "")) or _num(item.get("o"))

            prev_close = _num(item.get("y"))
            change_amt = (
                round(last - prev_close, 2)
                if (last is not None and prev_close is not None)
                else None
            )
            change_pct = (
                round((last - prev_close) / prev_close * 100, 2)
                if (last is not None and prev_close not in (None, 0))
                else None
            )

            # 交易日期：MIS 'd' 欄位格式 YYYYMMDD
            d_raw = str(item.get("d", "")).strip()
            try:
                trade_dt = date(int(d_raw[:4]), int(d_raw[4:6]), int(d_raw[6:8]))
            except (ValueError, IndexError):
                trade_dt = date.today()

            result[code] = {
                "last_price":    last,
                "open_price":    _num(item.get("o")),
                "high_price":    _num(item.get("h")),
                "low_price":     _num(item.get("l")),
                "prev_close":    prev_close,
                "change_amount": change_amt,
                "change_pct":    change_pct,
                "volume":        int(_num(item.get("v")) or 0),  # 累積成交張數
                "bid_price":     _first_quote(item.get("b", "")),
                "ask_price":     _first_quote(item.get("a", "")),
                "quote_time":    str(item.get("t", "")).strip() or None,
                "trade_date":    trade_dt,
            }

        time.sleep(1.0)  # 批次間稍作間隔，避免限流

    return result


# ------------------------------------------------------------------
# Upsert stock_intraday_quotes
# ------------------------------------------------------------------

def _upsert_quotes(quotes: dict[str, dict]):
    for code, q in quotes.items():
        db.execute(
            """
            INSERT INTO stock_intraday_quotes
                (stock_code, trade_date, last_price, open_price, high_price,
                 low_price, prev_close, change_amount, change_pct, volume,
                 bid_price, ask_price, quote_time)
            VALUES
                (:code, :dt, :last, :op, :hp, :lp, :pc, :chg, :pct, :vol,
                 :bid, :ask, :qt)
            ON DUPLICATE KEY UPDATE
                trade_date    = VALUES(trade_date),
                last_price    = VALUES(last_price),
                open_price    = VALUES(open_price),
                high_price    = VALUES(high_price),
                low_price     = VALUES(low_price),
                prev_close    = VALUES(prev_close),
                change_amount = VALUES(change_amount),
                change_pct    = VALUES(change_pct),
                volume        = VALUES(volume),
                bid_price     = VALUES(bid_price),
                ask_price     = VALUES(ask_price),
                quote_time    = VALUES(quote_time)
            """,
            {
                "code": code,
                "dt":   str(q["trade_date"]),
                "last": q["last_price"],
                "op":   q["open_price"],
                "hp":   q["high_price"],
                "lp":   q["low_price"],
                "pc":   q["prev_close"],
                "chg":  q["change_amount"],
                "pct":  q["change_pct"],
                "vol":  q["volume"],
                "bid":  q["bid_price"],
                "ask":  q["ask_price"],
                "qt":   q["quote_time"],
            },
        )
    log.info(f"stock_intraday_quotes upsert：{len(quotes)} 筆")


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main():
    codes = list(WATCHLIST.keys())
    log.info(f"準備抓取 {len(codes)} 檔即時報價")

    quotes = fetch_realtime_quotes(codes)
    if not quotes:
        log.warning("未取得任何即時報價（可能非交易時段或 API 暫時不可用）。")
        return

    _upsert_quotes(quotes)

    # 摘要輸出，方便在 log 確認
    for code, q in quotes.items():
        log.info(
            f"  {code} {WATCHLIST[code][0]}：{q['last_price']} "
            f"({q['change_pct']:+.2f}%)  量 {q['volume']}  @ {q['quote_time']}"
            if q["change_pct"] is not None
            else f"  {code} {WATCHLIST[code][0]}：{q['last_price']}  @ {q['quote_time']}"
        )

    log.info("✅ 盤中即時報價同步完畢")


if __name__ == "__main__":
    main()
