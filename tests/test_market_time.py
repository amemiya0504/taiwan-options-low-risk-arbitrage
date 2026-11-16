import datetime as dt

import pytest

from market_time import require_txo_expiry, validate_txo_session


def stamp(value):
    return dt.datetime.fromisoformat(value).astimezone(dt.timezone.utc)


def test_friday_night_and_saturday_morning_belong_to_monday():
    expiry = dt.date(2026, 11, 18)
    assert validate_txo_session(stamp("2026-11-13T15:00:00+08:00"), "night", expiry) == dt.date(2026, 11, 16)
    assert validate_txo_session(stamp("2026-11-14T04:59:59+08:00"), "night", expiry) == dt.date(2026, 11, 16)


@pytest.mark.parametrize("value", ["2026-11-15T15:00:00+08:00", "2026-11-16T04:59:59+08:00"])
def test_sunday_does_not_open_or_continue_afterhours(value):
    with pytest.raises(ValueError, match="2026-11-15"):
        validate_txo_session(stamp(value), "night", dt.date(2026, 11, 18))


def test_expiry_schedule_accepts_regular_and_calendar_adjusted_dates():
    for day in (dt.date(2026, 11, 18), dt.date(2026, 11, 20), dt.date(2026, 2, 23), dt.date(2026, 7, 13), dt.date(2027, 3, 17)):
        require_txo_expiry(day)
    with pytest.raises(ValueError, match="2026-01-06"):
        require_txo_expiry(dt.date(2026, 1, 6))


def test_2027_lunar_new_year_eve_night_session_maps_to_first_trading_day_after_holiday():
    expiry = dt.date(2027, 3, 17)
    assert validate_txo_session(stamp("2027-02-01T15:00:00+08:00"), "night", expiry) == dt.date(2027, 2, 11)
    assert validate_txo_session(stamp("2027-02-02T04:59:59+08:00"), "night", expiry) == dt.date(2027, 2, 11)


def test_regular_session_rejects_after_close():
    expiry = dt.date(2026, 11, 18)
    with pytest.raises(ValueError, match="13:45"):
        validate_txo_session(stamp("2026-11-16T13:45:01+08:00"), "day", expiry)
