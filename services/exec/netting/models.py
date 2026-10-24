from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


VALID_SIDES = {"BUY", "SELL"}


def normalize_side(side: str) -> str:
    s = str(side or "").strip().upper()
    if s not in VALID_SIDES:
        raise ValueError(f"invalid side: {side}")
    return s


@dataclass(frozen=True)
class NetChildIntent:
    """A strategy-level intent that can participate in netted execution."""

    strategy_id: str
    decision_id: str
    symbol: str
    side: str
    qty: float
    submitted_at_us: int = 0
    priority: int = 50
    metadata: Dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> "NetChildIntent":
        return NetChildIntent(
            strategy_id=str(self.strategy_id),
            decision_id=str(self.decision_id),
            symbol=str(self.symbol).upper(),
            side=normalize_side(self.side),
            qty=float(self.qty),
            submitted_at_us=int(self.submitted_at_us or 0),
            priority=int(self.priority),
            metadata=dict(self.metadata or {}),
        )


@dataclass(frozen=True)
class NetParentPlan:
    """A single broker-facing parent order after netting."""

    group_id: str
    symbol: str
    side: str
    qty: float
    child_keys: List[str]


@dataclass(frozen=True)
class NettingDecision:
    """Output of netting for one batch of intents."""

    status: str
    reason: str
    parent_plan: Optional[NetParentPlan] = None
    accepted_children: List[NetChildIntent] = field(default_factory=list)
    rejected_children: List[NetChildIntent] = field(default_factory=list)


@dataclass
class ChildOrderState:
    """Mutable child-order state used for deterministic fill fan-out."""

    child_order_id: str
    strategy_id: str
    decision_id: str
    remaining_qty: float


@dataclass(frozen=True)
class FillAllocation:
    """One allocation slice from parent fill to one child order."""

    child_order_id: str
    strategy_id: str
    decision_id: str
    qty: float
    price: float
