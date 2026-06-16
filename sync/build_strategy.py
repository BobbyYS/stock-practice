"""
sync/build_strategy.py — 每日策略選股引擎（CHOSE / DRIVE + 3 年回測）

移植自 參考資料/main (1).py，改為：
  - 純 pandas 計算（不依賴 yfinance / pandas_ta）
  - 資料來源改讀 TiDB 的 stock_daily_data（官方 TWSE/TPEX 同步而來）
  - 結果寫入 daily_strategy_results → 供「每日選股雷達」顯示
  - 移除「股價 > 20」篩選條件
  - 修正成交量單位：stock_daily_data.volume 單位為「張」，
    參考程式門檻是「股」(80 萬/100 萬)，換算為 800 / 1000 張

使用方式：
  python sync/build_strategy.py                 # 以資料庫最新交易日計算
  python sync/build_strategy.py --date 2026-06-13
  python sync/build_strategy.py --no-backtest    # 跳過 3 年回測（較快）

注意：
  - CHOSE/DRIVE 需要約 200~250 個交易日歷史；3 年回測需要約 4 年。
    資料不足的個股會自動略過（不會報錯）。
"""
from __future__ import annotations

import argparse
import sys
import os
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault(
    "STREAMLIT_SECRETS_TOML",
    str(Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"),
)

import streamlit as st   # noqa: E402
import db                # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 策略參數（移植自參考程式，成交量改為「張」）
# ------------------------------------------------------------------

BENCH_CODE = "0050.TW"          # 大盤基準（本身會出現在全市場資料中）
MIN_VOLUME_CHOSE = 800          # 20 日均量門檻（張）；原 800,000 股 = 800 張
MIN_VOLUME_DRIVE = 1000         # 原 1,000,000 股 = 1000 張
RS_PERIOD_CHOSE = 20
RS_PERIOD_DRIVE = 60
INIT_STOP_PCT = 0.07            # 初始停損 -7%
MIN_ROWS_SCAN = 200             # 掃描所需最少交易日（MA200）
MIN_ROWS_BACKTEST = 300         # 回測所需最少交易日


# ------------------------------------------------------------------
# 取數：把一檔股票的日線讀成 pandas Series 集合
# ------------------------------------------------------------------

class Bars:
    """單一股票的 OHLCV 序列（已依日期升冪排序）。"""
    def __init__(self, df: pd.DataFrame):
        self.dates = df["trade_date"].tolist()
        self.close = df["close_price"].astype(float).reset_index(drop=True)
        self.high = df["high_price"].astype(float).reset_index(drop=True)
        self.low = df["low_price"].astype(float).reset_index(drop=True)
        self.open = df["open_price"].astype(float).reset_index(drop=True)
        self.vol = df["volume"].astype(float).reset_index(drop=True)

    def __len__(self):
        return len(self.close)


def _load_bars(stock_code: str) -> Bars | None:
    df = db.query_df(
        """
        SELECT trade_date, open_price, high_price, low_price, close_price, volume
        FROM stock_daily_data
        WHERE stock_code = :code
        ORDER BY trade_date ASC
        """,
        {"code": stock_code},
    )
    if df.empty:
        return None
    return Bars(df)


# ------------------------------------------------------------------
# 大盤基準 ROC
# ------------------------------------------------------------------

def _benchmark(bars: Bars | None):
    """回傳 (roc20, roc60, roc20_series_by_date)。資料不足時以 0 代替。"""
    if bars is None or len(bars) < RS_PERIOD_DRIVE + 1:
        return 0.0, 0.0, {}
    roc20 = float(bars.close.pct_change(RS_PERIOD_CHOSE).iloc[-1])
    roc60 = float(bars.close.pct_change(RS_PERIOD_DRIVE).iloc[-1])
    roc20_series = bars.close.pct_change(RS_PERIOD_CHOSE)
    by_date = {d: (float(v) if pd.notna(v) else 0.0)
               for d, v in zip(bars.dates, roc20_series)}
    return roc20, roc60, by_date


# ------------------------------------------------------------------
# CHOSE：買入型態判斷（移植，移除 price>20）
# ------------------------------------------------------------------

def analyze_chose(bars: Bars, bench_roc: float) -> dict | None:
    if len(bars) < MIN_ROWS_SCAN:
        return None
    close, high, vol, open_p = bars.close, bars.high, bars.vol, bars.open

    curr = float(close.iloc[-1])
    avg_vol = float(vol.rolling(20).mean().iloc[-1])
    if avg_vol < MIN_VOLUME_CHOSE:        # 移除了 curr < min_price 的股價門檻
        return None

    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])
    if not (curr > ma50 > ma200):
        return None

    stock_roc = float(close.pct_change(RS_PERIOD_CHOSE).iloc[-1])
    rs_rating = (stock_roc - bench_roc) * 100
    if rs_rating < 0:
        return None

    year_high = float(high.iloc[-250:].max())
    prev_20_high = float(high.iloc[-21:-1].max())
    is_breakout = (curr > prev_20_high) and (float(close.iloc[-2]) < prev_20_high)

    rally = (high.iloc[-60:].max() - close.iloc[-60:].min()) / close.iloc[-60:].min()
    setup, reason = "", ""
    if rally > 0.8 and (year_high - curr) / year_high < 0.25 and is_breakout:
        setup, reason = "🚀 高窄旗型", "飆漲動能突破"
    elif (float(open_p.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2]) > 0.08:
        setup, reason = "🕳️ 買進跳空", "強力消息缺口"
    elif is_breakout and (year_high - curr) / year_high < 0.15:
        setup, reason = "📦 VCP突破", "整理區帶量突破"

    if not setup:
        return None
    return {
        "pattern_type": setup,
        "chose_reason": reason,
        "rs_rating": round(rs_rating, 1),
        "suggested_entry": round(prev_20_high, 2),
    }


