import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import quality_check
import download_txo
import pytest


def make_db(path, rows, nullable_expiry=False):
    db = sqlite3.connect(path)
    expiry_constraint = "TEXT DEFAULT ''" if nullable_expiry else "TEXT NOT NULL DEFAULT ''"
    db.execute("""CREATE TABLE txo_daily (
        trade_date TEXT NOT NULL, symbol TEXT NOT NULL, contract_month TEXT NOT NULL,
        contract_expiry {expiry_constraint}, strike REAL NOT NULL, option_type TEXT NOT NULL,
        trading_session TEXT NOT NULL DEFAULT '', open REAL, high REAL, low REAL, close REAL,
        volume INTEGER NOT NULL, settlement_price REAL, open_interest INTEGER,
        best_bid REAL, best_ask REAL, source_json TEXT NOT NULL,
        PRIMARY KEY (trade_date, contract_month, contract_expiry, strike, option_type, trading_session))""".format(expiry_constraint=expiry_constraint))
    db.executemany("INSERT INTO txo_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()


def test_calendar_skips_weekends_and_official_closures(tmp_path):
    calendar = {
        "coverage_start": "2026-01-01", "coverage_end": "2026-01-05",
        "closed_weekdays": [{"date": "2026-01-01", "reason": "holiday"}],
        "sources": [],
    }
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(calendar), encoding="utf-8")
    actual = quality_check.official_expected_days(dt.date(2026, 1, 1), dt.date(2026, 1, 5), calendar)
    assert actual == [dt.date(2026, 1, 2), dt.date(2026, 1, 5)]
    assert quality_check.MONTH_RE.fullmatch("202607F2")
    assert quality_check.MONTH_RE.fullmatch("202607W5")
    closed = {dt.date(2026, 2, 27)}
    assert quality_check.contract_expiry_dates("202602F4", closed) == (
        dt.date(2026, 2, 27), dt.date(2026, 3, 2), None,
    )
    assert quality_check.contract_expiry_dates("202601W3", set())[2]


def test_audit_finds_bad_row_and_reports_source(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [(
        "2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般",
        11, 9, 10, 12, -1, 11, -3, 12, 10, '{"原始欄位":"可追溯"}',
    )])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    rules = {issue["rule"] for issue in report["issues"]}
    assert report["calendar_check"]["coverage_percent"] == 100.0
    assert {"ohlc_high_below_low", "ohlc_outside_range", "invalid_volume_or_open_interest", "crossed_best_quote"} <= rules
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_volume_or_open_interest")
    assert issue["source_record"] == {"原始欄位": "可追溯"}


def test_audit_quotes_unusual_extra_sqlite_column_name(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps({"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])
    with sqlite3.connect(db_path) as db:
        db.execute('ALTER TABLE txo_daily ADD COLUMN "store run" TEXT')

    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))

    assert report["null_counts"]["store run"] == 1


def test_binary_source_json_is_reported_without_aborting_audit(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, b"\xff")])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_source_record")
    assert issue["source_record"] == b"\xff"


