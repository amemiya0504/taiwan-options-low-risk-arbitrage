"""Local-only paper ledger for finite-risk, same-expiry option baskets."""

from __future__ import annotations

import copy
import datetime as dt
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from txo_engine import MAX_COST_AGE_DAYS, MAX_QUOTE_AGE_SECONDS, MAX_QUOTE_SKEW_SECONDS, MULTIPLIER, SETTLEMENT_METHOD, option_price_on_tick, round_twd

TAIPEI = dt.timezone(dt.timedelta(hours=8), "Asia/Taipei")
SESSION_NAMES = {"day": "一般", "一般": "一般", "night": "盤後", "盤後": "盤後"}


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
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
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
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
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
        self._order_depth = {}
        self._quote_events, self._quote_sequences, self._settlement_events = {}, {}, {}
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
        if strategy not in {"box_spread", "vertical_arbitrage", "butterfly"}:
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
        positions, contracts, entry_costs, settlement_fees, gross_entry_cashflow = [], 0, 0.0, 0.0, 0.0
        depth, depth_limits, contract_keys = defaultdict(int), {}, set()
        settlement_times = set()
        quote_events, quote_sequences = {}, {}
        for leg in legs:
            if not isinstance(leg, dict) or leg.get("instrument") != "option":
                return reject("MockBroker 不接受期貨、遠期或未識別商品腿")
            if leg.get("right") not in {"C", "P"} or leg.get("side") not in {"BUY", "SELL"}:
                return reject("option leg 必須標示 C/P 與 BUY/SELL")
            try:
                expiry = dt.date.fromisoformat(leg["expiry"])
                if expiry.isoformat() != leg["expiry"]:
                    return reject("expiry 必須使用 YYYY-MM-DD 格式")
                values = (leg["strike"], leg["bid"], leg["ask"], leg["limit_price"])
                if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
                    return reject("履約價及報價必須使用數值型別")
                strike, bid, ask, price = map(float, values)
                quote_time = _time(leg["timestamp"], "timestamp")
                received_at = _time(leg["received_at"], "received_at")
                settlement_utc = _time(leg["settlement_at"], "settlement_at")
                settlement = settlement_utc.astimezone(TAIPEI)
            except (KeyError, TypeError, ValueError) as exc:
                return reject(f"option leg 欄位錯誤：{exc}")
            if not all(math.isfinite(v) for v in (strike, bid, ask, price)) or strike <= 0 or bid < 0 or ask <= 0 or bid > ask:
                return reject("option leg 價格無效或買賣價交叉")
            strike_step = 25 if strike < 3000 else 50
            if int(strike) != strike or int(strike) % strike_step:
                return reject(f"履約價須符合 TAIFEX 最小間距 {strike_step} 點")
            if not option_price_on_tick(bid) or not option_price_on_tick(ask) or not option_price_on_tick(price):
                return reject("option bid/ask 不符合 TAIFEX 權利金跳動單位")
            if leg.get("multiplier") != MULTIPLIER:
                return reject("option multiplier 與 TXO 不符")
            leg_qty = leg.get("quantity")
            if not isinstance(leg_qty, int) or isinstance(leg_qty, bool) or leg_qty <= 0:
                return reject("leg quantity 必須是正整數")
            side = leg["side"]
            if (side == "BUY" and price != ask) or (side == "SELL" and price != bid):
                return reject("paper fill 必須採用買進 ask 或賣出 bid")
            size = leg.get("ask_size" if side == "BUY" else "bid_size")
            needed = leg_qty * package_qty
            if not isinstance(size, int) or isinstance(size, bool) or size < needed:
                return reject("報價口數不足以完成整組原子成交")
            if not isinstance(leg.get("source"), str) or not leg["source"].strip():
                return reject("quote 缺少 source")
            sequence = leg.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                return reject("quote sequence 必須是非負整數")
            session = leg.get("session")
            if session not in SESSION_NAMES:
                return reject("quote session 無效")
            for label, timestamp in (("timestamp", quote_time), ("received_at", received_at)):
                age = (asof - timestamp).total_seconds()
                if age < 0 or age > MAX_QUOTE_AGE_SECONDS:
                    return reject(f"{label} 過期或晚於 asof")
            if received_at < quote_time:
                return reject("received_at 早於 timestamp")
            quote_key = (expiry.isoformat(), strike, leg["right"], quote_time)
            quote_value = (bid, ask, leg.get("bid_size"), leg.get("ask_size"), SESSION_NAMES[session])
            sequence_key = (leg["source"], sequence, expiry.isoformat(), strike, leg["right"])
            sequence_event = (quote_key, quote_value)
            known_quote = quote_events.get(quote_key, self._quote_events.get(quote_key))
            known_sequence = quote_sequences.get(sequence_key, self._quote_sequences.get(sequence_key))
            if known_quote is not None and known_quote != quote_value:
                return reject("同一報價事件內容衝突")
            if known_sequence is not None and known_sequence != sequence_event:
                return reject("同一 source／sequence 不可重用於不同報價事件")
            quote_events.setdefault(quote_key, quote_value)
            quote_sequences.setdefault(sequence_key, sequence_event)
            settlement_id = f"{SETTLEMENT_METHOD}:{expiry.isoformat()}"
            if leg.get("settlement_id") != settlement_id:
                return reject("settlement_id 與 TXO 到期日／結算方法不符")
            if settlement.date() != expiry or settlement.time().replace(tzinfo=None) != dt.time(13, 30):
                return reject("settlement_at 必須是 TXO 到期日臺北時間 13:30")
            if asof >= settlement_utc:
                return reject("到期後拒絕新委託")
            contract_key = (expiry.isoformat(), strike, leg["right"])
            if contract_key in contract_keys:
                return reject("同一 option contract 不可在籃子內重複列腿")
            contract_keys.add(contract_key)
            depth_key = (quote_key, side)
            depth[depth_key] += needed
            depth_limits[depth_key] = size
            expiry_set.add(expiry)
            settlement_set.add(settlement_id)
            settlement_times.add(settlement_utc)
            sessions.add(session)
            quote_times.append(quote_time)
            receive_times.append(received_at)
            signed_qty = leg_qty if side == "BUY" else -leg_qty
            positions.append((leg["right"], strike, signed_qty, price))
            contracts += needed
            option_tax = round_twd(price * MULTIPLIER * self.costs.option_tax_rate)
            entry_costs += leg_qty * (self.costs.option_fee_twd_per_contract_side + option_tax)
            settlement_fees += leg_qty * self.costs.settlement_fee_twd_per_contract
            gross_entry_cashflow += (-1 if side == "BUY" else 1) * price * MULTIPLIER * leg_qty
        if len(expiry_set) != 1 or len(settlement_set) != 1 or len(settlement_times) != 1:
            return reject("只接受同到期、同結算方法的選擇權組合")
        if len(sessions) != 1:
            return reject("拒絕混用日盤與盤後報價")
        if (max(quote_times) - min(quote_times)).total_seconds() > MAX_QUOTE_SKEW_SECONDS or (max(receive_times) - min(receive_times)).total_seconds() > MAX_QUOTE_SKEW_SECONDS:
            return reject("quote 時間不同步")
        if not self._recognized_defined_risk(strategy, positions):
            return reject("契約腿結構不能證明有限到期損失")
        if self.costs.max_settlement_index_points <= max(strike for _, strike, _, _ in positions):
            return reject("結算壓力指數須高於所有履約價")
        stress_loss = self._stress_loss(positions, gross_entry_cashflow - entry_costs, settlement_fees, package_qty)
        if not math.isfinite(stress_loss):
            return reject("壓力損失無法安全計算")
        return (
            None, stress_loss, contracts, entry_costs * package_qty, settlement_fees * package_qty,
            dict(depth), next(iter(settlement_times)), quote_events, quote_sequences, depth_limits,
        )

    @staticmethod
    def _recognized_defined_risk(strategy: str, positions: list[tuple]) -> bool:
        combined = {}
        for right, strike, qty, _ in positions:
            combined[(right, strike)] = combined.get((right, strike), 0) + qty
        rows = [(right, strike, qty) for (right, strike), qty in combined.items() if qty]
        if strategy == "vertical_arbitrage":
            return len(rows) == 2 and rows[0][0] == rows[1][0] and rows[0][2] == -rows[1][2] and rows[0][1] != rows[1][1]
        if strategy == "box_spread":
            calls = sorted((strike, qty) for right, strike, qty in rows if right == "C")
            puts = sorted((strike, qty) for right, strike, qty in rows if right == "P")
            return (
                len(rows) == 4 and len(calls) == len(puts) == 2
                and calls[0][0] < calls[1][0] and calls[0][0] == puts[0][0] and calls[1][0] == puts[1][0]
                and calls[0][1] == -calls[1][1] and puts[0][1] == -puts[1][1]
                and calls[0][1] == -puts[0][1] and abs(calls[0][1]) == 1
            )
        if strategy == "butterfly" and len(rows) == 3 and len({right for right, _, _ in rows}) == 1:
            rows.sort(key=lambda row: row[1])
            (_, k1, q1), (_, k2, q2), (_, k3, q3) = rows
            return k1 < k2 < k3 and q1 * q3 > 0 and q2 == -q1 - q3 and q1 * (k2 - k1) == q3 * (k3 - k2)
        return False

    def _stress_loss(self, positions: list[tuple], entry_cashflow: float, settlement_fees: float, package_qty: int) -> float:
        max_index = self.costs.max_settlement_index_points
        scenarios = {0.0, max_index, *(strike for _, strike, _, _ in positions)}
        min_payout = min(
            sum(qty * (max(spot - strike, 0) if right == "C" else max(strike - spot, 0)) * MULTIPLIER
                for right, strike, qty, _ in positions)
            for spot in scenarios
        )
        tax_per_contract_upper_bound = round_twd(max_index * MULTIPLIER * self.costs.settlement_tax_rate)
        tax_upper_bound = sum(abs(qty) for _, _, qty, _ in positions) * tax_per_contract_upper_bound
        stress_pnl = (entry_cashflow + min_payout - settlement_fees - tax_upper_bound) * package_qty
        return max(0.0, -stress_pnl)

    def submit(self, intent: dict) -> dict:
        if not isinstance(intent, dict):
            return {"status": "rejected", "reason": "intent 必須是物件"}
        client_id = intent.get("client_order_id")
        if isinstance(client_id, str) and client_id in self._orders:
            order = self._orders[client_id]
            if order["intent"] != intent:
                raise ValueError("相同 client_order_id 不可重用於不同 intent")
            return copy.deepcopy(order)
        try:
            asof = _time(intent.get("asof"), "asof")
            trading_day = asof.astimezone(TAIPEI).date()
        except ValueError:
            asof, trading_day = None, None
        if asof is not None and self._latest_event_time is not None and asof < self._latest_event_time:
            reason, loss, contracts, entry_cost, settlement_fees, depth, quote_events, quote_sequences, depth_limits = "asof 早於 paper 模擬時鐘", 0, 0, 0, 0, {}, {}, {}, {}
        elif self._manual_kill_switch or trading_day in self._kill_switch_days:
            reason, loss, contracts, entry_cost, settlement_fees, depth, quote_events, quote_sequences, depth_limits = "kill switch 已鎖定", 0, 0, 0, 0, {}, {}, {}, {}
        else:
            reason, loss, contracts, entry_cost, settlement_fees, depth, settlement_at, quote_events, quote_sequences, depth_limits = self._validate(intent)
            if not reason and asof is not None:
                self._advance_clock(asof)
            if not reason:
                for key, needed in depth.items():
                    if self._reserved_depth[key] + self._consumed_depth[key] + needed > depth_limits[key]:
                        reason = "同一報價事件的可用口數已被其他 paper 訂單預占或使用"
                        break
        if not reason and self._reserved_risk + loss > self.limits.max_open_risk_twd:
            reason = "超過 max_open_risk_twd"
        if not reason and trading_day is not None and self._daily_pnl[trading_day] - self._reserved_risk - loss <= -self.limits.max_daily_loss_twd:
            reason = "超過 max_daily_loss_twd 剩餘風險額度"
        if not reason and self._reserved_contracts + contracts > self.limits.max_contracts:
            reason = "超過 max_contracts"
        if reason:
            result = {"status": "rejected", "reason": reason, "client_order_id": client_id}
            self._audit.append({"event": "reject", "client_order_id": client_id, "reason": reason})
            return result
        self._quote_events.update(quote_events)
        self._quote_sequences.update(quote_sequences)
        self._reserved_risk += loss
        self._reserved_contracts += contracts
        for key, quantity in depth.items():
            self._reserved_depth[key] += quantity
        result = {
            "status": "pending", "client_order_id": client_id, "intent": copy.deepcopy(intent),
            "computed_stress_loss_twd": round(loss, 4), "computed_entry_cost_twd": round(entry_cost, 4),
            "computed_settlement_fee_twd": round(settlement_fees, 4), "fills": [],
            "risk_basis": "設定結算指數範圍內的保守壓力估算，不是保證最大損失",
            "settlement_at_utc": settlement_at.isoformat(),
        }
        self._orders[client_id] = result
        self._order_depth[client_id] = depth
        self._audit.append({"event": "submit", "client_order_id": client_id, "reserved_risk_twd": round(loss, 4)})
        return copy.deepcopy(result)

    def fill_all(self, client_order_id: str, fill_id: str, fill_timestamp: str) -> dict:
        if client_order_id not in self._orders:
            raise KeyError(client_order_id)
        order = self._orders[client_order_id]
        intent, legs = order["intent"], order["intent"]["legs"]
        if fill_id in self._fills:
            existing = self._fills[fill_id]
            if existing["client_order_id"] != client_order_id:
                raise ValueError("fill_id 已被其他訂單使用")
            return copy.deepcopy(existing)
        if order["status"] != "pending":
            raise ValueError("只有 pending 訂單可模擬成交")
        filled_at = _time(fill_timestamp, "fill_timestamp")
        asof = _time(intent["asof"], "asof")
        if self._latest_event_time is not None and filled_at < self._latest_event_time:
            raise ValueError("成交時間早於 paper 模擬時鐘")
        self._advance_clock(filled_at)
        if order["status"] != "pending":
            raise ValueError(order["expiration_reason"])
        quote_times = [(_time(leg["timestamp"], "timestamp"), _time(leg["received_at"], "received_at")) for leg in legs]
        if filled_at < asof or any(filled_at < received_at for _, received_at in quote_times):
            self._expire_order(client_order_id, order, "成交時間已超過報價有效期限", fill_timestamp)
            raise ValueError("成交時間已超過報價有效期限")
        fills, cashflow = [], 0.0
        for index, leg in enumerate(legs):
            price = leg["ask"] if leg["side"] == "BUY" else leg["bid"]
            qty = leg["quantity"] * intent["quantity"]
            value = (-1 if leg["side"] == "BUY" else 1) * float(price) * MULTIPLIER * qty
            cashflow += value
            fills.append({
                "fill_id": f"{fill_id}:{index}",
                "symbol": f"TXO-{leg['expiry']}-{leg['right']}-{leg['strike']:g}",
                "side": leg["side"], "quantity": qty, "price": float(price),
                "cashflow_twd": round(value, 4), "quote_source": leg["source"],
                "quote_sequence": leg["sequence"],
            })
        cashflow -= order["computed_entry_cost_twd"]
        fill = {"fill_id": fill_id, "client_order_id": client_order_id, "fills": fills, "entry_cashflow_twd": round(cashflow, 4), "status": "filled"}
        self._fills[fill_id] = fill
        order["fills"], order["status"], order["fill_id"] = fills, "filled", fill_id
        for key, quantity in self._order_depth[client_order_id].items():
            self._reserved_depth[key] -= quantity
            self._consumed_depth[key] += quantity
        self._audit.append({"event": "fill", "client_order_id": client_order_id, "fill_id": fill_id, "entry_cashflow_twd": round(cashflow, 4)})
        return copy.deepcopy(fill)

    def _release_position(self, order: dict) -> None:
        self._reserved_risk = max(0.0, self._reserved_risk - order["computed_stress_loss_twd"])
        self._reserved_contracts = max(0, self._reserved_contracts - sum(leg["quantity"] for leg in order["intent"]["legs"]) * order["intent"]["quantity"])

    def _release_pending(self, client_order_id: str, order: dict) -> None:
        self._release_position(order)
        for key, quantity in self._order_depth[client_order_id].items():
            self._reserved_depth[key] -= quantity

    def _advance_clock(self, event_time: dt.datetime) -> None:
        if self._latest_event_time is None or event_time > self._latest_event_time:
            self._latest_event_time = event_time
        self._expire_stale_orders(event_time)

    def _expire_order(self, client_order_id: str, order: dict, reason: str, event_time: str) -> None:
        self._release_pending(client_order_id, order)
        order["status"], order["expiration_reason"] = "expired", reason
        self._audit.append({"event": "expire", "client_order_id": client_order_id, "reason": reason, "event_time": event_time})

    def _expire_stale_orders(self, event_time: dt.datetime) -> None:
        for client_order_id, order in self._orders.items():
            if order["status"] != "pending":
                continue
            if event_time >= _time(order["settlement_at_utc"], "settlement_at"):
                self._expire_order(client_order_id, order, "到期結算時點後不可成交", event_time.isoformat())
                continue
            if any(
                (event_time - _time(leg["timestamp"], "timestamp")).total_seconds() > MAX_QUOTE_AGE_SECONDS
                for leg in order["intent"]["legs"]
            ):
                self._expire_order(client_order_id, order, "成交時間已超過報價有效期限", event_time.isoformat())

    def cancel(self, client_order_id: str) -> dict:
        if client_order_id not in self._orders:
            raise KeyError(client_order_id)
        order = self._orders[client_order_id]
        if order["status"] == "pending":
            self._release_pending(client_order_id, order)
            order["status"] = "cancelled"
            self._audit.append({"event": "cancel", "client_order_id": client_order_id})
        return copy.deepcopy(order)

    def status(self, client_order_id: str) -> dict:
        if client_order_id not in self._orders:
            raise KeyError(client_order_id)
        return copy.deepcopy(self._orders[client_order_id])

    def close(self, client_order_id: str, settlement_index_points: float, settlement_timestamp: str, source_id: str) -> dict:
        if client_order_id not in self._orders:
            raise ValueError("找不到 paper position")
        order = self._orders[client_order_id]
        if isinstance(settlement_index_points, bool) or not isinstance(settlement_index_points, (int, float)) or not math.isfinite(settlement_index_points):
            raise ValueError("settlement_index_points 必須是有限數值")
        if settlement_index_points <= 0 or int(settlement_index_points) != settlement_index_points:
            raise ValueError("結算指數必須是正整數點數")
        if order["status"] == "closed":
            settled_at = _time(settlement_timestamp, "settlement_timestamp")
            existing = order["settlement"]
            if (float(settlement_index_points), settled_at, source_id) == (
                existing["index_points"], _time(existing["timestamp"], "settlement_timestamp"), existing["source_id"],
            ):
                return copy.deepcopy(order)
            raise ValueError("同一 paper position 不可套用衝突的結算事件")
        if order["status"] != "filled":
            raise ValueError("只有已成交的 paper position 可結清")
        if not 0 <= settlement_index_points <= self.costs.max_settlement_index_points:
            raise ValueError("結算指數超出設定壓力範圍")
        settled_at = _time(settlement_timestamp, "settlement_timestamp")
        first_leg = order["intent"]["legs"][0]
        expected_at = _time(first_leg["settlement_at"], "settlement_at")
        if settled_at < expected_at:
            raise ValueError("結算時間早於合約到期結算時點")
        if settled_at.astimezone(TAIPEI).date() != dt.date.fromisoformat(first_leg["expiry"]):
            raise ValueError("結算事件時間必須落在契約到期日")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id 不可空白")
        settlement_id = order["intent"]["legs"][0]["settlement_id"]
        settlement_event = (float(settlement_index_points), settled_at, source_id)
        previous_event = self._settlement_events.get(settlement_id)
        if previous_event is not None and previous_event != settlement_event:
            raise ValueError("同一到期結算識別碼已有不一致的結算事件")
        if self._latest_event_time is None or settled_at >= self._latest_event_time:
            self._advance_clock(settled_at)
        payout, settlement_tax, settlement_fee = 0.0, 0, 0.0
        for leg in order["intent"]["legs"]:
            strike, right = float(leg["strike"]), leg["right"]
            intrinsic = max(settlement_index_points - strike, 0) if right == "C" else max(strike - settlement_index_points, 0)
            contracts = leg["quantity"] * order["intent"]["quantity"]
            sign = 1 if leg["side"] == "BUY" else -1
            payout += sign * intrinsic * MULTIPLIER * contracts
            if intrinsic > 0:
                settlement_tax += round_twd(settlement_index_points * MULTIPLIER * self.costs.settlement_tax_rate) * contracts
                settlement_fee += self.costs.settlement_fee_twd_per_contract * contracts
        entry_cashflow = self._fills[order["fill_id"]]["entry_cashflow_twd"]
        realized_pnl = entry_cashflow + payout - settlement_tax - settlement_fee
        self._settlement_events.setdefault(settlement_id, settlement_event)
        self._release_position(order)
        trading_day = settled_at.astimezone(TAIPEI).date()
        self._daily_pnl[trading_day] += realized_pnl
        order["status"], order["realized_pnl_twd"] = "closed", round(realized_pnl, 4)
        order["settlement"] = {
            "index_points": float(settlement_index_points), "timestamp": settled_at.isoformat(),
            "source_id": source_id, "source_authenticated": False,
            "option_payoff_twd": round(payout, 4), "settlement_tax_twd": settlement_tax,
            "settlement_fee_twd": settlement_fee,
        }
        self._audit.append({
            "event": "close", "client_order_id": client_order_id, "realized_pnl_twd": round(realized_pnl, 4),
            "settlement_source_id": source_id, "source_authenticated": False,
        })
        if self._daily_pnl[trading_day] <= -self.limits.max_daily_loss_twd:
            self.trip_kill_switch("達到單日虧損上限", trading_day)
        return copy.deepcopy(order)

    def trip_kill_switch(self, reason: str = "manual", trading_day: dt.date | None = None) -> None:
        if trading_day is None:
            self._manual_kill_switch = True
        else:
            self._kill_switch_days.add(trading_day)
        for client_order_id, order in self._orders.items():
            order_day = _time(order["intent"]["asof"], "asof").astimezone(TAIPEI).date()
            if order["status"] == "pending" and (trading_day is None or order_day == trading_day):
                self._release_pending(client_order_id, order)
                order["status"] = "cancelled"
        self._audit.append({"event": "kill_switch", "reason": reason, "trading_day": trading_day.isoformat() if trading_day else None})

    def audit_log(self) -> list[dict]:
        return copy.deepcopy(self._audit)


