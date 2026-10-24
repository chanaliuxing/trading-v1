from __future__ import annotations

from dataclasses import replace
from datetime import time
from typing import Optional

from .position_policy import BasePositionPolicy, PositionContext
from .profit_management_policy import MARunnerProfitManagementPolicy
from .risk_policy import MARunnerRiskPolicy, TimeCutoffFlattenRiskPolicy
from ..types import ExitPlan


class MARunnerPositionPolicy(BasePositionPolicy):
    def __init__(
        self,
        policy_id: str = "ma_runner_position",
        *,
        risk_policy: MARunnerRiskPolicy | None = None,
        profit_management_policy: MARunnerProfitManagementPolicy | None = None,
        flatten_policy: TimeCutoffFlattenRiskPolicy | None = None,
    ) -> None:
        self.policy_id = policy_id
        self._risk_policy = risk_policy or MARunnerRiskPolicy()
        self._profit_management_policy = profit_management_policy or MARunnerProfitManagementPolicy()
        self._flatten_policy = flatten_policy or TimeCutoffFlattenRiskPolicy(policy_id="ma_runner_session_flatten")

    def build_exit_plan(self, ctx: PositionContext) -> Optional[ExitPlan]:
        for decision in (
            self._flatten_policy.evaluate(ctx),
            self._risk_policy.evaluate(ctx),
            self._profit_management_policy.evaluate(ctx),
        ):
            if decision.exit_plan is not None:
                return decision.exit_plan
        return None

    def build_exit_plans(self, ctx: PositionContext) -> list[ExitPlan]:
        runtime = ctx.runtime
        positions = list(runtime.active_positions or [])
        if not positions and runtime.active_position is not None:
            positions = [runtime.active_position]
        out: list[ExitPlan] = []
        for pos in positions:
            scoped_runtime = replace(runtime, active_position=pos, active_positions=[pos])
            plan = self.build_exit_plan(PositionContext(runtime=scoped_runtime, extras=dict(ctx.extras or {})))
            if plan is not None:
                out.append(plan)
        return out
