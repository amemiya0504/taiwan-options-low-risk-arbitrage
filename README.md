# 臺灣選擇權低風險套利

以 TAIFEX 規則為準的 TXO 歷史資料檢查、離線定價診斷與紙上帳本原型。程式不連線券商、不送出實際訂單；範例行情均為合成資料。

## 專案狀態

| WBS | 狀態 | 交付與限制 |
|---|---|---|
| T1 | 完成 | TAIFEX 日行情 CSV 下載及去重匯入 SQLite；拒絕分數／越界整數、範圍外日期、交易日空回應、相同檔名修訂原檔及重疊日期的既有主鍵內容衝突；原始 CSV 以暫存檔原子發布。 |
| T2 | 完成，有已追蹤警示 | 交易日與交易時段覆蓋、schema、重複主鍵、契約欄位、價格與數量檢查；抽樣重抓官方日行情。最新本機結果見下方。 |
| T3 | 完成草稿 | [TAIFEX 契約規則基準](docs/txo_contract_rules.md)，含生效日、費率與預告調整；待高階模型／市場專家簽核。 |
| T4 | 離線原型完成 | Black-76、IV 反解、價位 tick、成本、同步報價及 TAIFEX 交易日／時段／到期日檢查。 |
| T5 | 離線原型完成 | 買賣權平價限匹配到期、乘數 50 元的 MTX／等值期貨報價；成本含輸入的進場期交稅及到期準備，沒有期貨保證金及實際融資模型。 |
| T6 | 部分完成 | 同到期箱型價差檢查；日曆價差只列 IV 相對價值觀察，不標示無風險套利。 |
| T7 | 部分完成 | 垂直靜態界限及蝶式檢查；尚無完整波動率曲面凸性／期限無套利校驗。 |
| T8 | 部分完成 | 有限風險選擇權籃子的原子 paper ledger 與績效統計；沒有 T1 歷史可成交回測、逐筆／五檔撮合、部分成交或成交機率模型。 |
| T9 | 未實作 | 專案未整合券商 SDK、模擬帳戶或訂單 API。富邦公開 SDK 有測試環境文件，但專案尚無沙盒登入與端對端驗收。 |
| T10 | 離線原型 | 以使用者輸入費率、結算指數壓力範圍與風險上限做紙上風控；沒有 TAIFEX 組合保證金或實際持倉監控。Kill switch 會阻止當日新單並取消待成交 paper 單，不會平倉。 |
| T11 | 離線測試完成 | pytest 涵蓋單元與合成流程；沒有券商端對端、實際行情延遲或高頻負載測試。 |

T8/T9 與券商端 T11 驗收需真實逐筆／五檔資料及明確的券商模擬環境；目前不可用合成測試替代。系統設計與可重現限制見 [docs/system_design.md](docs/system_design.md)。

## 安裝與測試

Python 3.11 以上版本；下載與品質檢查使用標準庫，pytest 僅供開發測試。

```powershell
uv sync --group dev
uv run --python 3.12 pytest -q
```

最近一次完整測試：**149 passed**，2026-11-16。Python 3.12 使用 `uv run --python 3.12 pytest -q`；Python 3.11 使用隔離命令 `uv run --no-project --python 3.11 --with pytest pytest -q`。

## T1 下載行情

```powershell
uv run --python 3.12 download_txo.py self-check
uv run --python 3.12 download_txo.py download --start 2026-11-02 --end 2026-11-03
uv run --python 3.12 download_txo.py check
```

預設資料庫為 `data/txo.sqlite3`，原始 CSV 位於 `data/raw/`。這些本機市場資料受 `.gitignore` 排除，不應提交到 GitHub。

下載器會保留每次成功保存的原始 CSV。來源列出現重複主鍵會在匯入前拒絕，CLI 錯誤會附上兩筆原始列；若同一日期區間的原始檔已存在但內容不同，或重疊日期的 SQLite 主鍵已有不同行情，也會拒絕匯入。重複匯入完全相同的行情具冪等性；批次遇到衝突會整批 rollback，不會倒回較新的修訂。需要匯入修訂資料時，先將舊原始 CSV 移到具版本識別的封存位置，再依明確流程重建資料。成交量及未沖銷量以精確整數解析，並限制在 SQLite signed 64-bit 範圍內。

