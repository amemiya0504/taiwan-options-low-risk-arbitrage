"""Close-quote box-spread backtest on TAIFEX daily data (upper-bound estimate, read-only).

日行情只有每個序列收盤時的最後最佳買賣價，沒有掛單口數與報價時間，
因此結果只是「假設四腿都能用收盤報價各成交 1 口」的上限估計，不代表可成交損益。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import txo_engine
from quality_check import contract_expiry_dates, parse_date, readonly_connect

MULTIPLIER = txo_engine.MULTIPLIER
COST_KEYS = ("option_exchange_fee_twd", "option_broker_fee_twd", "option_tax_rate",
             "option_settlement_fee_twd", "option_settlement_tax_reserve_twd")


def load_costs(path: Path) -> dict:
    costs = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in COST_KEYS if key not in costs]
    if missing:
        raise ValueError(f"costs 缺少欄位：{', '.join(missing)}")
    result = {key: txo_engine._finite(key, costs[key], minimum=0) for key in COST_KEYS}
    if result["option_tax_rate"] > 1:
        raise ValueError("option_tax_rate 應以 0 至 1 的比率表示")
    return result


def leg_cost(premium: float, costs: dict) -> float:
    """單腿 1 口：交易經手費、券商費、權利金期交稅，以及到期交割費與稅金準備（保守地每腿都計）。"""
    return (costs["option_exchange_fee_twd"] + costs["option_broker_fee_twd"]
            + txo_engine.round_twd(premium * MULTIPLIER * costs["option_tax_rate"])
            + costs["option_settlement_fee_twd"] + costs["option_settlement_tax_reserve_twd"])


def box_candidates(strikes: dict, discount: float, costs: dict) -> list[dict]:
    """strikes: {strike: {"C": (bid, ask, volume), "P": (...)}}，回傳扣成本後淨現值為正的箱型。"""
    ks = sorted(k for k, q in strikes.items() if "C" in q and "P" in q)
    found = []
    for i, k1 in enumerate(ks):
        (c1b, c1a, c1v), (p1b, p1a, p1v) = strikes[k1]["C"], strikes[k1]["P"]
        for k2 in ks[i + 1:]:
            (c2b, c2a, c2v), (p2b, p2a, p2v) = strikes[k2]["C"], strikes[k2]["P"]
            payoff_pv = discount * (k2 - k1) * MULTIPLIER
            traded = min(c1v, c2v, p1v, p2v) > 0
            # long box：BUY C1、SELL C2、SELL P1、BUY P2，到期固定收 K2-K1。
            debit = (c1a - c2b - p1b + p2a) * MULTIPLIER
            long_net = payoff_pv - debit - sum(leg_cost(p, costs) for p in (c1a, c2b, p1b, p2a))
            # short box：反向四腿，進場收權利金、到期固定付 K2-K1。
            credit = (c1b - c2a - p1a + p2b) * MULTIPLIER
            short_net = credit - payoff_pv - sum(leg_cost(p, costs) for p in (c1b, c2a, p1a, p2b))
            for kind, net, cash in (("long_box", long_net, -debit), ("short_box", short_net, credit)):
                net = round(net, 4)
                if net > 0:
                    found.append({"strategy": kind, "strikes": [k1, k2], "net_pv_twd": net,
                                  "entry_cash_twd": round(cash, 4), "all_legs_traded": traded})
    return found


def run(db_path: Path, start: dt.date, end: dt.date, costs: dict, financing_rate: float,
        postponements: dict | None = None, closed_weekdays: set[dt.date] | None = None) -> dict:
    """postponements: {(contract_month, scheduled_expiry): last_trading_date}，來自官方日曆的颱風順延。"""
    if start > end:
        raise ValueError("開始日期不可晚於結束日期")
    postponements = postponements or {}
    closed_weekdays = closed_weekdays or set()
    groups: dict[tuple, dict] = defaultdict(dict)
    skipped_unknown_expiry = 0
    with closing(readonly_connect(db_path)) as db:
        rows = db.execute(
            """SELECT trade_date, trading_session, contract_month, contract_expiry, strike, option_type,
                      best_bid, best_ask, volume
               FROM txo_daily
               WHERE trade_date BETWEEN ? AND ? AND best_bid > 0 AND best_ask >= best_bid""",
            (start.isoformat(), end.isoformat()),
        )
        for date, session, month, expiry, strike, option_type, bid, ask, volume in rows:
            if not expiry:
                scheduled, expiry, error = contract_expiry_dates(month, closed_weekdays)
                if error is not None:
                    skipped_unknown_expiry += 1
                    continue
                scheduled_expiry = scheduled.isoformat()
                expiry = expiry.isoformat()
            else:
                scheduled_expiry = expiry
            right = {"買權": "C", "賣權": "P"}[option_type]
            groups[(date, session, month, expiry, scheduled_expiry)].setdefault(strike, {})[right] = (bid, ask, volume)

    candidates = []
    for (date, session, month, expiry, scheduled_expiry), strikes in sorted(groups.items()):
        settlement_date = parse_date(postponements.get((month, scheduled_expiry), expiry))
        if parse_date(date) > settlement_date:
            raise ValueError(f"{month} 交易日 {date} 晚於到期日 {settlement_date}，日曆缺少順延資料")
        years = (settlement_date - parse_date(date)).days / 365
        discount = txo_engine.discount_factor(financing_rate, years)
        for item in box_candidates(strikes, discount, costs):
            candidates.append({"trade_date": date, "session": session, "contract_month": month,
                               "contract_expiry": expiry, **item})

    # 每日最多記 1 筆（當日淨現值最高、四腿皆有成交），避免同一組腿重複計算。
    daily_best: dict[str, dict] = {}
    for item in candidates:
        if item["all_legs_traded"] and item["net_pv_twd"] > daily_best.get(item["trade_date"], {}).get("net_pv_twd", 0):
            daily_best[item["trade_date"]] = item
    traded = [item for item in candidates if item["all_legs_traded"]]
    return {
        "mode": "close_quote_upper_bound", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "assumptions": {
            "financing_rate": financing_rate, "costs": costs, "quantity_per_leg": 1,
            "fill": "四腿各以收盤最後最佳買賣價成交 1 口；日行情沒有掛單口數與報價時間",
            "discount": "以交易日至到期日曆天數 / 365 折現；借貸同一利率",
            "not_modeled": ["報價同時性", "可成交口數", "部分成交與單腿風險", "保證金占用", "盤中機會"],
        },
        "skipped_rows_unknown_expiry": skipped_unknown_expiry,
        "summary": {
            "quote_groups": len(groups), "candidates": len(candidates), "candidates_all_legs_traded": len(traded),
            "days_with_traded_candidate": len(daily_best),
            "daily_best_total_net_pv_twd": round(sum(item["net_pv_twd"] for item in daily_best.values()), 4),
        },
        "daily_best": [daily_best[date] for date in sorted(daily_best)],
        "top_candidates": sorted(candidates, key=lambda item: -item["net_pv_twd"])[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="以 TAIFEX 日行情收盤最佳報價做箱型價差上限回測（唯讀）")
    parser.add_argument("--db", type=Path, default=Path("data/txo.sqlite3"))
    parser.add_argument("--start", required=True, type=parse_date)
    parser.add_argument("--end", required=True, type=parse_date)
    parser.add_argument("--calendar", type=Path, default=Path("calendar/taifex_trading_calendar.json"))
    parser.add_argument("--costs", required=True, type=Path, help="JSON，欄位同 snapshot costs 的 option_* 費率")
    parser.add_argument("--financing-rate", required=True, type=float, help="年化資金成本，例如 0.015")
    parser.add_argument("--output", type=Path, default=Path("reports/close_backtest.json"))
    args = parser.parse_args()
    try:
        if args.output.resolve() == args.db.resolve():
            raise ValueError("輸出路徑不可為資料庫")
        calendar = json.loads(args.calendar.read_text(encoding="utf-8"))
        postponements = {(item["contract_month"], item["scheduled_expiry"]): item["last_trading_date"]
                         for item in calendar.get("expiry_postponements", [])}
        closed_weekdays = {parse_date(item["date"]) for item in calendar.get("closed_weekdays", [])}
        report = run(args.db, args.start, args.end, load_costs(args.costs), args.financing_rate,
                     postponements, closed_weekdays)
    except (OSError, ValueError, KeyError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s = report["summary"]
    print(f"候選 {s['candidates']:,}；四腿皆有成交 {s['candidates_all_legs_traded']:,}；"
          f"有候選的日數 {s['days_with_traded_candidate']}；每日最佳合計淨現值 {s['daily_best_total_net_pv_twd']:,} 元")
    print(f"報告：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
