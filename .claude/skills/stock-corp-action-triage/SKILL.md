---
name: stock-corp-action-triage
description: Triage suspected stock corporate actions (capital reduction / split / merger) in this project's TiDB `stock_daily_data`, distinguishing them from genuine crashes/rallies AND from systemic data-sync gaps (this project has known multi-week periods of partial market coverage — see Step 0), then register confirmed events in `stock_corp_actions` so `build_strategy.py` reads a continuous, unbroken price series. Use when `market_sync.py`'s anomaly log flags a stock, when someone asks "為什麼這檔股票跌/漲這麼多", when working through `sync/corp_action_candidates.csv`, or when asked to sync/backfill/audit price data for split or capital-reduction distortions.
---

# 股票公司行動判斷（減資 / 分割 / 合併 vs 真實漲跌）

## 背景

這個專案（stock-practice-main）用 TWSE/TPEX 官方逐日歸檔資料同步進 TiDB 的
`stock_daily_data`，**原始資料不做除權息還原**。當一檔股票發生減資、股票分割、
股份合併等「股數變動」的公司行動時，官方資料在生效日前後會出現價格斷層（同一檔
股票，前一天和當天的收盤價差了好幾倍），如果放著不管，會讓 `build_strategy.py`
裡的 MA50/MA200/年高/RS強度/3年回測 全部算錯——可能讓真正該選中的股票被濾掉，
也可能反過來把資料斷層誤判成「爆量突破」生出假的買進訊號。

修正機制已經做好了（非破壞性、view-time 調整，`stock_daily_data` 原始表永遠不動）：

- `stock_corp_actions` 表：登記 `(stock_code, ex_date, adjust_factor, action_type, note)`
- `sync/build_strategy.py` 的 `_load_bars()` / `_apply_corp_actions()`：讀取歷史序列時，
  自動把 `ex_date` 之前的 OHLC 乘上 `adjust_factor`（volume 除以同一係數），下游的
  `analyze_chose`/`analyze_drive`/`backtest_3y` 完全不用改
- `sync/market_sync.py` 的 `_detect_price_anomalies()`：每次同步完一天資料後，自動比對
  每檔股票「前一個既有交易日」收盤，單日變動 ≥30%（`ANOMALY_PCT_THRESHOLD`）就記警告 log

**這個機制唯一沒做、也做不到的一步，就是判斷「這筆異常到底是不是真的公司行動」**——
沒有一支免費官方 API 能可靠回溯查詢任意歷史日期的減資/分割事件（已實測見下），所以
這一步永遠需要人工或 AI 逐筆確認。這個 skill 就是那套判斷流程。

## 資料庫連線

DB 設定在 `.streamlit/secrets.toml`（同目錄下的 `db.py` 會自動讀取）。所有查詢都可以用
專案既有的 `db.query_df(sql, params)` / `db.execute(sql, params)`，例如：

```python
import sys, os
from pathlib import Path
os.environ.setdefault("STREAMLIT_SECRETS_TOML", str(Path(".streamlit/secrets.toml").resolve()))
import db
```

## Step 0 — 先排除「系統性資料缺口」，這不是公司行動，也最容易一次解釋掉一大批候選

**這是 2026-07-22 實際跑過一輪候選清單後才發現的重要教訓，優先權在個股判斷之前。**
`corp_action_candidates.csv` 原本 458 筆候選，事後查出其中 **214 筆（47%）根本不是任何
公司的行動，而是這個專案自己同步歷史資料時留下的『部分覆蓋』缺口**：某一段期間
（例如整個月）全市場只同步到六成的股票，另外四成股票在那段期間完全沒有資料列——
等到之後補齊部分股票時，這些「消失了一個月」的股票會被 `LAG()` 抓到很久以前的
`prev_close`，算出一個假的巨幅漲跌，跟真正的減資/分割長得很像（都有「缺口」）。

