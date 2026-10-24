from __future__ import annotations

from typing import Any, Dict, Optional

from strategies.options_kernel.modules import ContractSelectionContext, SignalContext
from strategies.options_kernel.types import EntryPlan, PluginRuntimeState, Selection, Signal
from strategies.options_plugins.ma_option_definition_plugin_base import DefinitionDrivenPureOptionPlugin


class SPYIntradayMomentumOptionPlugin(DefinitionDrivenPureOptionPlugin):
    component_name = "spy_intraday_momentum_option_plugin"

    def _definition_id(self) -> str:
        return "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"

    def apply_default_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        original_extras = dict(extras or {})
        merged = dict(original_extras)
        meta = self._definition_metadata()
        merged.setdefault("definition_id", meta["definition_id"])
        merged.setdefault("audit_namespace", meta["audit_namespace"])
        merged.setdefault("underlying", "SPY")
        merged.setdefault("risk_symbol", meta["risk_symbol"] or "OPT:SPY:0DTE")
        merged.setdefault("kernel_mode", "pure_plugin")
        merged.setdefault("strict_scope_validation", True)
        merged.setdefault("enable_live_parity_snapshot", True)
        merged.setdefault("enable_live_shadow_mode", False)
        merged.setdefault("live_parity_profile_id", "spy_intraday_momentum_option_paper_v1")
        merged.setdefault("market_data_profile_id", "standard_close_extended_sma")
        merged.setdefault("contracts", 1)
        merged.setdefault("entry_start_am", "09:45:00")
        merged.setdefault("entry_end_am", "12:00:00")
        merged.setdefault("entry_start_pm", "13:00:00")
        merged.setdefault("entry_end_pm", "15:00:00")
        merged.setdefault("no_new_entry_after", "15:00:00")
        merged.setdefault("force_flatten_time", "15:50:00")
        merged.setdefault("dte_preferred", 0)
        merged.setdefault("dte_fallback_max", 1)
        merged.setdefault("delta_min", 0.30)
        merged.setdefault("delta_max", 0.55)
        merged.setdefault("delta_fallback_min", 0.25)
        merged.setdefault("delta_fallback_max", 0.60)
        merged.setdefault("spread_max_pct", 0.18)
        merged.setdefault("min_open_interest", 0)
        merged.setdefault("signal_v3m_abs_min", 0.08)
        merged.setdefault("hard_stop_atr_mult", 0.7)
        merged.setdefault("target_1r_atr_mult", 0.9)
        merged.setdefault("target_1p2r_atr_mult", 1.4)
        merged.setdefault("logic_retreat_min_hold_sec", 180)
        merged.setdefault("logic_retreat_v3m_atr_mult", 0.12)
        merged = self._finalize_definition_extras(original_extras, merged)
        self._last_extras = dict(merged)
        return merged

    def build_signal_pure(self, runtime: PluginRuntimeState) -> Optional[Signal]:
        self._pure_reset_if_needed(runtime)
        definition = self._resolve_strategy_definition()
        return definition.signal_module.build_signal(
            SignalContext(runtime=runtime, extras=dict(self._last_extras or {}))
        )

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal) -> Optional[EntryPlan]:
        definition = self._resolve_strategy_definition()
        plan = definition.signal_module.build_entry_plan(
            SignalContext(runtime=runtime, extras=dict(self._last_extras or {})),
            signal,
        )
        if plan is None:
            return None
        mapped_side = definition.direction_mapping.map_signal_side(
            type("DirectionCtx", (), {"signal": signal, "extras": dict(self._last_extras or {})})()
        )
        payload = dict(plan.payload or {})
        return EntryPlan(
            ts=plan.ts,
            side=str(mapped_side),
            signal_kind=plan.signal_kind,
            structure_stop=plan.structure_stop,
            target_1r=plan.target_1r,
            target_1p2r=plan.target_1p2r,
            r_per_share=plan.r_per_share,
            contracts=plan.contracts,
            payload=payload,
        )

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan) -> Optional[Selection]:
        definition = self._resolve_strategy_definition()
        return definition.contract_policy.select_contract(
            ContractSelectionContext(runtime=runtime, entry_plan=entry_plan, extras=dict(self._last_extras or {}))
        )
