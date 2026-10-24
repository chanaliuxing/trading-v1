from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any, Optional

from .types import OptionBarContext, OptionQuoteRequest, OptionQuoteSelection


@dataclass(frozen=True)
class OptionStrategyManifest:
    strategy_key: str
    underlying: str
    trigger_source_ids: tuple[str, ...]
    expiry_policy: str = "0DTE"
    allowed_rights: tuple[str, ...] = ("CALL", "PUT")
    max_spread_pct: float = 0.20
    quote_ttl_sec: float = 15.0
    option_chain_source: str = "ib_option_chain"
    contracts: int = 1
    max_premium_usd: float = 500.0
    max_daily_loss_usd: float = 500.0
    entry_start: str = "09:30:00"
    entry_end: str = "15:59:00"
    no_new_entry_after: str = "15:00:00"
    force_flatten_time: str = "15:58:00"
    recovery_policy_id: str = "option_close_only"
    no_overnight: bool = True
    production_blocking: bool = True
    data_requirements: tuple[dict[str, Any], ...] = ()

    def data_contract(self) -> dict[str, Any]:
        underlying = str(self.underlying or "").strip().upper()
        requirements = [dict(item) for item in self.data_requirements]
        if not requirements:
            requirements = [
                {"dataset": "ticks", "symbols": [underlying], "ttl_sec": 15, "rth": True},
                {"dataset": "bars_5s", "symbols": [underlying], "ttl_sec": 20, "rth": True},
            ]
        return {
            "production_blocking": bool(self.production_blocking),
            "symbols": [underlying],
            "ttl_sec": 30,
            "requirements": requirements,
        }

    def production_manifest(self) -> dict[str, Any]:
        return {
            "production_blocking": bool(self.production_blocking),
            "entry_contract": {"uses_decision_pipeline": True, "uom_only": True},
            "protection_contract": {"required": True, "fallback": "flatten"},
            "recovery_contract": {
                "policy_id": str(self.recovery_policy_id or "option_close_only"),
                "unknown_owner_action": "quarantine",
                "fallback": "flatten",
            },
            "eod_contract": {
                "no_overnight": bool(self.no_overnight),
                "platform_eod_required": True,
            },
            "option_contract": {
                "required": True,
                "trigger_ledger_required": True,
                "contract_selection_required": True,
                "exact_contract_fill_attribution_required": True,
            },
            "tradability_reporting": {"reports_strategy_tradability_gate": True},
        }


class OptionStrategyPlugin(ABC):
    """
    Pluggable option strategy behavior.

    Return conventions:
    - maybe_enter / sync_and_manage_position: return True when handled.
      Returning False lets kernel fallback to shared default behavior.
    - select_quote: return OptionQuoteSelection when handled.
      Returning None lets kernel fallback to shared default behavior.
    """

    component_name: str = "option_universal_plugin"

    def apply_default_extras(self, extras: dict) -> None:
        pass

    async def on_post_initialize(self, strategy: "UniversalOptionLiveStrategy") -> None:
        pass

    async def maybe_enter(self, strategy: "UniversalOptionLiveStrategy", ctx: OptionBarContext) -> bool:
        return False

    async def sync_and_manage_position(self, strategy: "UniversalOptionLiveStrategy", ctx: OptionBarContext) -> bool:
        return False

    def select_quote(self, strategy: "UniversalOptionLiveStrategy", req: OptionQuoteRequest) -> Optional[OptionQuoteSelection]:
        return None

    def production_manifest(self) -> OptionStrategyManifest | None:
        return None


class DefaultPassThroughPlugin(OptionStrategyPlugin):
    component_name = "option_universal_default_passthrough"
