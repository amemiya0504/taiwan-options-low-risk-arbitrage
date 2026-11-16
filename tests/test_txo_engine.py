import datetime as dt
import json
import os
import sqlite3
import sys

import pytest

import txo_engine


def make_snapshot():
    asof = "2026-01-05T10:00:00+08:00"
    expiry = "2026-02-23"
    options = []
    rows = [
        (100, "C", 2, 3), (125, "C", 10, 11),
        (100, "P", 10, 11), (125, "P", 2, 3),
    ]
    for sequence, (strike, right, bid, ask) in enumerate(rows, 1):
        options.append({
            "expiry": expiry, "strike": strike, "right": right, "bid": bid, "ask": ask,
            "bid_size": 10, "ask_size": 10, "timestamp": asof, "received_at": asof,
            "sequence": sequence, "source": "fixture", "session": "day", "multiplier": 50,
        })
    return {
        "schema_version": 1, "asof": asof, "max_quote_age_seconds": 2,
        "max_quote_skew_seconds": 1,
        "expiries": {expiry: {
            "settlement_at": "2026-02-23T13:30:00+08:00", "pricing_rate": 0.0,
            "borrowing_rate": 0.0, "investment_rate": 0.0,
            "settlement_method": "TAIFEX_TAIEX_LAST30M",
            "settlement_id": "TAIFEX_TAIEX_LAST30M:2026-02-23",
        }},
        "costs": {
            "effective_date": "2026-01-01", "option_exchange_fee_twd": 0,
            "option_broker_fee_twd": 0, "option_tax_rate": 0,
            "option_settlement_fee_twd": 0, "option_settlement_tax_reserve_twd": 0,
            "forward_exchange_fee_twd": 0, "forward_broker_fee_twd": 0,
            "forward_tax_rate": 0, "forward_settlement_fee_twd": 0,
            "forward_settlement_tax_reserve_twd": 0,
        },
        "options": options,
        "forwards": [],
    }


def test_black76_put_call_parity_and_iv_round_trip():
    call = txo_engine.black76(100, 100, 1, 0, 0.2, "C")
    put = txo_engine.black76(100, 100, 1, 0, 0.2, "P")
    assert call == pytest.approx(7.9655674554, rel=1e-8)
    assert call - put == pytest.approx(txo_engine.discount_factor(0.03, 1) * (100 - 100))
    assert txo_engine.implied_volatility(call, 100, 100, 1, 0, "C") == pytest.approx(0.2, abs=1e-7)
    assert txo_engine.black76(100, 110, 1, 0, 0, "C") == 0


def test_iv_rejects_expired_and_out_of_bounds_prices():
    with pytest.raises(ValueError, match="到期"):
        txo_engine.implied_volatility(1, 100, 100, 0, 0, "C")
    with pytest.raises(ValueError, match="界外"):
        txo_engine.implied_volatility(101, 100, 100, 1, 0, "C")
    with pytest.raises(ValueError, match="下界"):
        txo_engine.implied_volatility(0, 100, 150, 1 / 365, 0.02, "C")
    with pytest.raises(ValueError, match="利率"):
        txo_engine.discount_factor(-1000, 1)


@pytest.mark.parametrize("price, expected", [(9.9, 0.1), (10, 0.5), (50, 1), (500, 5), (1000, 10)])
def test_taifex_option_price_tick_boundaries(price, expected):
    assert txo_engine.option_tick(price) == expected
    assert txo_engine.option_price_on_tick(price)
    assert not txo_engine.option_price_on_tick(price + expected / 2)


@pytest.mark.parametrize("spot", [50, 100, 105, 110, 150])
def test_box_payoff_is_fixed_outside_and_inside_strikes(spot):
    assert txo_engine.box_payoff(spot, 100, 110) == 10


def test_synchronized_box_snapshot_produces_costed_candidate():
    report = txo_engine.analyze_snapshot(make_snapshot())
    assert report["status"] == "analyzed"
    signal = next(item for item in report["signals"] if item["strategy"] == "long_box")
    assert signal["net_edge_twd"] == pytest.approx(1950)
    assert signal["present_value_payoff_twd"] == 1250


def test_session_aliases_are_canonicalized_before_synchronization():
    snapshot = make_snapshot()
    expected = next(
        item for item in txo_engine.analyze_snapshot(snapshot)["signals"]
        if item["strategy"] == "long_box"
    )
    snapshot["options"][0]["session"] = "一般"
    actual = next(
        item for item in txo_engine.analyze_snapshot(snapshot)["signals"]
        if item["strategy"] == "long_box"
    )
    assert actual["net_edge_twd"] == expected["net_edge_twd"]

    quote = txo_engine.Quote.from_dict(snapshot["options"][0])
    forward = txo_engine.ForwardQuote.from_dict({
        "expiry": "2026-02-23", "bid": 99, "ask": 100, "bid_size": 2, "ask_size": 2,
        "timestamp": snapshot["asof"], "received_at": snapshot["asof"], "sequence": 1,
        "source": "fixture-forward", "session": "一般",
        "settlement_id": "TAIFEX_TAIEX_LAST30M:2026-02-23", "multiplier": 50,
    })
    assert quote.session == forward.session == "day"


