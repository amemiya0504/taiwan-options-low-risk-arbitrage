"""Read-only TAIFEX TXO daily-data quality audit."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import download_txo


PK = ["trade_date", "contract_month", "contract_expiry", "strike", "option_type", "trading_session"]
PRICE_FIELDS = ["open", "high", "low", "close", "settlement_price", "best_bid", "best_ask"]
REQUIRED = ["trade_date", "symbol", "contract_month", "strike", "option_type", "trading_session", "volume", "source_json"]
ROW_FIELDS = [
    "trade_date", "symbol", "contract_month", "contract_expiry", "strike", "option_type",
    "trading_session", "open", "high", "low", "close", "volume", "settlement_price",
    "open_interest", "best_bid", "best_ask", "source_json",
]
MONTH_RE = re.compile(r"^(\d{4})(\d{2})(?:[WF]\d+)?$")
GITHUB_REFERENCES = [
    {"name": "QuantConnect Lean", "url": "https://github.com/QuantConnect/Lean", "license": "Apache-2.0", "purpose": "large algorithmic trading engine with market-data adapters", "used": "reviewed data-source separation; no code copied", "limit": "C#/.NET, broad engine scope, and far larger than this SQLite audit"},
    {"name": "Freqtrade", "url": "https://github.com/freqtrade/freqtrade", "license": "GPL-3.0", "purpose": "Python crypto trading bot with OHLCV history handling and candle validation", "used": "time-range validation and testable OHLCV quality checks informed the rule list; no code copied", "limit": "crypto exchange data and GPL copyleft; not suitable as a dependency for this project"},
    {"name": "Pandera", "url": "https://github.com/unionai-oss/pandera", "license": "MIT", "purpose": "schema and column-level validation for dataframe-like datasets", "used": "named field/domain checks and traceable failures informed the report structure; no code copied", "limit": "requires dataframe integrations; unnecessary dependency for streaming SQLite rows"},
    {"name": "tw_option_collector", "url": "https://github.com/psliurt/tw_option_collector", "license": "No license file found; not counted as open source", "purpose": "small Taiwan futures/options website collector", "used": "confirmed local-market relevance only; no code copied", "limit": "no documented license, tests, or data-quality workflow"},
]


def readonly_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def record_key(row: dict) -> dict:
    return {name: row[name] for name in PK}


def source_record(row: dict):
    try:
        return json.loads(row["source_json"])
    except (TypeError, json.JSONDecodeError):
        return row["source_json"]


def official_expected_days(start: dt.date, end: dt.date, calendar: dict) -> list[dt.date]:
    covered_start = dt.date.fromisoformat(calendar["coverage_start"])
    covered_end = dt.date.fromisoformat(calendar["coverage_end"])
    if start < covered_start or end > covered_end:
        raise ValueError(f"官方日曆快照涵蓋 {covered_start} 至 {covered_end}，不涵蓋要求期間 {start} 至 {end}")
    closed = {dt.date.fromisoformat(item["date"]) for item in calendar["closed_weekdays"]}
    days = []
    date = start
    while date <= end:
        if date.weekday() < 5 and date not in closed:
            days.append(date)
        date += dt.timedelta(days=1)
    return days


def audit_database(db_path: Path, calendar_path: Path, start: dt.date | None = None, end: dt.date | None = None) -> dict:
    calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "database": str(db_path.resolve()),
        "calendar": {"file": str(calendar_path.resolve()), "sources": calendar["sources"]},
        "github_references": GITHUB_REFERENCES,
        "summary": {}, "calendar_check": {}, "schema_check": {}, "null_counts": {}, "resolved_expiry_postponements": [],
        "distribution": {}, "issues": [], "official_samples": [], "status": "incomplete",
    }
    with readonly_connect(db_path) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='txo_daily'").fetchone()
        if not exists:
            raise ValueError("資料庫缺少 txo_daily 資料表")
        schema = db.execute("PRAGMA table_info(txo_daily)").fetchall()
        columns = {row[1] for row in schema}
        missing_columns = sorted(set(ROW_FIELDS) - columns)
        if missing_columns:
            raise ValueError(f"資料表缺少欄位：{missing_columns}")
        actual_pk = [row[1] for row in sorted((row for row in schema if row[5]), key=lambda row: row[5])]
        report["schema_check"] = {"expected_primary_key": PK, "actual_primary_key": actual_pk, "matches": actual_pk == PK}

        min_date, max_date, total = db.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM txo_daily").fetchone()
        if not total:
            raise ValueError("txo_daily 沒有資料")
        if start is None or end is None:
            valid_dates = []
            for (value,) in db.execute("SELECT DISTINCT trade_date FROM txo_daily WHERE trade_date IS NOT NULL"):
                try:
                    valid_dates.append(dt.date.fromisoformat(value))
                except (TypeError, ValueError):
                    continue
            if not valid_dates:
                raise ValueError("資料庫沒有可解析的交易日期")
            start = start or min(valid_dates)
            end = end or max(valid_dates)
        if start > end:
            raise ValueError("開始日期不可晚於結束日期")
        count_in_range = db.execute("SELECT COUNT(*) FROM txo_daily WHERE trade_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())).fetchone()[0]
        report["summary"] = {
            "database_rows": total, "audited_rows": count_in_range,
            "database_min_date": min_date, "database_max_date": max_date,
            "audit_start": start.isoformat(), "audit_end": end.isoformat(),
            "distinct_trading_dates": db.execute("SELECT COUNT(DISTINCT trade_date) FROM txo_daily WHERE trade_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())).fetchone()[0],
        }
        nulls = {}
        for name in sorted(columns):
            nulls[name] = db.execute(f"SELECT COUNT(*) FROM txo_daily WHERE {name} IS NULL").fetchone()[0]
        report["null_counts"] = nulls

        expected = official_expected_days(start, end, calendar)
        actual = {dt.date.fromisoformat(row[0]) for row in db.execute("SELECT DISTINCT trade_date FROM txo_daily WHERE trade_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))}
        missing_days = sorted(set(expected) - actual)
        unexpected_days = sorted(actual - set(expected))
        closures = [item for item in calendar["closed_weekdays"] if start.isoformat() <= item["date"] <= end.isoformat()]
        report["calendar_check"] = {
            "source": calendar["sources"], "expected_trading_dates": len(expected),
            "observed_trading_dates": len(actual),
            "coverage_percent": round(100 * len(set(expected) & actual) / len(expected), 4) if expected else 100.0,
            "missing_dates": [date.isoformat() for date in missing_days],
            "unexpected_dates": [date.isoformat() for date in unexpected_days],
            "weekday_closures": closures,
        }

        duplicate_count = db.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM txo_daily GROUP BY {','.join(PK)} HAVING COUNT(*)>1)").fetchone()[0]
        report["schema_check"]["duplicate_primary_key_groups"] = duplicate_count
        for row in db.execute(f"SELECT {','.join(PK)}, COUNT(*) AS count FROM txo_daily GROUP BY {','.join(PK)} HAVING COUNT(*)>1"):
            report["issues"].append({"rule": "duplicate_primary_key", "severity": "error", "record_key": {k: row[i] for i, k in enumerate(PK)}, "count": row[-1]})

        dist = {"max_volume": 0, "max_open_interest": 0, "positive_volume_rows": 0, "zero_volume_rows": 0}
        postponements = {(item["contract_month"], item["scheduled_expiry"]): item for item in calendar.get("expiry_postponements", [])}
        resolved_expiries = Counter()
        source_anomaly_sample_date = None
        expected_option_types = {"買權", "賣權"}
        date_values = set()
        cursor = db.execute("SELECT * FROM txo_daily")
        while batch := cursor.fetchmany(8000):
            for sqlite_row in batch:
                row = dict(sqlite_row)
                key = record_key(row)
                raw = source_record(row)

                def issue(rule, severity, fields):
                    report["issues"].append({"rule": rule, "severity": severity, "record_key": key, "fields": fields, "source_record": raw})

                try:
                    record_date = dt.date.fromisoformat(row["trade_date"])
                except (TypeError, ValueError):
                    issue("invalid_trade_date", "error", {"trade_date": row["trade_date"]})
                    continue
                if not start <= record_date <= end:
                    continue
                date_values.add(record_date)
                for field in REQUIRED:
                    value = row[field]
                    if value is None or (isinstance(value, str) and not value.strip()):
                        issue("required_null_or_blank", "error", {field: value})
                if record_date.weekday() >= 5:
                    issue("weekend_trade_date", "error", {"trade_date": row["trade_date"]})
                if not isinstance(row["source_json"], str) or not row["source_json"].strip() or not isinstance(raw, dict):
                    issue("invalid_source_record", "error", {"source_json": row["source_json"]})
                if row["symbol"] != "TXO":
                    issue("wrong_symbol", "error", {"symbol": row["symbol"]})
                if row["option_type"] not in expected_option_types:
                    issue("invalid_option_type", "error", {"option_type": row["option_type"]})
                match = MONTH_RE.fullmatch(row["contract_month"] or "")
                if not match or not 1 <= int(match[2]) <= 12:
                    issue("invalid_contract_month", "error", {"contract_month": row["contract_month"]})
                expiry = row["contract_expiry"]
                if expiry:
                    try:
                        expiry_date = dt.date.fromisoformat(expiry)
                        if expiry_date < record_date:
                            postponed = postponements.get((row["contract_month"], expiry))
                            if postponed and record_date <= dt.date.fromisoformat(postponed["last_trading_date"]):
                                resolved_expiries[(row["contract_month"], expiry, postponed["last_trading_date"])] += 1
                            else:
                                issue("expired_contract_traded", "error", {"contract_expiry": expiry, "trade_date": row["trade_date"]})
                    except (TypeError, ValueError):
                        issue("invalid_contract_expiry", "error", {"contract_expiry": expiry})
                if not isinstance(row["strike"], (int, float)) or not math.isfinite(row["strike"]) or row["strike"] <= 0:
                    issue("invalid_strike", "error", {"strike": row["strike"]})
                for field in ["volume", "open_interest"]:
                    value = row[field]
                    if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value):
                        issue("invalid_volume_or_open_interest", "error", {field: value})
                dist["max_volume"] = max(dist["max_volume"], row["volume"] or 0)
                if row["volume"]:
                    dist["positive_volume_rows"] += 1
                else:
                    dist["zero_volume_rows"] += 1
                if row["open_interest"] is not None:
                    dist["max_open_interest"] = max(dist["max_open_interest"], row["open_interest"])
                for field in PRICE_FIELDS:
                    value = row[field]
                    if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                        issue("invalid_price", "error", {field: value})
                o, h, l, c = (row[name] for name in ("open", "high", "low", "close"))
                if h is not None and l is not None and h < l:
                    issue("ohlc_high_below_low", "error", {"high": h, "low": l})
                if h is not None and l is not None:
                    outside = {name: row[name] for name in ("open", "close") if row[name] is not None and not l <= row[name] <= h}
                    if outside:
                        issue("ohlc_outside_range", "error", {"low": l, "high": h, **outside})
                if row["volume"] and any(row[name] is None for name in ("open", "high", "low", "close")):
                    issue("volume_without_ohlc", "warning", {name: row[name] for name in ("open", "high", "low", "close", "volume")})
                    if source_anomaly_sample_date is None:
                        source_anomaly_sample_date = record_date
                if row["best_bid"] is not None and row["best_ask"] is not None and row["best_bid"] > row["best_ask"]:
                    issue("crossed_best_quote", "warning", {"best_bid": row["best_bid"], "best_ask": row["best_ask"]})

        report["distribution"] = dist
        report["resolved_expiry_postponements"] = [
            {"contract_month": month, "scheduled_expiry": expiry, "official_last_trading_date": last_trade_date,
             "reason": postponements[(month, expiry)]["reason"], "database_rows": count}
            for (month, expiry, last_trade_date), count in sorted(resolved_expiries.items())
        ]
        if not report["schema_check"]["matches"]:
            report["issues"].append({"rule": "primary_key_schema_mismatch", "severity": "error", "record_key": None, "fields": report["schema_check"]})
        if missing_days:
            report["issues"].append({"rule": "missing_calendar_dates", "severity": "error", "dates": [day.isoformat() for day in missing_days]})
        if unexpected_days:
            report["issues"].append({"rule": "unexpected_calendar_dates", "severity": "error", "dates": [day.isoformat() for day in unexpected_days]})
        report["issue_counts"] = [
            {"rule": rule, "severity": severity, "count": count}
            for (rule, severity), count in sorted(Counter((item["rule"], item["severity"]) for item in report["issues"]).items())
        ]

        samples = []
        chosen_dates = sorted(date_values)
        if chosen_dates:
            samples = sorted({chosen_dates[0], chosen_dates[len(chosen_dates) // 2], chosen_dates[-1], source_anomaly_sample_date} - {None})
        report["_sample_dates"] = [date.isoformat() for date in samples]
    return report


def verify_official_samples(report: dict, db_path: Path) -> None:
    dates = report.pop("_sample_dates", [])
    conn = readonly_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for date_text in dates:
            result = {
                "date": date_text, "source": "TAIFEX official daily options download",
                "source_url": "https://www.taifex.com.tw/cht/3/optDailyMarketView",
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "comparison": "all downloaded rows and normalized database fields except source_json",
                "status": "error", "differences": [],
            }
            try:
                official_rows = download_txo.parse_csv(download_txo.Taifex().csv(dt.date.fromisoformat(date_text), dt.date.fromisoformat(date_text)))
                query = conn.execute("SELECT * FROM txo_daily WHERE trade_date=?", (date_text,))
                local_rows = [dict(row) for row in query.fetchall()]
                official_by_key = {tuple(row[name] for name in PK): row for row in official_rows}
                local_by_key = {tuple(row[name] for name in PK): row for row in local_rows}
                official_keys, local_keys = set(official_by_key), set(local_by_key)
                result.update({"status": "passed", "official_rows": len(official_rows), "database_rows": len(local_rows),
                               "official_only_keys": len(official_keys - local_keys), "database_only_keys": len(local_keys - official_keys),
                               "field_mismatch_rows": 0})
                fields = [name for name in ROW_FIELDS if name != "source_json"]
                for key in sorted(official_keys & local_keys, key=str):
                    left, right = official_by_key[key], local_by_key[key]
                    changed = {}
                    for field in fields:
                        a, b = left.get(field), right.get(field)
                        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                            equal = math.isclose(a, b, rel_tol=0, abs_tol=1e-9)
                        else:
                            equal = a == b
                        if not equal:
                            changed[field] = {"database": b, "official": a}
                    if changed:
                        result["field_mismatch_rows"] += 1
                        result["differences"].append({"record_key": dict(zip(PK, key)), "fields": changed, "database_source_record": source_record(right)})
                result["status"] = "passed" if not any(result[k] for k in ("official_only_keys", "database_only_keys", "field_mismatch_rows")) else "failed"
            except Exception as exc:  # Report external service failure while keeping the audit artifact.
                result["error"] = f"{type(exc).__name__}: {exc}"
            report["official_samples"].append(result)
            if result["status"] == "failed":
                report["issues"].append({"rule": "official_sample_mismatch", "severity": "error", "sample": result})
            elif result["status"] == "error":
                report["issues"].append({"rule": "official_sample_unavailable", "severity": "warning", "sample": result})
    finally:
        conn.close()


def markdown(report: dict) -> str:
    summary = report["summary"]
    cal = report["calendar_check"]
    lines = [
        "# TXO 行情資料品質報告", "", f"- 狀態：**{report['status']}**",
        f"- 產生時間：`{report['generated_at']}`", f"- 資料庫：`{report['database']}`",
        f"- 範圍：`{summary['audit_start']}` 至 `{summary['audit_end']}`",
        f"- 資料列：{summary['audited_rows']:,}；交易日：{cal['observed_trading_dates']}/{cal['expected_trading_dates']}，覆蓋率 {cal['coverage_percent']}%", "",
        "## 交易日核對", "", f"官方日曆來源：{', '.join(cal['source'])}", "",
    ]
    if cal["missing_dates"]:
        lines += ["缺少日期："] + [f"- `{date}`" for date in cal["missing_dates"]]
    else:
        lines.append("未缺少預期交易日。")
    if cal["unexpected_dates"]:
        lines += ["資料出現在官方休市日："] + [f"- `{date}`" for date in cal["unexpected_dates"]]
    if cal["weekday_closures"]:
        lines += ["", "區間內的平日休市日："] + [f"- `{item['date']}`：{item['reason']}" for item in cal["weekday_closures"]]
    lines += ["", "## Schema、NULL 與資料檢查", "", f"- 主鍵符合預期：{report['schema_check']['matches']}；重複主鍵群組：{report['schema_check']['duplicate_primary_key_groups']}", "- NULL 數："]
    lines += [f"  - `{name}`：{count:,}" for name, count in report["null_counts"].items() if count]
    if not any(report["null_counts"].values()):
        lines.append("  - 無")
    lines += ["", f"- 成交量最大值：{report['distribution']['max_volume']:,}；未平倉量最大值：{report['distribution']['max_open_interest']:,}", "", "## TAIFEX 官方行情抽樣", ""]
    if report["official_samples"]:
        for sample in report["official_samples"]:
            lines.append(f"- `{sample['date']}`：{sample['status']}，官方 {sample.get('official_rows', 'N/A')} 列 / DB {sample.get('database_rows', 'N/A')} 列，鍵差異 {sample.get('official_only_keys', 'N/A')} + {sample.get('database_only_keys', 'N/A')}，欄位不符列 {sample.get('field_mismatch_rows', 'N/A')}")
            if sample.get("error"):
                lines.append(f"  - 錯誤：{sample['error']}")
    else:
        lines.append("未執行")
    lines += ["", "## 異常項目", ""]
    if report["issue_counts"]:
        lines += [f"- `{item['rule']}` ({item['severity']})：{item['count']:,}" for item in report["issue_counts"]]
    else:
        lines.append("沒有列級資料異常。")
    lines += ["", "## GitHub 設計參考", "", "以下只借鑑設計概念，沒有複製來源程式碼或新增相依套件。", "",
              "| 專案 | 用途／授權 | 採用概念 | 限制 |", "|---|---|---|---|"]
    lines += [f"| [{item['name']}]({item['url']}) | {item['purpose']}；{item['license']} | {item['used']} | {item['limit']} |" for item in report["github_references"]]
    lines += ["", "完整異常列與原始 TAIFEX 欄位見同目錄 JSON 報告。", ""]
    return "\n".join(lines)


def run(db_path: Path, calendar_path: Path, out_dir: Path, start: dt.date | None = None, end: dt.date | None = None) -> dict:
    report = audit_database(db_path, calendar_path, start, end)
    verify_official_samples(report, db_path)
    errors = sum(item["severity"] == "error" for item in report["issues"])
    warnings = sum(item["severity"] == "warning" for item in report["issues"])
    sample_errors = any(item["status"] == "error" for item in report["official_samples"])
    report["status"] = "failed" if errors else "incomplete" if sample_errors else "passed_with_warnings" if warnings else "passed"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data_quality.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "data_quality.md").write_text(markdown(report), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="以唯讀方式驗證 TAIFEX TXO 日行情資料")
    parser.add_argument("--db", type=Path, default=Path("data/txo.sqlite3"))
    parser.add_argument("--calendar", type=Path, default=Path("calendar/taifex_trading_calendar.json"))
    parser.add_argument("--out-dir", type=Path, default=Path("reports"))
    parser.add_argument("--start", type=dt.date.fromisoformat)
    parser.add_argument("--end", type=dt.date.fromisoformat)
    args = parser.parse_args()
    try:
        report = run(args.db, args.calendar, args.out_dir, args.start, args.end)
        print(f"{report['status']}: {report['summary']['audited_rows']:,} rows, {report['calendar_check']['coverage_percent']}% calendar coverage")
        print(f"Reports: {args.out_dir / 'data_quality.json'}, {args.out_dir / 'data_quality.md'}")
        return 0 if report["status"].startswith("passed") else 1
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