def test_run_writes_both_reports_without_network(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    out_dir = tmp_path / "reports"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "session_closures": [{"date": "2026-01-05", "session": "盤後", "reason": "test fixture"}], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])
    original_db = db_path.read_bytes()
    monkeypatch.setattr(quality_check, "verify_official_samples", lambda report, path: report.pop("_sample_dates"))
    report = quality_check.run(db_path, calendar_path, out_dir, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    assert report["status"] == "passed"
    assert db_path.read_bytes() == original_db
    assert (out_dir / "data_quality.json").is_file()
    assert (out_dir / "data_quality.md").is_file()


@pytest.mark.parametrize("target", ["database", "wal", "shm", "journal"])
def test_report_path_rejects_hardlinks_to_database_and_sidecars(tmp_path, target):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    calendar_path.write_text("{}", encoding="utf-8")
    make_db(db_path, [])
    protected = db_path if target == "database" else Path(f"{db_path}-{target}")
    if target != "database":
        protected.write_bytes(f"{target} bytes".encode())
    report_path = out_dir / "data_quality.json"
    try:
        os.link(protected, report_path)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    original = protected.read_bytes()

    with pytest.raises(ValueError, match="不可覆寫"):
        quality_check.ensure_safe_report_paths(db_path, calendar_path, out_dir)

    assert protected.read_bytes() == original
    assert report_path.read_bytes() == original


def test_run_atomically_replaces_unrelated_report_hardlink(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    calendar_path.write_text(json.dumps({
        "coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [],
        "session_closures": [{"date": "2026-01-05", "session": "盤後", "reason": "test fixture"}], "sources": [],
    }), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_bytes(b"must stay unchanged")
    report_path = out_dir / "data_quality.json"
    try:
        os.link(unrelated, report_path)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    monkeypatch.setattr(quality_check, "verify_official_samples", lambda report, path: report.pop("_sample_dates"))

    quality_check.run(db_path, calendar_path, out_dir, dt.date(2026, 1, 5), dt.date(2026, 1, 5))

    assert unrelated.read_bytes() == b"must stay unchanged"
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "passed"
    assert not os.path.samefile(report_path, unrelated)


def test_audit_reports_invalid_date_instead_of_crashing(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-07", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-06bad", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"raw":"bad date"}')])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 7))
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_trade_date")
    assert issue["source_record"] == {"raw": "bad date"}


def test_compact_database_date_is_rejected_as_noncanonical(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("20260105", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"raw":"compact date"}')])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_trade_date")
    assert issue["fields"]["trade_date"] == "20260105"


def test_missing_expiry_is_allowed_only_for_intro_night_session(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2025-12-08", "coverage_end": "2025-12-09", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    rows = [
        ("2025-12-08", "TXO", "202512", "", 25000, "買權", "盤後", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}"),
        ("2025-12-09", "TXO", "202512", "", 25000, "買權", "盤後", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}"),
    ]
    make_db(db_path, rows)
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2025, 12, 8), dt.date(2025, 12, 9))
    missing = [item["record_key"]["trade_date"] for item in report["issues"] if item["rule"] == "missing_contract_expiry"]
    assert missing == ["2025-12-09"]


def test_run_serializes_nonfinite_values_as_strict_json(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    out_dir = tmp_path / "reports"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", float("inf"), 2, 1, 2, 1, 2, 1, 1, 2, '{"raw":Infinity}')])
    monkeypatch.setattr(quality_check, "verify_official_samples", lambda report, path: report.pop("_sample_dates"))
    quality_check.run(db_path, calendar_path, out_dir, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    text = (out_dir / "data_quality.json").read_text(encoding="utf-8")
    report = json.loads(text, parse_constant=lambda value: pytest.fail(f"non-standard JSON constant: {value}"))
    invalid = next(item for item in report["issues"] if item["rule"] == "invalid_price")
    assert invalid["fields"]["open"] == {"non_finite_float": "inf"}
    assert invalid["source_record"]["raw"] == {"non_finite_float": "inf"}


def test_explicit_period_finds_missing_boundary_days(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-07", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-06", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 7))
    assert report["calendar_check"]["missing_dates"] == ["2026-01-05", "2026-01-07"]
    assert report["calendar_check"]["coverage_percent"] == pytest.approx(33.3333)


def test_bad_sqlite_numeric_type_is_reported_without_crashing(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, "bad", 2, 1, 1, 2, '{"raw":"bad volume"}')])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_volume_or_open_interest")
    assert issue["fields"] == {"volume": "bad"}
    assert report["distribution"]["max_volume"] == 0


def test_option_price_tick_violation_is_traceable(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 10.1, 11, 10, 10.5, 1, 10.5, 1, 10.1, 10.5, '{"raw":"bad tick"}')])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_option_price_tick")
    assert issue["fields"]["open"] == 10.1
    assert issue["source_record"] == {"raw": "bad tick"}


