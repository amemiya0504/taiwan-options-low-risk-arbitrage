"""Local-only paper ledger for finite-risk, same-expiry option baskets."""

from __future__ import annotations

import copy
import datetime as dt
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from market_time import require_txo_expiry, validate_txo_session

from txo_engine import MAX_COST_AGE_DAYS, MAX_QUOTE_AGE_SECONDS, MAX_QUOTE_SKEW_SECONDS, MULTIPLIER, SETTLEMENT_METHOD, option_price_on_tick, round_twd

TAIPEI = dt.timezone(dt.timedelta(hours=8), "Asia/Taipei")
SESSION_NAMES = {"day": "一般", "一般": "一般", "night": "盤後", "盤後": "盤後"}


def _finite_amount(*values) -> bool:
    try:
        return all(math.isfinite(value) for value in values)
    except (OverflowError, TypeError):
        return False


def _time(value: str, name: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{name} 必須是含時區的 ISO 8601 時間") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} 必須包含時區")
    return parsed.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class RiskLimits:
    max_daily_loss_twd: float
    max_open_risk_twd: float
    max_contracts: int

    def __post_init__(self):
        for name in ("max_daily_loss_twd", "max_open_risk_twd"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite_amount(value) or value <= 0:
                raise ValueError(f"{name} 必須是正的有限金額")
        if not isinstance(self.max_contracts, int) or isinstance(self.max_contracts, bool) or self.max_contracts <= 0:
            raise ValueError("max_contracts 必須是正整數")


@dataclass(frozen=True)
class PaperCosts:
    effective_date: dt.date
    option_fee_twd_per_contract_side: float
    option_tax_rate: float
    settlement_fee_twd_per_contract: float
    settlement_tax_rate: float
    max_settlement_index_points: float

    def __post_init__(self):
        if not isinstance(self.effective_date, dt.date) or isinstance(self.effective_date, dt.datetime):
            raise ValueError("effective_date 必須是日期")
        for name in (
            "option_fee_twd_per_contract_side", "option_tax_rate",
            "settlement_fee_twd_per_contract", "settlement_tax_rate", "max_settlement_index_points",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite_amount(value) or value < 0:
                raise ValueError(f"{name} 必須是非負有限數值")
        if self.max_settlement_index_points <= 0:
            raise ValueError("max_settlement_index_points 必須大於零")
        if self.option_tax_rate > 1 or self.settlement_tax_rate > 1:
            raise ValueError("稅率必須介於 0 至 1")


class MockBroker:
    """In-memory atomic simulator, limited to boxes, verticals, and butterflies."""

    def __init__(self, limits: RiskLimits, costs: PaperCosts):
        self.limits, self.costs = limits, costs
        self._orders, self._fills, self._audit = {}, {}, []
        self._reserved_depth, self._consumed_depth = defaultdict(int), defaultdict(int)
        self._order_depth, self._order_risk, self._order_entry_cost, self._fill_cashflow = {}, {}, {}, {}
        self._order_risk = {}
        self._quote_events, self._quote_sequences, self._latest_quote_events, self._settlement_events = {}, {}, {}, {}
        self._reserved_risk = 0.0
        self._reserved_contracts = 0
        self._daily_pnl = defaultdict(float)
        self._kill_switch_days = set()
        self._manual_kill_switch = False
        self._latest_event_time = None

    def _validate(self, intent: dict) -> tuple[str | None, float, int, float, float, dict, dt.datetime | None, dict, dict, dict]:
        def reject(reason: str):
            return reason, 0.0, 0, 0.0, 0.0, {}, None, {}, {}, {}

        if not isinstance(intent, dict) or intent.get("mode") != "paper":
            return reject("只接受 mode=paper")
        strategy = intent.get("strategy")
        if not isinstance(strategy, str) or strategy not in {"box_spread", "vertical_arbitrage", "butterfly"}:
            return reject("只接受箱型、垂直價差或蝶式選擇權籃子")
        if not isinstance(intent.get("client_order_id"), str) or not intent["client_order_id"].strip():
            return reject("client_order_id 不可空白")
        package_qty = intent.get("quantity")
        if not isinstance(package_qty, int) or isinstance(package_qty, bool) or package_qty <= 0:
            return reject("quantity 必須是正整數")
        try:
            asof = _time(intent["asof"], "asof")
        except (KeyError, ValueError) as exc:
            return reject(str(exc))
        cost_age = (asof.astimezone(TAIPEI).date() - self.costs.effective_date).days
        if cost_age < 0:
            return reject("費率生效日晚於 asof")
        if cost_age > MAX_COST_AGE_DAYS:
            return reject("費率資料超過 30 日，需重新核對")
        legs = intent.get("legs")
        if not isinstance(legs, list) or len(legs) < 2:
            return reject("只接受多腿組合")

        expiry_set, settlement_set, sessions = set(), set(), set()
        quote_times, receive_times = [], []
        positions, contracts, entry_costs, settlement_fees, gross_entry_cashflow, fill_cashflow = [], 0, 0.0, 0.0, 0.0, 0.0
        depth, depth_limits, contract_keys = defaultdict(int), {}, set()
        settlement_times = set()
        quote_events, quote_sequences = {}, {}
        for leg in legs:
            if not isinstance(leg, dict) or leg.get("instrument") != "option":
                return reject("MockBroker 不接受期貨、遠期或未識別商品腿")
            if (not isinstance(leg.get("right"), str) or leg["right"] not in {"C", "P"}
                    or not isinstance(leg.get("side"), str) or leg["side"] not in {"BUY", "SELL"}):
                return reject("option ley�~�����k�w��`    forward_settlement_tax_reserve_twd=27,
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
