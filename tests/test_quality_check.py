import datetime as dt
import json
import sqlite3

import quality_check
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


def test_run_writes_both_reports_without_network(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    out_dir = tmp_path / "reports"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, "{}")])
    original_db = db_path.read_bytes()
    monkeypatch.setattr(quality_check, "verify_official_samples", lambda report, path: report.pop("_sample_dates"))
    report = quality_check.run(db_path, calendar_path, out_dir, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    assert report["status"] == "passed"
    assert db_path.read_bytes() == original_db
    assert (out_dir / "data_quality.json").is_file()
    assert (out_dir / "data_quality.md").is_file()


def test_audit_reports_invalid_date_instead_of_crashing(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-07", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-06bad", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"raw":"bad date"}')])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 7))
    issue = next(item for item in report["issues"] if item["rule"] == "invalid_trade_date")
    assert issue["source_record"] == {"raw": "bad date"}


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


def test_contract_expiry_strike_and_session_rules(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    calendar_path = tmp_path / "calendar.json"
    calendar = {"coverage_start": "2026-01-05", "coverage_end": "2026-01-05", "closed_weekdays": [], "sources": []}
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    make_db(db_path, [("2026-01-05", "TXO", "202601W1", "2026-06-17", 25001, "買權", "GARBAGE", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"raw":"bad contract"}')])
    report = quality_check.audit_database(db_path, calendar_path, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    rules = {issue["rule"] for issue in report["issues"]}
    assert {"contract_expiry_mismatch", "invalid_strike_increment", "invalid_trading_session"} <= rules


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


def test_official_sample_duplicate_keys_fail_with_row_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    make_db(db_path, [("2026-01-05", "TXO", "202601", "2026-01-21", 25000, "買權", "一般", 1, 2, 1, 2, 1, 2, 1, 1, 2, '{"source":"database"}')])
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        local = dict(db.execute("SELECT * FROM txo_daily").fetchone())
    duplicate = local.copy()
    duplicate["volume"] = 3
    duplicate["source_json"] = '{"source":"duplicate"}'

    class FakeTaifex:
        def csv(self, *_args):
            return b"fixture"

    monkeypatch.setattr(quality_check.download_txo, "Taifex", FakeTaifex)
    monkeypatch.setattr(quality_check.download_txo, "parse_csv", lambda _data: [local, duplicate])
    report = {"_sample_dates": ["2026-01-05"], "issues": [], "official_samples": []}
    quality_check.verify_official_samples(report, db_path)
    sample = report["official_samples"][0]
    assert sample["status"] == "failed"
    assert sample["official_rows"] == 2 and sample["database_rows"] == 1
    assert len(sample["duplicate_official_keys"]) == 1
    assert {row["source_record"]["source"] for row in sample["differences"][0]["official_records"]} == {"database", "duplicate"}
