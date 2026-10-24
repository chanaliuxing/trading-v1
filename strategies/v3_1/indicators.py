"""Indicator helpers for V3.1 strategy."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None


def ema(series: List[float], period: int) -> Optional[float]:
    if not series or period <= 0:
        return None
    alpha = 2.0 / (period + 1.0)
    ema_val = series[0]
    for v in series[1:]:
        ema_val = alpha * v + (1 - alpha) * ema_val
    return ema_val


def ema_slope(series: List[float], period: int, lookback: int = 5) -> Optional[float]:
    if len(series) < period + lookback:
        return None
    ema_now = ema(series[-period:], period)
    ema_prev = ema(series[-period - lookback:-lookback], period)
    if ema_now is None or ema_prev is None:
        return None
    return ema_now - ema_prev


def atr(bars: List[Bar], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        hi = bars[i].high
        lo = bars[i].low
        prev_close = bars[i - 1].close
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / float(period)


def highest_high(bars: List[Bar], lookback: int = 20) -> Optional[float]:
    if len(bars) < lookback:
        return None
    return max(b.high for b in bars[-lookback:])


def avg_volume(bars: List[Bar], lookback: int = 20) -> Optional[float]:
    if len(bars) < lookback:
        return None
    return sum(b.volume for b in bars[-lookback:]) / float(lookback)


def wall_dist_pct(close: float, wall_price: Optional[float]) -> Optional[float]:
    if not wall_price or close <= 0:
        return None
    return (wall_price - close) / close
