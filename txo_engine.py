"""Offline TXO pricing and static-arbitrage diagnostics.

Signals from this module are research candidates only. The T1 SQLite source is
daily data and does not contain synchronized, sized, timestamped quotes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from market_time import require_txo_expiry, validate_txo_session

MULTIPLIER = 50
RIGHTS = {"C": "C", "CALL": "C", "買權": "C", "P": "P", "PUT": "P", "賣權": "P"}
SESSION_ALIASES = {"day": "day", "一般": "day", "night": "night", "盤後": "night"}
SESSIONS = set(SESSION_ALIASES)
TAIPEI = dt.timezone(dt.timedelta(hours=8), "Asia/Taipei")
MAX_QUOTE_AGE_SECONDS = 5.0
MAX_QUOTE_SKEW_SECONDS = 1.0
MAX_COST_AGE_DAYS = 30
SETTLEMENT_METHOD = "TAIFEX_TAIEX_LAST30M"


def _finite(name: str, value, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必須是有限數值")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} 必須是有限數值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} 必須是有限數值")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} 不可小於 {minimum}")
    return result


def _positive_rounded_edge(value: float) -> float | None:
    if not math.isfinite(value):
        return None
    rounded = round(value, 4)
    return rounded if rounded > 0 else None


def _aware(value: str, name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必須是含時區的 ISO 8601 時間")
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} 不是有效的 ISO 8601 時間") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} 必須包含時區")
    return result.astimezone(dt.timezone.utc)


def _right(value: str) -> str:
    try:
        return RIGHTS[value.upper() if value.isascii() else value]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"買賣權別無效：{value!r}") from exc


def discount_factor(rate: float, years: float) -> float:
    rate = _finite("rate", rate)
    years = _finite("years", years, minimum=0)
    if abs(rate) > 1:
        raise ValueError("年化利率超出安全範圍 ±100%")
    try:
        result = math.exp(-rate * years)
    except OverflowError as exc:
        raise ValueError("折現因子超出可計算範圍") from exc
    if not math.isfinite(result):
        raise ValueError("折現因子不是有限數值")
    return result


def option_tick(price: float) -> float:
    price = _finite("option price", price, minimum=0)
    if price < 10:
        return 0.1
    if price < 50:
        return 0.5
    if price < 500:
        return 1.0
    if price < 1000:
        return 5.0
    return 10.0


def option_price_on_tick(price: float) -> bool:
    price = _finite("option price", price, minimum=0)
    tick = option_tick(price)
    return math.isclose(price / tick, round(price / tick), rel_tol=0, abs_tol=1e-8)


def round_twd(amount: float) -> int:
    """TAIFEX transaction-tax amount is rounded to whole TWD per contract."""
    amount = _finite("TWD amount", amount, minimum=0)
    return math.floor(amount + 0.5)


def black76(forward: float, strike: float, years: float, rate: float, volatility: float, right: str) -> float:
    """Black-76 European option value in index points."""
    forward = _finite("forward", forward, minimum=0)
    strike = _finite("strike", strike, minimum=0)
    years = _finite("years", years, minimum=0)
    volatility = _finite("volatility", volatility, minimum=0)
    right = _right(right)
    discount = discount_factor(rate, years)
    if years == 0 or volatility == 0:
        intrinsic = max(forward - strike, 0) if right == "C" else max(strike - forward, 0)
        return discount * intrinsic
    if forward == 0 or strike == 0:
        return discount * (forward if right == "C" else strike)
    sigma_root_t = volatility * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * volatility * volatility * years) / sigma_root_t
    d2 = d1 - sigma_root_t
    cdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    if right == "C":
        return discount * (forward * cdf(d1) - strike * cdf(d2))
    return discount * (strike * cdf(-d2) - forward * cdf(-d1))


def implied_volatility(price: float, forward: float, strike: float, years: float, rate: float, right: str) -> float:
    """Invert Black-76 with price-bound checks; rejects numerically unidentifiable cases."""
    price = _finite("price", price, minimum=0)
    forward = _finite("forward", forward, minimum=0)
    strike = _finite("strike", strike, minimum=0)
    years = _finite("years", years, minimum=0)
    right = _right(right)
    if years == 0:
        raise ValueError("到期時無法由權利金識別隱含波動率")
    discount = discount_factor(rate, years)
    lower = discount * (max(forward - strike, 0) if right == "C" else max(strike - forward, 0))
    upper = discount * (forward if right == "C" else strike)
    tolerance = 1e-10 * max(1.0, upper)
    if price < lower - tolerance or price >= upper - tolerance:
        raise ValueError("權利金落在 Black-76 價格界外或無法識別波動率")
    if price <= lower + tolerance:
        raise ValueError("權利金接近理論下界，隱含波動率不可識別")
    low, high = 1e-10, 8.0
    if black76(forward, strike, years, rate, high, right) < price:
        raise ValueError("隱含波動率超出求解範圍")
    for _ in range(100):
        mid = (low + high) / 2
        if black76(forward, strike, years, rate, mid, right) < price:
            low = mid
        else:
            high = mid
    if high - low > 1e-8:
        raise ValueError("隱含波動率求解未收斂")
    return (low + high) / 2


@dataclass(frozen=True)
class Quote:
    expiry: dt.date
    strike: float
    right: str
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    timestamp: dt.datetime
    received_at: dt.datetime
    sequence: int
    source: str
    session: str
    multiplier: int = MULTIPLIER

    @classmethod
    def from_dict(cls, row: dict) -> "Quote":
        if not isinstance(row, dict):
            raise ValueError("quote 必須是物件")
        try:
            expiry = dt.date.fromisoformat(row["expiry"])
            right = _right(row["right"])
            bid = _finite("bid", row["bid"], minimum=0)
            ask = _finite("ask", row["ask"], minimum=0)
            bid_size = row["bid_size"]
            ask_size = row["ask_size"]
            sequence = row["sequence"]
            multiplier = row.get("multiplier", MULTIPLIER)
        except (KeyError, TypeError) as exc:
            raise ValueError(f"quote 欄位不完整：{exc}") from exc
        if not isinstance(bid_size, int) or isinstance(bid_size, bool) or bid_size <= 0:
            raise ValueError("bid_size 必須是正整數")
        if not isinstance(ask_size, int) or isinstance(ask_size, bool) or ask_size <= 0:
            raise ValueError("ask_size 必須是正整數")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("sequence 必須是非負整數")
        if multiplier != MULTIPLIER:
            raise ValueError(f"TXO 對應指數乘數必須為 {MULTIPLIER}")
        if bid > ask:
            raise ValueError("bid 大於 ask")
        if ask == 0:
            raise ValueError("ask 必須大於零")
        if not option_price_on_tick(bid) or not option_price_on_tick(ask):
            raise ValueError("option bid/ask 不符合 TAIFEX 權利金跳動單位")
        source, session = row.get("source"), row.get("session")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("quote 缺少 source")
        if not isinstance(session, str) or session not in SESSIONS:
            raise ValueError(f"交易時段無效：{session!r}")
        session = SESSION_ALIASES[session]
        strike = _finite("strike", row.get("strike"), minimum=0.0000001)
        strike_step = 25 if strike < 3000 else 50
        if int(strike) != strike or int(strike) % strike_step:
            raise ValueError(f"履約價須符合 TAIFEX 最小間距 {strike_step} 點")
        return cls(
            expiry, strike, right, bid, ask,
            bid_size, ask_size, _aware(row.get("timestamp"), "timestamp"),
            _aware(row.get("received_at"), "received_at"), sequence, source, session, multiplier,
        )

    def key(self) -> tuple[dt.date, float, str]:
        return self.expiry, self.strike, self.right


@dataclass(frozen=True)
class ForwardQuote:
    expiry: dt.date
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    timestamp: dt.datetime
    received_at: dt.datetime
    sequence: int
    source: str
    session: str
    settlement_id: str
    multiplier: int

    @classmethod
    def from_dict(cls, row: dict) -> "ForwardQuote":
        if not isinstance(row, dict):
            raise ValueError("forward 必須是物件")
        try:
            expiry = dt.date.fromisoformat(row["expiry"])
            bid = _finite("forward bid", row["bid"], minimum=0)
            ask = _finite("forward ask", row["ask"], minimum=0)
            bid_size, ask_size = row["bid_size"], row["ask_size"]
            sequence, multiplier = row["sequence"], row["multiplier"]
            settlement_id, source, session = row["settlement_id"], row["source"], row["session"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"forward 欄位不完整：{exc}") from exc
        if not all(isinstance(size, int) and not isinstance(size, bool) and size > 0 for size in (bid_size, ask_size)):
            raise ValueError("forward bid_size/ask_size 必須是正整數")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("forward sequence 必須是非負整數")
        if multiplier != MULTIPLIER:
            raise ValueError(f"遠期乘數須與單口 TXO 相同，預期 {MULTIPLIER}")
        if bid > ask:
            raise ValueError("forward bid 大於 ask")
        if ask == 0:
            raise ValueError("forward ask 必須大於零")
        if int(bid) != bid or int(ask) != ask:
            raise ValueError("臺股期貨買賣價須符合指數 1 點跳動單位")
        if not isinstance(settlement_id, str) or not settlement_id.strip():
            raise ValueError("forward 缺少 settlement_id")
        if not isinstance(source, str) or not source.strip() or not isinstance(session, str) or session not in SESSIONS:
            raise ValueError("forward 缺少 source 或交易時段無效")
        session = SESSION_ALIASES[session]
        return cls(
            expiry, bid, ask, bid_size, ask_size, _aware(row.get("timestamp"), "forward timestamp"),
            _aware(row.get("received_at"), "forward received_at"), sequence, source, session,
            settlement_id, multiplier,
        )


def _validate_time(quote, asof: dt.datetime, max_age: float) -> None:
    for label, stamp in (("timestamp", quote.timestamp), ("received_at", quote.received_at)):
        age = (asof - stamp).total_seconds()
        if age < 0:
            raise ValueError(f"{label} 晚於 snapshot asof")
        if age > max_age:
            raise ValueError(f"{label} 過期 {age:.3f} 秒")
    if quote.received_at < quote.timestamp:
        raise ValueError("received_at 早於交易所 timestamp")


def _synchronous(quotes: list[Quote | ForwardQuote], max_skew: float) -> bool:
    if len({q.session for q in quotes}) != 1:
        return False
    stamps = [q.timestamp for q in quotes]
    received = [q.received_at for q in quotes]
    return (max(stamps) - min(stamps)).total_seconds() <= max_skew and (max(received) - min(received)).total_seconds() <= max_skew


def _has_size(legs: list[tuple[Quote | ForwardQuote, str, int]]) -> bool:
    return all((quote.ask_size if side == "BUY" else quote.bid_size) >= quantity for quote, side, quantity in legs)


def _cost_model(snapshot: dict, asof: dt.datetime) -> dict:
    costs = snapshot.get("costs")
    if not isinstance(costs, dict):
        raise ValueError("snapshot 必須提供明確的 costs 設定")
    required = (
        "effective_date", "option_exchange_fee_twd", "option_broker_fee_twd", "option_tax_rate",
        "option_settlement_fee_twd", "option_settlement_tax_reserve_twd",
        "forward_exchange_fee_twd", "forward_broker_fee_twd", "forward_tax_rate",
        "forward_settlement_fee_twd", "forward_settlement_tax_reserve_twd",
    )
    if any(key not in costs for key in required):
        raise ValueError(f"costs 缺少欄位：{', '.join(k for k in required if k not in costs)}")
    try:
        effective_date = dt.date.fromisoformat(costs["effective_date"])
    except (TypeError, ValueError) as exc:
        raise ValueError("costs.effective_date 必須是日期") from exc
    cost_age = (asof.astimezone(TAIPEI).date() - effective_date).days
    if cost_age < 0:
        raise ValueError("費率生效日晚於 snapshot asof")
    if cost_age > MAX_COST_AGE_DAYS:
        raise ValueError(f"費率資料已過期 {cost_age} 天，需重新核對官方資料")
    result = {key: _finite(key, costs[key], minimum=0) for key in required if key != "effective_date"}
    if result["option_tax_rate"] > 1 or result["forward_tax_rate"] > 1:
        raise ValueError("option_tax_rate 與 forward_tax_rate 應以 0 至 1 的比率表示")
    result["effective_date"] = costs["effective_date"]
    result["age_days"] = cost_age
    return result


def _option_trade_cost(quote: Quote, side: str, costs: dict, quantity: int = 1) -> tuple[float, float]:
    premium = quote.ask if side == "BUY" else quote.bid
    fee = (costs["option_exchange_fee_twd"] + costs["option_broker_fee_twd"]) * quantity
    tax = round_twd(premium * quote.multiplier * costs["option_tax_rate"]) * quantity
    settlement = (costs["option_settlement_fee_twd"] + costs["option_settlement_tax_reserve_twd"]) * quantity
    return premium * quote.multiplier * quantity, fee + tax + settlement


def _forward_fee(costs: dict, quote: ForwardQuote, side: str) -> float:
    price = quote.ask if side == "BUY" else quote.bid
    tax = round_twd(price * quote.multiplier * costs["forward_tax_rate"])
    return (
        costs["forward_exchange_fee_twd"] + costs["forward_broker_fee_twd"] + tax
        + costs["forward_settlement_fee_twd"] + costs["forward_settlement_tax_reserve_twd"]
    )


def _valuation(snapshot: dict, expiry: dt.date, asof: dt.datetime) -> tuple[float, float, float, float, float, str]:
    require_txo_expiry(expiry)
    try:
        item = snapshot["expiries"][expiry.isoformat()]
        settlement_at = _aware(item["settlement_at"], "settlement_at")
        pricing_rate = _finite("pricing_rate", item["pricing_rate"])
        borrowing_rate = _finite("borrowing_rate", item["borrowing_rate"], minimum=0)
        investment_rate = _finite("investment_rate", item["investment_rate"], minimum=0)
        settlement_method = item["settlement_method"]
        settlement_id = item["settlement_id"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"expiry {expiry} 缺少 settlement_at、利率或結算識別資料") from exc
    local_settlement = settlement_at.astimezone(TAIPEI)
    if local_settlement.date() != expiry or local_settlement.time().replace(tzinfo=None) != dt.time(13, 30):
        raise ValueError(f"expiry {expiry} 的結算時點須為該日臺北時間 13:30")
    if settlement_method != SETTLEMENT_METHOD or settlement_id != f"{SETTLEMENT_METHOD}:{expiry.isoformat()}":
        raise ValueError(f"expiry {expiry} 的結算方法或識別碼與 TXO 官方結算規格不符")
    if borrowing_rate < investment_rate:
        raise ValueError("borrowing_rate 不可低於 investment_rate；兩者目前皆須非負")
    years = (settlement_at - asof).total_seconds() / (365 * 24 * 60 * 60)
    if years <= 0:
        raise ValueError(f"expiry {expiry} 已到期或 settlement_at 不晚於 asof")
    return (
        years, discount_factor(pricing_rate, years), discount_factor(borrowing_rate, years),
        discount_factor(investment_rate, years), pricing_rate, settlement_id,
    )


def _analyze_snapshot(snapshot: dict) -> dict:
    """Analyze one timestamped, sized snapshot. No order is ever sent."""
    try:
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
            raise ValueError("snapshot schema_version 必須為 1")
        asof = _aware(snapshot.get("asof"), "asof")
        max_age, max_skew = MAX_QUOTE_AGE_SECONDS, MAX_QUOTE_SKEW_SECONDS
        costs = _cost_model(snapshot, asof)
        options = [Quote.from_dict(row) for row in snapshot.get("options", [])]
        forwards = [ForwardQuote.from_dict(row) for row in snapshot.get("forwards", [])]
        if not options:
            raise ValueError("snapshot 沒有 options quote")
        option_map = {}
        for quote in options:
            if quote.key() in option_map:
                raise ValueError(f"重複 option quote：{quote.key()}")
            _validate_time(quote, asof, max_age)
            quote_day = validate_txo_session(quote.timestamp, quote.session, quote.expiry)
            if validate_txo_session(asof, quote.session, quote.expiry) != quote_day:
                raise ValueError("snapshot asof 與 option 報價不在同一 TXO 交易時段")
            _valuation(snapshot, quote.expiry, asof)
            option_map[quote.key()] = quote
        forward_map = {}
        for quote in forwards:
            if quote.expiry in forward_map:
                raise ValueError(f"重複 forward quote：{quote.expiry}")
            _validate_time(quote, asof, max_age)
            quote_day = validate_txo_session(quote.timestamp, quote.session, quote.expiry)
            if validate_txo_session(asof, quote.session, quote.expiry) != quote_day:
                raise ValueError("snapshot asof 與 forward 報價不在同一 TXO 交易時段")
            forward_map[quote.expiry] = quote
    except (TypeError, ValueError, KeyError) as exc:
        return {"status": "rejected", "mode": "offline_research_only", "error": str(exc), "signals": []}

    by_expiry_right: dict[tuple[dt.date, str], list[Quote]] = defaultdict(list)
    for quote in options:
        by_expiry_right[(quote.expiry, quote.right)].append(quote)
    for group in by_expiry_right.values():
        group.sort(key=lambda q: q.strike)

    parity, boxes, verticals, butterflies, calendars = [], [], [], [], []

    for (expiry, strike, _), call in option_map.items():
        if call.right != "C":
            continue
        put = option_map.get((expiry, strike, "P"))
        forward = forward_map.get(expiry)
        if not put or not forward:
            continue
        years, discount, borrow_discount, invest_discount, pricing_rate, settlement_id = _valuation(snapshot, expiry, asof)
        sync = [call, put, forward]
        if not _synchronous(sync, max_skew) or forward.settlement_id != settlement_id:
            continue
        if not _has_size([(call, "BUY", 1), (put, "SELL", 1), (forward, "SELL", 1)]):
            continue
        call_ask, call_buy_cost = _option_trade_cost(call, "BUY", costs)
        put_bid, put_sell_cost = _option_trade_cost(put, "SELL", costs)
        cost = call_buy_cost + put_sell_cost + _forward_fee(costs, forward, "SELL")
        locked_cashflow = (forward.bid - strike) * MULTIPLIER
        cash_discount = borrow_discount if locked_cashflow >= 0 else invest_discount
        edge = cash_discount * locked_cashflow - (call_ask - put_bid) - cost
        edge_twd = _positive_rounded_edge(edge)
        if edge_twd is not None:
            parity.append({
                "strategy": "put_call_parity_long_synthetic_short_forward", "expiry": expiry.isoformat(),
                "strike": strike, "net_edge_twd": edge_twd, "settlement_id": settlement_id,
                "legs": ["BUY CALL", "SELL PUT", "SELL matched forward"],
                "risk_note": "期貨每日盯市、保證金與腿風險未由此快照模型計入。",
            })

        call_bid, call_sell_cost = _option_trade_cost(call, "SELL", costs)
        put_ask, put_buy_cost = _option_trade_cost(put, "BUY", costs)
        cost = call_sell_cost + put_buy_cost + _forward_fee(costs, forward, "BUY")
        if not _has_size([(call, "SELL", 1), (put, "BUY", 1), (forward, "BUY", 1)]):
            continue
        locked_cashflow = (strike - forward.ask) * MULTIPLIER
        cash_discount = borrow_discount if locked_cashflow >= 0 else invest_discount
        edge = (call_bid - put_ask) + cash_discount * locked_cashflow - cost
        edge_twd = _positive_rounded_edge(edge)
        if edge_twd is not None:
            parity.append({
                "strategy": "put_call_parity_short_synthetic_long_forward", "expiry": expiry.isoformat(),
                "strike": strike, "net_edge_twd": edge_twd, "settlement_id": settlement_id,
                "legs": ["SELL CALL", "BUY PUT", "BUY matched forward"],
                "risk_note": "期貨每日盯市、保證金與腿風險未由此快照模型計入。",
            })

    for expiry in sorted({q.expiry for q in options}):
        years, discount, borrow_discount, invest_discount, pricing_rate, _ = _valuation(snapshot, expiry, asof)
        calls = {q.strike: q for q in by_expiry_right[(expiry, "C")]}
        puts = {q.strike: q for q in by_expiry_right[(expiry, "P")]}
        for strikes in itertools.combinations(sorted(set(calls) & set(puts)), 2):
            k1, k2 = strikes
            c1, c2, p1, p2 = calls[k1], calls[k2], puts[k1], puts[k2]
            legs = [c1, c2, p1, p2]
            if not _synchronous(legs, max_skew):
                continue
            debit = c1.ask - c2.bid - p1.bid + p2.ask
            gross_cost = debit * MULTIPLIER
            long_legs = [(c1, "BUY", 1), (c2, "SELL", 1), (p1, "SELL", 1), (p2, "BUY", 1)]
            if _has_size(long_legs):
                cost = sum(_option_trade_cost(q, side, costs, qty)[1] for q, side, qty in long_legs)
                edge = borrow_discount * (k2 - k1) * MULTIPLIER - gross_cost - cost
            else:
                edge = 0
            edge_twd = _positive_rounded_edge(edge)
            if edge_twd is not None:
                boxes.append({
                    "strategy": "long_box", "expiry": expiry.isoformat(), "strikes": [k1, k2],
                    "net_edge_twd": edge_twd, "present_value_payoff_twd": round(borrow_discount * (k2 - k1) * MULTIPLIER, 4),
                    "entry_debit_twd": round(gross_cost, 4), "legs": ["BUY C1", "SELL C2", "SELL P1", "BUY P2"],
                })
            credit = (c1.bid - c2.ask - p1.ask + p2.bid) * MULTIPLIER
            short_legs = [(c1, "SELL", 1), (c2, "BUY", 1), (p1, "BUY", 1), (p2, "SELL", 1)]
            if _has_size(short_legs):
                cost = sum(_option_trade_cost(q, side, costs, qty)[1] for q, side, qty in short_legs)
                edge = credit - invest_discount * (k2 - k1) * MULTIPLIER - cost
            else:
                edge = 0
            edge_twd = _positive_rounded_edge(edge)
            if edge_twd is not None:
                boxes.append({
                    "strategy": "short_box", "expiry": expiry.isoformat(), "strikes": [k1, k2],
                    "net_edge_twd": edge_twd, "present_value_payoff_twd": round(invest_discount * (k2 - k1) * MULTIPLIER, 4),
                    "entry_credit_twd": round(credit, 4), "legs": ["SELL C1", "BUY C2", "BUY P1", "SELL P2"],
                })

        for right, group in (("C", by_expiry_right[(expiry, "C")]), ("P", by_expiry_right[(expiry, "P")])):
            for low, high in itertools.combinations(group, 2):
                if not _synchronous([low, high], max_skew):
                    continue
                width_pv = invest_discount * (high.strike - low.strike) * MULTIPLIER
                if right == "C":
                    checks = (
                        (high.bid - low.ask, "call_monotonicity", [(low, "BUY", 1), (high, "SELL", 1)], ["BUY lower call", "SELL higher call"]),
                        (low.bid - high.ask - invest_discount * (high.strike - low.strike), "call_vertical_upper_bound", [(low, "SELL", 1), (high, "BUY", 1)], ["SELL lower call", "BUY higher call"]),
                    )
                else:
                    checks = (
                        (low.bid - high.ask, "put_monotonicity", [(high, "BUY", 1), (low, "SELL", 1)], ["BUY higher put", "SELL lower put"]),
                        (high.bid - low.ask - invest_discount * (high.strike - low.strike), "put_vertical_upper_bound", [(high, "SELL", 1), (low, "BUY", 1)], ["SELL higher put", "BUY lower put"]),
                    )
                for raw_edge_points, name, priced_legs, leg_names in checks:
                    if not _has_size(priced_legs):
                        continue
                    friction = sum(_option_trade_cost(q, side, costs, qty)[1] for q, side, qty in priced_legs)
                    edge = raw_edge_points * MULTIPLIER - friction
                    edge_twd = _positive_rounded_edge(edge)
                    if edge_twd is not None:
                        verticals.append({
                            "strategy": name, "expiry": expiry.isoformat(), "right": right,
                            "strikes": [low.strike, high.strike], "net_edge_twd": edge_twd, "legs": leg_names,
                            "vertical_width_pv_twd": round(width_pv, 4),
                        })

        # Integer-ratio butterflies preserve the non-negative payoff for non-uniform strike gaps.
        for right, group in (("C", by_expiry_right[(expiry, "C")]), ("P", by_expiry_right[(expiry, "P")])):
            for q1, q2, q3 in itertools.combinations(group, 3):
                if not _synchronous([q1, q2, q3], max_skew):
                    continue
                d1, d2 = int(round(q2.strike - q1.strike)), int(round(q3.strike - q2.strike))
                if d1 <= 0 or d2 <= 0 or not math.isclose(q2.strike - q1.strike, d1) or not math.isclose(q3.strike - q2.strike, d2):
                    continue
                divisor = math.gcd(d1, d2)
                w1, w3 = d2 // divisor, d1 // divisor
                w2 = w1 + w3
                quantities = ((q1, "BUY", w1), (q2, "SELL", w2), (q3, "BUY", w3))
                if not _has_size(list(quantities)):
                    continue
                premium = sum((q.ask if side == "BUY" else -q.bid) * qty * MULTIPLIER for q, side, qty in quantities)
                friction = sum(_option_trade_cost(q, side, costs, qty)[1] for q, side, qty in quantities)
                edge = -premium - friction
                edge_twd = _positive_rounded_edge(edge)
                if edge_twd is not None:
                    butterflies.append({
                        "strategy": f"{right.lower()}_butterfly_negative_cost", "expiry": expiry.isoformat(),
                        "strikes": [q1.strike, q2.strike, q3.strike], "contract_weights": [w1, -w2, w3],
                        "net_edge_twd": edge_twd,
                        "legs": [f"BUY {w1} {right} {q1.strike}", f"SELL {w2} {right} {q2.strike}", f"BUY {w3} {right} {q3.strike}"],
                    })

    # Calendar spreads are reported as IV diagnostics, never as arbitrage.
    for (strike, right), expiries in _calendar_groups(options).items():
        for near, far in itertools.combinations(expiries, 2):
            near_q, far_q = near, far
            if near_q.expiry >= far_q.expiry or not _synchronous([near_q, far_q], max_skew):
                continue
            near_forward, far_forward = forward_map.get(near_q.expiry), forward_map.get(far_q.expiry)
            if not near_forward or not far_forward or not _synchronous([near_q, far_q, near_forward, far_forward], max_skew):
                continue
            _, _, _, _, _, near_settlement_id = _valuation(snapshot, near_q.expiry, asof)
            _, _, _, _, _, far_settlement_id = _valuation(snapshot, far_q.expiry, asof)
            if near_forward.settlement_id != near_settlement_id or far_forward.settlement_id != far_settlement_id:
                continue
            try:
                near_t, _, _, _, near_rate, _ = _valuation(snapshot, near_q.expiry, asof)
                far_t, _, _, _, far_rate, _ = _valuation(snapshot, far_q.expiry, asof)
                near_iv = implied_volatility((near_q.bid + near_q.ask) / 2, (near_forward.bid + near_forward.ask) / 2, strike, near_t, near_rate, right)
                far_iv = implied_volatility((far_q.bid + far_q.ask) / 2, (far_forward.bid + far_forward.ask) / 2, strike, far_t, far_rate, right)
            except (KeyError, ValueError):
                continue
            calendars.append({
                "classification": "directional_relative_value_watchlist", "strike": strike, "right": right,
                "near_expiry": near_q.expiry.isoformat(), "far_expiry": far_q.expiry.isoformat(),
                "near_iv": round(near_iv, 8), "far_iv": round(far_iv, 8), "iv_difference": round(far_iv - near_iv, 8),
                "risk_note": "跨期價差承受標的方向、波動率曲面及到期差異風險，不是無風險套利。",
            })

    return {
        "status": "analyzed", "mode": "offline_research_only", "asof": snapshot["asof"],
        "cost_model": costs, "quote_count": len(options), "forward_count": len(forwards),
        "quote_policy": {"max_age_seconds": max_age, "max_skew_seconds": max_skew, "max_cost_age_days": MAX_COST_AGE_DAYS},
        "signal_policy": "Candidates use top-of-book prices as diagnostics. This tool never sends orders; no fill probability, margin, or live execution is modeled.",
        "signals": parity + boxes + verticals + butterflies,
        "calendar_watchlist": calendars,
    }


def analyze_snapshot(snapshot: dict) -> dict:
    try:
        return _analyze_snapshot(snapshot)
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        return {"status": "rejected", "mode": "offline_research_only", "error": str(exc), "signals": []}


def _calendar_groups(options: list[Quote]) -> dict[tuple[float, str], list[Quote]]:
    result: dict[tuple[float, str], list[Quote]] = defaultdict(list)
    for quote in options:
        result[(quote.strike, quote.right)].append(quote)
    for rows in result.values():
        rows.sort(key=lambda item: item.expiry)
    return result


def box_payoff(spot: float, lower_strike: float, upper_strike: float) -> float:
    """Gross long-box payoff in index points; useful for independent self-checks."""
    if not lower_strike < upper_strike:
        raise ValueError("lower_strike 必須小於 upper_strike")
    return (
        max(spot - lower_strike, 0) - max(spot - upper_strike, 0)
        + max(upper_strike - spot, 0) - max(lower_strike - spot, 0)
    )


def database_readiness(db_path: Path) -> dict:
    """Read schema only; report whether the T1 database can support execution-grade replay."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "txo_daily" not in tables:
            raise ValueError("資料庫沒有 txo_daily")
        columns = {row[1] for row in db.execute("PRAGMA table_info(txo_daily)")}
        count, first, last = db.execute("SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM txo_daily").fetchone()
    required = {"quote_timestamp", "received_at", "sequence", "bid_size", "ask_size", "forward_bid", "forward_ask"}
    return {
        "status": "diagnostic_only", "database": str(db_path), "rows": count,
        "trade_date_range": [first, last], "available_columns": sorted(columns),
        "missing_execution_replay_fields": sorted(required - columns),
        "historical_executable_backtest_supported": False,
        "reason": "T1 stores daily OHLC and last best bid/ask without event timestamps, quote sizes, source sequence, or matched-expiry forward quotes.",
    }


