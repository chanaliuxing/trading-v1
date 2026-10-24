from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from strategies.qqq_intraday_session_gate_logic import in_session_window, previous_session_gate, session_window_gate

ET = ZoneInfo("America/New_York")


def test_in_session_window_day_session() -> None:
    now = datetime(2026, 3, 26, 8, 0, 0, tzinfo=ET)
    assert in_session_window(now_et=now, start_hms=(4, 0, 0), end_hms=(9, 29, 59)) is True
    now2 = datetime(2026, 3, 26, 10, 0, 0, tzinfo=ET)
    assert in_session_window(now_et=now2, start_hms=(4, 0, 0), end_hms=(9, 29, 59)) is False


def test_in_session_window_overnight_wrap() -> None:
    now = datetime(2026, 3, 26, 22, 0, 0, tzinfo=ET)
    assert in_session_window(now_et=now, start_hms=(20, 0, 0), end_hms=(3, 59, 59)) is True
    now2 = datetime(2026, 3, 26, 12, 0, 0, tzinfo=ET)
    assert in_session_window(now_et=now2, start_hms=(20, 0, 0), end_hms=(3, 59, 59)) is False


def test_previous_session_gate_blocks_when_not_flat() -> None:
    decision = previous_session_gate(
        require_prev_session_flat=True,
        runtime_flat=False,
        broker_flat=True,
        open_orders_clear=True,
        runtime_check_ok=True,
        broker_check_ok=True,
        open_orders_check_ok=True,
    )
    assert decision.allowed is False
    assert decision.reason == "previous_session_runtime_not_flat"


def test_session_window_gate_blocks_outside_window() -> None:
    decision = session_window_gate(
        now_et=datetime(2026, 3, 26, 12, 0, 0, tzinfo=ET),
        start_hms=(4, 0, 0),
        end_hms=(9, 29, 59),
        enforce_entry_window=True,
    )
    assert decision.allowed is False
    assert decision.reason == "outside_session_window"