# ------------------------------------------------------------------
# DRIVE：大戶動態評分（移植，移除 price>20）
# ------------------------------------------------------------------

def analyze_drive(bars: Bars, bench_roc: float) -> dict | None:
    if len(bars) < MIN_ROWS_SCAN:
        return None
    close, high, low, vol = bars.close, bars.high, bars.low, bars.vol

    curr = float(close.iloc[-1])
    avg_vol = float(vol.rolling(20).mean().iloc[-1])
    if avg_vol < MIN_VOLUME_DRIVE:        # 移除了 curr < min_price 的股價門檻
        return None

    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])
    year_high = float(high.iloc[-250:].max())
    if not (curr > ma50 > ma200 and (year_high - curr) / year_high < 0.25):
        return None

    stock_roc = float(close.pct_change(RS_PERIOD_DRIVE).iloc[-1])
    rs_rating = (stock_roc - bench_roc) * 100
    if rs_rating < 5:
        return None

    # MVP：15 天內收紅 >= 9 天 + 量能比前段放大
    up_days = int((close.iloc[-16:-1].diff() > 0).sum())
    vol_ratio = float(vol.iloc[-16:-1].mean() / vol.iloc[-31:-16].mean())
    is_mvp = up_days >= 9 and vol_ratio >= 1.2

    score, comments = 0, []
    prev_20_high = float(close.iloc[-21:-1].max())
    if curr > prev_20_high and float(vol.iloc[-1]) > avg_vol * 1.3:
        score += 50; comments.append("樞紐突破")
    if is_mvp:
        score += 30; comments.append("🔥MVP吸籌")
    if rs_rating > 30:
        score += 20; comments.append("超強RS")

    if score < 30:
        return None
    return {
        "drive_score": score,
        "rs_rating": round(rs_rating, 1),
        "chip_feature": " + ".join(comments),
    }