## T2 歷史資料品質檢查

```powershell
uv run --python 3.12 quality_check.py --start 2025-11-14 --end 2026-11-13
```

`--start` 與 `--end` 必填且使用 `YYYY-MM-DD`。檢查器以唯讀模式開啟同一個 SQLite snapshot，依 `calendar/taifex_trading_calendar.json` 核對交易日與一般／盤後時段，包含缺漏時段及官方停盤時段卻有資料的檢查，並檢查主鍵、重複資料、NULL、日期、契約月份／到期日、履約價、買賣權、OHLC、TAIFEX 權利金 tick、成交量與未平倉量。抽樣日期會重新下載 TAIFEX 官方行情並比對全部欄位。異常列保存 SQLite 主鍵及原始來源列；官方來源 CSV 的重複主鍵會列出兩筆原始列。網路不可用時，報告會標記為 `incomplete`，不會把官方核對算成通過。

報告輸出至 `reports/data_quality.json` 及 `reports/data_quality.md`。JSON 使用標準 JSON；SQLite REAL 非有限值會以可追溯的 `{"non_finite_float": "inf"}` 標記。JSON 保存完整異常列、原始來源列與官方 CSV SHA-256，留在本機；受 Git 追蹤的 Markdown 僅包含摘要及抽樣證據。`data/` 與完整 JSON 報告不提交到 GitHub。

### 最近一次 T2 結果

2026-11-16 執行：`passed_with_warnings`。資料庫 **1,248,294** 列，預期交易日 **241/241**、預期交易時段 **482/482**，兩者覆蓋率皆為 **100%**；缺漏／非預期日期 **0**、缺漏／非預期時段 **0**、重複主鍵 **0**。TAIFEX 官方抽樣重抓 **4** 個日期共 **19,102** 列，主鍵與所有標準化欄位全數相符：2025-11-14（3,342 列）、2025-12-09（3,212 列）、2026-05-20（5,622 列）、2026-11-13（6,926 列）。報告記錄每份官方 CSV 的 SHA-256；執行前後 SQLite 與 14 份原始 CSV 的 SHA-256 雜湊一致。

已追蹤 **56** 筆警示：來源列成交量大於 0，但 OHLC 為空。另有 680 列 `202607F2` 及 756 列 `202610F4` 屬颱風順延後仍有交易的契約列，已按官方公告列作已解釋例外。驗證器不補值、不推定 56 筆成因。履約價採最小合法間距檢查；完整中心／外圍序列需要 schema 未保存的前日指數收盤價。完整 JSON／Markdown 報告位於本機 `reports/data_quality.json` 與 `reports/data_quality.md`，不提交含列級來源證據的本機報告或行情資料。

## T3 官方規格與開源參考

- [TXO 規則、費率、部位限制及查閱日期](docs/txo_contract_rules.md)
- [開源專案用途、授權、借鑑與限制](docs/github_references.md)

TAIFEX 公告、規章、日曆及當期費率優先於本專案快照。A/B/C 保證金、部位上限、券商費率與實際到期日會變動，必須在使用前重查。

## T4–T7 離線報價診斷

```powershell
uv run --python 3.12 txo_engine.py readiness --db data/txo.sqlite3 --output reports/system_readiness.json
uv run --python 3.12 txo_engine.py analyze --snapshot examples/snapshot.example.json --output reports/example_analysis.json
```

`readiness` 唯讀說明 T1 schema 是否含有事件時間、報價口數及配對期貨欄位。`analyze` 輸入需帶交易所時間、接收時間、雙邊買賣價及正口數、報價序號、session、到期結算識別、利率及有效成本；報價最長 5 秒、時間差最多 1 秒、成本資料最長 30 日。TAIFEX 來源若只有單邊委託或缺邊，這版會拒絕整份 snapshot；方向性深度支援留待正式 feed adapter。`examples/snapshot.example.json` 是**合成**輸入，不代表真實市場價格。候選訊號只供研究，沒有成交機率、保證金或實盤執行估計。

