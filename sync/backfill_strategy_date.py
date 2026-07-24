"""
sync/backfill_strategy_date.py — 回補「已被新資料覆蓋」的歷史交易日 CHOSE/DRIVE 訊號

背景：
  build_strategy.py 判斷資料是否過期，只認「每檔股票最後一筆K棒」是否等於目標
  交易日；一旦該股之後又同步了更新的交易日，用 build_strategy.py 對更早的日期
  重算就會被整批當成舊資料略過（無法回填）。

做法：
  重用 build_strategy.py 的分析函式（analyze_chose / analyze_drive / backtest_3y /
  _upsert_result），但改用「只取到指定日期為止」的K棒序列，還原成「當天收盤時」
  的計算基礎。

使用方式：
  python sync/backfill_strategy_date.py --date 2026-07-13
  python sync/backfill_strategy_date.py --date 2026-07-13 --no-backtest
"""
from __future__ import annotations

import argparse
import sys
import os
import logging
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault(
    "STREAMLIT_SECRETS_TOML",
    str(Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"),
)

import streamlit as st   # noqa: E402
import db                # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_strategy as bs  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _load_bars_as_of(
    stock_code: str,
    as_of: date,
    corp_actions: dict[str, list[tuple[date, float]]] | None = None,
) -> bs.Bars | None:
    df = db.query_df(
        """
        SELECT trade_date, open_price, high_price, low_price, close_price, volume
        FROM stock_daily_data
        WHERE stock_code = :code AND trade_date <= :as_of
        ORDER BY trade_date ASC
        """,
        {"code": stock_code, "as_of": str(as_of)},
    )
    if df.empty:
        return None
    if corp_actions is None:
        corp_actions = bs._fetch_corp_actions(stock_code)
    # bs._apply_corp_actions 以「傳入資料序列的最後一天」為基準，只調整已經發生的事件，
    # 對回補歷史日（as_of 早於事件生效日）天然安全，不會誤調整尚未發生的未來事件。
    df = bs._apply_corp_actions(df, corp_actions.get(stock_code))
    return bs.Bars(df)


def main():
    parser = argparse.ArgumentParser(description="回補指定歷史交易日的 CHOSE/DRIVE 選股訊號")
    parser.add_argument("--date", required=True, help="要回補的交易日 YYYY-MM-DD")
    parser.add_argument("--no-backtest", action="store_true", help="跳過 3 年回測")
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.date)
    log.info(f"回補交易日：{trade_date}")

    bench_bars = _load_bars_as_of(bs.BENCH_CODE, trade_date)
    bench20, bench60, bench_series = bs._benchmark(bench_bars)
    if bench_bars is None:
        log.warning(f"找不到大盤基準 {bs.BENCH_CODE} 的資料，RS 將以 0 基準計算。")

    codes = db.query_df(
        "SELECT DISTINCT stock_code FROM stock_daily_data ORDER BY stock_code"
    )["stock_code"].tolist()
    log.info(f"掃描 {len(codes)} 檔股票…")

    corp_actions = bs._fetch_corp_actions()
    if corp_actions:
        log.info(f"公司行動調整表：{len(corp_actions)} 檔股票有登記減資/分割/合併調整")

    n_chose = n_drive = n_written = n_stale = 0
    stale_codes: list[str] = []
    for code in codes:
        if code == bs.BENCH_CODE:
            continue
        bars = _load_bars_as_of(code, trade_date, corp_actions)
        if bars is None or len(bars) < bs.MIN_ROWS_SCAN:
            continue
        if bars.dates[-1] != trade_date:
            # 該股在 trade_date 當天沒有交易資料（停牌/未上市/尚未同步），略過
            n_stale += 1
            stale_codes.append(code)
            continue

        chose = bs.analyze_chose(bars, bench20)
        drive = bs.analyze_drive(bars, bench60)
        if not chose and not drive:
            continue

        backtest = None
        if chose and not args.no_backtest:
            backtest = bs.backtest_3y(bars, bench_series)

        bs._upsert_result(trade_date, code, chose, drive, backtest)
        n_written += 1
        n_chose += 1 if chose else 0
        n_drive += 1 if drive else 0

    n_cleaned = bs._cleanup_stale_results(trade_date, stale_codes)

    log.info(f"✅ 回補完成：寫入 {n_written} 檔（CHOSE {n_chose} / DRIVE {n_drive}，"
             f"略過 {n_stale} 檔當日無資料，清除 {n_cleaned} 筆與行情資料矛盾的殘留紀錄）@ {trade_date}")


if __name__ == "__main__":
    main()
