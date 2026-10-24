from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import os
import time
from dataclasses import replace
from datetime import datetime, time as time_module
from pathlib import Path
from typing import Any, Dict

from prometheus_client import Counter, Gauge
from libs.strategy.base import DecisionEnvelope, OrderStyle
from libs.strategy import OrderUpdate, PositionSnapshot

from strategies.stocks_kernel.execution.kernel import EquityExecutionKernel
from strategies.stocks_kernel.execution.order_identity import order_ref_belongs_to_strategy
from strategies.stocks_kernel.gates.base import GateRuntimeContext
from strategies.stocks_kernel.features.registry import default_feature_registry
from strategies.stocks_kernel.assembly import KernelAssembly, KernelAssemblyProvider, legacy_kernel_assembly_provider
from strategies.stocks_kernel.gates import GatePipeline
from strategies.stocks_kernel.runtime.audit import AsyncLocalAuditWriter
from strategies.stocks_kernel.runtime.bridge_events import (
    build_broker_submit_ack_event,
    build_db_persist_event,
    build_fill_received_event,
)
from strategies.stocks_kernel.runtime.evaluator import StrategyEvaluator
from strategies.stocks_kernel.runtime.orchestrator import RuntimeOrchestrator
from strategies.stocks_kernel.runtime.stage_registry import RuntimeStageRegistry
from strategies.stocks_kernel.runtime.submit_bridge import StrategySubmitBridge
from strategies.stocks_kernel.plugin import BaseStockPlugin, StockStrategyManifest
from services.strategy_runtime_v2 import CallbackStateTransitionSink
from services.production.strategy_tradability import write_strategy_tradability_heartbeat
from services.production.scoped_tradability import (
    BlockerScopeType,
    FlowType,
    HaltClass,
    ScopedBlocker,
    TradabilityInputs,
    TradabilityScope,
    decision_to_dict,
    evaluate_tradability,
)
from strategies.shared_data import shared_underlying_data_plane
from strategies.stocks_kernel.data.contexts import build_data_context_bundle
from strategies.stocks_kernel.data.readiness import evaluate_data_readiness
from strategies.stocks_kernel.data.time_utils import ET
from strategies.stocks_kernel.data.profiles import get_data_profile
from strategies.stocks_kernel.data.providers import CtxHistoryProvider, FeatureRowSnapshotProvider
from strategies.stocks_kernel.types import ActionType, EntryPlan, ExitPlan, KernelExecutionEvent, KernelFillReport
from strategies.stocks_kernel.modules import (
    ActionArbiter,
    BrokerTruthSyncModule,
    ExecutionPriorityResolver,
    ExecutionQualityModule,
    FlattenCompletionVerifier,
    ExecutionSemanticsModule,
    OwnershipPolicy,
    OrderLifecycleModule,
    PortfolioExposurePolicy,
    ProtectionInvariantWatchdog,
    ProtectionModule,
    RecoveryModule,
    SessionModule,
)

from .base import EquityKernelStrategy
from .config_loader import KernelStrategyConfig, load_kernel_config


stocks_kernel_startup_preflight_total = Counter(
    "stocks_kernel_startup_preflight_total",
    "Stocks kernel startup preflight results",
    ["strategy_id", "result", "reason"],
)

stocks_kernel_startup_blocked_gauge = Gauge(
    "stocks_kernel_startup_blocked",
    "Whether a stocks kernel strategy instance is currently startup blocked",
    ["strategy_id"],
)

stocks_kernel_symbol_kill_switch_total = Counter(
    "stocks_kernel_symbol_kill_switch_total",
    "Stocks kernel symbol kill switch activations",
    ["strategy_id", "symbol", "reason"],
)

exec_stock_plugin_intent_blocks_total = Counter(
    "exec_stock_plugin_intent_blocks_total",
    "Stocks kernel plugin intent blocks",
    ["strategy_id", "plugin_component", "symbol", "reason"],
)


