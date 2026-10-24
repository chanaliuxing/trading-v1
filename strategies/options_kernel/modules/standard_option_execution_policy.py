from __future__ import annotations

from strategies.options_kernel.execution_profile import build_execution_profile_from_extras
from strategies.options_kernel.types import ExecutionProfile

from .execution_policy import BaseExecutionPolicy, ExecutionContext


class StandardOptionExecutionPolicy(BaseExecutionPolicy):
    def __init__(
        self,
        *,
        policy_id: str = "standard_option_execution",
        default_profile_id: str = "qqq_option_ma_ext_v1",
    ) -> None:
        self.policy_id = policy_id
        self.default_profile_id = default_profile_id

    def build_execution_profile(self, ctx: ExecutionContext) -> ExecutionProfile:
        extras = dict(ctx.extras or {})
        return build_execution_profile_from_extras(
            profile_id=str(extras.get("live_parity_profile_id") or self.default_profile_id),
            market_data_profile_id=str(extras.get("market_data_profile_id") or "standard_close_extended_sma"),
            extras=extras,
        )