# ------------------------------------------------------------------
# 3 年回測（移植，成交量改張、移除 price>20）
# ------------------------------------------------------------------

def backtest_3y(bars: Bars, bench_roc_series: dict) -> tuple[float, float]:
    if len(bars) < MIN_ROWS_BACKTEST:
        return 0.0, 0.0
    c = bars.close; h = bars.high; o = bars.open; v = bars.vol
    ma10 = c.rolling(10).mean()
    ma20 = c.rolling(20).mean()
    ma50 = c.rolling(50).mean()
    ma200 = c.rolling(200).mean()
    avg_vol_20 = v.rolling(20).mean()

    trades: list[float] = []
    in_pos = False
    entry_p = 0.0
    start_idx = max(len(bars) - 750, 250)   # 確保 i-250 不越界

    for i in range(start_idx, len(bars)):
        curr_c = float(c.iloc[i])
        dt = bars.dates[i]

        if not in_pos:
            # 進場：CHOSE 邏輯（移除 curr_c < 20；量改張）
            if avg_vol_20.iloc[i] < MIN_VOLUME_CHOSE:
                continue
            if not (curr_c > ma50.iloc[i] > ma200.iloc[i]):
                continue
            s_roc = float(c.iloc[i] / c.iloc[i - RS_PERIOD_CHOSE] - 1)
            if (s_roc - bench_roc_series.get(dt, 0)) < 0:
                continue
            y_high = float(h.iloc[i - 250:i].max())
            p20_high = float(h.iloc[i - 21:i].max())
            is_break = (curr_c > p20_high) and (float(c.iloc[i - 1]) < p20_high)
            rally = (h.iloc[i - 60:i].max() - c.iloc[i - 60:i].min()) / c.iloc[i - 60:i].min()
            is_flag = rally > 0.8 and (y_high - curr_c) / y_high < 0.25 and is_break
            is_gap = (float(o.iloc[i]) - float(c.iloc[i - 1])) / float(c.iloc[i - 1]) > 0.08
            is_vcp = is_break and (y_high - curr_c) / y_high < 0.15
            if is_flag or is_gap or is_vcp:
                entry_p = curr_c
                in_pos = True
        else:
            # 出場：考特賣出法則
            r_mult = (curr_c - entry_p) / (entry_p * INIT_STOP_PCT)
            is_super = bool((c.iloc[i - 34:i + 1] > ma10.iloc[i - 34:i + 1]).all())
            check_ma = float(ma10.iloc[i] if is_super else ma20.iloc[i])
            exit_now = (
                curr_c < entry_p * (1 - INIT_STOP_PCT)
                or (r_mult >= 2 and curr_c < entry_p)
                or curr_c < check_ma
            )
            if exit_now:
                trades.append((curr_c - entry_p) / entry_p)
                in_pos = False

    if not trades:
        return 0.0, 0.0
    wr = len([t for t in trades if t > 0]) / len(trades) * 100
    tr = (np.prod([1 + t for t in trades]) - 1) * 100
    return round(float(wr), 1), round(float(tr), 1)


# ------------------------------------------------------------------
# 主流程：掃描全市場 → upsert daily_strategy_results
# ------------------------------------------------------------------

