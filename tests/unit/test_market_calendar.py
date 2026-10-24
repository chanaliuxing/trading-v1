from __future__ import annotations

from datetime import date

from libs.market_calendar import is_trading_day


def test_is_trading_day_marks_weekend_false_without_exchange_calendar() -> None:
    assert is_trading_day(date(2026, 4, 24)) is True
    assert is_trading_day(date(2026, 4, 25)) is False
    assert is_trading_day(date(2026, 4, 26)) is False
