"""
modules/blind_test.py — 模組三：120 天隨機盲測修練（Blind Test Mode）

Session state keys used (all prefixed with `bt_`):
  bt_stock_code       : 真實股票代碼（隱藏，結案前不顯示）
  bt_start_date       : 盲測起點日期字串
  bt_df               : 完整 120 天 K 線 DataFrame（一次載入，後續僅切割）
  bt_signals          : 策略訊號 DataFrame（按 trade_date 索引）
  bt_step             : 當前已揭露的交易日數（初始 30）
  bt_position         : dict {entry_day, entry_price, stop_loss, take_profit} or None
  bt_closed           : bool，是否已強制平倉
  bt_pnl              : 最終損益%（平倉後）
  bt_trade_id         : 寫入 DB 後的 trade_id
"""
from __future__ import annotations

import random
import streamlit as st
import pandas as pd
from datetime import date
import db
from modules.utils import add_indicators, build_chart, fmt_price, fmt_pct


# ------------------------------------------------------------------
# 公開入口
# ------------------------------------------------------------------

def render():
    st.header("🎲 120 天隨機盲測修練")

    # ---- 尚未開始，或主動重設 ----
    if "bt_df" not in st.session_state or st.button("🔀 重新抽取新盲測股票", key="bt_reset"):
        _start_new_session()
        return

    if st.session_state.get("bt_closed"):
        _render_closed_review()
        return

    _render_active_session()


# ------------------------------------------------------------------
# 初始化新盲測
# ------------------------------------------------------------------

def _start_new_session():
    with st.spinner("隨機抽取盲測股票中…"):
        try:
            stock_code, start_date = db.fetch_random_blind_stock()
        except ValueError as e:
            st.error(str(e))
            return

    # 一次性批量撈取 120 天資料（之後所有步進操作全在記憶體內完成）
    df = db.fetch_kline(stock_code, start_date, limit=120)
    if len(df) < 60:
        st.error("該股資料不足，請再試一次。")
        return

    # 撈取策略訊號（在同一區間）
    end_date = df["trade_date"].max()
    signals = db.fetch_strategy_signals_for_stock(stock_code, start_date, str(end_date))
    signals_set = set(signals["trade_date"].astype(str).tolist()) if not signals.empty else set()

    st.session_state.update({
        "bt_stock_code": stock_code,
        "bt_start_date": start_date,
        "bt_df": df,
        "bt_signals": signals,
        "bt_signals_set": signals_set,
        "bt_step": 30,           # 初始揭露前 30 天
        "bt_position": None,
        "bt_closed": False,
        "bt_pnl": None,
        "bt_trade_id": None,
    })
    st.rerun()


# ------------------------------------------------------------------
# 主盲測畫面
# ------------------------------------------------------------------

def _render_active_session():
    df: pd.DataFrame = st.session_state["bt_df"]
    step: int = st.session_state["bt_step"]
    position = st.session_state.get("bt_position")
    total_days = len(df)

    # 當前可見的資料切片
    visible_df = df.iloc[:step].copy()
    visible_df = add_indicators(visible_df)
    visible_df = visible_df.reset_index(drop=True)

    # X 軸替換：「第 N 交易日」
    x_labels = [f"第{i+1}交易日" for i in range(len(visible_df))]

    # ---- 策略訊號警報 ----
    _check_signal_alert(visible_df, step)

    # ---- 強制停損偵測 ----
    if position:
        current_close = visible_df["close"].iloc[-1]
        sl = position["stop_loss"]
        tp = position["take_profit"]
        if sl and current_close <= sl:
            _force_close("已結案_停損", current_close, visible_df)
            return
        if tp and current_close >= tp:
            _force_close("已結案_停利", current_close, visible_df)
            return

    # ---- K 線圖（盲測：隱藏日期、代號）----
    fig = build_chart(
        visible_df,
        title="神祕飆股 (X) — 盲測中",
        x_labels=x_labels,
        show_chips=True,
    )
    st.plotly_chart(fig, use_container_width=True)

    # ---- 進度條 ----
    st.progress(step / total_days, text=f"第 {step} / {total_days} 交易日")

    # ---- 操作按鈕列 ----
    col1, col2, col3 = st.columns(3)

    with col1:
        if step < total_days:
            if st.button("▶ 前進下一天", key="bt_next"):
                st.session_state["bt_step"] += 1
                st.rerun()
        else:
            st.warning("已到達 120 天盡頭，強制結案。")
            _force_close(
                "已結案_停損",
                visible_df["close"].iloc[-1],
                visible_df,
                forced=True,
            )
            return

    with col2:
        if not position:
            if st.button("🛒 模擬買進", key="bt_buy"):
                st.session_state["bt_show_entry_form"] = True
                st.rerun()
        else:
            st.success(
                f"持倉中 | 進場：{fmt_price(position['entry_price'])} "
                f"停損：{fmt_price(position.get('stop_loss'))} "
                f"停利：{fmt_price(position.get('take_profit'))}"
            )

    with col3:
        if position:
            if st.button("🚪 主動出場", key="bt_exit"):
                _force_close(
                    "已結案_停損",
                    visible_df["close"].iloc[-1],
                    visible_df,
                )
                return

    # ---- 買進表單 ----
    if st.session_state.get("bt_show_entry_form") and not position:
        _render_entry_form(visible_df)