def test_contract_expiry_strike_and_session_rules(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601W1", "2026-06-17", 25001, "買權", "GARBAGE", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"raw":"bad contract"}')])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    rules = {issue["rule"] for issue in report["issues"]}
    assert {"contract_expiry_mismatch", "invalid_strike_increment", "invalid_trading_session"} <= rules


def test_audit_detects_missing_session_and_honors_official_session_closure(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {
        "coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": [],
    }
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])

    missing = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))

    assert missing["calendar_check"]["missing_sessions"] == [{"date": "2026-01-05", "session": "盤後"}]
    assert any(issue["rule"] == "missing_calendar_sessions" for issue in missing["issues"])

    calendar["session_closures"] = [{"date": "2026-01-05", "session": "盤後", "reason": "official suspension"}]
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    closed = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))

    assert closed["calendar_check"]["missing_sessions"] == []
    assert closed["calendar_check"]["closed_sessions"] == calendar["session_closures"]

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO txo_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-01-05", "TXO", "202601", "2026-01-21", 25050, "買權", "盤後", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"raw":"closed session"}'),
        )
    unexpected = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))

    assert unexpected["calendar_check"]["unexpected_sessions"] == [{
        "date": "2026-01-05", "session": "盤後", "reason": "official suspension", "record_count": 1,
    }]
    issue = next(item for item in unexpected["issues"] if item["rule"] == "unexpected_calendar_sessions")
    assert issue["sessions"][0]["records"][0]["record_key"]["strike"] == 25050
    assert issue["sessions"][0]["records"][0]["source_record"] == {"raw": "closed session"}


def test_audit_uses_one_read_snapshot_during_wal_writer(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps({"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}), encoding="utf-8")
    row = ("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")
    make_db(db_path, [row])
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")

    def write_during_audit(start, end, _calendar):
        with sqlite3.connect(db_path) as writer:
            writer.execute("INSERT INTO txo_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("2026-01-05", "TXO", "202601", "2026-01-21", 25050, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}"))
        return [start]

    monkeypatch.setattr(quality_check, "official_expected_days", write_during_audit)
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))

    assert report["summary"]["database_rows"] == report["summary"]["audited_rows"] == 1
    assert len(report["_sample_local_rows"]["2026-01-05"]) == 1