**判斷特徵**：同一個 `trade_date`（或同一小段區間），**好幾檔互不相關產業的股票同時
「復活」**，且該股票平常覆蓋率很高（本來就常態交易）。任何一檔公司的減資/分割都是
單一公司事件，不會跟其他無關公司同一天發生。

**檢查方式**（處理任何候選前，先跑這個，比查新聞便宜很多）：

```sql
-- 1. 抓出全市場每日股票數，跟半年內的最大值比，找出覆蓋率明顯偏低的區間
SELECT trade_date, c, roll_max, c/roll_max AS coverage_ratio FROM (
    SELECT trade_date, COUNT(*) c,
           MAX(COUNT(*)) OVER (ORDER BY trade_date ROWS BETWEEN 60 PRECEDING AND 60 FOLLOWING) AS roll_max
    FROM stock_daily_data GROUP BY trade_date
) t WHERE c/roll_max < 0.85 ORDER BY trade_date
```

（用 rolling **MAX** 當基準，不要用 rolling median/average——如果缺口本身長達一整個
月，中位數會被缺口自己拉低，反而偵測不到。這個坑已經踩過一次。）

若候選的 `[prev_date, trade_date]` 區間跟上面查出的低覆蓋率區間重疊 → **標記為「資料
缺口待補」，不要當成公司行動處理**，也先不要下判斷說它是真實漲跌——正確做法是**把
那段期間的資料補齊**（見下方「已知的資料缺口」表格與補齊注意事項），補完後這檔股票
的 `prev_close` 會自動指向正確的前一交易日，斷層通常就消失了，不需要再判斷。

### 已知的資料缺口 — 已於 2026-07-22 全部修復

原本查出 2018~2026 共 116 個交易日全市場覆蓋率明顯偏低（最大一次是 2026-05-06~07-02
連續41天只同步到~74%），已經全部補齊，目前 2017 至今零缺口（用 Step 0 的 SQL 重新
掃過一次確認）。**`market_sync.py` 的 `_upsert_ohlcv`/`_upsert_info` 也已經改成批次
INSERT**（每 300 檔一次連線，原本是每檔股票各開一次連線）：優化前單一天要 8 分鐘以上
還跑不完，優化後單一天約 10~20 秒，114 天實測總共約 756 秒。以後若再發現類似缺口，
直接呼叫 `market_sync.sync_day(d)` 逐天補就很快，不需要再顧慮效能問題。

**背景執行的坑**：用背景 bash 補資料時，不要在已經設定背景執行模式的情況下自己又在
指令尾端加 `&`——那會變成雙重背景化，外層工具以為指令立刻結束，實際 python 行程變成
孤兒行程，跑一小段就被中斷（此專案debug時真的踩過兩次，第二次才發現）。只靠外層的
背景執行參數，不要自己再加 `&`。

修復後重新掃描全市場異常，`corp_action_candidates.csv` 的候選數會變：資料缺口修好後，
有些原本「同一天一堆不相關股票集體復活」的假候選會自然消失（`prev_close` 改指向正確
的前一交易日），但也會有一些原本被缺口蓋住、真正屬於該股票自己的異常重新浮現——不能
假設資料缺口修復後那批候選就全部沒事了，要重新用 Step 0 的查詢跑一次比對。

## 判斷流程（排除 Step 0 之後，每一筆候選都照這個順序走）

### Step 1 — 拿到候選的基本資料

輸入通常是 `market_sync.py` log 裡的一行警告（含 stock_code、前後日期、前後收盤價、估計
adjust_factor），或是 `sync/corp_action_candidates.csv` 裡的一列。先查它的中文名稱（news
搜尋要用中文名，不能只搜代號）：

```sql
SELECT stock_code, stock_name, market_type, industry_type
FROM stock_info WHERE stock_code = :code
```

再拉出事件前後 ±15 個交易日的價格，確認斷層的確切日期、前後收盤、成交量：

