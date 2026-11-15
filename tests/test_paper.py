import datetime as dt

import pytest

from paper import MockBroker, PaperCosts, RiskLimits, performance_metrics


ASOF = "2026-01-05T10:00:00+08:00"
EXPIRY = "2026-02-20"
SETTLEMENT_ID = "TAIFEX_TAIEX_LAST30M:2026-02-20"
SETTLEMENT_AT = "2026-02-20T13:30:00+08:00"


def intent(client_id="paper-1"):
    rows = [
        ("C", 100, "BUY", 24.5, 25),
        ("C", 125, "SELL", 5, 5.5),
        ("P", 100, "SELL", 5, 5.5),
        ("P", 125, "BUY", 24.5, 25),
    ]
    legs = []
    for sequence, (right, strike, side, bid, ask) in enumerate(rows, 1):
        legs.append({
            "symbol": "UNTRUSTED-SYMBOL", "instrument": "option", "right": right,
            "strike": strike, "expiry": EXPIRY, "settlement_id": SETTLEMENT_ID,
            "settlement_at": SETTLEMENT_AT, "side": side, "quantity": 1,
            "limit_price": ask if side == "BUY" else bid, "bid": bid, "ask": ask,
            "bid_size": 5, "ask_size": 5, "timestamp": ASOF, "received_at": ASOF,
            "sequence": sequence, "source": "fixture", "session": "day", "multiplier": 50,
        })
    return {
        "client_order_id": client_id, "asof": ASOF, "mode": "paper", "strategy": "box_spread",
        "quantity": 1, "legs": legs, "max_loss_twd": 1,
    }


def broker(limits=None, costs=None):
    limits = limits or RiskLimits(1000, 10000, 10)
    costs = costs or PaperCosts(dt.date(2026, 1, 1), 1, 0.001, 1, 0.00002, 50000)
    return MockBroker(limits, costs)


def test_mock_broker_derives_risk_fill_and_settlement_pnl():
    mock = broker()
    first = mock.submit(intent())
    assert first["status"] == "pending"
    assert first["computed_stress_loss_twd"] == pytest.approx(960)
    assert first["computed_entry_cost_twd"] == pytest.approx(6)
    assert first["computed_settlement_fee_twd"] == pytest.approx(4)
    assert "不是保證最大損失" in first["risk_basis"]
    assert mock.submit(intent()) == first
    fill = mock.fill_all("paper-1", "fill-1", ASOF)
    assert fill["status"] == "filled"
    assert fill["entry_cashflow_twd"] == pytest.approx(-2006)
    assert fill["fills"][0]["symbol"] == "TXO-2026-02-20-C-100"
    assert mock.fill_all("paper-1", "fill-1", ASOF) == fill
    closed = mock.close("paper-1", 30000, SETTLEMENT_AT, "fixture-settlement")
    assert closed["realized_pnl_twd"] == pytest.approx(-818)
    assert closed["settlement"]["option_payoff_twd"] == pytest.approx(1250)
    assert closed["settlement"]["settlement_tax_twd"] == 60
    assert closed["settlement"]["settlement_fee_twd"] == 2
    assert closed["realized_pnl_twd"] == pytest.approx(-818)
    assert closed["settlement"]["source_authenticated"] is False
    assert mock.close("paper-1", 30000, SETTLEMENT_AT, "fixture-settlement") == closed
    with pytest.raises(ValueError, match="衝突"):
        mock.close("paper-1", 1, SETTLEMENT_AT, "fixture-settlement")
    assert [event["event"] for event in mock.audit_log()] == ["submit", "fill", "close"]