def _write_json(report: dict, output: Path | None, protected: Path) -> None:
    content = json.dumps(_json_safe(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output is None:
        print(content, end="")
        return
    output = output.resolve()
    protected = protected.resolve()
    lower_name = output.name.lower()
    if lower_name.endswith(("-wal", "-shm", "-journal")) or any(
        output == Path(f"{protected}{suffix}") for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("JSON 報告不可覆寫 SQLite 資料庫 sidecar")
    if output.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("JSON 報告不可使用 SQLite 檔名")
    if output.exists():
        with output.open("rb") as stream:
            header = stream.read(16)
            if (
                header == b"SQLite format 3\x00"
                or header[:4] in {b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"}
                or header[:8] == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
            ):
                raise ValueError("JSON 報告不可覆寫 SQLite 資料庫")
    if output.exists() and protected.exists() and os.path.samefile(output, protected):
        raise ValueError("輸出路徑不可覆寫輸入檔")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"報告已寫入 {output}")


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite_float": repr(value)}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="離線 TXO 定價、套利診斷與資料能力檢查")
    sub = parser.add_subparsers(dest="command", required=True)
    readiness = sub.add_parser("readiness", help="唯讀檢查 T1 SQLite 是否可供可成交回測")
    readiness.add_argument("--db", type=Path, default=Path("data/txo.sqlite3"))
    readiness.add_argument("--output", type=Path)
    analyze = sub.add_parser("analyze", help="分析單一同步報價 JSON snapshot")
    analyze.add_argument("--snapshot", required=True, type=Path)
    analyze.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "readiness":
            report = database_readiness(args.db)
            _write_json(report, args.output, args.db)
        else:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
            report = analyze_snapshot(snapshot)
            _write_json(report, args.output, args.snapshot)
            if report["status"] == "rejected":
                return 2
        return 0
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
