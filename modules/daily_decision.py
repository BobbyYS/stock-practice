"""
modules/daily_decision.py — 模組二：每日選股決策修練（Daily Decision Mode）
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
from datetime import date, timedelta
import db
from modules.utils import add_indicators, build_chart, build_comparison_chart, fmt_price


# ------------------------------------------------------------------
# 公開入口
# ------------------------------------------------------------------

def render():
    st.header("🎯 每日選股決策修練")

    # ---- 接收來自模組一的 preload 資訊 ----
    preload = st.session_state.pop("decision_preload", None)
    if preload:
        st.session_state["dd_preloaded_code"] = preload["stock_code"]
        st.session_state["dd_preloaded_date"] = preload["trade_date"]
        st.success(
            f"已從選股看板帶入：{preload['stock_code']} {preload['stock_name']}"
            f"（基準日 {preload['trade_date']}）"
        )

    # ---- 情境設定區 ----
    st.subheader("① 選擇歷史情境日")
    latest_str = db.fetch_latest_strategy_date()
    default_dt = (
        date.fromisoformat(latest_str)
        if latest_str
        else date.today() - timedelta(days=30)
    )
    practice_date = st.date_input(
        "歷史基準日（當天策略選股清單）",
        value=date.fromisoformat(st.session_state.get("dd_preloaded_date", str(default_dt))),
        key="dd_date",
    )

    if st.button("載入當日選股清單", key="dd_load"):
        st.session_state["dd_strategy_date"] = str(practice_date)
        # 只清這個查詢自己的快取（強制重抓，避免同一天資料在3600秒TTL內被更新卻看不到）；
        # 不用 st.cache_data.clear()——那會清空全站快取，連K線、公司行動紀錄等不相干的
        # 查詢也一起砍掉，其他頁面下次還要重新打DB，沒必要。
        db.fetch_strategy_by_date.clear()

    strategy_date = st.session_state.get("dd_strategy_date", str(practice_date))

    df_all = db.fetch_strategy_by_date(strategy_date)
    if df_all.empty:
        st.warning(f"📭 {strategy_date} 無策略選股資料。")
        return

    stock_options = [
        f"{r['stock_code']} {r['stock_name']}" for _, r in df_all.iterrows()
    ]
    preloaded_code = st.session_state.get("dd_preloaded_code", "")
    preload_index = next(
        (i for i, o in enumerate(stock_options) if o.startswith(preloaded_code)), 0
    )

    # ---- 個股瀏覽 ----
    st.subheader("② 逐一審閱篩選個股")
    selected_option = st.selectbox(
        "切換個股（查看 K 線與籌碼）",
        stock_options,
        index=preload_index,
        key="dd_select_stock",
    )
    view_code = selected_option.split(" ")[0]

    # 撈取該股在基準日之前 270 天的 K 線（供技術分析判斷）
    view_end = strategy_date
    view_start = str(date.fromisoformat(strategy_date) - timedelta(days=270))
    kline_df = db.fetch_kline_range(view_code, view_start, view_end)

    if kline_df.empty:
        st.warning(f"{view_code} 在此期間無 K 線資料。")
    else:
        kline_w_ind = add_indicators(kline_df)
        kline_w_ind = kline_w_ind.reset_index(drop=True)
        fig = build_chart(kline_w_ind, title=f"{view_code} — 基準日前 K 線走勢")
        st.plotly_chart(fig, use_container_width=True)

    # ---- 下達決策 ----
    st.divider()
    st.subheader("③ 下達聚焦決策")
    st.caption("選出 1~2 檔最看好的個股，設定主觀停損停利，記錄進場決策。")

    col1, col2 = st.columns(2)
    with col1:
        focus_stocks = st.multiselect(
            "勾選最看好個股（最多2檔）",
            stock_options,
            max_selections=2,
            key="dd_focus",
        )
    with col2:
        entry_price_input = st.number_input(
            "模擬進場價格",
            min_value=0.01, step=0.1, format="%.2f",
            key="dd_entry_price",
        )

    col3, col4 = st.columns(2)
    with col3:
        stop_loss_input = st.number_input(
            "主觀停損價",
            min_value=0.01, step=0.1, format="%.2f",
            key="dd_stop_loss",
        )
    with col4:
        take_profit_input = st.number_input(
            "主觀停利價",
            min_value=0.01, step=0.1, format="%.2f",
            key="dd_take_profit",
        )

    if st.button("✅ 確認進場 — 記錄決策", key="dd_confirm"):
        if not focus_stocks:
            st.warning("請先勾選個股。")
        elif entry_price_input <= 0:
            st.warning("請填入有效的進場價格。")
        else:
            user_id = db.get_user_id()
            for opt in focus_stocks:
                code = opt.split(" ")[0]
                db.execute(
                    """
                    INSERT INTO trading_practice_records
                        (user_id, stock_code, practice_mode, practice_start_date,
                         entry_date, entry_price, user_stop_loss, user_take_profit, status)
                    VALUES
                        (:uid, :code, 'DAILY_DECISION', :start_date,
                         :entry_date, :ep, :sl, :tp, '持股中')
                    """,
                    {
                        "uid": user_id,
                        "code": code,
                        "start_date": strategy_date,
                        "entry_date": strategy_date,
                        "ep": entry_price_input,
                        "sl": stop_loss_input if stop_loss_input > 0 else None,
                        "tp": take_profit_input if take_profit_input > 0 else None,
                    },
                )
            st.success(f"🎉 已記錄 {len(focus_stocks)} 檔決策，狀態：持股中")
            st.session_state["dd_entered_codes"] = [o.split(" ")[0] for o in focus_stocks]
            st.session_state["dd_all_codes"] = [o.split(" ")[0] for o in stock_options]

    # ---- 未來驗證 ----
    st.divider()
    _render_forward_comparison(strategy_date)


# ------------------------------------------------------------------
# 未來 10 交易日對決折線圖
# ------------------------------------------------------------------

def _render_forward_comparison(strategy_date: str):
    st.subheader("④ ⏱️ 推進未來 10 交易日驗證")
    st.caption("查看你鎖定的個股報酬率 vs 放棄的篩選股平均報酬率")

    entered_codes = st.session_state.get("dd_entered_codes", [])
    all_codes = st.session_state.get("dd_all_codes", [])

    if not entered_codes:
        st.info("請先在上方「下達聚焦決策」後，再進行未來驗證。")
        return

    if st.button("▶ 拉取未來 10 交易日真實數據", key="dd_forward"):
        fwd_start = strategy_date
        fwd_end = str(date.fromisoformat(strategy_date) + timedelta(days=20))

        # 所選個股資料（取第一檔，多選時各自繪製）
        selected_frames = []
        for code in entered_codes:
            fdf = db.fetch_kline_range(code, fwd_start, fwd_end)
            if not fdf.empty:
                fdf = fdf.head(11)  # 含起點，共11筆 = 10個漲跌日
                fdf["stock_code"] = code
                selected_frames.append(fdf)

        # 其餘放棄的個股
        other_codes = [c for c in all_codes if c not in entered_codes]
        other_frames = []
        for code in other_codes:
            odf = db.fetch_kline_range(code, fwd_start, fwd_end)
            if not odf.empty:
                odf = odf.head(11)
                odf["stock_code"] = code
                other_frames.append(odf)

        if not selected_frames:
            st.warning("所選個股在此區間無後續 K 線資料。")
            return

        others_combined = pd.concat(other_frames) if other_frames else pd.DataFrame()

        for sel_df in selected_frames:
            code = sel_df["stock_code"].iloc[0]
            label = f"你選的 {code}"
            fig = build_comparison_chart(
                sel_df, others_combined,
                selected_label=label,
                other_label=f"放棄的 {len(other_codes)} 檔（平均）",
            )
            st.plotly_chart(fig, use_container_width=True)
