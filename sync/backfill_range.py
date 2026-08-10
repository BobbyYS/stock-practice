"""
sync/backfill_range.py — 批次重算一段日期範圍內的 CHOSE/DRIVE/3Y 回測

背景：
  backfill_strategy_date.py 每個日期都會對全市場每檔股票各查一次 DB（2168 檔 ×
  1 次 DB 往返），重算一段長區間（例如近一年 ~245 個交易日）等於要跑 245 次，
  單日已知約 6 分鐘 → 245 天要接近一天，且對 TiDB Cloud 下數十萬次查詢。

做法：
  改成「每檔股票只查一次 DB」（股票 outer、日期 inner），把整段區間的技術指標
  滾動計算與訊號判斷都留在記憶體裡處理，重用 build_strategy.py 既有、已驗證過的
  analyze_chose / analyze_drive / backtest_3y / _upsert_result，不重寫任何判斷邏輯。

公司行動（減資/分割/合併）正確性：
  _apply_corp_actions 的調整基準是「傳入資料的最後一天」，只套用「已經發生」的
  事件（ex_date <= 最後一天），刻意避免把「當時還沒發生的未來事件」套用到過去。
  若整段歷史只查一次、只調整一次，遇到「調整事件的 ex_date 落在回補區間之後」
  的股票，就會把區間內每一天都當成「事件已發生」來算技術指標，這其實是把未來
  資訊洩漏進歷史計算——跟這次修 CHOSE 進場窗口的問題是同一類坑。
  因此：只有「該股票所有公司行動事件的 ex_date 都早於回補區間起點」時，才用
  快速路徑（整段只調整一次）；否則該股票改用逐日重新套用調整的慢路徑（一樣不
  必重新查 DB，只是多做幾次向量化的 pandas 運算，成本可接受）。

使用方式：
  python sync/backfill_range.py --start 2025-08-07 --end 2026-08-07
  python sync/backfill_range.py --start 2025-08-07 --end 2026-08-07 --dry-run
  python sync/backfill_range.py --start 2025-08-07 --end 2026-08-07 --no-backtest
"""
from __future__ import annotations

import argparse
import sys
import os
import logging
from datetime import date
from pathlib import Path

import pandas as pd

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


# ------------------------------------------------------------------
# 輔助：不重查 DB，直接從已載入的 Bars 截出「前 idx+1 天」的視圖
# ------------------------------------------------------------------

def _truncate(bars: bs.Bars, idx: int) -> bs.Bars:
    b = bs.Bars.__new__(bs.Bars)
    b.dates = bars.dates[: idx + 1]
    b.close = bars.close.iloc[: idx + 1]
    b.high = bars.high.iloc[: idx + 1]
    b.low = bars.low.iloc[: idx + 1]
    b.open = bars.open.iloc[: idx + 1]
    b.vol = bars.vol.iloc[: idx + 1]
    return b


def _raw_query(code: str) -> pd.DataFrame:
    return db.query_df(
        """
        SELECT trade_date, open_price, high_price, low_price, close_price, volume
        FROM stock_daily_data
        WHERE stock_code = :code
        ORDER BY trade_date ASC
        """,
        {"code": code},
    )


def _bench_series(bench_bars: bs.Bars | None, period: int) -> dict:
    if bench_bars is None:
        return {}
    roc = bench_bars.close.pct_change(period)
    return {d: (float(v) if pd.notna(v) else 0.0) for d, v in zip(bench_bars.dates, roc)}


