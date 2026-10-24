#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from datetime import timezone
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from llm_trader.ny_time import (
    now_ny as get_ny_now, to_ny, fmt_ny, next_market_close_ny, seconds_until, NY_TZ
)


def test_get_ny_now_is_aware_and_in_ny():
    now = get_ny_now()
    assert now.tzinfo is not None
    assert str(now.tzinfo) in ("America/New_York", getattr(NY_TZ, "key", str(NY_TZ)))


def test_to_ny_idempotent():
    ny = get_ny_now()
    back = to_ny(ny)
    assert abs(back.timestamp() - ny.timestamp()) < 1


def test_format_and_close_helpers():
    ny = get_ny_now()
    s = fmt_ny(ny)
    assert isinstance(s, str) and len(s) >= 19

    close_dt = next_market_close_ny(ny)
    assert close_dt.hour == 16 and close_dt.tzinfo is not None

    wait = seconds_until(close_dt, ny)
    assert wait >= 0