```sql
SELECT trade_date, open_price, high_price, low_price, close_price, volume
FROM stock_daily_data
WHERE stock_code = :code AND trade_date BETWEEN :start AND :end
ORDER BY trade_date
```

### Step 2 — 看「有沒有停牌缺口」，這是最強的判斷依據

算出 `gap_days = 異常當天日期 - 前一個既有交易日日期`（注意是「前一個既有交易日」，
不是日曆上的前一天——減資/分割通常會停牌數天到數週，中間完全沒有資料）。

- **`gap_days >= 5`（扣掉正常週末，代表確實停牌過）→ 高度懷疑是真公司行動**，繼續 Step 3
- **`gap_days < 5`（交易連續、沒停牌）→ 極可能是真實市場漲跌，不是公司行動**。
  減資/分割/合併在台股都需要停牌換發新股，正常交易中不會無縫出現這種價格跳躍。
  仍可以做 Step 4 的新聞查證確認一下（尤其變動超過50%的），但預設立場是「這是真的」，
  不要輕易當成資料異常去調整——調整了反而會把真實的大漲/大跌訊號洗掉。

  **實測案例**：5386.TWO（青雲）2026-07-20 單日 -37.8%，連續交易無缺口，查證後是
  總經理因晶片走私案遭收押的真實利空，不是公司行動——絕對不能加進 `stock_corp_actions`。

### Step 2.5 — 用「比例乾淨度」排優先順序（省 token 用，不能單獨當判斷依據）

候選一多，不可能每筆都查新聞。可以先算 `adjust_factor` 離「乾淨比例」多近，優先處理
最接近的：

```python
clean_ratios = [1/2,1/3,1/4,1/5,1/6,1/7,1/8,1/9,1/10, 2,3,4,5,6,7,8,9,10]
clean_dev = min(abs(factor - c) / c for c in clean_ratios)   # 越接近0代表比例越乾淨
```

**已驗證的準確度（2026-07-22，抽測8筆 `gap_days>=5` 且比例乾淨的候選）**：
5筆明確查到新聞證實為真公司行動（富驛-KY 減資50%、華上 減資70%、愛普 面額變更1拆2、
新零售 減資彌補虧損、東訊 確認經常性減資但查無單次公告），3筆查無新聞佐證但並未被
反證（多半是2017年的舊事件，新聞索引不到，不代表不是真的）。**沒有出現「比例乾淨+
有停牌缺口，結果查出來是真實漲跌」的反例**——也就是說 `gap_days>=5` 且 `clean_dev`
很小（大致 <5%）時，可信度已經很高，可以把 Step 4 的新聞查證當成「加分確認」而非
「必要條件」，但**沒查到新聞前還是不要寫入 `stock_corp_actions`**，先按 Step 2.5
排序處理積壓的候選，查不到新聞的維持未確認狀態即可，不要用比例乾淨當唯一依據下判斷。

比例乾淨度只對「分割」「乾淨比率的減資」有效。減資比例其實可以是任意數字（國巨那筆
0.2619、虹光那筆3.2576都不特別乾淨），比例不乾淨不代表不是公司行動，只是没辦法用這
個方法優先篩選，仍要個別查證。

**第二輪驗證（2026-07-22，抽測12筆）加碼確認**：8/12 查到新聞證實，命中率跟第一輪
一致。這輪還發現一個很常見的特定型態：`adjust_factor ≈ 0.1` 或 `≈ 10`（比 0.11 或 1/9
還乾淨，非常接近整數10倍）幾乎都是**「面額變更10元→1元」**（1股分割10股，近年台股
非常流行的操作，跟減資無關，是分割）——這輪測到的華義、長華、長科、尚凡全部命中。
看到 `clean_dev` 對 10 或 0.1 特別小時，優先當「面額變更」查（關鍵字換成「面額變更」
「10元 1元」而不是「減資」），比較容易查到。

