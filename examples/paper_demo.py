"""Run one deterministic synthetic option basket through the paper ledger."""

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper import MockBroker, PaperCosts, RiskLimits


def main() -> None:
    asof = "2026-01-05T10:00:00+08:00"
    expiry = "2026-02-20"
    settlement_id = f"TAIFEX_TAIEX_LAST30M:{expiry}"
    rows = [("C", 100, "BUY", 24.5, 25), ("C", 125, "SELL", 5, 5.5),
            ("P", 100, "SELL", 5, 5.5), ("P", 125, "BUY", 24.5, 25)]
    legs = [{
        "instrument": "option", "right": right, "strike": strike, "expiry": expiry,
        "settlement_id": settlement_id, "settlement_at": f"{expiry}T13:30:00+08:00",
        "side": side, "quantity": 1, "limit_price": ask if side == "BUY" else bid,
        "bid": bid, "ask": ask, "bid_size": 5, "ask_size": 5,
        "timestamp": asof, "received_at": asof, "sequence": sequence,
        "source": "synthetic-fixture", "session": "day", "multiplier": 50,
    } for sequence, (right, strike, side, bid, ask) in enumerate(rows, 1)]
    intent = {"client_order_id": "demo-1", "asof": asof, "mode": "paper",
              "strategy": "box_spread", "quantity": 1, "legs": legs}
    costs = PaperCosts(dt.date(2026, 1, 1), 1, 0.001, 1, 0.00002, 50000)
    broker = MockBroker(RiskLimits(1000, 10000, 10), costs)
    submitted = broker.submit(intent)
    if submitted["status"] != "pending":
        raise RuntimeError(submitted["reason"])
    broker.fill_all("demo-1", "fill-1", asof)
    closed = broker.close("demo-1", 30000, f"{expiry}T13:30:00+08:00", "synthetic-settlement")
    print(json.dumps({"fixture": "synthetic_only", "order": closed, "audit": broker.audit_log()},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