def test_zero_edge_long_box_is_not_reported_as_arbitrage():
    snapshot = make_snapshot()
    for quote, premium in zip(snapshot["options"], (16.5, 0.1, 0.1, 8.7)):
        quote["bid"] = quote["ask"] = premium

    report = txo_engine.analyze_snapshot(snapshot)

    assert not any(item["strategy"] == "long_box" for item in report["signals"])


def test_extreme_integer_strike_returns_rejected_snapshot():
    snapshot = make_snapshot()
    snapshot["options"][0]["strike"] = 10**309

    report = txo_engine.analyze_snapshot(snapshot)

    assert report["status"] == "rejected"
    assert "有限數值" in report["error"]
    assert report["signals"] == []


def test_overflowing_quote_arithmetic_returns_rejected_snapshot():
    snapshot = make_snapshot()
    snapshot["options"][0]["bid"] = snapshot["options"][0]["ask"] = 1e308

    report = txo_engine.analyze_snapshot(snapshot)

    assert report["status"] == "rejected"
    assert report["signals"] == []


@pytest.mark.parametrize("closed", [False, True])
def test_snapshot_rejects_weekend_or_closed_expiry(closed):
    snapshot = make_snapshot()
    if closed:
        expiry = "2026-02-20"
        original = snapshot["expiries"].pop("2026-02-23")
        original["settlement_at"] = expiry + "T13:30:00+08:00"
        original["settlement_id"] = "TAIFEX_TAIEX_LAST30M:" + expiry
        snapshot["expiries"][expiry] = original
        for quote in snapshot["options"]:
            quote["expiry"] = expiry
    else:
        snapshot["asof"] = "2026-01-04T10:00:00+08:00"
        for quote in snapshot["options"]:
            quote["timestamp"] = quote["received_at"] = snapshot["asof"]

    report = txo_engine.analyze_snapshot(snapshot)

    assert report["status"] == "rejected"
    assert report["signals"] == []


def test_snapshot_rejects_asof_after_session_close():
    snapshot = make_snapshot()
    snapshot["asof"] = "2026-01-05T13:45:01+08:00"
    for quote in snapshot["options"]:
        quote["timestamp"] = quote["received_at"] = "2026-01-05T13:45:00+08:00"

    report = txo_engine.analyze_snapshot(snapshot)

    assert report["status"] == "rejected"
    assert "13:45" in report["error"]


def test_expensive_costs_remove_box_candidate():
    snapshot = make_snapshot()
    snapshot["costs"]["option_exchange_fee_twd"] = 1000
    report = txo_engine.analyze_snapshot(snapshot)
    assert not any(item["strategy"] == "long_box" for item in report["signals"])


def test_parity_requires_independent_matched_settlement_forward():
    snapshot = make_snapshot()
    snapshot["forwards"] = [{
        "expiry": "2026-02-23", "bid": 99, "ask": 100, "bid_size": 2, "ask_size": 2,
        "timestamp": snapshot["asof"], "received_at": snapshot["asof"], "sequence": 5,
        "source": "fixture-forward", "session": "day", "settlement_id": "different-contract", "multiplier": 50,
    }]
    report = txo_engine.analyze_snapshot(snapshot)
    assert not any(item["strategy"].startswith("put_call_parity") for item in report["signals"])
    snapshot["forwards"][0]["settlement_id"] = "TAIFEX_TAIEX_LAST30M:2026-02-23"
    report = txo_engine.analyze_snapshot(snapshot)
    assert any(item["strategy"] == "put_call_parity_short_synthetic_long_forward" for item in report["signals"])


def test_forward_parity_costs_include_entry_tax_and_expiry_reserves():
    snapshot = make_snapshot()
    snapshot["options"] = [
        {**snapshot["options"][0], "strike": 27000, "bid": 11, "ask": 11.5},
        {**snapshot["options"][2], "strike": 27000, "bid": 10, "ask": 10.5},
    ]
    snapshot["forwards"] = [{
        "expiry": "2026-02-23", "bid": 27000, "ask": 27000, "bid_size": 2, "ask_size": 2,
        "timestamp": snapshot["asof"], "received_at": snapshot["asof"], "sequence": 5,
        "source": "fixture-forward", "session": "day",
        "settlement_id": "TAIFEX_TAIEX_LAST30M:2026-02-23", "multiplier": 50,
    }]
    assert any(item["strategy"] == "put_call_parity_short_synthetic_long_forward"
               for item in txo_engine.analyze_snapshot(snapshot)["signals"])
    snapshot["costs"].update(
        forward_tax_rate=0.00002,
        forward_settlement_fee_twd=5,
        forward_settlement_tax_reserve_twd=27,
    )
    report = txo_engine.analyze_snapshot(snapshot)
    assert not any(item["strategy"].startswith("put_call_parity") for item in report["signals"])