### Step 2.6 — 個股本身冷不冷門，決定 gap 訊號可不可信

`gap_days>=5` 對熱門股是強訊號（正常不會停牌，一停就是公司行動），但對本來就冷門、
交易稀疏的股票會失真——冷門股「上一筆交易」可能本來就是幾個月前，跟停牌換股完全
是两回事。判斷方式：

```python
coverage_ratio = 該股總交易筆數 / 全市場總交易日數   # 抓 stock_daily_data 的 COUNT(*)
```

`coverage_ratio < 0.6` 的股票，`gap_days` 訊號不可靠，優先權要往後排（不是不能是公司
行動，只是不能靠 gap 本身判斷，一定要查新聞）。這輪已經把這批分流到
`sync/corp_action_candidates_illiquid.csv`，之後排查完常態流動的候選再回頭處理。

### Step 3 — 官方 API 交叉比對（僅供輔助，別太依賴）

已實測過的兩支 TWSE API，各有明確限制，不要對它們期待過高：

- **除權除息計算結果表**：`https://www.twse.com.tw/rwd/zh/exRight/TWT49U?date=YYYYMMDD&response=json`
  只涵蓋現金股利/股票股利/現金增資，**不涵蓋減資**。查某天股票不在清單裡，
  不代表它沒有公司行動，只代表不是這幾種類型。
- **減資恢復買賣參考價**：`https://www.twse.com.tw/rwd/zh/reducation/TWTAUU?date=YYYYMMDD&response=json`
  有回傳減資前收盤、復牌參考價，**但不支援指定歷史日期查詢**——不管傳什麼 `date`，
  都只回傳「最新一期」的資料。只有在事件剛發生（近幾天內）時才用得上，
  查歷史事件基本上會查到不相關的最新一筆。

這兩支都查不到的情況（尤其歷史事件）非常常見，是正常現象，直接進 Step 4。

### Step 4 — 用 WebSearch 查真實新聞（決定性判斷依據）

用 Step 1 查到的中文名稱 + 代號 + 關鍵字組合去搜，關鍵字視情況換：
`"{股票名稱} {代號} 減資 恢復交易"`、`"{股票名稱} {代號} 股票分割 一拆X"`、
`"{股票名稱} {代號} 合併 股份轉換"`、或什麼都不加只搜 `"{股票名稱} {代號} 股價 重大訊息 {年月}"`
讓搜尋結果自己告訴你發生了什麼事。

判斷標準：

- 新聞明確提到「減資」「股票分割」「一拆X」「合併」「股份轉換」等字眼，且時間點、
  比例對得上 → **確認為公司行動**，記下新聞來源，進 Step 5
- 新聞提到的是業績、併購傳聞、高管異動、訴訟、財報等基本面消息 → **確認為真實市場
  漲跌**，不要動它，只需要在自己的紀錄／回報裡註明原因即可
- 查不到任何新聞（新股、極冷門股常見）→ **維持未確認狀態，不要用猜的加進資料表**。
  寧可先跳過，等之後有更多資訊再處理，也不要在沒有證據時假設它是公司行動。

### Step 5 — 確認為真公司行動後，寫入 `stock_corp_actions`

調整係數用「事件前後**實際成交收盤價**」計算，不要用官方理論參考價——因為復牌當天
市場不一定剛好收在參考價（虹光那筆參考價23.86，實際收在跌停21.50，兩者若混用會在
邊界多製造出一段假跳空）：

```python
adjust_factor = 復牌後收盤 / 停牌前最後收盤

db.execute("""
    INSERT INTO stock_corp_actions (stock_code, ex_date, adjust_factor, action_type, note)
    VALUES (:code, :ex, :factor, :typ, :note)
    ON DUPLICATE KEY UPDATE adjust_factor = VALUES(adjust_factor), note = VALUES(note)
""", {
    "code": "xxxx.TW",
    "ex": "YYYY-MM-DD",            # 復牌/生效當天，調整套用於此日之前的資料
    "factor": adjust_factor,
    "typ": "減資",                  # 減資 / 分割 / 合併，僅供備註
    "note": "減資前收盤X(日期) -> 復牌收盤Y(日期)，新聞來源：...",
})
```

