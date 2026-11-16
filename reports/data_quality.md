# TXO 行情資料品質報告

- 狀態：**passed_with_warnings**
- 產生時間：`2026-11-16T06:36:39.504434+00:00`
- 資料庫：`txo.sqlite3`
- 範圍：`2025-11-14` 至 `2026-11-13`
- 資料列：1,248,294；交易日：241/241，覆蓋率 100.0%
- 交易時段：482/482，覆蓋率 100.0%

## 交易日核對

官方日曆來源：https://www.taifex.com.tw/file/taifex/eng/eng4/2025Calendar_ev2.pdf, https://www.taifex.com.tw/file/taifex/eng/eng4/2026Calendar_en.pdf, https://www.taifex.com.tw/file/taifex/eng/eng11/TAIFEX_Holiday_Schedule_2027.pdf, https://www.taifex.com.tw/file/taifex/CHINESE/11/attach/%E5%8F%B0%E6%9C%9F%E4%BA%A4%E5%AD%97%E7%AC%AC1150003597%E8%99%9F%E5%87%BD.pdf, https://www.taifex.com.tw/enl/eng11/newsDetail?idx=10470&newsType=1, https://www.taifex.com.tw/enl/eng11/announcement

未缺少預期交易日。

區間內的平日休市日：
- `2025-12-25`：Constitution Day; 2025 TAIFEX calendar
- `2026-01-01`：New Year's Day; 2026 TAIFEX calendar
- `2026-02-12`：Lunar New Year closure; 2026 TAIFEX calendar
- `2026-02-13`：Lunar New Year closure; 2026 TAIFEX calendar
- `2026-02-16`：Lunar New Year; 2026 TAIFEX calendar
- `2026-02-17`：Lunar New Year; 2026 TAIFEX calendar
- `2026-02-18`：Lunar New Year; 2026 TAIFEX calendar
- `2026-02-19`：Lunar New Year; 2026 TAIFEX calendar
- `2026-02-20`：Lunar New Year make-up holiday; 2026 TAIFEX calendar
- `2026-02-27`：Peace Memorial Day make-up holiday; 2026 TAIFEX calendar
- `2026-04-03`：Children's Day and Tomb Sweeping Day make-up holiday; 2026 TAIFEX calendar
- `2026-04-06`：Children's Day and Tomb Sweeping Day make-up holiday; 2026 TAIFEX calendar
- `2026-05-01`：Labor Day; 2026 TAIFEX calendar
- `2026-06-19`：Dragon Boat Festival; 2026 TAIFEX calendar
- `2026-07-10`：Closed due to Typhoon Bavi; official notice dated 2026-07-09
- `2026-09-25`：Mid-Autumn Festival; 2026 TAIFEX calendar
- `2026-09-28`：Teachers' Day; 2026 TAIFEX calendar
- `2026-10-09`：National Day make-up holiday; 2026 TAIFEX calendar
- `2026-10-23`：Closed due to Typhoon Champi; official notice dated 2026-10-22
- `2026-10-26`：Taiwan Retrocession Day make-up holiday; 2026 TAIFEX calendar

## Schema、NULL 與資料檢查

- 主鍵符合預期：True；重複主鍵群組：0
- 契約規格來源：https://www.taifex.com.tw/cht/4/contractName1, https://www.taifex.com.tw/cht/2/tXO, https://www.taifex.com.tw/cht/3/optDailyMarketReport
- NULL 數：
  - `best_ask`：689,869
  - `best_bid`：434,384
  - `close`：857,715
  - `high`：857,715
  - `low`：857,715
  - `open`：857,715
  - `open_interest`：606,118
  - `settlement_price`：606,118

- 成交量最大值：66,867；未平倉量最大值：20,445

## TAIFEX 官方行情抽樣

- `2025-11-14`：passed，官方 3342 列 / DB 3342 列，鍵差異 0 + 0，欄位不符列 0
  - 官方 CSV SHA-256：`0949db4bed6f641e531cc276decaf3c6609258c64c64d0de716af2bf14593735`
- `2025-12-09`：passed，官方 3212 列 / DB 3212 列，鍵差異 0 + 0，欄位不符列 0
  - 官方 CSV SHA-256：`3ac211db5c28593fc74545edd02afbba1c3e72a4d041832934efd90d19a25d89`
- `2026-05-20`：passed，官方 5622 列 / DB 5622 列，鍵差異 0 + 0，欄位不符列 0
  - 官方 CSV SHA-256：`abe88d611bdd8ff41b64fbe6bb2036e4f514710ba0759e56bcbd0c0817e98d23`
- `2026-11-13`：passed，官方 6926 列 / DB 6926 列，鍵差異 0 + 0，欄位不符列 0
  - 官方 CSV SHA-256：`6a01b234366a8af8d0e46ff845d4182d2c1c7b8d4ec830936184d2cbfde616d2`

## 異常項目

- `volume_without_ohlc` (warning)：56

## GitHub 設計參考

以下只借鑑設計概念，沒有複製來源程式碼或新增相依套件。

| 專案 | 用途／授權 | 採用概念 | 限制 |
|---|---|---|---|
| [QuantConnect Lean](https://github.com/QuantConnect/Lean) | large algorithmic trading engine with market-data adapters；Apache-2.0 | reviewed data-source separation; no code copied | C#/.NET, broad engine scope, and far larger than this SQLite audit |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | Python crypto trading bot with OHLCV history handling and candle validation；GPL-3.0 | time-range validation and testable OHLCV quality checks informed the rule list; no code copied | crypto exchange data and GPL copyleft; not suitable as a dependency for this project |
| [Pandera](https://github.com/unionai-oss/pandera) | schema and column-level validation for dataframe-like datasets；MIT | named field/domain checks and traceable failures informed the report structure; no code copied | requires dataframe integrations; unnecessary dependency for streaming SQLite rows |
| [tw_option_collector](https://github.com/psliurt/tw_option_collector) | small Taiwan futures/options website collector；No license file found; not counted as open source | confirmed local-market relevance only; no code copied | no documented license, tests, or data-quality workflow |

完整異常列與原始 TAIFEX 欄位見同目錄 JSON 報告。
