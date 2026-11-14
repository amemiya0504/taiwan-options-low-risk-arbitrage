# 臺指選擇權歷史行情資料

用 Python 3.11 以上版本與標準庫下載 TAIFEX TXO 每日行情 CSV，並存入 SQLite。無需安裝第三方套件。

```powershell
uv run --python 3.12 download_txo.py self-check
uv run --python 3.12 download_txo.py download --start 2026-11-02 --end 2026-11-03
uv run --python 3.12 download_txo.py check
```

## T2 資料品質檢查

安裝開發相依套件並執行 pytest 與整庫驗證：

```powershell
uv sync --group dev
uv run --python 3.12 pytest -q
uv run --python 3.12 quality_check.py --start 2025-11-14 --end 2026-11-13
```

`--start` 與 `--end` 必填，定義預期涵蓋期間，避免用資料庫首末日期隱藏區間邊界缺漏。`quality_check.py` 以唯讀模式開啟 SQLite，不改動行情資料；也會拒絕報告輸出路徑與資料庫或日曆檔相同。它依 `calendar/taifex_trading_calendar.json` 檢查交易日，驗證主鍵與異常列來源、契約月份和到期日、交易時段、履約價最低間距、買賣權別、OHLC、價格、成交量與未平倉量，並抽取區間首日、中位交易日、末日，以及第一個有量無 OHLC 的日期，重新下載 TAIFEX 官方日行情比對該日全部資料列。契約代碼與履約價規則參考 [TAIFEX 商品代碼](https://www.taifex.com.tw/cht/4/contractName1)及[臺指選擇權契約規格](https://www.taifex.com.tw/cht/2/tXO)；到期日欄位啟用日期參考[每日行情說明](https://www.taifex.com.tw/cht/3/optDailyMarketReport)。畸形日期與數值會列入異常報告，不中斷整次檢查。網路不可用時仍會產生報告，官方抽樣狀態標記為 `error`，整體狀態為 `incomplete`。

報告輸出至 `reports/data_quality.json` 與 `reports/data_quality.md`。JSON 對每筆異常保留 SQLite 主鍵及 `source_json` 原始列，重複主鍵與官方抽樣差異也包含逐筆證據。報告目錄已加入 `.gitignore`，避免上傳含行情原始欄位的 JSON；Markdown 摘要可單獨提交。履約價檢查採最小合法間距，完整中心／外圍履約價序列尚需前一日指數收盤價，現有 schema 未保存該欄位。外部市場日曆每年及臨時休市公告更新後，需人工更新日曆快照；快照目前涵蓋 `2025-11-14` 至 `2026-12-31`。

### 最近一次檢查結果

2026-11-15 執行：`passed_with_warnings`。資料庫 1,248,294 列、241 個預期交易日全部覆蓋，缺漏/非預期日期均為 0；schema 主鍵相符，重複主鍵為 0。TAIFEX 官方抽樣重抓 4 個日期共 19,102 列，所有主鍵與欄位均相符。

目前有 56 筆警示：來源列成交量大於 0，但 OHLC 欄位為空。JSON 報告逐筆保留原始列供審查，驗證器不推測成因，也不補值。另有 680 列 `202607F2` 與 756 列 `202610F4` 的原排定到期日早於行情日期；這兩組已依 TAIFEX 颱風順延公告登錄為已解釋例外。其他欄位的來源空值統計列在 JSON/Markdown 報告中，不一律當成資料錯誤。

GitHub 專案比較與授權紀錄見 [`docs/github_references.md`](docs/github_references.md)。驗證器只借鑑檢查設計概念，沒有複製來源程式碼或引入額外執行相依套件。

預設資料庫為 `data/txo.sqlite3`，原始 CSV 保存在 `data/raw/`。下載器每次按月份切小批次；重跑會更新相同契約的資料列，資料庫以日期、到期月份、到期日、履約價、買賣權和交易時段去重。已存在的原始 CSV 不會覆寫。

資料表 `txo_daily` 保存交易日期、商品、到期月份/週別、到期日、履約價、買賣權、交易時段、開高低收、成交量、結算價、未沖銷契約數、最後最佳買賣價及原始欄位。無成交的 OHLC 欄位可能為空，這是來源資料的表示方式，不代表下載缺漏。

此資料來源提供每日彙總行情，不含逐筆成交或委託簿。TAIFEX 每日下載頁限制查詢區間不超過一個月；年度壓縮檔沒有自動解壓與回填功能，需按日期範圍執行下載。

來源：[TAIFEX 選擇權每日交易行情下載](https://www.taifex.com.tw/cht/3/optDailyMarketView)。
