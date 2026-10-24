"""Market state evaluator (OFF/TREND_OK/CAUTION)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .params_store import ParamSet


@dataclass
class MarketState:
    state: str
    breadth20: float


def evaluate_state(features_by_symbol: Dict[str, Dict], params: ParamSet) -> MarketState:
    total = 0
    above = 0
    spy = features_by_symbol.get("SPY") or {}
    for sym, f in features_by_symbol.items():
        close = f.get("close")
        ema20 = f.get("ema20") or f.get("ema_20")
        if close is None or ema20 is None:
            continue
        total += 1
        if close > ema20:
            above += 1
    breadth20 = (above / total) if total else 0.0

    spy_close = spy.get("close")
    spy_ema50 = spy.get("ema50") or spy.get("ema_50")
    spy_ema20 = spy.get("ema20") or spy.get("ema_20")
    spy_ema20_slope = spy.get("ema20_slope")

    off = False
    if spy_close is not None and spy_ema50 is not None:
        if spy_close < spy_ema50 and breadth20 < params.breadth_off_a:
            off = True
    if breadth20 < params.breadth_off_b:
        off = True
    if off:
        return MarketState("OFF", breadth20)

    trend_ok = False
    if spy_close is not None and spy_ema20 is not None and spy_ema20_slope is not None:
        if spy_close > spy_ema20 and spy_ema20_slope > 0:
            trend_ok = True
    if breadth20 > params.breadth_ok:
        trend_ok = True
    if trend_ok:
        return MarketState("TREND_OK", breadth20)

    return MarketState("CAUTION", breadth20)