def test_duplicate_primary_key_report_contains_source_rows(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    rows = [
        ("2026-01-05", "TXO", "202601", None, 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"source":"first"}'),
        ("2026-01-05", "TXO", "202601", None, 25000, "買權", "一般", 1, 2, 1, 2, 2, 2, 1, 1, 2, '{"source":"second"}'),
    ]
    make_db(db_path, rows, nullable_expiry=True)
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    issue = next(item for item in report["issues"] if item["rule"] == "duplicate_primary_key")
    assert issue["count"] == 2
    assert {row["source_record"]["source"] for row in issue["records"]} == {"first", "second"}


@pytest.mark.parametrize("collision", ["database", "calendar"])
def test_run_refuses_to_overwrite_input_files(tmp_path, monkeypatch, collision):
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    db_path = out_dir / "data_quality.json" if collision == "database" else tmp_path / "test.sqlite3"
    calendar_path = out_dir / "data_quality.json" if collision == "calendar" else tmp_path / "calendar.json"
    if collision == "database":
        calendar_path.write_text(json.dumps({"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}), encoding="utf-8")
        make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])
    else:
        make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])
        calendar_path.write_text(json.dumps({"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}), encoding="utf-8")
    protected_path = db_path if collision == "database" else calendar_path
    original = protected_path.read_bytes()
    with pytest.raises(ValueError, match="不可覆寫輸入檔"):
        quality_check.run(db_path, calendar_path, out_dir, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    assert protected_path.read_bytes() == original


def test_official_sample_real_duplicate_csv_fails_with_both_source_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"source":"database"}')])
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        local = dict(db.execute("SELECT * FROM txo_daily").fetchone())
    header = "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,交易時段,契約到期日"
    first = "2026/01/05,TXO,202601,25000,買權,1,2,1,2,1,2,1,1,2,一般,20260121"
    duplicate = "2026/01/05,TXO,202601,25000,買權,1,2,1,2,3,2,1,1,2,一般,20260121"
    raw_csv = f"{header}\n{first}\n{duplicate}\n".encode("cp950")

    class FakeTaifex:
        def csv(self, *_args):
            return raw_csv

    monkeypatch.setattr(quality_check.download_txo, "Taifex", FakeTaifex)
    report = {"_sample_dates": ["2026-01-05"], "_sample_local_rows": {"2026-01-05": [local]}, "issues": [], "official_samples": []}
    quality_check.verify_official_samples(report, db_path)
    sample = report["official_samples"][0]
    assert sample["status"] == "failed"
    assert sample["official_csv_sha256"] == hashlib.sha256(raw_csv).hexdigest()
    assert sample["database_rows"] == 1
    assert sample["validation_error"]["rule"] == "duplicate_official_primary_key"
    assert len(sample["duplicate_official_keys"]) == 1
    assert [list(row["source_record"].values())[9] for row in sample["differences"][0]["official_records"]] == ["1", "3"]
    assert sample["differences"][0]["database_records"][0]["source_record"] == {"source": "database"}
    assert report["issues"][0]["rule"] == "official_sample_invalid"


def test_markdown_sample_error_does_not_include_raw_source_rows(tmp_path):
    report = {
        "status": "failed", "generated_at": "2026-01-01T00:00:00+00:00",
        "database": str(tmp_path / "private" / "data" / "test.sqlite3"),
        "summary": {"audit_start": "2026-01-05", "audit_end": "2026-01-05", "audited_rows": 1},
        "calendar_check": {
            "observed_trading_dates": 1, "expected_trading_dates": 1, "coverage_percent": 100,
            "observed_session_records": 2, "expected_session_records": 2, "session_coverage_percent": 100,
            "source": [], "missing_dates": [], "missing_sessions": [], "unexpected_sessions": [],
            "closed_sessions": [], "unexpected_dates": [], "weekday_closures": [],
        },
        "schema_check": {"matches": True, "duplicate_primary_key_groups": 0},
        "calendar": {"contract_rule_sources": []}, "null_counts": {},
        "distribution": {"max_volume": 1, "max_open_interest": 1},
        "official_samples": [{
            "date": "2026-01-05", "status": "failed", "database_rows": 1,
            "error_type": "DuplicateCSVPrimaryKey", "error": "DuplicateCSVPrimaryKey: secret raw row",
            "validation_error": {"rule": "duplicate_official_primary_key"},
        }],
        "issue_counts": [], "github_references": [],
    }

    rendered = quality_check.markdown(report)

    assert "錯誤類型：DuplicateCSVPrimaryKey" in rendered
    assert "duplicate_official_primary_key" in rendered
    assert "secret raw row" not in rendered
    assert "test.sqlite3" in rendered
    assert "private" not in rendered


def test_official_sample_match_records_raw_csv_hash(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    make_db(db_path, [(
        "2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般",
        1, 2, 1, 2, 1, 2, 1, 1, 2, '{"source":"database"}',
    )])
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        local = dict(db.execute("SELECT * FROM txo_daily").fetchone())
    header = "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,交易時段,契約到期日"
    row = "2026/01/05,TXO,202601,25000,買權,1,2,1,2,1,2,1,1,2,一般,20260121"
    raw_csv = f"{header}\n{row}\n".encode("cp950")

    class FakeTaifex:
        def csv(self, *_args):
            return raw_csv

    monkeypatch.setattr(quality_check.download_txo, "Taifex", FakeTaifex)
    report = {"_sample_dates": ["2026-01-05"], "_sample_local_rows": {"2026-01-05": [local]}, "issues": [], "official_samples": []}
    quality_check.verify_official_samples(report, db_path)

    sample = report["official_samples"][0]
    assert sample["status"] == "passed"
    assert sample["official_rows"] == sample["database_rows"] == 1
    assert sample["official_csv_sha256"] == hashlib.sha256(raw_csv).hexdigest()


def test_official_sample_compares_large_integer_fields_exactly(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    make_db(db_path, [(
        "2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般",
        1, 2, 1, 2, 9007199254740993, 2, 1, 1, 2, '{"source":"database"}',
    )])
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        local = dict(db.execute("SELECT * FROM txo_daily").fetchone())
    header = "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,交易時段,契約到期日"
    row = "2026/01/05,TXO,202601,25000,買權,1,2,1,2,9007199254740992,2,1,1,2,一般,20260121"
    raw_csv = f"{header}\n{row}\n".encode("cp950")

    class FakeTaifex:
        def csv(self, *_args):
            return raw_csv

    monkeypatch.setattr(quality_check.download_txo, "Taifex", FakeTaifex)
    report = {"_sample_dates": ["2026-01-05"], "_sample_local_rows": {"2026-01-05": [local]}, "issues": [], "official_samples": []}
    quality_check.verify_official_samples(report, db_path)

    sample = report["official_samples"][0]
    assert sample["status"] == "failed"
    assert sample["field_mismatch_rows"] == 1
    assert sample["differences"][0]["fields"]["volume"] == {
        "database": 9007199254740993, "official": 9007199254740992,
    }


def test_volume_without_ohlc_matching_official_block_trade_becomes_info(tmp_path, monkeypatch):
    def issue(strike, volume):
        key = {"trade_date": "2025-12-09", "contract_month": "202601", "contract_expiry": "2026-01-21",
               "strike": strike, "option_type": "賣權", "trading_session": "一般"}
        return {"rule": "volume_without_ohlc", "severity": "warning", "record_key": key,
                "fields": {"open": None, "high": None, "low": None, "close": None, "volume": volume}}

    raw_csv = ("組合式序號,日期,契約數,交易時段\n"
               "組合式序號,子序號,契約,到期月份(週別),履約價格,買/賣權,成交價格,成交數量\n"
               "C4,2025/12/09,2,一般,\nC4,1,TX,202512  ,-,-,28275,24,\nC4,2,TXO    ,202601  ,26600,P,244,660,\n").encode("big5")

    class FakeTaifex:
        def block_trades_csv(self, *_args):
            return raw_csv

    monkeypatch.setattr(quality_check.download_txo, "Taifex", FakeTaifex)
    report = {"issues": [issue(26600.0, 660), issue(27600.0, 660)]}
    quality_check.reconcile_block_trades(report)

    matched, unmatched = report["issues"]
    assert (matched["rule"], matched["severity"], matched["official_block_trade_volume"]) == ("volume_from_block_trade", "info", 660)
    assert (unmatched["rule"], unmatched["severity"], unmatched["official_block_trade_volume"]) == ("volume_without_ohlc", "warning", None)
    block = report["block_trade_reconciliation"]
    assert (block["status"], block["matched"]) == ("partial", 1)
    assert block["csv_sha256"][0]["sha256"] == hashlib.sha256(raw_csv).hexdigest()


def test_block_trade_download_failure_keeps_warnings(monkeypatch):
    class FailingTaifex:
        def __init__(self):
            raise OSError("offline")

    monkeypatch.setattr(quality_check.download_txo, "Taifex", FailingTaifex)
    report = {"issues": [{"rule": "volume_without_ohlc", "severity": "warning",
                          "record_key": {"trade_date": "2025-12-09"}, "fields": {"volume": 1}}]}
    quality_check.reconcile_block_trades(report)
    assert [item["rule"] for item in report["issues"]] == ["volume_without_ohlc", "block_trade_reconciliation_unavailable"]
