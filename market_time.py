"""TAIFEX trading-date and TXO session checks for offline inputs."""

from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

TAIPEI = dt.timezone(dt.timedelta(hours=8), "Asia/Taipei")
CALENDAR_PATH = Path(__file__).with_name("calendar") / "taifex_trading_calendar.json"
# shortcut: uses the checked-in calendar, refresh it when TAIFEX adds dates or special closures.


@lru_cache(maxsize=1)
def _calendar() -> tuple[dt.date, dt.date, dict[dt.date, str], set[dt.date]]:
    data = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    closures = {dt.date.fromisoformat(row["date"]): row["reason"] for row in data["closed_weekdays"]}
    postponed = {dt.date.fromisoformat(row["last_trading_date"]) for row in data.get("expiry_postponements", [])}
    return dt.date.fromisoformat(data["coverage_start"]), dt.date.fromisoformat(data["coverage_end"]), closures, postponed


def _require_trading_day(day: dt.date, label: str) -> None:
    start, end, closures, _ = _calendar()
    if not start <= day <= end:
        raise ValueError(f"{label} {day} 超出 TAIFEX 日曆涵蓋範圍 {start} 至 {end}")
    if day.weekday() >= 5:
        raise ValueError(f"{label} {day} 為週末，TAIFEX 不交易")
    if day in closures:
        raise ValueError(f"{label} {day} 為 TAIFEX 休市日：{closures[day]}")


def is_trading_day(day: dt.date) -> bool:
    start, end, closures, _ = _calendar()
    if not start <= day <= end:
        raise ValueError(f"日期 {day} 超出 TAIFEX 日曆涵蓋範圍 {start} 至 {end}")
    return day.weekday() < 5 and day not in closures


def require_txo_expiry(day: dt.date) -> None:
    _require_trading_day(day, "契約到期日")
    start, end, closures, postponed = _calendar()
    if day.weekday() in {2, 4} or day in postponed:
        return
    for closed_day in closures:
        if closed_day.weekday() not in {2, 4}:
            continue
        adjusted = closed_day + dt.timedelta(days=1)
        while adjusted <= end and not is_trading_day(adjusted):
            adjusted += dt.timedelta(days=1)
        if adjusted == day:
            return
    raise ValueError(f"契約到期日 {day} 不符合 TAIFEX TXO 週三／週五排程或已知順延日")


def require_trading_day(day: dt.date, label: str = "交易日") -> None:
    _require_trading_day(day, label)


def validate_txo_session(timestamp: dt.datetime, session: str, expiry: dt.date | None = None) -> dt.date:
    """Validate an exchange timestamp and return its TAIFEX trading date."""
    local = timestamp.astimezone(TAIPEI)
    clock = local.timetz().replace(tzinfo=None)
    day = local.date()
    if session in {"day", "一般"}:
        _require_trading_day(day, "報價交易日")
        close = dt.time(13, 30) if expiry == day else dt.time(13, 45)
        if not dt.time(8, 45) <= clock <= close:
            raise ValueError(f"一般時段報價時間 {clock} 不在 08:45 至 {close} 之間")
        return day
    if session not in {"night", "盤後"}:
        raise ValueError(f"未知 TXO 交易時段：{session!r}")
    if clock >= dt.time(15, 0):
        start_day = day
    elif clock <= dt.time(5, 0):
        start_day = day - dt.timedelta(days=1)
    else:
        raise ValueError(f"盤後時段報價時間 {clock} 不在 15:00 至次日 05:00 之間")
    _require_trading_day(start_day, "盤後開市日")
    trading_day = start_day + dt.timedelta(days=1)
    while trading_day <= _calendar()[1] and not is_trading_day(trading_day):
        trading_day += dt.timedelta(days=1)
    _require_trading_day(trading_day, "盤後交易日")
    return trading_day
