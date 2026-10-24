from __future__ import annotations

import datetime as dt

from strategies.qqq_intraday_market_state_logic import (
    coerce_et_datetime,
    history_value,
    live_value_at_or_before,
    minute_history_map,
    session_pos_day_from_history,
    upsert_history_row,
    upsert_live_sample,
)


def test_upsert_history_row_and_history_value_round_trip() -> None:
    minute = dt.datetime(2026, 3, 26, 9, 35)
    rows = upsert_history_row(rows=[], minute_iso=minute.isoformat(), field="close", value=501.25)
    rows = upsert_history_row(rows=rows, minute_iso=minute.isoformat(), field="close", value=502.0)

    assert len(rows) == 1
    assert history_value(rows=rows, minute_ts=minute, field="close") == 502.0


def test_minute_history_map_normalizes_to_et_minute() -> None:
    rows = [{"minute": "2026-03-26T09:35:45-04:00", "vwap": 500.5}]
    values = minute_history_map(rows=rows, field="vwap")

    minute = dt.datetime(2026, 3, 26, 9, 35, tzinfo=coerce_et_datetime(rows[0]["minute"]).tzinfo)
    assert values[minute] == 500.5


def test_upsert_live_sample_and_value_at_or_before() -> None:
    rows = []
    rows = upsert_live_sample(rows=rows, ts_iso="2026-03-26T09:35:01-04:00", close_now=500.0)
    rows = upsert_live_sample(rows=rows, ts_iso="2026-03-26T09:35:06-04:00", close_now=501.0)

    target = dt.datetime.fromisoformat("2026-03-26T09:35:05-04:00")
    assert live_value_at_or_before(rows=rows, target_ts=target) == 500.0


def test_session_pos_day_from_history_uses_intraday_range() -> None:
    rows = [
        {"minute": "2026-03-26T09:30:00-04:00", "close": 100.0},
        {"minute": "2026-03-26T09:31:00-04:00", "close": 102.0},
        {"minute": "2026-03-26T09:32:00-04:00", "close": 101.0},
    ]
    now = dt.datetime.fromisoformat("2026-03-26T09:33:00-04:00")
    value = session_pos_day_from_history(rows=rows, eval_now=now, current_close=101.5)

    assert value is not None
    assert 0.0 <= value <= 1.0
    assert round(value, 4) == 0.75