# ------------------------------------------------------------------
# 買進表單
# ------------------------------------------------------------------

def _render_entry_form(visible_df: pd.DataFrame):
    st.divider()
    with st.form("bt_entry_form"):
        st.subheader("🛒 設定模擬買進條件")
        last_close = float(visible_df["close"].iloc[-1])
        ep = st.number_input(
            "進場價格",
            value=last_close,
            min_value=0.01, step=0.1, format="%.2f",
        )
        sl = st.number_input(
            "停損價",
            value=round(last_close * 0.93, 2),
            min_value=0.01, step=0.1, format="%.2f",
        )
        tp = st.number_input(
            "停利價",
            value=round(last_close * 1.15, 2),
            min_value=0.01, step=0.1, format="%.2f",
        )
        submitted = st.form_submit_button("確認買進")

    if submitted:
        step = st.session_state["bt_step"]
        entry_day_label = f"第{step}交易日"

        # 寫入 DB（status='持股中'）
        stock_code = st.session_state["bt_stock_code"]
        start_date = st.session_state["bt_start_date"]
        entry_date_actual = str(
            st.session_state["bt_df"]["trade_date"].iloc[step - 1]
        )
        trade_id = db.execute(
            """
            INSERT INTO trading_practice_records
                (user_id, stock_code, practice_mode, practice_start_date,
                 entry_date, entry_price, user_stop_loss, user_take_profit, status)
            VALUES
                (:uid, :code, 'BLIND_120', :start, :ed, :ep, :sl, :tp, '持股中')
            """,
            {
                "uid": db.get_user_id(),
                "code": stock_code,
                "start": start_date,
                "ed": entry_date_actual,
                "ep": ep, "sl": sl, "tp": tp,
            },
        )
        st.session_state["bt_position"] = {
            "entry_day": step,
            "entry_day_label": entry_day_label,
            "entry_price": ep,
            "stop_loss": sl,
            "take_profit": tp,
        }
        st.session_state["bt_trade_id"] = trade_id
        st.session_state["bt_show_entry_form"] = False
        st.rerun()


# ------------------------------------------------------------------
# 訊號警報（盲測步進提示）
# ------------------------------------------------------------------

def _check_signal_alert(visible_df: pd.DataFrame, step: int):
    signals_set: set = st.session_state.get("bt_signals_set", set())
    signals_df: pd.DataFrame = st.session_state.get("bt_signals", pd.DataFrame())

    current_date = str(visible_df["trade_date"].iloc[-1]) if "trade_date" in visible_df.columns else ""
    if not current_date or current_date not in signals_set:
        return

    if signals_df.empty:
        return

    row = signals_df[signals_df["trade_date"].astype(str) == current_date]
    if row.empty:
        return
    row = row.iloc[0]

    alerts = []
    if row.get("is_chose_trigger") == 1:
        alerts.append(f"📐 型態觸發【{row.get('pattern_type', 'CHOSE')}】")
        if row.get("chose_reason"):
            alerts.append(f"   ↳ {row['chose_reason']}")
    if row.get("is_drive_trigger") == 1:
        score = row.get("drive_score", "")
        alerts.append(f"🦈 大戶動態評分【{score} 分】")
        if row.get("chip_feature"):
            alerts.append(f"   ↳ {row['chip_feature']}")
    if row.get("rs_rating"):
        alerts.append(f"💹 RS 強度：{row['rs_rating']:.1f}")

    if alerts:
        msg = "🔥 **本日觸發策略訊號！**\n\n" + "\n\n".join(alerts)
        st.warning(msg)


# ------------------------------------------------------------------
# 強制平倉
# ------------------------------------------------------------------

