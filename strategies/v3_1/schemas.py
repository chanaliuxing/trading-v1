"""Schema helpers for V3.1 signals, exits, and trade logs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class HedgeAction:
    required: bool
    method: str
    size_reduction_pct: float = 0.0
    alt_method: Optional[str] = None
    hedge_cost_ratio: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "required": self.required,
            "method": self.method,
            "size_reduction_pct": round(float(self.size_reduction_pct), 4),
            "alt_method": self.alt_method,
            "hedge_cost_ratio": round(float(self.hedge_cost_ratio), 6),
        }


@dataclass
class ExitTarget:
    id: str
    price: float
    sell_pct: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "price": float(self.price),
            "sell_pct": round(float(self.sell_pct), 4),
            "reason": self.reason,
        }


@dataclass
class ExitPlan:
    priority_chain: List[str]
    targets: List[ExitTarget]
    gap_rule: Dict[str, Any]
    trail: Dict[str, Any]
    time_stop: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "priority_chain": list(self.priority_chain),
            "targets": [t.to_dict() for t in self.targets],
            "gap_rule": dict(self.gap_rule),
            "trail": dict(self.trail),
            "time_stop": dict(self.time_stop),
        }


@dataclass
class Signal:
    ticker: str
    ts: str
    state: str
    lane: str
    grade: str
    quality_score: int
    features: Dict[str, Any]
    execution: Dict[str, Any]
    hedge_action: HedgeAction
    exit_plan: ExitPlan

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "ts": self.ts,
            "state": self.state,
            "lane": self.lane,
            "grade": self.grade,
            "quality_score": int(self.quality_score),
            "features": dict(self.features),
            "execution": dict(self.execution),
            "hedge_action": self.hedge_action.to_dict(),
            "exit_plan": self.exit_plan.to_dict(),
        }


@dataclass
class TradeEvent:
    ts: str
    event: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "event": self.event, "detail": dict(self.detail)}


@dataclass
class TradeLog:
    ticker: str
    lane: str
    state: str
    grade: str
    quality_score: int
    entry_px: float
    stop0: float
    r_per_share: float
    shares: int
    risk_budget: float
    hedge_action: HedgeAction
    events: List[TradeEvent] = field(default_factory=list)
    mfe_r: float = 0.0
    mae_r: float = 0.0
    realized_r: float = 0.0
    costs_bp: float = 0.0
    slippage_bp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "lane": self.lane,
            "state": self.state,
            "grade": self.grade,
            "quality_score": int(self.quality_score),
            "entry_px": float(self.entry_px),
            "stop0": float(self.stop0),
            "r_per_share": float(self.r_per_share),
            "shares": int(self.shares),
            "risk_budget": float(self.risk_budget),
            "hedge_action": self.hedge_action.to_dict(),
            "events": [e.to_dict() for e in self.events],
            "mfe_r": float(self.mfe_r),
            "mae_r": float(self.mae_r),
            "realized_r": float(self.realized_r),
            "costs_bp": float(self.costs_bp),
            "slippage_bp": float(self.slippage_bp),
        }
