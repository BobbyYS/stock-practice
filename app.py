"""
app.py — 股票篩選與模擬進出場修練系統
Streamlit 主入口，負責全域導覽、Session State 初始化。
"""
from __future__ import annotations

import streamlit as st
from streamlit_option_menu import option_menu

# ------------------------------------------------------------------
# 頁面基礎設定（必須是第一個 Streamlit 指令）
# ------------------------------------------------------------------
st.set_page_config(
    page_title="股票操盤修練系統",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",   # 手機端預設收起側欄
)

# ------------------------------------------------------------------
# 全域 CSS：手機端優化
# ------------------------------------------------------------------
st.markdown("""
<style>
/* Streamlit Community Cloud 的固定 header/工具列會蓋住頂部自訂導覽列；
   本 app 以 option_menu 自行導覽、未使用 sidebar，直接隱藏預設 header。 */
header[data-testid="stHeader"] { display: none; }
[data-testid="stToolbar"] { display: none; }
/* 縮小手機端頂部留白（header 已隱藏，留一點呼吸空間即可） */
.block-container { padding-top: 1.5rem !important; }
/* 按鈕全寬 */
.stButton > button { width: 100%; border-radius: 8px; }
/* 指標數字加大 */
[data-testid="metric-container"] { font-size: 1rem; }
/* 深色背景卡片邊框 */
[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"] {
    border-radius: 8px;
}
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------
# 頁面導覽
# ------------------------------------------------------------------

PAGES = {
    "每日選股雷達": "📡",
    "每日決策修練": "🎯",
    "120天盲測修練": "🎲",
    "我的覆盤紀錄": "📋",
}

# 支援從模組一「帶入決策練習」按鈕觸發的強制跳轉
if "nav_page" in st.session_state:
    default_page = list(PAGES.keys()).index(st.session_state.pop("nav_page"))
else:
    default_page = 0

selected = option_menu(
    menu_title=None,
    options=list(PAGES.keys()),
    icons=[v.replace(" ", "") for v in PAGES.values()],
    default_index=default_page,
    orientation="horizontal",
    styles={
        "container": {"padding": "0!important", "margin-bottom": "0.5rem"},
        "nav-link": {"font-size": "13px", "padding": "6px 4px"},
        "nav-link-selected": {"background-color": "#FF6B35"},
    },
    key="main_nav",
)

# ------------------------------------------------------------------
# 覆盤紀錄頁（內嵌，定義在路由之前）
# ------------------------------------------------------------------

def _render_review_page():
    import db
    st.header("📋 我的覆盤紀錄")

    user_id = db.get_user_id()

    tab_trades, tab_notes = st.tabs(["交易練習紀錄", "股性筆記庫"])

    with tab_trades:
        df = db.query_df(
            """
            SELECT t.trade_id, t.practice_mode, t.stock_code,
                   s.stock_name, t.practice_start_date,
                   t.entry_date, t.entry_price,
                   t.user_stop_loss, t.user_take_profit,
                   t.exit_date, t.exit_price,
                   t.status, t.pnl_percent
            FROM trading_practice_records t
            JOIN stock_info s USING (stock_code)
            WHERE t.user_id = :uid
            ORDER BY t.created_at DESC
            LIMIT 100
            """,
            {"uid": user_id},
        )
        if df.empty:
            st.info("尚無練習紀錄。")
        else:
            # 標色：損益正負
            def _style_pnl(val):
                if val is None or str(val) == "nan":
                    return ""
                return "color: #EF5350" if float(val) < 0 else "color: #26A69A"

            st.dataframe(
                df.style.applymap(_style_pnl, subset=["pnl_percent"]),
                use_container_width=True,
                hide_index=True,
            )

    with tab_notes:
        notes = db.query_df(
            """
            SELECT n.note_id, n.stock_code, s.stock_name,
                   n.stock_tags, n.review_content, n.updated_at
            FROM stock_character_notes n
            JOIN stock_info s USING (stock_code)
            WHERE n.user_id = :uid
            ORDER BY n.updated_at DESC
            LIMIT 50
            """,
            {"uid": user_id},
        )
        if notes.empty:
            st.info("尚無股性筆記。")
        else:
            for _, row in notes.iterrows():
                with st.expander(
                    f"{row['stock_code']} {row['stock_name']}  —  {row['updated_at']}"
                ):
                    if row["stock_tags"]:
                        tags = row["stock_tags"].split(",")
                        st.markdown(
                            " ".join(f"`{t.strip()}`" for t in tags if t.strip())
                        )
                    st.markdown(row["review_content"] or "（無覆盤文字）")


# ------------------------------------------------------------------
# 路由至各模組（函式已定義於上方）
# ------------------------------------------------------------------

if selected == "每日選股雷達":
    from modules.daily_board import render
    render()

elif selected == "每日決策修練":
    from modules.daily_decision import render
    render()

elif selected == "120天盲測修練":
    from modules.blind_test import render
    render()

elif selected == "我的覆盤紀錄":
    _render_review_page()
