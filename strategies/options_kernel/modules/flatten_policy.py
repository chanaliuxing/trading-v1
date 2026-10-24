from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from .position_policy import PositionContext
from .risk_policy import RiskDecision, TimeCutoffFlattenRiskPolicy


@dataclass(frozen=True)
class FlattenDecision:
    exit_plan: Optional[Any]
    reason: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


class BaseFlattenPolicy(Protocol):
    policy_id: str

    def evaluate(self, ctx: PositionContext) -> FlattenDecision:
        ...


class TimeCutoffFlattenPolicy:
    def __init__(self, policy_id: str = "time_cutoff_flatten") -> None:
        self.policy_id = policy_id
        self._delegate = TimeCutoffFlattenRiskPolicy(policy_id=policy_id)

    def evaluate(self, ctx: PositionContext) -> FlattenDecision:
        decision: RiskDecision = self._delegate.evaluate(ctx)
        return FlattenDecision(exit_plan=decision.exit_plan, reason=decision.reason, payload=decision.payload)