def main():
    parser = argparse.ArgumentParser(description="批次重算一段日期範圍內的 CHOSE/DRIVE/3Y 回測")
    parser.add_argument("--start", required=True, help="回補起始日 YYYY-MM-DD（含）")
    parser.add_argument("--end", required=True, help="回補結束日 YYYY-MM-DD（含）")
    parser.add_argument("--no-backtest", action="store_true", help="跳過 3 年回測")
    parser.add_argument("--dry-run", action="store_true", help="只計算不寫入 DB，僅印出統計")
    parser.add_argument("--progress-every", type=int, default=100, help="每處理幾檔股票印一次進度")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    if start_date > end_date:
        log.error("--start 不能晚於 --end")
        return

    log.info(f"=== 批次回補 {start_date} ~ {end_date}（dry_run={args.dry_run}）===")

    # 大盤基準（全歷史一次載入，供每個目標日查表用）
    bench_bars = bs._load_bars(bs.BENCH_CODE)
    bench20_by_date = _bench_series(bench_bars, bs.RS_PERIOD_CHOSE)
    bench60_by_date = _bench_series(bench_bars, bs.RS_PERIOD_DRIVE)
    if bench_bars is None:
        log.warning(f"找不到大盤基準 {bs.BENCH_CODE}，RS 將以 0 基準計算。")

    # 目標日期清單：區間內實際存在全市場資料的交易日
    target_dates = db.query_df(
        """
        SELECT DISTINCT trade_date FROM stock_daily_data
        WHERE trade_date BETWEEN :s AND :e
        ORDER BY trade_date
        """,
        {"s": str(start_date), "e": str(end_date)},
    )["trade_date"].tolist()
    target_dates = [d if isinstance(d, date) else date.fromisoformat(str(d)) for d in target_dates]
    target_set = set(target_dates)
    log.info(f"區間內共 {len(target_dates)} 個交易日")

    # 既有紀錄（用來判斷「重算後不再觸發」的舊資料要不要刪除）
    existing = db.query_df(
        """
        SELECT trade_date, stock_code FROM daily_strategy_results
        WHERE trade_date BETWEEN :s AND :e
        """,
        {"s": str(start_date), "e": str(end_date)},
    )
    existing_keys = {
        (row["trade_date"] if isinstance(row["trade_date"], date) else date.fromisoformat(str(row["trade_date"])),
         row["stock_code"])
        for _, row in existing.iterrows()
    }
    log.info(f"區間內既有紀錄 {len(existing_keys)} 筆（重算後仍無訊號者將被清除）")

    codes = db.query_df(
        "SELECT DISTINCT stock_code FROM stock_daily_data ORDER BY stock_code"
    )["stock_code"].tolist()

    corp_actions = bs._fetch_corp_actions()
    if corp_actions:
        log.info(f"公司行動調整表：{len(corp_actions)} 檔股票有登記減資/分割/合併調整")

    to_delete: dict[date, list[str]] = {}
    n_skipped_stock = 0
    n_stocks_done = 0

    for code in codes:
        if code == bs.BENCH_CODE:
            continue
        n_stocks_done += 1
        if args.progress_every and n_stocks_done % args.progress_every == 0:
            n_del_so_far = sum(len(v) for v in to_delete.values())
            log.info(f"進度：已處理 {n_stocks_done}/{len(codes)} 檔股票，"
                     f"寫入 {_STATS['written']} 筆（CHOSE {_STATS['chose']} / DRIVE {_STATS['drive']}），"
                     f"待清除 {n_del_so_far} 筆")

        raw_df = _raw_query(code)
        if raw_df.empty or len(raw_df) < bs.MIN_ROWS_SCAN:
            n_skipped_stock += 1
            continue

        actions = corp_actions.get(code)
        risky = bool(actions) and any(ex_date > start_date for ex_date, _factor in actions)

        if not risky:
            adj_df = bs._apply_corp_actions(raw_df.copy(), actions)
            bars_full = bs.Bars(adj_df)
            date_idx = {d: i for i, d in enumerate(bars_full.dates)}

            for D in target_dates:
                if D not in date_idx:
                    continue
                idx = date_idx[D]
                if idx + 1 < bs.MIN_ROWS_SCAN:
                    continue
                truncated = _truncate(bars_full, idx)
                _evaluate_and_write(
                    truncated, D, code, bench20_by_date, bench60_by_date,
                    args, existing_keys, to_delete,
                )
        else:
            # 慢路徑：該股票有公司行動事件落在回補區間之後，逐日重新套用調整，
            # 避免把「當時還沒發生的事件」提早套用到過去的技術指標計算。
            raw_dates = raw_df["trade_date"].tolist()
            raw_dates = [d if isinstance(d, date) else date.fromisoformat(str(d)) for d in raw_dates]
            for D in target_dates:
                if D not in raw_dates:
                    continue
                idx = raw_dates.index(D)
                if idx + 1 < bs.MIN_ROWS_SCAN:
                    continue
                sliced = raw_df.iloc[: idx + 1].copy()
                adj = bs._apply_corp_actions(sliced, actions)
                truncated = bs.Bars(adj)
                _evaluate_and_write(
                    truncated, D, code, bench20_by_date, bench60_by_date,
                    args, existing_keys, to_delete,
                )

    if to_delete and not args.dry_run:
        n_del_total = 0
        for D, delcodes in to_delete.items():
            db.execute(
                "DELETE FROM daily_strategy_results WHERE trade_date = :dt AND stock_code IN :codes",
                {"dt": str(D), "codes": tuple(delcodes)},
            )
            n_del_total += len(delcodes)
        log.info(f"清除重算後不再有訊號的舊紀錄：{n_del_total} 筆")
    elif to_delete:
        n_del_total = sum(len(v) for v in to_delete.values())
        log.info(f"[dry-run] 會清除 {n_del_total} 筆重算後不再有訊號的舊紀錄")

    log.info(f"✅ 批次回補完成：略過 {n_skipped_stock} 檔資料不足的股票")


# ------------------------------------------------------------------
# 對單一 (股票, 日期) 評估訊號並寫入（或收集刪除）
# ------------------------------------------------------------------

_STATS = {"written": 0, "chose": 0, "drive": 0}


def _evaluate_and_write(
    truncated: bs.Bars,
    D: date,
    code: str,
    bench20_by_date: dict,
    bench60_by_date: dict,
    args,
    existing_keys: set,
    to_delete: dict[date, list[str]],
):
    bench20 = bench20_by_date.get(D, 0.0)
    bench60 = bench60_by_date.get(D, 0.0)

    chose = bs.analyze_chose(truncated, bench20)
    drive = bs.analyze_drive(truncated, bench60)

    if not chose and not drive:
        if (D, code) in existing_keys:
            to_delete.setdefault(D, []).append(code)
        return

    backtest = None
    if chose and not args.no_backtest:
        backtest = bs.backtest_3y(truncated, bench20_by_date)

    _STATS["written"] += 1
    _STATS["chose"] += 1 if chose else 0
    _STATS["drive"] += 1 if drive else 0

    if not args.dry_run:
        bs._upsert_result(D, code, chose, drive, backtest)


if __name__ == "__main__":
    main()
    log.info(f"總計：寫入 {_STATS['written']} 筆（CHOSE {_STATS['chose']} / DRIVE {_STATS['drive']}）")