分析器依已提交的 TAIFEX 日曆檢查一般／盤後時段、休市日、週三／週五到期排程及已知順延日；目前日曆涵蓋 2025-11-14 至 2027-12-31，納入 TAIFEX 2027 公告及春節前夜盤歸屬規則。日曆需隨官方公告更新；這只能驗證日期規則，不能證明個別到期契約實際已掛牌。分析拒絕時仍會寫出 JSON 報告，CLI 回傳非零結束碼。

已實作 Black-76 與 IV 反解、買賣權平價、箱型價差、垂直靜態界限、蝶式檢查及跨期 IV 觀察清單。報價履約價需符合 TAIFEX 最小合法間距；套利現金流模型要求借款與投資利率皆為非負，且借款利率不得低於投資利率。Black-76 定價利率仍可為負。期貨對沖報價乘數固定為 50 元，代表 MTX 或相同乘數的契約，不代表乘數 200 元的 TX。成本快照要求期貨進場稅率及到期交割費／期交稅準備；進場稅依期貨成交價與乘數逐口四捨五入。TAIFEX 費率由使用者提供並須每 30 日更新。

## T8–T10 紙上帳本與風控示範

```powershell
uv run --python 3.12 examples/paper_demo.py
```

範例只用合成資料，輸出委託、成交、結算及 audit log，不連網也不呼叫券商。`MockBroker` 只接受同到期的箱型、垂直價差與蝶式選擇權籃子；按 ask／bid 原子成交，報價深度預占、撤單釋放、成交消耗。相同交易所報價事件的價格／口數／正規化 session 不可改寫，接收時間、同義 session、來源名稱或序號不會增加深度；單調模擬事件推進時會逾時或到期並釋放 pending 預占。結算價按 `settlement_id` 唯一，相同已登記結算事件可結清同到期多筆部位；實際交割費及履約稅只按價內履約口數計算。

更新的同契約報價會使舊報價委託失效，避免用已被取代的價格成交；送單與成交時間都必須在報價所屬 TAIFEX 交易時段，時鐘跨過收盤時會一併釋放該時段所有 pending 委託。委託輸入在修改額度前先完成複製，複製失敗不會留下資金或深度預占。結算超出進場壓力範圍仍會依輸入結算值照實結清，並在 settlement 與 audit log 標記壓力範圍違約，避免已成交部位永久鎖定；結算來源仍未經驗證。

風險金額是使用者設定結算指數範圍內的保守壓力值，不保證涵蓋該範圍外的市場風險，也不等同 TAIFEX／期貨商保證金。結算事件可在模擬時鐘較晚時補入，但 `settlement_timestamp` 必須落在契約到期日；損益按結算事件日期歸檔，模擬時鐘不回退。未記錄結算回報接收時間。`source_id` 是輸入標籤，未經簽章驗證。日損觸發的 kill switch 鎖定該模擬日並只取消當日 pending；直接呼叫 `trip_kill_switch()` 會鎖定整個 `MockBroker` 執行個體並取消所有 pending。Kill switch 不會自動平倉。

## 已知未完成項目

- T1 是日彙總資料，缺逐筆事件時間、五檔深度、匹配到期 MTX 報價及成交序號，因此不能做可成交的歷史回測或部分成交重播。富邦官方 v2.5.0 提供 TXO 歷史日行情與選擇權歷史 K 線；K 線資料是 OHLCV／成交資訊，沒有歷史 bid/ask 與委託簿深度，據此仍不能驗證多腿可成交性。[歷史日行情](https://www.fbs.com.tw/TradeAPI/docs/market-data-future/http-api/historical/daily/)、[歷史 K 線](https://www.fbs.com.tw/TradeAPI/docs/market-data-future/http-api/historical/candles/)
- 專案尚未整合富邦 SDK 或沙盒帳戶；券商 API 登入、帳務與測試環境未做端對端驗收。程式沒有任何實盤下單路徑。
- 保證金抵減、未平倉市值、盤中風險監控、告警通道、完整 IV 曲面檢查與硬體負載門檻尚未實作。
- T3 規則文件依 2026-11-15 公開頁面整理，並註明 2026-12-17 部位限制調整；文件仍需高階模型／市場專家簽核。

來源：[TAIFEX 選擇權每日交易行情下載](https://www.taifex.com.tw/cht/3/optDailyMarketView)。
