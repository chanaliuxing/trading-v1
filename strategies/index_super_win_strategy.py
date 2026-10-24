from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.strategy import OrderUpdate, StrategyAlgorithm, StrategyParams, make_decision_id

from services.strategy_runtime_v2 import (
    BrokerSnapshotService,
    CallbackStateTransitionSink,
    CoordinatorConfig,
    CrossStateSnapshot,
    RuntimeCoordinator,
    RuntimeLifecycle,
    RuntimeState,
    get_shared_ownership_service,
    get_shared_runtime_store,
    validate_cross_state_consistency,
)
from services.strategy_runtime_v2.coordinator import TradeIntent
from strategies.index_super_win_components import (
    FeatureDrivenSignalModel,
    InstrumentMap,
    SignalDecision,
)
from strategies.index_super_win_manifest import ReplicaManifest

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


class _IBPositionSource:
    def __init__(self, strategy: "IndexSuperWinStrategy", symbols: list[str]) -> None:
        self._strategy = strategy
        self._symbols = list(symbols)

    async def positions(self):
        if not await self._strategy._refresh_ib_positions_best_effort(force=False):
            return []
        ib = self._strategy._resolve_ib()
        if ib is None or not hasattr(ib, "positions"):
            return []
        try:
            rows = ib.positions()
        except Exception:
            return []
        account = self._strategy._resolve_ib_account()
        filtered = []
        for row in list(rows or []):
            try:
                contract = getattr(row, "contract", None)
                symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
                if self._symbols and symbol not in self._symbols:
                    continue
                row_account = str(getattr(row, "account", "") or "").strip()
                if account and row_account and row_account != account:
                    continue
                filtered.append(row)
            except Exception:
                continue
        return filtered


class _IBOpenOrderSource:
    def __init__(self, strategy: "IndexSuperWinStrategy", symbols: list[str]) -> None:
        self._strategy = strategy
        self._symbols = list(symbols)

    async def open_orders(self):
        ib = self._strategy._resolve_ib()
        if ib is None or not hasattr(ib, "openTrades"):
            return []
        account = self._strategy._resolve_ib_account()
        try:
            trades = list(ib.openTrades() or [])
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for trade in trades:
            try:
                contract = getattr(trade, "contract", None)
                symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
                if not symbol:
                    continue
                if self._symbols and symbol not in self._symbols:
                    continue
                order = getattr(trade, "order", None)
                status = getattr(trade, "orderStatus", None)
                row_account = str(getattr(order, "account", "") or "").strip()
                if account and row_account and row_account != account:
                    continue
                broker_order_id = str(getattr(order, "orderId", "") or "").strip()
                if not broker_order_id:
                    continue
                out.append(
                    {
                        "broker_order_id": broker_order_id,
                        "symbol": symbol,
                        "side": str(getattr(order, "action", "") or "").strip().upper() or "UNKNOWN",
                        "qty": float(getattr(order, "totalQuantity", 0.0) or 0.0),
                        "order_type": str(getattr(order, "orderType", "") or "").strip().upper() or "UNKNOWN",
                        "state": str(getattr(status, "status", "") or "").strip().upper() or "UNKNOWN",
                        "order_ref": str(getattr(order, "orderRef", "") or "").strip(),
                        "account": row_account or None,
                    }
                )
            except Exception:
                continue
        return out


@dataclass(frozen=True)
class SignalObservation:
    signal_symbol: str
    exec_symbol: str
    signal_active: bool
    target_qty: float
    side: Optional[str]
    reason: str
    signal_price: Optional[float] = None
    exec_price: Optional[float] = None


def _safe_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