`ex_date` 一定填「復牌/生效當天」（也就是斷層之後那一天），`_apply_corp_actions()`
是把 `ex_date` **之前**的資料乘上係數，方向不能填反。

### Step 6 — 驗證調整生效

```python
sys.path.insert(0, "sync")
import build_strategy as bs
bars = bs._load_bars("xxxx.TW")   # 不傳 corp_actions，會自動查表套用
# 確認事件前的收盤價已經被調整、事件後(=最新資料)的收盤價不受影響
```

## 系統性優先事項：先查大盤基準與熱門 ETF

`build_strategy.py` 用 `0050.TW` 當全市場 RS 強度與回測的基準（`BENCH_CODE`）。這檔
（或任何被廣泛當作參考基準/熱門追蹤的 ETF）如果有未登記的分割，影響的不是一檔股票，
是**全部股票**的 RS 計算與 3 年回測——優先權要高於一般個股候選。已確認 0050(1拆4,
2025-06-18)、0052(1拆7, 2025-11-26) 這兩檔基準/熱門 ETF。

**完整已確認清單請直接查 `SELECT * FROM stock_corp_actions`**，不在這裡逐筆重複列出
——已累積 52 筆且持續增加，維護兩份清單容易不同步、也浪費篇幅。處理新一批候選前，
先跑這個查詢確認有沒有漏掉任何一檔權重股/常用基準即可。

**精確度校準（多筆案例累積驗證）**：減資類事件，新聞公告的「理論股數比」換算出的
adjust_factor，跟我們用「實際成交收盤價」算出的 adjust_factor，通常會有 **5~10% 左右
的合理落差**（倫飛理論7.899 vs 實際7.12；大魯閣理論2.239 vs 實際2.06；錸德理論1.851
vs 實際1.95；陽明理論2.141 vs 實際2.163，只差1%；及成理論10.927 vs 實際10.83）——這是
因為復牌當天市場不一定剛好收在理論參考價，跟 Step 5 一開始就強調「用實際成交價、不要
用官方參考價」的原則一致。**看到 5~10% 內的落差是正常現象，不是算錯，不用因此懷疑
查到的新聞跟候選對不上。**

**落差超過 20~30% 時，寧可不寫入，需要更深入查證**——實測踩過兩個反例：華冠(8101)
2024-11 那筆新聞明確寫「減資80%」（理論係數5.0），但候選的實際成交係數只有2.71，
差了46%；如興(4414) 新聞寫「減資83.6%」（理論係數約6.1），實際係數只有3.55，差了
42%。這兩筆都選擇不寫入 `stock_corp_actions`——型態(減資)、公司、大致時間都對得上，
但比例差太多，很可能是：(a) 候選抓到的其實是另一個時間點的價格變動、不是這次減資
本身；(b) 該次減資的停牌期間還疊加了其他真實的市場波動；(c) 新聞裡的比例跟候選對應
的實際公告不是同一次事件。不確定就不要硬寫，比對不上的案例標記待查即可。

## 待處理清單

`sync/corp_action_candidates.csv`：全市場、單日變動 ≥30%、與前一個既有交易日之間有
≥5 天缺口、且**整體覆蓋率 >= 60%（非冷門股）**的候選清單。狀態（截至 2026-07-22，
第二輪處理後）：

- **52 筆已確認為真公司行動**並寫入 `stock_corp_actions`（完整清單請直接查表，
  這裡不再逐筆列出——已經太長）
