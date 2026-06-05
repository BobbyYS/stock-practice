"""
db.py — TiDB Cloud 資料庫連線管理
提供 SQLAlchemy engine 建立、常用查詢輔助，以及 Streamlit cache 裝飾器。
"""
from __future__ import annotations

import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
import pandas as pd
from typing import Any


# ------------------------------------------------------------------
# Engine 建立（全域單例，避免重複建立連線）
# ------------------------------------------------------------------

@st.cache_resource
def get_engine():
    """從 st.secrets 讀取 TiDB 連線參數，建立 SQLAlchemy engine。"""
    cfg = st.secrets["tidb"]
    ssl_args: dict = {}
    if cfg.get("ssl_ca"):
        ssl_args = {"ssl": {"ca": cfg["ssl_ca"]}}

    conn_url = (
        f"mysql+pymysql://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}"
        "?charset=utf8mb4"
    )
    engine = create_engine(
        conn_url,
        poolclass=QueuePool,
        pool_size=3,
        max_overflow=5,
        pool_pre_ping=True,          # 自動偵測斷線並重連
        connect_args=ssl_args,
    )
    return engine


# ------------------------------------------------------------------
# 查詢輔助
# ------------------------------------------------------------------

def query_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    """執行 SELECT 並回傳 DataFrame。"""
    engine = get_engine()
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def execute(sql: str, params: dict | None = None) -> Any:
    """執行 INSERT / UPDATE / DELETE，回傳 lastrowid。"""
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text(sql), params or {})
        return result.lastrowid


# ------------------------------------------------------------------
# 帶快取的常用查詢（避免重複 IO）
# ------------------------------------------------------------------

@st.cache_data(ttl=3600)
def fetch_latest_strategy_date() -> str | None:
    """取得 daily_strategy_results 表中最新的交易日（字串）。"""
    df = query_df("SELECT MAX(trade_date) AS max_date FROM daily_strategy_results")
    val = df["max_date"].iloc[0]
    return str(val) if val else None


@st.cache_data(ttl=3600)
def fetch_strategy_by_date(trade_date: str) -> pd.DataFrame:
    """撈出指定日期全部策略選股紀錄，JOIN stock_info 取中文名稱。"""
    sql = """
        SELECT
            d.trade_date, d.stock_code,
            s.stock_name, s.industry_type,
            d.is_chose_trigger, d.pattern_type, d.chose_reason,
            d.is_drive_trigger, d.drive_score, d.rs_rating, d.chip_feature,
            d.suggested_entry, d.stop_loss, d.target_price_1, d.target_price_2,
            d.backtest_win_rate, d.backtest_total_pnl
        FROM daily_strategy_results d
        JOIN stock_info s USING (stock_code)
        WHERE d.trade_date = :dt
        ORDER BY d.drive_score DESC, d.rs_rating DESC
    """
    return query_df(sql, {"dt": trade_date})


@st.cache_data(ttl=3600)
def fetch_kline(stock_code: str, start_date: str, limit: int = 120) -> pd.DataFrame:
    """
    撈出指定股票從 start_date 起最多 limit 筆 K 線與籌碼資料。
    用於盲測模式的一次性批量載入。
    """
    sql = """
        SELECT trade_date, open_price, high_price, low_price, close_price,
               volume, foreign_buy, investment_buy, dealer_buy
        FROM stock_daily_data
        WHERE stock_code = :code AND trade_date >= :start
        ORDER BY trade_date ASC
        LIMIT :lim
    """
    return query_df(sql, {"code": stock_code, "start": start_date, "lim": limit})


@st.cache_data(ttl=3600)
def fetch_kline_range(stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """撈出指定股票在日期區間內的 K 線資料（用於每日決策模式）。"""
    sql = """
        SELECT trade_date, open_price, high_price, low_price, close_price,
               volume, foreign_buy, investment_buy, dealer_buy
        FROM stock_daily_data
        WHERE stock_code = :code
          AND trade_date BETWEEN :start AND :end
        ORDER BY trade_date ASC
    """
    return query_df(sql, {"code": stock_code, "start": start_date, "end": end_date})


@st.cache_data(ttl=3600)
def fetch_strategy_signals_for_stock(
    stock_code: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """取得指定股票在日期區間內的策略觸發紀錄（用於盲測步進提示）。"""
    sql = """
        SELECT trade_date, is_chose_trigger, is_drive_trigger,
               drive_score, rs_rating, chip_feature,
               pattern_type, chose_reason,
               suggested_entry, stop_loss, target_price_1, target_price_2
        FROM daily_strategy_results
        WHERE stock_code = :code
          AND trade_date BETWEEN :start AND :end
        ORDER BY trade_date ASC
    """
    return query_df(sql, {"code": stock_code, "start": start_date, "end": end_date})


@st.cache_data(ttl=300)
def fetch_distinct_stocks() -> list[str]:
    """取得 stock_daily_data 中所有不重複的 stock_code 清單。"""
    df = query_df("SELECT DISTINCT stock_code FROM stock_daily_data ORDER BY stock_code")
    return df["stock_code"].tolist()


def fetch_random_blind_stock() -> tuple[str, str]:
    """
    使用高效率的 COUNT + OFFSET 方式隨機抽取一檔股票及其安全盲測起點。
    回傳 (stock_code, start_date_str)
    """
    import random

    stocks = fetch_distinct_stocks()
    if not stocks:
        raise ValueError("stock_daily_data 尚無資料，請先執行 ETL 同步。")

    stock_code = random.choice(stocks)

    # 撈出該股所有交易日（已排序）
    df = query_df(
        "SELECT trade_date FROM stock_daily_data "
        "WHERE stock_code = :code ORDER BY trade_date ASC",
        {"code": stock_code},
    )
    dates = df["trade_date"].astype(str).tolist()
    if len(dates) <= 120:
        raise ValueError(f"{stock_code} 歷史資料不足 120 天，請換一檔。")

    # 安全起點：從有效 pool 中隨機選（確保後面仍有 120 天）
    safe_pool = dates[: len(dates) - 120]
    start_date = random.choice(safe_pool)
    return stock_code, start_date


def get_user_id() -> str:
    """從 secrets 讀取 user_id（提供預設值以防未設定）。"""
    try:
        return st.secrets["app"]["user_id"]
    except Exception:
        return "default_user"