def test_pending_orders_reserve_and_cancel_releases_quote_depth_and_risk():
    mock = broker(RiskLimits(10000, 2000, 8))
    first_intent = intent()
    for leg in first_intent["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
    assert mock.submit(first_intent)["status"] == "pending"
    second_intent = intent("paper-2")
    for leg in second_intent["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
    assert "口數已被" in mock.submit(second_intent)["reason"]
    assert mock.cancel("paper-1")["status"] == "cancelled"
    assert mock.submit(second_intent)["status"] == "pending"


@pytest.mark.parametrize("mutation, expected", [
    (lambda order: order.update(mode="live"), "mode=paper"),
    (lambda order: order["legs"].pop(), "契約腿結構"),
    (lambda order: order["legs"][0].update(instrument="forward"), "不接受期貨"),
    (lambda order: order["legs"][0].update(ask_size=0), "口數不足"),
    (lambda order: order["legs"][0].update(settlement_id="wrong"), "settlement_id"),
    (lambda order: order.update(strategy="calendar_spread"), "只接受箱型"),
    (lambda order: order["legs"].append(order["legs"][0].copy()), "重複列腿"),
    (lambda order: order["legs"][0].update(strike="100"), "數值型別"),
    (lambda order: order["legs"][1].update(strike=110), "最小間距"),
])
def test_broker_rejects_invalid_or_unsupported_intents(mutation, expected):
    order = intent()
    mutation(order)
    result = broker().submit(order)
    assert result["status"] == "rejected"
    assert expected in result["reason"]


def test_broker_uses_computed_risk_and_broker_costs_not_caller_claims():
    limits = RiskLimits(1000, 100, 10)
    assert "max_open_risk" in broker(limits).submit(intent())["reason"]
    high_costs = PaperCosts(dt.date(2026, 1, 1), 10001, 0.001, 1, 0.00002, 50000)
    assert broker(costs=high_costs).submit(intent())["status"] == "rejected"


def test_daily_loss_budget_includes_reserved_stress_risk():
    limits = RiskLimits(300, 10000, 10)
    result = broker(limits).submit(intent())
    assert result["status"] == "rejected"
    assert "max_daily_loss_twd" in result["reason"]


def test_daily_loss_ledger_is_date_scoped():
    costs = PaperCosts(dt.date(2026, 2, 1), 1, 0.001, 1, 0.00002, 50000)
    mock = broker(RiskLimits(961, 10000, 10), costs)
    first = intent()
    first["asof"] = "2026-02-01T10:00:00+08:00"
    for leg in first["legs"]:
        leg["timestamp"] = leg["received_at"] = first["asof"]
    mock.submit(first)
    mock.fill_all("paper-1", "fill-1", first["asof"])
    mock.close("paper-1", 30000, SETTLEMENT_AT, "fixture-settlement")

    later_day = intent("later-day")
    later_day["asof"] = "2026-02-20T14:00:00+08:00"
    for leg in later_day["legs"]:
        leg.update(expiry="2026-03-18", settlement_id="TAIFEX_TAIEX_LAST30M:2026-03-18",
                   settlement_at="2026-03-18T13:30:00+08:00", timestamp=later_day["asof"],
                   received_at=later_day["asof"], sequence=leg["sequence"] + 100)
    assert "max_daily_loss_twd" in mock.submit(later_day)["reason"]

    later_day["client_order_id"] = "next-day"
    later_day["asof"] = "2026-02-23T09:00:00+08:00"
    for leg in later_day["legs"]:
        leg["timestamp"] = leg["received_at"] = later_day["asof"]
        leg["sequence"] += 100
    assert mock.submit(later_day)["status"] == "pending"


def test_first_seen_quote_is_immutable_even_if_source_or_sequence_changes():
    mock = broker(RiskLimits(5000, 10000, 10))
    first = intent()
    for leg in first["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
    assert mock.submit(first)["status"] == "pending"
    changed = intent("paper-2")
    for leg in changed["legs"]:
        leg["bid_size"] = leg["ask_size"] = 5
        leg["source"] = "renamed-source"
        leg["sequence"] += 100
    assert "報價事件內容衝突" in mock.submit(changed)["reason"]


def test_fill_api_cannot_accept_caller_prices_and_quote_depth_stays_consumed():
    mock = broker(RiskLimits(5000, 10000, 10))
    first = intent()
    for leg in first["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
    assert mock.submit(first)["status"] == "pending"
    with pytest.raises(TypeError):
        mock.fill_all("paper-1", "fill-1", ASOF, [0, 100000, 0, 100000])
    mock.fill_all("paper-1", "fill-1", ASOF)
    repeated = intent("paper-2")
    for leg in repeated["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
    assert "口數已被" in mock.submit(repeated)["reason"]
    for leg in repeated["legs"]:
        leg["sequence"] += 10
        leg["timestamp"] = leg["received_at"] = "2026-01-05T10:00:01+08:00"
    repeated["asof"] = "2026-01-05T10:00:01+08:00"
    assert mock.submit(repeated)["status"] == "pending"
    with pytest.raises(ValueError, match="已被其他訂單使用"):
        mock.fill_all("paper-2", "fill-1", "2026-01-05T10:00:01+08:00")


def test_stale_fill_expires_pending_order_and_releases_reservations():
    mock = broker()
    mock.submit(intent())
    with pytest.raises(ValueError, match="報價有效期限"):
        mock.fill_all("paper-1", "late-fill", "2026-01-05T10:00:06+08:00")
    assert mock.status("paper-1")["status"] == "expired"
    assert "早於 paper 模擬時鐘" in mock.submit(intent("paper-2"))["reason"]
    newer = intent("paper-3")
    newer["asof"] = "2026-01-05T10:00:06+08:00"
    for leg in newer["legs"]:
        leg["timestamp"] = leg["received_at"] = newer["asof"]
        leg["sequence"] += 10
    assert mock.submit(newer)["status"] == "pending"


def test_new_quote_event_expires_old_pending_before_contract_limit_check():
    mock = broker(RiskLimits(10000, 10000, 4))
    assert mock.submit(intent("old"))["status"] == "pending"
    rejected = mock.submit({
        "client_order_id": "invalid", "mode": "live", "asof": "2026-01-05T10:00:06+08:00",
    })
    assert rejected["status"] == "rejected"
    assert mock.status("old")["status"] == "pending"
    newer = intent("new")
    newer["asof"] = "2026-01-05T10:00:06+08:00"
    for leg in newer["legs"]:
        leg["timestamp"] = leg["received_at"] = newer["asof"]
        leg["sequence"] += 100
    assert mock.submit(newer)["status"] == "pending"
    assert mock.status("old")["status"] == "expired"


def test_expiry_sweep_releases_partially_overlapping_quote_depth():
    mock = broker(RiskLimits(10000, 10000, 4))
    first = intent("old")
    first["legs"][0]["timestamp"] = first["legs"][0]["received_at"] = "2026-01-05T09:59:59.100000+08:00"
    for leg in first["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
    assert mock.submit(first)["status"] == "pending"

    second = intent("new")
    second["asof"] = "2026-01-05T10:00:04.500000+08:00"
    for leg in second["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
    second["legs"][0]["sequence"] += 100
    assert mock.submit(second)["status"] == "pending"
    assert mock.status("old")["status"] == "expired"


def test_pending_fill_at_or_after_settlement_expires_and_releases_risk():
    limits = RiskLimits(10000, 10000, 4)
    costs = PaperCosts(dt.date(2026, 2, 1), 1, 0.001, 1, 0.00002, 50000)
    mock = broker(limits, costs)
    late = intent("late")
    late["asof"] = "2026-02-20T13:29:59+08:00"
    for leg in late["legs"]:
        leg["timestamp"] = leg["received_at"] = late["asof"]
    assert mock.submit(late)["status"] == "pending"
    with pytest.raises(ValueError, match="到期結算時點"):
        mock.fill_all("late", "late-fill", "2026-02-20T13:30:01+08:00")
    assert mock.status("late")["status"] == "expired"
    assert mock._reserved_contracts == 0 and mock._reserved_risk == 0


def test_quote_depth_identity_ignores_receive_time_and_session_alias():
    mock = broker(RiskLimits(5000, 10000, 10))
    first = intent("first")
    for leg in first["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
    assert mock.submit(first)["status"] == "pending"
    mock.fill_all("first", "fill-first", ASOF)
    repeated = intent("repeat")
    repeated["asof"] = "2026-01-05T10:00:00.100000+08:00"
    for leg in repeated["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
        leg["received_at"] = repeated["asof"]
        leg["sequence"] += 100
        leg["session"] = "一般"
    result = mock.submit(repeated)
    assert result["status"] == "rejected"
    assert "口數已被" in result["reason"]
    cross_session = intent("cross-session")
    cross_session["asof"] = "2026-01-05T10:00:00.200000+08:00"
    for leg in cross_session["legs"]:
        leg["bid_size"] = leg["ask_size"] = 1
        leg["received_at"] = cross_session["asof"]
        leg["sequence"] += 200
        leg["session"] = "night"
    result = mock.submit(cross_session)
    assert result["status"] == "rejected"
    assert "報價事件內容衝突" in result["reason"]


def test_rejected_sequence_does_not_poison_later_valid_quote():
    mock = broker(RiskLimits(5000, 10000, 12))
    assert mock.submit(intent("first"))["status"] == "pending"
    rejected = intent("rejected")
    rejected["asof"] = "2026-01-05T10:00:01+08:00"
    for leg in rejected["legs"]:
        leg["timestamp"] = leg["received_at"] = rejected["asof"]
        leg["bid"] += 0.5
        leg["ask"] += 0.5
        leg["limit_price"] = leg["ask"] if leg["side"] == "BUY" else leg["bid"]
    assert "sequence" in mock.submit(rejected)["reason"]
    valid = intent("valid")
    valid["asof"] = rejected["asof"]
    for leg in valid["legs"]:
        leg["timestamp"] = leg["received_at"] = valid["asof"]
        leg["bid"] += 0.5
        leg["ask"] += 0.5
        leg["limit_price"] = leg["ask"] if leg["side"] == "BUY" else leg["bid"]
        leg["sequence"] += 100
    assert mock.submit(valid)["status"] == "pending"


def test_registered_settlement_can_close_same_expiry_after_clock_advances():
    costs = PaperCosts(dt.date(2026, 2, 1), 1, 0.001, 1, 0.00002, 50000)
    mock = broker(RiskLimits(10000, 10000, 20), costs)
    first, second = intent("first"), intent("second")
    first["asof"] = "2026-02-01T10:00:00+08:00"
    second["asof"] = "2026-02-01T10:00:01+08:00"
    for leg in first["legs"]:
        leg["timestamp"] = leg["received_at"] = first["asof"]
    for leg in second["legs"]:
        leg["timestamp"] = leg["received_at"] = second["asof"]
        leg["sequence"] += 10
    mock.submit(first)
    mock.fill_all("first", "fill-first", first["asof"])
    mock.submit(second)
    mock.fill_all("second", "fill-second", second["asof"])
    mock.close("first", 30000, SETTLEMENT_AT, "official-fixture")

    later = intent("later")
    later["asof"] = "2026-02-20T14:00:00+08:00"
    for leg in later["legs"]:
        leg.update(expiry="2026-03-18", settlement_id="TAIFEX_TAIEX_LAST30M:2026-03-18",
                   settlement_at="2026-03-18T13:30:00+08:00", timestamp=later["asof"],
                   received_at=later["asof"], sequence=leg["sequence"] + 100)
    assert mock.submit(later)["status"] == "pending"
    mock.fill_all("later", "fill-later", later["asof"])
    closed = mock.close("second", 30000, SETTLEMENT_AT, "official-fixture")
    assert closed["status"] == "closed"


def test_late_settlement_event_keeps_clock_monotonic_and_uses_expiry_day_pnl():
    costs = PaperCosts(dt.date(2026, 2, 1), 1, 0.001, 1, 0.00002, 50000)
    mock = broker(RiskLimits(10000, 10000, 12), costs)
    first = intent("first")
    first["asof"] = "2026-02-01T10:00:00+08:00"
    for leg in first["legs"]:
        leg["timestamp"] = leg["received_at"] = first["asof"]
    mock.submit(first)
    mock.fill_all("first", "fill-first", first["asof"])

    later = intent("later")
    later["asof"] = "2026-02-20T14:00:00+08:00"
    for leg in later["legs"]:
        leg.update(expiry="2026-03-18", settlement_id="TAIFEX_TAIEX_LAST30M:2026-03-18",
                   settlement_at="2026-03-18T13:30:00+08:00", timestamp=later["asof"],
                   received_at=later["asof"], sequence=leg["sequence"] + 100)
    assert mock.submit(later)["status"] == "pending"
    mock.fill_all("later", "fill-later", later["asof"])
    clock_before_close = mock._latest_event_time

    closed = mock.close("first", 30000, SETTLEMENT_AT, "late-official-result")
    assert closed["status"] == "closed"
    assert mock._latest_event_time == clock_before_close
    assert mock._daily_pnl[dt.date(2026, 2, 20)] == pytest.approx(-818)


def test_broker_rejects_stale_quote_and_after_expiry_intents():
    order = intent()
    order["legs"][0]["timestamp"] = "2026-01-05T09:00:00+08:00"
    assert "過期" in broker().submit(order)["reason"]
    after_expiry = intent()
    after_expiry["asof"] = SETTLEMENT_AT
    for leg in after_expiry["legs"]:
        leg["timestamp"] = leg["received_at"] = SETTLEMENT_AT
    costs = PaperCosts(dt.date(2026, 2, 1), 1, 0.001, 1, 0.00002, 50000)
    assert "到期後" in broker(costs=costs).submit(after_expiry)["reason"]


def test_close_requires_post_expiry_settlement_within_configured_range():
    mock = broker()
    mock.submit(intent())
    mock.fill_all("paper-1", "fill-1", ASOF)
    with pytest.raises(ValueError, match="早於合約到期"):
        mock.close("paper-1", 30000, "2026-02-20T13:29:59+08:00", "fixture")
    with pytest.raises(ValueError, match="超出設定壓力範圍"):
        mock.close("paper-1", 50001, SETTLEMENT_AT, "fixture")
    assert mock.status("paper-1")["status"] == "filled"
    mock.close("paper-1", 30000, SETTLEMENT_AT, "fixture")


def test_same_expiry_cannot_accept_conflicting_settlement_events():
    mock = broker(RiskLimits(5000, 10000, 10))
    first = intent()
    second = intent("paper-2")
    second["asof"] = "2026-01-05T10:00:01+08:00"
    for leg in second["legs"]:
        leg["timestamp"] = leg["received_at"] = second["asof"]
        leg["sequence"] += 10
    mock.submit(first)
    mock.fill_all("paper-1", "fill-1", ASOF)
    mock.submit(second)
    mock.fill_all("paper-2", "fill-2", second["asof"])
    mock.close("paper-1", 30000, SETTLEMENT_AT, "official-fixture")
    with pytest.raises(ValueError, match="結算識別碼"):
        mock.close("paper-2", 1, SETTLEMENT_AT, "official-fixture")
    assert mock.status("paper-2")["status"] == "filled"


def test_zero_stress_loss_basket_is_allowed():
    order = intent()
    prices = [(1, 2), (10, 11), (10, 11), (1, 2)]
    for leg, (bid, ask) in zip(order["legs"], prices):
        leg["bid"], leg["ask"] = bid, ask
        leg["limit_price"] = ask if leg["side"] == "BUY" else bid
    submitted = broker(RiskLimits(100, 1000, 4)).submit(order)
    assert submitted["status"] == "pending"
    assert submitted["computed_stress_loss_twd"] == 0


def test_date_scoped_kill_switch_keeps_other_day_pending_orders():
    mock = broker(RiskLimits(5000, 10000, 12))
    yesterday, today = intent("yesterday"), intent("today")
    yesterday["asof"] = "2026-01-04T23:59:59+08:00"
    for leg in yesterday["legs"]:
        leg["timestamp"] = leg["received_at"] = yesterday["asof"]
        leg["sequence"] += 100
    today["asof"] = "2026-01-05T00:00:00+08:00"
    for leg in today["legs"]:
        leg["timestamp"] = leg["received_at"] = today["asof"]
    mock.submit(yesterday)
    mock.submit(today)
    mock.trip_kill_switch("test", dt.date(2026, 1, 4))
    assert mock.status("yesterday")["status"] == "cancelled"
    assert mock.status("today")["status"] == "pending"


def test_performance_metrics_handles_zero_variance_and_insolvency():
    assert performance_metrics([0, 0], 100000)["sharpe_unavailable_reason"] == "報酬標準差為零"
    insolvent = performance_metrics([10, 10, -200, 100, 100, 10], 100)
    assert insolvent["sharpe_annualized"] is None
    assert insolvent["sharpe_unavailable_reason"] == "資金曲線曾歸零或為負"
    with pytest.raises(ValueError, match="正的有限金額"):
        performance_metrics([1], 0)
