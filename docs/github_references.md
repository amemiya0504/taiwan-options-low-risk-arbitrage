# GitHub 參考專案與授權

查閱日期：2026-11-14。GitHub 倉庫與授權檔是用途和授權狀態的主要證據；官方交易日與市場資料仍以 TAIFEX 為準。沒有複製這些專案的程式碼。

| 專案 | 用途與授權 | 可借鑑設計 | 限制與本專案決定 |
|---|---|---|---|
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | Python/C# 量化交易引擎；Apache-2.0。授權：[LICENSE](https://github.com/QuantConnect/Lean/blob/master/LICENSE)。 | 分離行情資料來源與策略/運算層；本專案維持資料驗證獨立於 T1 下載及資料表。 | C#/.NET 與完整交易引擎規模遠大於只讀 SQLite 驗證；不引入 Lean 或其依賴。 |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | Python 加密貨幣交易機器人，含 OHLCV 歷史資料檢查；GPL-3.0。授權：[LICENSE](https://github.com/freqtrade/freqtrade/blob/develop/LICENSE)。資料時間範圍驗證可見 [`IDataHandler`](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/data/history/datahandlers/idatahandler.py)。 | 時間範圍、OHLCV 條件與測試夾具作為驗證項目參考。 | 市場/資料格式為加密貨幣；GPL-3.0 與大量交易機器人相依不適合當本專案相依套件；只借鑑概念，不複製程式碼。 |
| [unionai-oss/pandera](https://github.com/unionai-oss/pandera) | DataFrame 欄位與資料集驗證框架；MIT。授權狀態由 [GitHub repository metadata](https://api.github.com/repos/unionai-oss/pandera) 列為 MIT；其 README 展示 `DataFrameSchema` 與 `Check`。 | 依欄位列出型別/範圍規則，並將失敗項目分別命名、計數及回報。 | 需要 pandas/其他 DataFrame adapter；本案逐列串流 SQLite，標準庫即可完成，故不引入套件。 |

## 候選但未列入開源專案計數

[psliurt/tw_option_collector](https://github.com/psliurt/tw_option_collector) 是臺灣期貨/選擇權網站資料收集器，與 TXO 最接近；檢查倉庫時沒有找到授權檔，因此不能視為已授權的開源程式，也未借用程式碼。它的 README 只描述網站資料收集用途，沒有資料品質驗證或測試說明。

## 設計套用對照

- Freqtrade 的歷史 OHLCV 驗證概念對應 `quality_check.py` 的日期、價格與 OHLC 檢查。
- Pandera 的欄位 schema/check 概念對應命名規則、異常計數、逐列原始紀錄追溯與 JSON 報告。
- Lean 的資料來源分層概念對應 T1 下載器、T2 唯讀驗證器分開；沒有加入交易引擎。
- 以上均為概念參考；本專案未引入上述套件、程式碼、安裝指令或自動執行腳本。
