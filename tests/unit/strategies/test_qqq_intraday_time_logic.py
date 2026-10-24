from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from strategies.qqq_intraday_time_logic import coerce_datetime, feature_now_et, parse_hms_tuple

ET = ZoneInfo("America/New_York")


def test_coerce_datetime_iso() -> None:
    parsed = coerce_datetime("2026-03-25T09:45:00-04:00")
    assert isinstance(parsed, datetime)
    assert parsed is not None
    assert parsed.hour == 9


def test_parse_hms_tuple() -> None:
    assert parse_hms_tuple("15:59:00", fallback=(0, 0, 0)) == (15, 59, 0)
    assert parse_hms_tuple("bad", fallback=(1, 2, 3)) == (1, 2, 3)


def test_feature_now_et_with_iso_and_fallback_time() -> None:
    dt = feature_now_et({"now_et": "2026-03-25T09:45:00-04:00"}, tz=ET)
    assert dt.hour == 9 and dt.minute == 45

    fixed_now = datetime(2026, 3, 25, 10, 1, 2, tzinfo=ET)
    dt2 = feature_now_et(
        {"now_time_et": "09:33:10"},
        tz=ET,
        now_fn=lambda _tz: fixed_now,
    )
    assert dt2.hour == 9 and dt2.minute == 33 and dt2.second == 10
