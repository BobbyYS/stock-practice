"""
modules/daily_board.py — 模組一：每日策略選股看板（觀測雷達）
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
from datetime import date, timedelta
import db
from modules.utils import fmt_price, fmt_pct


def _s(val, default: str = "") -> str:
    """安全轉字串：DB NULL 讀出來可能是 None 或 NaN，直接內插進 f-string 會顯示成
    字面上的「None」/「nan」文字（`.get(key, default)` 對已存在但值為空的欄位不會退回
    default）。缺值時一律回傳 default。"""
    return str(val) if pd.notna(val) else default


def _fmt_rs(val) -> str:
    """RS 強度格式化：用 pd.notna 判斷缺值，而不是 `if val` 真值判斷——因為 Python 裡
    NaN 是 truthy，`if val:` 對 NaN 不會走到 else 分支，會把 f"{nan:.1f}" 這種字面上的
    「nan」文字印到畫面上。"""
    return f"{val:.1f}" if pd.notna(val) else "—"


def _fmt_forward_returns(row: pd.Series) -> str | None:
    """組合「進場後 N 日仍為正報酬」機率文字，附樣本數避免小樣本數字誤導使用者。
    三個窗口都沒有可用樣本時回傳 None（不顯示這行）。"""
    parts = []
    for n in (5, 10, 20):
        rate = row.get(f"fwd_return_{n}d_win_rate")
        cnt = row.get(f"fwd_return_{n}d_n")
        if pd.notna(rate) and pd.notna(cnt):
            parts.append(f"{n}日後{rate:.0f}%（{int(cnt)}次）")
    if not parts:
        return None
    return "📈 進場後上漲機率：" + "　".join(parts)


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

    corp_actions = db.fetch_corp_actions()

    tab1, tab2, tab3, tab4 = st.tabs([
        f"🔥 黃金交集區 ({len(gold)})",
        f"📈 動態突破區 ({len(chose_only)})",
        f"👑 大戶潛伏區 ({len(drive_only)})",
        f"🔧 公司行動紀錄 ({len(corp_actions)})",
    ])

    with tab1:
        _render_gold_tab(gold, queried_date, corp_actions)

    with tab2:
        _render_chose_tab(chose_only, queried_date, corp_actions)

    with tab3:
        _render_drive_tab(drive_only, queried_date, corp_actions)

    with tab4:
        _render_corp_actions_tab(corp_actions)


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


# RS強度／型態辨識最長回看 250 個交易日（年高點），換算日曆天約 250~350 天，抓 380 天含
# 週末假日緩衝。3Y 回測（僅黃金交集區顯示）回看長達 750 個交易日（約 3 年），換算日曆天
# 約 1050~1100 天，抓 1100 天緩衝——兩種窗口長度差很多，用錯窗口會漏掉黃金交集區裡
# 「RS 沒受影響、但 3Y 回測數字其實已套用調整係數」的情況。
DEFAULT_WINDOW_DAYS = 380
BACKTEST_WINDOW_DAYS = 1100


def _corp_action_badge(
    stock_code: str,
    date_str: str,
    corp_actions: pd.DataFrame,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> str | None:
    """
    若該股票在 date_str 往前 window_days 天內有登記公司行動（減資/分割/合併），
    回傳警示文字；否則回傳 None。提醒使用者這段期間顯示的技術指標（RS強度／
    3Y回測，視呼叫端傳入的 window_days 而定）已套用調整係數，不是原始未校正的數字。
    """
    if corp_actions.empty:
        return None
    hits = corp_actions[corp_actions["stock_code"] == stock_code]
    if hits.empty:
        return None
    ref_date = date.fromisoformat(date_str)
    window_start = ref_date - timedelta(days=window_days)
    recent = hits[
        (pd.to_datetime(hits["ex_date"]).dt.date <= ref_date)
        & (pd.to_datetime(hits["ex_date"]).dt.date >= window_start)
    ]
    if recent.empty:
        return None
    r = recent.iloc[0]
    return f"⚙️ 已係數校正：{r['action_type'] or '公司行動'} @ {r['ex_date']}"


def _render_gold_tab(df: pd.DataFrame, date_str: str, corp_actions: pd.DataFrame):
    if df.empty:
        st.info("本日無黃金交集（CHOSE + DRIVE 雙重觸發）個股。")
        return

    for _, row in df.iterrows():
        with st.container(border=True):
            col_left, col_right = st.columns([3, 1])
            with col_left:
                st.markdown(
                    f"### {row['stock_code']} {row['stock_name']}"
                    f"  `{_s(row.get('industry_type'))}` "
                    f"  🏷️ *{_s(row.get('pattern_type'))}*"
                )
                # 這個 tab 有顯示 3Y 回測，回測回看窗口長達 750 個交易日（約 3 年），
                # 比 RS/型態辨識用的窗口長很多，用 BACKTEST_WINDOW_DAYS 才能正確涵蓋。
                badge = _corp_action_badge(row["stock_code"], date_str, corp_actions, BACKTEST_WINDOW_DAYS)
                if badge:
                    st.caption(badge)
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("建議進場", fmt_price(row["suggested_entry"]))
                c2.metric("防守停損", fmt_price(row["stop_loss"]))
                c3.metric("第一目標", fmt_price(row["target_price_1"]))
                c4.metric("第二目標", fmt_price(row["target_price_2"]))

                c5, c6, c7 = st.columns(3)
                wr = row["backtest_win_rate"]
                # 用 pd.notna 判斷「有沒有回測資料」，不能用 `if wr:`——0.0 是合法的「勝率
                # 0%」結果（Python 裡 0.0 是 falsy，會被誤判成無資料而藏起來，等於把真正
                # 差的績效藏起來）；NaN／None 才是真的無資料，才該顯示「—」灰色。
                if pd.isna(wr):
                    wr_color = "⚪"
                elif wr >= 60:
                    wr_color = "🟢"
                elif wr >= 50:
                    wr_color = "🟡"
                else:
                    wr_color = "🔴"
                c5.markdown(f"**3Y 回測勝率**  \n{wr_color} **{fmt_pct(wr)}**")
                c6.metric("3Y 總報酬", fmt_pct(row["backtest_total_pnl"]))
                c7.metric("RS 強度", _fmt_rs(row["rs_rating"]))

                fwd_text = _fmt_forward_returns(row)
                if fwd_text:
                    st.caption(fwd_text)

                if pd.notna(row.get("chip_feature")):
                    st.caption(f"🧲 籌碼：{row['chip_feature']}")
                if pd.notna(row.get("chose_reason")):
                    st.caption(f"📐 型態：{row['chose_reason']}")
                if pd.notna(row.get("drive_score")):
                    st.caption(f"🦈 大戶評分：{row['drive_score']} 分")

            with col_right:
                _action_button(row, date_str, "gold")


def _render_chose_tab(df: pd.DataFrame, date_str: str, corp_actions: pd.DataFrame):
    if df.empty:
        st.info("本日無純型態突破（僅 CHOSE 觸發）個股。")
        return

    for _, row in df.iterrows():
        with st.container(border=True):
            col_left, col_right = st.columns([3, 1])
            with col_left:
                st.markdown(
                    f"### {row['stock_code']} {row['stock_name']}"
                    f"  `{_s(row.get('industry_type'))}`"
                )
                # 這個 tab 不顯示 3Y 回測欄位，只需涵蓋 RS/型態辨識的窗口即可。
                badge = _corp_action_badge(row["stock_code"], date_str, corp_actions)
                if badge:
                    st.caption(badge)
                c1, c2 = st.columns(2)
                c1.markdown(f"**型態**：{_s(row.get('pattern_type'), '—')}")
                c2.metric("RS 強度", _fmt_rs(row["rs_rating"]))
                if pd.notna(row.get("chose_reason")):
                    st.caption(f"📐 {row['chose_reason']}")
            with col_right:
                _action_button(row, date_str, "chose")


def _render_drive_tab(df: pd.DataFrame, date_str: str, corp_actions: pd.DataFrame):
    if df.empty:
        st.info("本日無大戶潛伏（僅 DRIVE 觸發）個股。")
        return

    for _, row in df.iterrows():
        with st.container(border=True):
            col_left, col_right = st.columns([3, 1])
            with col_left:
                st.markdown(
                    f"### {row['stock_code']} {row['stock_name']}"
                    f"  `{_s(row.get('industry_type'))}`"
                )
                # 這個 tab 不顯示 3Y 回測欄位，只需涵蓋 RS 計算的窗口即可。
                badge = _corp_action_badge(row["stock_code"], date_str, corp_actions)
                if badge:
                    st.caption(badge)
                c1, c2, c3 = st.columns(3)
                score = row.get("drive_score")
                if pd.isna(score):
                    score_icon = "⚪"
                elif score >= 100:
                    score_icon = "🔴"
                elif score >= 50:
                    score_icon = "🟡"
                else:
                    score_icon = "⚪"
                c1.markdown(f"**大戶評分**  \n{score_icon} **{score if pd.notna(score) else '—'} 分**")
                c2.metric("RS 強度", _fmt_rs(row["rs_rating"]))
                c3.markdown(f"**籌碼特徵**  \n{_s(row.get('chip_feature'), '—')}")
            with col_right:
                _action_button(row, date_str, "drive")


def _render_corp_actions_tab(corp_actions: pd.DataFrame):
    """列出所有已確認的減資/分割/合併調整紀錄，供使用者查核選股雷達的技術指標
    是否受過公司行動影響（見 build_strategy.py 的 stock_corp_actions 調整機制）。"""
    st.caption(
        "以下股票在標記日期發生過減資／分割／合併，官方原始股價資料在該日前後會出現"
        "斷層。選股雷達已自動用調整係數校正 MA／RS強度／回測，讓計算看到連續價格；"
        "「調整係數」= 生效日收盤 ÷ 生效日前一交易日收盤（分割/增股 >1，減資/縮股 <1）。"
    )
    if corp_actions.empty:
        st.info("目前尚無已登記的公司行動紀錄。")
        return

    show_df = corp_actions.rename(columns={
        "stock_code": "代號", "stock_name": "名稱", "ex_date": "生效日",
        "adjust_factor": "調整係數", "action_type": "類型", "note": "備註",
    })
    st.dataframe(
        show_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "調整係數": st.column_config.NumberColumn(format="%.6f"),
        },
    )
