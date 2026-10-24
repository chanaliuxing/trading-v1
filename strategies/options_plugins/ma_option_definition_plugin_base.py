from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from typing import Any, Dict, Optional

from libs.strategy.base import StrategyContext, StrategyParams
from strategies.options_kernel.execution_profile import build_execution_profile_from_extras
from strategies.options_kernel.modules import ExecutionContext, PositionContext, SignalContext, definition_registry
from strategies.options_kernel.plugin import BaseOptionPlugin
from strategies.options_kernel.types import EntryPlan, ExecutionProfile, ExitPlan, PluginRuntimeState, Selection, Signal
from strategies.options_plugins.ma_1dte_reject_shared import minute_floor


class DefinitionDrivenPureOptionPlugin(BaseOptionPlugin):
    supports_pure_kernel = True
    assembly_adapter_mode = True

    def __init__(self) -> None:
        self._pure_replay_strategy = None
        self._pure_replay_strategy_id: str = ""
        self._pure_pending_setup = None
        self._pure_entry_plan: Optional[Dict[str, Any]] = None
        self._pure_last_day = None
        self._last_extras: Dict[str, Any] = {}
        self._pure_strategy_id: str = ""
        self._pure_underlying: str = ""
        self._pure_log: Any = None
        self._pure_kernel: Any = None
        self._pure_structured_observability_minute: str = ""
        self._pure_day_block_counts: Dict[str, int] = {}
        self._pure_trades_filled_today: int = 0
        self._pure_quote_health_block_day: bool = False
        self._strategy_definition: Any = None

    def _definition_id(self) -> str:
        raise NotImplementedError

    def _default_replay_strategy_id(self) -> str:
        definition = definition_registry.get(self._definition_id())
        return str(definition.extras.get("replay_strategy_id") or "").strip().lower()

    def _resolve_strategy_definition(self) -> Any:
        if self._strategy_definition is None:
            self._strategy_definition = definition_registry.get(self._definition_id())
        return self._strategy_definition

    def _definition_metadata(self) -> Dict[str, str]:
        definition = definition_registry.get(self._definition_id())
        definition_id = str(definition.definition_id or "").strip()
        audit_namespace = str(definition.audit_namespace or definition_id or "").strip()
        position_namespace = f"optk.{definition_id.lower()}" if definition_id else ""
        return {
            "definition_id": definition_id,
            "audit_namespace": audit_namespace,
            "position_namespace": position_namespace,
            "risk_symbol": str(definition.risk_symbol or "").strip(),
        }

    def _finalize_definition_extras(self, original_extras: Dict[str, Any], merged_extras: Dict[str, Any]) -> Dict[str, Any]:
        definition = self._resolve_strategy_definition()
        return definition.enforce_extra_contract(original_extras, merged_extras)

    def _get_replay_strategy_id(self) -> str:
        strategy_id = str(
            (self._last_extras or {}).get("replay_strategy_id")
            or self._pure_replay_strategy_id
            or self._default_replay_strategy_id()
        ).strip().lower()
        self._pure_replay_strategy_id = strategy_id
        return strategy_id

    async def initialize_pure(self, ctx: StrategyContext, params: StrategyParams, kernel: Any) -> None:
        original_extras = dict(params.extras or {})
        extras = self.apply_default_extras(dict(original_extras))
        self._strategy_definition = self._resolve_strategy_definition()
        meta = self._definition_metadata()
        strategy_suffix = str(ctx.strategy_id or "").strip().lower()
        instance_position_namespace = str(meta["position_namespace"] or "").strip()
        if strategy_suffix:
            instance_position_namespace = f"{instance_position_namespace}.{strategy_suffix}" if instance_position_namespace else strategy_suffix
        extras["position_namespace"] = instance_position_namespace
        extras = self._finalize_definition_extras(original_extras, extras)
        params.extras = dict(extras)
        strategy_id = str(extras.get("replay_strategy_id") or self._default_replay_strategy_id()).strip().lower()
        if strategy_id != str(getattr(self, "_pure_replay_strategy_id", "") or "").strip().lower():
            self._pure_replay_strategy = None
        self._pure_replay_strategy_id = strategy_id
        underlying = str(extras.get("underlying") or self._strategy_definition.underlying).strip().upper()
        self._pure_strategy_id = str(ctx.strategy_id)
        self._pure_underlying = underlying
        self._pure_log = getattr(ctx, "log", None)
        self._pure_kernel = kernel
        self._pure_structured_observability_minute = ""
        self._pure_day_block_counts = {}
        self._last_extras = dict(extras)
        preload_bars = max(0, int(extras.get("preload_history_bars", 260) or 260))
        if preload_bars > 0:
            try:
                rows = await ctx.history(underlying, "1m", preload_bars)
                loaded = kernel.preload_bar_history(underlying, list(rows or []))
                ctx.log.info(
                    "option_kernel_pure_preload_ok",
                    strategy_id=str(ctx.strategy_id),
                    plugin=str(self.component_name),
                    underlying=underlying,
                    resolution="1m",
                    loaded_bars=int(loaded),
                )
            except Exception as exc:
                ctx.log.warning(
                    "option_kernel_pure_preload_failed",
                    strategy_id=str(ctx.strategy_id),
                    plugin=str(self.component_name),
                    underlying=underlying,
                    resolution="1m",
                    error=str(exc),
                )
        source_bar_sec = max(5, int(extras.get("source_bar_sec", 60) or 60))
        await ctx.subscribe_features([underlying], bar_sec=source_bar_sec)
        ctx.log.info(
            "option_kernel_pure_subscribed",
            strategy_id=str(ctx.strategy_id),
            plugin=str(self.component_name),
            underlying=underlying,
            bar_sec=source_bar_sec,
        )
        snapshot = {
            "definition_id": self._definition_id(),
            "replay_strategy_id": self._get_replay_strategy_id(),
            "underlying": underlying,
            "audit_namespace": str(extras.get("audit_namespace") or ""),
            "position_namespace": str(extras.get("position_namespace") or ""),
            "risk_symbol": str(extras.get("risk_symbol") or ""),
            "signal_module_id": str(getattr(self._strategy_definition.signal_module, "module_id", "")),
            "direction_mapping_policy": str(getattr(self._strategy_definition.direction_mapping, "policy_id", "")),
            "contract_policy_id": str(getattr(self._strategy_definition.contract_policy, "policy_id", "")),
            "execution_policy_id": str(getattr(self._strategy_definition.execution_policy, "policy_id", "")),
            "position_policy_id": str(getattr(self._strategy_definition.position_policy, "policy_id", "")),
            "risk_policy_id": str(getattr(self._strategy_definition.risk_policy, "policy_id", "")),
            "profit_management_policy_id": str(getattr(self._strategy_definition.profit_management_policy, "policy_id", "")),
            "flatten_policy_id": str(getattr(self._strategy_definition.flatten_policy, "policy_id", "")),
            "kernel_mode": str(extras.get("kernel_mode") or ""),
            "ab_time_start": str(extras.get("ab_time_start") or ""),
            "ab_time_end": str(extras.get("ab_time_end") or ""),
            "entry_start_am": str(extras.get("entry_start_am") or ""),
            "entry_end_am": str(extras.get("entry_end_am") or ""),
            "entry_start_pm": str(extras.get("entry_start_pm") or ""),
            "entry_end_pm": str(extras.get("entry_end_pm") or ""),
            "no_new_entry_after": str(extras.get("no_new_entry_after") or ""),
            "force_flatten_time": str(extras.get("force_flatten_time") or ""),
            "confirm_window_bars": int(extras.get("confirm_window_bars", 2) or 2),
            "dte_preferred": int(extras.get("dte_preferred", 1) or 1),
            "dte_fallback_max": int(extras.get("dte_fallback_max", 2) or 2),
            "delta_min": float(extras.get("delta_min", 0.45) or 0.45),
            "delta_max": float(extras.get("delta_max", 0.55) or 0.55),
            "delta_fallback_min": float(extras.get("delta_fallback_min", 0.40) or 0.40),
            "delta_fallback_max": float(extras.get("delta_fallback_max", 0.60) or 0.60),
            "spread_max_pct": float(extras.get("spread_max_pct", 0.12) or 0.12),
            "min_open_interest": int(extras.get("min_open_interest", 0) or 0),
            "adaptive_entry_improve_pct": float(extras.get("adaptive_entry_improve_pct", 0.0) or 0.0),
            "urgent_entry_improve_pct": float(extras.get("urgent_entry_improve_pct", 0.0) or 0.0),
            "time_stop_soft_bars": int(extras.get("time_stop_soft_bars", 5) or 5),
            "time_stop_hard_bars": int(extras.get("time_stop_hard_bars", 10) or 10),
            "contracts": int(extras.get("contracts", 4) or 4),
        }
        ctx.log.info(
            "option_ma_strategy_snapshot",
            strategy_id=str(ctx.strategy_id),
            plugin=str(self.component_name),
            underlying=underlying,
            replay_strategy_id=self._get_replay_strategy_id(),
            config_hash=hashlib.md5(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()[:12],
            snapshot=snapshot,
        )

    def _pure_reset_if_needed(self, runtime: PluginRuntimeState) -> None:
        day = runtime.now.date()
        if self._pure_last_day != day:
            self._pure_last_day = day
            self._pure_pending_setup = None
            self._pure_entry_plan = None
            self._pure_trades_filled_today = 0
            self._pure_quote_health_block_day = False
            self._pure_day_block_counts = {}
            if self._strategy_definition is not None:
                self._strategy_definition.signal_module.reset_for_new_day(
                    SignalContext(runtime=runtime, extras=getattr(self, "_last_extras", {}) or {})
                )

    @staticmethod
    def _parse_hms(value: Any, default: time) -> time:
        raw = str(value or "").strip()
        if not raw:
            return default
        try:
            hh, mm, ss = [int(part) for part in raw.split(":")]
            return time(hh, mm, ss)
        except Exception:
            return default

    @staticmethod
    def _bars_since_setup(now: datetime, setup_ts: datetime) -> int:
        try:
            now_minute = minute_floor(now)
            setup_minute = minute_floor(setup_ts)
            delta_sec = (now_minute - setup_minute).total_seconds()
            return max(0, int(round(delta_sec / 60.0)))
        except Exception:
            return 0

    def build_execution_profile_pure(
        self,
        runtime: PluginRuntimeState,
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        if self._strategy_definition is None:
            extras = getattr(self, "_last_extras", {}) or {}
            return build_execution_profile_from_extras(
                profile_id=str((extras or {}).get("live_parity_profile_id") or ""),
                market_data_profile_id=str((extras or {}).get("market_data_profile_id") or "standard_close_extended_sma"),
                extras=extras,
            )
        return self._strategy_definition.execution_policy.build_execution_profile(
            ExecutionContext(runtime=runtime, entry_plan=entry_plan, selection=selection, extras=getattr(self, "_last_extras", {}) or {})
        )

    def on_entry_submitted_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        position_lot_id: str,
    ) -> None:
        return None

    def build_exit_plan_pure(self, runtime: PluginRuntimeState) -> Optional[ExitPlan]:
        if self._strategy_definition is None:
            return None
        return self._strategy_definition.position_policy.build_exit_plan(
            PositionContext(runtime=runtime, extras=getattr(self, "_last_extras", {}) or {})
        )

    def build_exit_plans_pure(self, runtime: PluginRuntimeState) -> list[ExitPlan]:
        if self._strategy_definition is None:
            return []
        return list(
            self._strategy_definition.position_policy.build_exit_plans(
                PositionContext(runtime=runtime, extras=getattr(self, "_last_extras", {}) or {})
            )
            or []
        )