def _force_close(
    status: str,
    exit_price: float,
    visible_df: pd.DataFrame,
    forced: bool = False,
):
    position = st.session_state.get("bt_position")
    trade_id = st.session_state.get("bt_trade_id")
    step = st.session_state["bt_step"]

    pnl_pct: float | None = None
    if position:
        ep = position["entry_price"]
        pnl_pct = round((exit_price - ep) / ep * 100, 2) if ep > 0 else None

        exit_date_actual = str(
            st.session_state["bt_df"]["trade_date"].iloc[min(step - 1, len(st.session_state["bt_df"]) - 1)]
        )
        if trade_id:
            db.execute(
                """
                UPDATE trading_practice_records
                SET status = :st, exit_date = :ed, exit_price = :ep, pnl_percent = :pnl
                WHERE trade_id = :tid
                """,
                {"st": status, "ed": exit_date_actual,
                 "ep": exit_price, "pnl": pnl_pct, "tid": trade_id},
            )
    elif not forced:
        # 純觀望：INSERT 一筆無持倉的結案紀錄
        start_date = st.session_state["bt_start_date"]
        stock_code = st.session_state["bt_stock_code"]
        trade_id = db.execute(
            """
            INSERT INTO trading_practice_records
                (user_id, stock_code, practice_mode, practice_start_date, status)
            VALUES (:uid, :code, 'BLIND_120', :start, '純觀望')
            """,
            {"uid": db.get_user_id(), "code": stock_code, "start": start_date},
        )
        st.session_state["bt_trade_id"] = trade_id

    st.session_state["bt_closed"] = True
    st.session_state["bt_pnl"] = pnl_pct
    st.rerun()


# ------------------------------------------------------------------
# 結案覆盤看板
# ------------------------------------------------------------------

def _render_closed_review():
    stock_code = st.session_state.get("bt_stock_code", "???")
    pnl = st.session_state.get("bt_pnl")
    position = st.session_state.get("bt_position")
    df: pd.DataFrame = st.session_state["bt_df"]

    # 解鎖真實身分（查詢 stock_info）
    info_df = db.query_df(
        "SELECT stock_name, industry_type FROM stock_info WHERE stock_code = :code",
        {"code": stock_code},
    )
    stock_name = info_df["stock_name"].iloc[0] if not info_df.empty else "未知"

    st.success(f"🏁 盲測結案！此股真實身分：**{stock_code} {stock_name}**")

    # 顯示完整 K 線
    full_df = add_indicators(df.copy()).reset_index(drop=True)
    fig = build_chart(full_df, title=f"{stock_code} {stock_name} — 完整 {len(df)} 天走勢")
    st.plotly_chart(fig, use_container_width=True)

    if pnl is not None:
        color = "🟢" if pnl > 0 else "🔴"
        st.metric("最終損益", fmt_pct(pnl), delta=fmt_pct(pnl))
    elif position is None:
        st.info("本次盲測為純觀望（未建立部位）。")

    # ---- 股性貼籤與覆盤筆記（強制填寫）----
    st.divider()
    st.subheader("📝 覆盤筆記 — 股性歸納（必填）")

    tag_options = [
        "假突破常客", "投信鎖碼大戶股", "跳空缺口易補", "高波動成長股",
        "籌碼乾淨穩定", "外資長線買超", "主力坐轎", "季線支撐強",
        "連板動能強", "容易放量失速",
    ]

    with st.form("bt_review_form"):
        selected_tags = st.multiselect(
            "勾選符合的股性標籤（可多選）",
            tag_options,
        )
        custom_tags = st.text_input("補充自訂標籤（逗號分隔）")
        review_text = st.text_area(
            "深度覆盤心得",
            placeholder="記錄這檔股票的股性特徵、量價行為、籌碼節奏…",
            height=150,
        )
        submitted = st.form_submit_button("💾 儲存覆盤筆記")

    if submitted:
        all_tags_list = selected_tags[:]
        if custom_tags.strip():
            all_tags_list += [t.strip() for t in custom_tags.split(",") if t.strip()]
        tags_str = ",".join(all_tags_list)
        trade_id = st.session_state.get("bt_trade_id")

        db.execute(
            """
            INSERT INTO stock_character_notes
                (user_id, stock_code, trade_id, stock_tags, review_content)
            VALUES (:uid, :code, :tid, :tags, :review)
            """,
            {
                "uid": db.get_user_id(),
                "code": stock_code,
                "tid": trade_id,
                "tags": tags_str,
                "review": review_text,
            },
        )
        st.success("✅ 覆盤筆記已儲存！股性知識庫更新完畢。")

    st.divider()
    if st.button("🔀 開始新一輪盲測", key="bt_new_round"):
        # 清除所有 bt_ session state
        keys_to_clear = [k for k in st.session_state if k.startswith("bt_")]
        for k in keys_to_clear:
            del st.session_state[k]
        st.rerun()