class IndexSuperWinStrategy(StrategyAlgorithm):
    """
    New modular IndexSuperWin strategy.

    Safety design:
    - does not auto-claim broker inventory
    - unknown broker positions/open-orders go to quarantine
    - preview does not persist pending/live state
    """

    def __init__(self, ctx, params: StrategyParams):
        super().__init__(ctx, params)
        self._runtime_store = None
        self._ownership_service = None
        self._broker_snapshots = BrokerSnapshotService()
        self._coordinator: Optional[RuntimeCoordinator] = None
        self._symbols: list[str] = []
        self._exec_symbols: list[str] = []
        self._feature_bar_sec: int = 5
        self._dry_run: bool = True
        self._last_observation_by_symbol: dict[str, SignalObservation] = {}
        self._last_preview_by_symbol: dict[str, dict[str, Any]] = {}
        self._instrument_map = InstrumentMap([])
        self._signal_model = FeatureDrivenSignalModel()
        self._broker_positions_ready: bool = False
        self._last_broker_positions_refresh_mono: float = 0.0
        self._last_broker_positions_refresh_warn_mono: float = 0.0
        self._diag_enabled: bool = True
        self._diag_log_path: Optional[Path] = None
        self._diag_snapshot_path: Optional[Path] = None
        self._diag_flush_interval_sec: float = 5.0
        self._diag_last_flush_mono: float = 0.0
        self._diag_day_et: str = ""
        self._diag_counters: dict[str, int] = {}
        self._diag_reason_counters: dict[str, int] = {}
        self._diag_symbol_counters: dict[str, dict[str, int]] = {}
        self._diag_symbol_reason_counters: dict[str, dict[str, int]] = {}
        self._startup_recovery_by_symbol: dict[str, dict[str, Any]] = {}
        self._runtime_health_by_symbol: dict[str, dict[str, Any]] = {}
        self._startup_consistency_by_symbol: dict[str, dict[str, Any]] = {}
        self._entry_enabled_by_symbol: dict[str, bool] = {}
        self._local_audit_enabled: bool = True
        self._audit_context_by_symbol: dict[str, dict[str, Any]] = {}
        self._market_audit_enabled: bool = True
        self._market_audit_gap_sec: float = 30.0
        self._market_audit_by_symbol: dict[str, dict[str, Any]] = {}
        self._chain_report_by_symbol: dict[str, dict[str, Any]] = {}
        self._protective_orders_by_symbol: dict[str, list[str]] = {}
        self._ownership_conflicts_by_symbol: dict[str, dict[str, Any]] = {}
        self._replica_manifest: Optional[ReplicaManifest] = None

    async def initialize(self) -> None:
        extras = dict(self.params.extras or {})
        self._diag_enabled = bool(extras.get("enable_observability_file", True))
        diag_log_raw = str(
            extras.get("diag_log_path", f"runtime/state/index_super_win_diag_{self.strategy_id}.jsonl")
            or f"runtime/state/index_super_win_diag_{self.strategy_id}.jsonl"
        ).strip()
        diag_snapshot_raw = str(
            extras.get("diag_snapshot_path", f"runtime/state/index_super_win_diag_snapshot_{self.strategy_id}.json")
            or f"runtime/state/index_super_win_diag_snapshot_{self.strategy_id}.json"
        ).strip()
        self._diag_log_path = Path(diag_log_raw) if diag_log_raw else None
        self._diag_snapshot_path = Path(diag_snapshot_raw) if diag_snapshot_raw else None
        self._diag_flush_interval_sec = max(1.0, float(extras.get("diag_flush_interval_sec", 5.0) or 5.0))
        self._local_audit_enabled = bool(extras.get("enable_local_audit_bridge", True))
        self._market_audit_enabled = bool(extras.get("enable_market_audit_bridge", True))
        self._market_audit_gap_sec = max(1.0, float(extras.get("market_audit_gap_sec", 30.0) or 30.0))
        self._diag_day_et = datetime.now().astimezone().strftime("%Y-%m-%d")
        self._diag_event(
            node="initialize",
            status="start",
            reason="strategy_boot",
            extras={
                "strategy_id": self.strategy_id,
                "dry_run": bool(extras.get("dry_run", True)),
            },
        )
        scope = str(extras.get("runtime_scope", "index_super_win") or "index_super_win").strip()
        self._runtime_store = get_shared_runtime_store(scope)
        self._ownership_service = get_shared_ownership_service(scope)

        self._replica_manifest = ReplicaManifest.from_params(extras)
        self._instrument_map = InstrumentMap.from_params(extras)
        self._symbols = self._instrument_map.signal_symbols()
        self._exec_symbols = self._instrument_map.exec_symbols()
        self._feature_bar_sec = int(extras.get("feature_bar_sec", 5) or 5)
        self._dry_run = bool(extras.get("dry_run", True))
        self._signal_model = FeatureDrivenSignalModel(
            default_notional_usd=float(extras.get("notional_usd", 0.0) or 0.0)
        )
        self._broker_snapshots = BrokerSnapshotService(
            position_source=_IBPositionSource(self, self._exec_symbols),
            open_order_source=_IBOpenOrderSource(self, self._exec_symbols),
        )
        self._coordinator = RuntimeCoordinator(
            runtime_store=self._runtime_store,
            ownership_service=self._ownership_service,
            config=CoordinatorConfig(
                dry_run=self._dry_run,
                ttl_ms=int(getattr(self.params, "ttl_ms", 2000) or 2000),
                dlb_ms=int(extras.get("dlb_ms", 500) or 500),
                order_namespace=str(extras.get("order_namespace", "iswv2") or "iswv2"),
            ),
            transition_sink=CallbackStateTransitionSink(self._on_state_transition_event),
        )
        if self._symbols:
            await self.ctx.subscribe_features(self._symbols, bar_sec=self._feature_bar_sec)
        await self.ctx.set_cadence(int(getattr(self.params, "cadence_s", 5) or 5))
        self._emit_local_audit(
            domain="system",
            stage="strategy_started",
            status="started",
            payload={
                "dry_run": self._dry_run,
                "symbols": list(self._symbols),
                "exec_symbols": list(self._exec_symbols),
                "runtime_scope": scope,
            },
        )

        self.state["phase"] = "active"
        self.state["symbols"] = list(self._symbols)
        self.state["exec_symbols"] = list(self._exec_symbols)
        self.state["replica_manifest"] = self._replica_manifest.as_dict() if self._replica_manifest is not None else None
        self.state["dry_run"] = self._dry_run
        self.state["runtime_scope"] = scope
        self.state["broker_reconcile_on_init"] = bool(extras.get("broker_reconcile_on_init", True))
        self.state["pre_entry_broker_reconcile"] = bool(extras.get("pre_entry_broker_reconcile", True))
        self.state["pre_entry_open_orders_reconcile"] = bool(extras.get("pre_entry_open_orders_reconcile", True))
        self.state["diag_enabled"] = self._diag_enabled
        self.state["local_audit_bridge_enabled"] = self._local_audit_enabled
        self.state["market_audit_bridge_enabled"] = self._market_audit_enabled
        self.state["diag_log_path"] = str(self._diag_log_path) if self._diag_log_path else None
        self.state["diag_snapshot_path"] = str(self._diag_snapshot_path) if self._diag_snapshot_path else None
        self.state["startup_recovery_by_symbol"] = {}
        self.state["runtime_health_by_symbol"] = {}
        self.state["startup_consistency_by_symbol"] = {}
        self.state["entry_enabled_by_symbol"] = {}
        self.state["protective_orders_by_symbol"] = {}

        if bool(extras.get("broker_reconcile_on_init", True)):
            await self._reconcile_broker_positions(reason="initialize")
            await self._reconcile_broker_open_orders(reason="initialize")
        if bool(extras.get("startup_runtime_recovery", True)):
            await self._run_startup_runtime_recovery()
        if bool(extras.get("startup_consistency_check", True)):
            await self._run_startup_consistency_checks()

        logger.info(
            "index_super_win_initialized",
            strategy_id=self.strategy_id,
            symbols=self._symbols,
            exec_symbols=self._exec_symbols,
            dry_run=self._dry_run,
            feature_bar_sec=self._feature_bar_sec,
            runtime_scope=scope,
        )
        self._diag_event(
            node="initialize",
            status="ready",
            reason="strategy_ready",
            extras={
                "symbols": list(self._symbols),
                "exec_symbols": list(self._exec_symbols),
                "runtime_scope": scope,
            },
        )
        self._emit_local_audit(
            domain="system",
            stage="strategy_ready",
            status="ready",
            payload={
                "exec_symbols": list(self._exec_symbols),
                "entry_enabled_by_symbol": dict(self._entry_enabled_by_symbol),
            },
        )

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        symbol_u = str(symbol or "").strip().upper()
        self._diag_inc("features.total", symbol=symbol_u)
        self._diag_event(
            node="features",
            status="received",
            symbol=symbol_u,
            reason="feature_bar_received",
            extras={"feature_keys": sorted(list(features.keys()))[:20]},
        )
        binding = self._instrument_map.binding_for_signal(symbol_u)
        if binding is None:
            logger.debug(
                "index_super_win_feature_ignored_unbound_symbol",
                strategy_id=self.strategy_id,
                symbol=symbol_u,
            )
            self._diag_event(
                node="entry_gate",
                status="blocked",
                symbol=symbol_u,
                reason="unbound_symbol",
            )
            self._emit_gate_blocked_audit(symbol=symbol_u, reason="unbound_symbol")
            return
        obs = self._extract_signal_observation(binding, features)
        self._last_observation_by_symbol[symbol_u] = obs
        self._record_market_feature_summary(symbol_u, binding=binding, features=features, obs=obs)
        self._diag_event(
            node="signal",
            status="observed",
            symbol=symbol_u,
            reason=obs.reason,
            extras={
                "signal_active": bool(obs.signal_active),
                "side": obs.side,
                "target_qty": float(obs.target_qty),
                "exec_symbol": obs.exec_symbol,
                "signal_price": obs.signal_price,
                "exec_price": obs.exec_price,
            },
        )
        if not obs.signal_active or obs.side is None or obs.target_qty <= 0:
            block_reason = "signal_inactive" if not obs.signal_active else ("missing_side" if obs.side is None else "non_positive_qty")
            self._diag_event(
                node="entry_gate",
                status="blocked",
                symbol=symbol_u,
                reason=block_reason,
                extras={
                    "signal_active": bool(obs.signal_active),
                    "side": obs.side,
                    "target_qty": float(obs.target_qty),
                },
            )
            self._emit_gate_blocked_audit(
                symbol=symbol_u,
                reason=block_reason,
                payload={
                    "exec_symbol": obs.exec_symbol,
                    "signal_active": bool(obs.signal_active),
                    "side": obs.side,
                    "target_qty": float(obs.target_qty),
                },
            )
            return
        assert self._coordinator is not None
        self._diag_inc("entry.attempts", symbol=symbol_u)
        if not self._entry_enabled_by_symbol.get(obs.exec_symbol, True):
            startup_check = dict(self._startup_consistency_by_symbol.get(obs.exec_symbol) or {})
            block_reason = str(startup_check.get("block_reason") or "startup_snapshot_not_ready").strip()
            self._last_preview_by_symbol[symbol_u] = {
                "status": "STARTUP_NOT_READY",
                "runtime_lifecycle": "flat",
                "target_qty": float(obs.target_qty),
                "exec_symbol": obs.exec_symbol,
                "block_reason": block_reason,
            }
            self._diag_event(
                node="entry_gate",
                status="blocked",
                symbol=symbol_u,
                reason=block_reason,
                extras={
                    "exec_symbol": obs.exec_symbol,
                    "startup_consistency": startup_check,
                },
            )
            self._emit_gate_blocked_audit(
                symbol=symbol_u,
                reason=block_reason,
                payload={"exec_symbol": obs.exec_symbol, "startup_consistency": startup_check},
            )
            return
        runtime_block = await self._pre_entry_runtime_block(obs.exec_symbol)
        if runtime_block is not None:
            self._last_preview_by_symbol[symbol_u] = {
                "status": "RUNTIME_GUARD_BLOCKED",
                "trigger_id": runtime_block.active_trigger_id,
                "order_ref": runtime_block.entry_order_ref,
                "runtime_lifecycle": runtime_block.lifecycle.value,
                "target_qty": runtime_block.target_qty,
                "exec_symbol": obs.exec_symbol,
                "block_reason": runtime_block.last_error or runtime_block.quarantine_reason or runtime_block.lifecycle.value,
            }
            self._diag_event(
                node="entry_gate",
                status="blocked",
                symbol=symbol_u,
                reason=str(runtime_block.last_error or runtime_block.quarantine_reason or runtime_block.lifecycle.value),
                extras={
                    "exec_symbol": obs.exec_symbol,
                    "runtime_lifecycle": runtime_block.lifecycle.value,
                    "order_ref": runtime_block.entry_order_ref,
                },
            )
            self._remember_audit_context(
                symbol=symbol_u,
                trigger_id=runtime_block.active_trigger_id,
                order_ref=runtime_block.entry_order_ref,
            )
            self._emit_local_audit(
                stage="runtime_guard_blocked",
                severity="warning",
                status="blocked",
                reason=str(runtime_block.last_error or runtime_block.quarantine_reason or runtime_block.lifecycle.value),
                symbol=symbol_u,
                trigger_id=runtime_block.active_trigger_id,
                trace_id=str((self._audit_context_by_symbol.get(symbol_u) or {}).get("trace_id") or runtime_block.active_trigger_id or ""),
                payload={
                    "exec_symbol": obs.exec_symbol,
                    "runtime_lifecycle": runtime_block.lifecycle.value,
                    "order_ref": runtime_block.entry_order_ref,
                },
            )
            return
        if self.state.get("pre_entry_broker_reconcile"):
            broker_decision = await self._reconcile_broker_symbol(obs.exec_symbol, reason="pre_entry")
            if broker_decision is not None:
                self._last_preview_by_symbol[symbol_u] = {
                    "status": broker_decision.status,
                    "trigger_id": broker_decision.runtime_state.active_trigger_id,
                    "order_ref": broker_decision.runtime_state.entry_order_ref,
                    "runtime_lifecycle": broker_decision.runtime_state.lifecycle.value,
                    "target_qty": broker_decision.runtime_state.target_qty,
                    "exec_symbol": obs.exec_symbol,
                }
                logger.warning(
                    "index_super_win_entry_blocked_by_broker_truth",
                    strategy_id=self.strategy_id,
                    symbol=symbol_u,
                    exec_symbol=obs.exec_symbol,
                    status=broker_decision.status,
                    reasons=list(broker_decision.reasons),
                )
                self._diag_event(
                    node="entry_gate",
                    status="blocked",
                    symbol=symbol_u,
                    reason=f"broker:{broker_decision.status}",
                    extras={"reasons": list(broker_decision.reasons), "exec_symbol": obs.exec_symbol},
                )
                self._remember_audit_context(
                    symbol=symbol_u,
                    trigger_id=broker_decision.runtime_state.active_trigger_id,
                    order_ref=broker_decision.runtime_state.entry_order_ref,
                )
                self._emit_gate_blocked_audit(
                    symbol=symbol_u,
                    reason=f"broker:{broker_decision.status}",
                    trigger_id=broker_decision.runtime_state.active_trigger_id,
                    order_ref=broker_decision.runtime_state.entry_order_ref,
                    payload={"reasons": list(broker_decision.reasons), "exec_symbol": obs.exec_symbol},
                )
                return
        if self.state.get("pre_entry_open_orders_reconcile"):
            order_decision = await self._reconcile_broker_open_orders_for_symbol(obs.exec_symbol, reason="pre_entry")
            if order_decision is not None:
                self._last_preview_by_symbol[symbol_u] = {
                    "status": order_decision.status,
                    "trigger_id": order_decision.runtime_state.active_trigger_id,
                    "order_ref": order_decision.runtime_state.entry_order_ref,
                    "runtime_lifecycle": order_decision.runtime_state.lifecycle.value,
                    "target_qty": order_decision.runtime_state.target_qty,
                    "exec_symbol": obs.exec_symbol,
                }
                logger.warning(
                    "index_super_win_entry_blocked_by_open_orders",
                    strategy_id=self.strategy_id,
                    symbol=symbol_u,
                    exec_symbol=obs.exec_symbol,
                    status=order_decision.status,
                    reasons=list(order_decision.reasons),
                )
                self._diag_event(
                    node="entry_gate",
                    status="blocked",
                    symbol=symbol_u,
                    reason=f"open_orders:{order_decision.status}",
                    extras={"reasons": list(order_decision.reasons), "exec_symbol": obs.exec_symbol},
                )
                self._remember_audit_context(
                    symbol=symbol_u,
                    trigger_id=order_decision.runtime_state.active_trigger_id,
                    order_ref=order_decision.runtime_state.entry_order_ref,
                )
                self._emit_gate_blocked_audit(
                    symbol=symbol_u,
                    reason=f"open_orders:{order_decision.status}",
                    trigger_id=order_decision.runtime_state.active_trigger_id,
                    order_ref=order_decision.runtime_state.entry_order_ref,
                    payload={"reasons": list(order_decision.reasons), "exec_symbol": obs.exec_symbol},
                )
                return

        decision = await self._coordinator.preview_entry(
            TradeIntent(
                strategy_id=self.strategy_id,
                symbol=obs.exec_symbol,
                target_qty=float(obs.target_qty),
                side=obs.side,
                reason=obs.reason,
                signal_price=obs.exec_price,
                metadata={
                    "dry_run": self._dry_run,
                    "signal_symbol": obs.signal_symbol,
                    "exec_symbol": obs.exec_symbol,
                    "signal_price": obs.signal_price,
                    "exec_price": obs.exec_price,
                },
            )
        )
        self._diag_event(
            node="preview",
            status=decision.status,
            symbol=symbol_u,
            reason=obs.reason,
            extras={
                "trigger_id": decision.metadata.get("trigger_id"),
                "order_ref": decision.metadata.get("order_ref"),
                "exec_symbol": obs.exec_symbol,
                "target_qty": float(obs.target_qty),
            },
        )
        self._remember_audit_context(
            symbol=symbol_u,
            trigger_id=str(decision.metadata.get("trigger_id") or ""),
            order_ref=str(decision.metadata.get("order_ref") or ""),
        )
        self._emit_local_audit(
            stage="preview_evaluated",
            status=str(decision.status or "").lower() or None,
            reason=obs.reason,
            symbol=symbol_u,
            trigger_id=str(decision.metadata.get("trigger_id") or ""),
            trace_id=str((self._audit_context_by_symbol.get(symbol_u) or {}).get("trace_id") or decision.metadata.get("trigger_id") or ""),
            payload={
                "exec_symbol": obs.exec_symbol,
                "target_qty": float(obs.target_qty),
                "runtime_lifecycle": decision.runtime_state.lifecycle.value,
                "order_ref": decision.metadata.get("order_ref"),
            },
        )
        self._last_preview_by_symbol[symbol_u] = {
            "status": decision.status,
            "trigger_id": decision.metadata.get("trigger_id"),
            "order_ref": decision.metadata.get("order_ref"),
            "runtime_lifecycle": decision.runtime_state.lifecycle.value,
            "target_qty": decision.runtime_state.target_qty,
            "exec_symbol": obs.exec_symbol,
        }
        if decision.status in {"OWNERSHIP_CONFLICT", "QUARANTINED"}:
            owner_strategy_id = self._extract_owner_strategy_id(decision.reasons)
            if decision.status == "OWNERSHIP_CONFLICT":
                self._ownership_conflicts_by_symbol[symbol_u] = {
                    "owner_strategy_id": owner_strategy_id,
                    "reasons": list(decision.reasons),
                    "exec_symbol": obs.exec_symbol,
                }
                self._diag_inc(
                    "conflict.foreign_symbol_ownership",
                    symbol=symbol_u,
                    reason="pre_entry:foreign_symbol_ownership_already_claimed",
                )
                self._diag_event(
                    node="ownership",
                    status="blocked",
                    symbol=symbol_u,
                    reason="foreign_symbol_ownership_already_claimed",
                    extras={"owner_strategy_id": owner_strategy_id, "exec_symbol": obs.exec_symbol},
                )
            logger.warning(
                "index_super_win_preview_blocked",
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                exec_symbol=obs.exec_symbol,
                status=decision.status,
                reasons=list(decision.reasons),
                order_ref=decision.metadata.get("order_ref"),
            )
            self._diag_event(
                node="entry_gate",
                status="blocked",
                symbol=symbol_u,
                reason=(
                    "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed"
                    if decision.status == "OWNERSHIP_CONFLICT"
                    else f"preview:{decision.status}"
                ),
                extras={"reasons": list(decision.reasons), "owner_strategy_id": owner_strategy_id},
            )
            self._emit_local_audit(
                stage="preview_blocked",
                severity="warning",
                status=str(decision.status or "").lower() or "blocked",
                reason=(
                    "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed"
                    if decision.status == "OWNERSHIP_CONFLICT"
                    else f"preview:{decision.status}"
                ),
                symbol=symbol_u,
                trigger_id=str(decision.metadata.get("trigger_id") or ""),
                trace_id=str((self._audit_context_by_symbol.get(symbol_u) or {}).get("trace_id") or decision.metadata.get("trigger_id") or ""),
                payload={"reasons": list(decision.reasons), "owner_strategy_id": owner_strategy_id, "order_ref": decision.metadata.get("order_ref")},
            )
            return
        logger.info(
            "index_super_win_signal_preview",
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            exec_symbol=obs.exec_symbol,
            status=decision.status,
            side=obs.side,
            target_qty=round(float(obs.target_qty), 6),
            reason=obs.reason,
            order_ref=decision.metadata.get("order_ref"),
        )
        if decision.envelope is None:
            self._diag_event(
                node="submit",
                status="skipped",
                symbol=symbol_u,
                reason="dry_run_or_no_envelope",
                extras={"order_ref": decision.metadata.get("order_ref")},
            )
        if decision.envelope is not None:
            audit_ctx = self._inject_trace_fields(
                decision.envelope,
                symbol=symbol_u,
                trigger_id=str(decision.metadata.get("trigger_id") or ""),
            )
            self._diag_event(
                node="submit",
                status="start",
                symbol=symbol_u,
                reason=obs.reason,
                extras={
                    "order_ref": decision.metadata.get("order_ref"),
                    "decision_id": audit_ctx.get("decision_id"),
                },
            )
            self._emit_local_audit(
                stage="entry_intent_requested",
                status="requested",
                reason=obs.reason,
                symbol=symbol_u,
                decision_id=audit_ctx.get("decision_id"),
                trigger_id=audit_ctx.get("trigger_id"),
                trace_id=audit_ctx.get("trace_id"),
                payload={
                    "exec_symbol": obs.exec_symbol,
                    "target_qty": float(obs.target_qty),
                    "side": obs.side,
                    "order_ref": decision.metadata.get("order_ref"),
                },
            )
            try:
                submit_result = await self.ctx.submit_intent(decision.envelope)
            except Exception as exc:
                self._last_preview_by_symbol[symbol_u]["status"] = "SUBMIT_EXCEPTION"
                self._last_preview_by_symbol[symbol_u]["submit_error"] = str(exc)
                logger.error(
                    "index_super_win_submit_error",
                    strategy_id=self.strategy_id,
                    symbol=symbol_u,
                    exec_symbol=obs.exec_symbol,
                    order_ref=decision.metadata.get("order_ref"),
                    error=str(exc),
                )
                self._diag_event(
                    node="submit",
                    status="exception",
                    symbol=symbol_u,
                    reason="submit_intent_exception",
                    extras={"error": str(exc), "order_ref": decision.metadata.get("order_ref")},
                )
                self._emit_local_audit(
                    stage="entry_intent_submit_failed",
                    severity="warning",
                    status="failed",
                    reason="submit_intent_exception",
                    symbol=symbol_u,
                    decision_id=audit_ctx.get("decision_id"),
                    trigger_id=audit_ctx.get("trigger_id"),
                    trace_id=audit_ctx.get("trace_id"),
                    payload={"error": str(exc), "order_ref": decision.metadata.get("order_ref")},
                )
                return
            status, reasons = self._extract_submit_status(submit_result)
            self._last_preview_by_symbol[symbol_u]["submit_status"] = status or "UNKNOWN"
            self._last_preview_by_symbol[symbol_u]["submit_reasons"] = list(reasons)
            self._diag_event(
                node="submit",
                status=status or "UNKNOWN",
                symbol=symbol_u,
                reason="pipeline_submit_result",
                extras={"reasons": list(reasons), "order_ref": decision.metadata.get("order_ref")},
            )
            if status == "APPROVED":
                submitted = await self._coordinator.mark_entry_submitted(
                    TradeIntent(
                        strategy_id=self.strategy_id,
                        symbol=obs.exec_symbol,
                        target_qty=float(obs.target_qty),
                        side=obs.side,
                        reason=obs.reason,
                        signal_price=obs.exec_price,
                        metadata={
                            "dry_run": self._dry_run,
                            "signal_symbol": obs.signal_symbol,
                            "exec_symbol": obs.exec_symbol,
                            "signal_price": obs.signal_price,
                            "exec_price": obs.exec_price,
                        },
                    ),
                    trigger_id=str(decision.metadata.get("trigger_id") or ""),
                    order_ref=str(decision.metadata.get("order_ref") or ""),
                )
                self._last_preview_by_symbol[symbol_u]["status"] = submitted.status
                self._last_preview_by_symbol[symbol_u]["runtime_lifecycle"] = submitted.runtime_state.lifecycle.value
                self._diag_event(
                    node="runtime",
                    status=submitted.status,
                    symbol=symbol_u,
                    reason="intent_submitted",
                    extras={
                        "runtime_lifecycle": submitted.runtime_state.lifecycle.value,
                        "order_ref": decision.metadata.get("order_ref"),
                    },
                )
                self._emit_local_audit(
                    stage="entry_intent_submitted",
                    status="submitted",
                    reason="intent_submitted",
                    symbol=symbol_u,
                    decision_id=audit_ctx.get("decision_id"),
                    trigger_id=audit_ctx.get("trigger_id"),
                    trace_id=audit_ctx.get("trace_id"),
                    payload={
                        "submit_status": status,
                        "runtime_lifecycle": submitted.runtime_state.lifecycle.value,
                        "order_ref": decision.metadata.get("order_ref"),
                    },
                )
                logger.info(
                    "index_super_win_submit_approved",
                    strategy_id=self.strategy_id,
                    symbol=symbol_u,
                    exec_symbol=obs.exec_symbol,
                    order_ref=decision.metadata.get("order_ref"),
                    submit_status=status,
                )
                return
            self._last_preview_by_symbol[symbol_u]["status"] = f"SUBMIT_{status or 'UNKNOWN'}"
            self._diag_event(
                node="entry_gate",
                status="blocked",
                symbol=symbol_u,
                reason=f"submit:{status or 'UNKNOWN'}",
                extras={"reasons": list(reasons), "order_ref": decision.metadata.get("order_ref")},
            )
            self._emit_local_audit(
                stage="entry_intent_rejected",
                severity="warning",
                status=str(status or "unknown").lower(),
                reason=f"submit:{status or 'UNKNOWN'}",
                symbol=symbol_u,
                decision_id=audit_ctx.get("decision_id"),
                trigger_id=audit_ctx.get("trigger_id"),
                trace_id=audit_ctx.get("trace_id"),
                payload={"reasons": list(reasons), "order_ref": decision.metadata.get("order_ref")},
            )
            logger.warning(
                "index_super_win_submit_not_approved",
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                exec_symbol=obs.exec_symbol,
                order_ref=decision.metadata.get("order_ref"),
                submit_status=status or "UNKNOWN",
                reasons=list(reasons),
            )

    async def on_shutdown(self, reason: str) -> None:
        self.state["shutdown_reason"] = reason
        self._diag_event(
            node="shutdown",
            status="start",
            reason=reason,
            extras={"observed_symbols": sorted(self._last_observation_by_symbol.keys())},
        )
        self._emit_local_audit(
            domain="system",
            stage="strategy_shutdown_started",
            status="stopping",
            reason=reason,
            payload={"observed_symbols": sorted(self._last_observation_by_symbol.keys())},
        )
        self._flush_all_market_feature_summaries()
        self._diag_flush_snapshot(force=True)
        logger.info(
            "index_super_win_shutdown",
            strategy_id=self.strategy_id,
            reason=reason,
            observed_symbols=sorted(self._last_observation_by_symbol.keys()),
        )

    async def on_order_update(self, update: OrderUpdate) -> None:
        if self._runtime_store is None or self._coordinator is None:
            return
        symbol_u = str(getattr(update, "symbol", "") or "").strip().upper()
        if not symbol_u:
            return
        order_ref = str(getattr(update, "order_ref", "") or "").strip()
        state_norm = self._normalize_order_update_state(getattr(update, "state", ""))
        current = await self._runtime_store.get_state(self.strategy_id, symbol_u)
        trusted, trust_reason = self._is_trusted_order_update(update=update, symbol=symbol_u, current=current)
        if not trusted:
            if str(getattr(update, "strategy_id", "") or "").strip() == self.strategy_id:
                self._diag_event(
                    node="order_update",
                    status="ignored",
                    symbol=symbol_u,
                    reason="untrusted_order_update",
                    extras={
                        "state": state_norm or str(getattr(update, "state", "") or ""),
                        "order_ref": order_ref or None,
                        "broker_order_id": str(getattr(update, "broker_order_id", "") or "").strip() or None,
                    },
                )
            return

        trigger_id = self._extract_trigger_id_from_order_ref(order_ref) or getattr(current, "active_trigger_id", None)
        audit_ctx = self._ensure_audit_context(
            symbol=symbol_u,
            trigger_id=trigger_id,
            order_ref=order_ref or getattr(current, "entry_order_ref", None),
        )
        lifecycle_before = current.lifecycle.value if current is not None else "missing"
        action = "noop"
        action_reason = trust_reason
        runtime_after = lifecycle_before

        self._diag_event(
            node="order_update",
            status=state_norm or "UNKNOWN",
            symbol=symbol_u,
            reason=trust_reason,
            extras={
                "order_ref": order_ref or None,
                "broker_order_id": str(getattr(update, "broker_order_id", "") or "").strip() or None,
                "runtime_lifecycle_before": lifecycle_before,
            },
        )

        if state_norm in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "APIPENDING"}:
            next_state = await self._mark_runtime_pending_from_order_update(
                symbol=symbol_u,
                order_ref=order_ref or getattr(current, "entry_order_ref", None),
                trigger_id=trigger_id,
                current=current,
            )
            action = "mark_pending_entry"
            action_reason = f"order_update:{state_norm.lower()}"
            runtime_after = next_state.lifecycle.value
        elif state_norm in {"PARTIAL", "FILLED"}:
            decision = await self._reconcile_broker_symbol(symbol_u, reason=f"order_update_{state_norm.lower()}")
            if decision is not None:
                action = decision.status.lower()
                action_reason = "|".join(decision.reasons) if decision.reasons else f"order_update:{state_norm.lower()}"
                runtime_after = decision.runtime_state.lifecycle.value
            else:
                next_state = await self._mark_runtime_pending_from_order_update(
                    symbol=symbol_u,
                    order_ref=order_ref or getattr(current, "entry_order_ref", None),
                    trigger_id=trigger_id,
                    current=current,
                )
                action = "awaiting_broker_position"
                action_reason = f"order_update:{state_norm.lower()}:broker_position_not_visible"
                runtime_after = next_state.lifecycle.value
        elif state_norm in {"CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED"}:
            decision = await self._resolve_terminal_order_update(symbol=symbol_u, state_norm=state_norm)
            action = decision.status.lower()
            action_reason = "|".join(decision.reasons) if decision.reasons else f"order_update:{state_norm.lower()}"
            runtime_after = decision.runtime_state.lifecycle.value

        self._emit_local_audit(
            domain="exec",
            stage="order_state_updated",
            status=state_norm.lower() if state_norm else None,
            reason=action_reason,
            symbol=symbol_u,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload={
                "order_ref": order_ref or None,
                "broker_order_id": str(getattr(update, "broker_order_id", "") or "").strip() or None,
                "state": state_norm or str(getattr(update, "state", "") or ""),
                "runtime_lifecycle_before": lifecycle_before,
                "runtime_lifecycle_after": runtime_after,
                "action": action,
                "trust_reason": trust_reason,
            },
        )

    async def record_protective_order_refs(
        self,
        symbol: str,
        order_refs: list[str] | tuple[str, ...],
        *,
        source: str = "manual_record",
    ) -> list[str]:
        symbol_u = str(symbol or "").strip().upper()
        refs = [str(ref).strip() for ref in list(order_refs or []) if str(ref).strip()]
        self._protective_orders_by_symbol[symbol_u] = list(refs)
        self.state["protective_orders_by_symbol"] = dict(self._protective_orders_by_symbol)
        if self._coordinator is not None and symbol_u:
            await self._coordinator.update_protective_order_refs(
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                order_refs=refs,
            )
        self._diag_event(
            node="protective_orders",
            status="recorded",
            symbol=symbol_u,
            reason=source,
            extras={"count": len(refs), "order_refs": list(refs[:10])},
        )
        return list(refs)

    def _owned_order_prefix(self, symbol: str) -> str:
        assert self._coordinator is not None
        symbol_u = str(symbol or "").strip().upper()
        return f"{self._coordinator.config.order_namespace}:{self.strategy_id}:{symbol_u}:"

    @staticmethod
    def _normalize_order_update_state(state: Any) -> str:
        return str(state or "").strip().replace(" ", "").replace("_", "").upper()

    @staticmethod
    def _extract_trigger_id_from_order_ref(order_ref: Optional[str]) -> Optional[str]:
        ref = str(order_ref or "").strip()
        marker = ":TRG:"
        idx = ref.upper().find(marker)
        if idx < 0:
            return None
        tail = ref[idx + len(marker):]
        trigger_id = str(tail.split(":", 1)[0] or "").strip()
        return trigger_id or None

    def _is_trusted_order_update(
        self,
        *,
        update: OrderUpdate,
        symbol: str,
        current: Optional[RuntimeState],
    ) -> tuple[bool, str]:
        symbol_u = str(symbol or "").strip().upper()
        order_ref = str(getattr(update, "order_ref", "") or "").strip()
        strategy_match = str(getattr(update, "strategy_id", "") or "").strip() == self.strategy_id
        if not order_ref:
            return False, "missing_order_ref"
        if not order_ref.startswith(self._owned_order_prefix(symbol_u)):
            return False, "foreign_order_ref"
        if ":ENTRY:" not in order_ref.upper():
            return False, "non_entry_order_ref"
        if current is not None and current.entry_order_ref and order_ref == current.entry_order_ref:
            return True, "owned_entry_order_ref"
        if strategy_match:
            return True, "owned_strategy_order_ref"
        return True, "owned_entry_order_ref"

    async def _mark_runtime_pending_from_order_update(
        self,
        *,
        symbol: str,
        order_ref: Optional[str],
        trigger_id: Optional[str],
        current: Optional[RuntimeState] = None,
    ) -> RuntimeState:
        assert self._coordinator is not None
        return await self._coordinator.mark_pending_entry_from_order_update(
            strategy_id=self.strategy_id,
            symbol=symbol,
            order_ref=order_ref,
            trigger_id=trigger_id or getattr(current, "active_trigger_id", None),
        )

    async def _resolve_terminal_order_update(self, *, symbol: str, state_norm: str):
        assert self._coordinator is not None
        symbol_u = str(symbol or "").strip().upper()
        snapshot, read_error = await self._safe_read_broker_position(symbol_u, stage=f"order_update:{state_norm.lower()}")
        if read_error is not None:
            return await self._coordinator.mark_quarantine(
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                reason=f"order_update:{state_norm.lower()}:position_snapshot_error:{read_error}",
                broker_qty=None,
            )
        broker_qty = float(snapshot.qty) if snapshot is not None else 0.0
        if abs(broker_qty) > 1e-12:
            return await self._coordinator.mark_live_from_broker(
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                broker_qty=broker_qty,
                avg_px=getattr(snapshot, "avg_px", None),
                reason=f"order_update:{state_norm.lower()}",
            )
        orders, orders_error = await self._safe_read_broker_open_orders(symbol_u, stage=f"order_update:{state_norm.lower()}")
        if orders_error is not None:
            return await self._coordinator.mark_quarantine(
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                reason=f"order_update:{state_norm.lower()}:open_orders_snapshot_error:{orders_error}",
                broker_qty=0.0,
            )
        owned_prefix = self._owned_order_prefix(symbol_u)
        foreign_open_orders = [o for o in orders if not str(getattr(o, "order_ref", "") or "").startswith(owned_prefix)]
        if foreign_open_orders:
            return await self._coordinator.mark_quarantine(
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                reason=f"order_update:{state_norm.lower()}:foreign_open_orders",
                broker_qty=0.0,
            )
        if orders:
            return await self._coordinator.mark_quarantine(
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                reason=f"order_update:{state_norm.lower()}:owned_open_orders_still_present",
                broker_qty=0.0,
            )
        return await self._coordinator.mark_flat(
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            reason=f"order_update:{state_norm.lower()}:broker_flat",
        )

    async def _broker_position_qty(self, symbol: str) -> Optional[float]:
        snap, _ = await self._safe_read_broker_position(symbol, stage="broker_position_qty")
        if snap is None:
            self._diag_event(
                node="broker_position",
                status="missing",
                symbol=str(symbol or "").strip().upper(),
                reason="snapshot_none",
            )
            return None
        self._diag_event(
            node="broker_position",
            status="read",
            symbol=str(symbol or "").strip().upper(),
            reason="snapshot_ok",
            extras={"qty": float(snap.qty), "avg_px": snap.avg_px},
        )
        return float(snap.qty)

    async def _current_exec_qty_for_symbol(self, symbol: str) -> float:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return 0.0
        try:
            pos = await self.ctx.position(symbol_u)
            qty_ctx = float(getattr(pos, "pos_qty", 0.0) or 0.0)
        except Exception:
            self._diag_event(
                node="ctx_position",
                status="error",
                symbol=symbol_u,
                reason="ctx_position_exception_assume_flat",
            )
            return 0.0
        if abs(qty_ctx) <= 1e-9:
            self._diag_event(
                node="ctx_position",
                status="flat",
                symbol=symbol_u,
                reason="ctx_position_zero",
            )
            return 0.0
        self._diag_event(
            node="ctx_position",
            status="nonzero",
            symbol=symbol_u,
            reason="ctx_position_nonzero",
            extras={"qty_ctx": float(qty_ctx)},
        )
        return float(qty_ctx)

    def _extract_signal_observation(self, binding, features: Dict[str, Any]) -> SignalObservation:
        decision: SignalDecision = self._signal_model.extract(binding=binding, features=features)
        return SignalObservation(
            signal_symbol=decision.signal_symbol,
            exec_symbol=decision.exec_symbol,
            signal_active=decision.signal_active,
            target_qty=max(0.0, decision.target_qty),
            side=decision.side,
            reason=decision.reason,
            signal_price=decision.signal_price,
            exec_price=decision.exec_price,
        )

    def _resolve_ib(self):
        try:
            engine = getattr(self.ctx, "engine", None)
            market_data_service = getattr(engine, "market_data_service", None)
            ib_adapter = getattr(market_data_service, "ib_adapter", None)
            return getattr(ib_adapter, "ib", None)
        except Exception:
            return None

    def _resolve_ib_account(self) -> str:
        try:
            engine = getattr(self.ctx, "engine", None)
            market_data_service = getattr(engine, "market_data_service", None)
            ib_adapter = getattr(market_data_service, "ib_adapter", None)
            return str(getattr(ib_adapter, "account", "") or "").strip()
        except Exception:
            return ""

    async def _refresh_ib_positions_best_effort(self, *, force: bool = False) -> bool:
        extras = self.params.extras or {}
        interval_sec = max(0.1, float(extras.get("broker_positions_refresh_interval_sec", 2.0)))
        timeout_sec = max(0.5, float(extras.get("broker_positions_refresh_timeout_sec", 3.0)))
        warn_every_sec = max(2.0, float(extras.get("broker_positions_refresh_warn_interval_sec", 10.0)))
        now_mono = 0.0
        try:
            now_mono = asyncio.get_running_loop().time()
        except Exception:
            pass
        if (not force) and now_mono > 0 and (now_mono - self._last_broker_positions_refresh_mono) < interval_sec:
            return bool(self._broker_positions_ready)
        ib = self._resolve_ib()
        if ib is None:
            self._diag_event(node="ibkr_positions_refresh", status="missing_ib", reason="ib_handle_missing")
            return False
        try:
            is_connected = getattr(ib, "isConnected", None)
            if callable(is_connected) and (not bool(is_connected())):
                self._diag_event(node="ibkr_positions_refresh", status="disconnected", reason="ib_not_connected")
                return False
        except Exception:
            self._diag_event(node="ibkr_positions_refresh", status="error", reason="ib_connected_check_failed")
            return False
        try:
            refresh_ret = ib.reqPositionsAsync() if hasattr(ib, "reqPositionsAsync") else None
            if asyncio.iscoroutine(refresh_ret):
                await asyncio.wait_for(refresh_ret, timeout=timeout_sec)
            elif hasattr(ib, "positions"):
                _ = ib.positions()
            self._broker_positions_ready = True
            self._last_broker_positions_refresh_mono = now_mono
            self._diag_event(
                node="ibkr_positions_refresh",
                status="ok",
                reason="positions_refresh_ok",
                extras={"force": bool(force), "timeout_sec": timeout_sec},
            )
            return True
        except Exception as exc:
            if now_mono <= 0 or (now_mono - self._last_broker_positions_refresh_warn_mono) >= warn_every_sec:
                self._last_broker_positions_refresh_warn_mono = now_mono
                logger.warning(
                    "index_super_win_ib_positions_refresh_failed",
                    strategy_id=self.strategy_id,
                    error_type=type(exc).__name__,
                    error_repr=repr(exc),
                    timeout_sec=timeout_sec,
                )
            self._diag_event(
                node="ibkr_positions_refresh",
                status="error",
                reason=type(exc).__name__,
                extras={"error": repr(exc), "force": bool(force), "timeout_sec": timeout_sec},
            )
            return False

    async def _reconcile_broker_positions(self, *, reason: str) -> None:
        self._diag_event(
            node="broker_reconcile_positions",
            status="start",
            reason=reason,
            extras={"exec_symbols": list(self._exec_symbols)},
        )
        for symbol in self._exec_symbols:
            await self._reconcile_broker_symbol(symbol, reason=reason)
        self._diag_event(node="broker_reconcile_positions", status="done", reason=reason)

    async def _reconcile_broker_symbol(self, symbol: str, *, reason: str):
        assert self._coordinator is not None
        snapshot, read_error = await self._safe_read_broker_position(symbol, stage=f"broker_reconcile:{reason}")
        if read_error is not None:
            decision = await self._coordinator.mark_quarantine(
                strategy_id=self.strategy_id,
                symbol=symbol,
                reason=f"broker_reconcile:{reason}:position_snapshot_error:{read_error}",
                broker_qty=None,
            )
            self._diag_event(
                node="broker_position",
                status="quarantined",
                symbol=str(symbol or "").strip().upper(),
                reason=f"{reason}:position_snapshot_error:{read_error}",
            )
            self._emit_local_audit(
                domain="recon",
                stage="broker_position_quarantined",
                severity="warning",
                status="quarantined",
                symbol=str(symbol or "").strip().upper(),
                reason=f"{reason}:position_snapshot_error:{read_error}",
                payload={"runtime_lifecycle": decision.runtime_state.lifecycle.value},
            )
            return decision
        if snapshot is None or abs(float(snapshot.qty or 0.0)) <= 1e-12:
            self._diag_event(
                node="broker_position",
                status="flat",
                symbol=str(symbol or "").strip().upper(),
                reason=f"{reason}:qty_zero_or_missing",
            )
            return None
        decision = await self._coordinator.mark_live_from_broker(
            strategy_id=self.strategy_id,
            symbol=snapshot.symbol,
            broker_qty=float(snapshot.qty),
            avg_px=snapshot.avg_px,
            reason=f"broker_reconcile:{reason}",
        )
        logger.warning(
            "index_super_win_broker_position_detected",
            strategy_id=self.strategy_id,
            symbol=snapshot.symbol,
            broker_qty=round(float(snapshot.qty), 6),
            avg_px=snapshot.avg_px,
            status=decision.status,
            reason=reason,
        )
        if decision.status == "QUARANTINED":
            self._diag_inc(
                "conflict.foreign_position",
                symbol=snapshot.symbol,
                reason=f"{reason}:foreign_or_unknown_broker_position",
            )
        self._diag_event(
            node="broker_position",
            status=decision.status,
            symbol=snapshot.symbol,
            reason=f"{reason}:broker_qty_nonzero",
            extras={
                "broker_qty": float(snapshot.qty),
                "avg_px": snapshot.avg_px,
                "runtime_lifecycle": decision.runtime_state.lifecycle.value,
            },
        )
        if decision.status == "QUARANTINED":
            self._emit_local_audit(
                domain="recon",
                stage="broker_position_quarantined",
                severity="warning",
                status="quarantined",
                symbol=snapshot.symbol,
                reason=f"{reason}:broker_qty_nonzero",
                payload={
                    "broker_qty": float(snapshot.qty),
                    "avg_px": snapshot.avg_px,
                    "runtime_lifecycle": decision.runtime_state.lifecycle.value,
                },
            )
        return decision

    async def _reconcile_broker_open_orders(self, *, reason: str) -> None:
        self._diag_event(
            node="broker_reconcile_open_orders",
            status="start",
            reason=reason,
            extras={"exec_symbols": list(self._exec_symbols)},
        )
        for symbol in self._exec_symbols:
            await self._reconcile_broker_open_orders_for_symbol(symbol, reason=reason)
        self._diag_event(node="broker_reconcile_open_orders", status="done", reason=reason)

    async def _reconcile_broker_open_orders_for_symbol(self, symbol: str, *, reason: str):
        assert self._coordinator is not None
        orders, read_error = await self._safe_read_broker_open_orders(symbol, stage=f"broker_reconcile:{reason}")
        if read_error is not None:
            decision = await self._coordinator.mark_quarantine(
                strategy_id=self.strategy_id,
                symbol=symbol,
                reason=f"broker_reconcile:{reason}:open_orders_snapshot_error:{read_error}",
                broker_qty=None,
            )
            self._diag_event(
                node="broker_open_orders",
                status="quarantined",
                symbol=str(symbol or "").strip().upper(),
                reason=f"{reason}:open_orders_snapshot_error:{read_error}",
            )
            self._emit_local_audit(
                domain="recon",
                stage="broker_open_orders_quarantined",
                severity="warning",
                status="quarantined",
                symbol=str(symbol or "").strip().upper(),
                reason=f"{reason}:open_orders_snapshot_error:{read_error}",
                payload={"runtime_lifecycle": decision.runtime_state.lifecycle.value},
            )
            return decision
        if not orders:
            self._diag_event(
                node="broker_open_orders",
                status="clean",
                symbol=str(symbol or "").strip().upper(),
                reason=f"{reason}:no_open_orders",
            )
            return None
        namespace = self._coordinator.config.order_namespace
        symbol_u = str(symbol or "").strip().upper()
        owned_prefix = f"{namespace}:{self.strategy_id}:{symbol_u}:"
        unknown = [o for o in orders if not str(o.order_ref or "").startswith(owned_prefix)]
        if not unknown:
            self._diag_event(
                node="broker_open_orders",
                status="owned_only",
                symbol=symbol_u,
                reason=f"{reason}:open_orders_owned",
                extras={"open_orders_count": len(orders)},
            )
            return None
        decision = await self._coordinator.mark_quarantine(
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            reason=f"broker_reconcile:{reason}:unknown_open_orders",
            broker_qty=None,
        )
        self._diag_inc(
            "conflict.foreign_open_orders",
            symbol=symbol_u,
            reason=f"{reason}:foreign_open_orders",
            delta=len(unknown),
        )
        logger.warning(
            "index_super_win_unknown_open_orders_detected",
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            reason=reason,
            unknown_count=len(unknown),
            unknown_order_ids=[o.broker_order_id for o in unknown[:10]],
        )
        self._diag_event(
            node="broker_open_orders",
            status="quarantined",
            symbol=symbol_u,
            reason=f"{reason}:unknown_open_orders",
            extras={
                "unknown_count": len(unknown),
                "unknown_order_ids": [o.broker_order_id for o in unknown[:10]],
            },
        )
        self._emit_local_audit(
            domain="recon",
            stage="broker_open_orders_quarantined",
            severity="warning",
            status="quarantined",
            symbol=symbol_u,
            reason=f"{reason}:unknown_open_orders",
            payload={
                "unknown_count": len(unknown),
                "unknown_order_ids": [o.broker_order_id for o in unknown[:10]],
                "runtime_lifecycle": decision.runtime_state.lifecycle.value,
            },
        )
        return decision

    def _diag_inc(self, key: str, *, symbol: Optional[str] = None, reason: Optional[str] = None, delta: int = 1) -> None:
        key_u = str(key or "").strip() or "unknown"
        self._diag_counters[key_u] = int(self._diag_counters.get(key_u, 0)) + int(delta)
        symbol_u = str(symbol or "").strip().upper()
        if symbol_u:
            bucket = self._diag_symbol_counters.setdefault(symbol_u, {})
            bucket[key_u] = int(bucket.get(key_u, 0)) + int(delta)
        reason_u = str(reason or "").strip()
        if reason_u:
            self._diag_reason_counters[reason_u] = int(self._diag_reason_counters.get(reason_u, 0)) + int(delta)
            if symbol_u:
                reason_bucket = self._diag_symbol_reason_counters.setdefault(symbol_u, {})
                reason_bucket[reason_u] = int(reason_bucket.get(reason_u, 0)) + int(delta)

    def _diag_snapshot_payload(self) -> dict[str, Any]:
        top_reasons = sorted(self._diag_reason_counters.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:50]
        symbol_reason_top: dict[str, dict[str, int]] = {}
        for sym, counters in sorted(self._diag_symbol_reason_counters.items()):
            top = sorted(counters.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:20]
            symbol_reason_top[sym] = {k: int(v) for k, v in top}
        conflict_report: dict[str, dict[str, Any]] = {}
        for sym in sorted(set(self._symbols) | set(self._runtime_health_by_symbol.keys()) | set(self._diag_symbol_counters.keys())):
            health = dict(self._runtime_health_by_symbol.get(sym) or {})
            symbol_counters = self._diag_symbol_counters.get(sym, {})
            ownership_conflict = dict(self._ownership_conflicts_by_symbol.get(sym) or {})
            conflict_report[sym] = {
                "foreign_open_order_conflicts": int(health.get("foreign_order_conflicts_detected", 0) or 0),
                "foreign_position_conflict_detected": bool(health.get("foreign_position_conflict_detected", False)),
                "broker_open_orders_quarantined": int(symbol_counters.get("broker_open_orders.quarantined", 0)),
                "broker_position_quarantined": int(symbol_counters.get("broker_position.QUARANTINED", 0)),
                "startup_recovery_action": health.get("action"),
                "startup_recovery_reason": health.get("action_reason"),
                "foreign_owner_strategy_id": ownership_conflict.get("owner_strategy_id"),
                "foreign_symbol_ownership_conflicts": int(symbol_counters.get("conflict.foreign_symbol_ownership", 0)),
            }
        shadow_report: dict[str, dict[str, Any]] = {}
        for sym in sorted(set(conflict_report.keys()) | set(self._startup_consistency_by_symbol.keys()) | set(self._chain_report_by_symbol.keys())):
            startup_check = dict(self._startup_consistency_by_symbol.get(sym) or {})
            chain = dict(self._chain_report_by_symbol.get(sym) or {})
            conflict = dict(conflict_report.get(sym) or {})
            shadow_report[sym] = {
                "entry_enabled": bool(self._entry_enabled_by_symbol.get(sym, True)),
                "block_reason": startup_check.get("block_reason"),
                "foreign_order_conflicts_detected": int(conflict.get("foreign_open_order_conflicts", 0)),
                "foreign_position_conflicts_detected": bool(conflict.get("foreign_position_conflict_detected", False)),
                "last_gate_status": (chain.get("entry_gate") or {}).get("status"),
                "last_gate_reason": (chain.get("entry_gate") or {}).get("reason"),
                "last_preview_status": (chain.get("preview") or {}).get("status"),
                "last_submit_status": (chain.get("submit") or {}).get("status"),
                "last_runtime_status": (chain.get("runtime") or {}).get("status"),
            }
        trader_view: dict[str, dict[str, Any]] = {}
        for sym in sorted(set(self._symbols) | set(self._diag_symbol_counters.keys()) | set(self._last_preview_by_symbol.keys())):
            sym_counters = self._diag_symbol_counters.get(sym, {})
            sym_reasons = symbol_reason_top.get(sym, {})
            attempts = int(sym_counters.get("entry.attempts", 0))
            blocked = int(sym_counters.get("entry_gate.blocked", 0))
            submitted = int(sym_counters.get("runtime.INTENT_SUBMITTED", 0))
            preview = self._last_preview_by_symbol.get(sym)
            if submitted > 0:
                final_state = "entered"
            elif blocked > 0:
                final_state = "blocked"
            elif attempts > 0:
                final_state = "attempted_not_entered"
            else:
                final_state = "no_attempt"
            trader_view[sym] = {
                "final_state": final_state,
                "entry_attempts": attempts,
                "entry_blocked": blocked,
                "entry_submitted": submitted,
                "which_gate_blocked": (self._chain_report_by_symbol.get(sym, {}).get("entry_gate") or {}).get("reason"),
                "broker_open_order_conflict": bool((conflict_report.get(sym) or {}).get("foreign_open_order_conflicts", 0)),
                "broker_position_conflict": bool((conflict_report.get(sym) or {}).get("foreign_position_conflict_detected", False)),
                "foreign_owner_strategy_id": (conflict_report.get(sym) or {}).get("foreign_owner_strategy_id"),
                "top_reasons": dict(sym_reasons),
                "last_preview": dict(preview or {}),
            }
        return {
            "strategy_id": self.strategy_id,
            "day_et": self._diag_day_et,
            "generated_at_us": int(time.time() * 1_000_000),
            "dry_run": self._dry_run,
            "symbols": list(self._symbols),
            "exec_symbols": list(self._exec_symbols),
            "counters": dict(sorted(self._diag_counters.items())),
            "reason_counters_top": {k: int(v) for k, v in top_reasons},
            "symbol_counters": {sym: dict(sorted(cnt.items())) for sym, cnt in sorted(self._diag_symbol_counters.items())},
            "symbol_reason_counters_top": symbol_reason_top,
            "startup_recovery_by_symbol": dict(self._startup_recovery_by_symbol),
            "runtime_health_by_symbol": dict(self._runtime_health_by_symbol),
            "startup_consistency_by_symbol": dict(self._startup_consistency_by_symbol),
            "entry_enabled_by_symbol": dict(self._entry_enabled_by_symbol),
            "conflict_report_by_symbol": conflict_report,
            "chain_report_by_symbol": dict(self._chain_report_by_symbol),
            "shadow_report_by_symbol": shadow_report,
            "trader_view_by_symbol": trader_view,
            "last_preview_by_symbol": dict(self._last_preview_by_symbol),
            "protective_orders_by_symbol": dict(self._protective_orders_by_symbol),
        }

    def _diag_flush_snapshot(self, *, force: bool = False) -> None:
        now_mono = 0.0
        try:
            now_mono = asyncio.get_running_loop().time()
        except Exception:
            now_mono = 0.0
        payload = self._diag_snapshot_payload()
        self.state["diag_snapshot"] = payload
        if not self._diag_enabled or self._diag_snapshot_path is None:
            self._diag_last_flush_mono = now_mono
            return
        if (not force) and now_mono > 0 and (now_mono - self._diag_last_flush_mono) < self._diag_flush_interval_sec:
            return
        try:
            self._diag_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._diag_snapshot_path.with_suffix(self._diag_snapshot_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self._diag_snapshot_path)
        except Exception as exc:
            logger.warning(
                "index_super_win_diag_snapshot_write_failed",
                strategy_id=self.strategy_id,
                path=str(self._diag_snapshot_path),
                error=str(exc),
            )
        self._diag_last_flush_mono = now_mono

    def _diag_event(
        self,
        *,
        node: str,
        status: str,
        symbol: Optional[str] = None,
        reason: Optional[str] = None,
        extras: Optional[dict[str, Any]] = None,
    ) -> None:
        node_u = str(node or "unknown").strip()
        status_u = str(status or "unknown").strip()
        symbol_u = str(symbol or "").strip().upper() or None
        reason_u = str(reason or "").strip() or None
        self._diag_inc(f"{node_u}.{status_u}", symbol=symbol_u, reason=reason_u)
        payload = {
            "strategy_id": self.strategy_id,
            "ts_us": int(time.time() * 1_000_000),
            "node": node_u,
            "status": status_u,
            "symbol": symbol_u,
            "reason": reason_u,
            "extras": dict(extras or {}),
        }
        if symbol_u:
            bucket = self._chain_report_by_symbol.setdefault(symbol_u, {})
            bucket[node_u] = {
                "status": status_u,
                "reason": reason_u,
                "ts_us": int(payload["ts_us"]),
                "extras": dict(extras or {}),
            }
        logger.info(
            "index_super_win_diag_event",
            strategy_id=self.strategy_id,
            node=node_u,
            status=status_u,
            symbol=symbol_u,
            reason=reason_u,
            extras=dict(extras or {}),
        )
        if self._diag_enabled and self._diag_log_path is not None:
            try:
                self._diag_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self._diag_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
            except Exception as exc:
                logger.warning(
                    "index_super_win_diag_log_write_failed",
                    strategy_id=self.strategy_id,
                    path=str(self._diag_log_path),
                    error=str(exc),
                )
        self._diag_flush_snapshot(force=False)

    def _resolve_local_audit_writer(self):
        engine = getattr(self.ctx, "engine", None)
        candidates = [
            getattr(engine, "audit_writer", None),
            getattr(getattr(engine, "_strategy_engine", None), "audit_writer", None),
        ]
        for writer in candidates:
            if writer is not None and hasattr(writer, "emit"):
                return writer
        return None

    def _remember_audit_context(
        self,
        *,
        symbol: str,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        order_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        symbol_u = str(symbol or "").strip().upper()
        current = dict(self._audit_context_by_symbol.get(symbol_u) or {})
        if decision_id:
            current["decision_id"] = str(decision_id)
        if trigger_id:
            current["trigger_id"] = str(trigger_id)
        if trace_id:
            current["trace_id"] = str(trace_id)
        if order_ref:
            current["order_ref"] = str(order_ref)
        self._audit_context_by_symbol[symbol_u] = current
        return current

    def _ensure_audit_context(
        self,
        *,
        symbol: str,
        trigger_id: Optional[str] = None,
        order_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        symbol_u = str(symbol or "").strip().upper()
        current = dict(self._audit_context_by_symbol.get(symbol_u) or {})
        trigger_val = str(trigger_id or current.get("trigger_id") or make_decision_id() or "").strip()
        trace_val = str(current.get("trace_id") or trigger_val).strip()
        return self._remember_audit_context(
            symbol=symbol_u,
            decision_id=str(current.get("decision_id") or "").strip() or None,
            trigger_id=trigger_val or None,
            trace_id=trace_val or None,
            order_ref=str(order_ref or current.get("order_ref") or "").strip() or None,
        )

    def _inject_trace_fields(self, envelope: Any, *, symbol: str, trigger_id: Optional[str]) -> dict[str, Any]:
        extras = dict(getattr(envelope, "extras", {}) or {})
        decision_id = str(getattr(envelope, "decision_id", "") or extras.get("decision_id") or "").strip()
        trigger_id_val = str(extras.get("trigger_id") or trigger_id or decision_id).strip()
        trace_id = str(extras.get("trace_id") or decision_id or trigger_id_val).strip()
        strategy_run_id = str(
            extras.get("strategy_run_id")
            or getattr(self.ctx, "strategy_run_id", "")
            or getattr(getattr(self.ctx, "engine", None), "strategy_run_id", "")
            or ""
        ).strip()
        if strategy_run_id:
            extras["strategy_run_id"] = strategy_run_id
        if trigger_id_val:
            extras["trigger_id"] = trigger_id_val
        if trace_id:
            extras["trace_id"] = trace_id
        extras.setdefault("intent_source", "index_super_win")
        envelope.extras = extras
        return self._remember_audit_context(
            symbol=symbol,
            decision_id=decision_id or None,
            trigger_id=trigger_id_val or None,
            trace_id=trace_id or None,
            order_ref=str(extras.get("order_ref") or "").strip() or None,
        )

    def _emit_local_audit(
        self,
        *,
        stage: str,
        domain: str = "strategy",
        severity: str = "info",
        status: Optional[str] = None,
        reason: Optional[str] = None,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._local_audit_enabled:
            return
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        symbol_u = str(symbol or "").strip().upper()
        audit_ctx = dict(self._audit_context_by_symbol.get(symbol_u) or {})
        decision_id_val = str(decision_id or audit_ctx.get("decision_id") or "").strip() or None
        trigger_id_val = str(trigger_id or audit_ctx.get("trigger_id") or "").strip() or None
        trace_id_val = str(trace_id or audit_ctx.get("trace_id") or decision_id_val or trigger_id_val or "").strip() or None
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        try:
            writer.emit(
                domain=domain,
                component="index_super_win_strategy",
                stage=stage,
                severity=severity,
                strategy_id=self.strategy_id,
                strategy_run_id=strategy_run_id,
                trigger_id=trigger_id_val,
                decision_id=decision_id_val,
                trace_id=trace_id_val,
                symbol=symbol_u or None,
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            logger.warning(
                "index_super_win_local_audit_emit_failed",
                strategy_id=self.strategy_id,
                stage=stage,
                symbol=symbol_u or None,
                error=str(exc),
            )

    def _emit_gate_blocked_audit(
        self,
        *,
        symbol: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
        trigger_id: Optional[str] = None,
        order_ref: Optional[str] = None,
    ) -> None:
        audit_ctx = self._ensure_audit_context(symbol=symbol, trigger_id=trigger_id, order_ref=order_ref)
        self._emit_local_audit(
            stage="entry_gate_blocked",
            severity="warning",
            status="blocked",
            reason=reason,
            symbol=symbol,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload=payload,
        )

    def _on_state_transition_event(self, event) -> None:
        self._emit_local_audit(
            domain="state",
            stage=f"{event.state_machine}_transition",
            status=event.after_state,
            reason=event.reason,
            symbol=event.symbol,
            decision_id=event.decision_id,
            trigger_id=event.trigger_id,
            trace_id=event.trace_id,
            payload=event.to_dict(),
        )

    @staticmethod
    def _feature_ts_us(features: Dict[str, Any]) -> int:
        for key in ("ts_bar_end_us", "bar_end_ts_us", "feature_ts_us", "ts_us"):
            try:
                value = int(features.get(key) or 0)
            except Exception:
                value = 0
            if value > 0:
                return value
        return int(time.time() * 1_000_000)

    def _feature_float(
        self,
        features: Dict[str, Any],
        *keys: str,
        default: Optional[float] = None,
    ) -> Optional[float]:
        for key in keys:
            if not key:
                continue
            value = _safe_float(features.get(key))
            if value is not None:
                return value
        return default

    def _market_feature_snapshot(self, *, binding, features: Dict[str, Any], obs: SignalObservation) -> Dict[str, Any]:
        signal_symbol = str(getattr(binding, "signal_symbol", "") or "").strip().upper()
        confirm_symbol = str(getattr(binding, "confirm_symbol", "") or "").strip().upper()
        signal_prefix = signal_symbol.lower()
        confirm_prefix = confirm_symbol.lower()
        signal_price = self._feature_float(
            features,
            f"{signal_prefix}_close_now",
            "qqq_close_now",
            "signal_price",
            "close",
            "last_px",
            default=obs.signal_price,
        )
        exec_price = self._feature_float(
            features,
            "exec_price",
            "exec_last_px",
            "exec_close",
            default=obs.exec_price,
        )
        if exec_price is None:
            exec_price = signal_price
        atr_live = self._feature_float(features, "atr_live", "atr")
        atr_eff = self._feature_float(features, "atr_eff", "entry_atr_used", "atr")
        return {
            "last_signal_reason": str(obs.reason or "").strip() or None,
            "last_signal_side": str(obs.side or "").strip().upper() or None,
            "last_target_qty": float(obs.target_qty or 0.0),
            "last_signal_active": bool(obs.signal_active),
            "last_signal_price": signal_price,
            "last_exec_price": exec_price,
            "last_vwap_now": self._feature_float(features, f"{signal_prefix}_vwap_now", "vwap_now", "qqq_vwap", "vwap"),
            "last_confirm_symbol": confirm_symbol or None,
            "last_confirm_price": self._feature_float(features, f"{confirm_prefix}_close_now", "qqew_close_now"),
            "last_confirm_vwap_now": self._feature_float(features, f"{confirm_prefix}_vwap_now", "qqew_vwap_now"),
            "last_atr_live": atr_live,
            "last_atr_eff": atr_eff,
            "last_atr_seed": self._feature_float(features, "atr_seed"),
            "last_atr_cap": self._feature_float(features, "atr_cap"),
            "last_distance_from_vwap_atr": self._feature_float(features, "distance_from_vwap_atr"),
            "last_vol_surge_ratio": self._feature_float(features, "vol_surge_ratio"),
            "last_vol_ratio": self._feature_float(features, "vol_ratio"),
            "last_v3m_price": self._feature_float(features, "v3m_price"),
            "last_v3m_signed": self._feature_float(features, "v3m_signed"),
        }

    def _record_market_feature_summary(self, symbol: str, *, binding, features: Dict[str, Any], obs: SignalObservation) -> None:
        if not self._market_audit_enabled:
            return
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return
        ts_us = self._feature_ts_us(features)
        minute_bucket = int(ts_us // 60_000_000)
        state = self._market_audit_by_symbol.get(symbol_u)
        if state is None or int(state.get("minute_bucket", -1)) != minute_bucket:
            self._flush_market_feature_summary(symbol_u)
            state = {
                "minute_bucket": minute_bucket,
                "feature_count": 0,
                "signal_active_count": 0,
                "last_feature_ts_us": None,
                "last_signal_price": None,
                "last_exec_price": None,
                "max_gap_ms": 0.0,
                "missing_price_events": 0,
                "last_signal_reason": None,
                "last_signal_side": None,
                "last_target_qty": 0.0,
                "last_signal_active": False,
                "last_vwap_now": None,
                "last_confirm_symbol": None,
                "last_confirm_price": None,
                "last_confirm_vwap_now": None,
                "last_atr_live": None,
                "last_atr_eff": None,
                "last_atr_seed": None,
                "last_atr_cap": None,
                "last_distance_from_vwap_atr": None,
                "last_vol_surge_ratio": None,
                "last_vol_ratio": None,
                "last_v3m_price": None,
                "last_v3m_signed": None,
            }
            self._market_audit_by_symbol[symbol_u] = state
        last_feature_ts_us = state.get("last_feature_ts_us")
        if last_feature_ts_us:
            gap_ms = max(0.0, float(ts_us - int(last_feature_ts_us)) / 1000.0)
            state["max_gap_ms"] = max(float(state.get("max_gap_ms", 0.0) or 0.0), gap_ms)
            if gap_ms >= float(self._market_audit_gap_sec) * 1000.0:
                self._emit_local_audit(
                    domain="market",
                    stage="feature_stream_gap",
                    severity="warning",
                    status="stale",
                    symbol=symbol_u,
                    reason="feature_stream_gap_exceeded",
                    payload={
                        "gap_ms": round(float(gap_ms), 3),
                        "threshold_sec": float(self._market_audit_gap_sec),
                        "ts_us": ts_us,
                    },
                )
        state["feature_count"] = int(state.get("feature_count", 0) or 0) + 1
        if bool(obs.signal_active):
            state["signal_active_count"] = int(state.get("signal_active_count", 0) or 0) + 1
        state["last_feature_ts_us"] = ts_us
        state.update(self._market_feature_snapshot(binding=binding, features=features, obs=obs))
        if bool(obs.signal_active) and ((obs.signal_price is None or float(obs.signal_price or 0.0) <= 0.0) or (obs.exec_price is None or float(obs.exec_price or 0.0) <= 0.0)):
            state["missing_price_events"] = int(state.get("missing_price_events", 0) or 0) + 1
            self._emit_local_audit(
                domain="market",
                stage="feature_price_missing",
                severity="warning",
                status="incomplete",
                symbol=symbol_u,
                reason="signal_active_missing_price",
                payload={
                    "signal_price": obs.signal_price,
                    "exec_price": obs.exec_price,
                    "ts_us": ts_us,
                },
            )

    def _flush_market_feature_summary(self, symbol: str) -> None:
        if not self._market_audit_enabled:
            return
        symbol_u = str(symbol or "").strip().upper()
        state = self._market_audit_by_symbol.get(symbol_u)
        if not state:
            return
        minute_bucket = int(state.get("minute_bucket", 0) or 0)
        if minute_bucket <= 0:
            return
        bucket_dt = datetime.fromtimestamp(minute_bucket * 60, tz=ZoneInfo("UTC")).astimezone(ET)
        self._emit_local_audit(
            domain="market",
            stage="feature_minute_summary",
            status="ok",
            symbol=symbol_u,
            payload={
                "minute_et": bucket_dt.strftime("%Y-%m-%d %H:%M:00"),
                "feature_count": int(state.get("feature_count", 0) or 0),
                "signal_active_count": int(state.get("signal_active_count", 0) or 0),
                "last_feature_ts_us": state.get("last_feature_ts_us"),
                "last_signal_price": state.get("last_signal_price"),
                "last_exec_price": state.get("last_exec_price"),
                "last_signal_reason": state.get("last_signal_reason"),
                "last_signal_side": state.get("last_signal_side"),
                "last_target_qty": state.get("last_target_qty"),
                "last_signal_active": state.get("last_signal_active"),
                "last_vwap_now": state.get("last_vwap_now"),
                "last_confirm_symbol": state.get("last_confirm_symbol"),
                "last_confirm_price": state.get("last_confirm_price"),
                "last_confirm_vwap_now": state.get("last_confirm_vwap_now"),
                "last_atr_live": state.get("last_atr_live"),
                "last_atr_eff": state.get("last_atr_eff"),
                "last_atr_seed": state.get("last_atr_seed"),
                "last_atr_cap": state.get("last_atr_cap"),
                "last_distance_from_vwap_atr": state.get("last_distance_from_vwap_atr"),
                "last_vol_surge_ratio": state.get("last_vol_surge_ratio"),
                "last_vol_ratio": state.get("last_vol_ratio"),
                "last_v3m_price": state.get("last_v3m_price"),
                "last_v3m_signed": state.get("last_v3m_signed"),
                "max_gap_ms": round(float(state.get("max_gap_ms", 0.0) or 0.0), 3),
                "missing_price_events": int(state.get("missing_price_events", 0) or 0),
            },
        )
        self._market_audit_by_symbol.pop(symbol_u, None)

    def _flush_all_market_feature_summaries(self) -> None:
        for symbol in list(self._market_audit_by_symbol.keys()):
            self._flush_market_feature_summary(symbol)

    async def _run_startup_consistency_checks(self) -> None:
        results: dict[str, dict[str, Any]] = {}
        entry_enabled: dict[str, bool] = {}
        self._diag_event(
            node="startup_consistency",
            status="start",
            reason="initialize",
            extras={"exec_symbols": list(self._exec_symbols)},
        )
        for symbol in self._exec_symbols:
            result = await self._collect_startup_consistency(symbol)
            results[symbol] = result
            entry_enabled[symbol] = bool(result.get("entry_enabled", False))
        self._startup_consistency_by_symbol = dict(results)
        self._entry_enabled_by_symbol = dict(entry_enabled)
        self.state["startup_consistency_by_symbol"] = dict(results)
        self.state["entry_enabled_by_symbol"] = dict(entry_enabled)
        self._diag_event(
            node="startup_consistency",
            status="done",
            reason="initialize",
            extras={"entry_enabled_by_symbol": dict(entry_enabled)},
        )

    async def _collect_startup_consistency(self, symbol: str) -> dict[str, Any]:
        symbol_u = str(symbol or "").strip().upper()
        positions_refresh_ok = await self._refresh_ib_positions_best_effort(force=True)
        open_orders_probe_ok, open_orders_probe_error = await self._probe_ib_open_orders_health()
        _, position_read_error = await self._safe_read_broker_position(symbol_u, stage="startup_consistency")
        _, open_orders_read_error = await self._safe_read_broker_open_orders(symbol_u, stage="startup_consistency")
        block_reason = ""
        if not positions_refresh_ok:
            block_reason = "startup_consistency:positions_refresh_unavailable"
        elif not open_orders_probe_ok:
            block_reason = f"startup_consistency:open_orders_probe_error:{open_orders_probe_error or 'unknown'}"
        elif position_read_error is not None:
            block_reason = f"startup_consistency:position_snapshot_error:{position_read_error}"
        elif open_orders_read_error is not None:
            block_reason = f"startup_consistency:open_orders_snapshot_error:{open_orders_read_error}"
        entry_enabled = not bool(block_reason)
        result = {
            "entry_enabled": bool(entry_enabled),
            "positions_refresh_ok": bool(positions_refresh_ok),
            "open_orders_probe_ok": bool(open_orders_probe_ok),
            "open_orders_probe_error": open_orders_probe_error,
            "position_snapshot_error": position_read_error,
            "open_orders_snapshot_error": open_orders_read_error,
            "block_reason": block_reason or None,
        }
        self._diag_event(
            node="startup_consistency",
            status="ready" if entry_enabled else "blocked",
            symbol=symbol_u,
            reason=block_reason or "startup_consistency_ok",
            extras=dict(result),
        )
        self._emit_local_audit(
            domain="system",
            stage="startup_consistency_checked",
            severity="warning" if (not entry_enabled) else "info",
            status="ready" if entry_enabled else "blocked",
            symbol=symbol_u,
            reason=block_reason or "startup_consistency_ok",
            payload=dict(result),
        )
        return result

    async def _run_startup_runtime_recovery(self) -> None:
        results: dict[str, dict[str, Any]] = {}
        self._diag_event(
            node="startup_recovery",
            status="start",
            reason="initialize",
            extras={"exec_symbols": list(self._exec_symbols)},
        )
        for symbol in self._exec_symbols:
            results[symbol] = await self._recover_runtime_symbol_on_startup(symbol)
        self._startup_recovery_by_symbol = dict(results)
        self.state["startup_recovery_by_symbol"] = dict(results)
        self.state["runtime_health_by_symbol"] = dict(self._runtime_health_by_symbol)
        self._diag_event(
            node="startup_recovery",
            status="done",
            reason="initialize",
            extras={"recovered_symbols": sorted(results.keys())},
        )

    async def _recover_runtime_symbol_on_startup(self, symbol: str) -> dict[str, Any]:
        assert self._runtime_store is not None
        assert self._coordinator is not None
        symbol_u = str(symbol or "").strip().upper()
        runtime_state = await self._runtime_store.get_state(self.strategy_id, symbol_u)
        position, position_read_error = await self._safe_read_broker_position(symbol_u, stage="startup_recovery")
        open_orders, open_orders_read_error = await self._safe_read_broker_open_orders(symbol_u, stage="startup_recovery")
        if position_read_error is not None or open_orders_read_error is not None:
            decision = await self._coordinator.mark_quarantine(
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                reason=(
                    f"startup_recovery:snapshot_error:{position_read_error or ''}:{open_orders_read_error or ''}".rstrip(":")
                ),
                broker_qty=None,
            )
            runtime_state = decision.runtime_state
            lifecycle = runtime_state.lifecycle.value
            summary = {
                "runtime_lifecycle": lifecycle,
                "broker_qty": 0.0,
                "owned_open_orders": 0,
                "foreign_open_orders": 0,
                "foreign_order_conflicts_detected": 0,
                "foreign_position_conflict_detected": False,
                "action": "quarantined_snapshot_error",
                "action_reason": decision.runtime_state.quarantine_reason,
                "runtime_age_sec": None,
                "position_snapshot_error": position_read_error,
                "open_orders_snapshot_error": open_orders_read_error,
                "owned_open_orders_present": False,
            }
            self._runtime_health_by_symbol[symbol_u] = dict(summary)
            self._diag_event(
                node="startup_recovery",
                status="quarantined",
                symbol=symbol_u,
                reason=str(decision.runtime_state.quarantine_reason or "startup_recovery_snapshot_error"),
                extras=dict(summary),
            )
            self._emit_local_audit(
                domain="recon",
                stage="startup_recovery_quarantined",
                severity="warning",
                status="quarantined",
                symbol=symbol_u,
                reason=str(decision.runtime_state.quarantine_reason or "startup_recovery_snapshot_error"),
                payload=dict(summary),
            )
            return summary
        broker_qty = float(position.qty) if position is not None else 0.0
        namespace = self._coordinator.config.order_namespace
        owned_prefix = f"{namespace}:{self.strategy_id}:{symbol_u}:"
        owned_open_orders = [o for o in open_orders if str(o.order_ref or "").startswith(owned_prefix)]
        foreign_open_orders = [o for o in open_orders if not str(o.order_ref or "").startswith(owned_prefix)]
        lifecycle = runtime_state.lifecycle.value if runtime_state is not None else "missing"
        last_synced_at_us = int(getattr(runtime_state, "last_synced_at_us", 0) or 0)
        age_sec = max(0.0, (int(time.time() * 1_000_000) - last_synced_at_us) / 1_000_000.0) if last_synced_at_us > 0 else None
        action = "noop"
        action_reason = "no_runtime_state"
        if runtime_state is not None:
            action_reason = f"runtime:{runtime_state.lifecycle.value}"
        stale_submitted_age_sec = max(
            1.0,
            float((self.params.extras or {}).get("startup_stale_submitted_age_sec", 120.0) or 120.0),
        )
        if runtime_state is not None and runtime_state.lifecycle in {RuntimeLifecycle.INTENT_SUBMITTED, RuntimeLifecycle.PENDING_ENTRY}:
            pending_label = runtime_state.lifecycle.value
            if abs(float(broker_qty)) <= 1e-12 and (not owned_open_orders) and (not foreign_open_orders):
                if age_sec is not None and age_sec >= stale_submitted_age_sec:
                    decision = await self._coordinator.mark_flat(
                        strategy_id=self.strategy_id,
                        symbol=symbol_u,
                        reason=f"startup_stale_{pending_label}_cleared",
                    )
                    runtime_state = decision.runtime_state
                    lifecycle = runtime_state.lifecycle.value
                    action = f"cleared_stale_{pending_label}"
                    action_reason = f"startup_stale_{pending_label}_cleared"
                    self._diag_event(
                        node="startup_recovery",
                        status="cleared",
                        symbol=symbol_u,
                        reason=action_reason,
                        extras={"age_sec": age_sec, "stale_threshold_sec": stale_submitted_age_sec},
                    )
                else:
                    action = "kept_recent_pending_state"
                    action_reason = f"recent_{pending_label}"
            elif abs(float(broker_qty)) <= 1e-12 and foreign_open_orders:
                decision = await self._coordinator.mark_quarantine(
                    strategy_id=self.strategy_id,
                    symbol=symbol_u,
                    reason="startup_stale_intent_with_foreign_open_orders",
                    broker_qty=0.0,
                )
                runtime_state = decision.runtime_state
                lifecycle = runtime_state.lifecycle.value
                action = "quarantined_stale_intent_foreign_orders"
                action_reason = "startup_stale_intent_with_foreign_open_orders"
                self._diag_event(
                    node="startup_recovery",
                    status="quarantined",
                    symbol=symbol_u,
                    reason=action_reason,
                    extras={"foreign_open_orders": len(foreign_open_orders)},
                )
                self._emit_local_audit(
                    domain="recon",
                    stage="startup_recovery_quarantined",
                    severity="warning",
                    status="quarantined",
                    symbol=symbol_u,
                    reason=action_reason,
                    payload={"foreign_open_orders": len(foreign_open_orders)},
                )
            elif abs(float(broker_qty)) <= 1e-12 and owned_open_orders:
                action = "kept_pending_state_with_owned_open_orders"
                action_reason = f"{pending_label}:owned_open_orders_present"
        summary = {
            "runtime_lifecycle": lifecycle,
            "broker_qty": float(broker_qty),
            "owned_open_orders": len(owned_open_orders),
            "foreign_open_orders": len(foreign_open_orders),
            "foreign_order_conflicts_detected": len(foreign_open_orders),
            "foreign_position_conflict_detected": bool(abs(float(broker_qty)) > 1e-12 and lifecycle not in {"live", "quarantine"}),
            "action": action,
            "action_reason": action_reason,
            "runtime_age_sec": round(float(age_sec), 6) if age_sec is not None else None,
            "owned_open_orders_present": bool(owned_open_orders),
            "position_snapshot_error": None,
            "open_orders_snapshot_error": None,
        }
        if int(summary["foreign_order_conflicts_detected"]) > 0:
            self._diag_inc(
                "conflict.foreign_open_orders",
                symbol=symbol_u,
                reason="startup_recovery:foreign_open_orders",
                delta=int(summary["foreign_order_conflicts_detected"]),
            )
        if bool(summary["foreign_position_conflict_detected"]):
            self._diag_inc(
                "conflict.foreign_position",
                symbol=symbol_u,
                reason="startup_recovery:foreign_position_conflict",
            )
        lifecycle_enum = next((item for item in RuntimeLifecycle if item.value == lifecycle), RuntimeLifecycle.BROKER_UNKNOWN)
        issues = validate_cross_state_consistency(
            CrossStateSnapshot(
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                runtime_state=lifecycle_enum,
                broker_qty=float(broker_qty),
                has_owned_live_order=bool(owned_open_orders),
                allow_new_entry=False,
            )
        )
        if issues:
            summary["cross_state_issues"] = list(issues)
            self._emit_local_audit(
                domain="state",
                stage="cross_state_inconsistency_detected",
                severity="warning",
                status="warning",
                symbol=symbol_u,
                reason="|".join(issues),
                payload=dict(summary),
            )
        self._runtime_health_by_symbol[symbol_u] = dict(summary)
        return summary

    async def _pre_entry_runtime_block(self, symbol: str) -> Optional[Any]:
        assert self._runtime_store is not None
        symbol_u = str(symbol or "").strip().upper()
        state = await self._runtime_store.get_state(self.strategy_id, symbol_u)
        if state is None:
            return None
        if state.lifecycle in {RuntimeLifecycle.FLAT, RuntimeLifecycle.PREVIEW}:
            return None
        reason_map = {
            RuntimeLifecycle.INTENT_SUBMITTED: "runtime_guard:intent_already_submitted",
            RuntimeLifecycle.PENDING_ENTRY: "runtime_guard:pending_entry_exists",
            RuntimeLifecycle.LIVE: "runtime_guard:live_position_exists",
            RuntimeLifecycle.PENDING_EXIT: "runtime_guard:pending_exit_exists",
            RuntimeLifecycle.QUARANTINE: state.quarantine_reason or "runtime_guard:quarantined",
            RuntimeLifecycle.BROKER_UNKNOWN: "runtime_guard:broker_unknown",
        }
        return state.evolve(last_error=reason_map.get(state.lifecycle, f"runtime_guard:{state.lifecycle.value}"))

    async def _safe_read_broker_position(self, symbol: str, *, stage: str) -> tuple[Optional[Any], Optional[str]]:
        symbol_u = str(symbol or "").strip().upper()
        try:
            return await self._broker_snapshots.read_position(symbol_u), None
        except Exception as exc:
            err = type(exc).__name__
            self._diag_event(
                node="broker_position",
                status="error",
                symbol=symbol_u,
                reason=f"{stage}:snapshot_read_error:{err}",
                extras={"error": repr(exc)},
            )
            return None, err

    async def _safe_read_broker_open_orders(self, symbol: str, *, stage: str) -> tuple[list[Any], Optional[str]]:
        symbol_u = str(symbol or "").strip().upper()
        try:
            orders = await self._broker_snapshots.read_open_orders(symbols=[symbol_u])
            return list(orders or []), None
        except Exception as exc:
            err = type(exc).__name__
            self._diag_event(
                node="broker_open_orders",
                status="error",
                symbol=symbol_u,
                reason=f"{stage}:snapshot_read_error:{err}",
                extras={"error": repr(exc)},
            )
            return [], err

    async def _probe_ib_open_orders_health(self) -> tuple[bool, Optional[str]]:
        ib = self._resolve_ib()
        if ib is None or not hasattr(ib, "openTrades"):
            self._diag_event(node="ibkr_open_orders_probe", status="missing_ib", reason="ib_handle_missing")
            return False, "missing_ib"
        try:
            is_connected = getattr(ib, "isConnected", None)
            if callable(is_connected) and (not bool(is_connected())):
                self._diag_event(node="ibkr_open_orders_probe", status="disconnected", reason="ib_not_connected")
                return False, "ib_not_connected"
        except Exception:
            self._diag_event(node="ibkr_open_orders_probe", status="error", reason="ib_connected_check_failed")
            return False, "ib_connected_check_failed"
        try:
            _ = list(ib.openTrades() or [])
            self._diag_event(node="ibkr_open_orders_probe", status="ok", reason="open_orders_probe_ok")
            return True, None
        except Exception as exc:
            err = type(exc).__name__
            self._diag_event(
                node="ibkr_open_orders_probe",
                status="error",
                reason=err,
                extras={"error": repr(exc)},
            )
            return False, err

    @staticmethod
    def _extract_owner_strategy_id(reasons: tuple[str, ...] | list[str]) -> Optional[str]:
        for item in list(reasons or []):
            text = str(item or "").strip()
            if text.startswith("owned_by:"):
                owner = text.split(":", 1)[1].strip()
                return owner or None
        return None

    @staticmethod
    def _extract_submit_status(submit_result: Any) -> tuple[str, list[str]]:
        status = ""
        reasons: list[str] = []
        allowed_statuses = {"APPROVED", "REJECTED", "DEFERRED", "ERROR", "UNKNOWN"}
        try:
            if submit_result is None:
                return status, reasons
            if isinstance(submit_result, dict):
                status_raw = str(submit_result.get("status") or "").strip().upper()
                raw_reasons = submit_result.get("reasons")
            else:
                status_raw = str(getattr(submit_result, "status", "") or "").strip().upper()
                raw_reasons = getattr(submit_result, "reasons", None)
            status = status_raw if status_raw in allowed_statuses else ""
            if isinstance(raw_reasons, list):
                reasons = [str(x) for x in raw_reasons if str(x).strip()]
        except Exception:
            status = ""
            reasons = []
        return status, reasons
