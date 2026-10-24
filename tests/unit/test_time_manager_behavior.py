#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Ensure project root on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from llm_trader.time_manager import TimeManager  # type: ignore


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _ny(y, mo, d, hh, mm=0, ss=0) -> datetime:
    """Helper: build a NY-aware datetime from local wall time."""
    return datetime(y, mo, d, hh, mm, ss, tzinfo=ET)


def test_session_phase_core_boundaries():
    tm = TimeManager()

    # PRE: 04:00-09:30 (before 9:30)
    assert tm.session_phase(_ny(2025, 10, 24, 9, 15)) == "PRE"

    # OPEN: exactly 09:30
    assert tm.session_phase(_ny(2025, 10, 24, 9, 30, 0)) == "OPEN"

    # RTH: 09:30-16:00 (exclusive)
    assert tm.session_phase(_ny(2025, 10, 24, 10, 0, 0)) == "RTH"

    # CLOSE: exactly 16:00
    assert tm.session_phase(_ny(2025, 10, 24, 16, 0, 0)) == "CLOSE"

    # AFTERHOURS: 16:00-20:00
    assert tm.session_phase(_ny(2025, 10, 24, 16, 5, 0)) == "AFTERHOURS"

    # OVERNIGHT: >= 20:00 or < 03:50
    assert tm.session_phase(_ny(2025, 10, 24, 20, 0, 0)) == "OVERNIGHT"
    assert tm.session_phase(_ny(2025, 10, 24, 3, 45, 0)) == "OVERNIGHT"

    # OFF: 03:50-04:00 maintenance window
    assert tm.session_phase(_ny(2025, 10, 24, 3, 55, 0)) == "OFF"


def test_flatten_and_allows_with_close_at():
    tm = TimeManager(close_at="15:55", forbid_new_positions_before_min=5)

    # 15:54 -> not flattened yet, still allow (unless within gate window)
    t_1554 = _ny(2025, 10, 24, 15, 54, 0)
    assert tm.should_flatten_now(t_1554) is False

    # Within forbid window (15:55 gate and forbid_new_positions_before_min=5 -> from 15:55? No, gate is based on close 16:00)
    # The implementation forbids new positions within N minutes of 16:00.
    # At 15:56, allow_new_position should be False (since 16:00-5 = 15:55 gate_from)
    t_1556 = _ny(2025, 10, 24, 15, 56, 0)
    assert tm.allow_new_position(t_1556) is False

    # 15:55 -> flatten
    t_1555 = _ny(2025, 10, 24, 15, 55, 0)
    assert tm.should_flatten_now(t_1555) is True


def test_seconds_until_open_variants():
    tm = TimeManager()

    # Before open on same day
    t_0900 = _ny(2025, 10, 24, 9, 0, 0)
    sec = tm.seconds_until_market_open(t_0900)
    assert isinstance(sec, int) and sec > 0

    # During RTH -> None
    t_1000 = _ny(2025, 10, 24, 10, 0, 0)
    assert tm.seconds_until_market_open(t_1000) is None