def performance_metrics(daily_pnl_twd: list[float], initial_capital_twd: float | None = None) -> dict:
    values = [float(value) for value in daily_pnl_twd]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("daily_pnl_twd 必須全為有限數值")
    if initial_capital_twd is not None and (
        isinstance(initial_capital_twd, bool) or not isinstance(initial_capital_twd, (int, float))
        or not math.isfinite(initial_capital_twd) or initial_capital_twd <= 0
    ):
        raise ValueError("initial_capital_twd 必須是正的有限金額")
    total = sum(values)
    equity = float(initial_capital_twd) if initial_capital_twd is not None else 0.0
    peak, max_drawdown, returns, insolvent = equity, 0.0, [], False
    for value in values:
        before = equity
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if initial_capital_twd is not None:
            if before <= 0 or equity <= 0:
                insolvent = True
            elif not insolvent:
                returns.append(value / before)
    sharpe, reason = None, None
    if initial_capital_twd is None:
        reason = "缺少初始資金基數"
    elif insolvent:
        reason = "資金曲線曾歸零或為負"
    elif len(returns) < 2:
        reason = "至少需要兩個有效日報酬"
    elif statistics.stdev(returns) == 0:
        reason = "報酬標準差為零"
    else:
        sharpe = statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(252)
    return {
        "total_pnl_twd": round(total, 4), "max_drawdown_twd": round(max_drawdown, 4),
        "sharpe_annualized": round(sharpe, 8) if sharpe is not None else None,
        "sharpe_unavailable_reason": reason,
    }