def test_forward_quote_obeys_taifex_tx_one_point_tick():
    snapshot = make_snapshot()
    snapshot["forwards"] = [{
        "expiry": "2026-02-23", "bid": 99.5, "ask": 100, "bid_size": 2, "ask_size": 2,
        "timestamp": snapshot["asof"], "received_at": snapshot["asof"], "sequence": 5,
        "source": "fixture-forward", "session": "day",
        "settlement_id": "TAIFEX_TAIEX_LAST30M:2026-02-23", "multiplier": 50,
    }]
    report = txo_engine.analyze_snapshot(snapshot)
    assert report["status"] == "rejected"
    assert "1 點跳動單位" in report["error"]


def test_option_quote_rejects_strike_off_minimum_taifex_grid():
    snapshot = make_snapshot()
    snapshot["options"][1]["strike"] = 110
    report = txo_engine.analyze_snapshot(snapshot)
    assert report["status"] == "rejected"
    assert "最小間距" in report["error"]


def test_parity_uses_currency_once_and_discount_by_locked_cashflow_sign():
    snapshot = make_snapshot()
    snapshot["options"] = [
        {**snapshot["options"][0], "strike": 25000, "bid": 10.5, "ask": 10.5},
        {**snapshot["options"][2], "strike": 25000, "bid": 20, "ask": 20},
    ]
    expiry = snapshot["expiries"]["2026-02-23"]
    expiry["borrowing_rate"] = 1.0
    expiry["investment_rate"] = 0.0
    snapshot["forwards"] = [{
        "expiry": "2026-02-23", "bid": 24990, "ask": 24990, "bid_size": 2, "ask_size": 2,
        "timestamp": snapshot["asof"], "received_at": snapshot["asof"], "sequence": 5,
        "source": "fixture-forward", "session": "day",
        "settlement_id": expiry["settlement_id"], "multiplier": 50,
    }]
    report = txo_engine.analyze_snapshot(snapshot)
    assert report["status"] == "analyzed"
    assert not any(item["strategy"].startswith("put_call_parity") for item in report["signals"])


def test_bad_or_stale_snapshot_is_rejected_with_reason():
    snapshot = make_snapshot()
    snapshot["options"][0].pop("sequence")
    report = txo_engine.analyze_snapshot(snapshot)
    assert report["status"] == "rejected" and "sequence" in report["error"]
    snapshot = make_snapshot()
    snapshot["max_quote_age_seconds"] = 1e9
    snapshot["options"][0]["timestamp"] = "2016-01-05T09:00:00+08:00"
    report = txo_engine.analyze_snapshot(snapshot)
    assert report["status"] == "rejected" and "過期" in report["error"]


def test_borrowing_rate_must_not_understate_investment_rate():
    snapshot = make_snapshot()
    snapshot["expiries"]["2026-02-23"]["borrowing_rate"] = 0.0
    snapshot["expiries"]["2026-02-23"]["investment_rate"] = 0.99
    report = txo_engine.analyze_snapshot(snapshot)
    assert report["status"] == "rejected"
    assert "borrowing_rate" in report["error"]


def test_negative_financing_rates_are_rejected_but_negative_pricing_rate_is_allowed():
    snapshot = make_snapshot()
    snapshot["expiries"]["2026-02-23"]["pricing_rate"] = -0.01
    assert txo_engine.analyze_snapshot(snapshot)["status"] == "analyzed"
    snapshot["expiries"]["2026-02-23"]["borrowing_rate"] = -0.01
    snapshot["expiries"]["2026-02-23"]["investment_rate"] = -0.02
    report = txo_engine.analyze_snapshot(snapshot)
    assert report["status"] == "rejected"
    assert "borrowing_rate" in report["error"]


def test_option_tax_rounds_per_contract_to_whole_twd():
    quote = txo_engine.Quote.from_dict({
        "expiry": "2026-02-23", "strike": 25000, "right": "C", "bid": 10, "ask": 10,
        "bid_size": 10, "ask_size": 10, "timestamp": "2026-01-05T10:00:00+08:00",
        "received_at": "2026-01-05T10:00:00+08:00", "sequence": 1,
        "source": "fixture", "session": "day", "multiplier": 50,
    })
    cost = {"option_exchange_fee_twd": 0, "option_broker_fee_twd": 0, "option_tax_rate": 0.001,
            "option_settlement_fee_twd": 0, "option_settlement_tax_reserve_twd": 0}
    _, one_contract_cost = txo_engine._option_trade_cost(quote, "BUY", cost)
    _, ten_contract_cost = txo_engine._option_trade_cost(quote, "BUY", cost, 10)
    assert one_contract_cost == 1
    assert ten_contract_cost == 10


