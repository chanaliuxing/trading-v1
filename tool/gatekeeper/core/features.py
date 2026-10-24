from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def compute_vwap(bars: list[dict[str, Any]]) -> float | None:
    if not bars:
        return None

    total_volume = 0.0
    total_pv = 0.0
    closes: list[float] = []

    for bar in bars:
        close = float(bar.get("close") or 0.0)
        high = float(bar.get("high") or close)
        low = float(bar.get("low") or close)
        volume = float(bar.get("volume") or 0.0)

        closes.append(close)
        typical = (high + low + close) / 3.0
        total_volume += volume
        total_pv += typical * volume

    if total_volume > 0:
        return total_pv / total_volume
    return None


def last_n_closes(bars: list[dict[str, Any]], n: int) -> list[float]:
    if not bars:
        return []
    return [float(b.get("close") or 0.0) for b in bars[-n:]]


def last3_relation_to_vwap(bars: list[dict[str, Any]], vwap: float | None) -> list[str]:
    if vwap is None:
        return []
    closes = last_n_closes(bars, 3)
    relation: list[str] = []
    for c in closes:
        if c > vwap:
            relation.append("above")
        elif c < vwap:
            relation.append("below")
        else:
            relation.append("at")
    return relation


def is_last3_all_above_vwap(bars: list[dict[str, Any]], vwap: float | None) -> bool:
    rel = last3_relation_to_vwap(bars, vwap)
    return bool(rel) and all(r == "above" for r in rel)


def is_last3_all_below_vwap(bars: list[dict[str, Any]], vwap: float | None) -> bool:
    rel = last3_relation_to_vwap(bars, vwap)
    return bool(rel) and all(r == "below" for r in rel)


@dataclass(frozen=True, slots=True)
class ChopDetails:
    cross_count: int
    touch_count: int
    trend_pct: float
    cross_score: float
    touch_score: float
    trend_score: float
    chop_score: float


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def compute_chop(
    bars: list[dict[str, Any]],
    vwap: float | None,
    *,
    window_min: int,
    band_pct: float,
    cross_min: int,
    touch_min: int,
    trend_cap_pct: float,
    w_cross: float,
    w_touch: float,
    w_trend: float,
) -> ChopDetails | None:
    if vwap is None:
        return None
    if not bars:
        return None

    window = bars[-window_min:] if window_min > 0 else bars
    closes = [float(b.get("close") or 0.0) for b in window]
    closes = [c for c in closes if c > 0]
    if len(closes) < 2:
        return None

    band = abs(vwap) * float(band_pct)

    prev_non_zero_sign: int | None = None
    cross_count = 0
    touch_count = 0

    for close in closes:
        diff = close - vwap
        if abs(diff) <= band:
            touch_count += 1

        s = _sign(diff)
        if s == 0:
            continue
        if prev_non_zero_sign is None:
            prev_non_zero_sign = s
            continue
        if s != prev_non_zero_sign:
            cross_count += 1
            prev_non_zero_sign = s

    first = closes[0]
    last = closes[-1]
    trend_pct = abs(last - first) / first if first != 0 else 0.0

    cross_score = min(1.0, cross_count / float(max(1, cross_min)))
    touch_score = min(1.0, touch_count / float(max(1, touch_min)))
    trend_score = 1.0 - min(1.0, trend_pct / float(max(1e-9, trend_cap_pct)))

    chop_score = (w_cross * cross_score) + (w_touch * touch_score) + (w_trend * trend_score)
    if math.isnan(chop_score) or math.isinf(chop_score):
        return None

    return ChopDetails(
        cross_count=int(cross_count),
        touch_count=int(touch_count),
        trend_pct=float(trend_pct),
        cross_score=float(cross_score),
        touch_score=float(touch_score),
        trend_score=float(trend_score),
        chop_score=float(chop_score),
    )


def range_pct(bars: list[dict[str, Any]], window: int) -> float | None:
    if not bars:
        return None
    if window <= 0:
        return None
    window_bars = bars[-window:]
    highs = [float(b.get("high") or 0.0) for b in window_bars]
    lows = [float(b.get("low") or 0.0) for b in window_bars]
    closes = [float(b.get("close") or 0.0) for b in window_bars]
    last_close = closes[-1] if closes else 0.0
    if last_close <= 0:
        return None
    return (max(highs) - min(lows)) / last_close


def gap_from_open_pct(bars: list[dict[str, Any]]) -> float | None:
    if not bars:
        return None
    first_open = float(bars[0].get("open") or 0.0)
    last_close = float(bars[-1].get("close") or 0.0)
    if first_open <= 0:
        return None
    return abs(last_close - first_open) / first_open


def vwap_deviation_pct(last_close: float | None, vwap: float | None) -> float | None:
    if last_close is None or vwap is None:
        return None
    if vwap == 0:
        return None
    return abs(last_close - vwap) / abs(vwap)
