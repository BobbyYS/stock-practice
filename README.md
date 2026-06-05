# 📈 股票篩選與模擬進出場修練系統

> 輕量、手機端優化的盤後選股決策與盲測操盤修練工具  
> 技術棧：Streamlit + TiDB Cloud + GitHub Actions

---

## 🚀 快速開始

### 1. 建立 TiDB Cloud 資料庫

在 TiDB Cloud SQL Editor 執行 `sync/init_db.sql` 建立所有資料表。

> ⚠️ 必須將 IP Access List 設為 `0.0.0.0/0`（因應 Streamlit Cloud 浮動 IP）

### 2. 設定連線密鑰（本地開發）

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# 編輯 secrets.toml，填入 TiDB 連線資訊與 user_id
```

### 3. 安裝套件

```bash
pip install -r requirements.txt
```

### 4. 補充歷史資料（首次使用）

```bash
# 補充近 60 天歷史資料
python sync/sync_daily.py --backfill-days 60

# 補充指定日期
python sync/sync_daily.py --date 2026-06-03
```

### 5. 本地啟動

```bash
streamlit run app.py
```

---

## ☁️ 雲端部署（Streamlit Community Cloud）

1. Push 專案至 GitHub（確認 `.gitignore` 已排除 `secrets.toml`）
2. 前往 [share.streamlit.io](https://share.streamlit.io) 連結 Repo
3. 在 **Settings → Secrets** 貼入 `secrets.toml` 內容

### GitHub Actions 自動同步

在 GitHub Repo 的 **Settings → Secrets and variables → Actions** 新增以下 Secrets：

| 名稱 | 說明 |
|------|------|
| `TIDB_HOST` | TiDB 主機位址 |
| `TIDB_PORT` | 通常為 `4000` |
| `TIDB_USER` | 帳號 |
| `TIDB_PASSWORD` | 密碼 |
| `TIDB_DATABASE` | `stock_practice_db` |

排程已設定為**台北時間週一至週五 15:45**（盤後自動同步）。  
也可在 Actions 頁面手動觸發，支援補歷史資料。

---

## 🗂️ 專案結構

```
股市資訊同步/
├── app.py                        # Streamlit 主入口，全域導覽
├── db.py                         # TiDB 連線管理與查詢輔助
├── requirements.txt
├── .gitignore
│
├── modules/
│   ├── daily_board.py            # 模組一：每日策略選股看板
│   ├── daily_decision.py         # 模組二：每日選股決策修練
│   ├── blind_test.py             # 模組三：120 天隨機盲測修練
│   └── utils.py                  # 共用圖表與指標工具
│
├── sync/
│   ├── init_db.sql               # 資料庫 DDL（含修正版）
│   └── sync_daily.py             # 每日 ETL（yfinance + TWSE/TPEX）
│
├── .github/workflows/
│   └── sync.yml                  # GitHub Actions 每日自動同步
│
└── .streamlit/
    └── secrets.toml.example      # 連線設定範本（勿 commit 真實版本）
```

---

## 🔧 WATCHLIST 管理

編輯 `sync/sync_daily.py` 頂部的 `WATCHLIST` 字典新增股票：

```python
WATCHLIST: dict[str, tuple[str, str, str]] = {
    # stock_code   : (yfinance_ticker,  股票名稱,  市場別)
    "2330.TW":  ("2330.TW",  "台積電",  "上市"),
    "6449.TWO": ("6449.TWO", "鈺邦",    "上櫃"),
    # 新增更多...
}
```

---

## 📊 功能模組說明

| 模組 | 功能 |
|------|------|
| 📡 每日選股雷達 | 三分頁看板（黃金交集 / 型態突破 / 大戶潛伏），含帶入決策練習按鈕 |
| 🎯 每日決策修練 | 歷史情境重現 → 主觀挑股 → 10 日後驗證對決折線圖 |
| 🎲 120天盲測修練 | 隱藏代號與日期、電影步進、策略訊號提示、平倉後解鎖股性貼籤 |
| 📋 我的覆盤紀錄 | 交易紀錄損益統計 + 個股股性筆記庫 |

---

## ⚠️ 注意事項

- `daily_strategy_results` 表需由你的外部策略引擎（CHOSE / DRIVE）寫入，ETL 腳本只負責同步 OHLCV 與三大法人。
- `pandas_ta` 在部分新版 pandas 環境下可能需要 `pip install pandas_ta==0.3.14b` 固定版本。
