-- ============================================================
-- 股票篩選與模擬進出場修練系統 — 資料庫初始化 DDL
-- 修正版：修正 status 預設值矛盾、補上 FK、加 user_id 支援多使用者
-- 在 TiDB Cloud SQL Editor 中直接執行
-- ============================================================

CREATE DATABASE IF NOT EXISTS stock_practice_db
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE stock_practice_db;

-- ==========================================
-- TABLE 1: 股票基本母檔表
-- ==========================================
CREATE TABLE IF NOT EXISTS stock_info (
    stock_code   VARCHAR(10)  NOT NULL COMMENT '股票代碼，格式如 2330.TW 或 2061.TWO',
    stock_name   VARCHAR(50)  NOT NULL COMMENT '股票中文名稱',
    market_type  VARCHAR(20)  DEFAULT NULL COMMENT '上市 / 上櫃 / 美股',
    industry_type VARCHAR(50) DEFAULT NULL COMMENT '產業別，如：半導體、電器電纜',
    created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ==========================================
-- TABLE 2: 每日 K 線與籌碼面數據表（高頻增長核心數據表）
-- ==========================================
CREATE TABLE IF NOT EXISTS stock_daily_data (
    stock_code    VARCHAR(10)    NOT NULL COMMENT '股票代碼',
    trade_date    DATE           NOT NULL COMMENT '交易日期',
    open_price    DECIMAL(10, 2) NOT NULL COMMENT '開盤價',
    high_price    DECIMAL(10, 2) NOT NULL COMMENT '最高價',
    low_price     DECIMAL(10, 2) NOT NULL COMMENT '最低價',
    close_price   DECIMAL(10, 2) NOT NULL COMMENT '收盤價',
    volume        BIGINT         NOT NULL COMMENT '交易張數',
    foreign_buy   BIGINT DEFAULT 0 COMMENT '外資買賣超（正為買，負為賣）',
    investment_buy BIGINT DEFAULT 0 COMMENT '投信買賣超',
    dealer_buy    BIGINT DEFAULT 0 COMMENT '自營商買賣超',
    PRIMARY KEY (stock_code, trade_date),
    KEY idx_trade_date (trade_date) COMMENT '按日期範圍跨股檢索優化'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ==========================================
-- TABLE 2b: 盤中即時報價快照表（每檔僅保留最新一筆，每次同步覆蓋）
-- 資料來源：TWSE/TPEX MIS 即時報價 API（mis.twse.com.tw）
-- ==========================================
CREATE TABLE IF NOT EXISTS stock_intraday_quotes (
    stock_code    VARCHAR(10)    NOT NULL COMMENT '股票代碼，格式如 2330.TW / 8043.TWO',
    trade_date    DATE           NOT NULL COMMENT 'API 回報的交易日期',
    last_price    DECIMAL(10, 2) DEFAULT NULL COMMENT '最新成交價（無成交時以買價/開盤價回補）',
    open_price    DECIMAL(10, 2) DEFAULT NULL COMMENT '當日開盤價',
    high_price    DECIMAL(10, 2) DEFAULT NULL COMMENT '當日最高價',
    low_price     DECIMAL(10, 2) DEFAULT NULL COMMENT '當日最低價',
    prev_close    DECIMAL(10, 2) DEFAULT NULL COMMENT '昨日收盤價',
    change_amount DECIMAL(10, 2) DEFAULT NULL COMMENT '漲跌（last_price - prev_close）',
    change_pct    DECIMAL(7, 2)  DEFAULT NULL COMMENT '漲跌幅 %',
    volume        BIGINT         DEFAULT 0 COMMENT '當日累積成交張數',
    bid_price     DECIMAL(10, 2) DEFAULT NULL COMMENT '最佳一檔買價',
    ask_price     DECIMAL(10, 2) DEFAULT NULL COMMENT '最佳一檔賣價',
    quote_time    VARCHAR(8)     DEFAULT NULL COMMENT 'API 撮合時間 HH:MM:SS',
    updated_at    TIMESTAMP      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        COMMENT '本筆快照寫入時間',
    PRIMARY KEY (stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ==========================================
-- TABLE 3: 每日策略選股與量化因子表（CHOSE & DRIVE 數據核心）
-- ==========================================
CREATE TABLE IF NOT EXISTS daily_strategy_results (
    id               BIGINT AUTO_INCREMENT NOT NULL,
    trade_date       DATE           NOT NULL COMMENT '篩選日期',
    stock_code       VARCHAR(10)    NOT NULL COMMENT '股票代碼',
    is_chose_trigger TINYINT(1)     NOT NULL DEFAULT 0 COMMENT '型態掃描CHOSE是否觸發',
    pattern_type     VARCHAR(50)    DEFAULT NULL COMMENT '技術型態，如：高窄旗型、買進跳空',
    chose_reason     VARCHAR(255)   DEFAULT NULL COMMENT '型態觸發原因',
    is_drive_trigger TINYINT(1)     NOT NULL DEFAULT 0 COMMENT '大戶動態DRIVE是否觸發',
    drive_score      INT            DEFAULT NULL COMMENT '大戶動態評分：30 / 50 / 100',
    rs_rating        DECIMAL(5, 1)  DEFAULT NULL COMMENT 'RS 相對強度指標',
    chip_feature     VARCHAR(100)   DEFAULT NULL COMMENT '吸籌特徵，如：MVP吸籌+超強RS',
    suggested_entry  DECIMAL(10, 2) DEFAULT NULL COMMENT '推薦佈局價 / Entry',
    stop_loss        DECIMAL(10, 2) DEFAULT NULL COMMENT '防禦停損價 / Stop',
    target_price_1   DECIMAL(10, 2) DEFAULT NULL COMMENT '第一停利目標價',
    target_price_2   DECIMAL(10, 2) DEFAULT NULL COMMENT '第二停利目標價',
    backtest_win_rate   DECIMAL(5, 2) DEFAULT NULL COMMENT '策略3Y歷史回測勝率%',
    backtest_total_pnl  DECIMAL(7, 2) DEFAULT NULL COMMENT '策略3Y回測總報酬%',
    fwd_return_5d_win_rate  DECIMAL(5, 2) DEFAULT NULL COMMENT 'CHOSE進場後第5個交易日仍為正報酬的歷史機率%（樣本不足時為NULL）',
    fwd_return_10d_win_rate DECIMAL(5, 2) DEFAULT NULL COMMENT 'CHOSE進場後第10個交易日仍為正報酬的歷史機率%（樣本不足時為NULL）',
    fwd_return_20d_win_rate DECIMAL(5, 2) DEFAULT NULL COMMENT 'CHOSE進場後第20個交易日仍為正報酬的歷史機率%（樣本不足時為NULL）',
    fwd_return_5d_n  INT DEFAULT NULL COMMENT '5日統計的樣本數（歷史進場次數，扣除資料不足N天的進場點）',
    fwd_return_10d_n INT DEFAULT NULL COMMENT '10日統計的樣本數',
    fwd_return_20d_n INT DEFAULT NULL COMMENT '20日統計的樣本數',
    PRIMARY KEY (id),
    UNIQUE KEY uq_date_code (trade_date, stock_code),
    KEY idx_date_modules (trade_date, is_chose_trigger, is_drive_trigger),
    CONSTRAINT fk_strategy_stock FOREIGN KEY (stock_code) REFERENCES stock_info (stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ==========================================
-- TABLE 4: 模擬交易與練習模式紀錄表
-- 修正：補上 stock_code FK、修正 status 預設值為 '純觀望'、加 user_id
-- ==========================================
CREATE TABLE IF NOT EXISTS trading_practice_records (
    trade_id           BIGINT AUTO_INCREMENT NOT NULL,
    user_id            VARCHAR(50)    NOT NULL DEFAULT 'default_user' COMMENT '使用者識別 ID',
    stock_code         VARCHAR(10)    NOT NULL COMMENT '股票代碼',
    practice_mode      VARCHAR(20)    NOT NULL COMMENT '練習模式: BLIND_120 或 DAILY_DECISION',
    practice_start_date DATE          NOT NULL COMMENT '練習啟動時的歷史基準日期',
    entry_date         DATE           DEFAULT NULL COMMENT '模擬買進的歷史日期',
    entry_price        DECIMAL(10, 2) DEFAULT NULL COMMENT '模擬買進價格',
    entry_qty          INT            DEFAULT 1000 COMMENT '模擬交易數量（張）',
    user_stop_loss     DECIMAL(10, 2) DEFAULT NULL COMMENT '使用者設定的停損價',
    user_take_profit   DECIMAL(10, 2) DEFAULT NULL COMMENT '使用者設定的停利價',
    exit_date          DATE           DEFAULT NULL COMMENT '出場日期',
    exit_price         DECIMAL(10, 2) DEFAULT NULL COMMENT '出場價格',
    -- 修正：預設值改為 '純觀望'，與狀態清單一致
    status             VARCHAR(20)    NOT NULL DEFAULT '純觀望'
        COMMENT '狀態: 純觀望 / 持股中 / 已結案_停損 / 已結案_停利',
    pnl_percent        DECIMAL(5, 2)  DEFAULT NULL COMMENT '最終損益百分比%',
    created_at         TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_id),
    KEY idx_practice_user (user_id, practice_mode),
    CONSTRAINT fk_practice_stock FOREIGN KEY (stock_code) REFERENCES stock_info (stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ==========================================
-- TABLE 5: 個股股性標籤與覆盤筆記表
-- ==========================================
CREATE TABLE IF NOT EXISTS stock_character_notes (
    note_id        BIGINT AUTO_INCREMENT NOT NULL,
    user_id        VARCHAR(50)  NOT NULL DEFAULT 'default_user' COMMENT '使用者識別 ID',
    stock_code     VARCHAR(10)  NOT NULL COMMENT '股票代碼',
    trade_id       BIGINT       DEFAULT NULL COMMENT '關聯的交易練習ID，可為NULL',
    stock_tags     VARCHAR(255) DEFAULT NULL COMMENT '逗號分隔股性標籤，如: 假突破常客,投信鎖碼大戶股',
    review_content TEXT         COMMENT '個人覆盤心得與操盤筆記',
    updated_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (note_id),
    KEY idx_note_stock (stock_code),
    KEY idx_note_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ==========================================
-- TABLE 6: 個股公司行動調整係數表
-- 用途：減資/分割/合併等會讓 stock_daily_data 出現價格斷層的事件，在此登記調整係數。
-- 非破壞性設計：stock_daily_data 原始資料永遠不動，調整只在讀取分析（_load_bars）當下套用。
-- 沒有可靠的官方 API 能自動回溯偵測這類事件，因此採「異常偵測 log + 人工確認寫入」流程
-- （見 sync/market_sync.py 的 _detect_price_anomalies）。
-- ==========================================
CREATE TABLE IF NOT EXISTS stock_corp_actions (
    stock_code    VARCHAR(10)    NOT NULL COMMENT '股票代碼',
    ex_date       DATE           NOT NULL COMMENT '生效日（復牌/分割生效當天，調整套用於此日之前的資料）',
    adjust_factor DECIMAL(12, 6) NOT NULL COMMENT '調整係數 = 生效日收盤 / 生效日前一交易日收盤，套用於價格；量能則除以此係數',
    action_type   VARCHAR(20)    DEFAULT NULL COMMENT '公司行動類型：減資 / 分割 / 合併，僅供備註',
    note          VARCHAR(255)   DEFAULT NULL COMMENT '備註，如資料來源、人工確認方式',
    created_at    TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_code, ex_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
