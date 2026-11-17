"""Read-only TAIFEX TXO daily-data quality audit."""

from __future__ import annotations

import argparse
import calendar as pycal
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path

import download_txo
import txo_engine


PK = ["trade_date", "contract_month", "contract_expiry", "strike", "option_type", "trading_session"]
PRICE_FIELDS = ["open", "high", "low", "close", "settlement_price", "best_bid", "best_ask"]
REQUIRED = ["trade_date", "symbol", "contract_month", "strike", "option_type", "trading_session", "volume", "source_json"]
ROW_FIELDS = [
    "trade_date", "symbol", "contract_month", "contract_expiry", "strike", "option_type",
    "trading_session", "open", "high", "low", "close", "volume", "settlement_price",
    "open_interest", "best_bid", "best_ask", "source_json",
]
MONTH_RE = re.compile(r"^(\d{4})(0[1-9]|1[0-2])(?:([WF])([1-5]))?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXPECTED_SESSIONS = {"一般", "盤後"}
EXPIRY_FIELD_INTRODUCED = dt.date(2025, 12, 8)
GITHUB_REFERENCES = [
    {"name": "QuantConnect Lean", "url": "https://github.com/QuantConnect/Lean", "license": "Apache-2.0", "purpose": "large algorithmic trading engine with market-data adapters", "used": "reviewed data-source separation; no code copied", "limit": "C#/.NET, broad engine scope, and far larger than this SQLite audit"},
    {"name": "Freqtrade", "url": "https://github.com/freqtrade/freqtrade", "license": "GPL-3.0", "purpose": "Python crypto trading bot with OHLCV history handling and candle validation", "used": "time-range validation and testable OHLCV quality checks informed the rule list; no code copied", "limit": "crypto exchange data and GPL copyleft; not suitable as a dependency for this project"},
    {"name": "Pandera", "url": "https://github.com/unionai-oss/pandera", "license": "MIT", "purpose": "schema and column-level validation for dataframe-like datasets", "used": "named field/domain checks and traceable failures informed the report structure; no code copied", "limit": "requires dataframe integrations; unnecessary dependency for streaming SQLite rows"},
    {"name": "tw_option_collector", "url": "https://github.com/psliurt/tw_option_collector", "license": "No license file found; not counted as open source", "purpose": "small Taiwan futures/options website collector", "used": "confirmed local-market relevance only; no code copied", "limit": "no documented license, tests, or data-quality workflow"},
]


def readonly_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def parse_date(value) -> dt.date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError("日期必須使用 YYYY-MM-DD 格式")
    return dt.date.fromisoformat(value)


def record_key(row: dict) -> dict:
    return {name: row[name] for name in PK}


def source_record(row: dict):
    if not isinstance(row["source_json"], str):
        return row["source_json"]
    try:
        return json.loads(row["source_json"])
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return row["source_json"]


def finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def contract_expiry_dates(contract_month: str, closed_weekdays: set[dt.date]) -> tuple[dt.date | None, dt.date | None, str | None]:
    if not isinstance(contract_month, str):
        return None, None, "格式不符"
    match = MONTH_RE.fullmatch(contract_month or "")
    if not match:
        return None, None, "格式不符"
    try:
        year, month = int(match[1]), int(match[2])
        suffix, ordinal_text = match[3], match[4]
        weekday = 2 if suffix == "W" else 4 if suffix == "F" else 2
        ordinal = int(ordinal_text) if ordinal_text else 3
        candidates = [
            dt.date(year, month, day)
            for day in range(1, pycal.monthrange(year, month)[1] + 1)
            if dt.date(year, month, day).weekday() == weekday
        ]
    except ValueError:
        return None, None, "日期無效"
    if ordinal > len(candidates):
        return None, None, "該月份沒有此週別"
    scheduled = candidates[ordinal - 1]
    monthly_expiry = candidates[2] if len(candidates) >= 3 else None
    if suffix == "W" and scheduled == monthly_expiry:
        return None, None, "星期三週契約與月契約到期日重疊"
    adjusted = scheduled
    while adjusted.weekday() >= 5 or adjusted in closed_weekdays:
        adjusted += dt.timedelta(days=1)
    return scheduled, adjusted, None


def ensure_safe_report_paths(db_path: Path, calendar_path: Path, out_dir: Path) -> None:
    inputs = [db_path, calendar_path]
    for base in (db_path, db_path.resolve()):
        inputs.extend(Path(f"{base}{suffix}") for suffix in ("-wal", "-shm", "-journal"))
    protected = [(path, path.resolve()) for path in inputs]
    for output_path in (out_dir / name for name in ("data_quality.json", "data_quality.md")):
        output = output_path.resolve()
        for source_path, source in protected:
            same_file = output == source
            if not same_file and output_path.exists() and source_path.exists():
                same_file = output_path.samefile(source_path)
            if same_file:
                raise ValueError(f"報告輸出路徑不可覆寫輸入檔或 SQLite sidecar：{source_path}")


def _write_report_atomic(path: Path, content: str, db_path: Path, calendar_path: Path, out_dir: Path) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        ensure_safe_report_paths(db_path, calendar_path, out_dir)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_default(value):
    if isinstance(value, bytes):
        return {"sqlite_blob_hex": value.hex()}
    raise TypeError(f"無法序列化類型：{type(value).__name__}")


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite_float": repr(value)}
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def official_expected_days(start: dt.date, end: dt.date, calendar: dict) -> list[dt.date]:
    covered_start = parse_date(calendar["coverage_start"])
    covered_end = parse_date(calendar["coverage_end"])
    if start < covered_start or end > covered_end:
        raise ValueError(f"官方日曆快照涵蓋 {covered_start} 至 {covered_end}，不涵蓋要求期間 {start} 至 {end}")
    closed = {parse_date(item["date"]) for item in calendar["closed_weekdays"]}
    days = []
    date = start
    while date <= end:
        if date.weekday() < 5 and date not in closed:
            days.append(date)
        date += dt.timedelta(days=1)
    return days


def audit_database(db_path: Path, calendar_path: Path, start: dt.date, end: dt.date) -> dict:
    calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "database": str(db_path.resolve()),
        "calendar": {
            "file": str(calendar_path.resolve()), "sources": calendar["sources"],
            "contract_rule_sources": calendar.get("contract_rule_sources", []),
        },
        "github_references": GITHUB_REFERENCES,
        "summary": {}, "calendar_check": {}, "schema_check": {}, "null_counts": {}, "resolved_expiry_postponements": [],
        "distribution": {}, "issues": [], "official_samples": [], "status": "incomplete",
    }
    with closing(readonly_connect(db_path)) as db:
        db.execute("BEGIN")
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
            nulls[name] = db.execute(f"SELECT COUNT(*) FROM txo_daily WHERE {quote_identifier(name)} IS NULL").fetchone()[0]
        report["null_counts"] = nulls

        expected = official_expected_days(start, end, calendar)
        actual = set()
        for (value,) in db.execute("SELECT DISTINCT trade_date FROM txo_daily WHERE trade_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())):
            try:
                actual.add(parse_date(value))
            except (TypeError, ValueError):
                continue
        missing_days = sorted(set(expected) - actual)
        unexpected_days = sorted(actual - set(expected))
        closures = [item for item in calendar["closed_weekdays"] if start.isoformat() <= item["date"] <= end.isoformat()]
        closed_sessions = {
            (parse_date(item["date"]), item["session"]): item
            for item in calendar.get("session_closures", [])
            if start.isoformat() <= item["date"] <= end.isoformat()
        }
        expected_sessions = {
            (day, session)
            for day in expected
            for session in EXPECTED_SESSIONS
            if (day, session) not in closed_sessions
        }
        observed_sessions = set()
        for trade_date, session in db.execute(
            "SELECT DISTINCT trade_date, trading_session FROM txo_daily WHERE trade_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ):
            try:
                session_date = parse_date(trade_date)
            except (TypeError, ValueError):
                continue
            observed_sessions.add((session_date, session))
        missing_sessions = sorted(expected_sessions - observed_sessions)
        unexpected_session_keys = sorted(observed_sessions & set(closed_sessions))
        unexpected_session_records = []
        for day, session in unexpected_session_keys:
            rows = db.execute(
                "SELECT * FROM txo_daily WHERE trade_date=? AND trading_session=?", (day.isoformat(), session),
            )
            records = []
            for row in rows:
                row = dict(row)
                records.append({"record_key": record_key(row), "source_record": source_record(row)})
            unexpected_session_records.append({
                "date": day.isoformat(), "session": session,
                "reason": closed_sessions[(day, session)]["reason"], "records": records,
            })
        report["calendar_check"] = {
            "source": calendar["sources"], "expected_trading_dates": len(expected),
            "observed_trading_dates": len(actual),
            "coverage_percent": round(100 * len(set(expected) & actual) / len(expected), 4) if expected else 100.0,
            "missing_dates": [date.isoformat() for date in missing_days],
            "unexpected_dates": [date.isoformat() for date in unexpected_days],
            "weekday_closures": closures,
            "expected_session_records": len(expected_sessions),
            "observed_session_records": len(expected_sessions) - len(missing_sessions),
            "session_coverage_percent": round(100 * (len(expected_sessions) - len(missing_sessions)) / len(expected_sessions), 4) if expected_sessions else 100.0,
            "missing_sessions": [{"date": day.isoformat(), "session": session} for day, session in missing_sessions],
            "closed_sessions": [closed_sessions[key] for key in sorted(closed_sessions)],
            "unexpected_sessions": [
                {"date": item["date"], "session": item["session"], "reason": item["reason"], "record_count": len(item["records"])}
                for item in unexpected_session_records
            ],
        }

        duplicate_rows = db.execute(
            f"SELECT * FROM (SELECT *, COUNT(*) OVER (PARTITION BY {','.join(PK)}) AS duplicate_count FROM txo_daily) WHERE duplicate_count > 1"
        )
        duplicate_groups = {}
        for duplicate_row in duplicate_rows:
            row = dict(duplicate_row)
            key_tuple = tuple(row[name] for name in PK)
            duplicate_groups.setdefault(key_tuple, []).append(row)
        report["schema_check"]["duplicate_primary_key_groups"] = len(duplicate_groups)
        for key_tuple, rows in duplicate_groups.items():
            report["issues"].append({
                "rule": "duplicate_primary_key", "severity": "error",
                "record_key": dict(zip(PK, key_tuple)), "count": len(rows),
                "records": [
                    {"record_key": record_key(row),
                     "normalized_record": {name: row[name] for name in ROW_FIELDS if name != "source_json"},
                     "source_record": source_record(row)}
                    for row in rows
                ],
            })

        dist = {"max_volume": 0, "max_open_interest": 0, "positive_volume_rows": 0, "zero_volume_rows": 0}
        closed_weekdays = {parse_date(item["date"]) for item in calendar["closed_weekdays"]}
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

                for field in PK:
                    if row[field] is None:
                        issue("null_primary_key_field", "error", {field: None})

                try:
                    record_date = parse_date(row["trade_date"])
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
                if row["trading_session"] not in EXPECTED_SESSIONS:
                    issue("invalid_trading_session", "error", {"trading_session": row["trading_session"]})
                scheduled_expiry, adjusted_expiry, month_error = contract_expiry_dates(row["contract_month"], closed_weekdays)
                if month_error:
                    issue("invalid_contract_month", "error", {"contract_month": row["contract_month"]})
                expiry = row["contract_expiry"]
                expiry_was_supplied = expiry is not None and bool(str(expiry).strip())
                expiry_is_required = record_date > EXPIRY_FIELD_INTRODUCED or (
                    record_date == EXPIRY_FIELD_INTRODUCED and row["trading_session"] == "一般"
                )
                if not expiry_was_supplied and expiry_is_required:
                    issue("missing_contract_expiry", "error", {"contract_expiry": expiry, "effective_from": EXPIRY_FIELD_INTRODUCED.isoformat()})
                if expiry:
                    try:
                        expiry_date = parse_date(expiry)
                        allowed_expiries = {value for value in (scheduled_expiry, adjusted_expiry) if value is not None}
                        if allowed_expiries and expiry_date not in allowed_expiries:
                            issue("contract_expiry_mismatch", "error", {
                                "contract_month": row["contract_month"], "contract_expiry": expiry,
                                "expected_dates": sorted(value.isoformat() for value in allowed_expiries),
                            })
                        if expiry_date < record_date:
                            postponed = postponements.get((row["contract_month"], expiry))
                            if postponed and record_date <= parse_date(postponed["last_trading_date"]):
                                resolved_expiries[(row["contract_month"], expiry, postponed["last_trading_date"])] += 1
                            else:
                                issue("expired_contract_traded", "error", {"contract_expiry": expiry, "trade_date": row["trade_date"]})
                    except (TypeError, ValueError):
                        issue("invalid_contract_expiry", "error", {"contract_expiry": expiry})
                strike = row["strike"]
                if not finite_number(strike) or strike <= 0 or int(strike) != strike:
                    issue("invalid_strike", "error", {"strike": row["strike"]})
                elif strike % (25 if strike < 3000 else 50) != 0:
                    # shortcut: check the minimum legal strike grid; upgrade when prior-day index closes are added to the schema.
                    issue("invalid_strike_increment", "error", {"strike": strike, "minimum_grid": 25 if strike < 3000 else 50})
                valid_integer_fields = {}
                for field in ["volume", "open_interest"]:
                    value = row[field]
                    valid_integer_fields[field] = value is None or (finite_number(value) and value >= 0 and int(value) == value)
                    if not valid_integer_fields[field]:
                        issue("invalid_volume_or_open_interest", "error", {field: value})
                volume = row["volume"]
                valid_volume = valid_integer_fields["volume"] and volume is not None
                if valid_volume:
                    dist["max_volume"] = max(dist["max_volume"], volume)
                    if volume > 0:
                        dist["positive_volume_rows"] += 1
                    else:
                        dist["zero_volume_rows"] += 1
                open_interest = row["open_interest"]
                if valid_integer_fields["open_interest"] and open_interest is not None:
                    dist["max_open_interest"] = max(dist["max_open_interest"], open_interest)
                valid_prices = {}
                for field in PRICE_FIELDS:
                    value = row[field]
                    valid_prices[field] = value is None or (finite_number(value) and value >= 0)
                    if not valid_prices[field]:
                        issue("invalid_price", "error", {field: value})
                    elif value is not None and not txo_engine.option_price_on_tick(value):
                        issue("invalid_option_price_tick", "error", {field: value, "tick": txo_engine.option_tick(value)})
                o, h, l, c = (row[name] for name in ("open", "high", "low", "close"))
                if valid_prices["high"] and valid_prices["low"] and h is not None and l is not None and h < l:
                    issue("ohlc_high_below_low", "error", {"high": h, "low": l})
                if valid_prices["high"] and valid_prices["low"] and h is not None and l is not None:
                    outside = {name: row[name] for name in ("open", "close") if valid_prices[name] and row[name] is not None and not l <= row[name] <= h}
                    if outside:
                        issue("ohlc_outside_range", "error", {"low": l, "high": h, **outside})
                if valid_volume and volume > 0 and any(row[name] is None for name in ("open", "high", "low", "close")):
                    issue("volume_without_ohlc", "warning", {name: row[name] for name in ("open", "high", "low", "close", "volume")})
                    if source_anomaly_sample_date is None:
                        source_anomaly_sample_date = record_date
                if valid_prices["best_bid"] and valid_prices["best_ask"] and row["best_bid"] is not None and row["best_ask"] is not None and row["best_bid"] > row["best_ask"]:
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
        if missing_sessions:
            report["issues"].append({
                "rule": "missing_calendar_sessions", "severity": "error",
                "sessions": [{"date": day.isoformat(), "session": session} for day, session in missing_sessions],
            })
        if unexpected_session_records:
            report["issues"].append({
                "rule": "unexpected_calendar_sessions", "severity": "error",
                "sessions": unexpected_session_records,
            })
        report["issue_counts"] = [
            {"rule": rule, "severity": severity, "count": count}
            for (rule, severity), count in sorted(Counter((item["rule"], item["severity"]) for item in report["issues"]).items())
        ]

        samples = []
        chosen_dates = sorted(date_values)
        if chosen_dates:
            samples = sorted({chosen_dates[0], chosen_dates[len(chosen_dates) // 2], chosen_dates[-1], source_anomaly_sample_date} - {None})
        report["_sample_dates"] = [date.isoformat() for date in samples]
        report["_sample_local_rows"] = {
            date.isoformat(): [dict(row) for row in db.execute("SELECT * FROM txo_daily WHERE trade_date=?", (date.isoformat(),))]
            for date in samples
        }
    return report


def verify_official_samples(report: dict, db_path: Path) -> None:
    dates = report.pop("_sample_dates", [])
    local_rows_by_date = report.pop("_sample_local_rows", {})
    missing_cache = [date for date in dates if date not in local_rows_by_date]
    if missing_cache:
        with closing(readonly_connect(db_path)) as local_db:
            local_db.row_factory = sqlite3.Row
            local_db.execute("BEGIN")
            for date in missing_cache:
                local_rows_by_date[date] = [dict(row) for row in local_db.execute(
                    "SELECT * FROM txo_daily WHERE trade_date=?", (date,),
                )]

    for date_text in dates:
        result = {
            "date": date_text, "source": "TAIFEX official daily options download",
            "source_url": "https://www.taifex.com.tw/cht/3/optDailyMarketView",
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "comparison": "all downloaded rows and normalized database fields except source_json",
            "status": "error", "differences": [],
        }
        try:
            official_date = parse_date(date_text)
            official_data = download_txo.Taifex().csv(official_date, official_date)
        except Exception as exc:  # Network/service failures are distinct from invalid source data.
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["error_type"] = type(exc).__name__
            report["official_samples"].append(result)
            report["issues"].append({"rule": "official_sample_unavailable", "severity": "warning", "sample": result})
            continue
        result["official_csv_sha256"] = hashlib.sha256(official_data).hexdigest()

        local_rows = local_rows_by_date.get(date_text, [])
        local_by_key = {}
        for row in local_rows:
            local_by_key.setdefault(tuple(row[name] for name in PK), []).append(row)
        try:
            official_rows = download_txo.parse_csv(official_data)
        except Exception as exc:
            result.update({"status": "failed", "error_type": type(exc).__name__,
                           "error": f"{type(exc).__name__}: {exc}", "database_rows": len(local_rows)})
            if isinstance(exc, download_txo.DuplicateCSVPrimaryKey):
                evidence = exc.evidence()
                key = tuple(evidence["record_key"][name] for name in PK)
                result["validation_error"] = {"rule": "duplicate_official_primary_key", **evidence}
                result["duplicate_official_keys"] = [evidence["record_key"]]
                result["differences"].append({
                    "record_key": evidence["record_key"],
                    "official_records": [{"source_record": row} for row in evidence["source_records"]],
                    "database_records": [sample_evidence(row) for row in local_by_key.get(key, [])],
                })
            report["official_samples"].append(result)
            report["issues"].append({"rule": "official_sample_invalid", "severity": "error", "sample": result})
            continue

        official_by_key = {}
        for row in official_rows:
            official_by_key.setdefault(tuple(row[name] for name in PK), []).append(row)
        official_keys, local_keys = set(official_by_key), set(local_by_key)
        duplicate_official = sorted((key for key, rows in official_by_key.items() if len(rows) > 1), key=str)
        duplicate_database = sorted((key for key, rows in local_by_key.items() if len(rows) > 1), key=str)
        result.update({"status": "passed", "official_rows": len(official_rows), "database_rows": len(local_rows),
                       "official_only_keys": len(official_keys - local_keys), "database_only_keys": len(local_keys - official_keys),
                       "official_only_key_values": [dict(zip(PK, key)) for key in sorted(official_keys - local_keys, key=str)],
                       "database_only_key_values": [dict(zip(PK, key)) for key in sorted(local_keys - official_keys, key=str)],
                       "duplicate_official_keys": [dict(zip(PK, key)) for key in duplicate_official],
                       "duplicate_database_keys": [dict(zip(PK, key)) for key in duplicate_database],
                       "field_mismatch_rows": 0})
        fields = [name for name in ROW_FIELDS if name != "source_json"]
        for key in sorted(official_keys | local_keys, key=str):
            left_rows, right_rows = official_by_key.get(key, []), local_by_key.get(key, [])
            if len(left_rows) != 1 or len(right_rows) != 1:
                if len(left_rows) > 1 or len(right_rows) > 1 or left_rows or right_rows:
                    result["differences"].append({
                        "record_key": dict(zip(PK, key)),
                        "official_records": [sample_evidence(row) for row in left_rows],
                        "database_records": [sample_evidence(row) for row in right_rows],
                    })
                continue
            left, right = left_rows[0], right_rows[0]
            changed = {}
            for field in fields:
                a, b = left.get(field), right.get(field)
                if field in {"volume", "open_interest"}:
                    equal = a == b
                elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    equal = math.isclose(a, b, rel_tol=0, abs_tol=1e-9)
                else:
                    equal = a == b
                if not equal:
                    changed[field] = {"database": b, "official": a}
            if changed:
                result["field_mismatch_rows"] += 1
                result["differences"].append({"record_key": dict(zip(PK, key)), "fields": changed,
                                               "official_source_record": source_record(left), "database_source_record": source_record(right)})
        result["status"] = "passed" if not any(result[k] for k in (
            "official_only_keys", "database_only_keys", "field_mismatch_rows", "duplicate_official_keys", "duplicate_database_keys",
        )) and len(official_rows) == len(local_rows) else "failed"
        report["official_samples"].append(result)
        if result["status"] == "failed":
            report["issues"].append({"rule": "official_sample_mismatch", "severity": "error", "sample": result})


BLOCK_TRADE_URL = "https://www.taifex.com.tw/cht/3/dlBargainOrderView"
BLOCK_TRADE_HEADERS = (["組合式序號", "日期", "契約數", "交易時段"],
                       ["組合式序號", "子序號", "契約", "到期月份(週別)", "履約價格", "買/賣權", "成交價格", "成交數量"])


def parse_block_trades(data: bytes) -> Counter:
    """把官方議價申報成交 CSV 彙總成 (日期, 時段, 月份, 履約價, 買賣權) -> TXO 口數。"""
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(data.decode("big5")))]
    if [row[:len(h)] for row, h in zip(rows, BLOCK_TRADE_HEADERS)] != list(BLOCK_TRADE_HEADERS):
        raise ValueError("鉅額交易 CSV 表頭不符")
    headers, volumes = {}, Counter()
    for row in rows[2:]:
        if not any(row):
            continue
        if len(row) >= 4 and DATE_RE.match(row[1].replace("/", "-")):
            if row[3] not in EXPECTED_SESSIONS:
                raise ValueError(f"鉅額交易 CSV 交易時段不明：{row}")
            headers[row[0]] = (row[1].replace("/", "-"), row[3])
        elif len(row) >= 8 and row[0] in headers:
            if row[2] == "TXO":
                option_type = {"C": "買權", "P": "賣權"}[row[5]]
                volumes[(*headers[row[0]], row[3], float(row[4]), option_type)] += int(row[7])
        else:
            raise ValueError(f"鉅額交易 CSV 列格式不明：{row}")
    return volumes


def reconcile_block_trades(report: dict) -> None:
    """成交量有值但 OHLC 為 '-' 的列，若口數等於官方鉅額交易口數，降為 info 並保留官方證據。"""
    targets = [item for item in report["issues"] if item["rule"] == "volume_without_ohlc"]
    if not targets:
        return
    dates = sorted(item["record_key"]["trade_date"] for item in targets)
    result = {"source": "TAIFEX 鉅額交易議價申報成交下載", "source_url": BLOCK_TRADE_URL,
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "start": dates[0], "end": dates[-1],
              "csv_sha256": [], "candidates": len(targets), "matched": 0, "status": "error"}
    report["block_trade_reconciliation"] = result
    try:
        client = download_txo.Taifex()
        volumes = Counter()
        for start, end in download_txo.chunks(parse_date(dates[0]), parse_date(dates[-1])):
            data = client.block_trades_csv(start, end)
            result["csv_sha256"].append({"start": start.isoformat(), "end": end.isoformat(), "sha256": hashlib.sha256(data).hexdigest()})
            volumes.update(parse_block_trades(data))
    except Exception as exc:  # 取不到官方資料時保留原 warning。
        result["error"] = f"{type(exc).__name__}: {exc}"
        report["issues"].append({"rule": "block_trade_reconciliation_unavailable", "severity": "warning", "reconciliation": result})
        return
    for item in targets:
        key = item["record_key"]
        block_volume = volumes.get((key["trade_date"], key["trading_session"], key["contract_month"], key["strike"], key["option_type"]))
        item["official_block_trade_volume"] = block_volume
        if block_volume == item["fields"]["volume"]:
            item.update(rule="volume_from_block_trade", severity="info")
            result["matched"] += 1
    result["status"] = "passed" if result["matched"] == len(targets) else "partial"


def sample_evidence(row: dict) -> dict:
    return {
        "normalized_record": {name: row.get(name) for name in ROW_FIELDS if name != "source_json"},
        "source_record": source_record(row),
    }


def markdown(report: dict) -> str:
    summary = report["summary"]
    cal = report["calendar_check"]
    lines = [
        "# TXO 行情資料品質報告", "", f"- 狀態：**{report['status']}**",
        f"- 產生時間：`{report['generated_at']}`", f"- 資料庫：`{Path(report['database']).name}`",
        f"- 範圍：`{summary['audit_start']}` 至 `{summary['audit_end']}`",
        f"- 資料列：{summary['audited_rows']:,}；交易日：{cal['observed_trading_dates']}/{cal['expected_trading_dates']}，覆蓋率 {cal['coverage_percent']}%",
        f"- 交易時段：{cal['observed_session_records']}/{cal['expected_session_records']}，覆蓋率 {cal['session_coverage_percent']}%", "",
        "## 交易日核對", "", f"官方日曆來源：{', '.join(cal['source'])}", "",
    ]
    if cal["missing_dates"]:
        lines += ["缺少日期："] + [f"- `{date}`" for date in cal["missing_dates"]]
    else:
        lines.append("未缺少預期交易日。")
    if cal["missing_sessions"]:
        lines += ["缺少交易時段："] + [f"- `{item['date']}` `{item['session']}`" for item in cal["missing_sessions"]]
    if cal["unexpected_sessions"]:
        lines += ["官方停盤時段卻有資料："] + [
            f"- `{item['date']}` `{item['session']}`：{item['record_count']} 列；{item['reason']}"
            for item in cal["unexpected_sessions"]
        ]
    if cal["closed_sessions"]:
        lines += ["", "依官方例外不應有資料的時段："] + [f"- `{item['date']}` `{item['session']}`：{item['reason']}" for item in cal["closed_sessions"]]
    if cal["unexpected_dates"]:
        lines += ["資料出現在官方休市日："] + [f"- `{date}`" for date in cal["unexpected_dates"]]
    if cal["weekday_closures"]:
        lines += ["", "區間內的平日休市日："] + [f"- `{item['date']}`：{item['reason']}" for item in cal["weekday_closures"]]
    lines += ["", "## Schema、NULL 與資料檢查", "", f"- 主鍵符合預期：{report['schema_check']['matches']}；重複主鍵群組：{report['schema_check']['duplicate_primary_key_groups']}",
              f"- 契約規格來源：{', '.join(report['calendar'].get('contract_rule_sources', []))}", "- NULL 數："]
    lines += [f"  - `{name}`：{count:,}" for name, count in report["null_counts"].items() if count]
    if not any(report["null_counts"].values()):
        lines.append("  - 無")
    lines += ["", f"- 成交量最大值：{report['distribution']['max_volume']:,}；未平倉量最大值：{report['distribution']['max_open_interest']:,}", "", "## TAIFEX 官方行情抽樣", ""]
    if report["official_samples"]:
        for sample in report["official_samples"]:
            lines.append(f"- `{sample['date']}`：{sample['status']}，官方 {sample.get('official_rows', 'N/A')} 列 / DB {sample.get('database_rows', 'N/A')} 列，鍵差異 {sample.get('official_only_keys', 'N/A')} + {sample.get('database_only_keys', 'N/A')}，欄位不符列 {sample.get('field_mismatch_rows', 'N/A')}")
            if sample.get("official_csv_sha256"):
                lines.append(f"  - 官方 CSV SHA-256：`{sample['official_csv_sha256']}`")
            if sample.get("error"):
                lines.append(f"  - 錯誤類型：{sample.get('error_type', '未分類')}")
                if sample.get("validation_error", {}).get("rule"):
                    lines.append(f"  - 驗證規則：`{sample['validation_error']['rule']}`")
    else:
        lines.append("未執行")
    block = report.get("block_trade_reconciliation")
    if block:
        lines += ["", "## 鉅額交易核對", "",
                  f"- 成交量有值但無 OHLC 的列：{block['candidates']}；與 [TAIFEX 議價申報成交]({block['source_url']}) 口數完全相符：{block['matched']}；狀態：{block['status']}",
                  "- 這類成交不更新開高低收，相符列記為 `volume_from_block_trade` (info)，不影響品質狀態。"]
        lines += [f"  - `{item['start']}`～`{item['end']}` CSV SHA-256：`{item['sha256']}`" for item in block["csv_sha256"]]
        if block.get("error"):
            lines.append(f"- 下載失敗：{block['error']}")
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


def run(db_path: Path, calendar_path: Path, out_dir: Path, start: dt.date, end: dt.date) -> dict:
    ensure_safe_report_paths(db_path, calendar_path, out_dir)
    report = audit_database(db_path, calendar_path, start, end)
    verify_official_samples(report, db_path)
    reconcile_block_trades(report)
    report.pop("_sample_local_rows", None)
    report.pop("_sample_dates", None)
    report["issue_counts"] = [
        {"rule": rule, "severity": severity, "count": count}
        for (rule, severity), count in sorted(Counter((item["rule"], item["severity"]) for item in report["issues"]).items())
    ]
    errors = sum(item["severity"] == "error" for item in report["issues"])
    warnings = sum(item["severity"] == "warning" for item in report["issues"])
    sample_errors = any(item["status"] == "error" for item in report["official_samples"])
    report["status"] = "failed" if errors else "incomplete" if sample_errors else "passed_with_warnings" if warnings else "passed"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_report_atomic(
        out_dir / "data_quality.json",
        json.dumps(json_safe(report), ensure_ascii=False, indent=2, default=json_default, allow_nan=False) + "\n",
        db_path, calendar_path, out_dir,
    )
    _write_report_atomic(out_dir / "data_quality.md", markdown(report), db_path, calendar_path, out_dir)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="以唯讀方式驗證 TAIFEX TXO 日行情資料")
    parser.add_argument("--db", type=Path, default=Path("data/txo.sqlite3"))
    parser.add_argument("--calendar", type=Path, default=Path("calendar/taifex_trading_calendar.json"))
    parser.add_argument("--out-dir", type=Path, default=Path("reports"))
    parser.add_argument("--start", required=True, type=parse_date, help="預期資料涵蓋起日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=parse_date, help="預期資料涵蓋迄日 YYYY-MM-DD")
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