def _upsert_result(trade_date: date, code: str, chose: dict | None,
                   drive: dict | None, wr: float | None, tr: float | None):
    rs = (chose or drive or {}).get("rs_rating")
    entry = chose["suggested_entry"] if chose else None
    stop = round(entry * (1 - INIT_STOP_PCT), 2) if entry else None
    # 目標價以 R 倍數推估（R = entry * 7%）：第一目標 2R、第二目標 3R
    t1 = round(entry * (1 + 2 * INIT_STOP_PCT), 2) if entry else None
    t2 = round(entry * (1 + 3 * INIT_STOP_PCT), 2) if entry else None

    db.execute(
        """
        INSERT INTO daily_strategy_results
            (trade_date, stock_code, is_chose_trigger, pattern_type, chose_reason,
             is_drive_trigger, drive_score, rs_rating, chip_feature,
             suggested_entry, stop_loss, target_price_1, target_price_2,
             backtest_win_rate, backtest_total_pnl)
        VALUES
            (:dt, :code, :ct, :pt, :cr, :dt2, :ds, :rs, :cf,
             :se, :sl, :t1, :t2, :wr, :tr)
        ON DUPLICATE KEY UPDATE
            is_chose_trigger = VALUES(is_chose_trigger),
            pattern_type     = VALUES(pattern_type),
            chose_reason     = VALUES(chose_reason),
            is_drive_trigger = VALUES(is_drive_trigger),
            drive_score      = VALUES(drive_score),
            rs_rating        = VALUES(rs_rating),
            chip_feature     = VALUES(chip_feature),
            suggested_entry  = VALUES(suggested_entry),
            stop_loss        = VALUES(stop_loss),
            target_price_1   = VALUES(target_price_1),
            target_price_2   = VALUES(target_price_2),
            backtest_win_rate  = VALUES(backtest_win_rate),
            backtest_total_pnl = VALUES(backtest_total_pnl)
        """,
        {
            "dt": str(trade_date), "code": code,
            "ct": 1 if chose else 0,
            "pt": chose["pattern_type"] if chose else None,
            "cr": chose["chose_reason"] if chose else None,
            "dt2": 1 if drive else 0,
            "ds": drive["drive_score"] if drive else None,
            "rs": rs,
            "cf": drive["chip_feature"] if drive else None,
            "se": entry, "sl": stop, "t1": t1, "t2": t2,
            "wr": wr, "tr": tr,
        },
    )


def main():
    parser = argparse.ArgumentParser(description="每日策略選股引擎")
    parser.add_argument("--date", default=None, help="計算的交易日 YYYY-MM-DD，預設資料庫最新交易日")
    parser.add_argument("--no-backtest", action="store_true", help="跳過 3 年回測")
    args = parser.parse_args()

    # 決定 trade_date（資料庫最新交易日）
    if args.date:
        trade_date = date.fromisoformat(args.date)
    else:
        d = db.query_df("SELECT MAX(trade_date) AS d FROM stock_daily_data")
        val = d["d"].iloc[0]
        if val is None:
            log.warning("stock_daily_data 無資料，請先執行 ETL 同步。")
            return
        trade_date = val if isinstance(val, date) else date.fromisoformat(str(val))

    log.info(f"策略計算交易日：{trade_date}")

    # 大盤基準
    bench_bars = _load_bars(BENCH_CODE)
    bench20, bench60, bench_series = _benchmark(bench_bars)
    if bench_bars is None:
        log.warning(f"找不到大盤基準 {BENCH_CODE} 的資料，RS 將以 0 基準計算。")

    # 取全市場股票清單
    codes = db.query_df(
        "SELECT DISTINCT stock_code FROM stock_daily_data ORDER BY stock_code"
    )["stock_code"].tolist()
    log.info(f"掃描 {len(codes)} 檔股票…")

    n_chose = n_drive = n_written = 0
    for code in codes:
        if code == BENCH_CODE:
            continue
        bars = _load_bars(code)
        if bars is None or len(bars) < MIN_ROWS_SCAN:
            continue

        chose = analyze_chose(bars, bench20)
        drive = analyze_drive(bars, bench60)
        if not chose and not drive:
            continue

        wr = tr = None
        if chose and not args.no_backtest:
            wr, tr = backtest_3y(bars, bench_series)

        _upsert_result(trade_date, code, chose, drive, wr, tr)
        n_written += 1
        n_chose += 1 if chose else 0
        n_drive += 1 if drive else 0

    log.info(f"✅ 策略選股完成：寫入 {n_written} 檔"
             f"（CHOSE {n_chose} / DRIVE {n_drive}）@ {trade_date}")


if __name__ == "__main__":
    main()
