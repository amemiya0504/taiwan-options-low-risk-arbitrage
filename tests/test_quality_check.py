import datetime as dt
import json
import sqlite3

import quality_check


def make_db(path, rows):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE txo_daily (
        trade_date TEXT NOT NULL, symbol TEXT NOT NULL, contract_month TEXT NOT NULL,
        contract_expiry TEXT NOT NULL DEFAULT '', strike REAL NOT NULL, option_type TEXT NOT NULL,
        trading_session TEXT NOT NULL DEFAULT '', open REAL, high REAL, low REAL, close REAL,
        volume INTEGER NOT NULL, settlement_price REAL, open_interest INTEGER,
        best_bid REAL, best_ask REAL, source_json TEXT NOT NULL,
        PRIMARY KEY (trade_date, contract_month, contract_expiry, strike, option_type, trading_session))""")
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


def test_audit_finds_bad_row_and_reports_source(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [(
        "2026-01-05", "TXO", "202601", "", 25000, "買權", "一般",
        11, 9, 10, 12, -1, 11, -3, 12, 10, '{"原始欄位":"可追溯"}',
    )])
    report = quality_check.audit_database(db_path, calendar_path)
    rules = {issue["rule"] for issue in report["issues"]}
    assert report["calendar_check"]["coverage_percent"] == 100.0
    assert {"ohlc_high_below_low", "ohlc_outside_range", "invalid_volume_or_open_interest", "crossed_best_quote"} <= rules
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_volume_or_open_interest")
    assert issue["source_record"] == {"原始欄位": "可追溯"}


def test_run_writes_both_reports_without_network(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    out_dir = tmp_path / "reports"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])
    original_db = db_path.read_bytes()
    monkeypatch.setattr(quality_check, "verify_official_samples", lambda report, path: report.pop("_sample_dates"))
    report = quality_check.run(db_path, calendar_path, out_dir)
    assert report["status"] == "passed"
    assert db_path.read_bytes() == original_db
    assert (out_dir / "data_quality.json").is_file()
    assert (out_dir / "data_quality.md").is_file()


def test_audit_reports_invalid_date_instead_of_crashing(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-99-99", "TXO", "202601", "", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"raw":"bad date"}')])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_trade_date")
    assert issue["source_record"] == {"raw": "bad date"}
