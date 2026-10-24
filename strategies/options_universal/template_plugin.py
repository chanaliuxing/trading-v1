from __future__ import annotations

from .plugin import OptionStrategyManifest, OptionStrategyPlugin
from .types import OptionBarContext


class TemplateOptionPlugin(OptionStrategyPlugin):
    """
    Template plugin for new option strategy logic.

    Fill only strategy-specific logic here:
    - apply_default_extras
    - maybe_enter
    - sync_and_manage_position
    - select_quote (optional)
    """

    component_name = "option_template_plugin"

    def production_manifest(self) -> OptionStrategyManifest:
        return OptionStrategyManifest(
            strategy_key="template_option",
            underlying="SPY",
            trigger_source_ids=("template_signal",),
            expiry_policy="0DTE",
            allowed_rights=("CALL", "PUT"),
            contracts=1,
            max_premium_usd=500.0,
            max_daily_loss_usd=500.0,
        )

    def apply_default_extras(self, extras: dict) -> None:
        extras.setdefault("underlying", "SPY")
        extras.setdefault("risk_symbol", "OPT:SPY:0DTE")

    async def maybe_enter(self, strategy: "UniversalOptionLiveStrategy", ctx: OptionBarContext) -> bool:
        # return False to fallback to shared default entry behavior
        return False

    async def sync_and_manage_position(self, strategy: "UniversalOptionLiveStrategy", ctx: OptionBarContext) -> bool:
        # return False to fallback to shared default position/exits behavior
        return False
