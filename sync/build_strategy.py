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


def _fetch_corp_actions(stock_code: str | None = None) -> dict[str, list[tuple[date, float]]]:
    """讀取 stock_corp_actions 調整係數，回傳 {stock_code: [(ex_date, factor), ...]}（按日期升冪）。
    不帶 stock_code 時一次抓全表（給 main() 迴圈用，避免每檔股票各查一次）。"""
    sql = "SELECT stock_code, ex_date, adjust_factor FROM stock_corp_actions"
    params: dict = {}
    if stock_code:
        sql += " WHERE stock_code = :code"
        params["code"] = stock_code
    sql += " ORDER BY stock_code, ex_date"
    df = db.query_df(sql, params)
    out: dict[str, list[tuple[date, float]]] = {}
    for _, row in df.iterrows():
        ex_date = row["ex_date"] if isinstance(row["ex_date"], date) else date.fromisoformat(str(row["ex_date"]))
        out.setdefault(row["stock_code"], []).append((ex_date, float(row["adjust_factor"])))
    return out


def _apply_corp_actions(df: pd.DataFrame, actions: list[tuple[date, float]] | None) -> pd.DataFrame:
    """對 ex_date 之前的 OHLC 套用調整係數，還原成連續價格序列（volume 反向調整維持量能連續）。
    只調整「已經發生」的事件（ex_date <= 資料序列最後一天），避免誤調整尚未發生的未來事件。"""
    if not actions or df.empty:
        return df
    df["volume"] = df["volume"].astype(float)   # 量能欄位原始為整數，調整後可能非整數，先轉型避免指派報錯
    as_of = df["trade_date"].max()
    for ex_date, factor in actions:
        if ex_date > as_of:
            continue
        mask = df["trade_date"] < ex_date
        for col in ["open_price", "high_price", "low_price", "close_price"]:
            df.loc[mask, col] = df.loc[mask, col].astype(float) * factor
        df.loc[mask, "volume"] = df.loc[mask, "volume"] / factor
    return df


def _load_bars(
    stock_code: str,
    corp_actions: dict[str, list[tuple[date, float]]] | None = None,
) -> Bars | None:
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
    if corp_actions is None:
        corp_actions = _fetch_corp_actions(stock_code)
    df = _apply_corp_actions(df, corp_actions.get(stock_code))
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

FORWARD_RETURN_DAYS = (5, 10, 20)   # 進場後第 N 個交易日的正報酬機率統計


def _empty_backtest_result() -> dict:
    result = {"win_rate": 0.0, "total_pnl": 0.0}
    for n in FORWARD_RETURN_DAYS:
        result[f"fwd_win_rate_{n}"] = None
        result[f"fwd_n_{n}"] = None
    return result


def backtest_3y(bars: Bars, bench_roc_series: dict) -> dict:
    """
    3 年回測（進出場依考特買賣法則）＋ 進場後第 N 個交易日正報酬機率統計。

    回傳 dict：
      win_rate / total_pnl：既有的交易勝率／總報酬（考特賣出法則出場，不變）
      fwd_win_rate_{5,10,20}：與 win_rate 用「同一批」CHOSE 進場點，比對進場後第 N 個
        交易日收盤價是否仍高於進場價，計算「仍為正報酬」的歷史機率（%）。跟 win_rate
        不同的是這個看的是「固定 N 天後」，win_rate 看的是「考特法則實際出場」時的損益，
        兩者搭配可以看出「短期是否容易先蜜月拉回」還是「一路噴發」。
      fwd_n_{5,10,20}：上述統計的樣本數——最近的進場點可能不滿 N 個交易日的未來資料
        （尤其 5 日窗口離今天太近時），這些會被排除在分母外，不會當作 0% 硬湊。
    """
    if len(bars) < MIN_ROWS_BACKTEST:
        return _empty_backtest_result()
    c = bars.close; h = bars.high; o = bars.open; v = bars.vol
    ma10 = c.rolling(10).mean()
    ma20 = c.rolling(20).mean()
    ma50 = c.rolling(50).mean()
    ma200 = c.rolling(200).mean()
    avg_vol_20 = v.rolling(20).mean()

    trades: list[float] = []
    entry_indices: list[int] = []
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
                entry_indices.append(i)
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

    result = _empty_backtest_result()
    if trades:
        wr = len([t for t in trades if t > 0]) / len(trades) * 100
        tr = (np.prod([1 + t for t in trades]) - 1) * 100
        result["win_rate"] = round(float(wr), 1)
        result["total_pnl"] = round(float(tr), 1)

    for n in FORWARD_RETURN_DAYS:
        ups = total = 0
        for idx in entry_indices:
            fwd_idx = idx + n
            if fwd_idx >= len(bars):
                continue   # 資料不足 N 天，這個進場點不計入分母（不當成負報酬硬湊）
            total += 1
            if float(c.iloc[fwd_idx]) > float(c.iloc[idx]):
                ups += 1
        if total > 0:
            result[f"fwd_win_rate_{n}"] = round(ups / total * 100, 1)
            result[f"fwd_n_{n}"] = total

    return result


