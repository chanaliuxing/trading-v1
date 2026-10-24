from __future__ import annotations

import inspect
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class StockStrategyManifest:
    strategy_key: str
    symbols: tuple[str, ...]
    template_id: str = "offhours_equity"
    order_namespace: str = "stkv1"
    data_profile: str = "equity_offhours_tick_only"
    policy_id: str = ""
    production_blocking: bool = True
    no_overnight: bool = True
    protection_required: bool = True
    protection_fallback: str = "flatten"
    recovery_policy_id: str = "stocks_kernel_close_only"
    recovery_unknown_owner_action: str = "quarantine"
    recovery_fallback: str = "flatten"
    platform_eod_required: bool = True
    reports_strategy_tradability_gate: bool = True
    session_allowed: tuple[str, ...] = ("PRE", "POST", "OVERNIGHT")
    data_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    gate_pipeline: tuple[str, ...] = (
        "session_window",
        "freshness",
        "open_orders_isolation",
        "foreign_position_isolation",
    )
    execution_profile: dict[str, Any] = field(default_factory=dict)
    gate_overrides: dict[str, Any] = field(default_factory=dict)
    policy_registry_specs: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def data_contract(self) -> dict[str, Any]:
        symbols = [str(item or "").strip().upper() for item in self.symbols if str(item or "").strip()]
        requirements = [dict(item) for item in self.data_requirements]
        if not requirements:
            requirements = [
                {"dataset": "ticks", "symbols": symbols, "ttl_sec": 30, "rth": False},
                {"dataset": "bars_5s", "symbols": symbols, "ttl_sec": 90, "rth": False},
            ]
        return {
            "production_blocking": bool(self.production_blocking),
            "symbols": symbols,
            "ttl_sec": 30,
            "requirements": requirements,
        }

    def production_manifest(self) -> dict[str, Any]:
        return {
            "production_blocking": bool(self.production_blocking),
            "entry_contract": {"uses_decision_pipeline": True, "uom_only": True},
            "protection_contract": {
                "required": bool(self.protection_required),
                "fallback": str(self.protection_fallback or "flatten"),
            },
            "recovery_contract": {
                "policy_id": str(self.recovery_policy_id or "stocks_kernel_close_only"),
                "unknown_owner_action": str(self.recovery_unknown_owner_action or "quarantine"),
                "fallback": str(self.recovery_fallback or "flatten"),
            },
            "eod_contract": {
                "no_overnight": bool(self.no_overnight),
                "platform_eod_required": bool(self.platform_eod_required),
            },
            "tradability_reporting": {
                "reports_strategy_tradability_gate": bool(self.reports_strategy_tradability_gate),
            },
        }

    def default_gate_overrides(self) -> dict[str, Any]:
        merged = dict(self.gate_overrides or {})
        merged.setdefault(
            "session_window",
            {"params": {"allowed_sessions": [str(item) for item in self.session_allowed]}},
        )
        merged.setdefault("open_orders_isolation", {"enabled": True})
        merged.setdefault("foreign_position_isolation", {"enabled": True})
        return merged


class BaseStockPlugin(ABC):
    """
    Compatibility facade for stocks kernel migration.

    Current stocks kernel still runs through policy/gate/module assembly.
    This facade gives new work a single plugin-shaped entrypoint while the
    underlying DSL remains in place during transition.

    Lifecycle failure policy:
    - on_init is fail-fast. A broken plugin must prevent strategy startup.
    - runtime hooks are called by the strategy on the relevant path; callers may
      choose fail-safe handling for shutdown/fill paths where preserving the
      trading runtime is more important than plugin telemetry.
    """

    component_name: str = "stock_plugin"

    async def on_init(self, *, strategy: Any, config: Any) -> Optional[Dict[str, Any]]:
        """Called once after the stocks kernel runtime is assembled."""
        return None

    async def on_features(self, *, strategy: Any, symbol: str, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Called for each feature tick. Returning a dict replaces the feature payload."""
        return None

    async def on_intent(self, *, strategy: Any, envelope: Any, symbol: str, side: str) -> Any:
        """Called before an intent is submitted. Return False to block, or a replacement envelope."""
        return envelope

    async def on_fill(self, *, strategy: Any, fill: Any, apply_result: Any = None) -> Optional[Dict[str, Any]]:
        """Called after a fill has been applied to the execution ledger."""
        return None

    async def on_shutdown(self, *, strategy: Any, reason: str) -> Optional[Dict[str, Any]]:
        """Called during strategy shutdown before audit resources are closed."""
        return None

    def policy_id(self) -> str:
        return str(getattr(self, "component_name", self.__class__.__name__) or self.__class__.__name__)

    def build_policy_payload(self, *, strategy_id: str, extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "strategy_id": str(strategy_id or ""),
            "plugin_component": str(self.component_name or self.__class__.__name__),
            "extras": dict(extras or {}),
        }

    def production_manifest(self) -> StockStrategyManifest | None:
        """Return a production contract used by config generators and validators."""
        return None

    async def maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value