- **剩餘約 200 筆尚未處理**，留在 `corp_action_candidates.csv`，下次處理前先照
  Step 2.5 的比例乾淨度排序一次，越乾淨優先處理；`clean_dev` 對 10/0.1 特別小的
  優先當「面額變更」查，對 2/0.5 特別小的兩種關鍵字都試（「面額變更」與「減資」
  都很常見）
- 另有 **60 筆冷門股候選**分流在 `sync/corp_action_candidates_illiquid.csv`，`gap_days`
  訊號對這批不可靠，處理順序排在常態流動候選之後，且每筆都要靠 Step 4 新聞查證，
  不能靠 Step 2.5/2.6 的比例或缺口捷徑
- 遇到「查到公司確實常辦理減資，但抓不到跟候選日期精確對應的那一次公告」（例如
  東訊、世紀、東森都屬於這種連續減資好幾次的公司），**寧可先不寫入，標記待查**，
  不要因為公司類型對了就湊合著把不確定的日期也寫進去——寫錯 `ex_date` 或
  `adjust_factor` 比不寫更糟，會讓那段期間的分析比不修正還失真。

之後要繼續排查，就是從 `corp_action_candidates.csv` 裡挑股票，跑一次 Step 1~6，
處理完的話從 CSV 裡刪掉那一列，避免重複查證。**七輪合計抽測約 95 筆，52 筆確認、
其餘查無新聞佐證/日期兜不上/比例落差過大而暫不寫入——目前 0 誤植反例**（沒有任何
一筆已經寫進 `stock_corp_actions` 的事後被證明是真實漲跌），heuristic 持續有效。
查無新聞的命中率大約落在 35~65% 之間浮動，2017年附近的舊事件明顯較難查到，屬正常
現象，不代表候選是假的。剩餘約 200 筆常態候選 + 60 筆冷門股待查，可以分批繼續，
每批建議 15~25 筆一次查完再回歸驗證，太大批容易漏看比例落差過大的反例。

每處理完一批都要做的回歸驗證（已建立固定檢查清單，往後每輪都照跑）：
1. `stock_corp_actions` 有沒有同一股票重複、`adjust_factor` 有沒有 <=0/>50/接近1 的異常值
2. 全部已確認股票逐一跑 `bs._load_bars(code, corp_actions)`，確認能正常載入、無錯誤
3. `corp_action_candidates.csv` 跟 `stock_corp_actions` 交叉比對，確認沒有殘留重複列

清單怎麼重新產生（例如想抓 2026-07-22 之後的新資料）：

```sql
SELECT stock_code, trade_date, close_price, prev_close, prev_date, pct_chg,
       DATEDIFF(trade_date, prev_date) AS gap_days,
       close_price / prev_close AS adjust_factor
FROM (
    SELECT stock_code, trade_date, close_price,
           LAG(close_price) OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_close,
           LAG(trade_date) OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_date,
           (close_price - LAG(close_price) OVER (PARTITION BY stock_code ORDER BY trade_date))
           / LAG(close_price) OVER (PARTITION BY stock_code ORDER BY trade_date) AS pct_chg
    FROM stock_daily_data
) t
WHERE ABS(pct_chg) >= 0.30
ORDER BY stock_code, trade_date
```

日常同步（`market_sync.py` 排程跑的時候）不需要跑這支大查詢——`_detect_price_anomalies()`
已經會在每天同步完當天資料後自動檢查，新的候選會直接出現在 ETL 的 log 警告裡。

## 絕對不要做的事

- 不要修改 `stock_daily_data` 原始資料——所有調整只透過 `stock_corp_actions` + 讀取時套用
- 不要在沒有停牌缺口、也查不到新聞證據時，只憑「跌幅很大」就認定是公司行動
- 不要用官方參考價取代實際成交收盤價去算調整係數
- 不要把 `ex_date` 填成停牌前最後一天，一定是復牌/生效當天
- 每加一筆，務必留下 `note`（新聞來源或 API 依據），方便之後回頭稽核判斷是否正確
