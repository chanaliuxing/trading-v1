from __future__ import annotations

"""
Daily trading plan schemas for llm_trader (P0 minimal version).

Goals:
- Represent a single-day plan with time slots and per-symbol directives
- Keep structure strict but simple for initial execution

Notes:
- Use plain dict-based typing for compatibility; validation is caller's duty
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class EntryDirective:
    window: str  # e.g., "premarket", "intraday", "eod"
    side: str  # "buy" | "sell"
    style: str = "limit"  # only limit supported in P0
    px_rule: Optional[str] = None  # e.g., "preclose*0.995"; P0 can accept numeric via "=245.5"
    limit_price: Optional[float] = None  # direct numeric, alternative to px_rule
    qty_rule: str = "fixed(100)"  # e.g., "fixed(100)" or "notional(3000)"
    oco: Optional[Dict[str, int]] = None  # {"tp_bps":50, "sl_bps":35}
    ttl_sec: int = 600


@dataclass
class AdjustPolicy:
    cancel_ttl_sec: int = 600
    replace_step_bps: int = 5
    max_reprices: int = 3
    trailing: Optional[Dict[str, int]] = None  # {"activate_bps": 40, "trail_bps": 20}


@dataclass
class SymbolDirective:
    entry: List[EntryDirective] = field(default_factory=list)
    intraday: List[EntryDirective] = field(default_factory=list)
    exit: List[EntryDirective] = field(default_factory=list)
    adjust: AdjustPolicy = field(default_factory=AdjustPolicy)


@dataclass
class HedgeIntent:
    """Hedge intent for risk management."""
    symbols: List[str] = field(default_factory=list)
    notional_pct: float = 0.5  # 0.0-1.0
    urgency: str = "medium"  # low/medium/high
    instrument: Optional[str] = None  # SPY/QQQ/IWM/etc.
    strategy: str = "options_put"  # options_put/options_collar/stock_short
    confidence: Optional[float] = None  # Signal confidence affects sizing
    max_cost_bps: int = 100  # Max hedge cost in basis points


@dataclass
class DailyPlan:
    day_id: str
    risk_budget: Dict[str, Any] = field(default_factory=dict)
    slots: Dict[str, List[str]] = field(default_factory=dict)  # {"premarket": ["07:00",...], ...}
    directives: Dict[str, SymbolDirective] = field(default_factory=dict)  # symbol -> directives
    hedge_intent: Optional[HedgeIntent] = None  # Hedge specification for risk management


def parse_qty_rule(qty_rule: str, price: float) -> int:
    try:
        if qty_rule.startswith("fixed(") and qty_rule.endswith(")"):
            return max(1, int(float(qty_rule[6:-1])))
        if qty_rule.startswith("notional(") and qty_rule.endswith(")"):
            notional = max(0.0, float(qty_rule[9:-1]))
            if price > 0:
                return max(1, int(notional / price))
    except Exception:
        pass
    return 100


def approx_price_from_rule(px_rule: Optional[str], default_price: Optional[float] = None) -> Optional[float]:
    """P0 minimal evaluator. Supports:
    - "=123.45" exact price
    - None -> default_price
    Future: preclose*X, vwap_5m +- bps (requires data fetcher)
    """
    if px_rule is None:
        return default_price
    s = px_rule.strip()
    if s.startswith("="):
        try:
            return float(s[1:])
        except Exception:
            return default_price
    return default_price