class ComposableStocksKernelStrategy(EquityKernelStrategy):
    """
    Minimal loadable strategy shell for the new stocks kernel.

    It stays isolated from live broker paths for now and is meant to prove
    config/data/gate/policy composition under the existing StrategyAlgorithm
    contract.
    """

    def __init__(self, ctx, params):
        super().__init__(ctx, params)
        self.config: KernelStrategyConfig | None = None
        self.execution: EquityExecutionKernel | None = None
        self.feature_registry = default_feature_registry()
        self.gate_registry = None
        self.policy_registry = None
        self.policy = None
        self.gate_pipeline = GatePipeline([])
        self.audit_writer: AsyncLocalAuditWriter | None = None
        self.data_profile = None
        self.history_provider = None
        self.evaluator: StrategyEvaluator | None = None
        self.submit_bridge: StrategySubmitBridge | None = None
        self.session_module: SessionModule | None = None
        self.protection_module: ProtectionModule | None = None
        self.recovery_module: RecoveryModule | None = None
        self.execution_semantics_module: ExecutionSemanticsModule | None = None
        self.action_arbiter: ActionArbiter | None = None
        self.order_lifecycle_module: OrderLifecycleModule | None = None
        self.execution_quality_module: ExecutionQualityModule | None = None
        self.broker_truth_sync_module: BrokerTruthSyncModule | None = None
        self.flatten_completion_verifier: FlattenCompletionVerifier | None = None
        self.protection_watchdog: ProtectionInvariantWatchdog | None = None
        self.ownership_policy: OwnershipPolicy | None = None
        self.portfolio_exposure_policy: PortfolioExposurePolicy | None = None
        self._last_broker_truth_sync_ts: float = 0.0
        self._broker_open_orders_snapshot: tuple[Any, ...] = ()
        self._broker_open_orders_snapshot_ts: float = 0.0
        self._broker_open_orders_refresh_task: asyncio.Task | None = None
        self._last_market_summary_minute: dict[str, int] = {}
        self._last_feature_state: dict[tuple[str, str], str] = {}
        self._last_gate_audit_state: dict[tuple[str, str], tuple[bool, str]] = {}
        self._last_non_signal_skip_minute: dict[str, int] = {}
        self._last_offsession_idle_skip_minute: dict[str, int] = {}
        self._last_no_entry_submit_audit_minute: dict[str, int] = {}
        self._last_feature_not_ready_signature: dict[str, tuple[tuple[str, str], ...]] = {}
        self._central_data_gate_active: set[tuple[str, str]] = set()
        self._startup_block_reason: str | None = None
        self._startup_block_payload: dict[str, Any] = {}
        self._startup_recovery_attempted_reason: str | None = None
        self._last_entry_eval_minute: dict[str, int] = {}
        self._symbol_kill_switch: dict[str, str] = {}
        self._last_db_sync_ts: dict[str, float] = {}
        self._submitted_entry_decisions_by_day: dict[str, set[str]] = {}
        self._completed_round_trips_by_day: dict[str, set[str]] = {}
        self._day_realized_pnl_by_day: dict[str, float] = {}
        self._entry_rearm_block_until: dict[str, float] = {}
        self._latest_symbol_snapshots: dict[str, Any] = {}
        self._supplemental_history_cache: dict[str, tuple[int, Any]] = {}
        self._last_same_strategy_pending_detail: dict[tuple[str, ...], dict[str, Any]] = {}
        self._recent_strategy_stages: dict[str, list[str]] = {}
        self._broker_truth_mismatch_streak: dict[str, int] = {}
        self._runtime_stage_timing_seq: int = 0
        self.runtime_orchestrator: RuntimeOrchestrator | None = None
        self.runtime_stage_registry: RuntimeStageRegistry | None = None
        self._runtime_stage_registry_enabled = False
        self._dynamic_gate_registry_enabled = False
        self._dynamic_policy_registry_enabled = False
        self._gate_runtime_contract_enabled = False
        self._policy_factory_runtime_enabled = False
        self._stock_plugin: BaseStockPlugin | None = None
        self._stock_plugin_component: str | None = None
        self._tradability_heartbeat_task: asyncio.Task | None = None
        self._qqq_trace_enabled = False
        self._qqq_trace_root = Path("reports/qqq_signal_snapshots")
        self._kernel_assembly: KernelAssembly | None = self._assembly_attr("kernel_assembly")
        self._kernel_assembly_provider: KernelAssemblyProvider | None = self._assembly_attr("kernel_assembly_provider")

    def _assembly_attr(self, name: str) -> Any:
        params_value = getattr(self.params, name, None)
        if params_value is not None:
            return params_value
        return getattr(self.ctx, name, None)

    def _resolve_kernel_assembly(self, *, extras: dict[str, Any]) -> KernelAssembly:
        if self._kernel_assembly is not None:
            return self._kernel_assembly
        strict_injected = bool(dict(extras or {}).get("require_injected_kernel_assembly", False))
        if strict_injected and self._kernel_assembly_provider is None:
            raise ValueError(
                "require_injected_kernel_assembly=true requires kernel_assembly or kernel_assembly_provider injection"
            )
        provider = self._kernel_assembly_provider or legacy_kernel_assembly_provider
        assembly = provider(extras=dict(extras or {}))
        if not isinstance(assembly, KernelAssembly):
            raise TypeError("kernel assembly provider must return KernelAssembly")
        self._kernel_assembly = assembly
        return assembly

    def _publish_central_data_gate(
        self,
        *,
        symbol: str,
        reason: str,
        stage: str,
        payload: dict[str, Any],
    ) -> None:
        reporter = getattr(self.ctx, "report_data_unavailable", None)
        if not callable(reporter):
            return
        symbol_s = str(symbol or "").strip().upper()
        stage_s = str(stage or "stocks_kernel_readiness").strip()
        if not symbol_s:
            return
        try:
            reporter(
                {
                    "dataset": "strategy_tradability",
                    "symbol": symbol_s,
                    "resolution": "entry",
                    "reason": str(reason or "strategy_not_tradable"),
                    "source": "stocks_kernel",
                    "strategy_id": self.strategy_id,
                    "stage": stage_s,
                    "severity": "entry_blocking",
                    "payload": dict(payload or {}),
                }
            )
            self._central_data_gate_active.add((symbol_s, stage_s))
        except Exception as exc:
            self._audit(
                stage="central_data_gate_publish_failed",
                symbol=symbol_s,
                reason=str(reason or "strategy_not_tradable"),
                payload={"error": str(exc), "gate_stage": stage_s},
            )

    def _clear_central_data_gates(self, *, symbol: str) -> None:
        clearer = getattr(self.ctx, "clear_data_unavailable", None)
        if not callable(clearer):
            return
        symbol_s = str(symbol or "").strip().upper()
        if not symbol_s:
            return
        stages = {
            stage
            for active_symbol, stage in list(self._central_data_gate_active)
            if active_symbol == symbol_s
        }
        stages.update({"required_data_missing", "feature_not_ready"})
        for stage in sorted(stages):
            active_symbol = symbol_s
            if active_symbol != symbol_s:
                continue
            try:
                clearer(
                    {
                        "dataset": "strategy_tradability",
                        "symbol": symbol_s,
                        "resolution": "entry",
                        "strategy_id": self.strategy_id,
                        "stage": stage,
                    }
                )
                self._central_data_gate_active.discard((active_symbol, stage))
            except Exception as exc:
                self._audit(
                    stage="central_data_gate_clear_failed",
                    symbol=symbol_s,
                    reason="strategy_tradability_clear_failed",
                    payload={"error": str(exc), "gate_stage": stage},
                )

    def _required_seed_features(self) -> set[str]:
        if self.config is None:
            return set()
        gate_ids = {str(item or "").strip() for item in list(self.config.gate_pipeline or [])}
        if "seed_ready" not in gate_ids:
            return set()
        override = dict((self.config.gate_overrides or {}).get("seed_ready") or {})
        if bool(override.get("enabled", True)) is False:
            return set()
        params = dict(override.get("params") or {})
        params.update({k: v for k, v in override.items() if k not in {"enabled", "params"}})
        values = params.get("required_features")
        if values is None:
            values = ["vwap", "atr_eff", "v3m"]
        return {str(item or "").strip() for item in list(values or []) if str(item or "").strip()}

    def _blocking_feature_not_ready(self, feature_not_ready: dict[str, Any]) -> dict[str, Any]:
        if not feature_not_ready:
            return {}
        required = self._required_seed_features()
        if not required:
            return {}
        return {
            str(key): value
            for key, value in dict(feature_not_ready or {}).items()
            if str(key or "").strip() in required
        }

    def _load_stock_plugin(self, *, extras: dict[str, Any]) -> BaseStockPlugin | None:
        raw_instance = extras.get("plugin_instance") or extras.get("stock_plugin_instance")
        if raw_instance is not None:
            if not isinstance(raw_instance, BaseStockPlugin):
                raise TypeError("plugin_instance must be a BaseStockPlugin")
            return raw_instance

        raw_cls = extras.get("plugin_cls") or extras.get("stock_plugin_cls") or extras.get("plugin_import_path")
        raw_cls_s = str(raw_cls or "").strip()
        if not raw_cls_s:
            return None
        module_name, sep, class_path = raw_cls_s.partition(":")
        if not sep:
            module_name, _, class_path = raw_cls_s.rpartition(".")
        if not module_name or not class_path:
            raise ValueError("plugin_cls must use 'module:ClassName' or 'module.ClassName'")
        module = importlib.import_module(module_name)
        cls: Any = module
        for part in class_path.split("."):
            cls = getattr(cls, part)
        params = dict(extras.get("plugin_params") or {})
        plugin = cls(**params)
        if not isinstance(plugin, BaseStockPlugin):
            raise TypeError("plugin_cls must instantiate BaseStockPlugin")
        return plugin

    async def _call_stock_plugin_hook(self, hook_name: str, **kwargs: Any) -> Any:
        plugin = self._stock_plugin
        if plugin is None:
            return None
        hook = getattr(plugin, hook_name, None)
        if hook is None:
            return None
        result = hook(strategy=self, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _stock_plugin_block_reason(self, *, envelope: DecisionEnvelope) -> str:
        for key in ("stock_plugin_block_reason", "plugin_block_reason", "block_reason"):
            reason = str((envelope.extras or {}).get(key) or "").strip()
            if reason:
                return reason
        plugin = self._stock_plugin
        reason = str(getattr(plugin, "last_block_reason", "") or "").strip() if plugin is not None else ""
        return reason or "blocked_by_plugin"

    def _validate_stock_plugin_manifest_contract(self, *, extras: dict[str, Any]) -> None:
        plugin = self._stock_plugin
        if plugin is None:
            return
        manifest = plugin.production_manifest()
        if manifest is None:
            return
        if not isinstance(manifest, StockStrategyManifest):
            raise TypeError("stock plugin production_manifest() must return StockStrategyManifest")
        blockers: list[str] = []
        manifest_symbols = {str(item or "").strip().upper() for item in manifest.symbols if str(item or "").strip()}
        if not manifest_symbols:
            blockers.append("manifest_symbols_missing")
        data_contract = extras.get("data_contract") if isinstance(extras.get("data_contract"), dict) else {}
        configured_symbols = {
            str(item or "").strip().upper()
            for item in list(data_contract.get("symbols") or [])
            if str(item or "").strip()
        }
        if not configured_symbols:
            configured_symbols = {
                str(extras.get("signal_symbol") or "").strip().upper(),
                *{
                    str(item.get("signal_symbol") or "").strip().upper()
                    for item in list(extras.get("universe") or [])
                    if isinstance(item, dict)
                },
            }
            configured_symbols.discard("")
        missing_symbols = sorted(manifest_symbols - configured_symbols)
        if missing_symbols:
            blockers.append(f"manifest_symbols_not_configured:{','.join(missing_symbols)}")
        production_manifest = (
            extras.get("production_manifest")
            if isinstance(extras.get("production_manifest"), dict)
            else {}
        )
        if bool(manifest.production_blocking) and not production_manifest:
            blockers.append("production_manifest_missing")
        entry = production_manifest.get("entry_contract") if isinstance(production_manifest.get("entry_contract"), dict) else {}
        if bool(manifest.production_blocking) and not bool(entry.get("uom_only")):
            blockers.append("entry_contract_not_uom_only")
        recovery = production_manifest.get("recovery_contract") if isinstance(production_manifest.get("recovery_contract"), dict) else {}
        configured_policy = str(recovery.get("policy_id") or "").strip()
        if configured_policy and configured_policy != str(manifest.recovery_policy_id or "").strip():
            blockers.append("recovery_policy_mismatch")
        eod = production_manifest.get("eod_contract") if isinstance(production_manifest.get("eod_contract"), dict) else {}
        if bool(manifest.no_overnight) and not bool(eod.get("no_overnight")):
            blockers.append("eod_no_overnight_missing")
        tradability = (
            production_manifest.get("tradability_reporting")
            if isinstance(production_manifest.get("tradability_reporting"), dict)
            else {}
        )
        if bool(manifest.reports_strategy_tradability_gate) and not bool(
            tradability.get("reports_strategy_tradability_gate")
        ):
            blockers.append("tradability_reporting_missing")
        if blockers:
            raise ValueError(f"stock_plugin_manifest_contract_invalid: {', '.join(blockers)}")

    async def initialize(self) -> None:
        self.config = load_kernel_config(self.strategy_id, self.params)
        if getattr(self.ctx, "system_health", None) is not None:
            await self.subscribe_system_health()
        self._qqq_trace_enabled = bool(self.config.raw_extras.get("qqq_trace_enabled", False))
        self._qqq_trace_root = Path(str(self.config.raw_extras.get("qqq_trace_root") or "reports/qqq_signal_snapshots"))
        forbidden_force_keys = [
            key
            for key in (
                "force_test_entry_side",
                "force_test_entry_trigger_id",
                "force_test_entry_limit_offset_ticks",
            )
            if key in dict(self.config.raw_extras or {})
        ]
        if forbidden_force_keys:
            raise ValueError(f"force_test_entry config is not supported: {', '.join(sorted(forbidden_force_keys))}")
        assembly = self._resolve_kernel_assembly(extras=dict(self.config.raw_extras or {}))
        self._stock_plugin = self._load_stock_plugin(extras=dict(self.config.raw_extras or {}))
        self._stock_plugin_component = (
            str(getattr(self._stock_plugin, "component_name", "") or self._stock_plugin.__class__.__name__)
            if self._stock_plugin is not None
            else None
        )
        self._validate_stock_plugin_manifest_contract(extras=dict(self.config.raw_extras or {}))
        self.gate_registry = assembly.gate_registry
        self.policy_registry = assembly.policy_registry
        self.runtime_stage_registry = assembly.runtime_stage_registry
        self.execution = EquityExecutionKernel(order_namespace=self.config.order_namespace)
        self.execution.set_strategy_run_id(getattr(self.ctx, "strategy_run_id", None))
        self.data_profile = get_data_profile(self.config.data_profile)
        self._runtime_stage_registry_enabled = bool(self.config.raw_extras.get("enable_runtime_stage_registry", False))
        self._dynamic_gate_registry_enabled = bool(self.config.raw_extras.get("enable_dynamic_gate_registry", False))
        self._dynamic_policy_registry_enabled = bool(self.config.raw_extras.get("enable_dynamic_policy_registry", False))
        self._gate_runtime_contract_enabled = bool(self.config.raw_extras.get("enable_gate_runtime_contract", False))
        self._policy_factory_runtime_enabled = bool(self.config.raw_extras.get("enable_policy_factory_runtime", False))
        gate_specs = list(self.config.raw_extras.get("gate_registry_specs") or [])
        if gate_specs and not self._dynamic_gate_registry_enabled:
            raise ValueError("gate_registry_specs requires enable_dynamic_gate_registry=true")
        if gate_specs:
            self.gate_registry.register_from_specs(gate_specs)
        policy_specs = list(self.config.raw_extras.get("policy_registry_specs") or [])
        if policy_specs and not self._dynamic_policy_registry_enabled:
            raise ValueError("policy_registry_specs requires enable_dynamic_policy_registry=true")
        if policy_specs:
            self.policy_registry.register_from_specs(policy_specs)
        self.policy = self.policy_registry.build(
            policy_id=self.config.policy_id,
            policy_params=dict(self.config.policy_params or {}),
            extras=dict(self.config.raw_extras or {}),
            mode="factory" if self._policy_factory_runtime_enabled else "legacy",
        )
        gates = []
        budget_usd = self._trade_budget_usd()
        gate_ids = list(self.config.gate_pipeline or [])
        for gate_id in gate_ids:
            override = dict(self.config.gate_overrides.get(gate_id) or {})
            enabled = bool(override.pop("enabled", True))
            params = dict(override.pop("params", {}) or {})
            params.update(override)
            if gate_id == "risk_budget" and budget_usd > 0:
                # Single source of truth: default risk budget to the strategy capital limit
                # unless explicitly overridden in config.
                params.setdefault("max_notional", budget_usd)
            gates.append(self.gate_registry.build(gate_id, enabled=enabled, config=params))
        self.gate_pipeline = GatePipeline(gates, fail_mode=self.config.gate_fail_mode)
        self.audit_writer = AsyncLocalAuditWriter(root_dir=self.config.audit_root_dir)
        self.execution.audit.add_sink(lambda event: self.audit_writer.write_event(domain=event.domain, event=event))
        self.history_provider = CtxHistoryProvider(ctx=self.ctx)
        self.evaluator = StrategyEvaluator(
            feature_registry=self.feature_registry,
            gate_pipeline=self.gate_pipeline,
            policy=self.policy,
            data_profile=self.data_profile,
            history_provider=self.history_provider,
            feature_ids=self.config.feature_set,
            feature_params=self.config.feature_params,
            shared_history_cache_enabled=bool(self.config.raw_extras.get("enable_shared_history_cache", False)),
        )
        self.submit_bridge = StrategySubmitBridge(
            strategy_id=self.strategy_id,
            strategy_instance_id=str(self.config.raw_extras.get("strategy_instance_id") or self.strategy_id),
            strategy_run_id=getattr(self.ctx, "strategy_run_id", None),
            ttl_ms=int(getattr(self.params, "ttl_ms", 30_000)),
            dry_run=self.config.dry_run,
            action_decision_id_v2_enabled=bool(self.config.raw_extras.get("enable_action_decision_id_v2", True)),
        )
        runtime_extra_stages = list(self.config.raw_extras.get("runtime_extra_stages") or [])
        if runtime_extra_stages and not self._runtime_stage_registry_enabled:
            raise ValueError("runtime_extra_stages requires enable_runtime_stage_registry=true")
        if runtime_extra_stages:
            self.runtime_stage_registry.register_from_specs(runtime_extra_stages)
        if self._runtime_stage_registry_enabled:
            self.runtime_orchestrator = RuntimeOrchestrator(
                stage_overrides=dict(self.config.raw_extras.get("runtime_stage_overrides") or {}),
                stage_specs=self.runtime_stage_registry.stage_specs() if self.runtime_stage_registry is not None else None,
            )
        else:
            self.runtime_orchestrator = RuntimeOrchestrator(
                stage_overrides=dict(self.config.raw_extras.get("runtime_stage_overrides") or {})
            )
        self.session_module = SessionModule(strategy=self)
        self.protection_module = ProtectionModule(strategy=self)
        self.recovery_module = RecoveryModule(strategy=self)
        self.execution_semantics_module = ExecutionSemanticsModule(strategy=self)
        self.action_arbiter = ActionArbiter(resolver=ExecutionPriorityResolver())
        self.order_lifecycle_module = OrderLifecycleModule(
            pending_timeout_sec=float(self.config.raw_extras.get("entry_pending_convergence_timeout_sec") or 60.0),
            transition_sink=CallbackStateTransitionSink(self._on_state_transition_event),
            strategy_id=self.strategy_id,
        )
        self.execution_quality_module = ExecutionQualityModule()
        self.broker_truth_sync_module = BrokerTruthSyncModule()
        self.flatten_completion_verifier = FlattenCompletionVerifier(
            timeout_sec=float(self.config.raw_extras.get("flatten_convergence_timeout_sec") or 90.0)
        )
        self.protection_watchdog = ProtectionInvariantWatchdog(
            detect_sec=float(self.config.raw_extras.get("protection_missing_detect_sec") or 10.0),
            escalate_sec=float(self.config.raw_extras.get("protection_missing_escalate_sec") or 30.0),
        )
        self.ownership_policy = OwnershipPolicy(
            default_scope=str(self.config.raw_extras.get("default_ownership_scope") or "strategy")
        )
        self.portfolio_exposure_policy = PortfolioExposurePolicy(
            gross_limit=float(self.config.raw_extras.get("portfolio_gross_limit_usd") or 0.0),
            net_limit=float(self.config.raw_extras.get("portfolio_net_limit_usd") or 0.0),
            symbol_limit=float(self.config.raw_extras.get("portfolio_symbol_limit_usd") or 0.0),
        )
        if self._stock_plugin is not None:
            hook_payload = await self._call_stock_plugin_hook("on_init", config=self.config)
            self._audit(
                stage="stock_plugin_initialized",
                symbol="SYSTEM",
                reason=str(self._stock_plugin_component or "stock_plugin"),
                payload={
                    "plugin_component": self._stock_plugin_component,
                    "plugin_cls": self.config.raw_extras.get("plugin_cls") or self.config.raw_extras.get("stock_plugin_cls"),
                    "hook_payload": dict(hook_payload or {}) if isinstance(hook_payload, dict) else hook_payload,
                },
            )
        symbols = sorted(
            {
                instrument.signal_symbol
                for instrument in self.config.instruments
            }
            | {
                instrument.long_exec_symbol
                for instrument in self.config.instruments
            }
            | {
                instrument.short_exec_symbol
                for instrument in self.config.instruments
            }
            | {
                instrument.confirm_symbol
                for instrument in self.config.instruments
                if instrument.confirm_symbol
            }
            | {
                instrument.volatility_symbol
                for instrument in self.config.instruments
                if instrument.volatility_symbol
            }
        )
        if symbols:
            try:
                await self.ctx.subscribe_features(symbols, bar_sec=self.config.subscribe_bar_sec)
            except Exception as exc:
                self._audit(
                    stage="feature_subscription_failed",
                    symbol="SYSTEM",
                    reason=str(exc),
                    payload={"symbols": symbols, "bar_sec": self.config.subscribe_bar_sec},
                )
            try:
                await self.ctx.subscribe_tick_data(symbols)
            except Exception as exc:
                self._audit(
                    stage="tick_subscription_failed",
                    symbol="SYSTEM",
                    reason=str(exc),
                    payload={"symbols": symbols},
                )
        if not self.config.dry_run and self.config.startup_preflight_enabled:
            preflight = await self._run_startup_preflight()
            if not bool(preflight.get("ok")):
                stocks_kernel_startup_preflight_total.labels(
                    strategy_id=self.strategy_id,
                    result="blocked",
                    reason=str(preflight.get("reason") or "startup_preflight_failed"),
                ).inc()
                stocks_kernel_startup_blocked_gauge.labels(strategy_id=self.strategy_id).set(1)
                self._startup_block_reason = str(preflight.get("reason") or "startup_preflight_failed")
                self._startup_block_payload = dict(preflight)
                self._audit(
                    stage="startup_blocked",
                    symbol="SYSTEM",
                    reason=self._startup_block_reason,
                    payload=dict(preflight),
                )
                return
            stocks_kernel_startup_preflight_total.labels(
                strategy_id=self.strategy_id,
                result="passed",
                reason="ready",
            ).inc()
            stocks_kernel_startup_blocked_gauge.labels(strategy_id=self.strategy_id).set(0)
        else:
            stocks_kernel_startup_blocked_gauge.labels(strategy_id=self.strategy_id).set(0)
        self._audit(
            stage="dynamic_registry_startup_summary",
            symbol="SYSTEM",
            reason="startup_registry_summary",
            payload={
                "enable_dynamic_gate_registry": self._dynamic_gate_registry_enabled,
                "enable_dynamic_policy_registry": self._dynamic_policy_registry_enabled,
                "enable_runtime_stage_registry": self._runtime_stage_registry_enabled,
                "gate_registry_specs_count": len(gate_specs),
                "policy_registry_specs_count": len(policy_specs),
                "runtime_extra_stages_count": len(runtime_extra_stages),
                "registered_gate_count": self._registered_gate_count(),
                "registered_policy_count": self._registered_policy_count(),
                "registered_stage_count": self._registered_stage_count(),
            },
        )
        self._audit(
            stage="startup_self_check",
            symbol="SYSTEM",
            reason="ready",
            payload={
                "order_namespace": self.config.order_namespace,
                "data_profile": self.config.data_profile,
                "gate_count": len(self.config.gate_pipeline),
                "feature_count": len(self.config.feature_set),
                "dry_run": self.config.dry_run,
                "startup_preflight_enabled": self.config.startup_preflight_enabled,
                "runtime_stages": [stage.stage_id for stage in self.runtime_orchestrator.build_plan().stages] if self.runtime_orchestrator is not None else [],
                "ownership_scope": str(self.config.raw_extras.get("default_ownership_scope") or "strategy"),
            },
        )
        self._audit(
            stage="strategy_initialized",
            symbol="SYSTEM",
            reason="ready",
            payload={"symbols": symbols, "dry_run": self.config.dry_run, "policy_id": self.config.policy_id},
        )
        if self._strategy_tradability_reporting_enabled() and (
            self._tradability_heartbeat_task is None or self._tradability_heartbeat_task.done()
        ):
            self._tradability_heartbeat_task = asyncio.create_task(
                self._run_strategy_tradability_heartbeat_loop(),
                name=f"stocks_kernel_tradability:{self.strategy_id}",
            )

    def _registered_gate_count(self) -> int:
        if self.gate_registry is None:
            return 0
        known = getattr(self.gate_registry, "known_gate_ids", None)
        if callable(known):
            try:
                return len(list(known()))
            except Exception:
                return 0
        return 0

    def _strategy_tradability_reporting_enabled(self) -> bool:
        if bool(getattr(self, "_system_health_subscribed", False)):
            return True
        extras = dict(getattr(self.config, "raw_extras", {}) or {}) if self.config is not None else {}
        if bool(extras.get("production_blocking", False)):
            return True
        reporting = extras.get("tradability_reporting") if isinstance(extras.get("tradability_reporting"), dict) else {}
        if bool((reporting or {}).get("reports_strategy_tradability_gate", False)):
            return True
        contract = extras.get("data_contract") if isinstance(extras.get("data_contract"), dict) else {}
        return bool((contract or {}).get("production_blocking", False))

    def _registered_policy_count(self) -> int:
        if self.policy_registry is None:
            return 0
        total = 0
        for attr in ("_factories", "_legacy_builders", "_legacy_instances"):
            value = getattr(self.policy_registry, attr, None)
            if isinstance(value, dict):
                total += len(value)
        return total

    def _registered_stage_count(self) -> int:
        if self.runtime_stage_registry is None:
            return 0
        stage_specs = getattr(self.runtime_stage_registry, "stage_specs", None)
        if callable(stage_specs):
            try:
                return len(tuple(stage_specs()))
            except Exception:
                return 0
        return 0

    async def on_shutdown(self, reason: str) -> None:
        if self._tradability_heartbeat_task is not None:
            self._tradability_heartbeat_task.cancel()
            try:
                await self._tradability_heartbeat_task
            except asyncio.CancelledError:
                pass
            self._tradability_heartbeat_task = None
        try:
            await self._call_stock_plugin_hook("on_shutdown", reason=str(reason or "unknown"))
        except Exception as exc:
            self._audit(
                stage="stock_plugin_shutdown_failed",
                symbol="SYSTEM",
                reason=str(exc),
                payload={"plugin_component": self._stock_plugin_component},
            )
        if self.audit_writer is not None:
            try:
                self.audit_writer.flush(timeout_sec=1.5)
            except Exception:
                pass
            try:
                self.audit_writer.close(timeout_sec=1.5)
            except Exception:
                pass
        self.audit_writer = None

    def _allows_paper_degraded_entry(self) -> bool:
        if str(os.getenv("TRADING_MODE", "")).strip().lower() != "paper":
            return False
        extras = dict(getattr(getattr(self, "config", None), "raw_extras", {}) or {})
        return bool(extras.get("paper_smoke_only")) and bool(extras.get("allow_system_health_degraded_entry"))

    async def on_system_paused(self, event: Dict[str, Any]) -> None:
        event_type = str((event or {}).get("event_type") or "").strip().lower()
        if event_type in {"system_recovering", "system_degraded"} and self._allows_paper_degraded_entry():
            self._system_paused = False
            return
        await super().on_system_paused(event)

    @staticmethod
    def _production_state_hard_pause_blockers(production_state: Dict[str, Any]) -> tuple[str, ...]:
        hard_tokens = (
            "broker_truth_missing",
            "broker_truth_stale",
            "broker_truth_not_broker_backed",
            "position_safety_no_go",
            "position_safety_stale",
            "runtime_version_loaded_code_stale",
            "manual_recovery_required",
            "order_truth_unreconciled",
            "ledger_broker_divergence",
            "safety_kernel_global_halt",
            "global_halt",
        )
        blockers: list[str] = []
        for failure in list((production_state or {}).get("failures") or []):
            if not isinstance(failure, dict):
                continue
            text = " ".join(
                str(failure.get(key) or "")
                for key in ("issue_key", "detail", "error_type", "reason", "service")
            ).lower()
            for token in hard_tokens:
                if token in text and token not in blockers:
                    blockers.append(token)
        for blocker in list((production_state or {}).get("blockers") or []):
            text = str(blocker or "").strip().lower()
            for token in hard_tokens:
                if token in text and token not in blockers:
                    blockers.append(token)
        return tuple(blockers)

    def _system_pause_resume_blockers_from_production_state(self) -> tuple[str, ...]:
        production_state = self._read_runtime_json("runtime/readiness/production_state/production_state_latest.json")
        if not production_state:
            return ("production_state_missing",)
        if bool(production_state.get("global_halt")):
            return ("safety_kernel_global_halt",)
        go_no_go = str(production_state.get("go_no_go") or "").strip().lower()
        if go_no_go != "go":
            return (f"production_state_{go_no_go or 'unknown'}",)
        safety_go_no_go = str(production_state.get("safety_go_no_go") or "").strip().lower()
        if safety_go_no_go != "go":
            return (f"safety_go_no_go_{safety_go_no_go or 'unknown'}",)
        hard_blockers = self._production_state_hard_pause_blockers(production_state)
        if hard_blockers:
            return hard_blockers
        return ()

    def _system_pause_can_resume_from_tradability(self) -> bool:
        return len(self._system_pause_resume_blockers_from_production_state()) == 0

    def _entry_tradability_decision_while_paused(self, *, symbol: str, session: str) -> dict[str, Any]:
        extras = dict(getattr(getattr(self, "config", None), "raw_extras", {}) or {})
        account_id = str(extras.get("account_id") or extras.get("account") or "").strip()
        strategy_instance_id = str(extras.get("strategy_instance_id") or self.strategy_id or "").strip()
        scope = TradabilityScope(
            account_id=account_id,
            strategy_id=str(self.strategy_id or ""),
            strategy_instance_id=strategy_instance_id,
            symbol=str(symbol or "").strip().upper(),
            asset_type=str(extras.get("asset_type") or "STK"),
            session=str(session or "RTH").strip().upper() or "RTH",
            flow_type=FlowType.ENTRY,
            order_role=FlowType.ENTRY.value,
        )
        resume_blockers = self._system_pause_resume_blockers_from_production_state()
        if not resume_blockers:
            decision = evaluate_tradability(scope, TradabilityInputs())
            payload = decision_to_dict(decision)
            payload["reason"] = "system_pause_latch_released_by_production_state"
            payload["warnings"] = tuple(list(decision.warnings) + ["system_pause_latch_released"])
            return payload
        if any(
            blocker in {
                "global_halt",
                "broker_truth_missing",
                "broker_truth_stale",
                "broker_truth_not_broker_backed",
                "position_safety_no_go",
                "position_safety_stale",
                "runtime_version_loaded_code_stale",
                "manual_recovery_required",
                "order_truth_unreconciled",
                "ledger_broker_divergence",
                "safety_kernel_global_halt",
            }
            for blocker in resume_blockers
        ):
            return decision_to_dict(
                evaluate_tradability(scope, TradabilityInputs(global_blockers=resume_blockers))
            )
        blocker = ScopedBlocker(
            blocker="system_paused",
            scope_type=BlockerScopeType.STRATEGY_INSTANCE,
            account_id=account_id or None,
            strategy_id=str(self.strategy_id or ""),
            strategy_instance_id=strategy_instance_id,
        )
        decision = decision_to_dict(evaluate_tradability(scope, TradabilityInputs(scoped_blockers=(blocker,))))
        decision["blockers"] = tuple(dict.fromkeys(list(decision.get("blockers") or ()) + list(resume_blockers)))
        decision["halt_class"] = HaltClass.SCOPED_BLOCK.value
        return decision

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        symbol_u = str(symbol).strip().upper()
        if self._system_paused:
            session_u = str((features or {}).get("session") or "").strip().upper()
            tradability_decision = self._entry_tradability_decision_while_paused(
                symbol=symbol_u,
                session=session_u,
            )
            if bool(tradability_decision.get("allow")):
                self._system_paused = False
                self._audit(
                    stage="system_pause_latch_released_by_tradability",
                    symbol=symbol_u,
                    reason=str(tradability_decision.get("reason") or "tradability_allowed"),
                    payload={"tradability_decision": tradability_decision},
                )
            else:
                self._remember_latest_snapshot(symbol=symbol_u, features=features)
                self._publish_shared_underlying_data(symbol=symbol_u, features=features)
                try:
                    await self._run_safety_invariants_while_paused(symbol_u=symbol_u, features=dict(features or {}))
                except Exception as exc:
                    self._audit(
                        stage="paused_safety_invariant_check_failed",
                        symbol=symbol_u,
                        reason=str(exc),
                        payload={},
                    )
                if self.config is not None and self.evaluator is not None:
                    try:
                        await self._refresh_data_gates_while_paused(symbol_u=symbol_u, features=dict(features or {}))
                    except Exception as exc:
                        self._audit(
                            stage="paused_data_gate_refresh_failed",
                            symbol=symbol_u,
                            reason=str(exc),
                            payload={},
                        )
                    try:
                        await self._release_recoverable_kill_switches_while_paused(
                            symbol_u=symbol_u,
                            features=dict(features or {}),
                        )
                    except Exception as exc:
                        self._audit(
                            stage="paused_kill_switch_release_check_failed",
                            symbol=symbol_u,
                            reason=str(exc),
                            payload={},
                        )
                self._emit_strategy_tradability_heartbeat(
                    symbol=symbol_u,
                    session=session_u,
                    blockers=["system_paused"],
                    reason="tradability_decision_denied",
                )
                self._audit(
                    stage="tradability_entry_denied_while_system_paused",
                    symbol=symbol_u,
                    reason=str(tradability_decision.get("reason") or "system_paused"),
                    payload={"tradability_decision": tradability_decision},
                )
                return
        if self.config is None or self.policy is None or self.evaluator is None or self.submit_bridge is None:
            raise RuntimeError("strategy not initialized")
        plugin_features = await self._call_stock_plugin_hook(
            "on_features",
            symbol=symbol_u,
            features=dict(features or {}),
        )
        if isinstance(plugin_features, dict):
            features = dict(plugin_features)
            self._audit(
                stage="stock_plugin_features_applied",
                symbol=symbol_u,
                reason=str(self._stock_plugin_component or "stock_plugin"),
                payload={"plugin_component": self._stock_plugin_component},
            )
        self._remember_latest_snapshot(symbol=symbol_u, features=features)
        self._publish_shared_underlying_data(symbol=symbol_u, features=features)
        instrument = self._instrument_for_signal_symbol(symbol_u)
        exec_instrument = self._instrument_for_exec_symbol(symbol_u)
        session_u = str(features.get("session") or "").strip().upper()
        runtime_state: dict[str, Any] = {
            "symbol": symbol_u,
            "features": features,
            "instrument": instrument,
            "exec_instrument": exec_instrument,
            "runtime_instrument": instrument or exec_instrument,
            "session": session_u,
        }
        if self._should_short_circuit_signal_offsession_tick(
            session=session_u,
            instrument=instrument,
        ):
            runtime_state["snapshot"] = FeatureRowSnapshotProvider.build_snapshot(
                symbol=symbol_u,
                features=dict(features),
            )
            self._audit_offsession_idle_short_circuit(
                symbol=symbol_u,
                ts_epoch_s=float(features.get("ts_epoch_s") or 0.0),
                session=session_u,
            )
            self._emit_strategy_tradability_heartbeat(
                symbol=symbol_u,
                session=session_u,
                reason="offsession_housekeeping",
            )
            await self._run_offsession_housekeeping_runtime_stages(runtime_state=runtime_state)
            return
        if self._should_short_circuit_non_signal_tick(
            symbol=symbol_u,
            instrument=instrument,
            exec_instrument=exec_instrument,
        ):
            self._audit(
                stage="startup_recovered_non_signal_tick",
                symbol=symbol_u,
                reason="startup_recovered",
                payload={"startup_block_reason": self._startup_recovery_attempted_reason},
            )
            self._audit_non_signal_skip(symbol=symbol_u, ts_epoch_s=float(features.get("ts_epoch_s") or 0.0))
            return
        plan = self.runtime_orchestrator.build_plan() if self.runtime_orchestrator is not None else None
        runtime_state["runtime_plan"] = plan
        stage_ids = [stage.stage_id for stage in (plan.stages if plan is not None else ())]
        if not stage_ids:
            stage_ids = list(RuntimeOrchestrator.DEFAULT_STAGES)
        for stage_id in stage_ids:
            if plan is not None:
                stage = plan.stage(stage_id)
                if stage is not None and not stage.enabled:
                    continue
            if await self._execute_runtime_stage(stage_id=stage_id, runtime_state=runtime_state):
                return

    async def _run_offsession_housekeeping_runtime_stages(self, *, runtime_state: dict[str, Any]) -> None:
        plan = self.runtime_orchestrator.build_plan() if self.runtime_orchestrator is not None else None
        runtime_state["runtime_plan"] = plan
        stage_ids = [stage.stage_id for stage in (plan.stages if plan is not None else ())]
        if not stage_ids:
            stage_ids = list(RuntimeOrchestrator.DEFAULT_STAGES)
        housekeeping_stages = {
            "runtime_recovery",
            "broker_truth_sync",
            "safety_invariants",
            "session_flatten",
        }
        for stage_id in stage_ids:
            if stage_id not in housekeeping_stages:
                continue
            if plan is not None:
                stage = plan.stage(stage_id)
                if stage is not None and not stage.enabled:
                    continue
            if await self._execute_runtime_stage(stage_id=stage_id, runtime_state=runtime_state):
                return

    async def _execute_runtime_stage(self, *, stage_id: str, runtime_state: dict[str, Any]) -> bool:
        if not self._runtime_stage_registry_enabled:
            return bool(await self._execute_runtime_stage_legacy(stage_id=stage_id, runtime_state=runtime_state))
        plan = runtime_state.get("runtime_plan")
        stage = plan.stage(stage_id) if plan is not None and hasattr(plan, "stage") else None
        metadata = dict(getattr(stage, "metadata", {}) or {})
        handler = (
            self.runtime_stage_registry.resolve_handler(stage_id=stage_id, strategy=self, metadata=metadata)
            if self.runtime_stage_registry is not None
            else None
        )
        if handler is None:
            self._audit(
                stage="runtime_stage_unknown",
                symbol=str(runtime_state.get("symbol") or ""),
                reason="unknown_runtime_stage",
                payload={"stage_id": stage_id},
            )
            return False
        runtime_state["current_stage"] = stage_id
        started = time.perf_counter()
        result = handler(runtime_state)
        if inspect.isawaitable(result):
            result = await result
        self._audit_runtime_stage_timing(
            stage_id=stage_id,
            runtime_state=runtime_state,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return bool(result)

    async def _execute_runtime_stage_legacy(self, *, stage_id: str, runtime_state: dict[str, Any]) -> bool:
        handlers = {
            "runtime_recovery": self._runtime_stage_recovery,
            "broker_truth_sync": self._runtime_stage_broker_truth_sync,
            "data_evaluation": self._runtime_stage_data_evaluation,
            "action_arbitration": self._runtime_stage_action_arbitration,
            "policy_exit_actions": self._runtime_stage_policy_exit_actions,
            "live_exit_management": self._runtime_stage_live_exit_management,
            "safety_invariants": self._runtime_stage_safety_invariants,
            "session_flatten": self._runtime_stage_session_flatten,
            "entry_submission": self._runtime_stage_entry_submission,
        }
        handler = handlers.get(stage_id)
        if handler is None:
            self._audit(
                stage="runtime_stage_unknown",
                symbol=str(runtime_state.get("symbol") or ""),
                reason="unknown_runtime_stage",
                payload={"stage_id": stage_id},
            )
            return False
        runtime_state["current_stage"] = stage_id
        started = time.perf_counter()
        result = bool(await handler(runtime_state))
        self._audit_runtime_stage_timing(
            stage_id=stage_id,
            runtime_state=runtime_state,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return result

    async def _runtime_stage_recovery(self, runtime_state: dict[str, Any]) -> bool:
        if self.recovery_module is None:
            return False
        return not await self.recovery_module.ensure_runtime_ready_for_tick(
            symbol=str(runtime_state.get("symbol") or "")
        )

    async def _runtime_stage_broker_truth_sync(self, runtime_state: dict[str, Any]) -> bool:
        symbol_u = str(runtime_state.get("symbol") or "")
        instrument = runtime_state.get("runtime_instrument")
        features = dict(runtime_state.get("features") or {})
        features = await self._inject_runtime_guards(symbol=symbol_u, instrument=instrument, features=features)
        runtime_state["features"] = features
        session_u = str(features.get("session") or "").strip().upper()
        await self._maybe_run_broker_truth_sync(
            signal_symbol=symbol_u,
            instrument=instrument,
            session=session_u,
            now_ts_epoch_s=FeatureRowSnapshotProvider.now_timestamp_s(
                features,
                default=float(datetime.now().timestamp()),
            ),
        )
        return False

    async def _release_recoverable_kill_switches_while_paused(
        self,
        *,
        symbol_u: str,
        features: Dict[str, Any],
    ) -> None:
        if not self._symbol_kill_switch:
            return
        instrument = self._instrument_for_signal_symbol(symbol_u) or self._instrument_for_exec_symbol(symbol_u)
        exec_symbols = self._runtime_exec_symbols(symbol=symbol_u, instrument=instrument)
        if not exec_symbols:
            exec_symbols = [symbol_u] if symbol_u else []
        for exec_symbol in exec_symbols:
            exec_symbol_u = str(exec_symbol or "").strip().upper()
            reason = str(self._symbol_kill_switch.get(exec_symbol_u) or "").strip()
            if not reason or not self._is_recoverable_symbol_kill_switch_reason(reason):
                continue
            await self._maybe_release_symbol_kill_switch(
                signal_symbol=symbol_u,
                exec_symbol=exec_symbol_u,
                reason=reason,
            )

    async def _runtime_stage_data_evaluation(self, runtime_state: dict[str, Any]) -> bool:
        symbol_u = str(runtime_state.get("symbol") or "")
        instrument = runtime_state.get("runtime_instrument")
        features = self._inject_planned_qty(symbol=symbol_u, features=dict(runtime_state.get("features") or {}))
        session_u = str(features.get("session") or "").strip().upper()
        bundle = await self.evaluator.evaluate(symbol=symbol_u, features=features)
        snapshot = bundle.snapshot
        history = bundle.history
        readiness = bundle.readiness
        feature_context = bundle.feature_context
        data_context_bundle = bundle.data_context_bundle
        if self._requires_data_context_bundle():
            data_context_bundle = await self._build_runtime_data_context_bundle(
                instrument=instrument,
                signal_snapshot=snapshot,
                signal_history=history,
                signal_readiness=readiness,
                signal_feature_context=feature_context,
                now_epoch_s=FeatureRowSnapshotProvider.now_timestamp_s(features, default=snapshot.ts_epoch_s),
            )
        gate_runtime = GateRuntimeContext(data_context_bundle=data_context_bundle)
        gate_report, feature_context = self._evaluate_gates_with_compat(
            symbol=snapshot.symbol,
            feature_context=feature_context,
            readiness=readiness,
            gate_runtime=gate_runtime,
        )
        signal = self.policy.build_signal(symbol=snapshot.symbol, feature_context=feature_context, gate_report=gate_report)
        action_plans_raw = self.policy.build_action_plans(signal=signal, feature_context=feature_context)
        action_plans = tuple(
            sorted(
                [item for item in list(action_plans_raw or []) if item is not None],
                key=lambda item: int(getattr(item, "priority", 100)),
            )
        )
        bundle = replace(
            bundle,
            gate_report=gate_report,
            signal=signal,
            action_candidates=action_plans,
            action_plans=action_plans,
            winning_actions=action_plans,
            blocked_actions=(),
            primary_action=(action_plans[0] if action_plans else None),
            entry_plan=self.policy.build_entry_plan(signal=signal, feature_context=feature_context),
        )
        scoped_feature_requirements = self._scoped_feature_requirements()
        if scoped_feature_requirements and data_context_bundle is not None:
            scoped_values, scoped_readiness = self.feature_registry.build_scoped_feature_overlay(
                data_context_bundle=data_context_bundle,
                scope_feature_ids=scoped_feature_requirements,
                feature_params=self.config.feature_params if self.config is not None else {},
            )
            if scoped_values:
                feature_context = replace(
                    feature_context,
                    values={**dict(feature_context.values), **dict(scoped_values)},
                    readiness={**dict(feature_context.readiness), **dict(scoped_readiness)},
                )
                gate_report, feature_context = self._evaluate_gates_with_compat(
                    symbol=snapshot.symbol,
                    feature_context=feature_context,
                    readiness=readiness,
                    gate_runtime=gate_runtime,
                )
                signal = self.policy.build_signal(
                    symbol=snapshot.symbol,
                    feature_context=feature_context,
                    gate_report=gate_report,
                )
                action_plans_raw = self.policy.build_action_plans(signal=signal, feature_context=feature_context)
                action_plans = tuple(
                    sorted(
                        [item for item in list(action_plans_raw or []) if item is not None],
                        key=lambda item: int(getattr(item, "priority", 100)),
                    )
                )
                entry_plan = self.policy.build_entry_plan(signal=signal, feature_context=feature_context)
                bundle = replace(
                    bundle,
                    feature_context=feature_context,
                    gate_report=gate_report,
                    signal=signal,
                    action_candidates=action_plans,
                    action_plans=action_plans,
                    winning_actions=action_plans,
                    blocked_actions=(),
                    primary_action=(action_plans[0] if action_plans else None),
                    entry_plan=entry_plan,
                )
                self._audit(
                    stage="scoped_features_applied",
                    symbol=snapshot.symbol,
                    reason="scoped_features_ready",
                    payload={
                        "scopes": list(scoped_feature_requirements.keys()),
                        "feature_count": len(scoped_values),
                    },
                )
        multi_scope_decision = next(
            (
                decision for decision in tuple(gate_report.decisions or ())
                if str(getattr(decision, "gate_id", "") or "").strip() == "multi_scope_readiness"
            ),
            None,
        )
        if multi_scope_decision is not None and not bool(getattr(multi_scope_decision, "allowed", True)):
            suppressed_actions = []
            retained_actions = []
            for action in action_plans:
                action_type = str(getattr(action, "action_type", "") or "").strip().upper()
                if action_type in {ActionType.ENTRY, ActionType.ADD}:
                    suppressed_actions.append(action_type)
                else:
                    retained_actions.append(action)
            if suppressed_actions:
                self._audit(
                    stage="multi_scope_gate_blocked",
                    symbol=snapshot.symbol,
                    reason=str(getattr(multi_scope_decision, "reason", "") or "multi_scope_not_ready"),
                    payload={
                        "required_scopes": list(dict(getattr(multi_scope_decision, "payload", {}) or {}).get("required_scopes") or []),
                        "missing_scopes": list(dict(getattr(multi_scope_decision, "payload", {}) or {}).get("missing_scopes") or []),
                        "not_ready_scopes": list(dict(getattr(multi_scope_decision, "payload", {}) or {}).get("not_ready_scopes") or []),
                        "suppressed_actions": suppressed_actions,
                    },
                )
                action_plans = tuple(retained_actions)
                bundle = replace(bundle, action_plans=action_plans, winning_actions=action_plans, blocked_actions=tuple())
        bundle = replace(bundle, data_context_bundle=data_context_bundle)
        if self.order_lifecycle_module is not None:
            await self._reconcile_order_lifecycle_from_db(
                exec_symbols=self._runtime_exec_symbols(
                    symbol=symbol_u,
                    instrument=instrument or exec_instrument,
                ),
                now_ts_epoch_s=float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0),
            )
            for breach in self.order_lifecycle_module.sweep_breaches(
                now_ts_epoch_s=float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0)
            ):
                self._audit(
                    stage="entry_pending_convergence_timeout",
                    symbol=breach.symbol,
                    reason=breach.reason,
                    payload={
                        "order_ref": breach.order_ref,
                        "state": breach.state,
                        "age_sec": float(breach.age_sec),
                        "timeout_sec": float(breach.timeout_sec),
                    },
                )
                self._activate_symbol_kill_switch(symbol=breach.symbol, reason=breach.reason)
        self._emit_strategy_tradability_heartbeat(
            symbol=symbol_u,
            session=session_u,
            readiness=readiness,
            reason="runtime_data_evaluation",
        )
        entry_plan = bundle.entry_plan
        add_plan = self.policy.build_add_plan(signal=signal, feature_context=feature_context)
        stop_plan = self.policy.build_stop_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context)
        take_profit_plan = self.policy.build_take_profit_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context)
        retreat_plan = self.policy.build_retreat_plan(signal=signal, feature_context=feature_context)
        exit_plans = self.policy.build_exit_plans(feature_context=feature_context)
        if not readiness.ready:
            self._publish_central_data_gate(
                symbol=snapshot.symbol,
                reason=str(readiness.reason or "required_data_missing"),
                stage="required_data_missing",
                payload={
                    "freshness_ok": readiness.freshness_ok,
                    "history_ready": readiness.history_ready,
                    "snapshot_ready": readiness.snapshot_ready,
                    "details": readiness.payload,
                },
            )
            self._audit(
                stage="required_data_missing",
                symbol=snapshot.symbol,
                reason=readiness.reason,
                payload={
                    "freshness_ok": readiness.freshness_ok,
                    "history_ready": readiness.history_ready,
                    "snapshot_ready": readiness.snapshot_ready,
                    "details": readiness.payload,
                },
            )
        feature_not_ready = {
            key: reason
            for key, reason in feature_context.readiness.items()
            if reason != "ready"
        }
        blocking_feature_not_ready = self._blocking_feature_not_ready(feature_not_ready)
        if blocking_feature_not_ready:
            self._publish_central_data_gate(
                symbol=snapshot.symbol,
                reason="feature_seed_missing" if "feature_seed_missing" in set(blocking_feature_not_ready.values()) else "feature_not_ready",
                stage="feature_not_ready",
                payload=blocking_feature_not_ready,
            )
        if readiness.ready and not blocking_feature_not_ready:
            self._clear_central_data_gates(symbol=snapshot.symbol)
        if feature_not_ready and self._should_audit_feature_not_ready(snapshot.symbol, feature_not_ready):
            self._audit(
                stage="feature_not_ready",
                symbol=snapshot.symbol,
                reason="feature_not_ready",
                payload=feature_not_ready,
            )
        self._emit_feature_state_events(snapshot=snapshot, feature_context=feature_context)
        for decision in gate_report.decisions:
            self._audit_gate_decision(snapshot_symbol=snapshot.symbol, decision=decision)
        runtime_state.update(
            {
                "features": features,
                "bundle": bundle,
                "data_context_bundle": data_context_bundle,
                "snapshot": snapshot,
                "readiness": readiness,
                "feature_context": feature_context,
                "gate_report": gate_report,
                "signal": signal,
                "action_plans": action_plans,
                "entry_plan": entry_plan,
                "add_plan": add_plan,
                "stop_plan": stop_plan,
                "take_profit_plan": take_profit_plan,
                "retreat_plan": retreat_plan,
                "exit_plans": exit_plans,
            }
        )
        return False

    async def _refresh_data_gates_while_paused(self, *, symbol_u: str, features: dict[str, Any]) -> None:
        if self.evaluator is None:
            return
        instrument = self._instrument_for_signal_symbol(symbol_u)
        if instrument is None:
            return
        bundle = await self.evaluator.evaluate(symbol=symbol_u, features=dict(features or {}))
        snapshot = bundle.snapshot
        readiness = bundle.readiness
        feature_context = bundle.feature_context
        feature_not_ready = {
            key: reason
            for key, reason in dict(feature_context.readiness or {}).items()
            if reason != "ready"
        }
        blocking_feature_not_ready = self._blocking_feature_not_ready(feature_not_ready)
        if not readiness.ready:
            self._publish_central_data_gate(
                symbol=snapshot.symbol,
                reason=str(readiness.reason or "required_data_missing"),
                stage="required_data_missing",
                payload={
                    "freshness_ok": readiness.freshness_ok,
                    "history_ready": readiness.history_ready,
                    "snapshot_ready": readiness.snapshot_ready,
                    "details": readiness.payload,
                    "paused_probe": True,
                },
            )
            return
        if blocking_feature_not_ready:
            self._publish_central_data_gate(
                symbol=snapshot.symbol,
                reason=(
                    "feature_seed_missing"
                    if "feature_seed_missing" in set(blocking_feature_not_ready.values())
                    else "feature_not_ready"
                ),
                stage="feature_not_ready",
                payload={**blocking_feature_not_ready, "paused_probe": True},
            )
            return
        self._clear_central_data_gates(symbol=snapshot.symbol)

    def _evaluate_gates_with_compat(
        self,
        *,
        symbol: str,
        feature_context,
        readiness,
        gate_runtime: GateRuntimeContext,
    ):
        if self._gate_runtime_contract_enabled:
            report = self.gate_pipeline.evaluate(
                symbol=symbol,
                feature_context=feature_context,
                readiness=readiness,
                runtime=gate_runtime,
            )
            return report, feature_context
        # Legacy fallback: keep old magic-key injection path for gates not migrated.
        legacy_ctx = feature_context
        if gate_runtime.data_context_bundle is not None:
            legacy_ctx = replace(
                feature_context,
                values={**dict(getattr(feature_context, "values", {}) or {}), "__data_context_bundle__": gate_runtime.data_context_bundle},
            )
        report = self.gate_pipeline.evaluate(
            symbol=symbol,
            feature_context=legacy_ctx,
            readiness=readiness,
            runtime=None,
        )
        return report, legacy_ctx

    async def _runtime_stage_action_arbitration(self, runtime_state: dict[str, Any]) -> bool:
        snapshot = runtime_state.get("snapshot")
        if snapshot is None:
            return False
        instrument = runtime_state.get("runtime_instrument")
        action_plans = tuple(runtime_state.get("action_plans") or ())
        freeze_entry = self._entry_frozen_for_action_arbiter(
            signal_symbol=snapshot.symbol,
            instrument=instrument,
        )
        if self.action_arbiter is not None:
            max_entry_actions = self._max_entry_actions_per_tick()
            max_entry_total_qty = self._max_entry_total_qty_per_tick()
            arbiter = self.action_arbiter.arbitrate(
                action_plans=action_plans,
                freeze_entry=freeze_entry,
                max_entry_actions_per_tick=max_entry_actions,
                max_entry_total_qty_per_tick=max_entry_total_qty,
            )
            action_plans = arbiter.winning_actions
            if arbiter.blocked_actions:
                self._audit(
                    stage="action_arbiter_suppressed",
                    symbol=snapshot.symbol,
                    reason=arbiter.reason,
                    payload={
                        "freeze_applied": bool(arbiter.freeze_applied),
                        "highest_rank": int(arbiter.highest_rank),
                        "max_entry_actions_per_tick": max_entry_actions,
                        "max_entry_total_qty_per_tick": max_entry_total_qty,
                        "winning_actions": [str(getattr(a, "action_type", "") or "").upper() for a in arbiter.winning_actions],
                        "blocked_actions": [str(getattr(a, "action_type", "") or "").upper() for a in arbiter.blocked_actions],
                    },
                )
        action_plans, graph_blocked = self._apply_action_graph_contracts(action_plans=action_plans)
        if graph_blocked:
            self._audit(
                stage="action_graph_suppressed",
                symbol=snapshot.symbol,
                reason="action_graph_contract",
                payload={
                    "winning_actions": [str(getattr(a, "action_type", "") or "").upper() for a in action_plans],
                    "blocked_actions": [str(getattr(a, "action_type", "") or "").upper() for a in graph_blocked],
                },
            )
        feature_context = runtime_state.get("feature_context")
        signal = runtime_state.get("signal")
        entry_plans = self._entry_plans_from_action_plans(
            action_plans=action_plans,
            instrument=instrument,
            snapshot_symbol=snapshot.symbol,
            fallback=runtime_state.get("entry_plan"),
        )
        entry_plan = entry_plans[0] if entry_plans else None
        stop_plan = runtime_state.get("stop_plan")
        take_profit_plan = runtime_state.get("take_profit_plan")
        retreat_plan = runtime_state.get("retreat_plan")
        if entry_plan is not None:
            entry_plans = (entry_plan,)
        elif not entry_plans:
            entry_plans = ()
        self.kernel_state[snapshot.symbol] = {
            "readiness": runtime_state.get("readiness"),
            "feature_context": feature_context,
            "data_context_bundle": runtime_state.get("data_context_bundle"),
            "gate_report": runtime_state.get("gate_report"),
            "signal": signal,
            "action_plans": action_plans,
            "entry_plans": entry_plans,
            "entry_plan": entry_plan,
            "add_plan": runtime_state.get("add_plan"),
            "stop_plan": stop_plan,
            "take_profit_plan": take_profit_plan,
            "retreat_plan": retreat_plan,
            "exit_plans": runtime_state.get("exit_plans"),
        }
        runtime_state.update(
            {
                "signal": signal,
                "action_plans": action_plans,
                "entry_plans": entry_plans,
                "entry_plan": entry_plan,
                "stop_plan": stop_plan,
                "take_profit_plan": take_profit_plan,
                "retreat_plan": retreat_plan,
            }
        )
        return False

    def _max_entry_actions_per_tick(self) -> int | None:
        if self.config is None:
            return None
        raw = self.config.raw_extras.get("max_entry_actions_per_tick")
        try:
            value = int(raw) if raw is not None else 0
        except Exception:
            value = 0
        return value if value > 0 else None

    def _max_entry_total_qty_per_tick(self) -> float | None:
        if self.config is None:
            return None
        raw = self.config.raw_extras.get("max_entry_total_qty_per_tick")
        try:
            value = float(raw) if raw is not None else 0.0
        except Exception:
            value = 0.0
        return value if value > 0.0 else None

    def _apply_action_graph_contracts(self, *, action_plans: tuple[Any, ...]) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        rules = self._action_graph_rules()
        requires = dict(rules.get("requires") or {})
        suppresses = dict(rules.get("suppresses") or {})
        if not requires and not suppresses:
            return tuple(action_plans or ()), ()
        winners: list[Any] = []
        blocked: list[Any] = []
        for action in tuple(action_plans or ()):
            action_type = str(getattr(action, "action_type", "") or "").strip().upper()
            if not action_type:
                blocked.append(action)
                continue
            scope_matches = [item for item in winners if self._action_scope_key(item) == self._action_scope_key(action)]
            # If any already-accepted action suppresses current type on same scope, drop current.
            suppressed_by_existing = False
            for winner in scope_matches:
                winner_type = str(getattr(winner, "action_type", "") or "").strip().upper()
                suppressed_set = set(suppresses.get(winner_type) or ())
                if action_type in suppressed_set:
                    suppressed_by_existing = True
                    break
            if suppressed_by_existing:
                blocked.append(action)
                continue
            required_types = tuple(requires.get(action_type) or ())
            if required_types:
                if not any(str(getattr(item, "action_type", "") or "").strip().upper() in required_types for item in scope_matches):
                    blocked.append(action)
                    continue
            suppressed_now = set(suppresses.get(action_type) or ())
            if suppressed_now:
                retained: list[Any] = []
                for winner in winners:
                    winner_type = str(getattr(winner, "action_type", "") or "").strip().upper()
                    if self._action_scope_key(winner) == self._action_scope_key(action) and winner_type in suppressed_now:
                        blocked.append(winner)
                        continue
                    retained.append(winner)
                winners = retained
            winners.append(action)
        return tuple(winners), tuple(blocked)

    def _action_graph_rules(self) -> dict[str, dict[str, tuple[str, ...]]]:
        default_requires = {
            "ADD": ("ENTRY",),
        }
        default_suppresses = {
            "FLATTEN": ("ENTRY", "ADD", "REDUCE", "TAKE_PROFIT", "STOP", "RETREAT", "ENSURE_PROTECTION"),
        }
        raw_graph = {}
        if self.config is not None:
            raw_graph = dict(self.config.raw_extras.get("action_graph") or {})

        def _normalize_map(raw: Any) -> dict[str, tuple[str, ...]]:
            out: dict[str, tuple[str, ...]] = {}
            if not isinstance(raw, dict):
                return out
            for key, value in raw.items():
                key_u = str(key or "").strip().upper()
                if not key_u:
                    continue
                values = tuple(str(item or "").strip().upper() for item in list(value or []) if str(item or "").strip())
                if values:
                    out[key_u] = values
            return out

        requires = {**default_requires, **_normalize_map(raw_graph.get("requires"))}
        suppresses = {**default_suppresses, **_normalize_map(raw_graph.get("suppresses"))}
        return {"requires": requires, "suppresses": suppresses}

    @staticmethod
    def _action_scope_key(action: Any) -> tuple[str, ...]:
        lot_id = str(getattr(action, "position_lot_id", "") or "").strip()
        if lot_id:
            return ("lot", lot_id)
        symbol = str(getattr(action, "symbol", "") or "").strip().upper()
        lane_id = str(getattr(action, "lane_id", "") or "").strip()
        if lane_id:
            return ("lane", symbol, lane_id)
        if symbol:
            return ("symbol", symbol)
        return ("global",)

    async def _runtime_stage_policy_exit_actions(self, runtime_state: dict[str, Any]) -> bool:
        snapshot = runtime_state.get("snapshot")
        feature_context = runtime_state.get("feature_context")
        if snapshot is None or feature_context is None:
            return False
        return await self._execute_policy_exit_actions(
            action_plans=tuple(runtime_state.get("action_plans") or ()),
            instrument=runtime_state.get("runtime_instrument"),
            snapshot=snapshot,
            feature_context=feature_context,
        )

    async def _runtime_stage_live_exit_management(self, runtime_state: dict[str, Any]) -> bool:
        snapshot = runtime_state.get("snapshot")
        feature_context = runtime_state.get("feature_context")
        if snapshot is None or feature_context is None:
            return False
        await self._manage_live_exits(
            symbol=snapshot.symbol,
            instrument=runtime_state.get("runtime_instrument"),
            snapshot=snapshot,
            feature_context=feature_context,
        )
        return False

    async def _runtime_stage_safety_invariants(self, runtime_state: dict[str, Any]) -> bool:
        snapshot = runtime_state.get("snapshot")
        if snapshot is None:
            return False
        await self._check_runtime_safety_invariants(
            signal_symbol=snapshot.symbol,
            instrument=runtime_state.get("runtime_instrument"),
            snapshot=snapshot,
        )
        return False

    async def _runtime_stage_session_flatten(self, runtime_state: dict[str, Any]) -> bool:
        snapshot = runtime_state.get("snapshot")
        if snapshot is None:
            return False
        return await self._maybe_force_eod_flatten(
            symbol=snapshot.symbol,
            instrument=runtime_state.get("runtime_instrument"),
            snapshot=snapshot,
        )

    async def _runtime_stage_entry_submission(self, runtime_state: dict[str, Any]) -> bool:
        snapshot = runtime_state.get("snapshot")
        readiness = runtime_state.get("readiness")
        feature_context = runtime_state.get("feature_context")
        gate_report = runtime_state.get("gate_report")
        signal = runtime_state.get("signal")
        entry_plan = runtime_state.get("entry_plan")
        stop_plan = runtime_state.get("stop_plan")
        take_profit_plan = runtime_state.get("take_profit_plan")
        retreat_plan = runtime_state.get("retreat_plan")
        instrument = runtime_state.get("instrument")
        if snapshot is None or readiness is None or feature_context is None or gate_report is None or signal is None:
            return False
        self._emit_market_summary(
            snapshot=snapshot,
            readiness=readiness,
            feature_context=feature_context,
            gate_report=gate_report,
            signal=signal,
            entry_plan=entry_plan,
        )
        self._audit(
            stage="signal_evaluated",
            symbol=snapshot.symbol,
            reason=signal.action,
            payload={
                "signal_side": signal.side,
                "blocked_by": gate_report.blocked_by,
                "dry_run": self.config.dry_run,
                "fail_mode": gate_report.fail_mode,
            },
        )
        entry_plans = tuple(runtime_state.get("entry_plans") or ((entry_plan,) if entry_plan is not None else ()))
        self._persist_qqq_evaluation_trace(
            snapshot=snapshot,
            signal=signal,
            entry_plan=(entry_plans[0] if entry_plans else entry_plan),
        )
        if instrument is None or not entry_plans:
            self._audit_entry_not_submitted(
                snapshot=snapshot,
                signal=signal,
                gate_report=gate_report,
                action_plans=tuple(runtime_state.get("action_plans") or ()),
                reason=("instrument_unmapped" if instrument is None else "no_entry_plan"),
            )
            return instrument is None
        if self._entry_check_on_new_minute_only():
            ts_epoch_s = float(snapshot.ts_epoch_s or 0.0)
            minute_key = int(ts_epoch_s // 60) if ts_epoch_s > 0.0 else -1
            if minute_key >= 0:
                prev_minute = self._last_entry_eval_minute.get(snapshot.symbol)
                if prev_minute == minute_key:
                    self._audit(
                        stage="entry_blocked_same_minute",
                        symbol=snapshot.symbol,
                        reason="entry_check_on_new_minute_only",
                        payload={
                            "signal_side": signal.side,
                            "minute_key": minute_key,
                            "ts_epoch_s": ts_epoch_s,
                        },
                    )
                    return True
                self._last_entry_eval_minute[snapshot.symbol] = minute_key
        session_decision = self.session_module.evaluate_new_entry_window(
            ts_epoch_s=float(snapshot.ts_epoch_s or 0.0),
            signal_side=str(signal.side or ""),
        ) if self.session_module is not None else None
        if session_decision is not None and not session_decision.allowed:
            self._audit(
                stage="entry_blocked_time_cutoff",
                symbol=snapshot.symbol,
                reason=str(session_decision.reason),
                payload=dict(session_decision.payload),
            )
            return True
        trade_limit_hit = False
        submitted_any = False
        entry_conflict_baseline_lots: list[Any] | None = None
        baseline_pending_entry_count, baseline_pending_entry_symbols = self._pending_entry_stats_for_strategy()
        try:
            baseline_same_strategy_pending_orders = int(
                getattr(feature_context, "values", {}).get("same_strategy_pending_orders") or 0
            )
        except Exception:
            baseline_same_strategy_pending_orders = 0
        order_lifecycle_baseline_by_symbol: dict[str, tuple[int, tuple[str, ...]]] = {}
        for raw_plan in entry_plans:
            rewritten_plan = self._rewrite_entry_plan_symbol(entry_plan=raw_plan, instrument=instrument)
            baseline_symbol = str(getattr(rewritten_plan, "symbol", "") or "").strip().upper()
            if not baseline_symbol or baseline_symbol in order_lifecycle_baseline_by_symbol:
                continue
            order_lifecycle_baseline_by_symbol[baseline_symbol] = self._order_lifecycle_pending_stats(
                exec_symbols=[baseline_symbol]
            )
        for action_idx, raw_plan in enumerate(entry_plans, start=1):
            max_trades_per_day = int(self.config.policy_params.get("max_trades_per_day") or 0)
            if max_trades_per_day > 0:
                submitted_today = self._submitted_entry_count(ts_epoch_s=float(snapshot.ts_epoch_s or 0.0))
                if submitted_today >= max_trades_per_day:
                    trade_limit_hit = True
                    self._audit(
                        stage="entry_blocked_trade_limit",
                        symbol=snapshot.symbol,
                        reason="max_trades_per_day",
                        payload={
                            "signal_side": signal.side,
                            "submitted_today": submitted_today,
                            "max_trades_per_day": max_trades_per_day,
                            "action_index": action_idx,
                            "action_total": len(entry_plans),
                        },
                    )
                    break
            entry_plan = self._rewrite_entry_plan_symbol(entry_plan=raw_plan, instrument=instrument)
            permission = self.policy.evaluate_entry_permission(
                signal=signal,
                feature_context=feature_context,
                runtime_state=self._entry_policy_runtime_state(
                    ts_epoch_s=float(snapshot.ts_epoch_s or 0.0),
                    signal_symbol=snapshot.symbol,
                    exec_symbol=str(entry_plan.symbol or "").strip().upper(),
                    action_index=action_idx,
                    action_total=len(entry_plans),
                    feature_context=feature_context,
                    baseline_pending_entry_count=baseline_pending_entry_count,
                    baseline_pending_entry_symbols=baseline_pending_entry_symbols,
                    baseline_same_strategy_pending_orders=baseline_same_strategy_pending_orders,
                    baseline_order_lifecycle=order_lifecycle_baseline_by_symbol.get(
                        str(entry_plan.symbol or "").strip().upper(),
                        (0, ()),
                    ),
                ),
            ) if self.policy is not None else None
            if permission is not None and not bool(getattr(permission, "allowed", False)):
                self._audit(
                    stage="entry_blocked_policy_runtime",
                    symbol=entry_plan.symbol,
                    reason=str(getattr(permission, "reason", "") or "policy_runtime_blocked"),
                    payload={
                        "trigger_id": entry_plan.trigger_id,
                        "signal_symbol": snapshot.symbol,
                        "exec_symbol": entry_plan.symbol,
                        "action_index": action_idx,
                        "action_total": len(entry_plans),
                        **dict(getattr(permission, "payload", {}) or {}),
                    },
                )
                continue
            rearm_until = float(self._entry_rearm_block_until.get(str(entry_plan.symbol or "").strip().upper()) or 0.0)
            now_ts = float(snapshot.ts_epoch_s or 0.0)
            if rearm_until > 0.0 and now_ts > 0.0 and now_ts < rearm_until:
                self._audit(
                    stage="entry_blocked_rearm_cooldown",
                    symbol=entry_plan.symbol,
                    reason="entry_rearm_cooldown",
                    payload={
                        "trigger_id": entry_plan.trigger_id,
                        "signal_symbol": snapshot.symbol,
                        "exec_symbol": entry_plan.symbol,
                        "blocked_until_epoch_s": rearm_until,
                        "remaining_sec": max(0.0, rearm_until - now_ts),
                        "action_index": action_idx,
                        "action_total": len(entry_plans),
                    },
                )
                continue
            await self._reap_stale_pending_entries(
                instrument=instrument,
                now_ts_epoch_s=float(snapshot.ts_epoch_s or 0.0),
            )
            exec_snapshot = await self._ensure_exec_symbol_snapshot(exec_symbol=entry_plan.symbol)
            if exec_snapshot is None or getattr(exec_snapshot, "last_price", None) is None:
                self._audit(
                    stage="exec_symbol_snapshot_missing",
                    symbol=entry_plan.symbol,
                    reason="missing_exec_symbol_snapshot",
                    payload={
                        "trigger_id": entry_plan.trigger_id,
                        "signal_symbol": snapshot.symbol,
                        "exec_symbol": entry_plan.symbol,
                        "require_exec_symbol_snapshot": bool(
                            self.config.raw_extras.get("require_exec_symbol_snapshot") if self.config is not None else False
                        ),
                        "action_index": action_idx,
                        "action_total": len(entry_plans),
                    },
                )
                if self.config is not None and bool(self.config.raw_extras.get("require_exec_symbol_snapshot")):
                    continue
            entry_plan = self._maybe_apply_trade_budget(entry_plan=entry_plan, signal_symbol=snapshot.symbol)
            active_binding_lots = self._account_binding_lots(instrument=instrument)
            if entry_conflict_baseline_lots is None:
                # Freeze conflict-view lots at first action evaluation so same-tick
                # action-set submissions are not self-blocked by newly opened pending lots.
                entry_conflict_baseline_lots = list(active_binding_lots)
            if self.ownership_policy is not None:
                ownership = self.ownership_policy.allow_entry(
                    strategy_id=self.strategy_id,
                    lane_id=getattr(entry_plan, "lane_id", None),
                    symbol=entry_plan.symbol,
                    active_lots=tuple(active_binding_lots),
                )
                if not ownership.allowed:
                    self._audit(
                        stage="entry_blocked_ownership",
                        symbol=entry_plan.symbol,
                        reason=ownership.reason,
                        payload={
                            **dict(ownership.payload or {}),
                            "action_index": action_idx,
                            "action_total": len(entry_plans),
                        },
                    )
                    continue
            if self.portfolio_exposure_policy is not None:
                portfolio_decision = self.portfolio_exposure_policy.evaluate(
                    current_positions=tuple(self._portfolio_position_views(instrument=instrument)),
                    candidate_symbol=entry_plan.symbol,
                    candidate_qty=float(entry_plan.qty or 0.0),
                    candidate_price=getattr(exec_snapshot, "last_price", None),
                )
                if not portfolio_decision.allowed:
                    self._audit(
                        stage="entry_blocked_portfolio_exposure",
                        symbol=entry_plan.symbol,
                        reason=portfolio_decision.reason,
                        payload={
                            "gross_exposure": portfolio_decision.gross_exposure,
                            "net_exposure": portfolio_decision.net_exposure,
                            "action_index": action_idx,
                            "action_total": len(entry_plans),
                            **dict(portfolio_decision.payload),
                        },
                    )
                    continue
            kill_reason = self._symbol_kill_switch.get(entry_plan.symbol)
            if kill_reason:
                released = await self._maybe_release_symbol_kill_switch(
                    signal_symbol=snapshot.symbol,
                    exec_symbol=entry_plan.symbol,
                    reason=kill_reason,
                )
                if released:
                    kill_reason = self._symbol_kill_switch.get(entry_plan.symbol)
            if kill_reason:
                self._audit(
                    stage="symbol_kill_switch_blocked",
                    symbol=entry_plan.symbol,
                    reason=kill_reason,
                    payload={
                        "trigger_id": entry_plan.trigger_id,
                        "signal_symbol": snapshot.symbol,
                        "exec_symbol": entry_plan.symbol,
                        "action_index": action_idx,
                        "action_total": len(entry_plans),
                    },
                )
                continue
            conflict = (
                self.execution_semantics_module.evaluate_entry_conflict(
                    entry_symbol=entry_plan.symbol,
                    active_binding_lots=entry_conflict_baseline_lots if entry_conflict_baseline_lots is not None else active_binding_lots,
                    strategy_id=self.strategy_id,
                    entry_lane_id=getattr(entry_plan, "lane_id", None),
                    entry_ownership_scope=str(getattr(entry_plan, "ownership_scope", "strategy") or "strategy"),
                )
                if self.execution_semantics_module is not None
                else None
            )
            if conflict is not None and conflict.blocked:
                self._audit(
                    stage="entry_blocked_active_binding_lot",
                    symbol=entry_plan.symbol,
                    reason=conflict.reason,
                    payload={
                        "trigger_id": entry_plan.trigger_id,
                        "signal_symbol": snapshot.symbol,
                        "exec_symbol": entry_plan.symbol,
                        "action_index": action_idx,
                        "action_total": len(entry_plans),
                        **dict(conflict.payload),
                    },
                )
                continue
            self._audit(
                stage="entry_plan_built",
                symbol=snapshot.symbol,
                reason=entry_plan.side,
                payload={
                    "qty": entry_plan.qty,
                    "trigger_id": entry_plan.trigger_id,
                    "payload": entry_plan.payload,
                    "exec_symbol": entry_plan.symbol,
                    "action_index": action_idx,
                    "action_total": len(entry_plans),
                    "stop_plan": stop_plan.payload if stop_plan is not None else None,
                    "take_profit_plan": take_profit_plan.payload if take_profit_plan is not None else None,
                    "retreat_plan": retreat_plan.payload if retreat_plan is not None else None,
                },
            )
            self._persist_qqq_trace(
                snapshot=snapshot,
                signal=signal,
                entry_plan=entry_plan,
                action_index=action_idx,
                action_total=len(entry_plans),
                snapshot_kind="entry",
            )
            if self.config.dry_run:
                continue
            envelope = self.submit_bridge.build_envelope(symbol=entry_plan.symbol, entry_plan=entry_plan, snapshot=snapshot)
            envelope.extras["signal_symbol"] = snapshot.symbol
            envelope.extras["intent_action"] = "entry"
            envelope.extras["uom_intent_kind"] = "entry"
            envelope.extras.update(self._execution_session_extras(snapshot=snapshot))
            self._audit(
                stage="intent_ready",
                symbol=entry_plan.symbol,
                reason=entry_plan.side,
                payload={
                    "decision_id": envelope.decision_id,
                    "target_qty": envelope.target_qty,
                    "action_index": action_idx,
                    "action_total": len(entry_plans),
                },
            )
            await self._submit_intent(envelope=envelope, symbol=entry_plan.symbol, side=entry_plan.side)
            submitted_any = True
        return submitted_any or trade_limit_hit

    def _audit_entry_not_submitted(
        self,
        *,
        snapshot: Any,
        signal: Any,
        gate_report: Any,
        action_plans: tuple[Any, ...],
        reason: str,
    ) -> None:
        ts_epoch_s = float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0)
        minute_key = int(ts_epoch_s // 60) if ts_epoch_s > 0.0 else -1
        symbol = str(getattr(snapshot, "symbol", "") or "").strip().upper()
        if minute_key >= 0 and self._last_no_entry_submit_audit_minute.get(symbol) == minute_key:
            return
        if minute_key >= 0:
            self._last_no_entry_submit_audit_minute[symbol] = minute_key
        signal_payload = dict(getattr(signal, "payload", {}) or {})
        signal_reason = str(
            signal_payload.get("reason")
            or signal_payload.get("blocked_by")
            or getattr(signal, "action", "")
            or reason
        )
        self._audit(
            stage="entry_not_submitted",
            symbol=symbol,
            reason=str(reason or signal_reason or "no_entry_plan"),
            payload={
                "signal_action": str(getattr(signal, "action", "") or ""),
                "signal_side": str(getattr(signal, "side", "") or ""),
                "signal_reason": signal_reason,
                "blocked_by": getattr(gate_report, "blocked_by", None),
                "gate_allowed": bool(getattr(gate_report, "allowed", False)),
                "action_types": [
                    str(getattr(action, "action_type", "") or "").strip().upper()
                    for action in tuple(action_plans or ())
                ],
                "ts_epoch_s": ts_epoch_s,
            },
        )

    def _persist_qqq_trace(
        self,
        *,
        snapshot: Any,
        signal: Any,
        entry_plan: EntryPlan,
        action_index: int,
        action_total: int,
        snapshot_kind: str = "entry",
    ) -> None:
        if not self._qqq_trace_enabled:
            return
        signal_symbol = str(getattr(snapshot, "symbol", "") or "").strip().upper()
        if signal_symbol != "QQQ":
            return
        ts_epoch_s = float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0)
        ts_dt = datetime.fromtimestamp(ts_epoch_s, tz=ET) if ts_epoch_s > 0 else datetime.now(ET)
        day = ts_dt.strftime("%Y%m%d")
        decision_id = str(getattr(entry_plan, "trigger_id", "") or "").strip()
        if not decision_id:
            decision_id = (
                f"{self.strategy_id}:{int(ts_epoch_s // 60) if ts_epoch_s > 0 else 0}:"
                f"{str(entry_plan.symbol or '').strip().upper()}:{str(entry_plan.side or '').strip().upper()}:{action_index}"
            )
        signal_action = str(getattr(signal, "action", "") or "").strip().lower()
        signal_payload = dict(getattr(signal, "payload", {}) or {})
        gate_reason = str(signal_payload.get("reason") or signal_payload.get("blocked_by") or signal_action or "")
        allow = bool(snapshot_kind == "entry" or (signal_action == "enter" and str(entry_plan.symbol or "").strip()))
        row = {
            "strategy_id": str(self.strategy_id),
            "decision_id": decision_id,
            "trigger_id": str(getattr(entry_plan, "trigger_id", "") or ""),
            "trace_id": decision_id,
            "ts_et": ts_dt.isoformat(),
            "ts_epoch_s": ts_epoch_s,
            "snapshot_kind": str(snapshot_kind),
            "minute": ts_dt.strftime("%Y-%m-%dT%H:%M"),
            "direction": str(getattr(entry_plan, "side", "") or "").strip().upper(),
            "signal_authority": {
                "allow": allow,
                "side": str(getattr(entry_plan, "side", "") or "").strip().upper(),
                "reason": gate_reason,
            },
            "intent": {
                "symbol": str(getattr(entry_plan, "symbol", "") or "").strip().upper(),
                "side": str(getattr(entry_plan, "side", "") or "").strip().upper(),
                "target_qty": float(getattr(entry_plan, "qty", 0.0) or 0.0),
            },
            "order_plan": {
                "exec_symbol": str(getattr(entry_plan, "symbol", "") or "").strip().upper(),
                "side": str(getattr(entry_plan, "side", "") or "").strip().upper(),
                "qty": float(getattr(entry_plan, "qty", 0.0) or 0.0),
                "order_type": "DRY_RUN" if self.config is not None and self.config.dry_run else "LIVE",
            },
            "block_reason": (None if allow else gate_reason),
            "exec_symbol": str(getattr(entry_plan, "symbol", "") or "").strip().upper(),
            "action_index": int(action_index),
            "action_total": int(action_total),
            "legacy_mode": str((self.config.raw_extras if self.config is not None else {}).get("legacy_mode") or ""),
        }
        try:
            self._qqq_trace_root.mkdir(parents=True, exist_ok=True)
            path = self._qqq_trace_root / f"qqq_signal_snapshots_{day}_{self.strategy_id}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception as exc:
            self._audit(
                stage="qqq_trace_failed",
                symbol=signal_symbol,
                reason=str(exc),
                payload={"decision_id": decision_id, "trace_root": str(self._qqq_trace_root)},
            )

    def _persist_qqq_evaluation_trace(
        self,
        *,
        snapshot: Any,
        signal: Any,
        entry_plan: EntryPlan | None,
    ) -> None:
        signal_symbol = str(getattr(snapshot, "symbol", "") or "").strip().upper()
        if signal_symbol != "QQQ":
            return
        side = str(getattr(signal, "side", "") or "FLAT").strip().upper() or "FLAT"
        exec_symbol = str(getattr(entry_plan, "symbol", "") or "").strip().upper() if entry_plan is not None else ""
        qty = float(getattr(entry_plan, "qty", 0.0) or 0.0) if entry_plan is not None else 0.0
        trigger_id = str(getattr(entry_plan, "trigger_id", "") or "").strip() if entry_plan is not None else ""
        synthetic_plan = EntryPlan(
            symbol=exec_symbol,
            side=side,
            qty=qty,
            trigger_id=trigger_id or f"{signal_symbol}:{int(float(getattr(snapshot, 'ts_epoch_s', 0.0) or 0.0) // 60)}",
            payload={
                "decision": str(getattr(signal, "action", "") or ""),
                "gate_reason": str(
                    (getattr(signal, "payload", {}) or {}).get("reason")
                    or (getattr(signal, "payload", {}) or {}).get("blocked_by")
                    or "signal_evaluated"
                ),
            },
        )
        self._persist_qqq_trace(
            snapshot=snapshot,
            signal=signal,
            entry_plan=synthetic_plan,
            action_index=0,
            action_total=0,
            snapshot_kind="evaluation",
        )

    def _trade_budget_usd(self) -> float:
        """
        Single source of truth for sizing and risk-budget.

        Priority:
        1) Explicit per-strategy override in extras (`trade_budget_usd`)
        2) Backward-compat: `policy_params.trade_budget_usd` if present
        3) Strategy capital pool (`params.capital_limit_usd`)
        """
        if self.config is not None:
            raw = self.config.raw_extras
            try:
                value = float(raw.get("trade_budget_usd") or 0.0)
                if value > 0:
                    return value
            except Exception:
                pass
            try:
                value = float(self.config.policy_params.get("trade_budget_usd") or 0.0)
                if value > 0:
                    return value
            except Exception:
                pass
        try:
            value = float(getattr(self.params, "capital_limit_usd", 0.0) or 0.0)
            return value if value > 0 else 0.0
        except Exception:
            return 0.0

    def _submitted_entry_count(self, *, ts_epoch_s: float) -> int:
        return len(self._submitted_entry_decisions_by_day.get(self._day_key(ts_epoch_s=ts_epoch_s), set()))

    def _record_submitted_entry_decision(self, *, ts_epoch_s: float, decision_id: str) -> None:
        decision_id_v = str(decision_id or "").strip()
        if not decision_id_v:
            return
        self._submitted_entry_decisions_by_day.setdefault(self._day_key(ts_epoch_s=ts_epoch_s), set()).add(decision_id_v)

    def _completed_round_trip_count(self, *, ts_epoch_s: float) -> int:
        return len(self._completed_round_trips_by_day.get(self._day_key(ts_epoch_s=ts_epoch_s), set()))

    def _day_realized_pnl(self, *, ts_epoch_s: float) -> float:
        return float(self._day_realized_pnl_by_day.get(self._day_key(ts_epoch_s=ts_epoch_s), 0.0) or 0.0)

    def _entry_policy_runtime_state(
        self,
        *,
        ts_epoch_s: float,
        signal_symbol: str,
        exec_symbol: str,
        action_index: int,
        action_total: int,
        feature_context,
        baseline_pending_entry_count: int | None = None,
        baseline_pending_entry_symbols: tuple[str, ...] | None = None,
        baseline_same_strategy_pending_orders: int | None = None,
        baseline_order_lifecycle: tuple[int, tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        day_key = self._day_key(ts_epoch_s=ts_epoch_s)
        pending_entry_count, pending_entry_symbols = self._pending_entry_stats_for_strategy()
        if baseline_pending_entry_count is not None:
            pending_entry_count = int(baseline_pending_entry_count)
            pending_entry_symbols = tuple(
                str(item or "").strip().upper()
                for item in tuple(baseline_pending_entry_symbols or ())
                if str(item or "").strip()
            )
        order_lifecycle_pending_count, order_lifecycle_pending_refs = self._order_lifecycle_pending_stats(
            exec_symbols=[exec_symbol] if exec_symbol else []
        )
        if baseline_order_lifecycle is not None:
            order_lifecycle_pending_count = int((baseline_order_lifecycle or (0, ()))[0] or 0)
            order_lifecycle_pending_refs = tuple(
                str(item or "").strip()
                for item in tuple((baseline_order_lifecycle or (0, ()))[1] or ())
                if str(item or "").strip()
            )
        same_strategy_pending_orders = 0
        if baseline_same_strategy_pending_orders is not None:
            same_strategy_pending_orders = int(baseline_same_strategy_pending_orders)
        elif feature_context is not None:
            try:
                same_strategy_pending_orders = int(getattr(feature_context, "values", {}).get("same_strategy_pending_orders") or 0)
            except Exception:
                same_strategy_pending_orders = 0
        return {
            "day_key": day_key,
            "submitted_today": len(self._submitted_entry_decisions_by_day.get(day_key, set())),
            "success_count": len(self._completed_round_trips_by_day.get(day_key, set())),
            "day_realized_pnl": float(self._day_realized_pnl_by_day.get(day_key, 0.0) or 0.0),
            "pending_entry_count": int(pending_entry_count),
            "pending_entry_symbols": list(pending_entry_symbols),
            "same_strategy_pending_orders": int(same_strategy_pending_orders),
            "order_lifecycle_pending_count": int(order_lifecycle_pending_count),
            "order_lifecycle_pending_refs": list(order_lifecycle_pending_refs),
            "signal_symbol": str(signal_symbol or "").strip().upper(),
            "exec_symbol": str(exec_symbol or "").strip().upper(),
            "action_index": int(action_index),
            "action_total": int(action_total),
            "strategy_id": self.strategy_id,
            "ts_epoch_s": float(ts_epoch_s or 0.0),
        }

    def _order_lifecycle_pending_stats(self, *, exec_symbols: list[str]) -> tuple[int, tuple[str, ...]]:
        if self.order_lifecycle_module is None:
            return 0, ()
        try:
            return self.order_lifecycle_module.pending_non_terminal_orders(symbols=exec_symbols)
        except Exception:
            return 0, ()

    def _emit_strategy_tradability_heartbeat(
        self,
        *,
        symbol: str,
        session: str = "",
        readiness: Any = None,
        blockers: list[str] | None = None,
        reason: str = "runtime",
    ) -> None:
        symbol_u = str(symbol or "").strip().upper()
        session_u = str(session or "").strip().upper()
        blocker_list = [str(item) for item in list(blockers or []) if str(item or "").strip()]
        if (
            self._system_paused
            and "system_paused" not in blocker_list
            and not self._system_pause_can_resume_from_tradability()
        ):
            blocker_list.append("system_paused")

        instrument = None
        try:
            instrument = self._instrument_for_signal_symbol(symbol_u) or self._instrument_for_exec_symbol(symbol_u)
        except Exception:
            instrument = None
        exec_symbols = self._runtime_exec_symbols(symbol=symbol_u, instrument=instrument) if instrument is not None else [symbol_u]
        for exec_symbol in exec_symbols:
            exec_symbol_u = str(exec_symbol or "").strip().upper()
            reason_v = str(self._symbol_kill_switch.get(exec_symbol_u) or "").strip()
            if reason_v and self._kill_switch_release_allowed_by_flat_readiness(
                exec_symbol=exec_symbol_u,
                previous_reason=reason_v,
            ):
                removed_reason = self._symbol_kill_switch.pop(exec_symbol_u, None)
                if removed_reason:
                    self._audit(
                        stage="symbol_kill_switch_released",
                        symbol=exec_symbol_u,
                        reason="ready",
                        payload={
                            "previous_reason": str(removed_reason),
                            "signal_symbol": symbol_u,
                            "exec_symbol": exec_symbol_u,
                            "release_source": "strategy_tradability_heartbeat",
                        },
                    )
                reason_v = str(self._symbol_kill_switch.get(exec_symbol_u) or "").strip()
            if reason_v:
                blocker = f"symbol_kill_switch:{exec_symbol_u}:{reason_v}"
                if blocker not in blocker_list:
                    blocker_list.append(blocker)
        pending_count, pending_refs = self._order_lifecycle_pending_stats(exec_symbols=exec_symbols)
        if pending_count > 0 and "order_lifecycle_pending" not in blocker_list:
            blocker_list.append("order_lifecycle_pending")

        readiness_blocks_now = self._strategy_data_contract_active_for_session(session=session_u)
        data_age_sec = self._latest_snapshot_age_sec(symbol=symbol_u)
        ttl_sec = self._strategy_data_contract_ttl_sec()
        if readiness_blocks_now and (data_age_sec is None or data_age_sec > ttl_sec):
            blocker = "strategy_market_data_stale"
            if blocker not in blocker_list:
                blocker_list.append(blocker)
        if readiness is not None and readiness_blocks_now and bool(getattr(readiness, "ready", True)) is not True:
            readiness_reason = str(getattr(readiness, "reason", "") or "required_data_missing").strip()
            if readiness_reason and readiness_reason not in blocker_list:
                blocker_list.append(readiness_reason)

        write_strategy_tradability_heartbeat(
            strategy_id=str(self.strategy_id),
            tradable=len(blocker_list) == 0,
            blockers=blocker_list,
            source="stocks_kernel_runtime",
            details={
                "symbol": symbol_u,
                "session": session_u,
                "reason": str(reason or "runtime"),
                "exec_symbols": list(exec_symbols),
                "order_lifecycle_pending_count": int(pending_count),
                "order_lifecycle_pending_refs": list(pending_refs),
                "active_symbol_kill_switches": dict(self._symbol_kill_switch),
                "readiness_ready": bool(getattr(readiness, "ready", True)) if readiness is not None else None,
                "readiness_reason": str(getattr(readiness, "reason", "") or "") if readiness is not None else "",
                "data_contract_active": bool(readiness_blocks_now),
                "data_age_sec": data_age_sec,
                "data_ttl_sec": ttl_sec,
            },
        )

    async def _run_strategy_tradability_heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30.0)
                if self.config is None:
                    continue
                await self._recover_startup_block_for_strategy_tradability()
                symbols = [
                    str(getattr(instrument, "signal_symbol", "") or "").strip().upper()
                    for instrument in tuple(self.config.instruments or ())
                ]
                symbols = [symbol for symbol in symbols if symbol]
                if not symbols:
                    symbols = ["SYSTEM"]
                for symbol in sorted(set(symbols)):
                    await self._reconcile_heartbeat_order_lifecycle(symbol=symbol)
                    self._emit_strategy_tradability_heartbeat(
                        symbol=symbol,
                        session=self._current_strategy_session(),
                        reason="periodic_strategy_readiness",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._audit(
                    stage="strategy_tradability_heartbeat_failed",
                    symbol="SYSTEM",
                    reason="heartbeat_error",
                    payload={"error": str(exc)},
                )

    async def _recover_startup_block_for_strategy_tradability(self) -> bool:
        reason = str(self._startup_block_reason or "").strip()
        if not reason:
            return True
        recovered = await self._maybe_recover_startup_block()
        if recovered:
            return True
        self._audit(
            stage="startup_recovery_still_blocked_for_heartbeat",
            symbol="SYSTEM",
            reason=str(self._startup_block_reason or reason),
            payload=dict(self._startup_block_payload or {}),
        )
        return False

    async def _reconcile_heartbeat_order_lifecycle(self, *, symbol: str) -> None:
        if self.order_lifecycle_module is None or self.config is None:
            return
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u or symbol_u == "SYSTEM":
            return
        try:
            instrument = self._instrument_for_signal_symbol(symbol_u) or self._instrument_for_exec_symbol(symbol_u)
        except Exception:
            instrument = None
        exec_symbols = self._runtime_exec_symbols(symbol=symbol_u, instrument=instrument) if instrument is not None else [symbol_u]
        if not exec_symbols:
            return
        pending_count, _ = self._order_lifecycle_pending_stats(exec_symbols=list(exec_symbols))
        if pending_count <= 0:
            return
        await self._reconcile_order_lifecycle_from_db(
            exec_symbols=list(exec_symbols),
            now_ts_epoch_s=float(datetime.now().timestamp()),
        )

    def _strategy_data_contract_active_for_session(self, *, session: str) -> bool:
        extras = dict(getattr(self.config, "raw_extras", {}) or {}) if self.config is not None else {}
        contract = extras.get("data_contract") if isinstance(extras.get("data_contract"), dict) else {}
        requirements = list(contract.get("requirements") or []) if isinstance(contract, dict) else []
        if not requirements:
            return True
        if any(not bool((req or {}).get("rth", False)) for req in requirements if isinstance(req, dict)):
            return True
        session_u = str(session or "").strip().upper()
        return session_u in {"RTH", "REGULAR", "REGULAR_SESSION", "OPEN"}

    def _strategy_data_contract_ttl_sec(self) -> float:
        extras = dict(getattr(self.config, "raw_extras", {}) or {}) if self.config is not None else {}
        contract = extras.get("data_contract") if isinstance(extras.get("data_contract"), dict) else {}
        try:
            return max(5.0, float((contract or {}).get("ttl_sec") or 90.0))
        except Exception:
            return 90.0

    def _latest_snapshot_age_sec(self, *, symbol: str) -> float | None:
        symbol_u = str(symbol or "").strip().upper()
        snapshot = self._latest_symbol_snapshots.get(symbol_u)
        ts = float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0) if snapshot is not None else 0.0
        if ts <= 0.0:
            return None
        return max(0.0, time.time() - ts)

    def _current_strategy_session(self) -> str:
        now_et = datetime.now(tz=ET)
        if now_et.weekday() <= 4 and time_module(9, 30) <= now_et.time() <= time_module(16, 0):
            return "RTH"
        return "OFFSESSION"

    async def _run_safety_invariants_while_paused(self, *, symbol_u: str, features: dict[str, Any]) -> None:
        if self.config is None or self.execution is None:
            return
        snapshot = FeatureRowSnapshotProvider.build_snapshot(
            symbol=symbol_u,
            features=dict(features or {}),
        )
        instrument = self._instrument_for_signal_symbol(symbol_u) or self._instrument_for_exec_symbol(symbol_u)
        if instrument is None:
            return
        await self._check_runtime_safety_invariants(
            signal_symbol=str(getattr(instrument, "signal_symbol", "") or symbol_u).strip().upper(),
            instrument=instrument,
            snapshot=snapshot,
        )

    async def _reconcile_order_lifecycle_from_db(
        self,
        *,
        exec_symbols: list[str],
        now_ts_epoch_s: float,
    ) -> None:
        if self.order_lifecycle_module is None:
            return
        records = self.order_lifecycle_module.non_terminal_records(symbols=exec_symbols)
        refs = [str(record.order_ref or "").strip() for record in records if str(record.order_ref or "").strip()]
        if not refs:
            return
        broker_order_ids = sorted(
            {
                str(record.broker_order_id or "").strip()
                for record in records
                if str(record.broker_order_id or "").strip()
            }
        )
        decision_ids = sorted(
            {
                self._decision_id_from_order_ref(
                    str(record.order_ref or "").strip(),
                    symbol=str(record.symbol or "").strip().upper(),
                )
                for record in records
                if self._decision_id_from_order_ref(
                    str(record.order_ref or "").strip(),
                    symbol=str(record.symbol or "").strip().upper(),
                )
            }
        )
        perm_ids = sorted(
            {
                int(record.perm_id)
                for record in records
                if record.perm_id is not None
            }
        )
        pg = self._resolve_pg()
        if pg is None:
            return
        terminal_states = ("FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED")
        records_by_ref = {str(record.order_ref or "").strip(): record for record in records}

        def _matching_record_refs(payload: dict[str, Any]) -> list[str]:
            row_ref = str(payload.get("order_ref") or "").strip()
            row_broker = str(payload.get("broker_order_id") or "").strip()
            row_decision = str(payload.get("decision_id") or "").strip()
            if not row_decision:
                try:
                    note = json.loads(str(payload.get("note") or "{}"))
                    if isinstance(note, dict):
                        trace = note.get("trace") if isinstance(note.get("trace"), dict) else {}
                        row_decision = str(note.get("decision_id") or trace.get("decision_id") or "").strip()
                except Exception:
                    row_decision = ""
            row_perm_raw = payload.get("ib_perm_id")
            try:
                row_perm = int(row_perm_raw) if row_perm_raw is not None else None
            except Exception:
                row_perm = None
            matched: list[str] = []
            for record in records:
                record_ref = str(record.order_ref or "").strip()
                if not record_ref:
                    continue
                if row_ref and row_ref == record_ref:
                    matched.append(record_ref)
                    continue
                record_broker = str(record.broker_order_id or "").strip()
                if row_broker and record_broker and row_broker == record_broker:
                    matched.append(record_ref)
                    continue
                record_decision = self._decision_id_from_order_ref(record_ref, symbol=str(record.symbol or "").strip().upper())
                if row_decision and record_decision and row_decision == record_decision:
                    matched.append(record_ref)
                    continue
                if row_perm is not None and record.perm_id is not None and int(record.perm_id) == row_perm:
                    matched.append(record_ref)
                    continue
            return matched

        try:
            query = """
                SELECT order_ref, symbol, state, broker_order_id, ib_perm_id, note
                FROM core.core_order
                WHERE strategy_id = $1
                  AND (
                    order_ref = ANY($2::text[])
                    OR broker_order_id = ANY($4::text[])
                    OR ib_perm_id = ANY($5::bigint[])
                  )
                  AND UPPER(COALESCE(state, '')) = ANY($3::text[])
                """
            if hasattr(pg, "fetch"):
                rows = await pg.fetch(query, self.strategy_id, refs, list(terminal_states), broker_order_ids, perm_ids)
            elif hasattr(pg, "fetchall"):
                rows = await pg.fetchall(query, self.strategy_id, refs, list(terminal_states), broker_order_ids, perm_ids)
            else:
                return
        except Exception as exc:
            self._audit(
                stage="order_lifecycle_db_reconcile_failed",
                symbol="SYSTEM",
                reason="db_query_failed",
                payload={"error": str(exc), "order_refs": refs[:20]},
            )
            return
        terminal_by_ref: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            payload = dict(row)
            for ref in _matching_record_refs(payload):
                terminal_by_ref[ref] = payload
        missing_refs = [ref for ref in refs if ref not in terminal_by_ref]
        if missing_refs:
            event_terminal_states = {
                "ORDER_FILL": "FILLED",
                "FILL_RECEIVED": "FILLED",
                "ORDER_FILLED": "FILLED",
                "FILLED": "FILLED",
                "FLATTEN_FILLED": "FILLED",
                "ORDER_CANCELLED": "CANCELLED",
                "ORDER_CANCELED": "CANCELLED",
                "CANCELLED": "CANCELLED",
                "CANCELED": "CANCELLED",
                "ORDER_REJECTED": "REJECTED",
                "REJECTED": "REJECTED",
                "ORDER_EXPIRED": "EXPIRED",
                "EXPIRED": "EXPIRED",
                "ORDER_FAILED": "FAILED",
                "FAILED": "FAILED",
            }
            try:
                query = """
                    SELECT DISTINCT ON (
                        COALESCE(NULLIF(payload->>'order_ref', ''), broker_order_id, '')
                    )
                        payload->>'order_ref' AS order_ref,
                        symbol,
                        event_type,
                        broker_order_id,
                        decision_id,
                        CASE
                            WHEN payload->>'perm_id' ~ '^[0-9]+$'
                            THEN (payload->>'perm_id')::bigint
                            ELSE NULL
                        END AS ib_perm_id
                    FROM core.core_order_event
                    WHERE strategy_id = $1
                      AND (
                        payload->>'order_ref' = ANY($2::text[])
                        OR broker_order_id = ANY($4::text[])
                        OR decision_id = ANY($6::text[])
                        OR (
                            payload->>'perm_id' ~ '^[0-9]+$'
                            AND (payload->>'perm_id')::bigint = ANY($5::bigint[])
                        )
                      )
                      AND UPPER(COALESCE(event_type, '')) = ANY($3::text[])
                    ORDER BY COALESCE(NULLIF(payload->>'order_ref', ''), broker_order_id, ''), event_ts DESC
                """
                event_types = list(event_terminal_states.keys())
                if hasattr(pg, "fetch"):
                    event_rows = await pg.fetch(query, self.strategy_id, missing_refs, event_types, broker_order_ids, perm_ids, decision_ids)
                elif hasattr(pg, "fetchall"):
                    event_rows = await pg.fetchall(query, self.strategy_id, missing_refs, event_types, broker_order_ids, perm_ids, decision_ids)
                else:
                    event_rows = []
            except Exception as exc:
                event_rows = []
                self._audit(
                    stage="order_lifecycle_event_reconcile_failed",
                    symbol="SYSTEM",
                    reason="db_query_failed",
                    payload={"error": str(exc), "order_refs": missing_refs[:20]},
                )
            for row in event_rows or []:
                payload = dict(row)
                event_type = str(payload.get("event_type") or "").strip().upper()
                state = event_terminal_states.get(event_type)
                for ref in _matching_record_refs(payload):
                    if not state:
                        continue
                    terminal_by_ref[ref] = {
                        "order_ref": ref,
                        "symbol": str(payload.get("symbol") or "").strip().upper(),
                        "state": state,
                        "source": "core_order_event",
                        "event_type": event_type,
                    }
        if not terminal_by_ref:
            return
        for record in records:
            ref = str(record.order_ref or "").strip()
            terminal = terminal_by_ref.get(ref)
            if not terminal:
                continue
            state = str(terminal.get("state") or "").strip().upper()
            symbol = str(terminal.get("symbol") or records_by_ref.get(ref, record).symbol or record.symbol or "").strip().upper()
            self.order_lifecycle_module.mark_terminal(
                order_ref=ref,
                state=state,
                now_ts_epoch_s=float(now_ts_epoch_s or 0.0),
            )
            self.order_lifecycle_module.clear_freeze(symbol=symbol)
            pending_count, _ = self.order_lifecycle_module.pending_non_terminal_orders(symbols=[symbol])
            if pending_count <= 0:
                signal_symbol = self._signal_symbol_for_exec_symbol(symbol)
                await self._maybe_release_symbol_kill_switch(
                    signal_symbol=signal_symbol,
                    exec_symbol=symbol,
                    reason=self._symbol_kill_switch.get(symbol),
                )
            self._audit(
                stage="order_lifecycle_terminalized_from_db",
                symbol=symbol,
                reason="db_terminal_state",
                payload={
                    "order_ref": ref,
                    "db_state": state,
                    "source": str(terminal.get("source") or "core_order"),
                    "event_type": str(terminal.get("event_type") or ""),
                    "pending_count_after": int(pending_count),
                },
            )

    def _signal_symbol_for_exec_symbol(self, exec_symbol: str) -> str:
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return ""
        try:
            instrument = self._instrument_for_exec_symbol(symbol_u)
            signal_symbol = str(getattr(instrument, "signal_symbol", "") or "").strip().upper()
            if signal_symbol:
                return signal_symbol
        except Exception:
            pass
        return symbol_u

    def _pending_entry_stats_for_strategy(self) -> tuple[int, tuple[str, ...]]:
        if self.execution is None:
            return 0, ()
        count = 0
        symbols: set[str] = set()
        for lot in self.execution.ledger.list_lots(strategy_id=self.strategy_id, include_closed=False):
            state = str(getattr(lot, "state", "") or "").strip().lower()
            if state != "pending_entry":
                continue
            count += 1
            symbol = str(getattr(lot, "symbol", "") or "").strip().upper()
            if symbol:
                symbols.add(symbol)
        return count, tuple(sorted(symbols))

    def _day_key(self, *, ts_epoch_s: float) -> str:
        if ts_epoch_s <= 0.0:
            return datetime.now(ET).strftime("%Y-%m-%d")
        return datetime.fromtimestamp(float(ts_epoch_s), tz=ET).strftime("%Y-%m-%d")

    def _is_after_et_cutoff(self, *, ts_epoch_s: float, cutoff_hhmm: str) -> bool:
        cutoff = str(cutoff_hhmm or "").strip()
        if not cutoff or ts_epoch_s <= 0.0:
            return False
        try:
            hour_s, minute_s = cutoff.split(":", 1)
            cutoff_minutes = int(hour_s) * 60 + int(minute_s)
        except Exception:
            return False
        now_et = datetime.fromtimestamp(float(ts_epoch_s), tz=ET)
        return (now_et.hour * 60 + now_et.minute) >= cutoff_minutes

    def _entry_check_on_new_minute_only(self) -> bool:
        if self.config is None:
            return False
        extras = dict(getattr(self.config, "raw_extras", {}) or {})
        if "entry_check_on_new_minute_only" in extras:
            return bool(extras.get("entry_check_on_new_minute_only"))
        policy_params = dict(getattr(self.config, "policy_params", {}) or {})
        if "entry_check_on_new_minute_only" in policy_params:
            return bool(policy_params.get("entry_check_on_new_minute_only"))
        policy_id = str(getattr(self.config, "policy_id", "") or "").strip().lower()
        if policy_id in {"qqq_intraday_policy_v1", "spy_intraday_policy_v1"}:
            return bool(policy_params.get("rule_engine_parity"))
        return False

    async def _inject_runtime_guards(self, *, symbol: str, instrument, features: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(features)
        exec_symbols = self._runtime_exec_symbols(symbol=symbol, instrument=instrument)
        runtime_pending = await self._same_strategy_pending_orders(exec_symbols=exec_symbols)
        out["same_strategy_pending_orders"] = runtime_pending
        detail = dict(self._last_same_strategy_pending_detail.get(tuple(exec_symbols), {}) or {})
        if detail:
            out["same_strategy_pending_orders_detail"] = detail
        return out

    def _runtime_exec_symbols(self, *, symbol: str, instrument) -> list[str]:
        if instrument is not None:
            if hasattr(instrument, "exec_symbols"):
                try:
                    symbols = list(instrument.exec_symbols())
                    if symbols:
                        return symbols
                except Exception:
                    pass
            return sorted(
                {
                    str(getattr(instrument, "long_exec_symbol", "") or "").strip().upper(),
                    str(getattr(instrument, "short_exec_symbol", "") or "").strip().upper(),
                }
                - {""}
            )
        symbol_u = str(symbol or "").strip().upper()
        return [symbol_u] if symbol_u else []

    def _entry_frozen_for_action_arbiter(self, *, signal_symbol: str, instrument) -> bool:
        exec_symbols = self._runtime_exec_symbols(symbol=signal_symbol, instrument=instrument)
        for exec_symbol in exec_symbols:
            if self.order_lifecycle_module is not None and self.order_lifecycle_module.should_freeze_entry(symbol=exec_symbol):
                return True
        for lot in self._account_binding_lots(instrument=instrument):
            if self._lot_requires_runtime_entry_freeze(lot=lot, exec_symbols=exec_symbols):
                return True
        return False

    @staticmethod
    def _lot_requires_runtime_entry_freeze(*, lot, exec_symbols: list[str]) -> bool:
        if float(getattr(lot, "live_qty", 0.0) or 0.0) <= 0.0:
            return False
        lot_symbol = str(getattr(lot, "symbol", "") or "").strip().upper()
        if exec_symbols and lot_symbol not in set(str(symbol or "").strip().upper() for symbol in exec_symbols):
            return False
        protection_state = dict(getattr(lot, "extras", {}) or {}).get("protection_state") or {}
        protection_lifecycle = str(protection_state.get("protection_lifecycle") or "").strip().lower()
        if bool(protection_state.get("freeze_required")):
            return True
        return protection_lifecycle in {"repair_pending", "flatten_only", "degraded"}

    async def _reconcile_pending_entry_lots_from_db_position(
        self,
        *,
        exec_symbols: list[str] | tuple[str, ...],
        now_ts_epoch_s: float,
    ) -> list[Dict[str, Any]]:
        if self.execution is None:
            return []
        pg = self._resolve_pg()
        if pg is None:
            return []
        symbols = sorted({str(symbol or "").strip().upper() for symbol in exec_symbols if str(symbol or "").strip()})
        if not symbols:
            return []
        pending_by_symbol: dict[str, list[Any]] = {}
        for lot in self.execution.ledger.list_lots(strategy_id=self.strategy_id, include_closed=False):
            symbol_u = str(getattr(lot, "symbol", "") or "").strip().upper()
            if symbol_u not in symbols:
                continue
            if str(getattr(lot, "state", "") or "").strip().lower() != "pending_entry":
                continue
            pending_by_symbol.setdefault(symbol_u, []).append(lot)
        if not pending_by_symbol:
            return []
        refs = [
            str(getattr(lot, "entry_order_ref", "") or "").strip()
            for lots in pending_by_symbol.values()
            for lot in lots
            if str(getattr(lot, "entry_order_ref", "") or "").strip()
        ]
        if not refs:
            return []
        pending_by_lot_id = {
            str(getattr(lot, "position_lot_id", "") or "").strip(): lot
            for lots in pending_by_symbol.values()
            for lot in lots
            if str(getattr(lot, "position_lot_id", "") or "").strip()
        }
        lot_ids = sorted(pending_by_lot_id)
        if not lot_ids:
            return []
        extras = dict(getattr(getattr(self, "config", None), "raw_extras", {}) or {})
        strategy_instance_id = str(extras.get("strategy_instance_id") or self.strategy_id or "").strip()
        account_id = str(extras.get("account_id") or extras.get("account") or "").strip() or None
        try:
            query = """
                SELECT
                    plc.account_id,
                    plc.strategy_id,
                    plc.strategy_instance_id,
                    plc.symbol,
                    plc.lot_id,
                    plc.qty AS ledger_qty,
                    plc.avg_px,
                    co.order_ref,
                    co.broker_order_id,
                    co.ib_perm_id,
                    co.qty AS order_qty,
                    co.limit_px
                FROM core.position_ledger_current plc
                JOIN core.core_order co
                  ON co.strategy_id = plc.strategy_id
                 AND co.symbol = plc.symbol
                 AND co.order_ref = ANY($5::text[])
                 AND UPPER(COALESCE(co.state, '')) = 'FILLED'
                WHERE plc.strategy_id = $1
                  AND COALESCE(NULLIF(plc.strategy_instance_id, ''), plc.strategy_id) = $2
                  AND plc.symbol = ANY($3::text[])
                  AND plc.lot_id = ANY($4::text[])
                  AND ($6::text IS NULL OR plc.account_id = $6::text)
                  AND ABS(COALESCE(plc.qty, 0)) > 1e-9
            """
            if hasattr(pg, "fetch"):
                rows = await pg.fetch(query, self.strategy_id, strategy_instance_id, symbols, lot_ids, refs, account_id)
            elif hasattr(pg, "fetchall"):
                rows = await pg.fetchall(query, self.strategy_id, strategy_instance_id, symbols, lot_ids, refs, account_id)
            else:
                return []
        except Exception as exc:
            self._audit(
                stage="pending_entry_db_position_reconcile_failed",
                symbol="SYSTEM",
                reason="db_query_failed",
                payload={"error": str(exc), "exec_symbols": symbols},
            )
            return []
        actions: list[Dict[str, Any]] = []
        for row in rows or []:
            payload = dict(row)
            symbol_u = str(payload.get("symbol") or "").strip().upper()
            lot_id = str(payload.get("lot_id") or "").strip()
            lot = pending_by_lot_id.get(lot_id)
            if lot is None or symbol_u != str(getattr(lot, "symbol", "") or "").strip().upper():
                continue
            order_ref = str(payload.get("order_ref") or "").strip()
            if order_ref != str(getattr(lot, "entry_order_ref", "") or "").strip():
                continue
            allowed, blocked_reason = self._check_adopt_reconcile_authority(
                lot=lot,
                symbol=symbol_u,
                blocked_stage="pending_entry_db_position_reconcile_blocked_authority",
                granted_stage="pending_entry_db_position_reconcile_authority_granted",
                extra_payload={
                    "state_before": str(getattr(lot, "state", "") or ""),
                    "sync_source": "position_ledger_current",
                    "account_id": str(payload.get("account_id") or "") or None,
                    "strategy_instance_id": str(payload.get("strategy_instance_id") or "") or None,
                },
            )
            if not allowed:
                actions.append(
                    {
                        "action": "blocked_authority",
                        "position_lot_id": lot.position_lot_id,
                        "symbol": symbol_u,
                        "reason": blocked_reason or "adopt_authority_denied",
                    }
                )
                continue
            try:
                qty = abs(float(payload.get("ledger_qty") or payload.get("order_qty") or 0.0))
            except Exception:
                qty = 0.0
            try:
                price = float(payload.get("avg_px") or payload.get("limit_px") or 0.0)
            except Exception:
                price = 0.0
            if qty <= 0.0:
                continue
            perm_id = None
            if payload.get("ib_perm_id") is not None:
                try:
                    perm_id = int(payload.get("ib_perm_id"))
                except Exception:
                    perm_id = None
            reconciled = self.execution.ledger.record_entry_fill(
                position_lot_id=lot.position_lot_id,
                fill_qty=qty,
                fill_price=price,
                broker_order_id=str(payload.get("broker_order_id") or "").strip() or None,
                perm_id=perm_id,
            )
            if self.order_lifecycle_module is not None:
                self.order_lifecycle_module.mark_terminal(
                    order_ref=str(getattr(lot, "entry_order_ref", "") or "").strip(),
                    state="FILLED",
                    now_ts_epoch_s=float(now_ts_epoch_s or 0.0),
                )
                self.order_lifecycle_module.clear_freeze(symbol=symbol_u)
            action = {
                "action": "live_from_db_position",
                "position_lot_id": lot.position_lot_id,
                "symbol": symbol_u,
                "state_before": str(getattr(lot, "state", "") or ""),
                "state_after": str(reconciled.state or ""),
                "entry_order_ref": str(getattr(lot, "entry_order_ref", "") or ""),
                "account_id": str(payload.get("account_id") or "") or None,
                "strategy_instance_id": str(payload.get("strategy_instance_id") or "") or None,
                "qty": float(qty),
                "avg_px": float(price),
            }
            actions.append(action)
            self._audit(
                stage="pending_entry_lot_reconciled_from_db_position",
                symbol=symbol_u,
                reason="owner_ledger_filled_position",
                payload=action,
            )
        return actions

    async def _maybe_run_broker_truth_sync(self, *, signal_symbol: str, instrument, session: str, now_ts_epoch_s: float) -> None:
        if self.broker_truth_sync_module is None or self.execution is None:
            return
        now_ts = float(now_ts_epoch_s or 0.0)
        min_interval_sec = self._broker_truth_sync_min_interval_sec(session=session, instrument=instrument)
        if now_ts > 0.0 and self._last_broker_truth_sync_ts > 0.0 and (now_ts - self._last_broker_truth_sync_ts) < min_interval_sec:
            return
        adapter = self._resolve_broker_adapter()
        if adapter is None or not hasattr(adapter, "open_orders"):
            return
        exec_symbols = tuple(self._runtime_exec_symbols(symbol=signal_symbol, instrument=instrument))
        if not exec_symbols:
            return
        await self._reconcile_pending_entry_lots_from_db_position(
            exec_symbols=exec_symbols,
            now_ts_epoch_s=now_ts,
        )
        if not self._broker_truth_sync_requires_probe(exec_symbols=exec_symbols):
            if now_ts > 0.0:
                self._last_broker_truth_sync_ts = now_ts
            return
        broker_open_orders, snapshot_stale = await self._broker_truth_open_orders_for_runtime(
            adapter=adapter,
            signal_symbol=signal_symbol,
            session=session,
            instrument=instrument,
            now_ts=now_ts,
        )
        if broker_open_orders is None:
            return
        report = self.broker_truth_sync_module.evaluate(
            strategy_id=self.strategy_id,
            kernel=self.execution,
            broker_open_orders=tuple(broker_open_orders or ()),
            tracked_symbols=exec_symbols,
        )
        if snapshot_stale:
            if not report.ok:
                self._audit(
                    stage="broker_truth_sync_stale_snapshot_mismatch_deferred",
                    symbol=str(signal_symbol or "").strip().upper(),
                    reason="stale_snapshot_pending_refresh",
                    payload={
                        "orphan_order_refs": list(getattr(report, "orphan_order_refs", ()) or ()),
                        "missing_order_refs": list(getattr(report, "missing_order_refs", ()) or ()),
                        "freeze_symbols": list(getattr(report, "freeze_symbols", ()) or ()),
                    },
                )
            return
        if not report.ok:
            confirmed = await self._confirm_broker_truth_mismatch(
                adapter=adapter,
                signal_symbol=signal_symbol,
                exec_symbols=exec_symbols,
                initial_report=report,
            )
            if confirmed is None:
                if now_ts > 0.0:
                    self._last_broker_truth_sync_ts = now_ts
                return
            report = confirmed
        if now_ts > 0.0:
            self._last_broker_truth_sync_ts = now_ts
        if report.ok:
            self._reset_broker_truth_mismatch_streak(symbols=exec_symbols)
            for exec_symbol in exec_symbols:
                await self._maybe_release_symbol_kill_switch(
                    signal_symbol=signal_symbol,
                    exec_symbol=exec_symbol,
                    reason=self._symbol_kill_switch.get(str(exec_symbol or "").strip().upper()),
                )
            return
        if not self._broker_truth_mismatch_ready_to_freeze(
            signal_symbol=signal_symbol,
            report=report,
            exec_symbols=exec_symbols,
        ):
            return
        self._audit(
            stage="broker_truth_sync_mismatch",
            symbol=str(signal_symbol or "").strip().upper(),
            reason=report.reason,
            payload={
                "orphan_order_refs": list(report.orphan_order_refs),
                "missing_order_refs": list(report.missing_order_refs),
                "freeze_symbols": list(report.freeze_symbols),
            },
        )
        if self.order_lifecycle_module is not None:
            for sym in report.freeze_symbols:
                self.order_lifecycle_module.mark_sync_issue(symbol=sym, reason=report.reason)
                self._activate_symbol_kill_switch(symbol=sym, reason=report.reason)

    def _broker_truth_sync_requires_probe(self, *, exec_symbols: tuple[str, ...]) -> bool:
        if self.execution is None:
            return False
        if not hasattr(self.execution, "ledger"):
            # Unknown execution shape: keep legacy behavior and probe.
            return True
        for symbol in exec_symbols:
            if self._has_active_exec_lot(symbol):
                return True
        if self.order_lifecycle_module is not None:
            try:
                pending_count, _ = self.order_lifecycle_module.pending_non_terminal_orders(symbols=list(exec_symbols))
            except Exception:
                pending_count = 0
            if int(pending_count or 0) > 0:
                return True
        return any(str(symbol or "").strip().upper() in self._symbol_kill_switch for symbol in exec_symbols)

    def _broker_truth_mismatch_confirmations_required(self) -> int:
        if self.config is None:
            return 2
        extras = dict(getattr(self.config, "raw_extras", {}) or {})
        try:
            return max(1, int(extras.get("broker_truth_sync_mismatch_confirmations_required") or 2))
        except Exception:
            return 2

    def _broker_truth_mismatch_ready_to_freeze(self, *, signal_symbol: str, report, exec_symbols: tuple[str, ...]) -> bool:
        required = self._broker_truth_mismatch_confirmations_required()
        symbols = tuple(report.freeze_symbols or ()) or tuple(exec_symbols or ()) or (
            str(signal_symbol or "").strip().upper(),
        )
        if not symbols:
            return True
        ready = True
        streak_payload = {}
        for symbol in symbols:
            symbol_u = str(symbol or "").strip().upper()
            if not symbol_u:
                continue
            streak = int(self._broker_truth_mismatch_streak.get(symbol_u, 0)) + 1
            self._broker_truth_mismatch_streak[symbol_u] = streak
            streak_payload[symbol_u] = streak
            if streak < required:
                ready = False
        if ready:
            return True
        self._audit(
            stage="broker_truth_sync_mismatch_deferred",
            symbol=str(signal_symbol or "").strip().upper(),
            reason="mismatch_confirmation_pending",
            payload={
                "required_confirmations": required,
                "streak": streak_payload,
                "orphan_order_refs": list(getattr(report, "orphan_order_refs", ()) or ()),
                "missing_order_refs": list(getattr(report, "missing_order_refs", ()) or ()),
                "freeze_symbols": list(getattr(report, "freeze_symbols", ()) or ()),
            },
        )
        return False

    def _reset_broker_truth_mismatch_streak(self, *, symbols: tuple[str, ...]) -> None:
        for symbol in symbols:
            symbol_u = str(symbol or "").strip().upper()
            if not symbol_u:
                continue
            self._broker_truth_mismatch_streak.pop(symbol_u, None)

    async def _broker_truth_open_orders_for_runtime(
        self,
        *,
        adapter,
        signal_symbol: str,
        session: str,
        instrument,
        now_ts: float,
    ) -> tuple[tuple[Any, ...] | None, bool]:
        self._harvest_broker_open_orders_refresh(signal_symbol=signal_symbol)
        if self._broker_truth_snapshot_fresh(session=session, instrument=instrument, now_ts=now_ts):
            return tuple(self._broker_open_orders_snapshot or ()), False
        has_snapshot = self._broker_open_orders_snapshot_ts > 0.0
        if has_snapshot:
            self._ensure_broker_open_orders_refresh(adapter=adapter, signal_symbol=signal_symbol)
            return tuple(self._broker_open_orders_snapshot or ()), True
        broker_open_orders = await self._safe_fetch_broker_open_orders(
            adapter=adapter,
            signal_symbol=signal_symbol,
            force_refresh=False,
        )
        if broker_open_orders is None:
            return None, False
        self._store_broker_open_orders_snapshot(broker_open_orders=broker_open_orders, now_ts=now_ts)
        return tuple(self._broker_open_orders_snapshot or ()), False

    def _ensure_broker_open_orders_refresh(self, *, adapter, signal_symbol: str) -> None:
        task = self._broker_open_orders_refresh_task
        if task is not None and not task.done():
            return

        async def _refresh():
            return await self._safe_fetch_broker_open_orders(
                adapter=adapter,
                signal_symbol=signal_symbol,
                force_refresh=False,
            )

        self._broker_open_orders_refresh_task = asyncio.create_task(_refresh())

    def _harvest_broker_open_orders_refresh(self, *, signal_symbol: str) -> None:
        task = self._broker_open_orders_refresh_task
        if task is None or not task.done():
            return
        self._broker_open_orders_refresh_task = None
        try:
            broker_open_orders = task.result()
        except Exception as exc:
            self._audit(
                stage="broker_truth_sync_background_refresh_failed",
                symbol=str(signal_symbol or "").strip().upper(),
                reason="background_refresh_failed",
                payload={"error": str(exc)},
            )
            return
        if broker_open_orders is None:
            return
        self._store_broker_open_orders_snapshot(broker_open_orders=broker_open_orders)

    def _store_broker_open_orders_snapshot(self, *, broker_open_orders, now_ts: float | None = None) -> None:
        self._broker_open_orders_snapshot = tuple(broker_open_orders or ())
        if now_ts is not None and float(now_ts or 0.0) > 0.0:
            self._broker_open_orders_snapshot_ts = float(now_ts)
            return
        self._broker_open_orders_snapshot_ts = float(time.time())

    def _broker_truth_snapshot_fresh(self, *, session: str, instrument, now_ts: float) -> bool:
        snapshot_ts = float(self._broker_open_orders_snapshot_ts or 0.0)
        if snapshot_ts <= 0.0:
            return False
        current_ts = float(now_ts or 0.0)
        if current_ts <= 0.0:
            current_ts = float(time.time())
        return (current_ts - snapshot_ts) <= self._broker_truth_snapshot_ttl_sec(session=session, instrument=instrument)

    async def _safe_fetch_broker_open_orders(self, *, adapter, signal_symbol: str, force_refresh: bool):
        timeout_sec = self._broker_truth_open_orders_timeout_sec()
        try:
            if timeout_sec <= 0.0:
                return await adapter.open_orders(force_refresh=bool(force_refresh))
            return await asyncio.wait_for(
                adapter.open_orders(force_refresh=bool(force_refresh)),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            payload = {"timeout_sec": timeout_sec, "force_refresh": bool(force_refresh)}
            # For non-force probe, fallback to the last cached snapshot to avoid blocking hot path.
            if not force_refresh and self._broker_open_orders_snapshot:
                payload["fallback_cached_snapshot_count"] = len(self._broker_open_orders_snapshot)
                self._audit(
                    stage="broker_truth_sync_probe_timeout",
                    symbol=str(signal_symbol or "").strip().upper(),
                    reason="open_orders_probe_timeout_fallback_cached",
                    payload=payload,
                )
                return tuple(self._broker_open_orders_snapshot or ())
            self._audit(
                stage="broker_truth_sync_probe_timeout",
                symbol=str(signal_symbol or "").strip().upper(),
                reason="open_orders_probe_timeout",
                payload=payload,
            )
            return None
        except Exception as exc:
            self._audit(
                stage="broker_truth_sync_probe_failed",
                symbol=str(signal_symbol or "").strip().upper(),
                reason="open_orders_probe_failed",
                payload={"error": str(exc), "force_refresh": bool(force_refresh)},
            )
            return None

    async def _confirm_broker_truth_mismatch(
        self,
        *,
        adapter,
        signal_symbol: str,
        exec_symbols: tuple[str, ...],
        initial_report,
    ):
        broker_open_orders = await self._safe_fetch_broker_open_orders(
            adapter=adapter,
            signal_symbol=signal_symbol,
            force_refresh=True,
        )
        if broker_open_orders is None:
            self._audit(
                stage="broker_truth_sync_confirmation_deferred",
                symbol=str(signal_symbol or "").strip().upper(),
                reason="force_refresh_probe_unavailable",
                payload={
                    "initial_orphan_order_refs": list(getattr(initial_report, "orphan_order_refs", ()) or ()),
                    "initial_missing_order_refs": list(getattr(initial_report, "missing_order_refs", ()) or ()),
                    "initial_freeze_symbols": list(getattr(initial_report, "freeze_symbols", ()) or ()),
                },
            )
            return None
        self._store_broker_open_orders_snapshot(broker_open_orders=broker_open_orders)
        refreshed_report = self.broker_truth_sync_module.evaluate(
            strategy_id=self.strategy_id,
            kernel=self.execution,
            broker_open_orders=tuple(broker_open_orders or ()),
            tracked_symbols=exec_symbols,
        )
        if refreshed_report.ok:
            self._audit(
                stage="broker_truth_sync_recovered_after_force_refresh",
                symbol=str(signal_symbol or "").strip().upper(),
                reason="force_refresh_cleared_mismatch",
                payload={
                    "initial_orphan_order_refs": list(getattr(initial_report, "orphan_order_refs", ()) or ()),
                    "initial_missing_order_refs": list(getattr(initial_report, "missing_order_refs", ()) or ()),
                },
            )
            return refreshed_report
        return refreshed_report

    def _broker_truth_sync_min_interval_sec(self, *, session: str, instrument) -> float:
        if self.config is None:
            return 10.0
        extras = dict(getattr(self.config, "raw_extras", {}) or {})
        try:
            rth_default = max(1.0, float(extras.get("broker_truth_sync_interval_sec_rth") or 10.0))
        except Exception:
            rth_default = 10.0
        try:
            offsession_default = max(
                1.0,
                float(extras.get("broker_truth_sync_interval_sec_offsession") or rth_default),
            )
        except Exception:
            offsession_default = rth_default
        session_u = str(session or "").strip().upper()
        if session_u == "RTH":
            return rth_default
        allowed_sessions = self._configured_session_window_allowed_sessions()
        if session_u and session_u in allowed_sessions:
            return rth_default
        return offsession_default

    def _broker_truth_open_orders_timeout_sec(self) -> float:
        if self.config is None:
            return 0.35
        extras = dict(getattr(self.config, "raw_extras", {}) or {})
        try:
            return max(0.0, float(extras.get("broker_truth_open_orders_timeout_sec", 0.35) or 0.0))
        except Exception:
            return 0.35

    def _runtime_stage_timing_enabled(self) -> bool:
        if self.config is None:
            return True
        return bool(self.config.raw_extras.get("runtime_stage_timing_enabled", True))

    def _runtime_stage_timing_sample_every(self) -> int:
        if self.config is None:
            return 20
        try:
            return max(1, int(self.config.raw_extras.get("runtime_stage_timing_sample_every", 20) or 20))
        except Exception:
            return 20

    def _runtime_stage_slow_warn_ms(self) -> float:
        if self.config is None:
            return 300.0
        try:
            return max(50.0, float(self.config.raw_extras.get("runtime_stage_slow_warn_ms", 300.0) or 300.0))
        except Exception:
            return 300.0

    def _audit_runtime_stage_timing(self, *, stage_id: str, runtime_state: dict[str, Any], elapsed_ms: float) -> None:
        if not self._runtime_stage_timing_enabled():
            return
        self._runtime_stage_timing_seq += 1
        sample_every = self._runtime_stage_timing_sample_every()
        slow_warn_ms = self._runtime_stage_slow_warn_ms()
        is_slow = float(elapsed_ms) >= slow_warn_ms
        should_sample = (self._runtime_stage_timing_seq % sample_every) == 0
        if not is_slow and not should_sample:
            return
        symbol = str(runtime_state.get("symbol") or "").strip().upper()
        self._audit(
            stage=("runtime_stage_slow" if is_slow else "runtime_stage_timing"),
            symbol=symbol,
            reason=("slow_stage" if is_slow else "sampled_stage_timing"),
            payload={
                "stage_id": str(stage_id or ""),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "slow_warn_ms": float(slow_warn_ms),
                "sample_every": int(sample_every),
            },
        )

    def _broker_truth_snapshot_ttl_sec(self, *, session: str, instrument) -> float:
        if self.config is None:
            return 30.0
        extras = dict(getattr(self.config, "raw_extras", {}) or {})
        min_interval = self._broker_truth_sync_min_interval_sec(session=session, instrument=instrument)
        try:
            rth_ttl = max(
                min_interval,
                float(extras.get("broker_truth_snapshot_ttl_sec_rth") or max(30.0, min_interval)),
            )
        except Exception:
            rth_ttl = max(30.0, min_interval)
        try:
            offsession_ttl = max(
                rth_ttl,
                float(extras.get("broker_truth_snapshot_ttl_sec_offsession") or max(120.0, rth_ttl)),
            )
        except Exception:
            offsession_ttl = max(120.0, rth_ttl)
        session_u = str(session or "").strip().upper()
        if session_u == "RTH":
            return rth_ttl
        allowed_sessions = self._configured_session_window_allowed_sessions()
        if session_u and session_u in allowed_sessions:
            return rth_ttl
        return offsession_ttl

    async def _check_runtime_safety_invariants(self, *, signal_symbol: str, instrument, snapshot) -> None:
        if self.execution is None:
            return
        now_ts = float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0)
        exec_symbols = self._runtime_exec_symbols(symbol=signal_symbol, instrument=instrument)
        if self.flatten_completion_verifier is not None:
            for breach in self.flatten_completion_verifier.sweep(now_ts_epoch_s=now_ts):
                if breach.symbol not in set(exec_symbols):
                    continue
                lot = self.execution.ledger.get_lot(breach.position_lot_id)
                lot_live = (
                    lot is not None
                    and str(getattr(lot, "state", "") or "").strip().lower() in {"live", "pending_exit"}
                    and float(getattr(lot, "live_qty", 0.0) or 0.0) > 0.0
                )
                if not lot_live:
                    self.flatten_completion_verifier.clear_lot(
                        symbol=breach.symbol,
                        position_lot_id=breach.position_lot_id,
                    )
                    continue
                self._audit(
                    stage="flatten_convergence_timeout",
                    symbol=breach.symbol,
                    reason=breach.reason,
                    payload={
                        "position_lot_id": breach.position_lot_id,
                        "age_sec": float(breach.age_sec),
                        "timeout_sec": float(breach.timeout_sec),
                    },
                )
                if self.order_lifecycle_module is not None:
                    self.order_lifecycle_module.mark_sync_issue(symbol=breach.symbol, reason=breach.reason)
                self._activate_symbol_kill_switch(symbol=breach.symbol, reason=breach.reason)
        if self.protection_watchdog is None:
            return
        for exec_symbol in exec_symbols:
            lots = tuple(
                self.execution.ledger.list_lots(
                    strategy_id=self.strategy_id,
                    symbol=exec_symbol,
                    include_closed=False,
                )
            )
            events = self.protection_watchdog.evaluate(lots=lots, now_ts_epoch_s=now_ts)
            if not events:
                continue
            for event in events:
                lot = self.execution.ledger.get_lot(event.position_lot_id)
                if lot is not None and self.protection_module is not None:
                    self.execution.ledger.patch_lot_extras(
                        position_lot_id=event.position_lot_id,
                        updates=self.protection_module.mark_protection_missing(
                            lot=lot,
                            now_ts_epoch_s=now_ts,
                            escalate=bool(event.escalate),
                        ),
                    )
                self._audit(
                    stage="protection_invariant_breach",
                    symbol=event.symbol,
                    reason=event.reason,
                    payload={
                        "position_lot_id": event.position_lot_id,
                        "age_sec": float(event.age_sec),
                        "severity": event.severity,
                        "escalate": bool(event.escalate),
                    },
                )
                if lot is not None and not event.escalate:
                    rebuilt = await self._attempt_rebuild_missing_protection_for_lot(
                        lot=lot,
                        snapshot=snapshot,
                        reason="rebuild_stop_after_missing_protection",
                    )
                    if rebuilt:
                        continue
                if not event.escalate:
                    continue
                if self.order_lifecycle_module is not None:
                    self.order_lifecycle_module.mark_sync_issue(symbol=event.symbol, reason=event.reason)
                self._activate_symbol_kill_switch(symbol=event.symbol, reason=event.reason)
                lot = self.execution.ledger.get_lot(event.position_lot_id)
                if lot is None or bool(getattr(lot, "flatten_order_refs", None)):
                    continue
                if self.ownership_policy is not None:
                    flatten_auth = self.ownership_policy.can_flatten(
                        strategy_id=self.strategy_id,
                        lot=lot,
                        requested_lane_id=str(getattr(lot, "lane_id", "") or "").strip() or None,
                    )
                    if not flatten_auth.allowed:
                        self._audit(
                            stage="watchdog_flatten_blocked_authority",
                            symbol=event.symbol,
                            reason=flatten_auth.reason,
                            payload={
                                "position_lot_id": lot.position_lot_id,
                                **dict(flatten_auth.payload or {}),
                            },
                        )
                        continue
                await self._submit_exit_intent(
                    exit_plan=ExitPlan(
                        symbol=event.symbol,
                        order_role="FLATTEN",
                        qty=float(getattr(lot, "live_qty", 0.0) or 0.0),
                        position_lot_id=lot.position_lot_id,
                        payload={"reason": "unsafe_position_without_protection", "watchdog": True},
                    ),
                    snapshot=snapshot,
                    lot=lot,
                )

    async def _same_strategy_pending_orders(self, *, exec_symbols: list[str]) -> int:
        exec_symbols_key = tuple(exec_symbols or [])
        pending_states = {"pending_entry", "pending_exit"}
        detail: Dict[str, Any] = {
            "exec_symbols": list(exec_symbols or []),
            "ledger_pending_lot_ids": [],
            "ledger_pending_states": {},
            "db_pending": 0,
        }
        pending_lots = []
        ledger_pending = 0
        if self.execution is not None:
            for lot in self.execution.ledger.list_lots(strategy_id=self.strategy_id, include_closed=False):
                if exec_symbols and lot.symbol not in exec_symbols:
                    continue
                state = str(lot.state or "").strip().lower()
                if state in pending_states:
                    ledger_pending += 1
                    pending_lots.append(lot)
                    detail["ledger_pending_lot_ids"].append(lot.position_lot_id)
                    detail["ledger_pending_states"][lot.position_lot_id] = state
        pg = self._resolve_pg()
        if pg is None or not exec_symbols:
            detail["source"] = "ledger_only" if pg is None else "no_exec_symbols"
            detail["ledger_pending"] = ledger_pending
            detail["final_pending"] = ledger_pending
            self._last_same_strategy_pending_detail[exec_symbols_key] = detail
            return ledger_pending
        db_pending_known = True
        try:
            row = await pg.fetchrow(
                """
                SELECT COUNT(*) AS pending_count
                FROM core.core_order
                WHERE strategy_id = $1
                  AND symbol = ANY($2::text[])
                  AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
                """,
                self.strategy_id,
                exec_symbols,
            )
            payload = dict(row) if row is not None else {}
            db_pending = int(payload.get("pending_count") or 0)
        except Exception as exc:
            db_pending_known = False
            db_pending = 0
            detail["db_pending_error"] = str(exc)
        detail["db_pending"] = db_pending
        detail["db_pending_known"] = db_pending_known

        if ledger_pending > 0 and db_pending_known and db_pending == 0:
            db_position_actions = await self._reconcile_pending_entry_lots_from_db_position(
                exec_symbols=exec_symbols,
                now_ts_epoch_s=float(datetime.now().timestamp()),
            )
            if db_position_actions:
                detail["runtime_db_position_reconcile"] = db_position_actions
                pending_lots = []
                ledger_pending = 0
                detail["ledger_pending_lot_ids"] = []
                detail["ledger_pending_states"] = {}
                for lot in self.execution.ledger.list_lots(strategy_id=self.strategy_id, include_closed=False):
                    if exec_symbols and lot.symbol not in exec_symbols:
                        continue
                    state = str(lot.state or "").strip().lower()
                    if state in pending_states:
                        ledger_pending += 1
                        pending_lots.append(lot)
                        detail["ledger_pending_lot_ids"].append(lot.position_lot_id)
                        detail["ledger_pending_states"][lot.position_lot_id] = state
            adapter = self._resolve_broker_adapter()
            if adapter is not None:
                broker_blocked_symbols: list[Dict[str, Any]] = []
                for exec_symbol in exec_symbols:
                    isolation = await self._broker_account_isolation(symbol=exec_symbol)
                    if isolation.get("blocked"):
                        broker_blocked_symbols.append(
                            {
                                "symbol": exec_symbol,
                                "reason": str(isolation.get("reason") or "blocked"),
                                "broker_qty": float(isolation.get("broker_qty") or 0.0),
                                "open_order_count": int(isolation.get("open_order_count") or 0),
                            }
                        )
                detail["broker_blocked_symbols"] = broker_blocked_symbols
                if not broker_blocked_symbols and self.execution is not None:
                    cleanup_actions: list[Dict[str, Any]] = []
                    for lot in pending_lots:
                        state_before = str(lot.state or "").strip().lower()
                        allowed, blocked_reason = self._check_adopt_reconcile_authority(
                            lot=lot,
                            symbol=lot.symbol,
                            blocked_stage="runtime_pending_lot_reconcile_blocked_authority",
                            granted_stage="runtime_pending_lot_reconcile_authority_granted",
                            extra_payload={"state_before": state_before},
                        )
                        if not allowed:
                            cleanup_actions.append(
                                {
                                    "action": "blocked_authority",
                                    "position_lot_id": lot.position_lot_id,
                                    "symbol": lot.symbol,
                                    "state_before": state_before,
                                    "reason": blocked_reason or "adopt_authority_denied",
                                }
                            )
                            continue
                        if state_before == "pending_exit" or float(lot.live_qty or 0.0) > 0.0 or float(lot.filled_qty or 0.0) > 0.0:
                            reconciled = self.execution.ledger.reconcile_broker_qty(
                                position_lot_id=lot.position_lot_id,
                                live_qty=0.0,
                                avg_entry_price=lot.avg_entry_price,
                            )
                            action = "closed_from_broker_db_clean"
                            final_state = str(reconciled.state or "")
                        else:
                            quarantined = self.execution.ledger.quarantine_lot(
                                position_lot_id=lot.position_lot_id,
                                reason="stale_pending_lot_broker_db_clean",
                            )
                            action = "quarantined_from_broker_db_clean"
                            final_state = str(quarantined.state or "")
                        cleanup_payload = {
                            "position_lot_id": lot.position_lot_id,
                            "symbol": lot.symbol,
                            "state_before": state_before,
                            "state_after": final_state,
                            "action": action,
                            "entry_order_ref": lot.entry_order_ref,
                        }
                        cleanup_actions.append(cleanup_payload)
                        self._audit(
                            stage="runtime_pending_lot_reconciled",
                            symbol=lot.symbol,
                            reason="broker_db_clean",
                            payload=cleanup_payload,
                        )
                    detail["runtime_cleanup"] = cleanup_actions
                    ledger_pending = 0
                    pending_lots = []
            else:
                detail["broker_probe"] = "unavailable"

        final_pending = max(ledger_pending, db_pending)
        detail["source"] = "db" if db_pending >= ledger_pending else "ledger"
        detail["ledger_pending"] = ledger_pending
        detail["final_pending"] = final_pending
        self._last_same_strategy_pending_detail[exec_symbols_key] = detail
        return final_pending

    def _inject_planned_qty(self, *, symbol: str, features: Dict[str, Any]) -> Dict[str, Any]:
        """
        Inject `planned_qty` before evaluator/gates/policy run.

        This makes `planned_qty`/`planned_notional` visible to gates/policy without
        requiring upstream services to precompute sizing.
        """
        if features.get("planned_qty") is not None:
            return features
        budget = self._trade_budget_usd()
        if budget <= 0:
            return features
        try:
            exec_px = float(features.get("exec_price")) if features.get("exec_price") is not None else None
        except Exception:
            exec_px = None
        px = exec_px if (exec_px is not None and exec_px > 0) else FeatureRowSnapshotProvider.feature_price(features)
        try:
            px_f = float(px) if px is not None else 0.0
        except Exception:
            px_f = 0.0
        if px_f <= 0:
            return features
        qty = max(1, int(math.floor(budget / px_f + 1e-12)))
        out = dict(features)
        out["planned_qty"] = qty
        if out.get("planned_notional") is None:
            out["planned_notional"] = abs(float(qty)) * float(px_f)
        return out

    def _maybe_apply_trade_budget(self, *, entry_plan: EntryPlan, signal_symbol: str) -> EntryPlan:
        if self.config is None:
            return entry_plan
        payload = dict(getattr(entry_plan, "payload", {}) or {})
        if bool(payload.get("skip_trade_budget_sizing") or payload.get("absolute_signal_qty")):
            self._audit(
                stage="entry_qty_budget_skipped",
                symbol=str(entry_plan.symbol or "").strip().upper(),
                reason="absolute_signal_qty",
                payload={
                    "signal_symbol": str(signal_symbol or "").strip().upper(),
                    "qty": float(entry_plan.qty or 0.0),
                    "trigger_id": getattr(entry_plan, "trigger_id", None),
                },
            )
            return entry_plan
        try:
            budget = float(self._trade_budget_usd() or 0.0)
        except Exception:
            budget = 0.0
        if budget <= 0.0:
            return entry_plan

        exec_symbol = str(entry_plan.symbol or "").strip().upper()
        exec_snapshot = self._resolve_exec_snapshot(exec_symbol=exec_symbol)
        last_px = getattr(exec_snapshot, "last_price", None)
        try:
            px = float(last_px) if last_px is not None else 0.0
        except Exception:
            px = 0.0
        if px <= 0.0:
            self._audit(
                stage="entry_qty_budget_skipped",
                symbol=exec_symbol,
                reason="missing_exec_price",
                payload={
                    "signal_symbol": str(signal_symbol or "").strip().upper(),
                    "trade_budget_usd": budget,
                },
            )
            return entry_plan

        desired_qty = max(1, int(math.floor(budget / px + 1e-12)))
        prev_qty = float(entry_plan.qty or 0.0)
        if abs(float(desired_qty) - prev_qty) <= 1e-9:
            return entry_plan

        self._audit(
            stage="entry_qty_budget_applied",
            symbol=exec_symbol,
            reason="trade_budget_usd",
            payload={
                "signal_symbol": str(signal_symbol or "").strip().upper(),
                "trade_budget_usd": budget,
                "exec_last_price": px,
                "prev_qty": prev_qty,
                "desired_qty": desired_qty,
                "estimated_notional": float(desired_qty) * px,
            },
        )
        return replace(entry_plan, qty=float(desired_qty))

    def _exec_symbol_ready_for_entry(self, *, exec_symbol: str) -> bool:
        snapshot = self._resolve_exec_snapshot(exec_symbol=exec_symbol)
        return snapshot is not None and getattr(snapshot, "last_price", None) is not None

    async def _ensure_exec_symbol_snapshot(self, *, exec_symbol: str):
        snapshot = self._resolve_exec_snapshot(exec_symbol=exec_symbol)
        if snapshot is not None and getattr(snapshot, "last_price", None) is not None:
            return snapshot
        wait_ms = self._exec_snapshot_wait_ms()
        if wait_ms <= 0.0:
            return snapshot
        deadline = asyncio.get_running_loop().time() + (wait_ms / 1000.0)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0.0:
                return self._resolve_exec_snapshot(exec_symbol=exec_symbol)
            await asyncio.sleep(min(0.05, remaining))
            snapshot = self._resolve_exec_snapshot(exec_symbol=exec_symbol)
            if snapshot is not None and getattr(snapshot, "last_price", None) is not None:
                return snapshot

    def _exec_snapshot_wait_ms(self) -> float:
        if self.config is None:
            return 250.0
        try:
            return max(0.0, float(self.config.raw_extras.get("exec_snapshot_wait_ms") or 250.0))
        except Exception:
            return 250.0

    def _remember_latest_snapshot(self, *, symbol: str, features: Dict[str, Any]) -> None:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return
        try:
            snapshot = FeatureRowSnapshotProvider.build_snapshot(symbol=symbol_u, features=features)
        except Exception:
            return
        if getattr(snapshot, "ts_epoch_s", 0.0) <= 0.0:
            return
        self._latest_symbol_snapshots[symbol_u] = snapshot

    def _shared_underlying_data_plane_enabled(self) -> bool:
        if self.config is None:
            return False
        return bool(self.config.raw_extras.get("enable_shared_underlying_data_plane", True))

    def _shared_underlying_namespace(self) -> str:
        if self.config is None:
            return str(self.strategy_id)
        configured = str(self.config.raw_extras.get("shared_underlying_namespace") or "").strip().lower()
        if configured:
            return configured
        return str(self.strategy_id).strip().lower() or "stocks_kernel"

    def _publish_shared_underlying_data(self, *, symbol: str, features: Dict[str, Any]) -> None:
        if not self._shared_underlying_data_plane_enabled():
            return
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return
        snapshot = self._resolve_runtime_snapshot(symbol=symbol_u)
        plane = shared_underlying_data_plane()
        ts_epoch_s = float(getattr(snapshot, "ts_epoch_s", 0.0) or features.get("ts_epoch_s") or 0.0)
        plane.publish_underlying(
            symbol=symbol_u,
            namespace=self._shared_underlying_namespace(),
            ts_epoch_s=ts_epoch_s,
            last_price=getattr(snapshot, "last_price", None) if snapshot is not None else features.get("last_price"),
            bid_price=getattr(snapshot, "bid_price", None) if snapshot is not None else features.get("bid_price"),
            ask_price=getattr(snapshot, "ask_price", None) if snapshot is not None else features.get("ask_price"),
            vwap=getattr(snapshot, "vwap", None) if snapshot is not None else features.get("vwap"),
            features=dict(features or {}),
            bar={
                "ts_epoch_s": ts_epoch_s,
                "open": features.get("open"),
                "high": features.get("high"),
                "low": features.get("low"),
                "close": features.get("close") or getattr(snapshot, "last_price", None),
                "volume": features.get("volume"),
                "vwap": features.get("vwap") or getattr(snapshot, "vwap", None),
            },
        )

    def _resolve_exec_snapshot(self, *, exec_symbol: str):
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return None
        state = self.kernel_state.get(symbol_u) or {}
        readiness = state.get("readiness")
        feature_context = state.get("feature_context")
        snapshot = getattr(feature_context, "snapshot", None) if feature_context is not None else None
        if readiness is not None and bool(getattr(readiness, "ready", False)) and snapshot is not None:
            if getattr(snapshot, "last_price", None) is not None:
                return snapshot
        latest_snapshot = self._latest_symbol_snapshots.get(symbol_u)
        if latest_snapshot is not None and getattr(latest_snapshot, "last_price", None) is not None:
            return latest_snapshot
        return self._price_cache_exec_snapshot(exec_symbol=symbol_u)

    def _resolve_runtime_snapshot(self, *, symbol: str):
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return None
        if symbol_u in self._latest_symbol_snapshots:
            return self._latest_symbol_snapshots.get(symbol_u)
        local = self._resolve_exec_snapshot(exec_symbol=symbol_u)
        if local is not None:
            return local
        if not self._shared_underlying_data_plane_enabled():
            return None
        shared = shared_underlying_data_plane().latest_snapshot(
            symbol=symbol_u,
            namespace=self._shared_underlying_namespace(),
            max_staleness_sec=float(getattr(self.data_profile, "max_staleness_sec", 0.0) or 0.0),
            now_epoch_s=float(datetime.now(ET).timestamp()),
        )
        if shared is None:
            return None
        return type("SharedSnapshotProxy", (), {
            "symbol": shared.symbol,
            "ts_epoch_s": float(shared.ts_epoch_s or 0.0),
            "last_price": shared.last_price,
            "bid_price": shared.bid_price,
            "ask_price": shared.ask_price,
            "vwap": shared.vwap,
        })()

    async def _load_runtime_history(self, *, symbol: str, now_epoch_s: float, min_history_bars: int):
        if self.history_provider is None:
            return None
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return None
        minute_bucket = int(now_epoch_s // 60) if now_epoch_s > 0 else -1
        cached = self._supplemental_history_cache.get(symbol_u)
        if cached is not None:
            cached_bucket, cached_history = cached
            if cached_bucket == minute_bucket and len(getattr(cached_history, "bars_1m", ()) or ()) >= int(min_history_bars):
                return cached_history
        try:
            history = await self.history_provider.load(
                symbol=symbol_u,
                resolution="1m",
                limit=int(min_history_bars),
            )
        except Exception:
            history = None
        if history is None and self._shared_underlying_data_plane_enabled():
            from strategies.stocks_kernel.types import HistoryBar, HistoryBundle

            rows = shared_underlying_data_plane().recent_bars(
                symbol=symbol_u,
                namespace=self._shared_underlying_namespace(),
                limit=int(min_history_bars),
                max_staleness_sec=float(getattr(self.data_profile, "max_staleness_sec", 0.0) or 0.0),
                now_epoch_s=float(now_epoch_s or 0.0),
            )
            if rows:
                bars: list[HistoryBar] = []
                for row in rows:
                    ts_val = row.get("ts")
                    if hasattr(ts_val, "timestamp"):
                        try:
                            ts_epoch_s = float(ts_val.timestamp())
                        except Exception:
                            ts_epoch_s = float(row.get("ts_epoch_s") or 0.0)
                    else:
                        try:
                            ts_epoch_s = float(row.get("ts_epoch_s") or 0.0)
                        except Exception:
                            ts_epoch_s = 0.0
                    bars.append(
                        HistoryBar(
                            ts_epoch_s=ts_epoch_s,
                            open=float(row.get("open", 0.0) or 0.0),
                            high=float(row.get("high", 0.0) or 0.0),
                            low=float(row.get("low", 0.0) or 0.0),
                            close=float(row.get("close", 0.0) or 0.0),
                            volume=float(row.get("volume", 0.0) or 0.0),
                        )
                    )
                history = HistoryBundle(symbol=symbol_u, bars_1m=tuple(bars))
        if history is None:
            return None
        if minute_bucket >= 0 and history is not None:
            self._supplemental_history_cache[symbol_u] = (minute_bucket, history)
        return history

    async def _build_runtime_data_context_bundle(
        self,
        *,
        instrument,
        signal_snapshot,
        signal_history,
        signal_readiness,
        signal_feature_context,
        now_epoch_s: float,
    ):
        min_history_bars = int(getattr(self.data_profile, "min_history_bars", 1) or 1)

        async def _context_map(symbols: tuple[str, ...], scope: str) -> dict[str, dict[str, Any]]:
            out: dict[str, dict[str, Any]] = {}
            for raw_symbol in symbols:
                symbol_u = str(raw_symbol or "").strip().upper()
                if not symbol_u or symbol_u == str(getattr(signal_snapshot, "symbol", "") or "").strip().upper():
                    continue
                snapshot = self._resolve_runtime_snapshot(symbol=symbol_u)
                if snapshot is None:
                    continue
                history = await self._load_runtime_history(
                    symbol=symbol_u,
                    now_epoch_s=now_epoch_s,
                    min_history_bars=min_history_bars,
                )
                readiness = evaluate_data_readiness(
                    snapshot=snapshot,
                    history=history,
                    now_epoch_s=float(now_epoch_s or getattr(signal_snapshot, "ts_epoch_s", 0.0) or 0.0),
                    max_staleness_sec=float(getattr(self.data_profile, "max_staleness_sec", 0.0) or 0.0),
                    min_history_bars=min_history_bars,
                )
                feature_context = None
                if snapshot is not None and history is not None:
                    feature_context = self.feature_registry.build_context(
                        symbol=symbol_u,
                        snapshot=snapshot,
                        history=history,
                        feature_ids=[],
                        feature_params={},
                        scope=scope,
                    )
                out[symbol_u] = {
                    "snapshot": snapshot,
                    "history": history,
                    "readiness": readiness,
                    "feature_context": feature_context,
                }
            return out

        execution_symbols: tuple[str, ...] = ()
        confirm_symbols: tuple[str, ...] = ()
        hedge_symbols: tuple[str, ...] = ()
        if instrument is not None:
            execution_symbols = tuple(sorted({str(item.symbol or "").strip().upper() for item in instrument.lanes("execution")} - {""}))
            confirm_symbols = tuple(
                sorted(
                    ({str(item.symbol or "").strip().upper() for item in instrument.lanes("confirm")} | {str(getattr(instrument, "confirm_symbol", "") or "").strip().upper()})
                    - {""}
                )
            )
            hedge_symbols = tuple(
                sorted(
                    ({str(item.symbol or "").strip().upper() for item in instrument.lanes("hedge")} | {str(getattr(instrument, "volatility_symbol", "") or "").strip().upper()})
                    - {""}
                )
            )
        return build_data_context_bundle(
            signal_symbol=signal_snapshot.symbol,
            signal_snapshot=signal_snapshot,
            signal_history=signal_history,
            signal_readiness=signal_readiness,
            signal_feature_context=signal_feature_context,
            exec_contexts=await _context_map(execution_symbols, "execution"),
            confirm_contexts=await _context_map(confirm_symbols, "confirm"),
            hedge_contexts=await _context_map(hedge_symbols, "hedge"),
        )

    def _scoped_feature_requirements(self) -> dict[str, list[str]]:
        if self.config is None:
            return {}
        raw = dict(self.config.raw_extras.get("scoped_feature_requirements") or {})
        out: dict[str, list[str]] = {}
        for scope, feature_ids in raw.items():
            scope_v = str(scope or "").strip().lower()
            if scope_v not in {"execution", "confirm", "hedge"}:
                continue
            ids = [str(fid).strip() for fid in list(feature_ids or []) if str(fid).strip()]
            if ids:
                out[scope_v] = ids
        return out

    def _gate_enabled(self, gate_id: str) -> bool:
        if self.config is None:
            return False
        gate_key = str(gate_id or "").strip()
        if not gate_key:
            return False
        configured_in_pipeline = gate_key in set(str(item or "").strip() for item in list(self.config.gate_pipeline or []))
        override = dict(self.config.gate_overrides.get(gate_key) or {})
        override_enabled = override.pop("enabled", None)
        enabled = bool(configured_in_pipeline)
        if override_enabled is not None:
            enabled = bool(override_enabled)
        return bool(enabled)

    def _requires_data_context_bundle(self) -> bool:
        if self.config is None:
            return True
        # Backward-compatible default: keep always-on bundle until per-strategy
        # on-demand mode is explicitly enabled.
        if not bool(self.config.raw_extras.get("enable_data_context_bundle_on_demand", False)):
            return True
        if self._scoped_feature_requirements():
            return True
        return self._gate_enabled("multi_scope_readiness")

    def _price_cache_exec_snapshot(self, *, exec_symbol: str):
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return None
        engine = getattr(self.ctx, "engine", None)
        strategy_engine = getattr(engine, "_strategy_engine", None) if engine is not None else None
        price_cache = getattr(strategy_engine, "price_cache", None)
        if price_cache is None or not hasattr(price_cache, "price"):
            return None
        try:
            l1 = price_cache.price(symbol_u)
        except Exception:
            return None
        if l1 is None:
            return None
        try:
            ts_epoch_s = float(getattr(l1, "ts_us", 0) or 0) / 1_000_000.0
        except Exception:
            ts_epoch_s = 0.0
        if ts_epoch_s <= 0.0:
            return None
        now_epoch_s = datetime.now(ET).timestamp()
        max_staleness_sec = float(getattr(self.data_profile, "max_staleness_sec", 0.0) or 0.0)
        if max_staleness_sec > 0.0 and (now_epoch_s - ts_epoch_s) > max_staleness_sec:
            return None
        minute_stats = None
        if hasattr(price_cache, "minute_stats"):
            try:
                minute_stats = price_cache.minute_stats(symbol_u)
            except Exception:
                minute_stats = None
        return type("ExecSnapshotProxy", (), {
            "symbol": symbol_u,
            "ts_epoch_s": ts_epoch_s,
            "last_price": getattr(l1, "last", None),
            "bid_price": getattr(l1, "bid", None),
            "ask_price": getattr(l1, "ask", None),
            "vwap": getattr(minute_stats, "vwap", None) if minute_stats is not None else None,
        })()

    def _emit_market_summary(self, *, snapshot, readiness, feature_context, gate_report, signal, entry_plan) -> None:
        minute_key = int(float(snapshot.ts_epoch_s) // 60)
        prev = self._last_market_summary_minute.get(snapshot.symbol)
        if prev == minute_key:
            return
        self._last_market_summary_minute[snapshot.symbol] = minute_key
        if self.audit_writer is None:
            return
        self.audit_writer.write_event(
            domain="market",
            now=datetime.now(),
            event=KernelExecutionEvent(
                stage="market_summary",
                strategy_id=self.strategy_id,
                strategy_run_id=getattr(self.ctx, "strategy_run_id", None),
                symbol=snapshot.symbol,
                domain="market",
                component="strategy_runtime",
                status="summary",
                reason=signal.action,
                payload={
                    "last_price": snapshot.last_price,
                    "vwap": snapshot.vwap,
                    "session": snapshot.session,
                    "readiness_reason": readiness.reason,
                    "feature_values": dict(feature_context.values),
                    "feature_readiness": dict(feature_context.readiness),
                    "blocked_by": gate_report.blocked_by,
                    "fail_mode": gate_report.fail_mode,
                    "signal_side": signal.side,
                    "entry_qty": getattr(entry_plan, "qty", None),
                },
            ),
        )

    def _emit_feature_state_events(self, *, snapshot, feature_context) -> None:
        if self.audit_writer is None:
            return
        for feature_id, reason in feature_context.readiness.items():
            state_key = (snapshot.symbol, feature_id)
            prev = self._last_feature_state.get(state_key)
            if prev == reason:
                continue
            self._last_feature_state[state_key] = reason
            self.audit_writer.write_event(
                domain="market",
                now=datetime.now(),
                event=KernelExecutionEvent(
                    stage="feature_state_changed",
                    strategy_id=self.strategy_id,
                    strategy_run_id=getattr(self.ctx, "strategy_run_id", None),
                    symbol=snapshot.symbol,
                    domain="market",
                    component="feature_runtime",
                    status="ready" if reason == "ready" else "not_ready",
                    reason=reason,
                    payload={"feature_id": feature_id, "value": feature_context.values.get(feature_id)},
            ),
        )

    async def _submit_intent(self, *, envelope: DecisionEnvelope, symbol: str, side: str) -> None:
        plugin_intent = await self._call_stock_plugin_hook(
            "on_intent",
            envelope=envelope,
            symbol=symbol,
            side=side,
        )
        if plugin_intent is False:
            block_reason = self._stock_plugin_block_reason(envelope=envelope)
            try:
                exec_stock_plugin_intent_blocks_total.labels(
                    strategy_id=self.strategy_id,
                    plugin_component=str(self._stock_plugin_component or "stock_plugin"),
                    symbol=str(symbol or "").strip().upper(),
                    reason=block_reason,
                ).inc()
            except Exception:
                pass
            self._audit(
                stage="stock_plugin_intent_blocked",
                symbol=symbol,
                reason=block_reason,
                payload={
                    "decision_id": envelope.decision_id,
                    "trace_id": str((envelope.extras or {}).get("trace_id") or ""),
                    "plugin_component": self._stock_plugin_component,
                    "side": side,
                },
            )
            return
        if isinstance(plugin_intent, DecisionEnvelope):
            envelope = plugin_intent
        if self._startup_block_reason:
            self._audit(
                stage="startup_blocked_submit_skipped",
                symbol=symbol,
                reason=self._startup_block_reason,
                payload={
                    "decision_id": envelope.decision_id,
                    "trace_id": str(envelope.extras.get("trace_id") or ""),
                    "startup_block_payload": dict(self._startup_block_payload),
                },
            )
            return
        isolation = await self._binding_broker_account_isolation(
            signal_symbol=str(envelope.extras.get("signal_symbol") or ""),
            exec_symbol=symbol,
        )
        if isolation["blocked"]:
            raw_reason = str(isolation["reason"])
            kill_reason = self._normalize_kill_switch_reason(raw_reason)
            self._activate_symbol_kill_switch(symbol=symbol, reason=kill_reason)
            self._audit(
                stage="broker_account_isolation_blocked",
                symbol=symbol,
                reason=raw_reason,
                payload={
                    "decision_id": envelope.decision_id,
                    "trace_id": str(envelope.extras.get("trace_id") or ""),
                    "broker_qty": isolation["broker_qty"],
                    "open_order_count": isolation["open_order_count"],
                    "open_order_refs": isolation["open_order_refs"],
                    "blocked_symbols": isolation.get("blocked_symbols") or [],
                },
            )
            return
        opened_lot = None
        if self.execution is not None:
            trigger_id = str(envelope.extras.get("trigger_id") or "") or None
            lane_id = str(envelope.extras.get("lane_id") or "") or None
            authority = self._execution_lane_authorities(
                signal_symbol=str(envelope.extras.get("signal_symbol") or symbol),
                exec_symbol=symbol,
                lane_id=lane_id,
            )
            opened_lot = self.execution.open_entry_lot(
                strategy_id=self.strategy_id,
                symbol=symbol,
                side=side,
                decision_id=envelope.decision_id,
                lane_id=lane_id,
                ownership_scope=str(envelope.extras.get("ownership_scope") or "strategy"),
                flatten_authority=str(authority.get("flatten_authority") or "strategy"),
                protection_authority=str(authority.get("protection_authority") or "strategy"),
                adopt_authority=str(authority.get("adopt_authority") or "strategy"),
                trigger_id=trigger_id,
            )
            envelope.extras["position_lot_id"] = opened_lot.position_lot_id
            envelope.extras["order_ref"] = opened_lot.order_ref
            protective_oco = self._build_protective_oco_payload(
                signal_symbol=str(envelope.extras.get("signal_symbol") or symbol),
                exec_symbol=symbol,
                lane_id=str(envelope.extras.get("lane_id") or "") or None,
                snapshot=self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("feature_context", None).snapshot
                if self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("feature_context", None) is not None
                else None,
            )
            if protective_oco:
                envelope.extras["protective_oco"] = protective_oco
            if self.order_lifecycle_module is not None:
                self.order_lifecycle_module.register_created(
                    order_ref=opened_lot.order_ref,
                    symbol=symbol,
                    position_lot_id=opened_lot.position_lot_id,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            expected_price = None
            try:
                expected_price = float(self._resolve_exec_snapshot(exec_symbol=symbol).last_price or 0.0)
            except Exception:
                expected_price = None
            if expected_price is not None and expected_price <= 0.0:
                expected_price = None
            if self.execution_quality_module is not None:
                self.execution_quality_module.register_intent(
                    order_ref=opened_lot.order_ref,
                    symbol=symbol,
                    side=side,
                    order_role="ENTRY",
                    expected_price=expected_price,
                    trigger_price=expected_price,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            self.execution.ledger.patch_lot_extras(
                position_lot_id=opened_lot.position_lot_id,
                updates={
                    "entry_ts_epoch_s": float(envelope.ts_bar_end_us) / 1_000_000.0,
                    "entry_requested_ts_epoch_s": float(datetime.now().timestamp()),
                    "signal_symbol": str(envelope.extras.get("signal_symbol") or ""),
                    "stop_plan": dict(self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("stop_plan").payload)
                    if self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("stop_plan") is not None
                    else None,
                    "take_profit_plan": dict(self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("take_profit_plan").payload)
                    if self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("take_profit_plan") is not None
                    else None,
                    "retreat_plan": dict(self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("retreat_plan").payload)
                    if self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("retreat_plan") is not None
                    else None,
                    "protective_oco": dict(envelope.extras.get("protective_oco") or {}),
                    "protection_state": self.protection_module.build_initial_position_state(
                        signal_symbol=str(envelope.extras.get("signal_symbol") or symbol),
                        exec_symbol=symbol,
                        stop_plan=dict(self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("stop_plan").payload)
                        if self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("stop_plan") is not None
                        else None,
                        take_profit_plan=dict(self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("take_profit_plan").payload)
                        if self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("take_profit_plan") is not None
                        else None,
                        retreat_plan=dict(self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("retreat_plan").payload)
                        if self.kernel_state.get(str(envelope.extras.get("signal_symbol") or symbol), {}).get("retreat_plan") is not None
                        else None,
                        protective_oco=dict(envelope.extras.get("protective_oco") or {}),
                    ) if self.protection_module is not None else {},
                },
            )
        self._audit(
            stage="intent_requested",
            symbol=symbol,
            reason=side,
            payload={
                "decision_id": envelope.decision_id,
                "target_qty": envelope.target_qty,
                "trace_id": str(envelope.extras.get("trace_id") or ""),
                "position_lot_id": str(envelope.extras.get("position_lot_id") or "") or None,
                "order_ref": str(envelope.extras.get("order_ref") or "") or None,
            },
        )
        try:
            if self.order_lifecycle_module is not None and opened_lot is not None:
                self.order_lifecycle_module.mark_submitted(
                    order_ref=opened_lot.order_ref,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            result = await self.ctx.submit_intent(envelope)
            submit_failure_reason = self._infer_submit_failure_reason(result)
            if submit_failure_reason is not None:
                if self.execution is not None and opened_lot is not None:
                    self.execution.ledger.quarantine_lot(
                        position_lot_id=opened_lot.position_lot_id,
                        reason=submit_failure_reason,
                    )
                if self._should_activate_kill_switch(submit_failure_reason):
                    self._activate_symbol_kill_switch(symbol=symbol, reason=submit_failure_reason)
                self._audit(
                    stage="intent_submit_rejected",
                    symbol=symbol,
                    reason=submit_failure_reason,
                    payload={
                        "decision_id": envelope.decision_id,
                        "trace_id": str(envelope.extras.get("trace_id") or ""),
                        "position_lot_id": str(envelope.extras.get("position_lot_id") or "") or None,
                        "order_ref": str(envelope.extras.get("order_ref") or "") or None,
                        "result_type": type(result).__name__,
                    },
                )
                if self.order_lifecycle_module is not None and opened_lot is not None:
                    self.order_lifecycle_module.mark_terminal(
                        order_ref=opened_lot.order_ref,
                        state="REJECTED",
                        now_ts_epoch_s=float(datetime.now().timestamp()),
                    )
                return
            self._audit(
                stage="intent_submit_succeeded",
                symbol=symbol,
                reason=side,
                payload={
                    "decision_id": envelope.decision_id,
                    "result_type": type(result).__name__,
                    "trace_id": str(envelope.extras.get("trace_id") or ""),
                    "position_lot_id": str(envelope.extras.get("position_lot_id") or "") or None,
                    "order_ref": str(envelope.extras.get("order_ref") or "") or None,
                },
            )
            broker_order_id, perm_id = self._broker_identity_from_submit_result(result)
            if self.execution is not None and opened_lot is not None:
                self.execution.acknowledge_order(
                    order_ref=opened_lot.order_ref,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                )
            if self.order_lifecycle_module is not None and opened_lot is not None:
                self.order_lifecycle_module.mark_accepted(
                    order_ref=opened_lot.order_ref,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            if self.execution_quality_module is not None and opened_lot is not None:
                quality_ack = self.execution_quality_module.register_submit_ack(
                    order_ref=opened_lot.order_ref,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
                if quality_ack:
                    self._audit(
                        stage="execution_quality_submit_ack",
                        symbol=symbol,
                        reason="ack",
                        payload=quality_ack,
                    )
            self._record_submitted_entry_decision(
                ts_epoch_s=float(envelope.ts_bar_end_us or 0.0) / 1_000_000.0,
                decision_id=envelope.decision_id,
            )
            if self.audit_writer is not None:
                self.audit_writer.write_event(
                    domain="exec",
                    now=datetime.now(),
                    event=build_broker_submit_ack_event(
                        strategy_id=self.strategy_id,
                        strategy_run_id=getattr(self.ctx, "strategy_run_id", None),
                        symbol=symbol,
                        decision_id=envelope.decision_id,
                        trace_id=str(envelope.extras.get("trace_id") or envelope.decision_id),
                        order_ref=str(envelope.extras.get("order_ref") or "") or None,
                        broker_order_id=broker_order_id,
                        perm_id=perm_id,
                        payload={"result_type": type(result).__name__},
                    ),
                )
        except Exception as exc:
            if self.execution is not None and opened_lot is not None:
                self.execution.ledger.quarantine_lot(
                    position_lot_id=opened_lot.position_lot_id,
                    reason="submit_failed",
                )
            if self.order_lifecycle_module is not None and opened_lot is not None:
                self.order_lifecycle_module.mark_terminal(
                    order_ref=opened_lot.order_ref,
                    state="FAILED",
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            self._audit(
                stage="intent_submit_failed",
                symbol=symbol,
                reason=str(exc),
                payload={
                    "decision_id": envelope.decision_id,
                    "trace_id": str(envelope.extras.get("trace_id") or ""),
                    "position_lot_id": str(envelope.extras.get("position_lot_id") or "") or None,
                    "order_ref": str(envelope.extras.get("order_ref") or "") or None,
                },
            )

    async def _manage_live_exits(self, *, symbol: str, instrument, snapshot, feature_context) -> None:
        if self.config is None or self.execution is None or self.submit_bridge is None:
            return
        symbol_u = str(symbol or "").strip().upper()
        await self._sync_db_position(symbol=symbol_u, now_ts_epoch_s=float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0))
        if instrument is not None:
            lots = self._active_binding_lots(instrument=instrument)
        else:
            lots = self.execution.ledger.list_lots(strategy_id=self.strategy_id, symbol=symbol_u, include_closed=False)
        if not lots:
            return
        submitted_lot_ids: set[str] = set()
        for lot in lots:
            if (
                self.execution_semantics_module is not None
                and self.execution_semantics_module.should_skip_exit_for_lot(lot)
            ):
                continue
            lot_id = str(getattr(lot, "position_lot_id", "") or "").strip()
            if lot_id and lot_id in submitted_lot_ids:
                continue
            exit_plan = self._select_exit_plan_for_lot(lot=lot, snapshot=snapshot, feature_context=feature_context)
            if exit_plan is None:
                continue
            await self._submit_exit_intent(exit_plan=exit_plan, snapshot=snapshot, lot=lot)
            if lot_id:
                submitted_lot_ids.add(lot_id)

    async def _execute_policy_exit_actions(self, *, action_plans: tuple[Any, ...], instrument, snapshot, feature_context) -> bool:
        if self.config is None or self.execution is None or self.submit_bridge is None:
            return False
        if instrument is None:
            return False
        triggered = False
        submitted_keys: set[tuple[str, str]] = set()
        rebuilt_lot_ids: set[str] = set()
        for action in action_plans:
            action_type = str(getattr(action, "action_type", "") or "").strip().upper()
            if action_type == ActionType.CANCEL_WORKING:
                cancel_report = await self._execute_cancel_working_action(action=action, instrument=instrument)
                self._audit(
                    stage="policy_action_cancel_working_triggered",
                    symbol=str(cancel_report.get("symbol") or ""),
                    reason="cancel_working",
                    payload=cancel_report,
                )
                if int(cancel_report.get("cancelled_count") or 0) > 0:
                    triggered = True
                continue
            if action_type == ActionType.ENSURE_PROTECTION:
                ensure_report = await self._execute_ensure_protection_action(
                    action=action,
                    instrument=instrument,
                    snapshot=snapshot,
                    rebuilt_lot_ids=rebuilt_lot_ids,
                )
                self._audit(
                    stage="policy_action_ensure_protection_triggered",
                    symbol=str(ensure_report.get("symbol") or ""),
                    reason="ensure_protection",
                    payload=ensure_report,
                )
                if int(ensure_report.get("rebuilt_count") or 0) > 0:
                    triggered = True
                continue
            if action_type not in {
                ActionType.FLATTEN,
                ActionType.STOP,
                ActionType.TAKE_PROFIT,
                ActionType.RETREAT,
                ActionType.REDUCE,
            }:
                continue
            order_role = self._order_role_from_action_type(action_type)
            if order_role is None:
                continue
            side_filter = str(getattr(action, "side", "") or "").strip().upper()
            action_symbol = str(getattr(action, "symbol", "") or "").strip().upper()
            target_lot_id = str(getattr(action, "position_lot_id", "") or "").strip()
            lots = self._active_binding_lots(instrument=instrument)
            for lot in lots:
                if (
                    self.execution_semantics_module is not None
                    and self.execution_semantics_module.should_skip_exit_for_lot(lot)
                ):
                    continue
                lot_id = str(getattr(lot, "position_lot_id", "") or "").strip()
                if target_lot_id and lot_id != target_lot_id:
                    continue
                if side_filter in {"LONG", "SHORT"} and str(lot.side or "").strip().upper() != side_filter:
                    continue
                if action_symbol:
                    signal_symbol = str(getattr(instrument, "signal_symbol", "") or "").strip().upper()
                    if action_symbol not in {str(lot.symbol or "").strip().upper(), signal_symbol}:
                        continue
                qty_hint = getattr(action, "qty", None)
                try:
                    qty_f = float(qty_hint) if qty_hint is not None else float(lot.live_qty or 0.0)
                except Exception:
                    qty_f = float(lot.live_qty or 0.0)
                qty_f = max(0.0, min(float(lot.live_qty or 0.0), qty_f))
                if qty_f <= 0.0:
                    continue
                submit_key = (lot_id, order_role)
                if submit_key in submitted_keys:
                    continue
                payload = dict(getattr(action, "payload", {}) or {})
                payload.setdefault("reason", str(getattr(action, "reason", "") or f"policy_action_{order_role.lower()}"))
                payload.setdefault("action_type", action_type)
                await self._submit_exit_intent(
                    exit_plan=ExitPlan(
                        symbol=lot.symbol,
                        order_role=order_role,
                        qty=qty_f,
                        position_lot_id=lot.position_lot_id,
                        payload=payload,
                    ),
                    snapshot=snapshot,
                    lot=lot,
                )
                self._audit(
                    stage="policy_action_exit_triggered",
                    symbol=lot.symbol,
                    reason=action_type.lower(),
                    payload={
                        "position_lot_id": lot.position_lot_id,
                        "order_role": order_role,
                        "qty": qty_f,
                        "action_symbol": action_symbol or None,
                    },
                )
                submitted_keys.add(submit_key)
                triggered = True
        return triggered

    async def _execute_cancel_working_action(self, *, action: Any, instrument) -> dict[str, Any]:
        action_symbol = str(getattr(action, "symbol", "") or "").strip().upper()
        side_filter = str(getattr(action, "side", "") or "").strip().upper()
        target_lot_id = str(getattr(action, "position_lot_id", "") or "").strip()
        target_order_refs = {
            str(item).strip()
            for item in tuple(dict(getattr(action, "payload", {}) or {}).get("order_refs") or ())
            if str(item).strip()
        }
        lots = self._active_binding_lots(instrument=instrument)
        lot_order_refs: set[str] = set()
        matched_lot_ids: set[str] = set()
        exec_symbols = {str(item).strip().upper() for item in tuple(getattr(instrument, "exec_symbols", lambda: ())() or ()) if str(item).strip()}
        signal_symbol = str(getattr(instrument, "signal_symbol", "") or "").strip().upper()
        for lot in lots:
            lot_id = str(getattr(lot, "position_lot_id", "") or "").strip()
            if target_lot_id and lot_id != target_lot_id:
                continue
            if side_filter in {"LONG", "SHORT"} and str(getattr(lot, "side", "") or "").strip().upper() != side_filter:
                continue
            lot_symbol = str(getattr(lot, "symbol", "") or "").strip().upper()
            if action_symbol and action_symbol not in {lot_symbol, signal_symbol}:
                continue
            matched_lot_ids.add(lot_id)
            for ref in (
                str(getattr(lot, "entry_order_ref", "") or "").strip(),
                *[str(item).strip() for item in list(getattr(lot, "protection_order_refs", []) or [])],
                *[str(item).strip() for item in list(getattr(lot, "flatten_order_refs", []) or [])],
            ):
                if ref:
                    lot_order_refs.add(ref)
        if target_order_refs:
            lot_order_refs.update(target_order_refs)
        cancel_fn = self._resolve_broker_cancel_fn()
        orders = await self._open_orders_for_cancel_action()
        scanned = 0
        cancelled = 0
        for order in orders:
            if self._is_terminal_order_state(order):
                continue
            order_ref = self._open_order_ref(order)
            order_symbol = self._open_order_symbol(order)
            order_strategy_id = str(getattr(order, "strategy_id", "") or "").strip()
            if lot_order_refs and order_ref and order_ref not in lot_order_refs:
                continue
            if not lot_order_refs:
                if order_strategy_id and order_strategy_id != self.strategy_id:
                    continue
                if order_ref and not order_ref_belongs_to_strategy(
                    order_ref,
                    namespace=self.config.order_namespace,
                    strategy_id=self.strategy_id,
                ):
                    continue
                if action_symbol:
                    allowed_symbols = set(exec_symbols)
                    if signal_symbol:
                        allowed_symbols.add(signal_symbol)
                    if action_symbol in allowed_symbols:
                        if order_symbol and order_symbol not in allowed_symbols:
                            continue
                    elif order_symbol and order_symbol != action_symbol:
                        continue
            scanned += 1
            if cancel_fn is None:
                continue
            cancel_id = self._cancel_id_from_open_order(order)
            if cancel_id is None:
                continue
            try:
                result = await cancel_fn(int(cancel_id))
            except Exception as exc:
                self._audit(
                    stage="policy_action_cancel_working_error",
                    symbol=order_symbol or action_symbol or "SYSTEM",
                    reason="cancel_error",
                    payload={"cancel_id": cancel_id, "error": str(exc), "order_ref": order_ref or None},
                )
                continue
            if result is False:
                continue
            cancelled += 1
        return {
            "symbol": action_symbol or signal_symbol,
            "scanned_count": scanned,
            "cancelled_count": cancelled,
            "target_lot_id": target_lot_id or None,
            "target_order_refs": sorted(lot_order_refs),
            "matched_lot_count": len(matched_lot_ids),
        }

    async def _execute_ensure_protection_action(
        self,
        *,
        action: Any,
        instrument,
        snapshot,
        rebuilt_lot_ids: set[str],
    ) -> dict[str, Any]:
        action_symbol = str(getattr(action, "symbol", "") or "").strip().upper()
        side_filter = str(getattr(action, "side", "") or "").strip().upper()
        target_lot_id = str(getattr(action, "position_lot_id", "") or "").strip()
        lots = self._active_binding_lots(instrument=instrument)
        rebuilt_count = 0
        considered_count = 0
        for lot in lots:
            if (
                self.execution_semantics_module is not None
                and self.execution_semantics_module.should_skip_exit_for_lot(lot)
            ):
                continue
            lot_id = str(getattr(lot, "position_lot_id", "") or "").strip()
            if target_lot_id and lot_id != target_lot_id:
                continue
            if side_filter in {"LONG", "SHORT"} and str(getattr(lot, "side", "") or "").strip().upper() != side_filter:
                continue
            if action_symbol:
                signal_symbol = str(getattr(instrument, "signal_symbol", "") or "").strip().upper()
                if action_symbol not in {str(getattr(lot, "symbol", "") or "").strip().upper(), signal_symbol}:
                    continue
            if lot_id in rebuilt_lot_ids:
                continue
            considered_count += 1
            rebuilt = await self._attempt_rebuild_missing_protection_for_lot(
                lot=lot,
                snapshot=snapshot,
                reason=str(getattr(action, "reason", "") or "policy_action_ensure_protection"),
            )
            if rebuilt:
                rebuilt_lot_ids.add(lot_id)
                rebuilt_count += 1
        return {
            "symbol": action_symbol or str(getattr(instrument, "signal_symbol", "") or "").strip().upper(),
            "rebuilt_count": rebuilt_count,
            "considered_count": considered_count,
            "target_lot_id": target_lot_id or None,
        }

    def _resolve_broker_cancel_fn(self):
        engine = getattr(self.ctx, "engine", None)
        candidates = (
            getattr(getattr(getattr(engine, "execution_service", None), "kernel", None), "broker", None),
            getattr(engine, "broker_adapter", None),
            getattr(getattr(engine, "market_data_service", None), "ib_adapter", None),
        )
        for adapter in candidates:
            for fn_name in ("cancel", "cancel_order"):
                cancel_fn = getattr(adapter, fn_name, None)
                if not callable(cancel_fn):
                    continue

                async def _cancel(order_id: int, _fn=cancel_fn):
                    result = _fn(order_id)
                    if inspect.isawaitable(result):
                        return await result
                    return result

                return _cancel
        return None

    async def _open_orders_for_cancel_action(self, *, force_refresh: bool = True) -> list[Any]:
        adapter = self._resolve_broker_adapter()
        if adapter is not None and hasattr(adapter, "open_orders"):
            try:
                orders = await adapter.open_orders(force_refresh=bool(force_refresh))
                return list(orders or [])
            except Exception:
                pass
        ctx_open_orders = getattr(self.ctx, "open_orders", None)
        if callable(ctx_open_orders):
            try:
                result = ctx_open_orders()
                if inspect.isawaitable(result):
                    result = await result
                return list(result or [])
            except Exception:
                return []
        return []

    async def _cancel_order_refs_best_effort(
        self,
        *,
        order_refs: set[str],
        symbol: str,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        refs = {str(ref).strip() for ref in set(order_refs or set()) if str(ref).strip()}
        if not refs:
            return {"cancelled_count": 0, "scanned_count": 0, "requested_refs": []}
        cancel_fn = self._resolve_broker_cancel_fn()
        orders = await self._open_orders_for_cancel_action(force_refresh=force_refresh)
        cancelled = 0
        scanned = 0
        for order in orders:
            if self._is_terminal_order_state(order):
                continue
            order_ref = self._open_order_ref(order)
            if order_ref not in refs:
                continue
            scanned += 1
            if cancel_fn is None:
                continue
            cancel_id = self._cancel_id_from_open_order(order)
            if cancel_id is None:
                continue
            try:
                result = await cancel_fn(int(cancel_id))
            except Exception as exc:
                self._audit(
                    stage="stale_pending_entry_cancel_error",
                    symbol=str(symbol or "").strip().upper() or "SYSTEM",
                    reason="cancel_error",
                    payload={"cancel_id": cancel_id, "order_ref": order_ref, "error": str(exc)},
                )
                continue
            if result is False:
                continue
            cancelled += 1
        return {"cancelled_count": cancelled, "scanned_count": scanned, "requested_refs": sorted(refs)}

    @staticmethod
    def _is_terminal_order_state(order: Any) -> bool:
        state = str(getattr(order, "state", "") or "").strip().replace(" ", "").replace("_", "").upper()
        return state in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED", "EXPIRED", "FAILED"}

    @staticmethod
    def _open_order_symbol(order: Any) -> str:
        symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        if symbol:
            return symbol
        contract = getattr(order, "contract", None)
        if contract is not None:
            symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
            if symbol:
                return symbol
        return ""

    @staticmethod
    def _open_order_ref(order: Any) -> str:
        order_ref = str(getattr(order, "order_ref", "") or "").strip()
        if order_ref:
            return order_ref
        ib_order = getattr(order, "order", None)
        if ib_order is not None:
            order_ref = str(getattr(ib_order, "orderRef", "") or "").strip()
            if order_ref:
                return order_ref
        return ""

    @staticmethod
    def _cancel_id_from_open_order(order: Any) -> int | None:
        for field in ("broker_order_id", "order_id", "id"):
            raw = getattr(order, field, None)
            if raw is None:
                continue
            try:
                value = str(raw).strip()
                if value:
                    return int(value)
            except Exception:
                continue
        return None

    async def _maybe_force_eod_flatten(self, *, symbol: str, instrument, snapshot) -> bool:
        if self.config is None or self.execution is None:
            return False
        due, cutoff_et = self.session_module.eod_flatten_due(
            ts_epoch_s=float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0),
        ) if self.session_module is not None else (False, "")
        if not due:
            return False
        flattened_lot_ids: set[str] = set()
        exec_symbols = self._runtime_exec_symbols(symbol=symbol, instrument=instrument)
        for exec_symbol in exec_symbols:
            await self._sync_db_position(
                symbol=exec_symbol,
                now_ts_epoch_s=float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0),
            )
            lots = self.execution.ledger.list_lots(
                strategy_id=self.strategy_id,
                symbol=exec_symbol,
                include_closed=False,
            )
            for lot in lots:
                if (
                    self.execution_semantics_module is not None
                    and self.execution_semantics_module.should_skip_exit_for_lot(lot)
                ):
                    continue
                lot_id = str(getattr(lot, "position_lot_id", "") or "").strip()
                if lot_id and lot_id in flattened_lot_ids:
                    continue
                await self._submit_exit_intent(
                    exit_plan=ExitPlan(
                        symbol=lot.symbol,
                        order_role="FLATTEN",
                        qty=float(lot.live_qty),
                        position_lot_id=lot.position_lot_id,
                        payload={"reason": "eod_flatten", "flatten_before_close_et": cutoff_et},
                    ),
                    snapshot=snapshot,
                    lot=lot,
                )
                self._audit(
                    stage="eod_flatten_requested",
                    symbol=lot.symbol,
                    reason="flatten_before_close_et",
                    payload={
                        "position_lot_id": lot.position_lot_id,
                        "live_qty": float(lot.live_qty or 0.0),
                        "flatten_before_close_et": cutoff_et,
                    },
                )
                if lot_id:
                    flattened_lot_ids.add(lot_id)
        return bool(flattened_lot_ids)

    async def _sync_db_position(self, *, symbol: str, now_ts_epoch_s: float) -> None:
        if self.execution is None:
            return
        pg = self._resolve_pg()
        if pg is None:
            return
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return
        last = float(self._last_db_sync_ts.get(symbol_u) or 0.0)
        # Rate limit: DB read per symbol max once every ~15s.
        if now_ts_epoch_s > 0.0 and (now_ts_epoch_s - last) < 15.0:
            return
        active = self.execution.ledger.list_lots(strategy_id=self.strategy_id, symbol=symbol_u, include_closed=False)
        if not active:
            return
        if len(active) != 1:
            return
        active_lot = active[0]
        extras = dict(getattr(getattr(self, "config", None), "raw_extras", {}) or {})
        strategy_instance_id = str(extras.get("strategy_instance_id") or self.strategy_id or "").strip()
        account_id = str(extras.get("account_id") or extras.get("account") or "").strip() or None
        allowed, blocked_reason = self._check_adopt_reconcile_authority(
            lot=active_lot,
            symbol=symbol_u,
            blocked_stage="db_position_reconcile_blocked_authority",
            granted_stage="db_position_reconcile_authority_granted",
            extra_payload={
                "sync_source": "position_ledger_current",
                "strategy_instance_id": strategy_instance_id,
                "position_lot_id": str(getattr(active_lot, "position_lot_id", "") or "") or None,
            },
        )
        if not allowed:
            self._audit(
                stage="db_position_sync_skipped",
                symbol=symbol_u,
                reason=blocked_reason or "adopt_authority_denied",
                payload={
                    "position_lot_id": str(getattr(active_lot, "position_lot_id", "") or "") or None,
                    "sync_source": "position_ledger_current",
                },
            )
            return
        try:
            row = await pg.fetchrow(
                """
                SELECT account_id, strategy_id, strategy_instance_id, symbol, lot_id, qty, avg_px
                FROM core.position_ledger_current
                WHERE strategy_id = $1
                  AND COALESCE(NULLIF(strategy_instance_id, ''), strategy_id) = $2
                  AND symbol = $3
                  AND lot_id = $4
                  AND ($5::text IS NULL OR account_id = $5::text)
                  AND ABS(COALESCE(qty, 0)) > 1e-9
                """,
                self.strategy_id,
                strategy_instance_id,
                symbol_u,
                str(getattr(active_lot, "position_lot_id", "") or "").strip(),
                account_id,
            )
        except Exception as exc:
            self._audit(
                stage="db_position_sync_failed",
                symbol=symbol_u,
                reason="db_query_failed",
                payload={"error": str(exc)},
            )
            return
        payload = row
        if payload is None:
            payload = {}
        elif not isinstance(payload, dict):
            try:
                payload = dict(payload)
            except Exception:
                payload = {}
        pos_qty = float(payload.get("qty") or 0.0)
        avg_px = payload.get("avg_px")
        avg_entry = None
        try:
            avg_entry = float(avg_px) if avg_px is not None else None
        except Exception:
            avg_entry = None
        try:
            self.execution.ledger.reconcile_broker_qty(
                position_lot_id=active_lot.position_lot_id,
                live_qty=abs(pos_qty),
                avg_entry_price=avg_entry,
            )
            if now_ts_epoch_s > 0.0:
                self._last_db_sync_ts[symbol_u] = float(now_ts_epoch_s)
        except Exception as exc:
            self._audit(
                stage="db_position_sync_failed",
                symbol=symbol_u,
                reason="reconcile_failed",
                payload={"error": str(exc), "position_lot_id": active_lot.position_lot_id},
            )

    def _select_exit_plan_for_lot(self, *, lot, snapshot, feature_context) -> ExitPlan | None:
        protection_decision = None
        if self.protection_module is not None:
            protection_decision = self.protection_module.evaluate_live_management(
                lot=lot,
                snapshot=snapshot,
                feature_context=feature_context,
            )
            updates = dict((protection_decision or {}).get("updates") or {})
            if updates:
                self.execution.ledger.patch_lot_extras(
                    position_lot_id=lot.position_lot_id,
                    updates=updates,
                )
                audit_reason = str((protection_decision or {}).get("audit_reason") or "protection_state_updated")
                self._audit(
                    stage="protection_lifecycle_updated",
                    symbol=str(getattr(lot, "symbol", "") or ""),
                    reason=audit_reason,
                    payload=dict((protection_decision or {}).get("audit_payload") or {}),
                )
                lot = self.execution.ledger.get_lot(lot.position_lot_id) or lot
            exit_plan = (protection_decision or {}).get("exit_plan")
            if exit_plan is not None:
                return exit_plan
        if self.execution_semantics_module is None:
            return None
        fallback = self.execution_semantics_module.select_exit_plan_for_lot(
            lot=lot,
            snapshot=snapshot,
            feature_context=feature_context,
        )
        if fallback is None:
            return None
        if (
            self.protection_module is not None
            and str(getattr(fallback, "order_role", "") or "").strip().upper() in {"STOP", "TAKE_PROFIT"}
            and dict(getattr(lot, "extras", {}) or {}).get("protection_state")
        ):
            return None
        return fallback

    async def _submit_exit_intent(self, *, exit_plan: ExitPlan, snapshot, lot) -> None:
        if self.execution is None or self.submit_bridge is None:
            return
        if self.ownership_policy is not None:
            authority = (
                self.ownership_policy.can_flatten(
                    strategy_id=self.strategy_id,
                    lot=lot,
                    requested_lane_id=str(getattr(exit_plan, "lane_id", "") or "").strip() or str(getattr(lot, "lane_id", "") or "").strip() or None,
                )
                if str(exit_plan.order_role or "").strip().upper() == "FLATTEN"
                else self.ownership_policy.can_manage_protection(
                    strategy_id=self.strategy_id,
                    lot=lot,
                    requested_lane_id=str(getattr(exit_plan, "lane_id", "") or "").strip() or str(getattr(lot, "lane_id", "") or "").strip() or None,
                )
            )
            if not authority.allowed:
                self._audit_authority_decision(
                    stage="exit_blocked_authority",
                    symbol=exit_plan.symbol,
                    decision=authority,
                    lot=lot,
                    extra_payload={"requested_order_role": str(exit_plan.order_role or "").strip().upper()},
                )
                return
        if self.protection_module is not None:
            updates = self.protection_module.on_exit_intent(
                lot=lot,
                exit_plan=exit_plan,
                now_ts_epoch_s=float(datetime.now().timestamp()),
            )
            if updates:
                self.execution.ledger.patch_lot_extras(
                    position_lot_id=lot.position_lot_id,
                    updates=updates,
                )
        flatten_binding = self.execution.request_flatten(
            position_lot_id=lot.position_lot_id,
            strategy_id=self.strategy_id,
            symbol=exit_plan.symbol,
            lane_id=getattr(exit_plan, "lane_id", None) or getattr(lot, "lane_id", None),
            ownership_scope=str(getattr(exit_plan, "ownership_scope", getattr(lot, "ownership_scope", "strategy")) or "strategy"),
            trigger_id=str(exit_plan.payload.get("reason") or exit_plan.order_role or "exit"),
        )
        if str(exit_plan.order_role or "").strip().upper() == "FLATTEN" and self.flatten_completion_verifier is not None:
            self.flatten_completion_verifier.mark_flatten_submitted(
                symbol=exit_plan.symbol,
                position_lot_id=lot.position_lot_id,
                now_ts_epoch_s=float(datetime.now().timestamp()),
            )
        envelope = self.submit_bridge.build_exit_envelope(exit_plan=exit_plan, snapshot=snapshot, lot=lot)
        envelope.extras["order_ref"] = flatten_binding.order_ref
        envelope.extras["position_lot_id"] = lot.position_lot_id
        envelope.extras["exit_reason"] = str(exit_plan.payload.get("reason") or exit_plan.order_role or "exit")
        envelope.extras.update(self._execution_session_extras(snapshot=snapshot))
        if self.order_lifecycle_module is not None:
            self.order_lifecycle_module.register_created(
                order_ref=flatten_binding.order_ref,
                symbol=exit_plan.symbol,
                position_lot_id=lot.position_lot_id,
                now_ts_epoch_s=float(datetime.now().timestamp()),
            )
        if self.execution_quality_module is not None:
            expected_price = None
            try:
                expected_price = float(getattr(snapshot, "last_price", None) or 0.0)
            except Exception:
                expected_price = None
            if expected_price is not None and expected_price <= 0.0:
                expected_price = None
            self.execution_quality_module.register_intent(
                order_ref=flatten_binding.order_ref,
                symbol=exit_plan.symbol,
                side=str(lot.side or ""),
                order_role=str(exit_plan.order_role or "FLATTEN"),
                expected_price=expected_price,
                trigger_price=expected_price,
                now_ts_epoch_s=float(datetime.now().timestamp()),
            )
        self._audit(
            stage="exit_plan_triggered",
            symbol=exit_plan.symbol,
            reason=str(exit_plan.order_role or "FLATTEN"),
            payload={
                "position_lot_id": lot.position_lot_id,
                "qty": float(exit_plan.qty),
                "order_ref": flatten_binding.order_ref,
                "payload": dict(exit_plan.payload),
            },
        )
        self._audit(
            stage="exit_intent_requested",
            symbol=exit_plan.symbol,
            reason=str(exit_plan.order_role or "FLATTEN"),
            payload={
                "decision_id": envelope.decision_id,
                "position_lot_id": lot.position_lot_id,
                "order_ref": flatten_binding.order_ref,
            },
        )
        try:
            if self.order_lifecycle_module is not None:
                self.order_lifecycle_module.mark_submitted(
                    order_ref=flatten_binding.order_ref,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            result = await self.ctx.submit_intent(envelope)
            submit_failure_reason = self._infer_submit_failure_reason(result)
            if submit_failure_reason is not None:
                self.execution.ledger.quarantine_lot(position_lot_id=lot.position_lot_id, reason=submit_failure_reason)
                if self._should_activate_kill_switch(submit_failure_reason):
                    self._activate_symbol_kill_switch(symbol=exit_plan.symbol, reason=submit_failure_reason)
                self._audit(
                    stage="exit_intent_submit_rejected",
                    symbol=exit_plan.symbol,
                    reason=submit_failure_reason,
                    payload={
                        "decision_id": envelope.decision_id,
                        "position_lot_id": lot.position_lot_id,
                        "order_ref": flatten_binding.order_ref,
                        "result_type": type(result).__name__,
                    },
                )
                if self.order_lifecycle_module is not None:
                    self.order_lifecycle_module.mark_terminal(
                        order_ref=flatten_binding.order_ref,
                        state="REJECTED",
                        now_ts_epoch_s=float(datetime.now().timestamp()),
                    )
                return
            broker_order_id, perm_id = self._broker_identity_from_submit_result(result)
            self.execution.acknowledge_order(
                order_ref=flatten_binding.order_ref,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
            )
            if self.order_lifecycle_module is not None:
                self.order_lifecycle_module.mark_accepted(
                    order_ref=flatten_binding.order_ref,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            if self.execution_quality_module is not None:
                quality_ack = self.execution_quality_module.register_submit_ack(
                    order_ref=flatten_binding.order_ref,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
                if quality_ack:
                    self._audit(
                        stage="execution_quality_submit_ack",
                        symbol=exit_plan.symbol,
                        reason="ack",
                        payload=quality_ack,
                    )
            self._audit(
                stage="exit_intent_submit_succeeded",
                symbol=exit_plan.symbol,
                reason=str(exit_plan.order_role or "FLATTEN"),
                payload={
                    "decision_id": envelope.decision_id,
                    "position_lot_id": lot.position_lot_id,
                    "order_ref": flatten_binding.order_ref,
                    "broker_order_id": broker_order_id,
                    "perm_id": perm_id,
                },
            )
        except Exception as exc:
            self.execution.ledger.quarantine_lot(position_lot_id=lot.position_lot_id, reason="exit_submit_failed")
            if self.order_lifecycle_module is not None:
                self.order_lifecycle_module.mark_terminal(
                    order_ref=flatten_binding.order_ref,
                    state="FAILED",
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            self._audit(
                stage="exit_intent_submit_failed",
                symbol=exit_plan.symbol,
                reason=str(exc),
                payload={
                    "decision_id": envelope.decision_id,
                    "position_lot_id": lot.position_lot_id,
                    "order_ref": flatten_binding.order_ref,
                },
            )

    async def _attempt_rebuild_missing_protection_for_lot(self, *, lot, snapshot, reason: str) -> bool:
        if self.execution is None or self.submit_bridge is None:
            return False
        if float(getattr(lot, "live_qty", 0.0) or 0.0) <= 0.0:
            return False
        if bool(getattr(lot, "protection_order_refs", None)):
            return False
        lot_extras = dict(getattr(lot, "extras", {}) or {})
        protective_oco = dict(lot_extras.get("protective_oco") or {})
        if not protective_oco:
            return False
        lot_symbol = str(getattr(lot, "symbol", "") or "").strip().upper()
        if self.protection_module is not None:
            protective_oco = self.protection_module.with_exec_market_ref(
                payload=protective_oco,
                exec_symbol=lot_symbol,
                snapshot=self._resolve_exec_snapshot(exec_symbol=lot_symbol),
            )
        if self.ownership_policy is not None:
            protection_auth = self.ownership_policy.can_manage_protection(
                strategy_id=self.strategy_id,
                lot=lot,
                requested_lane_id=str(getattr(lot, "lane_id", "") or "").strip() or None,
            )
            if not protection_auth.allowed:
                self._audit_authority_decision(
                    stage="protection_rebuild_blocked_authority",
                    symbol=str(getattr(lot, "symbol", "") or ""),
                    decision=protection_auth,
                    lot=lot,
                    extra_payload={"rebuild_reason": reason},
                )
                return False
        now_ts = float(datetime.now().timestamp())
        last_attempt_ts = float(lot_extras.get("protection_rebuild_attempt_ts_epoch_s") or 0.0)
        if last_attempt_ts > 0.0 and now_ts - last_attempt_ts < 5.0:
            return False
        stop_binding = self.execution.register_stop_order(
            position_lot_id=lot.position_lot_id,
            strategy_id=self.strategy_id,
            symbol=lot_symbol,
            lane_id=getattr(lot, "lane_id", None),
            ownership_scope=str(getattr(lot, "ownership_scope", "strategy") or "strategy"),
            trigger_id=reason,
        )
        exit_plan = ExitPlan(
            symbol=lot_symbol,
            order_role="STOP",
            qty=float(getattr(lot, "live_qty", 0.0) or 0.0),
            position_lot_id=lot.position_lot_id,
            lane_id=getattr(lot, "lane_id", None),
            ownership_scope=str(getattr(lot, "ownership_scope", "strategy") or "strategy"),
            payload={
                "reason": reason,
                "protective_rebuild": True,
                "protective_oco": protective_oco,
            },
        )
        envelope = self.submit_bridge.build_exit_envelope(exit_plan=exit_plan, snapshot=snapshot, lot=lot)
        envelope.extras["order_ref"] = stop_binding.order_ref
        envelope.extras["position_lot_id"] = lot.position_lot_id
        envelope.extras["exit_reason"] = reason
        envelope.extras["protective_oco"] = protective_oco
        envelope.extras["protective_rebuild"] = True
        envelope.extras.update(self._execution_session_extras(snapshot=snapshot))
        if self.order_lifecycle_module is not None:
            self.order_lifecycle_module.register_created(
                order_ref=stop_binding.order_ref,
                symbol=exit_plan.symbol,
                position_lot_id=lot.position_lot_id,
                now_ts_epoch_s=now_ts,
            )
            self.order_lifecycle_module.mark_submitted(
                order_ref=stop_binding.order_ref,
                now_ts_epoch_s=now_ts,
            )
            self.order_lifecycle_module.mark_sync_issue(symbol=exit_plan.symbol, reason="repair_pending_missing_protection")
        if self.protection_module is not None:
            self.execution.ledger.patch_lot_extras(
                position_lot_id=lot.position_lot_id,
                updates={
                    "protection_rebuild_attempt_ts_epoch_s": now_ts,
                    **self.protection_module.mark_protection_missing(
                        lot=lot,
                        now_ts_epoch_s=now_ts,
                        escalate=False,
                    ),
                },
            )
        self._audit(
            stage="protection_rebuild_requested",
            symbol=exit_plan.symbol,
            reason=reason,
            payload={
                "position_lot_id": lot.position_lot_id,
                "order_ref": stop_binding.order_ref,
            },
        )
        try:
            result = await self.ctx.submit_intent(envelope)
            submit_failure_reason = self._infer_submit_failure_reason(result)
            if submit_failure_reason is not None:
                self._audit(
                    stage="protection_rebuild_submit_rejected",
                    symbol=exit_plan.symbol,
                    reason=submit_failure_reason,
                    payload={"position_lot_id": lot.position_lot_id, "order_ref": stop_binding.order_ref},
                )
                return False
            broker_order_id, perm_id = self._broker_identity_from_submit_result(result)
            self.execution.acknowledge_order(
                order_ref=stop_binding.order_ref,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
            )
            if self.order_lifecycle_module is not None:
                self.order_lifecycle_module.mark_accepted(
                    order_ref=stop_binding.order_ref,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            rebuilt_lot = self.execution.ledger.get_lot(lot.position_lot_id) or lot
            if self.protection_module is not None:
                self.execution.ledger.patch_lot_extras(
                    position_lot_id=lot.position_lot_id,
                    updates=self.protection_module.mark_protection_attached(
                        lot=rebuilt_lot,
                        now_ts_epoch_s=float(datetime.now().timestamp()),
                    ),
                )
            if self.order_lifecycle_module is not None and self.order_lifecycle_module.clear_freeze(symbol=exit_plan.symbol):
                self._audit(
                    stage="order_lifecycle_entry_unfreeze",
                    symbol=exit_plan.symbol,
                    reason="protection_rebuild_attached",
                    payload={"position_lot_id": lot.position_lot_id},
                )
            self._audit(
                stage="protection_rebuild_submit_succeeded",
                symbol=exit_plan.symbol,
                reason=reason,
                payload={
                    "position_lot_id": lot.position_lot_id,
                    "order_ref": stop_binding.order_ref,
                    "broker_order_id": broker_order_id,
                    "perm_id": perm_id,
                },
            )
            return True
        except Exception as exc:
            self._audit(
                stage="protection_rebuild_submit_failed",
                symbol=exit_plan.symbol,
                reason=str(exc),
                payload={"position_lot_id": lot.position_lot_id, "order_ref": stop_binding.order_ref},
            )
            return False

    def _instrument_for_signal_symbol(self, symbol: str):
        if self.config is None:
            return None
        symbol_u = str(symbol or "").strip().upper()
        for instrument in self.config.instruments:
            if instrument.signal_symbol == symbol_u:
                return instrument
        return None

    @staticmethod
    def _decision_id_from_order_ref(order_ref: str, *, symbol: str = "") -> str:
        ref = str(order_ref or "").strip()
        if not ref:
            return ""
        marker = ":TRG:"
        if marker not in ref:
            return ""
        tail = ref.split(marker, 1)[1]
        if ":LOT:" in tail:
            tail = tail.split(":LOT:", 1)[0]
        token = tail.strip()
        strategy_id = ""
        symbol_u = str(symbol or "").strip().upper()
        if symbol_u and f"_{symbol_u}_" in token:
            strategy_id, remainder = token.split(f"_{symbol_u}_", 1)
            parts = remainder.split("_", 2)
            if len(parts) == 3:
                ts_token, side_token, rest = parts
                rest = rest.replace("_lane-", ":lane-", 1).replace("_qty-", ":qty-", 1)
                return f"{strategy_id}:{symbol_u}:{ts_token}:{side_token}:{rest}".strip()
        return token

    @staticmethod
    def _broker_identity_from_submit_result(result: Any) -> tuple[str | None, int | None]:
        broker_order_id = None
        perm_id = None
        payload: dict[str, Any] = {}
        if isinstance(result, dict):
            payload = dict(result)
        else:
            metadata = dict(getattr(result, "metadata", {}) or {})
            exec_result = metadata.get("execution_result")
            if isinstance(exec_result, dict):
                payload.update(exec_result)
                nested = exec_result.get("metadata")
                if isinstance(nested, dict):
                    payload.update(nested)
        broker_order_id = str(
            payload.get("broker_order_id")
            or payload.get("order_id")
            or payload.get("ib_order_id")
            or ""
        ).strip() or None
        perm_val = payload.get("perm_id") or payload.get("ib_perm_id")
        try:
            perm_id = int(perm_val) if perm_val is not None else None
        except Exception:
            perm_id = None
        return broker_order_id, perm_id

    def _instrument_for_exec_symbol(self, symbol: str):
        if self.config is None:
            return None
        symbol_u = str(symbol or "").strip().upper()
        for instrument in self.config.instruments:
            if symbol_u in set(instrument.exec_symbols()):
                return instrument
        return None

    def _should_short_circuit_non_signal_tick(self, *, symbol: str, instrument, exec_instrument) -> bool:
        if instrument is not None:
            return False
        if exec_instrument is None:
            return True
        return not self._has_active_exec_lot(symbol)

    def _configured_session_window_allowed_sessions(self) -> set[str]:
        default_allowed = {"RTH", "PRE", "POST", "OVERNIGHT"}
        if self.config is None:
            return default_allowed
        pipeline = set(str(item or "").strip() for item in list(getattr(self.config, "gate_pipeline", ()) or ()))
        if "session_window" not in pipeline:
            return default_allowed
        gate_overrides = dict(getattr(self.config, "gate_overrides", {}) or {})
        override = gate_overrides.get("session_window")
        if not isinstance(override, dict):
            return default_allowed
        if "enabled" in override and not bool(override.get("enabled")):
            return default_allowed
        params = override.get("params")
        candidate = params if isinstance(params, dict) else override
        allowed_raw = candidate.get("allowed_sessions") if isinstance(candidate, dict) else None
        allowed = {str(item).strip().upper() for item in list(allowed_raw or []) if str(item).strip()}
        return allowed or default_allowed

    def _instrument_has_active_exec_lot(self, *, instrument) -> bool:
        exec_symbols = self._runtime_exec_symbols(
            symbol=str(getattr(instrument, "signal_symbol", "") or "").strip().upper(),
            instrument=instrument,
        )
        for exec_symbol in exec_symbols:
            if self._has_active_exec_lot(exec_symbol):
                return True
        return False

    def _should_short_circuit_signal_offsession_tick(self, *, session: str, instrument) -> bool:
        if instrument is None:
            return False
        allowed_sessions = self._configured_session_window_allowed_sessions()
        session_u = str(session or "").strip().upper()
        if not session_u:
            return False
        if session_u in allowed_sessions:
            return False
        return not self._instrument_has_active_exec_lot(instrument=instrument)

    def _has_active_exec_lot(self, exec_symbol: str) -> bool:
        if self.execution is None:
            return False
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return False
        for lot in self.execution.ledger.list_lots(
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            include_closed=False,
        ):
            if str(lot.state or "") in {"pending_entry", "live", "pending_exit"}:
                return True
        return False

    def _audit_non_signal_skip(self, *, symbol: str, ts_epoch_s: float) -> None:
        minute_key = int(ts_epoch_s // 60) if ts_epoch_s > 0 else -1
        if self._last_non_signal_skip_minute.get(symbol) == minute_key:
            return
        self._last_non_signal_skip_minute[symbol] = minute_key
        if self.config is None:
            configured_signal_symbols: list[str] = []
            configured_exec_symbols: list[str] = []
        else:
            configured_signal_symbols = [item.signal_symbol for item in self.config.instruments]
            configured_exec_symbols = sorted(
                {
                    item.long_exec_symbol
                    for item in self.config.instruments
                }
                | {
                    item.short_exec_symbol
                    for item in self.config.instruments
                }
            )
        self._audit(
            stage="non_signal_symbol_skipped",
            symbol=symbol,
            reason="non_signal_symbol",
            payload={
                "configured_signal_symbols": configured_signal_symbols,
                "configured_exec_symbols": configured_exec_symbols,
            },
        )

    def _audit_offsession_idle_short_circuit(self, *, symbol: str, ts_epoch_s: float, session: str) -> None:
        minute_key = int(ts_epoch_s // 60) if ts_epoch_s > 0 else -1
        if self._last_offsession_idle_skip_minute.get(symbol) == minute_key:
            return
        self._last_offsession_idle_skip_minute[symbol] = minute_key
        self._audit(
            stage="signal_offsession_idle_short_circuit",
            symbol=symbol,
            reason="outside_session_window",
            payload={
                "session": str(session or "").strip().upper(),
                "allowed_sessions": sorted(self._configured_session_window_allowed_sessions()),
            },
        )

    def _should_audit_feature_not_ready(self, symbol: str, payload: Dict[str, Any]) -> bool:
        signature = tuple(sorted((str(key), str(value)) for key, value in payload.items()))
        prev = self._last_feature_not_ready_signature.get(symbol)
        if prev == signature:
            return False
        self._last_feature_not_ready_signature[symbol] = signature
        return True

    def _audit_gate_decision(self, *, snapshot_symbol: str, decision) -> None:
        gate_id = str(getattr(decision, "gate_id", "") or "").strip() or "unknown_gate"
        allowed = bool(getattr(decision, "allowed", False))
        reason = str(getattr(decision, "reason", "") or "").strip()
        state_key = (snapshot_symbol, gate_id)
        prev = self._last_gate_audit_state.get(state_key)
        curr = (allowed, reason)
        if allowed and prev == curr:
            return
        self._last_gate_audit_state[state_key] = curr
        self._audit(
            stage="gate_allowed" if allowed else "gate_blocked",
            symbol=snapshot_symbol,
            reason=reason,
            payload={
                "gate_id": gate_id,
                "trader_reason_code": getattr(decision, "trader_reason_code", None),
                "payload": getattr(decision, "payload", None),
            },
        )

    @staticmethod
    def _rewrite_entry_plan_symbol(*, entry_plan, instrument):
        exec_symbol = instrument.long_exec_symbol if entry_plan.side == "LONG" else instrument.short_exec_symbol
        if entry_plan.symbol == exec_symbol:
            return entry_plan
        return entry_plan.__class__(
            symbol=exec_symbol,
            side=entry_plan.side,
            qty=entry_plan.qty,
            lane_id=getattr(entry_plan, "lane_id", None),
            ownership_scope=getattr(entry_plan, "ownership_scope", "strategy"),
            trigger_id=entry_plan.trigger_id,
            payload=dict(entry_plan.payload),
        )

    @classmethod
    def _entry_plan_from_action_plans(
        cls,
        *,
        action_plans: tuple[Any, ...],
        instrument,
        snapshot_symbol: str,
        fallback: EntryPlan | None,
    ) -> EntryPlan | None:
        plans = cls._entry_plans_from_action_plans(
            action_plans=action_plans,
            instrument=instrument,
            snapshot_symbol=snapshot_symbol,
            fallback=fallback,
        )
        return plans[0] if plans else None

    @classmethod
    def _entry_plans_from_action_plans(
        cls,
        *,
        action_plans: tuple[Any, ...],
        instrument,
        snapshot_symbol: str,
        fallback: EntryPlan | None,
    ) -> tuple[EntryPlan, ...]:
        resolved: list[EntryPlan] = []
        seen_keys: set[tuple[str, str, str, str, float]] = set()
        for action in action_plans:
            action_type = str(getattr(action, "action_type", "") or "").strip().upper()
            if action_type not in {ActionType.ENTRY, ActionType.ADD}:
                continue
            side = str(getattr(action, "side", "") or "").strip().upper()
            if side not in {"LONG", "SHORT"}:
                continue
            qty_raw = getattr(action, "qty", None)
            try:
                qty = float(qty_raw)
            except Exception:
                continue
            if qty <= 0.0:
                continue
            exec_symbol = cls._resolve_action_exec_symbol(action=action, instrument=instrument, side=side, snapshot_symbol=snapshot_symbol)
            if not exec_symbol:
                continue
            payload = dict(getattr(action, "payload", {}) or {})
            payload.setdefault("action_type", action_type)
            trigger_id = getattr(action, "trigger_id", None)
            lane_id, ownership_scope = cls._resolve_entry_lane_metadata(
                instrument=instrument,
                side=side,
                action_lane_id=getattr(action, "lane_id", None),
                action_ownership_scope=getattr(action, "ownership_scope", "strategy"),
            )
            plan = EntryPlan(
                symbol=exec_symbol,
                side=side,
                qty=qty,
                lane_id=lane_id,
                ownership_scope=ownership_scope,
                trigger_id=str(trigger_id or "") or None,
                payload=payload,
            )
            dedupe_key = (
                str(plan.symbol or "").strip().upper(),
                str(plan.side or "").strip().upper(),
                str(plan.lane_id or "").strip(),
                str(plan.trigger_id or "").strip(),
                round(float(plan.qty or 0.0), 6),
            )
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            resolved.append(plan)
        if resolved:
            return tuple(resolved)
        if fallback is None:
            return ()
        return (fallback,)

    @staticmethod
    def _resolve_entry_lane_metadata(
        *,
        instrument,
        side: str,
        action_lane_id: str | None,
        action_ownership_scope: str,
    ) -> tuple[str | None, str]:
        lane_id = str(action_lane_id or "").strip() or None
        ownership_scope = str(action_ownership_scope or "strategy").strip() or "strategy"
        if instrument is None:
            return lane_id, ownership_scope
        lane = None
        if lane_id and hasattr(instrument, "lane_by_id"):
            lane = instrument.lane_by_id(lane_id)
        if lane is None and hasattr(instrument, "lane_for_side"):
            lane = instrument.lane_for_side(side)
        if lane is None:
            return lane_id, ownership_scope
        lane_id = lane_id or (str(getattr(lane, "lane_id", "") or "").strip() or None)
        lane_scope = str(getattr(lane, "ownership_scope", "") or "").strip() or "strategy"
        if ownership_scope == "strategy" and lane_scope != "strategy":
            ownership_scope = lane_scope
        return lane_id, ownership_scope

    @staticmethod
    def _order_role_from_action_type(action_type: str) -> str | None:
        action_u = str(action_type or "").strip().upper()
        if action_u == ActionType.FLATTEN:
            return "FLATTEN"
        if action_u == ActionType.STOP:
            return "STOP"
        if action_u == ActionType.TAKE_PROFIT:
            return "TAKE_PROFIT"
        if action_u == ActionType.RETREAT:
            return "RETREAT"
        if action_u == ActionType.REDUCE:
            return "REDUCE"
        return None

    @staticmethod
    def _resolve_action_exec_symbol(*, action, instrument, side: str, snapshot_symbol: str) -> str:
        action_symbol = str(getattr(action, "symbol", "") or "").strip().upper()
        lane_id = str(getattr(action, "lane_id", "") or "").strip()
        if instrument is None:
            return action_symbol or str(snapshot_symbol or "").strip().upper()
        if lane_id:
            for lane in tuple(getattr(instrument, "execution_lanes", ()) or ()):
                if str(getattr(lane, "lane_id", "") or "").strip() == lane_id and str(getattr(lane, "symbol", "") or "").strip():
                    return str(lane.symbol).strip().upper()
        signal_symbol = str(getattr(instrument, "signal_symbol", "") or "").strip().upper()
        exec_symbols = set(instrument.exec_symbols()) if hasattr(instrument, "exec_symbols") else {
            str(getattr(instrument, "long_exec_symbol", "") or "").strip().upper(),
            str(getattr(instrument, "short_exec_symbol", "") or "").strip().upper(),
        }
        if action_symbol and action_symbol != signal_symbol:
            return action_symbol
        lane = instrument.lane_for_side(side) if hasattr(instrument, "lane_for_side") else None
        if lane is not None and str(getattr(lane, "symbol", "") or "").strip():
            return str(lane.symbol).strip().upper()
        if side == "LONG":
            candidate = str(getattr(instrument, "long_exec_symbol", "") or "").strip().upper()
            if candidate:
                return candidate
        if side == "SHORT":
            candidate = str(getattr(instrument, "short_exec_symbol", "") or "").strip().upper()
            if candidate:
                return candidate
        for candidate in exec_symbols:
            if candidate:
                return candidate
        return action_symbol or signal_symbol

    def _active_binding_lots(self, *, instrument) -> list[Any]:
        if self.execution is None:
            return []
        active_states = {"pending_entry", "live", "pending_exit"}
        active: list[Any] = []
        for exec_symbol in instrument.exec_symbols():
            lots = self.execution.ledger.list_lots(
                strategy_id=self.strategy_id,
                symbol=exec_symbol,
                include_closed=False,
            )
            for lot in lots:
                if str(lot.state or "") not in active_states:
                    continue
                active.append(lot)
        return active

    def _account_binding_lots(self, *, instrument) -> list[Any]:
        if self.execution is None:
            return []
        active_states = {"pending_entry", "live", "pending_exit"}
        active: list[Any] = []
        seen: set[str] = set()
        for exec_symbol in instrument.exec_symbols():
            lots = self.execution.ledger.list_lots(
                strategy_id=None,
                symbol=exec_symbol,
                include_closed=False,
            )
            for lot in lots:
                if str(lot.state or "") not in active_states:
                    continue
                lot_id = str(getattr(lot, "position_lot_id", "") or "")
                if lot_id in seen:
                    continue
                seen.add(lot_id)
                active.append(lot)
        return active

    def _portfolio_position_views(self, *, instrument) -> list[Any]:
        if self.execution is None:
            return []
        out: list[Any] = []
        for lot in self._account_binding_lots(instrument=instrument):
            qty = float(getattr(lot, "live_qty", 0.0) or 0.0)
            avg_px = float(getattr(lot, "avg_entry_price", 0.0) or 0.0)
            signed_qty = qty if str(getattr(lot, "side", "") or "").strip().upper() == "LONG" else -qty
            out.append(
                type(
                    "PortfolioPositionView",
                    (),
                    {
                        "symbol": lot.symbol,
                        "qty": signed_qty,
                        "price": avg_px,
                    },
                )()
            )
        return out

    def _execution_lane_authorities(self, *, signal_symbol: str, exec_symbol: str, lane_id: str | None) -> Dict[str, str]:
        instrument = self._instrument_for_signal_symbol(str(signal_symbol or "").strip().upper())
        if instrument is None:
            instrument = self._instrument_for_exec_symbol(str(exec_symbol or "").strip().upper())
        lane = None
        lane_id_v = str(lane_id or "").strip() or None
        exec_symbol_u = str(exec_symbol or "").strip().upper()
        if instrument is not None:
            if lane_id_v:
                lane = instrument.lane_by_id(lane_id_v)
            if lane is None:
                for item in tuple(instrument.lanes("execution") or ()):
                    if str(getattr(item, "symbol", "") or "").strip().upper() == exec_symbol_u:
                        lane = item
                        break
        return {
            "flatten_authority": str(getattr(lane, "flatten_authority", "") or "strategy").strip() or "strategy",
            "protection_authority": str(getattr(lane, "protection_authority", "") or "strategy").strip() or "strategy",
            "adopt_authority": str(getattr(lane, "adopt_authority", "") or "strategy").strip() or "strategy",
        }

    async def _reap_stale_pending_entries(self, *, instrument, now_ts_epoch_s: float) -> None:
        if self.execution is None:
            return
        timeout_sec = self._entry_pending_timeout_sec()
        for lot in self._active_binding_lots(instrument=instrument):
            should_quarantine, pending_detail = (
                self.execution_semantics_module.pending_entry_quarantine_candidate(
                    lot=lot,
                    now_ts_epoch_s=now_ts_epoch_s,
                    timeout_sec=timeout_sec,
                )
                if self.execution_semantics_module is not None
                else (False, {})
            )
            if not should_quarantine:
                continue
            isolation = await self._broker_account_isolation(symbol=lot.symbol)
            if isolation.get("blocked"):
                continue
            cancel_summary = await self._cancel_order_refs_best_effort(
                order_refs={str(getattr(lot, "entry_order_ref", "") or "").strip()},
                symbol=lot.symbol,
                force_refresh=False,
            )
            self.execution.ledger.quarantine_lot(
                position_lot_id=lot.position_lot_id,
                reason="stale_pending_entry_timeout",
            )
            self._arm_entry_timeout_cooldown(exec_symbol=lot.symbol, now_ts_epoch_s=now_ts_epoch_s)
            self._audit(
                stage="stale_pending_entry_quarantined",
                symbol=lot.symbol,
                reason="stale_pending_entry_timeout",
                payload={
                    "position_lot_id": lot.position_lot_id,
                    "age_sec": float(pending_detail.get("age_sec") or 0.0),
                    "timeout_sec": float(pending_detail.get("timeout_sec") or timeout_sec),
                    "entry_order_ref": lot.entry_order_ref,
                    "cancelled_count": int(cancel_summary.get("cancelled_count") or 0),
                    "cancel_scanned_count": int(cancel_summary.get("scanned_count") or 0),
                    "entry_timeout_reentry_cooldown_sec": float(self._entry_timeout_reentry_cooldown_sec()),
                },
            )

    def _entry_pending_timeout_sec(self) -> float:
        if self.execution_semantics_module is None:
            ttl_ms = int(getattr(self.params, "ttl_ms", 30_000) or 30_000)
            return max(15.0, float(ttl_ms) / 1000.0)
        return self.execution_semantics_module.entry_pending_timeout_sec()

    def _infer_submit_failure_reason(self, result: Any) -> str | None:
        if self.execution_semantics_module is None:
            return None
        return self.execution_semantics_module.infer_submit_failure_reason(result)

    def _should_activate_kill_switch(self, reason: str | None) -> bool:
        if self.execution_semantics_module is None:
            return False
        return self.execution_semantics_module.should_activate_kill_switch(reason)

    @staticmethod
    def _normalize_kill_switch_reason(reason: str | None) -> str:
        reason_v = str(reason or "").strip()
        if reason_v.startswith("broker_binding_"):
            return "broker_" + reason_v[len("broker_binding_") :]
        return reason_v

    def _protection_enabled(self) -> bool:
        if self.config is None:
            return False
        profile = dict(getattr(self.config, "execution_profile", {}) or {})
        return bool(profile.get("protection_enabled", False))

    def _execution_session_extras(self, *, snapshot) -> Dict[str, Any]:
        if self.session_module is None:
            return {}
        return self.session_module.execution_session_extras(snapshot=snapshot)

    def _protective_execution_session_extras(self, *, snapshot) -> Dict[str, Any]:
        if self.session_module is None:
            return {}
        return self.session_module.protective_execution_session_extras(snapshot=snapshot)

    def _build_protective_oco_payload(self, *, signal_symbol: str, exec_symbol: str, lane_id: str | None = None, snapshot=None) -> Dict[str, Any]:
        if self.protection_module is None:
            return {}
        return self.protection_module.build_entry_protection_payload(
            signal_symbol=signal_symbol,
            exec_symbol=exec_symbol,
            lane_id=lane_id,
            snapshot=snapshot,
        )

    def _protective_offhours_stop_defaults(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.protection_module is None:
            return {}
        return self.protection_module.protective_offhours_stop_defaults(payload)

    def _entry_rearm_sec(self) -> float:
        if self.config is None:
            return 0.0
        try:
            return max(0.0, float(self.config.raw_extras.get("entry_rearm_sec") or 15.0))
        except Exception:
            return 15.0

    def _entry_timeout_reentry_cooldown_sec(self) -> float:
        if self.config is None:
            return 75.0
        try:
            return max(0.0, float(self.config.raw_extras.get("entry_timeout_reentry_cooldown_sec") or 75.0))
        except Exception:
            return 75.0

    def _arm_entry_rearm_cooldown(self, *, exec_symbol: str, ts: datetime | None = None) -> None:
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return
        rearm_sec = self._entry_rearm_sec()
        if rearm_sec <= 0.0:
            return
        now_ts = (ts or datetime.now(ET)).timestamp()
        self._entry_rearm_block_until[symbol_u] = float(now_ts + rearm_sec)

    def _arm_entry_timeout_cooldown(self, *, exec_symbol: str, now_ts_epoch_s: float) -> None:
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return
        cooldown_sec = self._entry_timeout_reentry_cooldown_sec()
        if cooldown_sec <= 0.0:
            return
        base_ts = float(now_ts_epoch_s or 0.0)
        if base_ts <= 0.0:
            base_ts = float(datetime.now(ET).timestamp())
        current_until = float(self._entry_rearm_block_until.get(symbol_u) or 0.0)
        self._entry_rearm_block_until[symbol_u] = max(current_until, base_ts + cooldown_sec)

    async def _broker_account_isolation(self, *, symbol: str) -> Dict[str, Any]:
        if self.recovery_module is None:
            return {"blocked": False, "reason": "recovery_module_unavailable", "broker_qty": 0.0, "open_order_count": 0, "open_order_refs": []}
        return await self.recovery_module.broker_account_isolation(symbol=symbol)

    async def _binding_broker_account_isolation(
        self,
        *,
        signal_symbol: str | None = None,
        exec_symbol: str | None = None,
    ) -> Dict[str, Any]:
        if self.recovery_module is None:
            return {"blocked": False, "reason": "recovery_module_unavailable", "broker_qty": 0.0, "open_order_count": 0, "open_order_refs": [], "blocked_symbols": []}
        return await self.recovery_module.binding_broker_account_isolation(
            signal_symbol=signal_symbol,
            exec_symbol=exec_symbol,
        )

    def _resolve_broker_adapter(self):
        if self.recovery_module is None:
            return None
        return self.recovery_module.resolve_broker_adapter()

    async def _maybe_recover_startup_block(self) -> bool:
        if self.recovery_module is None:
            return False
        return await self.recovery_module.maybe_recover_startup_block()

    def _activate_symbol_kill_switch(self, *, symbol: str, reason: str) -> None:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return
        if self._symbol_kill_switch.get(symbol_u) == reason:
            return
        self._symbol_kill_switch[symbol_u] = reason
        stocks_kernel_symbol_kill_switch_total.labels(
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            reason=reason,
        ).inc()
        self._audit(
            stage="symbol_kill_switch_activated",
            symbol=symbol_u,
            reason=reason,
            payload={"blocked_symbol": symbol_u},
        )

    @staticmethod
    def _is_recoverable_symbol_kill_switch_reason(reason: str | None) -> bool:
        reason_v = str(reason or "").strip()
        if not reason_v:
            return False
        return (
            reason_v.startswith("broker_")
            or reason_v.startswith("entry_isolation_blocked")
            or reason_v in {
                "ack_not_converged_timeout",
                "pending_not_converged_timeout",
                "flatten_not_converged_timeout",
                "live_position_missing_protection",
                "unsafe_position_without_protection",
                "reservation_failed",
            }
        )

    async def _maybe_release_symbol_kill_switch(
        self,
        *,
        signal_symbol: str,
        exec_symbol: str,
        reason: str | None = None,
    ) -> bool:
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return False
        previous_reason = str(reason or self._symbol_kill_switch.get(symbol_u) or "").strip()
        if not self._is_recoverable_symbol_kill_switch_reason(previous_reason):
            return False
        isolation = await self._binding_broker_account_isolation(
            signal_symbol=signal_symbol,
            exec_symbol=symbol_u,
        )
        if isolation.get("blocked"):
            release_allowed = self._broker_truth_mismatch_release_allowed_from_readiness(
                signal_symbol=signal_symbol,
                exec_symbol=symbol_u,
                previous_reason=previous_reason,
                isolation=dict(isolation),
            )
            if not release_allowed:
                return False
        removed_reason = self._symbol_kill_switch.pop(symbol_u, None)
        if removed_reason is None:
            return False
        self._audit(
            stage="symbol_kill_switch_released",
            symbol=symbol_u,
            reason="ready",
            payload={
                "previous_reason": str(removed_reason),
                "signal_symbol": str(signal_symbol or "").strip().upper(),
                "exec_symbol": symbol_u,
            },
        )
        return True

    def _broker_truth_mismatch_release_allowed_from_readiness(
        self,
        *,
        signal_symbol: str | None,
        exec_symbol: str,
        previous_reason: str,
        isolation: Dict[str, Any],
    ) -> bool:
        if str(previous_reason or "").strip() != "broker_truth_mismatch":
            return False
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return False
        reason = str(isolation.get("reason") or "").strip()
        if reason not in {"broker_live_position_present", "broker_binding_live_position_present"}:
            return False

        broker_truth = self._read_runtime_json("runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json")
        if str(broker_truth.get("go_no_go") or "").strip().lower() != "go":
            return False
        if broker_truth.get("blockers") or broker_truth.get("active_quarantines"):
            return False
        if int(broker_truth.get("projection_divergence_count") or 0) > 0:
            return False
        if int(broker_truth.get("dirty_lot_position_count") or 0) > 0:
            return False

        position_safety = self._read_runtime_json("runtime/readiness/position_safety/position_safety_latest.json")
        if str(position_safety.get("go_no_go") or "").strip().lower() != "go":
            return False
        if int(position_safety.get("active_manual_lock_count") or 0) > 0:
            return False
        if int(position_safety.get("active_quarantine_count") or 0) > 0:
            return False

        strategy_id_v = str(self.strategy_id or "").strip()
        signal_symbol_u = str(signal_symbol or "").strip().upper()
        records = list(position_safety.get("records") or [])
        for record in records:
            if str(record.get("symbol") or "").strip().upper() != symbol_u:
                continue
            candidate_ids = {
                str(item or "").strip()
                for item in list(record.get("candidate_strategy_ids") or [])
                if str(item or "").strip()
            }
            if candidate_ids and strategy_id_v not in candidate_ids:
                continue
            if bool(record.get("protected_or_flattening")) is not True:
                continue
            self._audit(
                stage="symbol_kill_switch_release_allowed_by_readiness",
                symbol=symbol_u,
                reason="broker_truth_ready_protected_position",
                payload={
                    "previous_reason": previous_reason,
                    "signal_symbol": signal_symbol_u,
                    "exec_symbol": symbol_u,
                    "isolation_reason": reason,
                    "broker_truth_generated_at": str(broker_truth.get("generated_at") or ""),
                    "position_safety_generated_at": str(position_safety.get("generated_at") or ""),
                },
            )
            return True
        return False

    def _kill_switch_release_allowed_by_flat_readiness(
        self,
        *,
        exec_symbol: str,
        previous_reason: str,
    ) -> bool:
        reason = str(previous_reason or "").strip()
        if reason not in {
            "unsafe_position_without_protection",
            "live_position_missing_protection",
            "flatten_not_converged_timeout",
        }:
            return False
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return False
        position_safety = self._read_runtime_json("runtime/readiness/position_safety/position_safety_latest.json")
        if str(position_safety.get("go_no_go") or "").strip().lower() != "go":
            return False
        if int(position_safety.get("active_manual_lock_count") or 0) > 0:
            return False
        if int(position_safety.get("active_quarantine_count") or 0) > 0:
            return False
        for record in list(position_safety.get("records") or []):
            if str(record.get("symbol") or "").strip().upper() == symbol_u:
                candidate_ids = {
                    str(item or "").strip()
                    for key in ("candidate_strategy_ids", "contract_strategy_ids")
                    for item in list(record.get(key) or [])
                    if str(item or "").strip()
                }
                strategy_id_v = str(self.strategy_id or "").strip()
                if candidate_ids and strategy_id_v not in candidate_ids:
                    return False
                if bool(record.get("protected_or_flattening")) is True:
                    self._audit(
                        stage="symbol_kill_switch_release_allowed_by_readiness",
                        symbol=symbol_u,
                        reason="broker_and_position_safety_protected",
                        payload={
                            "previous_reason": reason,
                            "position_safety_generated_at": str(position_safety.get("generated_at") or ""),
                            "contract_strategy_ids": sorted(candidate_ids),
                        },
                    )
                    return True
                return False

        live_truth = self._read_runtime_json("runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json")
        if str(live_truth.get("go_no_go") or "").strip().lower() != "go":
            return False
        for position in list(live_truth.get("positions") or []):
            if str(position.get("symbol") or "").strip().upper() == symbol_u and abs(float(position.get("qty") or 0.0)) > 1e-6:
                if not self._broker_truth_snapshot_confirms_flat_after_live_snapshot(
                    exec_symbol=symbol_u,
                    live_truth=live_truth,
                ):
                    return False
                break
        for order in list(live_truth.get("open_orders") or []):
            if str(order.get("symbol") or "").strip().upper() == symbol_u:
                if not self._broker_truth_snapshot_confirms_flat_after_live_snapshot(
                    exec_symbol=symbol_u,
                    live_truth=live_truth,
                ):
                    return False
                break
        self._audit(
            stage="symbol_kill_switch_release_allowed_by_readiness",
            symbol=symbol_u,
            reason="broker_and_position_safety_flat",
            payload={
                "previous_reason": reason,
                "position_safety_generated_at": str(position_safety.get("generated_at") or ""),
                "broker_truth_generated_at": str(live_truth.get("generated_at") or ""),
            },
        )
        return True

    def _broker_truth_snapshot_confirms_flat_after_live_snapshot(
        self,
        *,
        exec_symbol: str,
        live_truth: Dict[str, Any],
    ) -> bool:
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return False
        broker_truth = self._read_runtime_json("runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json")
        if str(broker_truth.get("go_no_go") or "").strip().lower() != "go":
            return False
        if broker_truth.get("blockers") or broker_truth.get("active_quarantines"):
            return False
        for key in (
            "live_broker_open_order_count",
            "live_broker_position_count",
            "open_core_order_count",
            "nonzero_core_position_count",
            "nonzero_ledger_position_count",
            "projection_divergence_count",
            "dirty_lot_position_count",
            "order_truth_active_quarantine_count",
        ):
            if int(broker_truth.get(key) or 0) > 0:
                return False
        broker_ts = self._artifact_generated_at_epoch(broker_truth)
        live_ts = self._artifact_generated_at_epoch(live_truth)
        if broker_ts <= 0.0 or live_ts <= 0.0 or broker_ts < live_ts:
            return False
        self._audit(
            stage="symbol_kill_switch_release_allowed_by_broker_truth_summary",
            symbol=symbol_u,
            reason="newer_broker_truth_flat",
            payload={
                "broker_truth_generated_at": str(broker_truth.get("generated_at") or ""),
                "live_truth_generated_at": str(live_truth.get("generated_at") or ""),
                "previous_live_open_orders": len(list(live_truth.get("open_orders") or [])),
                "previous_live_positions": len(list(live_truth.get("positions") or [])),
            },
        )
        return True

    @staticmethod
    def _artifact_generated_at_epoch(payload: Dict[str, Any]) -> float:
        raw = str((payload or {}).get("generated_at") or "").strip()
        if not raw:
            return 0.0
        try:
            return float(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0.0

    @staticmethod
    def _read_runtime_json(path: str) -> Dict[str, Any]:
        try:
            raw = Path(path).read_text()
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    async def _run_startup_preflight(self) -> Dict[str, Any]:
        if self.recovery_module is None:
            return {"ok": True, "reason": "recovery_module_unavailable", "broker_checks": [], "db_check": {"ok": True, "reason": "skipped", "positions": [], "orders": []}, "runtime_clean": True}
        return await self.recovery_module.run_startup_preflight()

    async def _db_startup_isolation(self, *, exec_symbols: list[str]) -> Dict[str, Any]:
        if self.recovery_module is None:
            return {"ok": True, "reason": "recovery_module_unavailable", "positions": [], "orders": []}
        return await self.recovery_module.db_startup_isolation(exec_symbols=exec_symbols)

    def _resolve_pg(self):
        if self.recovery_module is None:
            return None
        return self.recovery_module.resolve_pg()

    def _clear_startup_blocked_gauge(self) -> None:
        stocks_kernel_startup_blocked_gauge.labels(strategy_id=self.strategy_id).set(0)

    def _audit(self, *, stage: str, symbol: str, reason: str, payload: Dict[str, Any]) -> None:
        symbol_u = str(symbol).strip().upper()
        if symbol_u:
            history = self._recent_strategy_stages.setdefault(symbol_u, [])
            history.append(str(stage or "").strip())
            if len(history) > 200:
                del history[:-200]
        if self.audit_writer is None:
            return
        self.audit_writer.write_event(
            domain="strategy",
            now=datetime.now(),
            event=KernelExecutionEvent(
                stage=stage,
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                domain="strategy",
                component="strategy_runtime",
                strategy_run_id=getattr(self.ctx, "strategy_run_id", None),
                reason=reason,
                payload=dict(payload),
            ),
        )

    def _audit_authority_decision(
        self,
        *,
        stage: str,
        symbol: str,
        decision,
        lot,
        extra_payload: Dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "position_lot_id": getattr(lot, "position_lot_id", None),
            "lot_strategy_id": getattr(lot, "strategy_id", None),
            "lane_id": getattr(lot, "lane_id", None),
            "symbol": getattr(lot, "symbol", None),
            **dict(getattr(decision, "payload", {}) or {}),
            **dict(extra_payload or {}),
        }
        self._audit(
            stage=stage,
            symbol=str(symbol or "").strip().upper(),
            reason=str(getattr(decision, "reason", "") or "authority_denied"),
            payload=payload,
        )

    def _check_adopt_reconcile_authority(
        self,
        *,
        lot: Any,
        symbol: str,
        blocked_stage: str,
        granted_stage: str,
        extra_payload: Dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        if self.ownership_policy is None:
            return True, None
        decision = self.ownership_policy.can_adopt_reconcile(
            strategy_id=self.strategy_id,
            lot=lot,
            requested_lane_id=str(getattr(lot, "lane_id", "") or "").strip() or None,
        )
        self._audit_authority_decision(
            stage=(granted_stage if decision.allowed else blocked_stage),
            symbol=str(symbol or ""),
            decision=decision,
            lot=lot,
            extra_payload=dict(extra_payload or {}),
        )
        if decision.allowed:
            return True, None
        return False, str(getattr(decision, "reason", "") or "adopt_authority_denied")

    def _on_state_transition_event(self, event) -> None:
        self._audit(
            stage=f"{event.state_machine}_transition",
            symbol=str(event.symbol or "SYSTEM"),
            reason=str(event.reason or event.after_state),
            payload=event.to_dict(),
        )

    async def on_order_update(self, update: OrderUpdate) -> None:
        if self.config is None or self.execution is None:
            return
        order_ref = str(getattr(update, "order_ref", "") or "").strip()
        if not order_ref_belongs_to_strategy(order_ref, namespace=self.config.order_namespace, strategy_id=self.strategy_id):
            return
        symbol = str(getattr(update, "symbol", "") or "").strip().upper()
        broker_order_id = str(getattr(update, "broker_order_id", "") or "").strip() or None
        perm_id_raw = getattr(update, "ib_perm_id", None)
        try:
            perm_id = int(perm_id_raw) if perm_id_raw is not None else None
        except Exception:
            perm_id = None
        state = str(getattr(update, "state", "") or "").strip().upper()
        try:
            binding = self.execution.acknowledge_order(
                order_ref=order_ref,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
            )
        except Exception:
            binding = self.execution.bindings.resolve(order_ref=order_ref, broker_order_id=broker_order_id, perm_id=perm_id)
        self._audit(
            stage="order_update_received",
            symbol=symbol,
            reason=state or "order_update",
            payload={
                "order_ref": order_ref,
                "broker_order_id": broker_order_id,
                "perm_id": perm_id,
                "order_type": getattr(update, "order_type", None),
                "position_lot_id": getattr(binding, "position_lot_id", None),
            },
        )
        if binding is None:
            return
        if self.order_lifecycle_module is not None:
            if state in {"SUBMITTED", "NEW", "PRESUBMITTED"}:
                self.order_lifecycle_module.mark_submitted(
                    order_ref=order_ref,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            elif state in {"ACK", "ACCEPTED", "WORKING", "PARTIALLY_FILLED", "PARTIAL"}:
                self.order_lifecycle_module.mark_accepted(
                    order_ref=order_ref,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
        if state in {"REJECTED", "CANCELLED", "CANCELED", "FAILED", "EXPIRED", "INACTIVE", "FILLED"}:
            if self.order_lifecycle_module is not None:
                self.order_lifecycle_module.mark_terminal(
                    order_ref=order_ref,
                    state=state,
                    now_ts_epoch_s=float(datetime.now().timestamp()),
                )
            lot = self.execution.ledger.get_lot(binding.position_lot_id)
            should_quarantine = state not in {"FILLED"}
            if should_quarantine and lot is not None and float(lot.live_qty or 0.0) <= 0.0:
                if str(binding.order_role or "").strip().upper() == "ENTRY":
                    self._arm_entry_rearm_cooldown(exec_symbol=symbol)
                self.execution.ledger.quarantine_lot(position_lot_id=binding.position_lot_id, reason=f"terminal_order_state:{state.lower()}")
                self._audit(
                    stage="lot_quarantined_from_order_update",
                    symbol=symbol,
                    reason=state.lower(),
                    payload={"order_ref": order_ref, "position_lot_id": binding.position_lot_id},
                )

    async def on_fill_received(
        self,
        fill: KernelFillReport,
        *,
        trace_id: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        if self.execution is None:
            return
        prior_lot = None
        lifecycle_order_ref = str(fill.order_ref or "").strip()
        if fill.order_ref:
            binding = self.execution.bindings.resolve(
                order_ref=str(fill.order_ref or "").strip() or None,
                broker_order_id=str(fill.broker_order_id or "").strip() or None,
                perm_id=fill.perm_id,
            )
            if binding is not None and getattr(binding, "position_lot_id", None):
                prior_lot = self.execution.ledger.get_lot(binding.position_lot_id)
                lifecycle_order_ref = str(getattr(binding, "order_ref", "") or "").strip() or lifecycle_order_ref
        if self.order_lifecycle_module is not None and lifecycle_order_ref:
            self.order_lifecycle_module.mark_fill(
                order_ref=lifecycle_order_ref,
                now_ts_epoch_s=float(datetime.now().timestamp()),
            )
        apply_result = self.execution.apply_fill(fill)
        try:
            await self._call_stock_plugin_hook("on_fill", fill=fill, apply_result=apply_result)
        except Exception as exc:
            self._audit(
                stage="stock_plugin_fill_failed",
                symbol=str(fill.symbol or "").strip().upper(),
                reason=str(exc),
                payload={"plugin_component": self._stock_plugin_component},
            )
        if self.audit_writer is not None:
            self.audit_writer.write_event(
                domain="exec",
                now=datetime.now(),
                event=build_fill_received_event(
                    strategy_id=self.strategy_id,
                    strategy_run_id=getattr(self.ctx, "strategy_run_id", None),
                    symbol=fill.symbol,
                    trace_id=str(trace_id or fill.exec_id or fill.order_ref or ""),
                    order_ref=fill.order_ref,
                    broker_order_id=fill.broker_order_id,
                    perm_id=fill.perm_id,
                    qty=float(fill.qty or 0.0),
                    price=fill.price,
                    exec_id=fill.exec_id,
                ),
            )
        if apply_result.position_lot_id:
            lot = self.execution.ledger.get_lot(apply_result.position_lot_id)
            self._record_fill_trade_stats(
                fill=fill,
                apply_result=apply_result,
                prior_lot=prior_lot,
                current_lot=lot,
            )
            if lot is not None and self.protection_module is not None:
                lifecycle_updates = {}
                if str(apply_result.order_role or "").strip().upper() == "ENTRY":
                    lifecycle_updates = self.protection_module.on_entry_fill(
                        lot=lot,
                        fill_price=fill.price,
                        now_ts_epoch_s=float(datetime.now().timestamp()),
                    )
                elif str(apply_result.order_role or "").strip().upper() in {"STOP", "FLATTEN"}:
                    lifecycle_updates = self.protection_module.on_exit_fill(
                        lot=lot,
                        fill_qty=float(fill.qty or 0.0),
                        fill_price=fill.price,
                        now_ts_epoch_s=float(datetime.now().timestamp()),
                    )
                if lifecycle_updates:
                    self.execution.ledger.patch_lot_extras(
                        position_lot_id=lot.position_lot_id,
                        updates=lifecycle_updates,
                    )
                    lot = self.execution.ledger.get_lot(lot.position_lot_id)
            if lot is not None and self.policy is not None:
                feature_context = self.kernel_state.get(lot.symbol, {}).get("feature_context")
                feedback = self.policy.on_fill_feedback(
                    position_lot_id=lot.position_lot_id,
                    order_role=str(apply_result.order_role or ""),
                    filled_qty=float(fill.qty or 0.0),
                    fill_price=fill.price,
                    feature_context=feature_context,
                )
                if feedback:
                    self.execution.ledger.patch_lot_extras(
                        position_lot_id=lot.position_lot_id,
                        updates={"last_fill_feedback": feedback},
                    )
        self._audit(
            stage="fill_bridge_applied",
            symbol=str(fill.symbol or "").strip().upper(),
            reason=apply_result.reason,
            payload={
                "position_lot_id": apply_result.position_lot_id,
                "order_role": apply_result.order_role,
                "action": apply_result.action,
                "trace_id": trace_id,
                "decision_id": decision_id,
            },
        )
        if self.execution_quality_module is not None:
            lot = self.execution.ledger.get_lot(apply_result.position_lot_id) if apply_result.position_lot_id else None
            quality_payload = self.execution_quality_module.register_fill(
                order_ref=fill.order_ref,
                symbol=str(fill.symbol or "").strip().upper(),
                order_role=str(apply_result.order_role or ""),
                side=str(getattr(lot, "side", "") or ""),
                fill_price=fill.price,
                fill_qty=float(fill.qty or 0.0),
                now_ts_epoch_s=float(datetime.now().timestamp()),
            )
            if quality_payload:
                self._audit(
                    stage="execution_quality_fill",
                    symbol=str(fill.symbol or "").strip().upper(),
                    reason="fill",
                    payload=quality_payload,
                )

    def _record_fill_trade_stats(self, *, fill: KernelFillReport, apply_result, prior_lot, current_lot) -> None:
        if prior_lot is None:
            return
        order_role = str(getattr(apply_result, "order_role", "") or "").strip().upper()
        if order_role not in {"STOP", "FLATTEN", "TAKE_PROFIT", "RETREAT", "REDUCE"}:
            return
        if fill.price is None or prior_lot.avg_entry_price is None:
            return
        fill_qty = float(fill.qty or 0.0)
        if fill_qty <= 0.0:
            return
        side = str(getattr(prior_lot, "side", "") or "").strip().upper()
        direction = 1.0 if side == "LONG" else -1.0 if side == "SHORT" else 0.0
        if direction == 0.0:
            return
        realized = (float(fill.price) - float(prior_lot.avg_entry_price)) * fill_qty * direction
        ts_epoch_s = float(datetime.now(ET).timestamp())
        day_key = self._day_key(ts_epoch_s=ts_epoch_s)
        self._day_realized_pnl_by_day[day_key] = float(self._day_realized_pnl_by_day.get(day_key, 0.0) or 0.0) + realized
        if current_lot is not None and float(getattr(prior_lot, "live_qty", 0.0) or 0.0) > 0.0 and float(getattr(current_lot, "live_qty", 0.0) or 0.0) <= 0.0:
            self._completed_round_trips_by_day.setdefault(day_key, set()).add(str(getattr(current_lot, "position_lot_id", "") or ""))
        self._audit(
            stage="trade_day_stats_updated",
            symbol=str(fill.symbol or "").strip().upper(),
            reason=order_role.lower(),
            payload={
                "position_lot_id": getattr(prior_lot, "position_lot_id", None),
                "realized_delta": realized,
                "day_realized_pnl": float(self._day_realized_pnl_by_day.get(day_key, 0.0) or 0.0),
                "success_count": len(self._completed_round_trips_by_day.get(day_key, set())),
            },
        )

    async def on_db_persist_result(
        self,
        *,
        order_ref: str,
        symbol: str,
        success: bool,
        trace_id: str | None = None,
        decision_id: str | None = None,
        broker_order_id: str | None = None,
        perm_id: int | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> None:
        if self.execution is None or self.audit_writer is None:
            return
        binding = self.execution.bindings.resolve(
            order_ref=str(order_ref or "").strip() or None,
            broker_order_id=str(broker_order_id or "").strip() or None,
            perm_id=perm_id,
        )
        if binding is not None:
            self.execution.ledger.patch_lot_extras(
                position_lot_id=binding.position_lot_id,
                updates={
                    "db_persist_success": bool(success),
                    "db_persist_payload": dict(payload or {}),
                    "db_persisted_order_ref": str(order_ref or "").strip() or None,
                },
            )
        self.audit_writer.write_event(
            domain="exec",
            now=datetime.now(),
            event=build_db_persist_event(
                strategy_id=self.strategy_id,
                strategy_run_id=getattr(self.ctx, "strategy_run_id", None),
                symbol=symbol,
                trace_id=str(trace_id or order_ref or ""),
                decision_id=decision_id,
                order_ref=str(order_ref or "").strip() or None,
                success=bool(success),
                payload={
                    **dict(payload or {}),
                    "position_lot_id": getattr(binding, "position_lot_id", None),
                    "order_role": getattr(binding, "order_role", None),
                },
            ),
        )
        if not bool(success) and self.order_lifecycle_module is not None:
            symbol_u = str(symbol or "").strip().upper()
            reason = "db_persist_failed"
            raw_payload = dict(payload or {})
            detail_reason = str(raw_payload.get("reason") or raw_payload.get("error") or "").strip()
            if detail_reason:
                reason = f"db_persist_failed:{detail_reason[:64]}"
            self.order_lifecycle_module.mark_sync_issue(symbol=symbol_u, reason=reason)
            self._audit(
                stage="order_lifecycle_entry_freeze",
                symbol=symbol_u,
                reason=reason,
                payload={
                    "order_ref": str(order_ref or "").strip() or None,
                    "trace_id": trace_id,
                    "decision_id": decision_id,
                },
            )

    async def on_position_update(self, position: PositionSnapshot) -> None:
        if self.config is None or self.execution is None:
            return
        symbol = str(getattr(position, "symbol", "") or "").strip().upper()
        if not symbol:
            return
        pos_qty = float(getattr(position, "pos_qty", 0.0) or 0.0)
        avg_px = getattr(position, "avg_px", None)
        active_lots = self.execution.ledger.list_lots(strategy_id=self.strategy_id, symbol=symbol, include_closed=False)
        reconciled_lot_id = None
        reconcile_blocked_reason = None
        if len(active_lots) == 1:
            allow_reconcile, reconcile_blocked_reason = self._check_adopt_reconcile_authority(
                lot=active_lots[0],
                symbol=symbol,
                blocked_stage="position_reconcile_blocked_authority",
                granted_stage="position_reconcile_authority_granted",
            )
            if allow_reconcile:
                reconciled = self.execution.ledger.reconcile_broker_qty(
                    position_lot_id=active_lots[0].position_lot_id,
                    live_qty=abs(pos_qty),
                    avg_entry_price=float(avg_px) if avg_px is not None else None,
                )
                reconciled_lot_id = reconciled.position_lot_id
        self._audit(
            stage="position_update_received",
            symbol=symbol,
            reason="position_snapshot",
            payload={
                "pos_qty": pos_qty,
                "avg_px": avg_px,
                "realized_pnl_usd": getattr(position, "realized_pnl_usd", None),
                "unrealized_pnl_usd": getattr(position, "unrealized_pnl_usd", None),
                "reconciled_position_lot_id": reconciled_lot_id,
                "reconcile_blocked_reason": reconcile_blocked_reason,
                "open_lot_count": len(active_lots),
            },
        )
        if abs(pos_qty) <= 1e-9 and self.flatten_completion_verifier is not None:
            self.flatten_completion_verifier.clear_symbol(symbol=symbol)
        if self.order_lifecycle_module is not None and abs(pos_qty) <= 1e-9 and len(active_lots) == 0:
            if self.order_lifecycle_module.clear_freeze(symbol=symbol):
                self._audit(
                    stage="order_lifecycle_entry_unfreeze",
                    symbol=symbol,
                    reason="position_flat",
                    payload={},
                )