def test_readiness_uses_read_only_schema_probe(tmp_path):
    path = tmp_path / "mini.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE txo_daily (trade_date TEXT, best_bid REAL, best_ask REAL)")
        db.execute("INSERT INTO txo_daily VALUES ('2026-01-05', 1, 2)")
    before = path.read_bytes()
    report = txo_engine.database_readiness(path)
    assert report["status"] == "diagnostic_only"
    assert report["historical_executable_backtest_supported"] is False
    assert "quote_timestamp" in report["missing_execution_replay_fields"]
    assert path.read_bytes() == before


def test_future_cost_date_and_wrong_settlement_date_reject_snapshot():
    snapshot = make_snapshot()
    snapshot["costs"]["effective_date"] = "2026-02-01"
    assert "晚於" in txo_engine.analyze_snapshot(snapshot)["error"]
    snapshot = make_snapshot()
    snapshot["expiries"]["2026-02-23"]["settlement_at"] = "2027-02-20T13:30:00+08:00"
    assert "結算時點" in txo_engine.analyze_snapshot(snapshot)["error"]


def test_borrowing_rate_can_reverse_box_candidate():
    snapshot = make_snapshot()
    snapshot["options"] = [
        {**snapshot["options"][0], "bid": 19.5, "ask": 20},
        {**snapshot["options"][1], "strike": 125, "bid": 18, "ask": 18.5},
        {**snapshot["options"][2], "bid": 7, "ask": 7.5},
        {**snapshot["options"][3], "strike": 125, "bid": 29.5, "ask": 30},
    ]
    snapshot["expiries"]["2026-02-23"]["borrowing_rate"] = 0.10
    report = txo_engine.analyze_snapshot(snapshot)
    assert not any(item["strategy"] == "long_box" for item in report["signals"])


def test_butterfly_requires_available_size_for_every_weight():
    snapshot = make_snapshot()
    expiry = "2026-02-23"
    base = snapshot["options"][0]
    snapshot["options"] = [
        {**base, "strike": 100, "bid": 1, "ask": 2, "bid_size": 1, "ask_size": 1},
        {**base, "strike": 125, "bid": 10, "ask": 11, "bid_size": 1, "ask_size": 1},
        {**base, "strike": 175, "bid": 1, "ask": 2, "bid_size": 1, "ask_size": 1},
    ]
    report = txo_engine.analyze_snapshot(snapshot)
    assert not any("butterfly" in item["strategy"] for item in report["signals"])


def test_atomic_report_write_refuses_hard_link_to_input(tmp_path):
    source = tmp_path / "input.sqlite3"
    output = tmp_path / "report.json"
    source.write_bytes(b"sqlite source data")
    try:
        os.link(source, output)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    with pytest.raises(ValueError, match="不可覆寫"):
        txo_engine._write_json({"status": "ok"}, output, source)
    assert source.read_bytes() == b"sqlite source data"


def test_json_output_cannot_overwrite_sqlite_even_when_renamed(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    database = tmp_path / "market.json"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE txo_daily (value INTEGER)")
    original = database.read_bytes()
    with pytest.raises(ValueError, match="SQLite 資料庫"):
        txo_engine._write_json({"status": "ok"}, database, snapshot)
    assert database.read_bytes() == original


@pytest.mark.parametrize("suffix, header", [
    ("-wal", b"\x37\x7f\x06\x82"),
    ("-shm", b""),
    ("-journal", b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"),
])
def test_json_output_cannot_overwrite_sqlite_sidecars(tmp_path, suffix, header):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    database = tmp_path / "market.sqlite3"
    sidecar = tmp_path / f"market.sqlite3{suffix}"
    if header:
        sidecar.write_bytes(header + b"contents")
        with pytest.raises(ValueError, match="SQLite"):
            txo_engine._write_json({"status": "ok"}, sidecar, snapshot)
    else:
        with pytest.raises(ValueError, match="sidecar"):
            txo_engine._write_json({"status": "ok"}, sidecar, database)


def test_analyze_cli_returns_nonzero_when_snapshot_is_rejected(tmp_path, monkeypatch):
    snapshot = tmp_path / "invalid.json"
    output = tmp_path / "report.json"
    snapshot.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "txo_engine.py", "analyze", "--snapshot", str(snapshot), "--output", str(output),
    ])

    assert txo_engine.main() == 2
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "rejected"