# ------------------------------------------------------------------
# 主流程：掃描全市場 → upsert daily_strategy_results
# ------------------------------------------------------------------

def _upsert_result(trade_date: date, code: str, chose: dict | None,
                   drive: dict | None, backtest: dict | None):
    rs = (chose or drive or {}).get("rs_rating")
    entry = chose["suggested_entry"] if chose else None
    stop = round(entry * (1 - INIT_STOP_PCT), 2) if entry else None
    # 目標價以 R 倍數推估（R = entry * 7%）：第一目標 2R、第二目標 3R
    t1 = round(entry * (1 + 2 * INIT_STOP_PCT), 2) if entry else None
    t2 = round(entry * (1 + 3 * INIT_STOP_PCT), 2) if entry else None
    bt = backtest or {}

    db.execute(
        """
        INSERT INTO daily_strategy_results
            (trade_date, stock_code, is_chose_trigger, pattern_type, chose_reason,
             is_drive_trigger, drive_score, rs_rating, chip_feature,
             suggested_entry, stop_loss, target_price_1, target_price_2,
             backtest_win_rate, backtest_total_pnl,
             fwd_return_5d_win_rate, fwd_return_10d_win_rate, fwd_return_20d_win_rate,
             fwd_return_5d_n, fwd_return_10d_n, fwd_return_20d_n)
        VALUES
            (:dt, :code, :ct, :pt, :cr, :dt2, :ds, :rs, :cf,
             :se, :sl, :t1, :t2, :wr, :tr,
             :fwr5, :fwr10, :fwr20, :fn5, :fn10, :fn20)
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
            backtest_total_pnl = VALUES(backtest_total_pnl),
            fwd_return_5d_win_rate  = VALUES(fwd_return_5d_win_rate),
            fwd_return_10d_win_rate = VALUES(fwd_return_10d_win_rate),
            fwd_return_20d_win_rate = VALUES(fwd_return_20d_win_rate),
            fwd_return_5d_n  = VALUES(fwd_return_5d_n),
            fwd_return_10d_n = VALUES(fwd_return_10d_n),
            fwd_return_20d_n = VALUES(fwd_return_20d_n)
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
            "wr": bt.get("win_rate"), "tr": bt.get("total_pnl"),
            "fwr5": bt.get("fwd_win_rate_5"), "fwr10": bt.get("fwd_win_rate_10"),
            "fwr20": bt.get("fwd_win_rate_20"),
            "fn5": bt.get("fwd_n_5"), "fn10": bt.get("fwd_n_10"), "fn20": bt.get("fwd_n_20"),
        },
    )


def _cleanup_stale_results(trade_date: date, stale_codes: list[str]) -> int:
    """
    自我修復：把「今天判定為 stale（行情資料跟不上 trade_date）」的股票，對照
    daily_strategy_results 裡是否殘留著這個 trade_date 的舊紀錄——若有，代表那筆紀錄
    是在更早、這檔股票資料還跟得上時寫入的，現在已經跟目前的 stock_daily_data 矛盾
    （股票可能已下市/停止交易，或同步中斷），選股雷達不該再顯示這種「看起來是當天
    觸發、實際上是舊資料殘影」的訊號，一併清掉。

    2026-07-24：實際在正式環境抓到 4 檔股票、18 筆這種矛盾紀錄（詳見對話紀錄），
    人工確認後刪除；加這段是為了讓同樣情況以後不需要人工介入就能自動收拾，不管
    根源是什麼（股票下市、同步中斷、或任何未來才會出現的成因）。
    """
    if not stale_codes:
        return 0
    hits = db.query_df(
        "SELECT stock_code FROM daily_strategy_results WHERE trade_date = :dt AND stock_code IN :codes",
        {"dt": str(trade_date), "codes": tuple(stale_codes)},
    )
    n = len(hits)
    if n == 0:
        return 0
    db.execute(
        "DELETE FROM daily_strategy_results WHERE trade_date = :dt AND stock_code IN :codes",
        {"dt": str(trade_date), "codes": tuple(stale_codes)},
    )
    log.warning(
        f"⚠️ 清除 {n} 筆與行情資料矛盾的殘留紀錄 @ {trade_date}："
        f"{hits['stock_code'].tolist()}"
    )
    return n


def main():
    parser = argparse.ArgumentParser(description="每日策略選股引擎")
    parser.add_argument("--date", default=None, help="計算的交易日 YYYY-MM-DD，預設資料庫最新交易日")
    parser.add_argument("--no-backtest", action="store_true", help="跳過 3 年回測")
    parser.add_argument("--no-sync", action="store_true",
                        help="不要自動補齊缺漏交易日（預設會補）")
    parser.add_argument("--sync-days", type=int, default=MIN_ROWS_SCAN + 20,
                        help="自動補齊時往回確保的全市場交易日數（預設約一年）")
    args = parser.parse_args()

    # Step 0: 自動補齊缺漏交易日（遇缺先同步全市場進 TiDB，再繼續篩選）
    if not args.no_sync:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import market_sync  # noqa: E402
        end_date = date.fromisoformat(args.date) if args.date else date.today()
        log.info(f"=== Step 0: 檢查/補齊全市場資料（往回 {args.sync_days} 個交易日）===")
        market_sync.ensure_history(end_date, args.sync_days)

    # 決定 trade_date（資料庫最新交易日）
    if args.date:
        trade_date = date.fromisoformat(args.date)
    else:
        d = db.query_df("SELECT MAX(trade_date) AS d FROM stock_daily_data")
        val = d["d"].iloc[0]
        if val is None:
            log.warning("stock_daily_data 無資料，且自動補齊未取得資料。")
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

    corp_actions = _fetch_corp_actions()
    if corp_actions:
        log.info(f"公司行動調整表：{len(corp_actions)} 檔股票有登記減資/分割/合併調整")

    n_chose = n_drive = n_written = n_stale = 0
    stale_codes: list[str] = []
    for code in codes:
        if code == BENCH_CODE:
            continue
        bars = _load_bars(code, corp_actions)
        if bars is None or len(bars) < MIN_ROWS_SCAN:
            continue
        if bars.dates[-1] != trade_date:
            # 該股同步進度落後於目標交易日，資料是舊的，不能當作當日訊號
            n_stale += 1
            stale_codes.append(code)
            continue

        chose = analyze_chose(bars, bench20)
        drive = analyze_drive(bars, bench60)
        if not chose and not drive:
            continue

        backtest = None
        if chose and not args.no_backtest:
            backtest = backtest_3y(bars, bench_series)

        _upsert_result(trade_date, code, chose, drive, backtest)
        n_written += 1
        n_chose += 1 if chose else 0
        n_drive += 1 if drive else 0

    n_cleaned = _cleanup_stale_results(trade_date, stale_codes)

    log.info(f"✅ 策略選股完成：寫入 {n_written} 檔"
             f"（CHOSE {n_chose} / DRIVE {n_drive}，略過 {n_stale} 檔舊資料，"
             f"清除 {n_cleaned} 筆與行情資料矛盾的殘留紀錄）@ {trade_date}")


if __name__ == "__main__":
    main()
