import json
import sqlite3

import pytest

import close_backtest

ZERO_COSTS = {key: 0 for key in close_backtest.COST_KEYS}


def test_long_box_net_pv_matches_hand_calculation():
    # BUY C1@120, SELL C2@30, SELL P1@20, BUY P2@20 -> debit 90 points; width 100 points.
    strikes = {20000: {"C": (119, 120, 5), "P": (20, 21, 5)}, 20100: {"C": (30, 31, 5), "P": (19, 20, 5)}}
    costs = {**ZERO_COSTS, "option_exchange_fee_twd": 6, "option_tax_rate": 0.001}
    found = close_backtest.box_candidates(strikes, 1.0, costs)
    long_box = [item for item in found if item["strategy"] == "long_box"]
    tax = sum(round(p * 50 * 0.001) for p in (120, 30, 20, 20))
    assert long_box == [{"strategy": "long_box", "strikes": [20000, 20100], "net_pv_twd": 10 * 50 - 4 * 6 - tax,
                         "entry_cash_twd": -4500, "all_legs_traded": True}]


def test_financing_cost_removes_long_box_and_untraded_leg_is_flagged():
    strikes = {20000: {"C": (119, 120, 5), "P": (20, 21, 0)}, 20100: {"C": (30, 31, 5), "P": (19, 20, 5)}}
    assert close_backtest.box_candidates(strikes, 1.0, ZERO_COSTS)[0]["all_legs_traded"] is False
    # 折現 0.85：long box 失去優勢；short box 收 86 點、到期付 100 點的現值 85 點，淨 1 點。
    discounted = close_backtest.box_candidates(strikes, 0.85, ZERO_COSTS)
    assert [(item["strategy"], item["net_pv_twd"]) for item in discounted] == [("short_box", 50)]


def test_run_reads_db_and_keeps_one_traded_box_per_day(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    db = sqlite3.connect(db_path)
    db.execute("""CREATE TABLE txo_daily (trade_date TEXT, trading_session TEXT, contract_month TEXT, contract_expiry TEXT,
                  strike REAL, option_type TEXT, best_bid REAL, best_ask REAL, volume INTEGER)""")
    rows = [("2026-01-05", "一般", "202601", "2026-01-21", 20000, "買權", 119, 120, 5),
            ("2026-01-05", "一般", "202601", "2026-01-21", 20000, "賣權", 20, 21, 5),
            ("2026-01-05", "一般", "202601", "2026-01-21", 20100, "買權", 30, 31, 5),
            ("2026-01-05", "一般", "202601", "2026-01-21", 20100, "賣權", 19, 20, 5),
            ("2025-12-01", "一般", "202512", "", 20000, "買權", 1, 2, 5)]
    db.executemany("INSERT INTO txo_daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    report = close_backtest.run(db_path, close_backtest.parse_date("2025-12-01"), close_backtest.parse_date("2026-01-31"),
                                ZERO_COSTS, 0.0)
    assert report["skipped_rows_without_expiry"] == 1
    assert report["summary"]["days_with_traded_candidate"] == 1
    assert report["daily_best"][0]["net_pv_twd"] == 500
    json.dumps(report)


def test_cost_file_must_have_all_option_fields(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"option_exchange_fee_twd": 6}), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少"):
        close_backtest.load_costs(path)
    assert close_backtest.load_costs(close_backtest.Path("examples/costs.example.json"))["option_broker_fee_twd"] == 10


def test_postponed_expiry_uses_official_last_trading_date(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    db = sqlite3.connect(db_path)
    db.execute("""CREATE TABLE txo_daily (trade_date TEXT, trading_session TEXT, contract_month TEXT, contract_expiry TEXT,
                  strike REAL, option_type TEXT, best_bid REAL, best_ask REAL, volume INTEGER)""")
    db.execute("INSERT INTO txo_daily VALUES ('2026-07-13','一般','202607F2','2026-07-10',20000,'買權',1,2,1)")
    db.commit()
    db.close()
    start = end = close_backtest.parse_date("2026-07-13")
    with pytest.raises(ValueError, match="順延"):
        close_backtest.run(db_path, start, end, ZERO_COSTS, 0.0)
    report = close_backtest.run(db_path, start, end, ZERO_COSTS, 0.0, {("202607F2", "2026-07-10"): "2026-07-13"})
    assert report["summary"]["quote_groups"] == 1
