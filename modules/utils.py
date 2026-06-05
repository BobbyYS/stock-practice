"""
modules/utils.py — 共用圖表與技術指標工具
"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
from plotly.subplots import make_subplots


# ------------------------------------------------------------------
# 技術指標計算
# ------------------------------------------------------------------

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    在傳入的 K 線 DataFrame 上原地計算常用技術指標。
    必須包含欄位：open_price, high_price, low_price, close_price, volume
    """
    df = df.copy()
    df.rename(columns={
        "open_price": "open", "high_price": "high",
        "low_price": "low", "close_price": "close", "volume": "volume"
    }, inplace=True)

    df["MA5"]  = ta.sma(df["close"], length=5)
    df["MA10"] = ta.sma(df["close"], length=10)
    df["MA20"] = ta.sma(df["close"], length=20)
    df["MA60"] = ta.sma(df["close"], length=60)
    df["RSI"]  = ta.rsi(df["close"], length=14)

    return df


# ------------------------------------------------------------------
# Plotly 主圖建立
# ------------------------------------------------------------------

def build_chart(
    df: pd.DataFrame,
    title: str = "",
    x_labels: list[str] | None = None,
    show_chips: bool = True,
) -> go.Figure:
    """
    將 K 線、MA、成交量、三大法人買賣超整合進單一 Plotly Figure。
    rows=3, cols=1，共享 X 軸，最大化手機顯存效率。

    Parameters
    ----------
    df        : 已呼叫 add_indicators() 的 DataFrame（含 open/high/low/close/volume 欄位）
    title     : 圖表標題
    x_labels  : 若非 None，以此取代 X 軸日期（用於盲測模式顯示「第N交易日」）
    show_chips: 是否顯示第三列法人買賣超；盲測模式設 True
    """
    rows = 3 if show_chips else 2
    row_heights = [0.55, 0.2, 0.25] if show_chips else [0.65, 0.35]

    fig = make_subplots(
        rows=rows, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
        subplot_titles=("K 線 & 均線", "成交量", "三大法人買賣超（張）") if show_chips
                       else ("K 線 & 均線", "成交量"),
    )

    x_axis = x_labels if x_labels is not None else df.index.astype(str).tolist()

    # ---- Row 1: Candlestick ----
    fig.add_trace(go.Candlestick(
        x=x_axis,
        open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="K線",
        increasing_line_color="#EF5350",
        decreasing_line_color="#26A69A",
        increasing_fillcolor="#EF5350",
        decreasing_fillcolor="#26A69A",
    ), row=1, col=1)

    ma_colors = {"MA5": "#FFD700", "MA10": "#FF6B35", "MA20": "#AEDFF7", "MA60": "#C8A2C8"}
    for ma, color in ma_colors.items():
        if ma in df.columns and df[ma].notna().any():
            fig.add_trace(go.Scatter(
                x=x_axis, y=df[ma], name=ma,
                line=dict(color=color, width=1.2),
                hovertemplate=f"{ma}: %{{y:.2f}}<extra></extra>",
            ), row=1, col=1)

    # ---- Row 2: Volume ----
    vol_colors = [
        "#EF5350" if c >= o else "#26A69A"
        for c, o in zip(df["close"], df["open"])
    ]
    fig.add_trace(go.Bar(
        x=x_axis, y=df["volume"], name="成交量",
        marker_color=vol_colors, showlegend=False,
        hovertemplate="成交量: %{y:,.0f}張<extra></extra>",
    ), row=2, col=1)

    # ---- Row 3: 三大法人買賣超 ----
    if show_chips and all(c in df.columns for c in ["foreign_buy", "investment_buy", "dealer_buy"]):
        for col_name, disp, color in [
            ("foreign_buy",    "外資", "#FF6B6B"),
            ("investment_buy", "投信", "#4ECDC4"),
            ("dealer_buy",     "自營", "#FFE66D"),
        ]:
            fig.add_trace(go.Bar(
                x=x_axis, y=df[col_name], name=disp,
                marker_color=color, opacity=0.8,
                hovertemplate=f"{disp}: %{{y:+,.0f}}張<extra></extra>",
            ), row=3, col=1)

    # ---- Layout ----
    fig.update_layout(
        title=dict(text=title, font=dict(size=14)),
        height=680,
        margin=dict(l=8, r=8, t=40, b=8),
        legend=dict(
            orientation="h", x=0, y=1.02,
            font=dict(size=10),
        ),
        paper_bgcolor="#0E1117",
        plot_bgcolor="#0E1117",
        font=dict(color="#FAFAFA"),
        xaxis_rangeslider_visible=False,   # 手機畫面省空間
        hovermode="x unified",
    )
    fig.update_xaxes(
        showgrid=True, gridcolor="#2D2D2D",
        tickfont=dict(size=9),
    )
    fig.update_yaxes(
        showgrid=True, gridcolor="#2D2D2D",
        tickfont=dict(size=9),
    )
    return fig


# ------------------------------------------------------------------
# 報酬率折線對決圖
# ------------------------------------------------------------------

def build_comparison_chart(
    selected_df: pd.DataFrame,
    others_df: pd.DataFrame,
    selected_label: str = "你選的股票",
    other_label: str = "放棄的篩選股（平均）",
) -> go.Figure:
    """
    模組二：繪製「使用者選中報酬率 vs 其餘篩選股平均報酬率」折線圖。
    selected_df / others_df 需包含 trade_date 和 close_price 欄位，
    且已按 trade_date 排序。
    """
    def calc_return(df: pd.DataFrame) -> pd.Series:
        base = df["close_price"].iloc[0]
        return ((df["close_price"] - base) / base * 100).round(2)

    x = selected_df["trade_date"].astype(str).tolist()
    selected_ret = calc_return(selected_df).tolist()

    # 其餘股票逐日計算報酬率後平均（排除停牌 NaN）
    other_series_list = []
    for code, grp in others_df.groupby("stock_code"):
        grp = grp.sort_values("trade_date")
        if len(grp) >= 2:
            other_series_list.append(calc_return(grp).reset_index(drop=True))

    if other_series_list:
        avg_others = pd.concat(other_series_list, axis=1).mean(axis=1).round(2).tolist()
    else:
        avg_others = [0.0] * len(x)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=selected_ret, name=selected_label,
        line=dict(color="#FF6B35", width=2.5),
        hovertemplate="%{y:+.2f}%<extra>" + selected_label + "</extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=avg_others, name=other_label,
        line=dict(color="#7986CB", width=1.5, dash="dot"),
        hovertemplate="%{y:+.2f}%<extra>" + other_label + "</extra>",
    ))
    fig.add_hline(y=0, line_color="#666", line_dash="dash", line_width=1)

    fig.update_layout(
        title="10 交易日對決報酬率",
        height=340,
        margin=dict(l=8, r=8, t=40, b=8),
        paper_bgcolor="#0E1117",
        plot_bgcolor="#0E1117",
        font=dict(color="#FAFAFA"),
        legend=dict(orientation="h", x=0, y=1.12, font=dict(size=10)),
        hovermode="x unified",
        yaxis=dict(ticksuffix="%"),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#2D2D2D", tickfont=dict(size=9))
    fig.update_yaxes(showgrid=True, gridcolor="#2D2D2D")
    return fig


# ------------------------------------------------------------------
# 輔助：格式化數字顯示
# ------------------------------------------------------------------

def fmt_price(v) -> str:
    return f"{v:.2f}" if v is not None and not pd.isna(v) else "—"


def fmt_pct(v) -> str:
    return f"{v:+.2f}%" if v is not None and not pd.isna(v) else "—"
