"""
modules/daily_board.py — 模組一：每日策略選股看板（觀測雷達）
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
from datetime import date, timedelta
import db
from modules.utils import fmt_price, fmt_pct


# ------------------------------------------------------------------
# 公開入口
# ------------------------------------------------------------------

def render():
    st.header("📡 每日策略選股看板（觀測雷達）")

    # ---- 日期選擇 ----
    latest_date_str = db.fetch_latest_strategy_date()
    default_date = (
        date.fromisoformat(latest_date_str)
        if latest_date_str
        else date.today() - timedelta(days=1)
    )
    selected_date = st.date_input("選擇交易日", value=default_date, key="board_date")

    if st.button("🔍 查詢", key="board_query"):
        st.session_state["board_queried_date"] = str(selected_date)

    queried_date = st.session_state.get("board_queried_date", str(default_date))

    # ---- 載入資料 ----
    with st.spinner("載入策略選股資料…"):
        df = db.fetch_strategy_by_date(queried_date)

    if df.empty:
        st.warning(f"📭 {queried_date} 無策略選股資料，請確認 ETL 已同步。")
        return

    gold = df[(df["is_chose_trigger"] == 1) & (df["is_drive_trigger"] == 1)]
    chose_only = df[(df["is_chose_trigger"] == 1) & (df["is_drive_trigger"] == 0)]
    drive_only = df[(df["is_chose_trigger"] == 0) & (df["is_drive_trigger"] == 1)]

    st.caption(f"資料日期：{queried_date}｜黃金交集 {len(gold)} 檔 ／ 型態突破 {len(chose_only)} 檔 ／ 大戶潛伏 {len(drive_only)} 檔")

    tab1, tab2, tab3 = st.tabs([
        f"🔥 黃金交集區 ({len(gold)})",
        f"📈 動態突破區 ({len(chose_only)})",
        f"👑 大戶潛伏區 ({len(drive_only)})",
    ])

    with tab1:
        _render_gold_tab(gold, queried_date)

    with tab2:
        _render_chose_tab(chose_only, queried_date)

    with tab3:
        _render_drive_tab(drive_only, queried_date)


# ------------------------------------------------------------------
# 三個 Tab 的子渲染函式
# ------------------------------------------------------------------

def _action_button(row: pd.Series, date_str: str, key_suffix: str):
    """帶入決策練習按鈕：將 stock_code 與日期存入 session state 並跳轉模組二。"""
    if st.button("📥 帶入決策練習", key=f"action_{row['stock_code']}_{key_suffix}"):
        st.session_state["nav_page"] = "每日決策修練"
        st.session_state["decision_preload"] = {
            "stock_code": row["stock_code"],
            "stock_name": row["stock_name"],
            "trade_date": date_str,
        }
        st.rerun()


def _render_gold_tab(df: pd.DataFrame, date_str: str):
    if df.empty:
        st.info("本日無黃金交集（CHOSE + DRIVE 雙重觸發）個股。")
        return

    for _, row in df.iterrows():
        with st.container(border=True):
            col_left, col_right = st.columns([3, 1])
            with col_left:
                st.markdown(
                    f"### {row['stock_code']} {row['stock_name']}"
                    f"  `{row.get('industry_type', '')}` "
                    f"  🏷️ *{row.get('pattern_type', '')}*"
                )
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("建議進場", fmt_price(row["suggested_entry"]))
                c2.metric("防守停損", fmt_price(row["stop_loss"]))
                c3.metric("第一目標", fmt_price(row["target_price_1"]))
                c4.metric("第二目標", fmt_price(row["target_price_2"]))

                c5, c6, c7 = st.columns(3)
                wr = row["backtest_win_rate"]
                wr_color = "🟢" if wr and wr >= 60 else ("🟡" if wr and wr >= 50 else "🔴")
                c5.markdown(f"**3Y 回測勝率**  \n{wr_color} **{fmt_pct(wr) if wr else '—'}**")
                c6.metric("3Y 總報酬", fmt_pct(row["backtest_total_pnl"]))
                c7.metric("RS 強度", f"{row['rs_rating']:.1f}" if row["rs_rating"] else "—")

                if row.get("chip_feature"):
                    st.caption(f"🧲 籌碼：{row['chip_feature']}")
                if row.get("chose_reason"):
                    st.caption(f"📐 型態：{row['chose_reason']}")
                if row.get("drive_score"):
                    st.caption(f"🦈 大戶評分：{row['drive_score']} 分")

            with col_right:
                _action_button(row, date_str, "gold")


def _render_chose_tab(df: pd.DataFrame, date_str: str):
    if df.empty:
        st.info("本日無純型態突破（僅 CHOSE 觸發）個股。")
        return

    for _, row in df.iterrows():
        with st.container(border=True):
            col_left, col_right = st.columns([3, 1])
            with col_left:
                st.markdown(
                    f"### {row['stock_code']} {row['stock_name']}"
                    f"  `{row.get('industry_type', '')}`"
                )
                c1, c2 = st.columns(2)
                c1.markdown(f"**型態**：{row.get('pattern_type', '—')}")
                c2.metric("RS 強度", f"{row['rs_rating']:.1f}" if row["rs_rating"] else "—")
                if row.get("chose_reason"):
                    st.caption(f"📐 {row['chose_reason']}")
            with col_right:
                _action_button(row, date_str, "chose")


def _render_drive_tab(df: pd.DataFrame, date_str: str):
    if df.empty:
        st.info("本日無大戶潛伏（僅 DRIVE 觸發）個股。")
        return

    for _, row in df.iterrows():
        with st.container(border=True):
            col_left, col_right = st.columns([3, 1])
            with col_left:
                st.markdown(
                    f"### {row['stock_code']} {row['stock_name']}"
                    f"  `{row.get('industry_type', '')}`"
                )
                c1, c2, c3 = st.columns(3)
                score = row.get("drive_score")
                score_icon = "🔴" if score and score >= 100 else ("🟡" if score and score >= 50 else "⚪")
                c1.markdown(f"**大戶評分**  \n{score_icon} **{score if score else '—'} 分**")
                c2.metric("RS 強度", f"{row['rs_rating']:.1f}" if row["rs_rating"] else "—")
                c3.markdown(f"**籌碼特徵**  \n{row.get('chip_feature', '—')}")
            with col_right:
                _action_button(row, date_str, "drive")
