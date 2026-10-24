from __future__ import annotations

from typing import Any, Dict, Optional

from strategies.stocks_kernel.plugin import BaseStockPlugin


class ExampleBreakoutStockPlugin(BaseStockPlugin):
    """
    Minimal stock plugin template used to prove the runtime plugin path.

    Initialization is intentionally fail-fast; runtime fill/shutdown handling in
    the strategy is fail-safe so telemetry bugs do not break cleanup.
    """

    component_name = "example_breakout_stock_plugin"

    async def on_init(self, *, strategy: Any, config: Any) -> Optional[Dict[str, Any]]:
        return {
            "strategy_id": str(getattr(strategy, "strategy_id", "") or ""),
            "policy_id": str(getattr(config, "policy_id", "") or ""),
        }

    async def on_features(self, *, strategy: Any, symbol: str, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        updated = dict(features or {})
        updated.setdefault("stock_plugin_component", self.component_name)
        return updated

    async def on_intent(self, *, strategy: Any, envelope: Any, symbol: str, side: str) -> Any:
        try:
            envelope.extras.setdefault("stock_plugin_component", self.component_name)
        except Exception:
            pass
        return envelope
