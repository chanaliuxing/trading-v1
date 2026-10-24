from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.strategy import PositionSnapshot, StrategyAlgorithm, StrategyParams, make_decision_id
from services.exec.protection_engine import ProtectionSpec

from strategies.index_super_win_components import FeatureDrivenSignalModel, InstrumentMap, SignalDecision
from strategies.index_super_win_replica_gate import evaluate_replica_entry_gate
from strategies.index_super_win_manifest import ReplicaManifest
from strategies.index_super_win_replica_core import ReplicaEntryCore
from strategies.index_super_win_replica_ownership import StockOwnershipLedger
from strategies.index_super_win_replica_ownership_types import ReplicaExitFillRecord, ReplicaOwnershipIntentRecord
from strategies.index_super_win_replica_types import ReplicaTradeState
from strategies.qqq_intraday_entry_logic import build_probe_add_plan, check_day_gross_notional_cap
from strategies.qqq_intraday_execution_logic import (
    action_submit_signature,
    should_submit_action,
    should_submit_stop,
    signed_position_qty,
    stop_submit_signature,
    submit_outcome,
    submit_outcome_with_stage,
)
from strategies.qqq_intraday_entry_block_logic import (
    block_stage_domain,
    blocked_preview_payload,
    gate_evaluated_payload,
    normalize_gate_details,
)
from strategies.qqq_intraday_feature_context_logic import apply_feature_context
from strategies.qqq_intraday_gate_audit_logic import (
    entry_block_payload as build_entry_block_payload,
    signal_block_reason as normalize_signal_block_reason,
    trader_reason_code as map_trader_reason_code,
)
from strategies.qqq_intraday_market_state_logic import (
    history_value as market_history_value,
    live_value_at_or_before as market_live_value_at_or_before,
    live_samples as market_live_samples,
    minute_history_map as market_minute_history_map,
    session_pos_day_from_history as market_session_pos_day_from_history,
    upsert_history_row as market_upsert_history_row,
    upsert_live_sample as market_upsert_live_sample,
)
from strategies.qqq_intraday_order_shell_logic import (
    cancel_id_from_order,
    matches_any_namespace_prefix,
    order_ref as get_order_ref,
    order_strategy_id as get_order_strategy_id,
    order_symbol as get_order_symbol,
    terminal_order_state,
)
from strategies.qqq_intraday_runtime_shell_logic import (
    build_runtime_snapshot_payload,
    ensure_audit_context as ensure_runtime_audit_context,
    remember_audit_context as remember_runtime_audit_context,
    resolve_audit_ids,
    restore_runtime_extras,
    restore_runtime_state_row,
    runtime_state_snapshot_row,
)
from strategies.qqq_intraday_protection_logic import (
    protection_audit_state,
    protection_thresholds,
    should_kill_switch,
    should_wait_for_self_heal,
    stop_audit_startup_grace_until,
)
from strategies.qqq_intraday_protection_payload_logic import (
    kill_switch_order_payload,
    protection_order_payload,
    protection_stop_rebuilt_payload,
)
from strategies.qqq_intraday_sync_logic import (
    evaluate_dual_conflict,
    evaluate_pending_decision_sync,
    is_sync_quarantined,
    sync_quarantine_reason,
    sync_reason_or_default,
)
from strategies.qqq_intraday_time_logic import (
    coerce_datetime as coerce_feature_datetime,
    feature_now_et as resolve_feature_now_et,
    parse_hms_tuple as parse_feature_hms_tuple,
)
from strategies.qqq_intraday_submit_preview_logic import preview_row, preview_row_from_outcome
from strategies.qqq_intraday_session_gate_logic import previous_session_gate, session_window_gate
from strategies.qqq_intraday_v47_logic import (
    V47GateConfig,
    drift_block_reason as evaluate_v47_drift_block_reason,
    early_override_ready as evaluate_v47_early_override_ready,
    opening_gate_reason as evaluate_v47_opening_gate_reason,
    veto_reason as evaluate_v47_veto_reason,
)


logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


class IndexSuperWinReplicaStrategy(StrategyAlgorithm):
    """
    Single-process, multi-symbol container for the new replica path.

    Current scope:
    1. manifest-driven symbol loading
    2. per-symbol runtime container
    3. entry intent build + submit + entry order-update convergence

    Exit/protection slices will be layered in incrementally.
    """

    def __init__(self, ctx, params: StrategyParams):
        super().__init__(ctx, params)
        self._manifest: ReplicaManifest = ReplicaManifest(symbols=[])
        self._instrument_map = InstrumentMap([])
        self._signal_model = FeatureDrivenSignalModel()
        self._feature_bar_sec = 5
        self._dry_run = True
        self._extras: dict[str, Any] = {}
        self._entry_core: ReplicaEntryCore | None = None
        self._ownership_ledger = StockOwnershipLedger()
        self._symbol_state: dict[str, ReplicaTradeState] = {}
        self._exec_to_signal_symbol: dict[str, str] = {}
        self._last_preview_by_symbol: dict[str, dict[str, Any]] = {}
        self._local_audit_enabled = True
        self._audit_context_by_symbol: dict[str, dict[str, Any]] = {}
        self._runtime_snapshot_path: Path | None = None
        self._entry_broker_position_guard_enabled: bool = True
        self._entry_broker_position_guard_fail_closed: bool = False
        self._entry_broker_position_guard_min_abs: float = 1e-9
        self._last_open_orders_block_warn_et_by_symbol: dict[str, datetime] = {}
        self._last_open_orders_check_warn_et_by_symbol: dict[str, datetime] = {}
        self._stop_audit_task: asyncio.Task | None = None
        self._stop_audit_started: bool = False
        self._session_name: str = ""
        self._session_start_hms: tuple[int, int, int] | None = None
        self._session_end_hms: tuple[int, int, int] | None = None
        self._enforce_entry_session_window: bool = False
        self._require_prev_session_flat: bool = False
        self._prev_session_strategy_id: str | None = None
        self._prev_session_runtime_snapshot_path: Path | None = None
        self._prev_session_order_namespace: str | None = None

    async def initialize(self) -> None:
        extras = dict(self.params.extras or {})
        self._extras = extras
        self._manifest = ReplicaManifest.from_params(extras)
        self._instrument_map = InstrumentMap.from_params(extras)
        self._signal_model = FeatureDrivenSignalModel(
            default_notional_usd=float(extras.get("notional_usd", 0.0) or 0.0)
        )
        self._feature_bar_sec = int(extras.get("feature_bar_sec", 5) or 5)
        self._dry_run = bool(extras.get("dry_run", True))
        self._local_audit_enabled = bool(extras.get("enable_local_audit_bridge", True))
        self._entry_broker_position_guard_enabled = bool(extras.get("entry_broker_position_guard_enabled", True))
        self._entry_broker_position_guard_fail_closed = bool(extras.get("entry_broker_position_guard_fail_closed", False))
        self._entry_broker_position_guard_min_abs = max(
            1e-9,
            float(extras.get("entry_broker_position_guard_min_abs", 1e-9) or 1e-9),
        )
        self._session_name = str(extras.get("session_name") or "").strip().lower()
        session_start_raw = str(extras.get("session_start_et") or "").strip()
        session_end_raw = str(extras.get("session_end_et") or "").strip()
        self._session_start_hms = self._parse_hms_tuple(session_start_raw, fallback=(0, 0, 0)) if session_start_raw else None
        self._session_end_hms = self._parse_hms_tuple(session_end_raw, fallback=(23, 59, 59)) if session_end_raw else None
        self._enforce_entry_session_window = bool(extras.get("enable_entry_during_session_only", bool(self._session_name)))
        self._require_prev_session_flat = bool(extras.get("require_prev_session_flat", False))
        prev_session_strategy_id = str(extras.get("prev_session_strategy_id") or "").strip()
        self._prev_session_strategy_id = prev_session_strategy_id or None
        prev_runtime_snapshot_raw = str(extras.get("prev_session_runtime_snapshot_path") or "").strip()
        self._prev_session_runtime_snapshot_path = Path(prev_runtime_snapshot_raw) if prev_runtime_snapshot_raw else None
        if self._prev_session_runtime_snapshot_path is not None and not self._prev_session_runtime_snapshot_path.is_absolute():
            self._prev_session_runtime_snapshot_path = Path.cwd() / self._prev_session_runtime_snapshot_path
        prev_order_namespace = str(extras.get("prev_session_order_namespace") or extras.get("order_namespace", "iswv2") or "").strip()
        self._prev_session_order_namespace = prev_order_namespace or None
        self._runtime_snapshot_path = self._resolve_runtime_snapshot_path(extras)
        self._entry_core = ReplicaEntryCore(
            strategy_id=self.strategy_id,
            order_namespace=str(extras.get("order_namespace", "iswv2") or "iswv2"),
            ttl_ms=int(getattr(self.params, "ttl_ms", 2000) or 2000),
            dlb_ms=int(extras.get("dlb_ms", 500) or 500),
        )
        for item in self._manifest.symbols:
            if not item.enabled:
                continue
            self._symbol_state[item.signal_symbol] = ReplicaTradeState(
                signal_symbol=item.signal_symbol,
                exec_symbol_long=str(item.long_exec_symbol or item.signal_symbol).strip().upper(),
                exec_symbol_short=str(item.short_exec_symbol or item.long_exec_symbol or item.signal_symbol).strip().upper(),
            )
            self._exec_to_signal_symbol[str(item.long_exec_symbol or item.signal_symbol).strip().upper()] = item.signal_symbol
            self._exec_to_signal_symbol[str(item.short_exec_symbol or item.long_exec_symbol or item.signal_symbol).strip().upper()] = item.signal_symbol
        self._subscribe_manifest_symbols_to_price_cache()
        symbols = self._instrument_map.signal_symbols()
        if symbols:
            await self.ctx.subscribe_features(symbols, bar_sec=self._feature_bar_sec)
        await self.ctx.set_cadence(int(getattr(self.params, "cadence_s", 5) or 5))
        self._restore_ownership_ledger()
        self._restore_replica_runtime()
        self.state["replica_manifest"] = self._manifest.as_dict()
        self.state["symbols"] = list(symbols)
        self.state["dry_run"] = self._dry_run
        self.state["local_audit_bridge_enabled"] = self._local_audit_enabled
        self._refresh_ownership_snapshot()
        self._persist_runtime_snapshot()
        self._emit_local_audit(stage="strategy_started", domain="system", status="started", reason="initialize_begin")
        await self._ensure_restart_live_protection()
        self._persist_runtime_snapshot()
        self._emit_local_audit(stage="strategy_ready", domain="system", status="ready", reason="initialize_complete")
        logger.info(
            "index_super_win_replica_initialized",
            strategy_id=self.strategy_id,
            symbols=symbols,
            dry_run=self._dry_run,
        )

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        assert self._entry_core is not None
        symbol_u = str(symbol or "").strip().upper()
        binding = self._instrument_map.binding_for_signal(symbol_u)
        state = self._symbol_state.get(symbol_u)
        if binding is None or state is None:
            self._last_preview_by_symbol[symbol_u] = {"status": "UNBOUND_SYMBOL"}
            return
        eval_now = self._feature_now_et(features)
        features = self._normalize_binding_freshness_features(features=features, binding=binding, eval_now=eval_now)
        self._update_primary_close_history(state=state, binding=binding, features=features, eval_now=eval_now)
        decision = self._derive_signal_decision(binding=binding, state=state, features=features, eval_now=eval_now)
        self._capture_feature_context(state, decision, features)
        quarantine_reason = sync_quarantine_reason(state.extras)
        if quarantine_reason:
            self._record_entry_block(
                symbol=symbol_u,
                exec_symbol=state.active_exec_symbol or decision.exec_symbol,
                side=state.active_side or decision.side,
                qty=int(max(0, state.extras.get("live_qty") or decision.target_qty or 0)),
                reason=quarantine_reason,
                trader_reason_code=self._trader_reason_code(quarantine_reason),
                gate_details={
                    "quarantined": True,
                    "exec_symbol": state.active_exec_symbol or decision.exec_symbol,
                },
                block_stage="sync_quarantined",
            )
            return
        if state.is_traded and not state.is_flattening:
            await self._manage_symbol_position(symbol_u=symbol_u, state=state, features=features, binding=binding)
            return
        session_gate = await self._evaluate_session_entry_gate(symbol_u=symbol_u, state=state, binding=binding, decision=decision, eval_now=eval_now)
        if session_gate is not None:
            return
        eval_second = eval_now.replace(microsecond=0)
        if state.extras.get("last_entry_eval_second") == eval_second:
            return
        state.extras["last_entry_eval_second"] = eval_second
        if bool(self._extras.get("entry_check_on_new_minute_only", False)):
            eval_minute = eval_now.replace(second=0, microsecond=0)
            if state.extras.get("last_entry_eval_minute") == eval_minute:
                return
            state.extras["last_entry_eval_minute"] = eval_minute
        cooldown_until = self._coerce_datetime(state.extras.get("entry_timeout_block_until_et"))
        if cooldown_until is not None and eval_now >= cooldown_until:
            self._clear_entry_timeout_cooldown(state)
            cooldown_until = None
        if cooldown_until is not None and eval_now < cooldown_until:
            warn_interval_sec = max(1.0, self._extra_float("entry_timeout_block_warn_interval_sec", 15.0))
            last_warn_at = self._coerce_datetime(state.extras.get("last_entry_timeout_block_warn_et"))
            if last_warn_at is None or (eval_now - last_warn_at).total_seconds() >= warn_interval_sec:
                state.extras["last_entry_timeout_block_warn_et"] = eval_now
                self._emit_local_audit(
                    stage="entry_timeout_cooldown_blocked",
                    domain="recon",
                    severity="warning",
                    status="blocked",
                    reason="entry_timeout_cooldown",
                    symbol=symbol_u,
                    payload={
                        "exec_symbol": state.active_exec_symbol or decision.exec_symbol,
                        "cooldown_until_et": cooldown_until.isoformat(),
                        "warn_interval_sec": warn_interval_sec,
                    },
                )
            self._last_preview_by_symbol[symbol_u] = {
                "status": "ENTRY_TIMEOUT_COOLDOWN_BLOCKED",
                "reason": "entry_timeout_cooldown",
                "exec_symbol": state.active_exec_symbol or decision.exec_symbol,
                "cooldown_until_et": cooldown_until.isoformat(),
            }
            return
        if state.pending_decision_id is not None:
            if await self._maybe_clear_stale_pending_decision(symbol_u=symbol_u, state=state):
                self._persist_runtime_snapshot()
                cooldown_until = self._coerce_datetime(state.extras.get("entry_timeout_block_until_et"))
                if cooldown_until is not None and eval_now < cooldown_until:
                    warn_interval_sec = max(1.0, self._extra_float("entry_timeout_block_warn_interval_sec", 15.0))
                    last_warn_at = self._coerce_datetime(state.extras.get("last_entry_timeout_block_warn_et"))
                    if last_warn_at is None or (eval_now - last_warn_at).total_seconds() >= warn_interval_sec:
                        state.extras["last_entry_timeout_block_warn_et"] = eval_now
                        self._emit_local_audit(
                            stage="entry_timeout_cooldown_blocked",
                            domain="recon",
                            severity="warning",
                            status="blocked",
                            reason="entry_timeout_cooldown",
                            symbol=symbol_u,
                            payload={
                                "exec_symbol": state.active_exec_symbol or decision.exec_symbol,
                                "cooldown_until_et": cooldown_until.isoformat(),
                                "warn_interval_sec": warn_interval_sec,
                            },
                        )
                    self._last_preview_by_symbol[symbol_u] = {
                        "status": "ENTRY_TIMEOUT_COOLDOWN_BLOCKED",
                        "reason": "entry_timeout_cooldown",
                        "exec_symbol": state.active_exec_symbol or decision.exec_symbol,
                        "cooldown_until_et": cooldown_until.isoformat(),
                    }
                    return
        if state.pending_decision_id is not None:
            self._emit_gate_evaluated(
                symbol=symbol_u,
                exec_symbol=state.active_exec_symbol or decision.exec_symbol,
                side=decision.side,
                qty=int(max(0, decision.target_qty)),
                status="blocked",
                reason="pending_decision_inflight",
                gate_allowed=False,
                block_stage="pending_decision_blocked",
            )
            self._last_preview_by_symbol[symbol_u] = {
                "status": "PENDING_DECISION_BLOCKED",
                "reason": "pending_decision_inflight",
                "exec_symbol": state.active_exec_symbol or decision.exec_symbol,
            }
            return
        if (not decision.signal_active or decision.side is None or decision.target_qty <= 0) and await self._maybe_sweep_flat_orphan_orders(symbol_u=symbol_u, state=state, eval_now=eval_now):
            self._persist_runtime_snapshot()
        has_active_open_orders = await self._has_active_strategy_open_orders(state=state)
        if has_active_open_orders is None:
            await self._maybe_warn_open_orders_check_failed(symbol_u=symbol_u, state=state, eval_now=eval_now)
        elif has_active_open_orders:
            self._emit_gate_evaluated(
                symbol=symbol_u,
                exec_symbol=state.active_exec_symbol or decision.exec_symbol,
                side=decision.side,
                qty=int(max(0, decision.target_qty)),
                status="blocked",
                reason="symbol_open_orders_inflight",
                gate_allowed=False,
                block_stage="open_orders_blocked",
            )
            await self._maybe_warn_open_orders_blocked(symbol_u=symbol_u, state=state, eval_now=eval_now)
            self._last_preview_by_symbol[symbol_u] = {
                "status": "OPEN_ORDERS_BLOCKED",
                "reason": "symbol_open_orders_inflight",
                "exec_symbol": state.active_exec_symbol or decision.exec_symbol,
            }
            return
        real_position_synced = await self._sync_from_real_exec_positions(symbol_u=symbol_u, state=state)
        if real_position_synced:
            sync_reason = sync_reason_or_default(state.extras, default="sync_from_real_position")
            self._emit_gate_evaluated(
                symbol=symbol_u,
                exec_symbol=state.active_exec_symbol,
                side=state.active_side or decision.side,
                qty=int(max(0, self._optional_float(state.extras.get("live_qty")) or 0)),
                status="blocked",
                reason=sync_reason,
                gate_allowed=False,
                block_stage="real_position_sync_blocked_entry",
            )
            self._last_preview_by_symbol[symbol_u] = {
                "status": "REAL_POSITION_QUARANTINED" if sync_reason != "sync_from_real_position" else "REAL_POSITION_BLOCKED",
                "reason": sync_reason,
                "exec_symbol": state.active_exec_symbol,
                "live_qty": int(max(0, self._optional_float(state.extras.get("live_qty")) or 0)),
            }
            self._persist_runtime_snapshot()
            return
        gate = evaluate_replica_entry_gate(features=features, extras=self._gate_extras_for_binding(binding))
        if not gate.allowed:
            trader_reason_code = self._trader_reason_code(gate.reason)
            gate_details = dict(gate.details or {})
            self._record_entry_block(
                symbol=symbol_u,
                exec_symbol=str(decision.exec_symbol or "").strip().upper() or None,
                side=decision.side,
                qty=int(max(0, decision.target_qty)),
                reason=str(gate.reason or "").strip() or "gate_blocked",
                trader_reason_code=trader_reason_code,
                gate_details=gate_details,
                block_stage="entry_gate_blocked",
            )
            return
        if not decision.signal_active or decision.side is None or decision.target_qty <= 0:
            signal_block_reason = self._signal_block_reason(decision)
            trader_reason_code = self._trader_reason_code(signal_block_reason)
            self._record_entry_block(
                symbol=symbol_u,
                exec_symbol=str(decision.exec_symbol or "").strip().upper() or None,
                side=decision.side,
                qty=int(max(0, decision.target_qty)),
                reason=signal_block_reason,
                trader_reason_code=trader_reason_code,
                gate_details=self._signal_gate_details(decision),
                block_stage="entry_gate_blocked",
            )
            return
        total_qty = int(max(0, decision.target_qty))
        decision_source = str((getattr(decision, "metadata", {}) or {}).get("decision_source") or "").strip().lower()
        use_probe_add_split = decision_source == "qqq_style_fallback"
        if use_probe_add_split:
            probe_add_plan = build_probe_add_plan(
                qty_total=total_qty,
                probe_ratio=self._extra_float("probe_ratio", 0.70),
            )
            probe_qty, add_qty = probe_add_plan.probe_qty, probe_add_plan.add_qty
        else:
            probe_qty, add_qty = total_qty, 0
        if probe_qty <= 0:
            trader_reason_code = self._trader_reason_code("missing_exec_price_for_sizing")
            self._record_entry_block(
                symbol=symbol_u,
                exec_symbol=str(decision.exec_symbol or "").strip().upper() or None,
                side=decision.side,
                qty=0,
                reason="missing_exec_price_for_sizing",
                trader_reason_code=trader_reason_code,
                gate_details=self._signal_gate_details(decision),
                block_stage="entry_gate_blocked",
            )
            return
        day_cap_allowed, day_cap_payload = await self._entry_day_gross_notional_allowed(
            state=state,
            probe_qty=probe_qty,
            exec_symbol=str(decision.exec_symbol or "").strip().upper(),
            exec_price=self._optional_float(getattr(decision, "exec_price", None)),
        )
        if not day_cap_allowed:
            self._record_entry_block(
                symbol=symbol_u,
                exec_symbol=str(decision.exec_symbol or "").strip().upper() or None,
                side=decision.side,
                qty=probe_qty,
                reason="day_gross_notional_cap",
                trader_reason_code="DAY_GROSS_NOTIONAL_CAP",
                gate_details=day_cap_payload,
                block_stage="entry_gate_blocked",
            )
            return

        conflict_allowed, conflict_reason, conflict_payload = await self._entry_broker_position_guard(
            symbol_u=symbol_u,
            exec_symbol=str(decision.exec_symbol or "").strip().upper(),
        )
        if not conflict_allowed:
            trader_reason_code = self._trader_reason_code(conflict_reason)
            gate_details = dict(conflict_payload or {})
            self._record_entry_block(
                symbol=symbol_u,
                exec_symbol=str(decision.exec_symbol or "").strip().upper() or None,
                side=decision.side,
                qty=int(max(0, decision.target_qty)),
                reason=str(conflict_reason or "").strip() or "ownership_conflict",
                trader_reason_code=trader_reason_code,
                gate_details=gate_details,
                block_stage="entry_gate_blocked",
            )
            return
        stop_reentry_cooldown_min = max(0.0, self._extra_float("stop_reentry_cooldown_min", 0.0))
        last_stop_exit_at = self._coerce_datetime(state.extras.get("last_stop_exit_at"))
        last_stop_exit_side = str(state.extras.get("last_stop_exit_side") or "").strip().upper()
        entry_side = str(decision.side or "").strip().upper()
        if (
            stop_reentry_cooldown_min > 0.0
            and last_stop_exit_at is not None
            and last_stop_exit_side == entry_side
            and (eval_now - last_stop_exit_at) < _dt.timedelta(minutes=stop_reentry_cooldown_min)
        ):
            self._record_entry_block(
                symbol=symbol_u,
                exec_symbol=str(decision.exec_symbol or "").strip().upper() or None,
                side=entry_side,
                qty=int(max(0, decision.target_qty)),
                reason="stop_reentry_cooldown",
                trader_reason_code="STOP_REENTRY_COOLDOWN",
                gate_details={
                    "side": entry_side,
                    "cooldown_min": stop_reentry_cooldown_min,
                    "last_stop_exit_at": last_stop_exit_at.isoformat(),
                },
                block_stage="entry_blocked_reentry_cooldown",
            )
            return

        state, intent, envelope = self._entry_core.build_entry_intent(
            state=state,
            side=decision.side,
            qty=probe_qty,
            reason=decision.reason,
            trigger_id=None,
            ref_px=decision.signal_price,
            ref_px_source="signal_price" if decision.signal_price is not None else None,
            now=datetime.now(ET),
        )
        state.extras["pending_decision_since_et"] = datetime.now(ET)
        state.extras["pending_decision_last_probe_et"] = None
        state.extras["pending_decision_flat_probe_count"] = 0
        state.extras["planned_total_qty"] = int(total_qty)
        state.extras["planned_probe_qty"] = int(probe_qty)
        state.extras["planned_add_qty"] = int(add_qty)
        state.extras["add_done"] = int(add_qty) <= 0
        state.extras["last_add_check_minute"] = None
        state.extras["add_deadline_et"] = (eval_now + _dt.timedelta(minutes=2)) if int(add_qty) > 0 else None
        position_lot_id = str(state.extras.get("active_position_lot_id") or f"lot:{self.strategy_id}:{symbol_u}:{intent.decision_id}")
        state.extras["active_position_lot_id"] = position_lot_id
        self._ownership_ledger.record_entry_intent(
            ReplicaOwnershipIntentRecord(
                strategy_id=self.strategy_id,
                decision_id=intent.decision_id,
                trigger_id=intent.trigger_id,
                signal_symbol=symbol_u,
                exec_symbol=intent.symbol,
                side=intent.side,
                qty=float(intent.qty),
                ts_bar_end_us=int(getattr(envelope, "ts_bar_end_us", 0) or 0),
                position_lot_id=position_lot_id,
                order_ref=str(intent.order_ref or ""),
                payload={
                    "reason": intent.reason,
                    "ref_px": intent.ref_px,
                    "ref_px_source": intent.ref_px_source,
                },
            )
        )
        self._symbol_state[symbol_u] = state
        self._refresh_ownership_snapshot()
        audit_ctx = self._remember_audit_context(
            symbol=symbol_u,
            decision_id=str(getattr(envelope, "decision_id", "") or "").strip() or None,
            trigger_id=intent.trigger_id,
            trace_id=intent.trace_id,
            order_ref=intent.order_ref,
        )
        self._emit_local_audit(
            stage="gate_evaluated",
            status="entry_allowed",
            reason="entry_allowed",
            symbol=intent.symbol,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload={
                "signal_symbol": symbol_u,
                "exec_symbol": intent.symbol,
                "side": intent.side,
                "qty": intent.qty,
                "qty_total": int(total_qty),
                "probe_qty": int(probe_qty),
                "add_qty": int(add_qty),
                "gate_details": dict(gate.details or {}),
            },
        )
        self._emit_local_audit(
            stage="entry_submit_ready",
            status="ready",
            reason="entry_prechecks_passed",
            symbol=intent.symbol,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload={
                "signal_symbol": symbol_u,
                "exec_symbol": intent.symbol,
                "side": intent.side,
                "qty_total": int(total_qty),
                "probe_qty": int(probe_qty),
                "add_qty": int(add_qty),
                "ref_px": intent.ref_px,
                "ref_px_source": intent.ref_px_source,
                "order_ref": intent.order_ref,
            },
        )
        self._emit_local_audit(
            stage="entry_intent_requested",
            status="requested",
            reason=intent.reason,
            symbol=intent.symbol,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload={
                "signal_symbol": symbol_u,
                "exec_symbol": intent.symbol,
                "order_ref": intent.order_ref,
                "side": intent.side,
                "qty": intent.qty,
                "qty_total": int(total_qty),
                "probe_qty": int(probe_qty),
                "add_qty": int(add_qty),
                "kind": "ENTRY",
                "ref_px": intent.ref_px,
                "ref_px_source": intent.ref_px_source,
            },
        )

        if self._dry_run:
            self._last_preview_by_symbol[symbol_u] = {
                "status": "DRY_RUN_PREVIEW",
                "exec_symbol": intent.symbol,
                "order_ref": intent.order_ref,
                "side": intent.side,
                "qty": intent.qty,
            }
            self._emit_local_audit(
                stage="entry_intent_preview",
                status="preview",
                reason=intent.reason,
                symbol=symbol_u,
                decision_id=audit_ctx.get("decision_id"),
                trigger_id=audit_ctx.get("trigger_id"),
                trace_id=audit_ctx.get("trace_id"),
                payload={"exec_symbol": intent.symbol, "order_ref": intent.order_ref, "side": intent.side, "qty": intent.qty},
            )
            return

        submit_result = await self.ctx.submit_intent(envelope)
        state, submit_decision = self._entry_core.handle_submit_result(state=state, submit_result=submit_result)
        self._symbol_state[symbol_u] = state
        self._last_preview_by_symbol[symbol_u] = {
            "status": submit_decision.event.upper(),
            "reason": submit_decision.reason,
            "runtime_transition": submit_decision.runtime_transition,
            "exec_symbol": intent.symbol,
            "order_ref": intent.order_ref,
            "side": intent.side,
            "qty": intent.qty,
        }
        self._emit_local_audit(
            stage=submit_decision.event,
            severity="warning" if not submit_decision.accepted else "info",
            status="submitted" if submit_decision.accepted else "rejected",
            reason=submit_decision.reason,
            symbol=symbol_u,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload={"exec_symbol": intent.symbol, "order_ref": intent.order_ref, "side": intent.side, "qty": intent.qty, "runtime_transition": submit_decision.runtime_transition},
        )
        self._persist_runtime_snapshot()

    async def on_order_update(self, update) -> None:
        assert self._entry_core is not None
        update_strategy_id = str(getattr(update, "strategy_id", "") or "").strip()
        if update_strategy_id and update_strategy_id != self.strategy_id:
            return
        symbol_u = str(getattr(update, "symbol", "") or "").strip().upper()
        signal_symbol = self._exec_to_signal_symbol.get(symbol_u, symbol_u)
        state = self._symbol_state.get(signal_symbol)
        if state is None:
            return
        if not update_strategy_id and not self._is_owned_order_update(update=update, state=state):
            self._emit_local_audit(
                stage="order_update_ignored",
                domain="recon",
                severity="info",
                status="ignored",
                reason="foreign_empty_strategy_id",
                symbol=signal_symbol,
                payload={
                    "exec_symbol": symbol_u or None,
                    "order_ref": str(getattr(update, "order_ref", "") or "").strip() or None,
                    "order_state": str(getattr(update, "state", "") or "").strip() or None,
                },
            )
            return
        pre_update_live_qty = int(max(0, self._optional_float(state.extras.get("live_qty")) or state.expected_entry_qty or 0))
        state, decision = self._entry_core.handle_entry_order_update(state=state, update=update)
        if (not decision.accepted) and decision.reason in {"foreign_order_ref", "non_entry_order_ref", "missing_order_ref"}:
            tp1_qty_hint = int(max(0, self._optional_float(state.extras.get("pending_tp1_qty")) or 0))
            state, decision = self._entry_core.handle_exit_order_update(
                state=state,
                update=update,
                tp1_qty_hint=tp1_qty_hint,
            )
        self._symbol_state[signal_symbol] = state
        order_ref = str(getattr(update, "order_ref", "") or "").strip()
        broker_order_id = str(getattr(update, "broker_order_id", "") or "").strip()
        perm_id = getattr(update, "ib_perm_id", None)
        order_lot = self._ownership_ledger.lot_for_identity(
            order_ref=order_ref or None,
            broker_order_id=broker_order_id or None,
            perm_id=perm_id,
        )
        active_lot_id = str(state.extras.get("active_position_lot_id") or "").strip()
        attach_lot_id = order_lot.position_lot_id if order_lot is not None else active_lot_id
        if attach_lot_id:
            self._ownership_ledger.attach_order_identity(
                position_lot_id=attach_lot_id,
                order_ref=order_ref or None,
                broker_order_id=broker_order_id or None,
                perm_id=perm_id,
            )
            order_lot = self._ownership_ledger.lot_by_id(attach_lot_id)
        audit_ctx = self._ensure_audit_context(
            symbol=signal_symbol,
            trigger_id=state.active_trigger_id,
            order_ref=order_ref or None,
        )
        self._last_preview_by_symbol[signal_symbol] = {
            "status": decision.event.upper(),
            "reason": decision.reason,
            "runtime_transition": decision.runtime_transition,
            "exec_symbol": state.active_exec_symbol,
        }
        self._emit_local_audit(
            stage=decision.event,
            severity="warning" if "terminal" in decision.event else "info",
            status="accepted" if decision.accepted else "ignored",
            reason=decision.reason,
            symbol=signal_symbol,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload={
                "exec_symbol": state.active_exec_symbol,
                "order_ref": order_ref or None,
                "runtime_transition": decision.runtime_transition,
                "order_state": str(getattr(update, "state", "") or "").strip() or None,
            },
        )
        if decision.event == "entry_confirmed":
            self._clear_pending_probe_state(state)
            self._clear_entry_timeout_cooldown(state)
            state.extras["live_qty"] = int(max(0, state.expected_entry_qty))
            if order_lot is not None:
                self._ownership_ledger.mark_entry_live(
                    position_lot_id=order_lot.position_lot_id,
                    filled_qty=float(order_lot.planned_qty or int(max(0, state.expected_entry_qty))),
                )
                if str(order_lot.payload.get("kind") or "").strip().upper() != "ADD":
                    state.extras["active_position_lot_id"] = order_lot.position_lot_id
                self._emit_local_audit(
                    stage="ownership_entry_lot_live",
                    domain="recon",
                    status="live",
                    reason="entry_confirmed",
                    symbol=signal_symbol,
                    decision_id=audit_ctx.get("decision_id"),
                    trigger_id=audit_ctx.get("trigger_id"),
                    trace_id=audit_ctx.get("trace_id"),
                    payload={
                        "exec_symbol": state.active_exec_symbol,
                        "position_lot_id": order_lot.position_lot_id,
                        "lot_kind": str(order_lot.payload.get("kind") or "ENTRY"),
                        "lot_live_qty": float(order_lot.planned_qty or int(max(0, state.expected_entry_qty))),
                        "order_ref": order_ref or None,
                    },
                )
            state.extras["protection_active"] = False
            state.extras["round_live_confirmed"] = False
            if state.entry_px is None:
                state.entry_px = self._optional_float(state.extras.get("entry_ref_px"))
            if state.extras.get("entry_time_et") is None:
                state.extras["entry_time_et"] = datetime.now(ET)
            self._ensure_stop_audit_task_running()
        elif decision.event == "entry_terminal":
            self._clear_pending_probe_state(state)
        elif decision.event == "protective_stop_live":
            state.extras["protection_active"] = True
            state.extras["protection_pending"] = False
            state.extras["kill_switch_pending"] = False
            if ":TRAIL" in str(order_ref or "").upper():
                state.extras["native_trail_active"] = True
                state.extras["native_trail_pending"] = False
        elif decision.event == "protective_stop_terminal":
            if ":TRAIL" in str(order_ref or "").upper():
                was_native_trail_active = bool(state.extras.get("native_trail_active"))
                state.extras["native_trail_pending"] = False
                state.extras["native_trail_active"] = False
                state.extras["protection_pending"] = False
                if was_native_trail_active:
                    state.extras["protection_active"] = False
            else:
                state.extras["protection_active"] = False
                state.extras["protection_pending"] = False
        elif decision.event == "tp1_exit_confirmed":
            tp1_exit_qty = int(max(0, self._optional_float(state.extras.get("pending_tp1_qty")) or 0))
            if tp1_exit_qty > 0:
                exec_symbol_u = str(state.active_exec_symbol or symbol_u).strip().upper()
                owned_qty_before = self._ownership_ledger.active_qty(
                    strategy_id=self.strategy_id,
                    signal_symbol=signal_symbol,
                    exec_symbol=exec_symbol_u,
                )
                allocated_qty = self._ownership_ledger.allocate_exit_fill(
                    strategy_id=self.strategy_id,
                    signal_symbol=signal_symbol,
                    exec_symbol=exec_symbol_u,
                    qty=float(tp1_exit_qty),
                    reason="tp1",
                    ts=datetime.now(ET),
                    order_ref=order_ref,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                )
                owned_qty_after = self._ownership_ledger.active_qty(
                    strategy_id=self.strategy_id,
                    signal_symbol=signal_symbol,
                    exec_symbol=exec_symbol_u,
                )
                self._emit_local_audit(
                    stage="ownership_exit_allocated",
                    domain="recon",
                    status="allocated",
                    reason="tp1",
                    symbol=signal_symbol,
                    decision_id=audit_ctx.get("decision_id"),
                    trigger_id=audit_ctx.get("trigger_id"),
                    trace_id=audit_ctx.get("trace_id"),
                    payload={
                        "exec_symbol": exec_symbol_u,
                        "requested_qty": float(tp1_exit_qty),
                        "allocated_qty": float(allocated_qty),
                        "owned_qty_before": float(owned_qty_before),
                        "owned_qty_after": float(owned_qty_after),
                        "order_ref": order_ref or None,
                    },
                )
            state.extras["pending_tp1_qty"] = 0
            remaining_live_qty = int(max(0, self._optional_float(state.extras.get("live_qty")) or 0))
            if remaining_live_qty > 0:
                await self._submit_native_runner_trail_after_tp1(
                    symbol_u=signal_symbol,
                    state=state,
                    live_qty=remaining_live_qty,
                )
        elif decision.event in {"flatten_confirmed", "protective_stop_filled"}:
            exit_reason = "protective_stop" if decision.event == "protective_stop_filled" else "flatten"
            live_exit_qty = int(max(0, pre_update_live_qty))
            if live_exit_qty > 0:
                exec_symbol_u = str(state.active_exec_symbol or symbol_u).strip().upper()
                owned_qty_before = self._ownership_ledger.active_qty(
                    strategy_id=self.strategy_id,
                    signal_symbol=signal_symbol,
                    exec_symbol=exec_symbol_u,
                )
                allocated_qty = self._ownership_ledger.allocate_exit_fill(
                    strategy_id=self.strategy_id,
                    signal_symbol=signal_symbol,
                    exec_symbol=exec_symbol_u,
                    qty=float(live_exit_qty),
                    reason=exit_reason,
                    ts=datetime.now(ET),
                    order_ref=order_ref,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                )
                owned_qty_after = self._ownership_ledger.active_qty(
                    strategy_id=self.strategy_id,
                    signal_symbol=signal_symbol,
                    exec_symbol=exec_symbol_u,
                )
                self._emit_local_audit(
                    stage="ownership_exit_allocated",
                    domain="recon",
                    status="allocated",
                    reason=exit_reason,
                    symbol=signal_symbol,
                    decision_id=audit_ctx.get("decision_id"),
                    trigger_id=audit_ctx.get("trigger_id"),
                    trace_id=audit_ctx.get("trace_id"),
                    payload={
                        "exec_symbol": exec_symbol_u,
                        "requested_qty": float(live_exit_qty),
                        "allocated_qty": float(allocated_qty),
                        "owned_qty_before": float(owned_qty_before),
                        "owned_qty_after": float(owned_qty_after),
                        "order_ref": order_ref or None,
                    },
                )
            state.extras["pending_tp1_qty"] = 0
            state.extras["kill_switch_pending"] = False
            self._clear_entry_timeout_cooldown(state)
            if decision.event == "protective_stop_filled":
                state.extras["last_stop_exit_at"] = datetime.now(ET)
                state.extras["last_stop_exit_side"] = str(state.active_side or "").strip().upper() or None
        elif decision.event == "flatten_terminal":
            state.extras["kill_switch_pending"] = False
        self._refresh_ownership_snapshot()
        self._persist_runtime_snapshot()

    async def on_position_update(self, position: PositionSnapshot) -> None:
        position_strategy_id = str(getattr(position, "strategy_id", "") or "").strip()
        if not position_strategy_id:
            symbol_u = str(getattr(position, "symbol", "") or "").strip().upper()
            signal_symbol = self._exec_to_signal_symbol.get(symbol_u, symbol_u)
            if signal_symbol in self._symbol_state:
                self._emit_local_audit(
                    stage="position_update_ignored",
                    domain="recon",
                    severity="warning",
                    status="ignored",
                    reason="missing_strategy_id",
                    symbol=signal_symbol,
                    payload={"exec_symbol": symbol_u or None},
                )
            return
        if position_strategy_id != self.strategy_id:
            return
        symbol_u = str(getattr(position, "symbol", "") or "").strip().upper()
        signal_symbol = self._exec_to_signal_symbol.get(symbol_u, symbol_u)
        state = self._symbol_state.get(signal_symbol)
        if state is None:
            return
        qty_signed = self._optional_float(getattr(position, "pos_qty", None)) or 0.0
        avg_px = self._optional_float(getattr(position, "avg_px", None))
        live_qty = int(max(0.0, abs(qty_signed)))
        inferred_side = "SHORT" if symbol_u == str(state.exec_symbol_short or "").strip().upper() else "LONG"
        active_lot_id = self._bind_or_recover_live_lot(
            signal_symbol=signal_symbol,
            state=state,
            exec_symbol=symbol_u,
            side=inferred_side,
            live_qty=live_qty,
            source="position_update",
        )
        state.extras["live_qty"] = live_qty
        if avg_px is not None and avg_px > 0:
            state.entry_px = avg_px
        if live_qty <= 0:
            if not self._should_commit_flat_sync(state=state):
                self._last_preview_by_symbol[signal_symbol] = {
                    "status": "POSITION_FLAT_PENDING_CONFIRM",
                    "exec_symbol": state.active_exec_symbol,
                    "live_qty": 0,
                    "confirm_count": int(max(0, int(state.extras.get("zero_position_flat_confirm_count") or 0))),
                }
                self._emit_local_audit(
                    stage="position_flat_pending_confirm",
                    domain="recon",
                    severity="warning",
                    status="pending",
                    reason="broker_position_zero_pending_confirm",
                    symbol=signal_symbol,
                    payload={
                        "exec_symbol": state.active_exec_symbol,
                        "confirm_count": int(max(0, int(state.extras.get("zero_position_flat_confirm_count") or 0))),
                        "confirm_need": max(1, self._extra_int("zero_position_flat_confirmations", 2)),
                    },
                )
                self._persist_runtime_snapshot()
                return
            self._clear_zero_position_pending(state)
            closed_owned_qty = self._ownership_ledger.close_active_lots(
                strategy_id=self.strategy_id,
                signal_symbol=signal_symbol,
                reason="position_sync_flat",
                ts=datetime.now(ET),
            )
            cancelled = await self._cancel_strategy_open_orders_best_effort(
                state=state,
                reason="position_reconciled_flat",
            )
            success_incremented = False
            session_end_flatten_pending = bool(state.extras.get("session_end_flatten_pending", False))
            session_end_flatten_reason = str(state.extras.get("session_end_flatten_reason") or "broker_position_zero")
            if bool(state.extras.get("round_live_confirmed", False)):
                max_trades_per_day = max(1, self._extra_int("max_trades_per_day", 3))
                success_count = int(max(0, int(state.extras.get("day_success_count") or 0)))
                success_count = min(success_count + 1, max_trades_per_day)
                state.extras["day_success_count"] = success_count
                success_incremented = True
                self._emit_local_audit(
                    stage="trade_round_completed",
                    domain="recon",
                    status="completed",
                    reason="position_reconciled_flat",
                    symbol=signal_symbol,
                    payload={
                        "exec_symbol": state.active_exec_symbol,
                        "success_count": success_count,
                        "max_trades_per_day": max_trades_per_day,
                    },
                )
            if session_end_flatten_pending:
                self._emit_local_audit(
                    stage="session_end_flatten_confirmed",
                    domain="recon",
                    status="confirmed",
                    reason=session_end_flatten_reason,
                    symbol=signal_symbol,
                    payload=self._session_handover_payload(
                        symbol_u=signal_symbol,
                        state=state,
                        extra={
                            "closed_owned_qty": closed_owned_qty,
                            "cancelled_orders": cancelled,
                        },
                    ),
                )
            self._clear_trade_runtime_state(state)
            self._last_preview_by_symbol[signal_symbol] = {
                "status": "POSITION_FLAT_SYNCED",
                "exec_symbol": state.active_exec_symbol,
                "live_qty": 0,
                "closed_owned_qty": closed_owned_qty,
                "cancelled_orders": cancelled,
                "success_incremented": success_incremented,
                "success_count": int(max(0, int(state.extras.get("day_success_count") or 0))),
            }
            self._emit_local_audit(
                stage="position_flat_synced",
                domain="recon",
                status="flat",
                reason="broker_position_zero",
                symbol=signal_symbol,
                payload={
                    "exec_symbol": state.active_exec_symbol,
                    "live_qty": 0,
                    "closed_owned_qty": closed_owned_qty,
                    "cancelled_orders": cancelled,
                    "success_count": int(max(0, int(state.extras.get("day_success_count") or 0))),
                },
            )
            self._refresh_ownership_snapshot()
            self._persist_runtime_snapshot()
            return
        self._clear_zero_position_pending(state)
        if qty_signed < -1e-9:
            await self._handle_unexpected_short_inventory(
                signal_symbol=signal_symbol,
                state=state,
                symbol_u=symbol_u,
                qty_signed=qty_signed,
                live_qty=live_qty,
            )
            return
        if is_sync_quarantined(state.extras):
            self._last_preview_by_symbol[signal_symbol] = {
                "status": "POSITION_SYNC_QUARANTINED",
                "reason": sync_quarantine_reason(state.extras),
                "exec_symbol": symbol_u,
                "live_qty": live_qty,
            }
            self._persist_runtime_snapshot()
            return
        state.is_traded = True
        state.pending_decision_id = None
        self._clear_pending_probe_state(state)
        self._clear_entry_timeout_cooldown(state)
        state.extras["round_live_confirmed"] = True
        if state.active_trigger_id is None:
            state.active_trigger_id = make_decision_id()
        state.active_exec_symbol = symbol_u
        state.active_side = inferred_side
        if active_lot_id:
            state.extras["active_position_lot_id"] = active_lot_id
        if state.extras.get("entry_time_et") is None:
            state.extras["entry_time_et"] = datetime.now(ET)
        self._normalize_live_trade_runtime(state)
        if state.stop_px is None or float(state.stop_px or 0.0) <= 0.0:
            await self._heal_missing_protection(
                symbol_u=signal_symbol,
                state=state,
                live_qty=live_qty,
                reason="position_reconcile_protection",
                status_prefix="AUDIT_SELF_HEAL_STOP",
            )
            return
        if not bool(state.extras.get("protection_active", False)):
            await self._heal_missing_protection(
                symbol_u=signal_symbol,
                state=state,
                live_qty=live_qty,
                reason="position_reconcile_protection",
                status_prefix="AUDIT_SELF_HEAL_STOP",
            )
            return
        self._last_preview_by_symbol[signal_symbol] = {
            "status": "POSITION_SYNCED",
            "exec_symbol": state.active_exec_symbol,
            "live_qty": live_qty,
            "stop_px": state.stop_px,
        }
        self._emit_local_audit(
            stage="position_synced",
            domain="recon",
            status="live",
            reason="broker_position_sync",
            symbol=signal_symbol,
            payload={
                "exec_symbol": state.active_exec_symbol,
                "live_qty": live_qty,
                "stop_px": state.stop_px,
                "position_lot_id": active_lot_id,
            },
        )
        self._refresh_ownership_snapshot()
        self._persist_runtime_snapshot()

    async def _handle_unexpected_short_inventory(
        self,
        *,
        signal_symbol: str,
        state: ReplicaTradeState,
        symbol_u: str,
        qty_signed: float,
        live_qty: int,
    ) -> None:
        assert self._entry_core is not None
        state.is_traded = True
        state.active_exec_symbol = symbol_u
        state.active_side = "SHORT" if symbol_u == str(state.exec_symbol_short or "").strip().upper() else "LONG"
        throttle_sec = max(2.0, self._extra_float("unexpected_short_action_interval_sec", 10.0))
        now_mono = asyncio.get_running_loop().time()
        last_action = float(state.extras.get("last_unexpected_short_action_mono") or 0.0)
        if (now_mono - last_action) < throttle_sec:
            self._last_preview_by_symbol[signal_symbol] = {
                "status": "UNEXPECTED_SHORT_THROTTLED",
                "reason": "unexpected_short_inventory",
                "exec_symbol": symbol_u,
                "live_qty": live_qty,
                "qty_signed": qty_signed,
            }
            self._emit_local_audit(
                stage="unexpected_short_throttled",
                domain="recon",
                severity="warning",
                status="throttled",
                reason="unexpected_short_inventory",
                symbol=signal_symbol,
                payload={"exec_symbol": symbol_u, "live_qty": live_qty, "qty_signed": qty_signed, "throttle_sec": throttle_sec},
            )
            return
        state.extras["last_unexpected_short_action_mono"] = now_mono
        state, intent, envelope = self._entry_core.build_flatten_intent(
            state=state,
            qty_signed=float(qty_signed),
            reason="unexpected_short_inventory",
            now=datetime.now(ET),
        )
        if self._dry_run:
            state.is_flattening = True
            self._last_preview_by_symbol[signal_symbol] = preview_row(
                status="UNEXPECTED_SHORT_FLATTEN_PREVIEW",
                reason="unexpected_short_inventory",
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra={"qty": intent.target_qty, "live_qty": live_qty},
            )
            self._emit_local_audit(
                stage="unexpected_short_flatten_preview",
                domain="recon",
                severity="warning",
                status="preview",
                reason="unexpected_short_inventory",
                symbol=signal_symbol,
                payload={"exec_symbol": intent.symbol, "order_ref": intent.order_ref, "qty": intent.target_qty, "live_qty": live_qty},
            )
            self._persist_runtime_snapshot()
            return
        submit_result = await self.ctx.submit_intent(envelope)
        status, _ = self._entry_core.extract_submit_status(submit_result)
        outcome = submit_outcome(
            status=status,
            ok_status="UNEXPECTED_SHORT_FLATTEN_SUBMITTED",
            rejected_status="UNEXPECTED_SHORT_FLATTEN_REJECTED",
            ok_reason="unexpected_short_inventory",
        )
        state.is_flattening = outcome.approved
        self._last_preview_by_symbol[signal_symbol] = preview_row_from_outcome(
            outcome=outcome,
            exec_symbol=intent.symbol,
            order_ref=intent.order_ref,
            extra={"qty": intent.target_qty, "live_qty": live_qty},
        )
        self._emit_local_audit(
            stage=outcome.audit_stage,
            domain="recon",
            severity="warning",
            status=outcome.audit_status,
            reason=outcome.reason,
            symbol=signal_symbol,
            payload={"exec_symbol": intent.symbol, "order_ref": intent.order_ref, "qty": intent.target_qty, "live_qty": live_qty},
        )
        self._persist_runtime_snapshot()

    async def on_shutdown(self, reason: str) -> None:
        self.state["shutdown_reason"] = reason
        self._emit_local_audit(stage="strategy_shutdown_started", domain="system", status="shutdown", reason=reason)
        if self._stop_audit_task is not None:
            self._stop_audit_task.cancel()
            try:
                await self._stop_audit_task
            except asyncio.CancelledError:
                pass
        self.state["replica_runtime"] = {
            symbol: runtime_state_snapshot_row(state)
            for symbol, state in self._symbol_state.items()
        }
        self._persist_runtime_snapshot()
        logger.info("index_super_win_replica_shutdown", strategy_id=self.strategy_id, reason=reason)

    async def audit_live_protection(self) -> None:
        for symbol_u, state in self._symbol_state.items():
            live_qty = int(max(0, self._optional_float(state.extras.get("live_qty")) or state.expected_entry_qty or 0))
            if not state.is_traded or state.is_flattening or live_qty <= 0:
                continue
            prev_miss_count = int(max(0, int(state.extras.get("protection_audit_miss_count") or 0)))
            has_live_stop = await self._has_live_protective_stop_order(symbol_u=symbol_u, state=state)
            audit_state = protection_audit_state(
                prev_miss_count=prev_miss_count,
                has_live_stop=has_live_stop,
                protection_active=bool(state.extras.get("protection_active", False)),
                protection_pending=bool(state.extras.get("protection_pending", False)),
            )
            state.extras["protection_active"] = audit_state.protection_active
            state.extras["protection_pending"] = audit_state.protection_pending
            state.extras["protection_audit_miss_count"] = int(audit_state.miss_count)
            if audit_state.recovered:
                self._emit_local_audit(
                    stage="protection_audit_recovered",
                    status="recovered",
                    reason="broker_stop_visible_again",
                    symbol=symbol_u,
                    payload={"exec_symbol": state.active_exec_symbol, "previous_miss_count": prev_miss_count},
                )
            miss_count = int(audit_state.miss_count)
            thresholds = protection_thresholds(
                self_heal_after=self._extra_int("protection_audit_self_heal_after_misses", 3),
                kill_switch_after=self._extra_int("protection_audit_kill_switch_after_misses", 8),
            )
            self_heal_threshold = int(thresholds.self_heal_threshold)
            kill_switch_threshold = int(thresholds.kill_switch_threshold)
            if has_live_stop is False:
                self._emit_local_audit(
                    stage="protection_audit_warning",
                    severity="warning",
                    status="missing",
                    reason="broker_stop_missing",
                    symbol=symbol_u,
                    payload={
                        "exec_symbol": state.active_exec_symbol,
                        "miss_count": miss_count,
                        "self_heal_threshold": self_heal_threshold,
                        "kill_switch_threshold": kill_switch_threshold,
                    },
                )
            if state.stop_px is None or float(state.stop_px or 0.0) <= 0.0:
                self._last_preview_by_symbol[symbol_u] = {
                    "status": "PROTECTION_AUDIT_STOP_MISSING",
                    "reason": "audit_missing_stop_px",
                    "exec_symbol": state.active_exec_symbol,
                    "live_qty": live_qty,
                    "miss_count": miss_count,
                }
                self._emit_local_audit(
                    stage="protection_audit_stop_missing",
                    severity="warning",
                    status="missing",
                    reason="audit_missing_stop_px",
                    symbol=symbol_u,
                    payload={"exec_symbol": state.active_exec_symbol, "live_qty": live_qty, "miss_count": miss_count},
                )
                if has_live_stop is False and not should_kill_switch(miss_count=miss_count, kill_switch_threshold=kill_switch_threshold):
                    continue
                rebuilt_stop_px = self._rebuild_stop_px_from_runtime_state(symbol_u=symbol_u, state=state)
                if rebuilt_stop_px is not None and rebuilt_stop_px > 0:
                    self._emit_local_audit(
                        stage="protection_audit_self_heal_stop_rebuilt",
                        domain="recon",
                        severity="warning",
                        status="rebuilt",
                        reason="audit_missing_stop_px",
                        symbol=symbol_u,
                        payload={
                            "exec_symbol": state.active_exec_symbol,
                            "entry_px": state.entry_px,
                            "entry_atr_used": state.entry_atr_used,
                            "stop_px": rebuilt_stop_px,
                            "stop_dist_pre": state.extras.get("stop_dist_pre"),
                            "miss_count": miss_count,
                        },
                    )
                    self._persist_runtime_snapshot()
                    await self._heal_missing_protection(
                        symbol_u=symbol_u,
                        state=state,
                        live_qty=live_qty,
                        reason="audit_missing_stop_px",
                        status_prefix="PROTECTION_AUDIT_SELF_HEAL",
                    )
                else:
                    await self._kill_switch_unprotected_live(
                        symbol_u=symbol_u,
                        state=state,
                        live_qty=live_qty,
                        reason="audit_missing_stop_px",
                        status_prefix="PROTECTION_AUDIT",
                    )
                continue
            if bool(state.extras.get("protection_active", False)) and has_live_stop is not False:
                self._last_preview_by_symbol[symbol_u] = {
                    "status": "PROTECTION_AUDIT_STOP_LIVE",
                    "reason": "stop_active",
                    "exec_symbol": state.active_exec_symbol,
                    "live_qty": live_qty,
                    "stop_px": state.stop_px,
                    "audit_source": "broker_open_orders" if has_live_stop is True else "runtime_flag",
                }
                self._emit_local_audit(
                    stage="protection_audit_stop_live",
                    status="live",
                    reason="stop_active",
                    symbol=symbol_u,
                    payload={"exec_symbol": state.active_exec_symbol, "live_qty": live_qty, "stop_px": state.stop_px, "audit_source": "broker_open_orders" if has_live_stop is True else "runtime_flag"},
                )
                continue
            self._last_preview_by_symbol[symbol_u] = {
                "status": "PROTECTION_AUDIT_SELF_HEAL_ATTEMPTED",
                "reason": "stop_inactive",
                "exec_symbol": state.active_exec_symbol,
                "live_qty": live_qty,
                "stop_px": state.stop_px,
                "miss_count": miss_count,
            }
            self._emit_local_audit(
                stage="protection_audit_self_heal_attempted",
                status="healing",
                reason="audit_stop_inactive",
                symbol=symbol_u,
                payload={"exec_symbol": state.active_exec_symbol, "live_qty": live_qty, "stop_px": state.stop_px, "miss_count": miss_count},
            )
            if has_live_stop is False and should_wait_for_self_heal(miss_count=miss_count, self_heal_threshold=self_heal_threshold):
                continue
            if has_live_stop is False and should_kill_switch(miss_count=miss_count, kill_switch_threshold=kill_switch_threshold):
                await self._kill_switch_unprotected_live(symbol_u=symbol_u, state=state, live_qty=live_qty, reason="audit_stop_inactive_kill_switch", status_prefix="PROTECTION_AUDIT")
                continue
            self._emit_local_audit(
                stage="protection_audit_self_heal",
                severity="warning",
                status="threshold_met",
                reason="audit_stop_inactive",
                symbol=symbol_u,
                payload={
                    "exec_symbol": state.active_exec_symbol,
                    "live_qty": live_qty,
                    "stop_px": state.stop_px,
                    "miss_count": miss_count,
                    "self_heal_threshold": self_heal_threshold,
                },
            )
            await self._heal_missing_protection(
                symbol_u=symbol_u,
                state=state,
                live_qty=live_qty,
                reason="audit_stop_inactive",
                status_prefix="PROTECTION_AUDIT_SELF_HEAL",
            )

    async def _manage_symbol_position(self, *, symbol_u: str, state: ReplicaTradeState, features: Dict[str, Any], binding: Any | None = None) -> None:
        assert self._entry_core is not None
        live_qty = int(max(0, self._optional_float(state.extras.get("live_qty")) or state.expected_entry_qty or 0))
        qty_signed = float(live_qty if str(state.active_side or "").upper() != "SHORT" else -live_qty)
        exec_symbol_u = str(state.active_exec_symbol or state.exec_symbol_for_side(state.active_side)).strip().upper()
        last_px = self._exec_price_from_features_or_cache(exec_symbol=exec_symbol_u, features=features) or self._feature_price(features)
        if live_qty <= 0 or last_px is None:
            self._last_preview_by_symbol[symbol_u] = {
                "status": "LIVE_HOLD",
                "reason": "missing_live_qty_or_price",
                "exec_symbol": state.active_exec_symbol,
                "live_qty": live_qty,
                "last_px": last_px,
            }
            return

        primary_close_now = self._binding_feature_float(features, binding=binding, suffix="close_now", legacy_keys=("qqq_close_now", "signal_price"))
        primary_vwap_now = self._primary_vwap_value(binding=binding, features=features, eval_now=self._feature_now_et(features))
        primary_age_sec = self._binding_feature_float(features, binding=binding, suffix="age_sec", legacy_keys=("qqq_age_sec",))
        state, manage = self._entry_core.evaluate_position_management(
            state=state,
            now=datetime.now(ET),
            last_px=last_px,
            live_qty=live_qty,
            primary_px=primary_close_now or last_px,
            primary_vwap=primary_vwap_now,
            primary_age_sec=primary_age_sec,
            atr_live=self._optional_float(features.get("atr_live")) or self._optional_float(features.get("atr")),
            hard_stop_delay_min=self._extra_float("hard_stop_delay_min", 3.0),
            enable_hard_stop=bool(self._extras.get("enable_hard_stop", True)),
            enable_exchange_stop_check=bool(self._extras.get("enable_exchange_stop_check", False)),
            enable_breakeven_shield=bool(self._extras.get("enable_breakeven_shield", False)),
            enable_dynamic_stop_widen=bool(self._extras.get("enable_dynamic_stop_widen", False)),
            enable_logic_retreat=bool(self._extras.get("enable_logic_retreat", True)),
            logic_retreat_min_hold_sec=self._extra_float("logic_retreat_min_hold_sec", 420.0),
            logic_retreat_max_primary_age_sec=self._extra_float("logic_retreat_max_primary_age_sec", self._extra_float("logic_retreat_max_qqq_age_sec", 15.0)),
            enable_add_on=bool(self._extras.get("enable_add_on", False)),
            eod_flatten_time_hhmm=self._parse_hms_tuple(self._extras.get("force_flatten_time", "15:59:00"), fallback=(15, 59, 0)),
        )
        self._symbol_state[symbol_u] = state

        if manage.updated_stop_px is not None:
            state.stop_px = manage.updated_stop_px
        if manage.updated_entry_px is not None:
            state.entry_px = manage.updated_entry_px

        if manage.action == "noop":
            self._last_preview_by_symbol[symbol_u] = {
                "status": "LIVE_HOLD",
                "reason": manage.reason,
                "exec_symbol": state.active_exec_symbol,
                "live_qty": live_qty,
                "last_px": last_px,
            }
            return

        if manage.action == "add_position":
            state, intent, envelope = self._entry_core.build_entry_intent(
                state=state,
                side=str(state.active_side or "LONG"),
                qty=int(manage.add_qty),
                reason=manage.reason,
                trigger_id=state.active_trigger_id,
                kind="ADD",
                lock_state=False,
                ref_px=last_px,
                ref_px_source="exec_price",
                now=datetime.now(ET),
            )
            add_lot_id = f"lot:{self.strategy_id}:{symbol_u}:{intent.decision_id}"
            self._ownership_ledger.record_entry_intent(
                ReplicaOwnershipIntentRecord(
                    strategy_id=self.strategy_id,
                    decision_id=intent.decision_id,
                    trigger_id=intent.trigger_id,
                    signal_symbol=symbol_u,
                    exec_symbol=intent.symbol,
                    side=intent.side,
                    qty=float(intent.qty),
                    ts_bar_end_us=int(getattr(envelope, "ts_bar_end_us", 0) or 0),
                    position_lot_id=add_lot_id,
                    order_ref=str(intent.order_ref or ""),
                    payload={
                        "reason": intent.reason,
                        "ref_px": intent.ref_px,
                        "ref_px_source": intent.ref_px_source,
                        "kind": "ADD",
                    },
                )
            )
            if manage.updated_entry_px is not None:
                state.entry_px = manage.updated_entry_px
            self._refresh_ownership_snapshot()
            if self._dry_run:
                self._last_preview_by_symbol[symbol_u] = preview_row(
                    status="ADD_POSITION_PREVIEW",
                    reason=manage.reason,
                    exec_symbol=intent.symbol,
                    order_ref=intent.order_ref,
                    extra={
                        "qty": intent.qty if intent.side == "LONG" else -intent.qty,
                        "updated_entry_px": manage.updated_entry_px,
                    },
                )
                return
            submit_result = await self.ctx.submit_intent(envelope)
            status, _ = self._entry_core.extract_submit_status(submit_result)
            outcome = submit_outcome(
                status=status,
                ok_status="ADD_POSITION_SUBMITTED",
                rejected_status="ADD_POSITION_REJECTED",
                ok_reason=manage.reason,
            )
            if outcome.approved:
                state.expected_entry_qty = int(max(0, live_qty + int(manage.add_qty)))
                state.extras["add_done"] = True
            self._emit_local_audit(
                stage=outcome.audit_stage,
                status=outcome.audit_status,
                severity=outcome.audit_severity,
                reason=outcome.reason,
                symbol=intent.symbol,
                decision_id=intent.decision_id,
                trigger_id=state.active_trigger_id,
                trace_id=intent.trace_id,
                payload={
                    "signal_symbol": symbol_u,
                    "exec_symbol": intent.symbol,
                    "order_ref": intent.order_ref,
                    "side": intent.side,
                    "qty": intent.qty,
                    "updated_entry_px": manage.updated_entry_px,
                },
            )
            self._last_preview_by_symbol[symbol_u] = preview_row_from_outcome(
                outcome=outcome,
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra={
                    "qty": intent.qty if intent.side == "LONG" else -intent.qty,
                    "updated_entry_px": manage.updated_entry_px,
                },
            )
            return

        if manage.action == "tp1_exit":
            state, intent, envelope = self._entry_core.build_tp1_partial_exit_intent(
                state=state,
                qty_signed=qty_signed,
                qty_to_exit=int(manage.tp1_qty),
                reason=manage.reason,
                now=datetime.now(ET),
            )
            if self._dry_run:
                self._last_preview_by_symbol[symbol_u] = preview_row(
                    status="TP1_PARTIAL_PREVIEW",
                    reason=manage.reason,
                    exec_symbol=intent.symbol,
                    order_ref=intent.order_ref,
                    extra={"qty": intent.target_qty},
                )
                return
            submit_result = await self.ctx.submit_intent(envelope)
            status, _ = self._entry_core.extract_submit_status(submit_result)
            outcome = submit_outcome_with_stage(
                status=status,
                ok_status="TP1_PARTIAL_SUBMITTED",
                rejected_status="TP1_PARTIAL_REJECTED",
                ok_reason=manage.reason,
                ok_audit_stage="tp1_submitted",
                rejected_audit_stage="tp1_rejected",
            )
            state.extras["pending_tp1_qty"] = int(manage.tp1_qty) if outcome.approved else 0
            self._emit_local_audit(
                stage=outcome.audit_stage,
                status=outcome.audit_status,
                severity=outcome.audit_severity,
                reason=outcome.reason,
                symbol=intent.symbol,
                decision_id=intent.decision_id,
                trigger_id=state.active_trigger_id,
                trace_id=intent.trace_id,
                payload={
                    "signal_symbol": symbol_u,
                    "exec_symbol": intent.symbol,
                    "order_ref": intent.order_ref,
                    "qty": intent.target_qty,
                    "tp1_qty": int(manage.tp1_qty),
                },
            )
            self._last_preview_by_symbol[symbol_u] = preview_row_from_outcome(
                outcome=outcome,
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra={"qty": intent.target_qty},
            )
            return

        if manage.action == "move_stop":
            state, intent, envelope = self._entry_core.build_protective_stop_intent(
                state=state,
                qty_signed=qty_signed,
                reason=manage.reason,
                stop_px=float(manage.updated_stop_px or state.stop_px or last_px),
                now=datetime.now(ET),
            )
            if self._dry_run:
                self._last_preview_by_symbol[symbol_u] = preview_row(
                    status="PROTECTIVE_STOP_PREVIEW",
                    reason=manage.reason,
                    exec_symbol=intent.symbol,
                    order_ref=intent.order_ref,
                    extra={"stop_px": intent.stop_px},
                )
                return
            submit_result = await self.ctx.submit_intent(envelope)
            status, _ = self._entry_core.extract_submit_status(submit_result)
            outcome = submit_outcome(
                status=status,
                ok_status="PROTECTIVE_STOP_SUBMITTED",
                rejected_status="PROTECTIVE_STOP_REJECTED",
                ok_reason=manage.reason,
            )
            self._emit_local_audit(
                stage=outcome.audit_stage,
                status=outcome.audit_status,
                severity=outcome.audit_severity,
                reason=outcome.reason,
                symbol=intent.symbol,
                decision_id=intent.decision_id,
                trigger_id=state.active_trigger_id,
                trace_id=intent.trace_id,
                payload={
                    "signal_symbol": symbol_u,
                    "exec_symbol": intent.symbol,
                    "order_ref": intent.order_ref,
                    "stop_px": intent.stop_px,
                    "qty": abs(int(qty_signed)),
                },
            )
            self._last_preview_by_symbol[symbol_u] = preview_row_from_outcome(
                outcome=outcome,
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra={"stop_px": intent.stop_px},
            )
            return

        if manage.action == "flatten":
            qty_signed = signed_position_qty(live_qty=live_qty, side=state.active_side)
            if self._skip_duplicate_session_flatten_action(
                symbol_u=symbol_u,
                state=state,
                live_qty=live_qty,
                reason=manage.reason,
            ):
                return
            state, intent, envelope = self._entry_core.build_flatten_intent(
                state=state,
                qty_signed=qty_signed,
                reason=manage.reason,
                now=datetime.now(ET),
            )
            if self._dry_run:
                if self._is_session_end_flatten_reason(manage.reason):
                    self._emit_local_audit(
                        stage="session_end_flatten_requested",
                        domain="recon",
                        status="preview",
                        reason=manage.reason,
                        symbol=symbol_u,
                        decision_id=intent.decision_id,
                        trigger_id=state.active_trigger_id,
                        trace_id=intent.trace_id,
                        payload=self._session_handover_payload(
                            symbol_u=symbol_u,
                            state=state,
                            extra={"order_ref": intent.order_ref, "qty": intent.target_qty},
                        ),
                    )
                self._last_preview_by_symbol[symbol_u] = preview_row(
                    status="FLATTEN_PREVIEW",
                    reason=manage.reason,
                    exec_symbol=intent.symbol,
                    order_ref=intent.order_ref,
                    extra={"qty": intent.target_qty},
                )
                return
            submit_result = await self.ctx.submit_intent(envelope)
            status, _ = self._entry_core.extract_submit_status(submit_result)
            outcome = submit_outcome(
                status=status,
                ok_status="FLATTEN_SUBMITTED",
                rejected_status="FLATTEN_REJECTED",
                ok_reason=manage.reason,
            )
            if self._is_session_end_flatten_reason(manage.reason):
                if outcome.approved:
                    state.extras["session_end_flatten_pending"] = True
                    state.extras["session_end_flatten_reason"] = manage.reason
                    state.extras["session_end_flatten_submit_sig"] = action_submit_signature(
                        exec_symbol=str(intent.symbol or state.active_exec_symbol or ""),
                        qty=int(max(0, live_qty)),
                        action="SESSION_END_FLATTEN",
                    )
                    state.extras["session_end_flatten_submit_mono"] = self._loop_time()
                self._emit_local_audit(
                    stage="session_end_flatten_requested",
                    domain="recon",
                    severity=outcome.audit_severity,
                    status="submitted" if outcome.approved else "rejected",
                    reason=outcome.reason,
                    symbol=symbol_u,
                    decision_id=intent.decision_id,
                    trigger_id=state.active_trigger_id,
                    trace_id=intent.trace_id,
                    payload=self._session_handover_payload(
                        symbol_u=symbol_u,
                        state=state,
                        extra={"order_ref": intent.order_ref, "qty": intent.target_qty},
                    ),
                )
            self._emit_local_audit(
                stage=outcome.audit_stage,
                status=outcome.audit_status,
                severity=outcome.audit_severity,
                reason=outcome.reason,
                symbol=intent.symbol,
                decision_id=intent.decision_id,
                trigger_id=state.active_trigger_id,
                trace_id=intent.trace_id,
                payload={
                    "signal_symbol": symbol_u,
                    "exec_symbol": intent.symbol,
                    "order_ref": intent.order_ref,
                    "qty": intent.target_qty,
                },
            )
            self._last_preview_by_symbol[symbol_u] = preview_row_from_outcome(
                outcome=outcome,
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra={"qty": intent.target_qty},
            )
            return

        self._last_preview_by_symbol[symbol_u] = {
            "status": "LIVE_HOLD",
            "reason": manage.reason,
            "exec_symbol": state.active_exec_symbol,
            "live_qty": live_qty,
            "last_px": last_px,
        }

    def _gate_extras_for_binding(self, binding: Any) -> Dict[str, Any]:
        extras = dict(self._extras or {})
        signal_symbol = str(getattr(binding, "signal_symbol", "") or "").strip().upper()
        confirm_symbol = str(getattr(binding, "confirm_symbol", "") or "").strip().upper()
        if signal_symbol:
            extras["gate_primary_symbol"] = signal_symbol
            extras["gate_primary_feature_prefix"] = signal_symbol.lower().replace("-", "_")
        if confirm_symbol:
            extras["gate_confirm_symbol"] = confirm_symbol
            extras["gate_confirm_feature_prefix"] = confirm_symbol.lower().replace("-", "_")
        return extras

    def _binding_feature_float(
        self,
        features: Dict[str, Any],
        *,
        binding: Any | None,
        suffix: str,
        legacy_keys: tuple[str, ...] = (),
        use_confirm_symbol: bool = False,
    ) -> float | None:
        symbol = ""
        if binding is not None:
            field = "confirm_symbol" if use_confirm_symbol else "signal_symbol"
            symbol = str(getattr(binding, field, "") or "").strip().upper()
        keys: list[str] = []
        if symbol:
            prefix = symbol.lower().replace("-", "_")
            keys.append(f"{prefix}_{suffix}")
        keys.extend(list(legacy_keys))
        for key in keys:
            value = self._optional_float(features.get(key))
            if value is not None:
                return value
        return None

    def _is_offhours_session(self) -> bool:
        return self._session_name in {"afterhours", "premarket", "overnight"}

    def _subscribe_manifest_symbols_to_price_cache(self) -> None:
        price_cache = getattr(self.ctx, "_price_cache", None)
        subscribe = getattr(price_cache, "subscribe", None)
        if not callable(subscribe):
            return
        symbols: list[str] = []
        for item in list(self._manifest.symbols or []):
            if not getattr(item, "enabled", True):
                continue
            for symbol in (
                getattr(item, "signal_symbol", None),
                getattr(item, "long_exec_symbol", None),
                getattr(item, "short_exec_symbol", None),
            ):
                symbol_u = str(symbol or "").strip().upper()
                if symbol_u and symbol_u not in symbols:
                    symbols.append(symbol_u)
        if not symbols:
            return
        try:
            subscribe(symbols)
        except Exception as exc:
            logger.warning("replica_price_cache_subscribe_failed", strategy_id=self.strategy_id, symbols=symbols, error=str(exc))

    @staticmethod
    def _is_rth_time(eval_now: datetime) -> bool:
        now_t = eval_now.timetz().replace(tzinfo=None)
        return _dt.time(9, 30) <= now_t <= _dt.time(16, 0)

    def _session_start_dt(self, eval_now: datetime) -> datetime | None:
        if self._session_start_hms is None:
            return None
        hour, minute, second = self._session_start_hms
        session_start = eval_now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if self._session_name == "overnight":
            start_t = _dt.time(hour, minute, second)
            now_t = eval_now.timetz().replace(tzinfo=None)
            if now_t < start_t:
                session_start -= _dt.timedelta(days=1)
        return session_start

    def _offhours_session_vwap_from_price_cache(self, *, symbol: str, eval_now: datetime) -> float | None:
        if not self._is_offhours_session() or self._is_rth_time(eval_now):
            return None
        session_start = self._session_start_dt(eval_now)
        if session_start is None:
            return None
        price_cache = getattr(self.ctx, "_price_cache", None)
        if price_cache is None:
            return None
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return None
        total_pv = 0.0
        total_volume = 0.0
        for bar in list(getattr(price_cache, "_bars_1m", {}).get(symbol_u, []) or []):
            bar_ts = getattr(bar, "ts", None)
            if not isinstance(bar_ts, datetime):
                continue
            bar_ts = bar_ts.replace(tzinfo=ET) if bar_ts.tzinfo is None else bar_ts.astimezone(ET)
            if bar_ts < session_start or bar_ts > eval_now:
                continue
            total_pv += float(getattr(bar, "pv", 0.0) or 0.0)
            total_volume += float(getattr(bar, "volume", 0.0) or 0.0)
        current_bar = getattr(price_cache, "_current_1m", {}).get(symbol_u)
        if current_bar is not None:
            bar_ts = getattr(current_bar, "ts", None)
            if isinstance(bar_ts, datetime):
                bar_ts = bar_ts.replace(tzinfo=ET) if bar_ts.tzinfo is None else bar_ts.astimezone(ET)
                if session_start <= bar_ts <= eval_now:
                    total_pv += float(getattr(current_bar, "pv", 0.0) or 0.0)
                    total_volume += float(getattr(current_bar, "volume", 0.0) or 0.0)
        if total_volume <= 0:
            return None
        return float(total_pv / total_volume)

    def _offhours_exec_atr_from_price_cache(self, *, exec_symbol: str, eval_now: datetime, period: int) -> float | None:
        if not self._is_offhours_session() or self._is_rth_time(eval_now):
            return None
        session_start = self._session_start_dt(eval_now)
        if session_start is None:
            return None
        price_cache = getattr(self.ctx, "_price_cache", None)
        if price_cache is None:
            return None
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return None

        bars: list[Any] = []
        for bar in list(getattr(price_cache, "_bars_5m", {}).get(symbol_u, []) or []):
            bar_ts = getattr(bar, "ts", None)
            if not isinstance(bar_ts, datetime):
                continue
            bar_ts = bar_ts.replace(tzinfo=ET) if bar_ts.tzinfo is None else bar_ts.astimezone(ET)
            if session_start <= bar_ts <= eval_now:
                bars.append(bar)
        current_bar = getattr(price_cache, "_current_5m", {}).get(symbol_u)
        if current_bar is not None:
            bar_ts = getattr(current_bar, "ts", None)
            if isinstance(bar_ts, datetime):
                bar_ts = bar_ts.replace(tzinfo=ET) if bar_ts.tzinfo is None else bar_ts.astimezone(ET)
                if session_start <= bar_ts <= eval_now:
                    bars.append(current_bar)
        if len(bars) < 2:
            return None

        bars.sort(key=lambda item: getattr(item, "ts"))
        true_ranges: list[float] = []
        prev_close: float | None = None
        for bar in bars:
            high_f = self._optional_float(getattr(bar, "high", None))
            low_f = self._optional_float(getattr(bar, "low", None))
            close_f = self._optional_float(getattr(bar, "close", None))
            if high_f is None or low_f is None or close_f is None:
                continue
            tr = float(high_f - low_f)
            if prev_close is not None:
                tr = max(tr, abs(float(high_f) - prev_close), abs(float(low_f) - prev_close))
            true_ranges.append(float(tr))
            prev_close = float(close_f)
        if not true_ranges:
            return None
        window = true_ranges[-max(1, int(period or 14)) :]
        if len(window) < min(3, max(1, int(period or 14))):
            return None
        return float(sum(window) / len(window))

    def _atr_effective_value(
        self,
        *,
        features: Dict[str, Any],
        exec_symbol: str | None,
        eval_now: datetime,
    ) -> float | None:
        atr_eff = self._optional_float(features.get("atr_eff"))
        if atr_eff is None or atr_eff <= 0:
            atr_eff = self._optional_float(features.get("entry_atr_used"))
        if atr_eff is None or atr_eff <= 0:
            atr_eff = self._optional_float(features.get("atr"))
        if atr_eff is not None and atr_eff > 0:
            return float(atr_eff)
        if not self._is_offhours_session() or self._is_rth_time(eval_now):
            return None
        return self._offhours_exec_atr_from_price_cache(
            exec_symbol=str(exec_symbol or "").strip().upper(),
            eval_now=eval_now,
            period=int(self._extras.get("atr_period", 14) or 14),
        )

    def _primary_vwap_value(self, *, binding: Any, features: Dict[str, Any], eval_now: datetime) -> float | None:
        primary_vwap = self._binding_feature_float(
            features,
            binding=binding,
            suffix="vwap_now",
            legacy_keys=("vwap_now", "qqq_vwap", "vwap"),
        )
        if primary_vwap is not None and primary_vwap > 0:
            return primary_vwap
        signal_symbol = str(getattr(binding, "signal_symbol", "") or "").strip().upper()
        return self._offhours_session_vwap_from_price_cache(symbol=signal_symbol, eval_now=eval_now)

    async def _entry_broker_position_guard(
        self,
        *,
        symbol_u: str,
        exec_symbol: str,
    ) -> tuple[bool, str, Dict[str, Any]]:
        if not self._entry_broker_position_guard_enabled:
            return True, "ok", {}
        exec_symbol_u = str(exec_symbol or "").strip().upper()
        if not exec_symbol_u:
            return True, "ok", {}
        qty, read_error = await self._read_ctx_position_qty(exec_symbol_u)
        if read_error is not None:
            if not self._entry_broker_position_guard_fail_closed:
                self._emit_local_audit(
                    stage="entry_position_guard_degraded",
                    severity="warning",
                    status="degraded",
                    reason="position_read_error_fail_open",
                    symbol=symbol_u,
                    payload={
                        "exec_symbol": exec_symbol_u,
                        "error": read_error,
                    },
                )
                return True, "ok", {}
            return False, "ownership_conflict", {
                "exec_symbol": exec_symbol_u,
                "position_guard": "read_error_fail_closed",
                "error": read_error,
            }
        qty_f = float(qty or 0.0)
        if abs(qty_f) < self._entry_broker_position_guard_min_abs:
            return True, "ok", {}
        return False, "ownership_conflict", {
            "exec_symbol": exec_symbol_u,
            "position_guard": "broker_position_nonflat",
            "broker_pos_qty": qty_f,
        }

    async def _read_ctx_position_qty(self, symbol_u: str) -> tuple[float, str | None]:
        position_reader = getattr(self.ctx, "position", None)
        if not callable(position_reader):
            return 0.0, "ctx_position_reader_missing"
        try:
            snap = await position_reader(symbol_u)
            qty = self._optional_float(getattr(snap, "pos_qty", None)) or 0.0
            return qty, None
        except Exception as exc:
            return 0.0, f"{type(exc).__name__}:{exc}"

    async def _has_active_strategy_open_orders(self, *, state: ReplicaTradeState) -> bool | None:
        open_orders = getattr(self.ctx, "open_orders", None)
        if not callable(open_orders):
            return None
        try:
            orders = await open_orders()
        except Exception:
            return None
        owned_symbols = {
            str(state.exec_symbol_long or "").strip().upper(),
            str(state.exec_symbol_short or "").strip().upper(),
            str(state.active_exec_symbol or "").strip().upper(),
        }
        owned_symbols.discard("")
        namespace_prefixes = {
            f"{self._entry_core.order_namespace}:{self.strategy_id}:{symbol}:"
            for symbol in owned_symbols
            if symbol
        }
        for order in list(orders or []):
            order_symbol = get_order_symbol(order)
            if order_symbol and order_symbol not in owned_symbols:
                continue
            order_strategy_id = get_order_strategy_id(order)
            order_ref = get_order_ref(order)
            owned_by_strategy = order_strategy_id == self.strategy_id
            owned_by_ref = matches_any_namespace_prefix(order_ref, namespace_prefixes)
            if not owned_by_strategy and not owned_by_ref:
                continue
            if not self._is_terminal_order_state(order):
                return True
        return False

    async def _maybe_warn_open_orders_blocked(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        eval_now: datetime,
    ) -> None:
        warn_interval_sec = max(10.0, self._extra_float("open_orders_block_warn_interval_sec", 60.0))
        last_warn_at = self._last_open_orders_block_warn_et_by_symbol.get(symbol_u)
        if last_warn_at is not None and (eval_now - last_warn_at).total_seconds() < warn_interval_sec:
            return
        self._last_open_orders_block_warn_et_by_symbol[symbol_u] = eval_now
        self._emit_local_audit(
            stage="open_orders_blocked_entry",
            domain="recon",
            severity="warning",
            status="blocked",
            reason="symbol_open_orders_inflight",
            symbol=symbol_u,
            payload={
                "exec_symbol": state.active_exec_symbol,
                "warn_interval_sec": warn_interval_sec,
            },
        )

    async def _maybe_warn_open_orders_check_failed(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        eval_now: datetime,
    ) -> None:
        warn_interval_sec = max(10.0, self._extra_float("open_orders_check_warn_interval_sec", 300.0))
        last_warn_at = self._last_open_orders_check_warn_et_by_symbol.get(symbol_u)
        if last_warn_at is not None and (eval_now - last_warn_at).total_seconds() < warn_interval_sec:
            return
        self._last_open_orders_check_warn_et_by_symbol[symbol_u] = eval_now
        self._emit_local_audit(
            stage="open_orders_check_failed",
            domain="recon",
            severity="warning",
            status="degraded",
            reason="open_orders_check_failed",
            symbol=symbol_u,
            payload={
                "exec_symbol": state.active_exec_symbol,
                "warn_interval_sec": warn_interval_sec,
            },
        )

    @staticmethod
    def _clear_pending_probe_state(state: ReplicaTradeState) -> None:
        state.extras["pending_decision_since_et"] = None
        state.extras["pending_decision_last_probe_et"] = None
        state.extras["pending_decision_flat_probe_count"] = 0

    @staticmethod
    def _clear_entry_timeout_cooldown(state: ReplicaTradeState) -> None:
        state.extras["entry_timeout_block_until_et"] = None
        state.extras["last_entry_timeout_block_warn_et"] = None

    async def _maybe_clear_stale_pending_decision(self, *, symbol_u: str, state: ReplicaTradeState) -> bool:
        pending_since = self._coerce_datetime(state.extras.get("pending_decision_since_et"))
        now_et = datetime.now(ET)
        entry_fill_timeout_sec = max(5.0, self._extra_float("entry_fill_timeout_sec", 60.0))
        entry_timeout_reentry_cooldown_sec = max(0.0, self._extra_float("entry_timeout_reentry_cooldown_sec", 75.0))
        min_hold_sec = max(3.0, self._extra_float("pending_decision_min_hold_sec", 8.0))
        probe_interval_sec = max(0.5, self._extra_float("pending_decision_probe_interval_sec", 1.0))
        probe_need = max(2, self._extra_int("pending_decision_flat_probe_need", 2))
        age_sec = (now_et - pending_since).total_seconds() if pending_since is not None else None
        qty_long, err_long = await self._read_ctx_position_qty(str(state.exec_symbol_long or "").strip().upper())
        qty_short, err_short = await self._read_ctx_position_qty(str(state.exec_symbol_short or "").strip().upper())
        broker_flat = True
        if err_long is None and abs(float(qty_long or 0.0)) > self._entry_broker_position_guard_min_abs:
            broker_flat = False
        if err_short is None and abs(float(qty_short or 0.0)) > self._entry_broker_position_guard_min_abs:
            broker_flat = False
        last_probe_at = self._coerce_datetime(state.extras.get("pending_decision_last_probe_et"))
        probe_interval_elapsed = last_probe_at is None or (now_et - last_probe_at).total_seconds() >= probe_interval_sec
        plan = evaluate_pending_decision_sync(
            age_sec=age_sec,
            broker_flat=broker_flat,
            min_hold_sec=min_hold_sec,
            timeout_sec=entry_fill_timeout_sec,
            probe_interval_elapsed=probe_interval_elapsed,
            has_active_orders=False,
            qty_long=float(qty_long or 0.0),
            qty_short=float(qty_short or 0.0),
            min_abs=self._entry_broker_position_guard_min_abs,
            current_probe_count=int(state.extras.get("pending_decision_flat_probe_count") or 0),
            probe_need=probe_need,
            ctx_read_failed=bool(err_long is not None and err_short is not None),
        )
        if plan.should_timeout_clear:
            orders = await self._fetch_open_orders()
            cancelled = await self._cancel_trigger_open_orders_best_effort(
                state=state,
                reason="entry_fill_timeout_cancel_trigger",
                orders=orders,
            )
            cancelled_all_fallback = False
            if cancelled <= 0:
                cancelled = await self._cancel_strategy_open_orders_best_effort(
                    state=state,
                    reason="entry_fill_timeout_cancel_all_fallback",
                    orders=orders,
                )
                cancelled_all_fallback = cancelled > 0
            state.pending_decision_id = None
            self._clear_pending_probe_state(state)
            state.active_trigger_id = None
            state.expected_entry_qty = 0
            state.entry_order_ref = None
            if entry_timeout_reentry_cooldown_sec > 0:
                state.extras["entry_timeout_block_until_et"] = now_et + _dt.timedelta(seconds=entry_timeout_reentry_cooldown_sec)
            else:
                self._clear_entry_timeout_cooldown(state)
            cooldown_until = self._coerce_datetime(state.extras.get("entry_timeout_block_until_et"))
            self._last_preview_by_symbol[symbol_u] = {
                "status": "ENTRY_FILL_TIMEOUT_CLEARED",
                "reason": "entry_fill_timeout",
                "exec_symbol": state.active_exec_symbol,
                "cancelled_orders": cancelled,
                "cooldown_until_et": cooldown_until.isoformat() if cooldown_until is not None else None,
            }
            self._emit_local_audit(
                stage="entry_fill_timeout_cleared",
                domain="recon",
                severity="warning",
                status="cleared",
                reason="entry_fill_timeout",
                symbol=symbol_u,
                payload={
                    "exec_symbol": state.active_exec_symbol,
                    "age_sec": age_sec,
                    "timeout_sec": entry_fill_timeout_sec,
                    "cancelled_orders": cancelled,
                    "cancel_scope": "all_fallback" if cancelled_all_fallback else "trigger",
                    "cooldown_sec": entry_timeout_reentry_cooldown_sec,
                },
            )
            return True
        if not plan.should_probe:
            return False
        has_active = await self._has_active_strategy_open_orders(state=state)
        plan = evaluate_pending_decision_sync(
            age_sec=age_sec,
            broker_flat=broker_flat,
            min_hold_sec=min_hold_sec,
            timeout_sec=entry_fill_timeout_sec,
            probe_interval_elapsed=probe_interval_elapsed,
            has_active_orders=has_active,
            qty_long=float(qty_long or 0.0),
            qty_short=float(qty_short or 0.0),
            min_abs=self._entry_broker_position_guard_min_abs,
            current_probe_count=int(state.extras.get("pending_decision_flat_probe_count") or 0),
            probe_need=probe_need,
            ctx_read_failed=bool(err_long is not None and err_short is not None),
        )
        if not plan.should_probe:
            state.extras["pending_decision_flat_probe_count"] = int(plan.next_probe_count)
            return False
        state.extras["pending_decision_last_probe_et"] = now_et
        probe_count = int(plan.next_probe_count)
        state.extras["pending_decision_flat_probe_count"] = probe_count
        if not plan.should_clear_flat_no_active_orders:
            return False
        state.pending_decision_id = None
        self._clear_pending_probe_state(state)
        state.active_trigger_id = None
        state.expected_entry_qty = 0
        state.entry_order_ref = None
        self._last_preview_by_symbol[symbol_u] = {
            "status": "PENDING_DECISION_CLEARED",
            "reason": "flat_no_active_orders",
            "exec_symbol": state.active_exec_symbol,
        }
        self._emit_local_audit(
            stage="pending_decision_cleared",
            domain="recon",
            severity="warning",
            status="cleared",
            reason="flat_no_active_orders",
            symbol=symbol_u,
            payload={"exec_symbol": state.active_exec_symbol, "age_sec": age_sec, "probe_need": probe_need},
        )
        return True

    def _resolve_broker_cancel_fn(self):
        engine = getattr(self.ctx, "engine", None)
        candidates = [
            getattr(getattr(getattr(engine, "execution_service", None), "kernel", None), "broker", None),
            getattr(engine, "broker_adapter", None),
            getattr(getattr(engine, "market_data_service", None), "ib_adapter", None),
        ]
        for adapter in candidates:
            cancel_fn = getattr(adapter, "cancel", None)
            if not callable(cancel_fn):
                continue
            fn_mod = str(type(cancel_fn).__module__ or "")
            if fn_mod.startswith("unittest.mock") and type(cancel_fn).__name__ != "AsyncMock":
                continue

            async def _cancel(order_id: int, _fn=cancel_fn):
                result = _fn(order_id)
                if inspect.isawaitable(result):
                    return await result
                return result

            return _cancel
        return None

    @staticmethod
    def _cancel_id_from_open_order(order: Any) -> int | None:
        return cancel_id_from_order(order)

    async def _fetch_open_orders(self) -> list[Any] | None:
        open_orders = getattr(self.ctx, "open_orders", None)
        if not callable(open_orders):
            return None
        try:
            return list(await open_orders() or [])
        except Exception:
            return None

    async def _cancel_strategy_open_orders_best_effort(self, *, state: ReplicaTradeState, reason: str, orders: list[Any] | None = None) -> int:
        if orders is None:
            orders = await self._fetch_open_orders()
        if orders is None:
            return 0
        owned_symbols = {
            str(state.exec_symbol_long or "").strip().upper(),
            str(state.exec_symbol_short or "").strip().upper(),
            str(state.active_exec_symbol or "").strip().upper(),
        }
        owned_symbols.discard("")
        namespace_prefixes = {
            f"{self._entry_core.order_namespace}:{self.strategy_id}:{symbol}:"
            for symbol in owned_symbols
            if symbol
        }
        active: list[Any] = []
        for order in orders:
            if self._is_terminal_order_state(order):
                continue
            order_symbol = get_order_symbol(order)
            if order_symbol and order_symbol not in owned_symbols:
                continue
            order_strategy_id = get_order_strategy_id(order)
            order_ref = get_order_ref(order)
            if order_strategy_id != self.strategy_id and not matches_any_namespace_prefix(order_ref, namespace_prefixes):
                continue
            active.append(order)
        if not active:
            return 0
        cancel_fn = self._resolve_broker_cancel_fn()
        if cancel_fn is None:
            return 0
        cancelled = 0
        for order in active:
            cancel_id = self._cancel_id_from_open_order(order)
            if cancel_id is None:
                continue
            try:
                result = await cancel_fn(int(cancel_id))
                if result is False:
                    continue
                cancelled += 1
            except Exception:
                continue
        return cancelled

    async def _cancel_trigger_open_orders_best_effort(self, *, state: ReplicaTradeState, reason: str, orders: list[Any] | None = None) -> int:
        trigger_id = str(state.active_trigger_id or state.pending_decision_id or "").strip()
        if not trigger_id:
            return 0
        if orders is None:
            orders = await self._fetch_open_orders()
        if orders is None:
            return 0
        cancel_fn = self._resolve_broker_cancel_fn()
        if cancel_fn is None:
            return 0
        cancelled = 0
        for order in orders:
            if self._is_terminal_order_state(order):
                continue
            order_strategy_id = get_order_strategy_id(order)
            order_ref = get_order_ref(order)
            if order_strategy_id and order_strategy_id != self.strategy_id:
                continue
            if trigger_id not in order_ref:
                continue
            if not self._is_owned_order_update(update=order, state=state):
                continue
            cancel_id = self._cancel_id_from_open_order(order)
            if cancel_id is None:
                continue
            try:
                result = await cancel_fn(int(cancel_id))
                if result is False:
                    continue
                cancelled += 1
            except Exception:
                continue
        if cancelled > 0:
            self._emit_local_audit(
                stage="trigger_open_orders_cancelled",
                domain="recon",
                severity="warning",
                status="cancelled",
                reason=reason,
                symbol=state.signal_symbol,
                payload={
                    "exec_symbol": state.active_exec_symbol,
                    "trigger_id": trigger_id,
                    "cancelled_orders": cancelled,
                },
            )
        return cancelled

    async def _maybe_sweep_flat_orphan_orders(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        eval_now: datetime,
    ) -> bool:
        if state.is_traded or state.is_flattening or state.pending_decision_id is not None:
            return False
        interval_sec = max(5.0, self._extra_float("flat_orphan_sweep_interval_sec", 30.0))
        last_sweep_at = self._coerce_datetime(state.extras.get("last_flat_orphan_sweep_et"))
        if last_sweep_at is not None and (eval_now - last_sweep_at).total_seconds() < interval_sec:
            return False
        state.extras["last_flat_orphan_sweep_et"] = eval_now
        cancelled = await self._cancel_strategy_open_orders_best_effort(
            state=state,
            reason="flat_orphan_sweep",
        )
        if cancelled <= 0:
            return False
        self._emit_local_audit(
            stage="flat_orphan_orders_cancelled",
            domain="recon",
            severity="warning",
            status="cancelled",
            reason="flat_orphan_sweep",
            symbol=symbol_u,
            payload={
                "exec_symbol": state.active_exec_symbol,
                "cancelled_orders": cancelled,
                "interval_sec": interval_sec,
            },
        )
        return True

    def _is_owned_order_update(self, *, update: Any, state: ReplicaTradeState) -> bool:
        assert self._entry_core is not None
        order_ref = str(getattr(update, "order_ref", "") or "").strip()
        if not order_ref:
            return False
        candidate_symbols = {
            str(state.exec_symbol_long or "").strip().upper(),
            str(state.exec_symbol_short or "").strip().upper(),
            str(state.active_exec_symbol or "").strip().upper(),
            str(getattr(update, "symbol", "") or "").strip().upper(),
        }
        candidate_symbols.discard("")
        for symbol in candidate_symbols:
            prefix = f"{self._entry_core.order_namespace}:{self.strategy_id}:{symbol}:"
            if order_ref.startswith(prefix):
                return True
        return False

    def _active_owned_lots(self, *, state: ReplicaTradeState, exec_symbol: str | None = None) -> list[Any]:
        exec_symbol_u = str(exec_symbol or "").strip().upper() or None
        return self._ownership_ledger.active_lots(
            strategy_id=self.strategy_id,
            signal_symbol=state.signal_symbol,
            exec_symbol=exec_symbol_u,
        )

    def _bind_or_recover_live_lot(
        self,
        *,
        signal_symbol: str,
        state: ReplicaTradeState,
        exec_symbol: str,
        side: str,
        live_qty: int,
        source: str,
    ) -> str | None:
        exec_symbol_u = str(exec_symbol or "").strip().upper()
        if not exec_symbol_u or live_qty <= 0:
            return None
        active_lot_id = str(state.extras.get("active_position_lot_id") or "").strip()
        bound_lot = self._ownership_ledger.lot_by_id(active_lot_id)
        if bound_lot is not None and bound_lot.remaining_qty > 0.0:
            self._ownership_ledger.sync_lot_live_qty(position_lot_id=bound_lot.position_lot_id, live_qty=float(live_qty))
            state.extras["sync_quarantine_reason"] = None
            state.extras["sync_quarantine_payload"] = None
            return bound_lot.position_lot_id
        active_lots = self._active_owned_lots(state=state, exec_symbol=exec_symbol_u)
        if len(active_lots) == 1:
            lot = active_lots[0]
            self._ownership_ledger.sync_lot_live_qty(position_lot_id=lot.position_lot_id, live_qty=float(live_qty))
            state.extras["active_position_lot_id"] = lot.position_lot_id
            state.extras["sync_quarantine_reason"] = None
            state.extras["sync_quarantine_payload"] = None
            self._emit_local_audit(
                stage="ownership_lot_rebound",
                domain="recon",
                status="rebound",
                reason=source,
                symbol=signal_symbol,
                payload={"exec_symbol": exec_symbol_u, "position_lot_id": lot.position_lot_id, "live_qty": live_qty},
            )
            return lot.position_lot_id
        if len(active_lots) > 1:
            self._set_sync_quarantine(
                state=state,
                reason="ownership_binding_conflict",
                payload={
                    "exec_symbol": exec_symbol_u,
                    "live_qty": live_qty,
                    "candidate_lot_ids": [lot.position_lot_id for lot in active_lots],
                },
            )
            self._emit_local_audit(
                stage="ownership_lot_binding_conflict",
                domain="recon",
                severity="warning",
                status="conflict",
                reason=source,
                symbol=signal_symbol,
                payload={
                    "exec_symbol": exec_symbol_u,
                    "live_qty": live_qty,
                    "candidate_lot_ids": [lot.position_lot_id for lot in active_lots],
                },
            )
            return None
        if bool(self._extras.get("strict_recovered_lot_mode", False)):
            self._set_sync_quarantine(
                state=state,
                reason="ownership_recovery_required",
                payload={"exec_symbol": exec_symbol_u, "live_qty": live_qty, "source": source},
            )
            self._emit_local_audit(
                stage="ownership_lot_recovery_required",
                domain="recon",
                severity="warning",
                status="quarantined",
                reason=source,
                symbol=signal_symbol,
                payload={"exec_symbol": exec_symbol_u, "live_qty": live_qty},
            )
            return None
        recovered_lot_id = f"lot:{self.strategy_id}:{signal_symbol}:recovered:{exec_symbol_u}:{int(datetime.now(ET).timestamp() * 1_000_000)}"
        self._ownership_ledger.record_entry_intent(
            ReplicaOwnershipIntentRecord(
                strategy_id=self.strategy_id,
                decision_id=str(state.pending_decision_id or state.active_trigger_id or "recovered"),
                trigger_id=str(state.active_trigger_id or "recovered"),
                signal_symbol=signal_symbol,
                exec_symbol=exec_symbol_u,
                side=str(side or "").strip().upper() or "LONG",
                qty=float(live_qty),
                ts_bar_end_us=int(datetime.now(ET).timestamp() * 1_000_000),
                position_lot_id=recovered_lot_id,
                order_ref=str(state.entry_order_ref or ""),
                payload={"recovered": True, "source": source},
            )
        )
        self._ownership_ledger.sync_lot_live_qty(position_lot_id=recovered_lot_id, live_qty=float(live_qty))
        state.extras["active_position_lot_id"] = recovered_lot_id
        self._emit_local_audit(
            stage="ownership_lot_recovered",
            domain="recon",
            severity="warning",
            status="recovered",
            reason=source,
            symbol=signal_symbol,
            payload={"exec_symbol": exec_symbol_u, "position_lot_id": recovered_lot_id, "live_qty": live_qty},
        )
        return recovered_lot_id

    async def _sync_from_real_exec_positions(self, *, symbol_u: str, state: ReplicaTradeState) -> bool:
        long_symbol = str(state.exec_symbol_long or "").strip().upper()
        short_symbol = str(state.exec_symbol_short or "").strip().upper()
        long_qty, long_err = (0.0, None)
        short_qty, short_err = (0.0, None)
        if long_symbol:
            long_qty, long_err = await self._read_ctx_position_qty(long_symbol)
        if short_symbol and short_symbol != long_symbol:
            short_qty, short_err = await self._read_ctx_position_qty(short_symbol)
        dual_conflict = evaluate_dual_conflict(
            qty_long=float(long_qty or 0.0) if long_err is None else 0.0,
            qty_short=float(short_qty or 0.0) if short_err is None else 0.0,
            min_abs=self._entry_broker_position_guard_min_abs,
        )
        if dual_conflict.crossed_inventory:
            self._set_sync_quarantine(
                state=state,
                reason="crossed_inventory_detected",
                payload={
                    "long_symbol": long_symbol,
                    "long_qty": float(long_qty or 0.0),
                    "short_symbol": short_symbol,
                    "short_qty": float(short_qty or 0.0),
                },
            )
            self._last_preview_by_symbol[symbol_u] = {
                "status": "REAL_POSITION_QUARANTINED",
                "reason": "crossed_inventory_detected",
                "long_symbol": long_symbol,
                "long_qty": float(long_qty or 0.0),
                "short_symbol": short_symbol,
                "short_qty": float(short_qty or 0.0),
            }
            self._emit_local_audit(
                stage="crossed_inventory_detected",
                domain="recon",
                severity="error",
                status="quarantined",
                reason="crossed_inventory_detected",
                symbol=symbol_u,
                payload={
                    "long_symbol": long_symbol,
                    "long_qty": float(long_qty or 0.0),
                    "short_symbol": short_symbol,
                    "short_qty": float(short_qty or 0.0),
                },
            )
            self._persist_runtime_snapshot()
            return True
        long_live = long_err is None and dual_conflict.live_symbol == "LONG"
        short_live = short_err is None and dual_conflict.live_symbol == "SHORT"
        candidates = []
        if long_live:
            candidates.append((long_symbol, "LONG", float(long_qty or 0.0)))
        if short_live:
            candidates.append((short_symbol, "SHORT", float(short_qty or 0.0)))
        for exec_symbol, side, qty_f in candidates:
            lot_id = self._bind_or_recover_live_lot(
                signal_symbol=symbol_u,
                state=state,
                exec_symbol=exec_symbol,
                side=side,
                live_qty=int(max(0.0, abs(qty_f))),
                source="real_position_sync",
            )
            if not lot_id and is_sync_quarantined(state.extras):
                self._last_preview_by_symbol[symbol_u] = {
                    "status": "REAL_POSITION_QUARANTINED",
                    "reason": sync_quarantine_reason(state.extras),
                    "exec_symbol": exec_symbol,
                    "live_qty": int(max(0.0, abs(qty_f))),
                }
                self._persist_runtime_snapshot()
                return True
            state.is_traded = True
            state.active_exec_symbol = exec_symbol
            state.active_side = side
            state.extras["live_qty"] = int(max(0.0, abs(qty_f)))
            state.extras["round_live_confirmed"] = True
            self._clear_entry_timeout_cooldown(state)
            if lot_id:
                state.extras["active_position_lot_id"] = lot_id
            self._emit_local_audit(
                stage="real_position_sync_blocked_entry",
                domain="recon",
                severity="warning",
                status="live",
                reason="sync_from_real_position",
                symbol=symbol_u,
                payload={
                    "exec_symbol": exec_symbol,
                    "live_qty": int(max(0.0, abs(qty_f))),
                    "broker_pos_qty": qty_f,
                    "position_lot_id": lot_id,
                },
            )
            self._refresh_ownership_snapshot()
            return True
        return False

    @staticmethod
    def _clear_zero_position_pending(state: ReplicaTradeState) -> None:
        state.extras["zero_position_flat_pending_since_et"] = None
        state.extras["zero_position_flat_confirm_count"] = 0

    def _should_commit_flat_sync(self, *, state: ReplicaTradeState) -> bool:
        confirm_need = max(1, self._extra_int("zero_position_flat_confirmations", 1))
        grace_sec = max(0.0, self._extra_float("zero_position_flat_grace_sec", 0.0))
        now_et = datetime.now(ET)
        pending_since = self._coerce_datetime(state.extras.get("zero_position_flat_pending_since_et"))
        confirm_count = int(max(0, int(state.extras.get("zero_position_flat_confirm_count") or 0)))
        if pending_since is None:
            pending_since = now_et
            confirm_count = 1
        else:
            confirm_count += 1
        state.extras["zero_position_flat_pending_since_et"] = pending_since
        state.extras["zero_position_flat_confirm_count"] = confirm_count
        age_sec = max(0.0, (now_et - pending_since).total_seconds())
        return confirm_count >= confirm_need and age_sec >= grace_sec

    def _set_sync_quarantine(self, *, state: ReplicaTradeState, reason: str, payload: Dict[str, Any] | None = None) -> None:
        state.extras["sync_quarantine_reason"] = str(reason or "").strip()
        state.extras["sync_quarantine_payload"] = dict(payload or {})

    def _derive_signal_decision(
        self,
        *,
        binding: Any,
        state: ReplicaTradeState,
        features: Dict[str, Any],
        eval_now: datetime,
    ) -> SignalDecision:
        if self._has_explicit_signal_fields(features):
            return self._signal_model.extract(binding=binding, features=features)
        return self._derive_qqq_style_signal_decision(binding=binding, state=state, features=features, eval_now=eval_now)

    @staticmethod
    def _has_explicit_signal_fields(features: Dict[str, Any]) -> bool:
        explicit_keys = {
            "is_entry_signal",
            "signal_active",
            "intent_side",
            "side",
            "intent_target_qty",
            "target_qty",
        }
        return any(key in features for key in explicit_keys)

    def _derive_qqq_style_signal_decision(
        self,
        *,
        binding: Any,
        state: ReplicaTradeState,
        features: Dict[str, Any],
        eval_now: datetime,
    ) -> SignalDecision:
        primary_px = self._binding_feature_float(
            features,
            binding=binding,
            suffix="close_now",
            legacy_keys=("qqq_close_now", "signal_price", "close", "last_px"),
        )
        primary_vwap = self._primary_vwap_value(binding=binding, features=features, eval_now=eval_now)
        vol_surge_ratio = self._optional_float(features.get("vol_surge_ratio"))
        if vol_surge_ratio is None:
            vol_surge_ratio = 1.0
        if primary_px is None or primary_px <= 0 or primary_vwap is None or primary_vwap <= 0:
            return self._inactive_signal_decision(binding=binding, features=features, reason="required_data_missing", signal_price=primary_px)
        if primary_px > primary_vwap:
            side = "LONG"
        elif primary_px < primary_vwap:
            side = "SHORT"
        else:
            return self._inactive_signal_decision(binding=binding, features=features, reason="direction_mismatch", signal_price=primary_px)
        exec_symbol = str(state.exec_symbol_for_side(side)).strip().upper()
        atr_eff = self._atr_effective_value(features=features, exec_symbol=exec_symbol, eval_now=eval_now)
        if atr_eff is None or atr_eff <= 0:
            return self._inactive_signal_decision(binding=binding, features=features, reason="atr_eff_not_ready", signal_price=primary_px)
        exec_price = self._exec_price_from_features_or_cache(exec_symbol=exec_symbol, features=features)
        if exec_price is None or exec_price <= 0:
            return self._inactive_signal_decision(
                binding=binding,
                features=features,
                reason="missing_exec_price_for_sizing",
                side=side,
                exec_symbol=exec_symbol,
                signal_price=primary_px,
                exec_price=exec_price,
            )
        self._update_fast_cross_state(
            state=state,
            eval_now=eval_now,
            primary_px=float(primary_px),
            primary_vwap=float(primary_vwap),
        )
        opening_gate_reason = self._fallback_opening_gate_reason(
            state=state,
            eval_now=eval_now,
            side=side,
            primary_px=float(primary_px),
            primary_vwap=float(primary_vwap),
            atr_eff=float(atr_eff),
            features=features,
        )
        if opening_gate_reason is not None:
            return self._inactive_signal_decision(
                binding=binding,
                features=features,
                reason=opening_gate_reason,
                side=side,
                exec_symbol=exec_symbol,
                signal_price=primary_px,
                exec_price=exec_price,
            )
        v3m_signed = self._optional_float(features.get("v3m_signed"))
        if v3m_signed is None:
            v3m_signed = self._derive_v3m_signed_from_history(state=state, eval_now=eval_now, current_close=primary_px)
        if v3m_signed is None:
            return self._inactive_signal_decision(
                binding=binding,
                features=features,
                reason="v3m_not_ready",
                side=side,
                exec_symbol=exec_symbol,
                signal_price=primary_px,
                exec_price=exec_price,
            )
        deadzone = self._extra_float("momentum_deadzone", 0.05)
        sign_ok = (side == "LONG" and v3m_signed > deadzone) or (side == "SHORT" and v3m_signed < -deadzone)
        v3m_atr = abs(float(v3m_signed)) / max(float(atr_eff), 1e-9)
        veto_reason = self._fallback_veto_reason(
            state=state,
            eval_now=eval_now,
            side=side,
            primary_px=float(primary_px),
            atr_eff=float(atr_eff),
        )
        if veto_reason is not None:
            reason = veto_reason
            target_qty = 0.0
            return SignalDecision(
                signal_symbol=str(getattr(binding, "signal_symbol", "") or state.signal_symbol).strip().upper(),
                exec_symbol=exec_symbol,
                signal_active=False,
                target_qty=0.0,
                side=side,
                reason=reason,
                signal_price=primary_px,
                exec_price=exec_price,
                metadata={
                    "binding_key": getattr(binding, "key", state.signal_symbol),
                    "decision_source": "qqq_style_fallback",
                    "v3m_signed": float(v3m_signed),
                    "v3m_atr": float(v3m_atr),
                    "atr_eff": float(atr_eff),
                },
            )
        fast_enabled = bool(self._extras.get("enable_fast", True))
        fast_v3m_atr_min = self._extra_float("fast_v3m_atr_min", 0.60)
        fast_vol_min = self._extra_float("fast_vol_min", 2.0)
        self._maybe_arm_fast_cooldown(state=state, eval_now=eval_now)
        fast_cooldown_until = self._coerce_datetime(state.extras.get("fast_cooldown_until_et"))
        fast_in_cooldown = fast_cooldown_until is not None and eval_now < fast_cooldown_until
        fast_ok = fast_enabled and sign_ok and v3m_atr >= fast_v3m_atr_min and float(vol_surge_ratio) >= fast_vol_min
        if fast_in_cooldown:
            fast_ok = False
        drift_ok = self._derive_drift_ok(
            state=state,
            eval_now=eval_now,
            side=side,
            atr_eff=float(atr_eff),
            sign_ok=sign_ok,
        )
        reason = "fast_drift_not_ready"
        if not sign_ok:
            reason = "sign_mismatch"
        elif fast_ok:
            reason = "qqq_intraday_entry"
        elif drift_ok:
            drift_block = self._fallback_drift_block_reason(
                state=state,
                eval_now=eval_now,
                side=side,
                primary_px=float(primary_px),
                primary_vwap=float(primary_vwap),
            )
            reason = drift_block or "qqq_intraday_entry"
        elif fast_in_cooldown and v3m_atr >= fast_v3m_atr_min and float(vol_surge_ratio) >= fast_vol_min:
            reason = "fast_cooldown_active"
        target_qty = 0.0
        if reason == "qqq_intraday_entry":
            target_qty = float(max(0, int(math.floor(float(self._signal_model.default_notional_usd or 0.0) / float(exec_price)))))
            if target_qty <= 0:
                reason = "missing_exec_price_for_sizing"
        signal_active = reason == "qqq_intraday_entry" and target_qty > 0
        return SignalDecision(
            signal_symbol=str(getattr(binding, "signal_symbol", "") or state.signal_symbol).strip().upper(),
            exec_symbol=exec_symbol,
            signal_active=signal_active,
            target_qty=max(0.0, float(target_qty)),
            side=side,
            reason=reason,
            signal_price=primary_px,
            exec_price=exec_price,
            metadata={
                "binding_key": getattr(binding, "key", state.signal_symbol),
                "decision_source": "qqq_style_fallback",
                "fast_ok": bool(fast_ok),
                "drift_ok": bool(drift_ok),
                "sign_ok": bool(sign_ok),
                "v3m_signed": float(v3m_signed),
                "v3m_atr": float(v3m_atr),
                "vol_surge_ratio": float(vol_surge_ratio),
                "atr_eff": float(atr_eff),
                "fast_cross_count": int(state.extras.get("fast_cross_count") or 0),
                "fast_in_cooldown": bool(fast_in_cooldown),
            },
        )

    @staticmethod
    def _split_probe_add_qty(total_qty: int, *, probe_ratio: float = 0.70) -> tuple[int, int]:
        qty_total = int(max(0, total_qty))
        if qty_total <= 0:
            return 0, 0
        ratio = min(1.0, max(0.0, float(probe_ratio)))
        probe_qty = int(math.floor(float(qty_total) * ratio))
        if probe_qty <= 0:
            probe_qty = qty_total
        add_qty = max(0, int(qty_total - probe_qty))
        return probe_qty, add_qty

    async def _entry_day_gross_notional_allowed(
        self,
        *,
        state: ReplicaTradeState,
        probe_qty: int,
        exec_symbol: str,
        exec_price: float | None,
    ) -> tuple[bool, Dict[str, Any]]:
        extras = self._extras or {}
        cap = self._optional_float(extras.get("max_day_gross_notional_usd"))
        if cap is None or cap <= 0:
            cap = self._optional_float(extras.get("notional_usd"))
        if cap is None or cap <= 0:
            return True, {}
        last_exec = self._optional_float(exec_price) or self._read_last_price_for_symbol(exec_symbol=exec_symbol)
        if last_exec is None or last_exec <= 0:
            return True, {}
        qty_t, err_t = await self._read_ctx_position_qty(str(state.exec_symbol_long or "").strip().upper())
        qty_s, err_s = await self._read_ctx_position_qty(str(state.exec_symbol_short or "").strip().upper())
        if err_t is not None or err_s is not None:
            return True, {}
        long_symbol = str(state.exec_symbol_long or "").strip().upper()
        short_symbol = str(state.exec_symbol_short or "").strip().upper()
        px_t = self._read_last_price_for_symbol(
            exec_symbol=long_symbol,
            fallback_price=last_exec if long_symbol == str(exec_symbol or "").strip().upper() else None,
        ) or 0.0
        px_s = self._read_last_price_for_symbol(
            exec_symbol=short_symbol,
            fallback_price=last_exec if short_symbol == str(exec_symbol or "").strip().upper() else None,
        ) or 0.0
        existing_gross_notional = (abs(float(qty_t or 0.0)) * float(px_t)) + (abs(float(qty_s or 0.0)) * float(px_s))
        day_cap_check = check_day_gross_notional_cap(
            cap=float(cap),
            existing_long_qty=float(qty_t or 0.0),
            existing_short_qty=float(qty_s or 0.0),
            long_price=float(px_t),
            short_price=float(px_s),
            probe_qty=int(probe_qty),
            entry_price=float(last_exec),
        )
        if day_cap_check.allowed:
            return True, {}
        return False, {
            "cap": float(day_cap_check.cap),
            "existing_gross_notional": float(day_cap_check.existing_gross_notional),
            "planned_notional": float(day_cap_check.planned_notional),
        }

    def _read_last_price_for_symbol(self, *, exec_symbol: str, fallback_price: float | None = None) -> float | None:
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return self._optional_float(fallback_price)
        try:
            snap = self.get_price(symbol_u)
        except Exception:
            snap = None
        if snap is None:
            ctx_get_price = getattr(self.ctx, "get_price", None)
            if callable(ctx_get_price):
                try:
                    snap = ctx_get_price(symbol_u)
                except Exception:
                    snap = None
        if snap is not None:
            for attr in ("last", "price", "mid", "close", "mark"):
                value = self._optional_float(getattr(snap, attr, None))
                if value is not None and value > 0:
                    return value
            if isinstance(snap, dict):
                for key in ("last", "price", "mid", "close", "mark"):
                    value = self._optional_float(snap.get(key))
                    if value is not None and value > 0:
                        return value
        return self._optional_float(fallback_price)

    def _inactive_signal_decision(
        self,
        *,
        binding: Any,
        features: Dict[str, Any],
        reason: str,
        side: str | None = None,
        exec_symbol: str | None = None,
        signal_price: float | None = None,
        exec_price: float | None = None,
    ) -> SignalDecision:
        side_u = str(side or "").strip().upper()
        resolved_side = side_u if side_u in {"LONG", "SHORT"} else None
        resolved_exec_symbol = str(exec_symbol or "").strip().upper()
        if not resolved_exec_symbol:
            resolved_exec_symbol = str(binding.exec_symbol_for_side(resolved_side)).strip().upper()
        metadata: dict[str, Any] = {
            "binding_key": getattr(binding, "key", None),
            "decision_source": "qqq_style_fallback",
            "feature_keys": sorted(list(features.keys()))[:20],
        }
        if reason == "required_data_missing":
            missing: list[str] = []
            signal_symbol = str(getattr(binding, "signal_symbol", "") or "").strip().upper()
            confirm_symbol = str(getattr(binding, "confirm_symbol", "") or "").strip().upper()
            if signal_price is None or signal_price <= 0:
                missing.append(f"{signal_symbol}:PRICE")
            primary_vwap = self._primary_vwap_value(binding=binding, features=features, eval_now=self._feature_now_et(features))
            if primary_vwap is None or primary_vwap <= 0:
                missing.append(f"{signal_symbol}:VWAP")
            if bool(self._extras.get("use_confirm_symbol", False)) and confirm_symbol:
                confirm_vwap = self._binding_feature_float(
                    features,
                    binding=binding,
                    suffix="vwap_now",
                    legacy_keys=("qqew_vwap_now",),
                    use_confirm_symbol=True,
                )
                if confirm_vwap is None or confirm_vwap <= 0:
                    missing.append(f"{confirm_symbol}:VWAP")
            if missing:
                metadata["missing_required"] = missing
        return SignalDecision(
            signal_symbol=str(getattr(binding, "signal_symbol", "") or "").strip().upper(),
            exec_symbol=resolved_exec_symbol,
            signal_active=False,
            target_qty=0.0,
            side=resolved_side,
            reason=str(reason or "signal_inactive_or_invalid").strip() or "signal_inactive_or_invalid",
            signal_price=signal_price,
            exec_price=exec_price,
            metadata=metadata,
        )

    @staticmethod
    def _signal_gate_details(decision: SignalDecision) -> Dict[str, Any]:
        metadata = getattr(decision, "metadata", None)
        if not isinstance(metadata, dict):
            return {}
        gate_details: Dict[str, Any] = {}
        missing_required = metadata.get("missing_required")
        if isinstance(missing_required, list) and missing_required:
            gate_details["missing_required"] = list(missing_required)
        missing = metadata.get("missing")
        if missing is not None:
            gate_details["missing"] = missing
        return gate_details

    def _fallback_opening_gate_reason(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        side: str,
        primary_px: float,
        primary_vwap: float,
        atr_eff: float,
        features: Dict[str, Any],
    ) -> str | None:
        atr_rank = self._optional_float(features.get("atr_rank_today"))
        if atr_rank is None:
            atr_rank = self._optional_float(self._extras.get("atr_rank_today"))
        early_override_feature = features.get("early_override_ready")
        if early_override_feature is None and self._extras.get("early_override_ready") is not None:
            early_override_feature = self._extras.get("early_override_ready")
        close_by_minute = self._minute_history_map(state=state, key="primary_close_history", field="close")
        vwap_by_minute = self._minute_history_map(state=state, key="primary_vwap_history", field="vwap")
        vol_surge_by_minute = self._minute_history_map(
            state=state,
            key="primary_vol_surge_history",
            field="vol_surge_ratio",
        )
        return evaluate_v47_opening_gate_reason(
            eval_now=eval_now,
            side=side,
            primary_px=float(primary_px),
            atr_eff=float(atr_eff),
            atr_rank=atr_rank,
            atr_d=self._optional_float(features.get("atr_d_override")) or atr_eff,
            session_open_price=self._optional_float(state.extras.get("session_open_price")),
            close_by_minute=close_by_minute,
            vwap_by_minute=vwap_by_minute,
            vol_surge_by_minute=vol_surge_by_minute,
            config=self._v47_gate_config(),
            early_override_feature=bool(early_override_feature) if early_override_feature is not None else None,
        )

    def _fallback_early_override_ready(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        side: str,
        atr_eff: float,
    ) -> bool:
        close_by_minute = self._minute_history_map(state=state, key="primary_close_history", field="close")
        vwap_by_minute = self._minute_history_map(state=state, key="primary_vwap_history", field="vwap")
        vol_surge_by_minute = self._minute_history_map(
            state=state,
            key="primary_vol_surge_history",
            field="vol_surge_ratio",
        )
        return evaluate_v47_early_override_ready(
            eval_now=eval_now,
            side=side,
            atr_eff=float(atr_eff),
            session_open_price=self._optional_float(state.extras.get("session_open_price")),
            px_0938=close_by_minute.get(eval_now.replace(hour=9, minute=38, second=0, microsecond=0)),
            close_by_minute=close_by_minute,
            vwap_by_minute=vwap_by_minute,
            vol_surge_by_minute=vol_surge_by_minute,
            config=self._v47_gate_config(),
        )

    def _fallback_drift_block_reason(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        side: str,
        primary_px: float,
        primary_vwap: float,
    ) -> str | None:
        close_by_minute = self._minute_history_map(state=state, key="primary_close_history", field="close")
        return evaluate_v47_drift_block_reason(
            eval_now=eval_now,
            side=side,
            primary_px=float(primary_px),
            primary_vwap=float(primary_vwap),
            close_by_minute=close_by_minute,
            session_pos_day=self._session_pos_day_from_history(state=state, eval_now=eval_now, current_close=primary_px),
            config=self._v47_gate_config(),
        )

    def _fallback_veto_reason(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        side: str,
        primary_px: float,
        atr_eff: float,
    ) -> str | None:
        return evaluate_v47_veto_reason(
            eval_now=eval_now,
            side=side,
            primary_px=float(primary_px),
            atr_eff=float(atr_eff),
            live_samples=self._live_samples(state=state),
            config=self._v47_gate_config(),
        )

    def _fallback_chase_guard_reason(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        side: str,
        primary_px: float,
        primary_vwap: float,
    ) -> str | None:
        if not bool(self._extras.get("enable_v47_chase_guard", False)):
            return None
        start_t = _dt.time(
            *self._parse_hms_tuple(self._extras.get("v47_chase_guard_start", "09:45:00"), fallback=(9, 45, 0))
        )
        end_t = _dt.time(
            *self._parse_hms_tuple(self._extras.get("v47_chase_guard_end", "11:50:00"), fallback=(11, 50, 0))
        )
        now_t = eval_now.timetz().replace(tzinfo=None)
        if not (start_t <= now_t <= end_t) or primary_vwap <= 0:
            return None
        pos_day = self._session_pos_day_from_history(state=state, eval_now=eval_now, current_close=primary_px)
        if pos_day is None:
            return None
        dev_vwap_pct = (float(primary_px) - float(primary_vwap)) / max(float(primary_vwap), 1e-9)
        long_pos_day_min = self._extra_float("v47_long_pos_day_min", 0.80)
        short_pos_day_max = self._extra_float("v47_short_pos_day_max", 0.20)
        long_dev_vwap_min = self._extra_float("v47_long_dev_vwap_min", 0.0020)
        short_dev_vwap_min = self._extra_float("v47_short_dev_vwap_min", 0.0020)
        if side == "LONG" and pos_day >= long_pos_day_min and dev_vwap_pct >= long_dev_vwap_min:
            return "v47_long_chase_guard"
        if side == "SHORT" and pos_day <= short_pos_day_max and dev_vwap_pct <= (-short_dev_vwap_min):
            return "v47_short_chase_guard"
        return None

    def _v47_gate_config(self) -> V47GateConfig:
        return V47GateConfig(
            atr_rank_skip_max=self._extra_float("atr_rank_skip_max", 0.15),
            early_override_window_start=_dt.time(
                *self._parse_hms_tuple(self._extras.get("early_override_window_start", "09:39:00"), fallback=(9, 39, 0))
            ),
            early_override_window_end=_dt.time(
                *self._parse_hms_tuple(self._extras.get("early_override_window_end", "09:45:00"), fallback=(9, 45, 0))
            ),
            early_override_move_mult=self._extra_float("early_override_move_mult", 0.70),
            early_override_vol_min=self._extra_float("early_override_vol_min", 1.50),
            delayed_confirm_end=_dt.time(
                *self._parse_hms_tuple(self._extras.get("delayed_confirm_end", "10:00:00"), fallback=(10, 0, 0))
            ),
            delayed_confirm_move5m_min=self._extra_float("delayed_confirm_move5m_min", 0.155),
            early_lock_mode=str(self._extras.get("early_lock_mode", "openclose")).strip().lower(),
            long_drift_second_confirm=bool(self._extras.get("long_drift_second_confirm", False)),
            long_drift_second_confirm_side=str(self._extras.get("long_drift_second_confirm_side", "long")).strip().lower(),
            long_drift_second_confirm_window_start=_dt.time(
                *self._parse_hms_tuple(
                    self._extras.get("long_drift_second_confirm_window_start", "09:45:00"),
                    fallback=(9, 45, 0),
                )
            ),
            long_drift_second_confirm_window_end=_dt.time(
                *self._parse_hms_tuple(
                    self._extras.get("long_drift_second_confirm_window_end", "10:29:00"),
                    fallback=(10, 29, 0),
                )
            ),
            long_drift_second_confirm_delay_min=max(1, self._extra_int("long_drift_second_confirm_delay_min", 1)),
            enable_v47_veto=bool(self._extras.get("enable_v47_veto", False)),
            veto_start=_dt.time(*self._parse_hms_tuple(self._extras.get("veto_start", "09:40:00"), fallback=(9, 40, 0))),
            veto_end=_dt.time(*self._parse_hms_tuple(self._extras.get("veto_end", "11:50:00"), fallback=(11, 50, 0))),
            veto_rebound_live_atr=self._extra_float("veto_rebound_live_atr", 0.25),
            enable_v47_chase_guard=bool(self._extras.get("enable_v47_chase_guard", False)),
            v47_chase_guard_start=_dt.time(
                *self._parse_hms_tuple(self._extras.get("v47_chase_guard_start", "09:45:00"), fallback=(9, 45, 0))
            ),
            v47_chase_guard_end=_dt.time(
                *self._parse_hms_tuple(self._extras.get("v47_chase_guard_end", "11:50:00"), fallback=(11, 50, 0))
            ),
            v47_long_pos_day_min=self._extra_float("v47_long_pos_day_min", 0.80),
            v47_short_pos_day_max=self._extra_float("v47_short_pos_day_max", 0.20),
            v47_long_dev_vwap_min=self._extra_float("v47_long_dev_vwap_min", 0.0020),
            v47_short_dev_vwap_min=self._extra_float("v47_short_dev_vwap_min", 0.0020),
        )

    def _minute_history_map(self, *, state: ReplicaTradeState, key: str, field: str) -> dict[datetime, float]:
        return market_minute_history_map(
            rows=list(state.extras.get(key) or []),
            field=field,
        )

    def _live_samples(self, *, state: ReplicaTradeState) -> list[tuple[datetime, float]]:
        return market_live_samples(rows=list(state.extras.get("primary_live_samples") or []))

    def _session_pos_day_from_history(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        current_close: float,
    ) -> float | None:
        return market_session_pos_day_from_history(
            rows=list(state.extras.get("primary_close_history") or []),
            eval_now=eval_now,
            current_close=float(current_close),
        )

    def _update_fast_cross_state(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        primary_px: float,
        primary_vwap: float,
    ) -> None:
        if not (_dt.time(9, 30) <= eval_now.timetz().replace(tzinfo=None) <= _dt.time(16, 0)):
            return
        if primary_vwap <= 0:
            return
        minute_ts = eval_now.replace(second=0, microsecond=0)
        if state.extras.get("last_fast_cross_eval_minute") == minute_ts.isoformat():
            return
        upper = float(primary_vwap) * 1.0008
        lower = float(primary_vwap) * 0.9992
        new_state = "MIDDLE"
        if float(primary_px) > upper:
            new_state = "ABOVE"
        elif float(primary_px) < lower:
            new_state = "BELOW"
        prev_state = str(state.extras.get("fast_cross_state") or "MIDDLE").strip().upper()
        if (prev_state == "ABOVE" and new_state == "BELOW") or (prev_state == "BELOW" and new_state == "ABOVE"):
            state.extras["fast_cross_count"] = int(state.extras.get("fast_cross_count") or 0) + 1
        if new_state != "MIDDLE":
            state.extras["fast_cross_state"] = new_state
        state.extras["last_fast_cross_eval_minute"] = minute_ts.isoformat()

    def _maybe_arm_fast_cooldown(self, *, state: ReplicaTradeState, eval_now: datetime) -> None:
        cross_count = int(state.extras.get("fast_cross_count") or 0)
        threshold = self._extra_int("fast_crosses_threshold", 3)
        trigger_cross = int(state.extras.get("fast_cooldown_trigger_cross") or 0)
        if cross_count <= threshold or cross_count <= trigger_cross:
            return
        cooldown_min = max(1.0, self._extra_float("fast_cooldown_min", 15.0))
        state.extras["fast_cooldown_trigger_cross"] = cross_count
        state.extras["fast_cooldown_until_et"] = eval_now + _dt.timedelta(minutes=cooldown_min)

    def _update_primary_close_history(
        self,
        *,
        state: ReplicaTradeState,
        binding: Any,
        features: Dict[str, Any],
        eval_now: datetime,
    ) -> None:
        close_now = self._binding_feature_float(
            features,
            binding=binding,
            suffix="close_now",
            legacy_keys=("qqq_close_now", "signal_price", "close", "last_px"),
        )
        primary_vwap = self._binding_feature_float(
            features,
            binding=binding,
            suffix="vwap_now",
            legacy_keys=("vwap_now", "qqq_vwap", "vwap"),
        )
        vol_surge_ratio = self._optional_float(features.get("vol_surge_ratio"))
        if close_now is None or close_now <= 0:
            return
        minute_ts = eval_now.replace(second=0, microsecond=0)
        minute_iso = minute_ts.isoformat()
        self._upsert_live_sample(state=state, eval_now=eval_now, close_now=float(close_now))
        state.extras["primary_close_history"] = market_upsert_history_row(
            rows=list(state.extras.get("primary_close_history") or []),
            minute_iso=minute_iso,
            field="close",
            value=float(close_now),
            keep=12,
        )
        if primary_vwap is not None and primary_vwap > 0:
            self._upsert_history_row(state=state, key="primary_vwap_history", minute_iso=minute_iso, field="vwap", value=float(primary_vwap))
        if vol_surge_ratio is not None:
            self._upsert_history_row(
                state=state,
                key="primary_vol_surge_history",
                minute_iso=minute_iso,
                field="vol_surge_ratio",
                value=float(vol_surge_ratio),
            )
        if state.extras.get("session_open_price") is None and minute_ts.time() >= _dt.time(9, 30):
            state.extras["session_open_price"] = float(close_now)

    @staticmethod
    def _upsert_history_row(
        *,
        state: ReplicaTradeState,
        key: str,
        minute_iso: str,
        field: str,
        value: float,
    ) -> None:
        state.extras[key] = market_upsert_history_row(
            rows=list(state.extras.get(key) or []),
            minute_iso=minute_iso,
            field=field,
            value=float(value),
            keep=12,
        )

    @staticmethod
    def _upsert_live_sample(
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        close_now: float,
    ) -> None:
        state.extras["primary_live_samples"] = market_upsert_live_sample(
            rows=list(state.extras.get("primary_live_samples") or []),
            ts_iso=eval_now.isoformat(),
            close_now=float(close_now),
            keep=180,
        )

    def _derive_v3m_signed_from_history(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        current_close: float,
    ) -> float | None:
        current_minute = eval_now.replace(second=0, microsecond=0)
        close_3m_ago = self._close_history_value(state=state, minute_ts=current_minute - _dt.timedelta(minutes=3))
        if close_3m_ago is None:
            return None
        return float(current_close) - float(close_3m_ago)

    def _derive_drift_ok(
        self,
        *,
        state: ReplicaTradeState,
        eval_now: datetime,
        side: str,
        atr_eff: float,
        sign_ok: bool,
    ) -> bool:
        if not sign_ok or atr_eff <= 0:
            return False
        current_minute = eval_now.replace(second=0, microsecond=0)
        c0 = self._close_history_value(state=state, minute_ts=current_minute)
        c1 = self._close_history_value(state=state, minute_ts=current_minute - _dt.timedelta(minutes=1))
        c2 = self._close_history_value(state=state, minute_ts=current_minute - _dt.timedelta(minutes=2))
        c3 = self._close_history_value(state=state, minute_ts=current_minute - _dt.timedelta(minutes=3))
        if None in {c0, c1, c2, c3}:
            return False
        bar_th = self._extra_float("drift_bar_min_atr", 0.10) * float(atr_eff)
        cum_th = self._extra_float("drift_cum_atr_min", 0.45) * float(atr_eff)
        d1 = float(c0) - float(c1)
        d2 = float(c1) - float(c2)
        d3 = float(c2) - float(c3)
        cum = float(c0) - float(c3)
        if side == "LONG":
            return d1 >= bar_th and d2 >= bar_th and d3 >= bar_th and cum >= cum_th
        return (-d1) >= bar_th and (-d2) >= bar_th and (-d3) >= bar_th and (-cum) >= cum_th

    def _close_history_value(self, *, state: ReplicaTradeState, minute_ts: datetime) -> float | None:
        return market_history_value(
            rows=list(state.extras.get("primary_close_history") or []),
            minute_ts=minute_ts,
            field="close",
        )

    def _vwap_history_value(self, *, state: ReplicaTradeState, minute_ts: datetime) -> float | None:
        return market_history_value(
            rows=list(state.extras.get("primary_vwap_history") or []),
            minute_ts=minute_ts,
            field="vwap",
        )

    def _vol_surge_history_value(self, *, state: ReplicaTradeState, minute_ts: datetime) -> float | None:
        return market_history_value(
            rows=list(state.extras.get("primary_vol_surge_history") or []),
            minute_ts=minute_ts,
            field="vol_surge_ratio",
        )

    def _live_sample_value_at_or_before(self, *, state: ReplicaTradeState, target_ts: datetime) -> float | None:
        return market_live_value_at_or_before(
            rows=list(state.extras.get("primary_live_samples") or []),
            target_ts=target_ts,
        )

    def _exec_price_from_features_or_cache(self, *, exec_symbol: str, features: Dict[str, Any]) -> float | None:
        symbol_u = str(exec_symbol or "").strip().upper()
        if not symbol_u:
            return None
        prefix = symbol_u.lower().replace("-", "_")
        for key in (
            f"{prefix}_close_now",
            f"{prefix}_last_price",
            f"{prefix}_price",
            "exec_price",
            "exec_last_px",
            "exec_close",
        ):
            value = self._optional_float(features.get(key))
            if value is not None and value > 0:
                return value
        try:
            snap = self.get_price(symbol_u)
        except Exception:
            snap = None
        if snap is None:
            return None
        for attr in ("last", "price", "mid", "close", "mark"):
            value = self._optional_float(getattr(snap, attr, None))
            if value is not None and value > 0:
                return value
        if isinstance(snap, dict):
            for key in ("last", "price", "mid", "close", "mark"):
                value = self._optional_float(snap.get(key))
                if value is not None and value > 0:
                    return value
        return None

    @staticmethod
    def _signal_block_reason(decision: SignalDecision) -> str:
        return normalize_signal_block_reason(getattr(decision, "reason", ""))

    def _capture_feature_context(self, state: ReplicaTradeState, decision: Any, features: Dict[str, Any]) -> None:
        apply_feature_context(
            state=state,
            decision_signal_price=getattr(decision, "signal_price", None),
            feature_price=self._feature_price(features),
            features=features,
            optional_float=self._optional_float,
            now_et=datetime.now(ET),
        )

    def _restore_replica_runtime(self) -> None:
        payload = self.state.get("replica_runtime")
        if not isinstance(payload, dict):
            return
        for symbol, saved in payload.items():
            state = self._symbol_state.get(str(symbol or "").strip().upper())
            if state is None or not isinstance(saved, dict):
                continue
            restore_runtime_state_row(state=state, saved=saved, optional_float=self._optional_float)
            extras = saved.get("extras")
            if isinstance(extras, dict):
                restored = restore_runtime_extras(extras, coerce_datetime=self._coerce_datetime)
                state.extras.update(restored)
            self._last_preview_by_symbol[state.signal_symbol] = {
                "status": "RESTART_STATE_RESTORED",
                "exec_symbol": state.active_exec_symbol,
                "is_traded": state.is_traded,
                "stop_px": state.stop_px,
                "live_qty": int(max(0, self._optional_float(state.extras.get("live_qty")) or state.expected_entry_qty or 0)),
            }

    def _restore_ownership_ledger(self) -> None:
        payload = self.state.get("ownership_ledger")
        if isinstance(payload, dict):
            self._ownership_ledger = StockOwnershipLedger.from_snapshot(payload)

    def _refresh_ownership_snapshot(self) -> None:
        self.state["ownership_ledger"] = self._ownership_ledger.snapshot()

    def _resolve_runtime_snapshot_path(self, extras: Dict[str, Any]) -> Path:
        raw = str(extras.get("runtime_snapshot_path") or "").strip()
        if raw:
            path = Path(raw)
        else:
            path = Path("runtime") / "state" / f"index_super_win_replica_runtime_{self.strategy_id}.json"
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    def _runtime_snapshot_payload(self) -> Dict[str, Any]:
        return build_runtime_snapshot_payload(
            strategy_id=self.strategy_id,
            generated_at_et=datetime.now(ET).isoformat(),
            ownership_snapshot=self._ownership_ledger.snapshot(),
            symbol_state=self._symbol_state,
        )

    def _persist_runtime_snapshot(self) -> None:
        path = self._runtime_snapshot_path
        if path is None:
            return
        self._refresh_ownership_snapshot()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._runtime_snapshot_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.state["runtime_snapshot_path"] = str(path)
        except Exception:
            logger.exception(
                "index_super_win_replica_runtime_snapshot_write_failed",
                strategy_id=self.strategy_id,
                path=str(path),
            )

    @staticmethod
    def _clear_trade_runtime_state(state: ReplicaTradeState) -> None:
        state.is_traded = False
        state.is_flattening = False
        state.pending_decision_id = None
        state.active_trigger_id = None
        state.expected_entry_qty = 0
        state.entry_order_ref = None
        state.active_side = None
        state.active_exec_symbol = None
        state.entry_px = None
        state.stop_px = None
        state.entry_atr_used = None
        state.tp1_hit = False
        state.trail_high_px = None
        state.trail_floor_px = None
        state.breakeven_done = False
        state.extras["live_qty"] = 0
        state.extras["protection_active"] = False
        state.extras["protection_pending"] = False
        state.extras["kill_switch_pending"] = False
        state.extras["round_live_confirmed"] = False
        state.extras["pending_tp1_qty"] = 0
        state.extras["entry_time_et"] = None
        state.extras["trade_target_px"] = None
        state.extras["add_done"] = False
        state.extras["add_deadline_et"] = None
        state.extras["entry_ref_px"] = None
        state.extras["stop_dist_pre"] = None
        state.extras["stop_dist_cur"] = None
        state.extras["stop_dist_max"] = None
        state.extras["active_position_lot_id"] = None
        state.extras["sync_quarantine_reason"] = None
        state.extras["sync_quarantine_payload"] = None
        state.extras["session_end_flatten_pending"] = False
        state.extras["session_end_flatten_reason"] = None
        state.extras["session_end_flatten_submit_sig"] = None
        state.extras["session_end_flatten_submit_mono"] = None
        IndexSuperWinReplicaStrategy._clear_pending_probe_state(state)
        IndexSuperWinReplicaStrategy._clear_entry_timeout_cooldown(state)
        IndexSuperWinReplicaStrategy._clear_zero_position_pending(state)

    def _normalize_live_trade_runtime(self, state: ReplicaTradeState) -> None:
        entry_px = self._optional_float(state.entry_px)
        stop_px = self._optional_float(state.stop_px)
        entry_atr_used = self._optional_float(state.entry_atr_used)
        if (
            entry_px is not None
            and entry_px > 0
            and stop_px is not None
            and stop_px > 0
            and self._optional_float(state.extras.get("stop_dist_pre")) is None
        ):
            dist = abs(float(entry_px) - float(stop_px))
            if dist > 0:
                state.extras["stop_dist_pre"] = float(dist)
                state.extras["stop_dist_cur"] = float(dist)
                if self._optional_float(state.extras.get("stop_dist_max")) is None:
                    state.extras["stop_dist_max"] = float(dist) * 1.5
        if (
            entry_px is not None
            and entry_px > 0
            and entry_atr_used is not None
            and entry_atr_used > 0
            and self._optional_float(state.extras.get("trade_target_px")) is None
        ):
            state.extras["trade_target_px"] = round(float(entry_px) + float(entry_atr_used), 2)
        if state.extras.get("add_done") is None:
            state.extras["add_done"] = False

    async def _ensure_restart_live_protection(self) -> None:
        for symbol_u, state in self._symbol_state.items():
            live_qty = int(max(0, self._optional_float(state.extras.get("live_qty")) or state.expected_entry_qty or 0))
            if not state.is_traded or state.is_flattening or live_qty <= 0:
                continue
            if state.stop_px is None or float(state.stop_px or 0.0) <= 0.0:
                rebuilt_stop_px = self._rebuild_stop_px_from_runtime_state(symbol_u=symbol_u, state=state)
                if rebuilt_stop_px is not None and rebuilt_stop_px > 0:
                    self._emit_local_audit(
                        stage="restart_protective_stop_stop_rebuilt",
                        domain="recon",
                        severity="warning",
                        status="rebuilt",
                        reason="rebuild_stop_after_restart",
                        symbol=symbol_u,
                        payload={
                            "exec_symbol": state.active_exec_symbol,
                            "entry_px": state.entry_px,
                            "entry_atr_used": state.entry_atr_used,
                            "stop_px": rebuilt_stop_px,
                            "stop_dist_pre": state.extras.get("stop_dist_pre"),
                        },
                    )
                    self._persist_runtime_snapshot()
                else:
                    await self._kill_switch_unprotected_live(
                        symbol_u=symbol_u,
                        state=state,
                        live_qty=live_qty,
                        reason="restart_unprotected_no_stop_px",
                        status_prefix="RESTART_UNPROTECTED",
                    )
                    continue
            await self._heal_missing_protection(
                symbol_u=symbol_u,
                state=state,
                live_qty=live_qty,
                reason="rebuild_stop_after_restart",
                status_prefix="RESTART_PROTECTIVE_STOP",
            )
        self._ensure_stop_audit_task_running()

    def _has_live_protection_candidates(self) -> bool:
        for state in self._symbol_state.values():
            live_qty = int(max(0, self._optional_float(state.extras.get("live_qty")) or state.expected_entry_qty or 0))
            if state.is_traded and (not state.is_flattening) and live_qty > 0:
                return True
        return False

    def _ensure_stop_audit_task_running(self) -> None:
        if not bool(self._extras.get("submit_exchange_stop_intent", True)):
            return
        if self._dry_run:
            return
        if not self._has_live_protection_candidates():
            return
        if self._stop_audit_task is not None and not self._stop_audit_task.done():
            self._stop_audit_started = True
            return
        self._stop_audit_task = asyncio.create_task(
            self._audit_stop_order_loop(),
            name=f"index_super_win_stop_audit:{self.strategy_id}",
        )
        self._stop_audit_started = True

    async def _audit_stop_order_loop(self) -> None:
        interval_sec = max(1.0, self._extra_float("stop_audit_interval_sec", 1.2))
        loop = asyncio.get_running_loop()
        startup_grace_until_mono = stop_audit_startup_grace_until(
            loop_now_mono=loop.time(),
            startup_grace_sec=self._extra_float("stop_audit_startup_grace_sec", 6.0),
        )
        try:
            while getattr(self, "_running", True):
                await asyncio.sleep(interval_sec)
                if not self._has_live_protection_candidates():
                    if loop.time() < startup_grace_until_mono:
                        continue
                    break
                await self.audit_live_protection()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "index_super_win_replica_stop_audit_loop_failed",
                strategy_id=self.strategy_id,
                error=str(exc),
            )
            self._emit_local_audit(
                stage="stop_audit_exception",
                domain="recon",
                severity="warning",
                status="degraded",
                reason="stop_audit_loop_exception",
                payload={"error": str(exc)},
            )
        finally:
            self._stop_audit_started = False
            self._stop_audit_task = None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _extra_float(self, key: str, default: float) -> float:
        value = self._optional_float(self._extras.get(key))
        return float(default) if value is None else float(value)

    def _extra_int(self, key: str, default: int) -> int:
        raw = self._extras.get(key)
        if raw is None:
            return int(default)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(default)

    def _feature_price(self, features: Dict[str, Any]) -> float | None:
        for key in ("exec_price", "signal_price", "last_price", "close", "price"):
            value = self._optional_float(features.get(key))
            if value is not None and value > 0:
                return value
        return None

    def _feature_now_et(self, features: Dict[str, Any]) -> datetime:
        return resolve_feature_now_et(
            features,
            tz=ET,
            coerce_datetime_fn=self._coerce_datetime,
            parse_hms_tuple_fn=lambda v: self._parse_hms_tuple(v, fallback=(0, 0, 0)),
            now_fn=datetime.now,
        )

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        return coerce_feature_datetime(value)

    @staticmethod
    def _coerce_timestamp_us(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, datetime):
            dt = value if value.tzinfo is not None else value.replace(tzinfo=ET)
            return int(dt.timestamp() * 1_000_000)
        if isinstance(value, str):
            parsed = coerce_feature_datetime(value)
            if parsed is not None:
                return int(parsed.timestamp() * 1_000_000)
            try:
                value = float(value.strip())
            except (TypeError, ValueError):
                return 0
        if isinstance(value, (int, float)):
            raw = float(value)
            if raw > 1e14:
                return int(raw)
            if raw > 1e11:
                return int(raw * 1_000.0)
            if raw > 1e9:
                return int(raw * 1_000_000.0)
        return 0

    @classmethod
    def _feature_ts_us(cls, features: Dict[str, Any]) -> int:
        for key in ("ts_bar_end_us", "bar_end_ts_us", "feature_ts_us", "timestamp_us", "ts_us", "ts_end_us", "ts_start_us", "ts"):
            ts_us = cls._coerce_timestamp_us(features.get(key))
            if ts_us > 0:
                return ts_us
        return 0

    @staticmethod
    def _feature_symbol_prefix(symbol: Any) -> str:
        return str(symbol or "").strip().lower().replace("-", "_")

    def _normalize_binding_freshness_features(
        self,
        *,
        features: Dict[str, Any],
        binding: Any,
        eval_now: datetime,
    ) -> Dict[str, Any]:
        ts_us = self._feature_ts_us(features)
        if ts_us <= 0:
            return features
        age_sec = max(0.0, (int(eval_now.timestamp() * 1_000_000) - ts_us) / 1_000_000.0)
        normalized = features
        for field in ("signal_symbol", "confirm_symbol"):
            symbol_u = str(getattr(binding, field, "") or "").strip().upper()
            if not symbol_u:
                continue
            prefix = self._feature_symbol_prefix(symbol_u)
            if self._optional_float(features.get(f"{prefix}_age_sec")) is not None:
                continue
            if self._optional_float(features.get(f"freshness_age_sec_{prefix}")) is not None:
                continue
            if normalized is features:
                normalized = dict(features)
            normalized[f"{prefix}_age_sec"] = float(age_sec)
        return normalized

    @staticmethod
    def _parse_hms_tuple(value: Any, *, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
        return parse_feature_hms_tuple(value, fallback=fallback)

    @staticmethod
    def _trader_reason_code(reason: Any) -> str:
        return map_trader_reason_code(reason)

    @staticmethod
    def _entry_block_payload(
        *,
        symbol: str,
        exec_symbol: str | None,
        reason: Any,
        trader_reason_code: str,
        gate_details: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        return build_entry_block_payload(
            symbol=symbol,
            exec_symbol=exec_symbol,
            reason=reason,
            trader_reason_code_value=trader_reason_code,
            gate_details=gate_details,
        )

    def _load_runtime_snapshot_payload(self, path: Path | None) -> Dict[str, Any]:
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        replica_runtime = payload.get("replica_runtime")
        return dict(replica_runtime) if isinstance(replica_runtime, dict) else dict(payload)

    async def _previous_session_open_orders_clear(self, *, binding: Any) -> tuple[bool, bool]:
        if not self._require_prev_session_flat:
            return True, True
        prev_strategy_id = str(self._prev_session_strategy_id or "").strip()
        if not prev_strategy_id:
            return False, False
        open_orders = getattr(self.ctx, "open_orders", None)
        if open_orders is None:
            return False, False
        try:
            orders = await open_orders()
        except Exception:
            return False, False
        exec_symbols = {
            str(getattr(binding, "long_exec_symbol", "") or "").strip().upper(),
            str(getattr(binding, "short_exec_symbol", "") or "").strip().upper(),
        }
        namespace = str(self._prev_session_order_namespace or self._entry_core.order_namespace).strip()
        for order in list(orders or []):
            if self._is_terminal_order_state(order):
                continue
            order_symbol = get_order_symbol(order)
            if order_symbol and order_symbol not in exec_symbols:
                continue
            order_strategy_id = get_order_strategy_id(order)
            order_ref = get_order_ref(order)
            if order_strategy_id == prev_strategy_id:
                return False, True
            for exec_symbol in exec_symbols:
                prefix = f"{namespace}:{prev_strategy_id}:{exec_symbol}:"
                if exec_symbol and order_ref.startswith(prefix):
                    return False, True
        return True, True

    async def _evaluate_session_entry_gate(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        binding: Any,
        decision: SignalDecision,
        eval_now: datetime,
    ) -> dict[str, Any] | None:
        window_gate = session_window_gate(
            now_et=eval_now,
            start_hms=self._session_start_hms,
            end_hms=self._session_end_hms,
            enforce_entry_window=self._enforce_entry_session_window,
        )
        if not window_gate.allowed:
            self._record_entry_block(
                symbol=symbol_u,
                exec_symbol=str(decision.exec_symbol or "").strip().upper() or None,
                side=decision.side,
                qty=int(max(0, decision.target_qty)),
                reason=window_gate.reason,
                trader_reason_code=self._trader_reason_code(window_gate.reason),
                gate_details={
                    "session_name": self._session_name or None,
                    "session_start_et": None if self._session_start_hms is None else "%02d:%02d:%02d" % self._session_start_hms,
                    "session_end_et": None if self._session_end_hms is None else "%02d:%02d:%02d" % self._session_end_hms,
                    "eval_now_et": eval_now.isoformat(),
                },
                block_stage="session_gate_blocked",
            )
            return self._last_preview_by_symbol.get(symbol_u)
        if not self._require_prev_session_flat:
            return None
        runtime_check_ok = self._prev_session_runtime_snapshot_path is not None
        runtime_flat = False
        if runtime_check_ok:
            prev_runtime = self._load_runtime_snapshot_payload(self._prev_session_runtime_snapshot_path)
            prev_state = dict(prev_runtime.get(symbol_u) or {})
            prev_extras = prev_state.get("extras") or {}
            prev_live_qty = int(max(0, self._optional_float(prev_extras.get("live_qty")) or prev_state.get("expected_entry_qty") or 0))
            runtime_flat = (not bool(prev_state.get("is_traded", False))) and prev_live_qty <= 0
        broker_check_ok = True
        broker_flat = True
        exec_symbols = [
            str(getattr(binding, "long_exec_symbol", "") or "").strip().upper(),
            str(getattr(binding, "short_exec_symbol", "") or "").strip().upper(),
        ]
        for exec_symbol in {item for item in exec_symbols if item}:
            qty, err = await self._read_ctx_position_qty(exec_symbol)
            if err is not None:
                broker_check_ok = False
                broker_flat = False
                break
            if abs(float(qty or 0.0)) > self._entry_broker_position_guard_min_abs:
                broker_flat = False
        open_orders_clear, open_orders_check_ok = await self._previous_session_open_orders_clear(binding=binding)
        prev_gate = previous_session_gate(
            require_prev_session_flat=self._require_prev_session_flat,
            runtime_flat=runtime_flat,
            broker_flat=broker_flat,
            open_orders_clear=open_orders_clear,
            runtime_check_ok=runtime_check_ok,
            broker_check_ok=broker_check_ok,
            open_orders_check_ok=open_orders_check_ok,
        )
        if prev_gate.allowed:
            self._emit_local_audit(
                stage="session_handover_ready",
                domain="recon",
                status="ready",
                reason=prev_gate.reason,
                symbol=symbol_u,
                payload={
                    "session_name": self._session_name or None,
                    "prev_session_strategy_id": self._prev_session_strategy_id,
                },
            )
            return None
        self._record_entry_block(
            symbol=symbol_u,
            exec_symbol=str(decision.exec_symbol or "").strip().upper() or None,
            side=decision.side,
            qty=int(max(0, decision.target_qty)),
            reason=prev_gate.reason,
            trader_reason_code=self._trader_reason_code(prev_gate.reason),
            gate_details={
                "session_name": self._session_name or None,
                "prev_session_strategy_id": self._prev_session_strategy_id,
                "prev_session_runtime_snapshot_path": str(self._prev_session_runtime_snapshot_path) if self._prev_session_runtime_snapshot_path else None,
                "runtime_flat": runtime_flat,
                "runtime_check_ok": runtime_check_ok,
                "broker_flat": broker_flat,
                "broker_check_ok": broker_check_ok,
                "open_orders_clear": open_orders_clear,
                "open_orders_check_ok": open_orders_check_ok,
            },
            block_stage="previous_session_not_flat_blocked",
        )
        return self._last_preview_by_symbol.get(symbol_u)

    async def _heal_missing_protection(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        live_qty: int,
        reason: str,
        status_prefix: str,
    ) -> None:
        assert self._entry_core is not None
        stop_px = self._optional_float(state.stop_px)
        if stop_px is None or stop_px <= 0:
            rebuilt_stop_px = self._rebuild_stop_px_from_runtime_state(symbol_u=symbol_u, state=state)
            if rebuilt_stop_px is not None and rebuilt_stop_px > 0:
                stop_px = rebuilt_stop_px
                self._emit_local_audit(
                    stage=f"{status_prefix.lower()}_stop_rebuilt",
                    domain="recon",
                    severity="warning",
                    status="rebuilt",
                    reason=reason,
                    symbol=symbol_u,
                    payload=protection_stop_rebuilt_payload(
                        exec_symbol=state.active_exec_symbol,
                        entry_px=state.entry_px,
                        entry_atr_used=state.entry_atr_used,
                        stop_px=stop_px,
                        stop_dist_pre=self._optional_float(state.extras.get("stop_dist_pre")),
                    ),
                )
                self._persist_runtime_snapshot()
            else:
                await self._kill_switch_unprotected_live(
                    symbol_u=symbol_u,
                    state=state,
                    live_qty=live_qty,
                    reason=f"{reason}_no_stop_px",
                    status_prefix=status_prefix,
                )
                return
        if self._skip_duplicate_protection_action(
            symbol_u=symbol_u,
            state=state,
            live_qty=live_qty,
            stop_px=stop_px,
            reason=reason,
            status_prefix=status_prefix,
        ):
            return
        state, intent, envelope = self._entry_core.build_protective_stop_intent(
            state=state,
            qty_signed=signed_position_qty(live_qty=live_qty, side=state.active_side),
            reason=reason,
            stop_px=stop_px,
            now=datetime.now(ET),
        )
        self._symbol_state[symbol_u] = state
        if self._dry_run:
            state.extras["protection_active"] = True
            state.extras["protection_pending"] = True
            state.extras["last_protection_fingerprint"] = self._protection_fingerprint(
                state=state,
                live_qty=live_qty,
                stop_px=stop_px,
                reason=reason,
                status_prefix=status_prefix,
            )
            state.extras["last_protection_submit_sig"] = stop_submit_signature(
                exec_symbol=str(state.active_exec_symbol or ""),
                qty=int(max(0, live_qty)),
                stop_px=float(stop_px),
            )
            state.extras["last_protection_submit_mono"] = self._loop_time()
            self._last_preview_by_symbol[symbol_u] = preview_row(
                status=f"{status_prefix}_PREVIEW",
                reason=reason,
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra={"stop_px": intent.stop_px, "live_qty": live_qty},
            )
            self._emit_local_audit(
                stage=f"{status_prefix.lower()}_preview",
                status="preview",
                reason=reason,
                symbol=symbol_u,
                payload=protection_order_payload(
                    exec_symbol=intent.symbol,
                    order_ref=intent.order_ref,
                    stop_px=intent.stop_px,
                    live_qty=live_qty,
                ),
            )
            return
        submit_result = await self.ctx.submit_intent(envelope)
        status, _ = self._entry_core.extract_submit_status(submit_result)
        outcome = submit_outcome(
            status=status,
            ok_status=f"{status_prefix}_SUBMITTED",
            rejected_status=f"{status_prefix}_REJECTED",
            ok_reason=reason,
        )
        state.extras["protection_active"] = outcome.approved
        state.extras["protection_pending"] = outcome.approved
        state.extras["last_protection_fingerprint"] = self._protection_fingerprint(
            state=state,
            live_qty=live_qty,
            stop_px=stop_px,
            reason=reason,
            status_prefix=status_prefix,
        )
        state.extras["last_protection_submit_sig"] = stop_submit_signature(
            exec_symbol=str(state.active_exec_symbol or ""),
            qty=int(max(0, live_qty)),
            stop_px=float(stop_px),
        )
        state.extras["last_protection_submit_mono"] = self._loop_time()
        self._last_preview_by_symbol[symbol_u] = preview_row_from_outcome(
            outcome=outcome,
            exec_symbol=intent.symbol,
            order_ref=intent.order_ref,
            extra={"stop_px": intent.stop_px, "live_qty": live_qty},
        )
        self._emit_local_audit(
            stage=outcome.audit_stage,
            severity=outcome.audit_severity,
            status=outcome.audit_status,
            reason=outcome.reason,
            symbol=symbol_u,
            payload=protection_order_payload(
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                stop_px=intent.stop_px,
                live_qty=live_qty,
            ),
        )

    def _rebuild_stop_px_from_runtime_state(self, *, symbol_u: str, state: ReplicaTradeState) -> float | None:
        entry_px = self._optional_float(state.entry_px)
        if entry_px is None or entry_px <= 0:
            return None
        stop_dist_pre = self._optional_float(state.extras.get("stop_dist_pre"))
        stop_dist_cur = self._optional_float(state.extras.get("stop_dist_cur"))
        dist = stop_dist_pre if stop_dist_pre is not None and stop_dist_pre > 0 else None
        if dist is None and stop_dist_cur is not None and stop_dist_cur > 0:
            dist = stop_dist_cur
        if dist is None:
            entry_atr_used = self._optional_float(state.entry_atr_used)
            stop_min_abs = self._optional_float(state.extras.get("stop_min_abs"))
            stop_atr_mult = self._optional_float(state.extras.get("stop_atr_mult"))
            stop_min_abs = stop_min_abs if stop_min_abs is not None and stop_min_abs > 0 else 0.50
            stop_atr_mult = stop_atr_mult if stop_atr_mult is not None and stop_atr_mult > 0 else 2.5
            if entry_atr_used is None or entry_atr_used <= 0:
                return None
            dist = max(stop_atr_mult * entry_atr_used, stop_min_abs)
            state.extras["stop_dist_pre"] = float(dist)
            state.extras["stop_dist_max"] = float(dist) * 1.5
            state.extras["stop_dist_cur"] = float(dist)
        elif stop_dist_pre is None or stop_dist_pre <= 0:
            state.extras["stop_dist_pre"] = float(dist)
            if self._optional_float(state.extras.get("stop_dist_max")) is None:
                state.extras["stop_dist_max"] = float(dist) * 1.5
            if self._optional_float(state.extras.get("stop_dist_cur")) is None:
                state.extras["stop_dist_cur"] = float(dist)
        stop_px = round(float(entry_px) - float(dist), 2)
        state.stop_px = stop_px
        logger.warning(
            "index_super_win_replica_rebuilt_stop_after_restart",
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            exec_symbol=state.active_exec_symbol,
            side=state.active_side,
            entry_px=round(float(entry_px), 4),
            stop_px=round(float(stop_px), 4),
            stop_dist=round(float(dist), 4),
        )
        return stop_px

    async def _kill_switch_unprotected_live(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        live_qty: int,
        reason: str,
        status_prefix: str,
    ) -> None:
        assert self._entry_core is not None
        if not bool(self._extras.get("kill_switch_on_unprotected_live", True)):
            self._last_preview_by_symbol[symbol_u] = {
                "status": f"{status_prefix}_BLOCKED",
                "reason": "missing_stop_px",
                "exec_symbol": state.active_exec_symbol,
                "live_qty": live_qty,
            }
            self._emit_local_audit(
                stage=f"{status_prefix.lower()}_blocked",
                severity="warning",
                status="blocked",
                reason="missing_stop_px",
                symbol=symbol_u,
                payload=kill_switch_order_payload(
                    exec_symbol=state.active_exec_symbol,
                    order_ref=None,
                    qty=None,
                    live_qty=live_qty,
                ),
            )
            return
        if self._skip_duplicate_kill_switch_action(
            symbol_u=symbol_u,
            state=state,
            live_qty=live_qty,
            reason=reason,
            status_prefix=status_prefix,
        ):
            return
        qty_signed = signed_position_qty(live_qty=live_qty, side=state.active_side)
        state, intent, envelope = self._entry_core.build_flatten_intent(
            state=state,
            qty_signed=qty_signed,
            reason=reason,
            now=datetime.now(ET),
        )
        self._symbol_state[symbol_u] = state
        if self._dry_run:
            state.extras["kill_switch_pending"] = True
            state.extras["last_kill_switch_fingerprint"] = self._kill_switch_fingerprint(
                state=state,
                live_qty=live_qty,
                reason=reason,
                status_prefix=status_prefix,
            )
            state.extras["last_kill_switch_submit_sig"] = action_submit_signature(
                exec_symbol=str(state.active_exec_symbol or ""),
                qty=int(max(0, live_qty)),
                action=f"{status_prefix}_KILL_SWITCH",
            )
            state.extras["last_kill_switch_submit_mono"] = self._loop_time()
            self._last_preview_by_symbol[symbol_u] = preview_row(
                status=f"{status_prefix}_KILL_SWITCH_PREVIEW",
                reason=reason,
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra={"qty": intent.target_qty, "live_qty": live_qty},
            )
            self._emit_local_audit(
                stage=f"{status_prefix.lower()}_kill_switch_preview",
                severity="warning",
                status="preview",
                reason=reason,
                symbol=symbol_u,
                payload=kill_switch_order_payload(
                    exec_symbol=intent.symbol,
                    order_ref=intent.order_ref,
                    qty=int(intent.target_qty),
                    live_qty=live_qty,
                ),
            )
            return
        submit_result = await self.ctx.submit_intent(envelope)
        status, _ = self._entry_core.extract_submit_status(submit_result)
        outcome = submit_outcome(
            status=status,
            ok_status=f"{status_prefix}_KILL_SWITCH_SUBMITTED",
            rejected_status=f"{status_prefix}_KILL_SWITCH_REJECTED",
            ok_reason=reason,
        )
        state.extras["kill_switch_pending"] = outcome.approved
        state.extras["last_kill_switch_fingerprint"] = self._kill_switch_fingerprint(
            state=state,
            live_qty=live_qty,
            reason=reason,
            status_prefix=status_prefix,
        )
        state.extras["last_kill_switch_submit_sig"] = action_submit_signature(
            exec_symbol=str(state.active_exec_symbol or ""),
            qty=int(max(0, live_qty)),
            action=f"{status_prefix}_KILL_SWITCH",
        )
        state.extras["last_kill_switch_submit_mono"] = self._loop_time()
        self._last_preview_by_symbol[symbol_u] = preview_row_from_outcome(
            outcome=outcome,
            exec_symbol=intent.symbol,
            order_ref=intent.order_ref,
            extra={"qty": intent.target_qty, "live_qty": live_qty},
        )
        self._emit_local_audit(
            stage=outcome.audit_stage,
            severity="warning",
            status=outcome.audit_status,
            reason=outcome.reason,
            symbol=symbol_u,
            payload=kill_switch_order_payload(
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                qty=int(intent.target_qty),
                live_qty=live_qty,
            ),
        )

    async def _has_live_protective_stop_order(self, *, symbol_u: str, state: ReplicaTradeState) -> bool | None:
        open_orders = getattr(self.ctx, "open_orders", None)
        if open_orders is None:
            return None
        try:
            orders = await open_orders()
        except Exception:
            return None
        active_symbol = str(state.active_exec_symbol or "").strip().upper()
        if not active_symbol:
            return False
        owned_prefix = f"{self._entry_core.order_namespace}:{self.strategy_id}:{active_symbol}:"
        for order in list(orders or []):
            if self._is_terminal_order_state(order):
                continue
            order_ref = get_order_ref(order)
            if not order_ref.startswith(owned_prefix):
                continue
            if not self._is_protective_order_ref_or_type(order=order, order_ref=order_ref):
                continue
            return True
        return False

    @staticmethod
    def _order_type_name(order: Any) -> str:
        for field in ("order_type", "orderType", "type"):
            value = getattr(order, field, None)
            if value is not None:
                text = str(value or "").strip().upper().replace(" ", "_")
                if text:
                    return text
        return ""

    @classmethod
    def _is_protective_order_ref_or_type(cls, *, order: Any, order_ref: str) -> bool:
        ref_u = str(order_ref or "").upper()
        if ":STOP" in ref_u or ":TRAIL" in ref_u or ":TRAIL_LIMIT" in ref_u:
            return True
        return cls._order_type_name(order) in {"STP", "STP_LMT", "TRAIL", "TRAIL_LIMIT"}

    async def _find_live_protective_order_id(self, *, symbol_u: str, state: ReplicaTradeState, prefer_fixed_stop: bool = True) -> int | None:
        orders = await self._fetch_open_orders()
        if orders is None:
            return None
        active_symbol = str(state.active_exec_symbol or "").strip().upper()
        if not active_symbol:
            return None
        owned_prefix = f"{self._entry_core.order_namespace}:{self.strategy_id}:{active_symbol}:"
        fallback_id: int | None = None
        for order in list(orders or []):
            if self._is_terminal_order_state(order):
                continue
            order_ref = get_order_ref(order)
            if not order_ref.startswith(owned_prefix):
                continue
            if not self._is_protective_order_ref_or_type(order=order, order_ref=order_ref):
                continue
            cancel_id = self._cancel_id_from_open_order(order)
            if cancel_id is None:
                continue
            order_type = self._order_type_name(order)
            if fallback_id is None:
                fallback_id = int(cancel_id)
            if prefer_fixed_stop and (":STOP" in order_ref.upper() or order_type in {"STP", "STP_LMT"}):
                return int(cancel_id)
        return fallback_id

    async def _submit_native_runner_trail_after_tp1(self, *, symbol_u: str, state: ReplicaTradeState, live_qty: int) -> None:
        assert self._entry_core is not None
        if not bool(self._extras.get("runner_native_trail_enabled", True)):
            return
        if bool(state.extras.get("native_trail_active")) or bool(state.extras.get("native_trail_pending")):
            return
        if live_qty <= 0 or not state.active_exec_symbol or not state.active_side:
            return
        trail_stop_px = self._optional_float(state.trail_floor_px) or self._optional_float(state.stop_px)
        trail_percent = self._optional_float(self._extras.get("runner_trail_percent"))
        trail_amount: float | None = None
        if trail_percent is None or trail_percent <= 0.0:
            trail_percent = None
            atr = self._optional_float(state.entry_atr_used)
            atr_mult = self._extra_float("runner_trail_atr_mult", self._extra_float("trail_atr_mult", 2.5))
            basis = float(atr or 0.0) * atr_mult
            trail_high = self._optional_float(state.trail_high_px)
            if trail_high is not None and trail_stop_px is not None:
                basis = max(basis, abs(trail_high - trail_stop_px))
            trail_amount = max(self._extra_float("runner_trail_min_abs", 0.01), basis)

        mode = str(self._extras.get("runner_trail_mode") or "TRAIL").strip().upper().replace(" ", "_")
        use_trail_limit = mode in {"TRAIL_LIMIT", "NATIVE_TRAIL_LIMIT"} and bool(self._extras.get("trail_limit_enabled", False))
        lmt_price_offset: float | None = None
        if use_trail_limit:
            lmt_price_offset = self._optional_float(self._extras.get("trail_limit_offset_abs"))
            if lmt_price_offset is None or lmt_price_offset <= 0.0:
                bps = self._extra_float("trail_limit_offset_bps", 5.0)
                ref_px = (
                    self._optional_float(state.trail_high_px)
                    or self._optional_float(state.entry_px)
                    or self._optional_float(state.stop_px)
                    or 0.0
                )
                lmt_price_offset = max(0.01, float(ref_px) * bps / 10_000.0) if ref_px > 0.0 else 0.01

        spec = self._build_runner_protection_spec_after_tp1(
            state=state,
            live_qty=live_qty,
            trail_amount=trail_amount,
            trail_percent=trail_percent,
            trail_stop_px=trail_stop_px,
            lmt_price_offset=lmt_price_offset,
            use_trail_limit=use_trail_limit,
        )
        qty_signed = signed_position_qty(live_qty=live_qty, side=state.active_side)
        reason = "tp1_runner_native_trail_limit" if use_trail_limit else "tp1_runner_native_trail"
        old_order_id = await self._find_live_protective_order_id(symbol_u=symbol_u, state=state, prefer_fixed_stop=True)
        state, intent, envelope = self._entry_core.build_protective_trailing_intent(
            state=state,
            qty_signed=qty_signed,
            reason=reason,
            trail_amount=spec.desired_trail_amount,
            trail_percent=spec.desired_trail_percent,
            trail_stop_px=spec.desired_stop_px,
            lmt_price_offset=spec.desired_limit_offset,
            now=datetime.now(ET),
        )
        envelope.extras["protection_spec_mode"] = spec.mode
        envelope.extras["protection_position_id"] = spec.position_id
        envelope.extras["runner_qty"] = float(spec.runner_qty or spec.qty)
        if old_order_id is not None:
            envelope.extras["replace_protective_order_id"] = int(old_order_id)
        state.extras["native_trail_pending"] = True
        state.extras["protection_pending"] = True
        self._symbol_state[symbol_u] = state

        audit_payload = {
            "exec_symbol": intent.symbol,
            "order_ref": intent.order_ref,
            "qty": intent.target_qty,
            "live_qty": live_qty,
            "old_order_id": old_order_id,
            "trail_amount": intent.trail_amount,
            "trail_percent": intent.trail_percent,
            "trail_stop_px": intent.trail_stop_px,
            "lmt_price_offset": intent.lmt_price_offset,
            "protection_spec_mode": spec.mode,
            "protection_position_id": spec.position_id,
        }
        if self._dry_run:
            self._last_preview_by_symbol[symbol_u] = preview_row(
                status="RUNNER_NATIVE_TRAIL_PREVIEW",
                reason=reason,
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra=audit_payload,
            )
            self._emit_local_audit(
                stage="runner_native_trail_preview",
                status="preview",
                reason=reason,
                symbol=symbol_u,
                payload=audit_payload,
            )
            return

        try:
            submit_result = await self.ctx.submit_intent(envelope)
        except Exception as exc:
            state.extras["native_trail_pending"] = False
            state.extras["protection_pending"] = False
            self._last_preview_by_symbol[symbol_u] = preview_row(
                status="RUNNER_NATIVE_TRAIL_REJECTED",
                reason="submit_exception",
                exec_symbol=intent.symbol,
                order_ref=intent.order_ref,
                extra={**audit_payload, "error": str(exc)},
            )
            self._emit_local_audit(
                stage="runner_native_trail_rejected",
                severity="warning",
                status="rejected",
                reason="submit_exception",
                symbol=symbol_u,
                payload={**audit_payload, "error": str(exc)},
            )
            return
        status, _ = self._entry_core.extract_submit_status(submit_result)
        outcome = submit_outcome(
            status=status,
            ok_status="RUNNER_NATIVE_TRAIL_SUBMITTED",
            rejected_status="RUNNER_NATIVE_TRAIL_REJECTED",
            ok_reason=reason,
        )
        state.extras["native_trail_pending"] = outcome.approved
        state.extras["protection_pending"] = outcome.approved
        state.extras["protection_active"] = bool(state.extras.get("protection_active")) or outcome.approved
        self._last_preview_by_symbol[symbol_u] = preview_row_from_outcome(
            outcome=outcome,
            exec_symbol=intent.symbol,
            order_ref=intent.order_ref,
            extra=audit_payload,
        )
        self._emit_local_audit(
            stage=outcome.audit_stage,
            severity=outcome.audit_severity,
            status=outcome.audit_status,
            reason=outcome.reason,
            symbol=symbol_u,
            payload=audit_payload,
        )

    def _build_runner_protection_spec_after_tp1(
        self,
        *,
        state: ReplicaTradeState,
        live_qty: int,
        trail_amount: float | None,
        trail_percent: float | None,
        trail_stop_px: float | None,
        lmt_price_offset: float | None,
        use_trail_limit: bool,
    ) -> ProtectionSpec:
        side = "SHORT" if str(state.active_side or "").strip().upper() == "SHORT" else "LONG"
        entry_px = self._optional_float(state.entry_px) or self._optional_float(state.extras.get("entry_ref_px")) or 0.0
        initial_stop_px = self._optional_float(state.stop_px) or self._optional_float(trail_stop_px) or entry_px
        risk = abs(entry_px - initial_stop_px) if entry_px > 0 and initial_stop_px > 0 else 0.0
        current_px = (
            self._optional_float(state.trail_high_px)
            or self._optional_float(state.entry_px)
            or self._optional_float(state.stop_px)
            or 0.0
        )
        position_id = str(
            state.extras.get("active_position_lot_id")
            or state.active_trigger_id
            or f"{self.strategy_id}:{state.active_exec_symbol or ''}"
        )
        return ProtectionSpec(
            symbol=str(state.active_exec_symbol or ""),
            position_id=position_id,
            side=side,
            qty=Decimal(str(int(max(0, live_qty)))),
            entry_px=float(entry_px),
            current_px=float(current_px),
            initial_stop_px=float(initial_stop_px),
            initial_risk_per_share=float(risk),
            tp1_filled=True,
            runner_qty=Decimal(str(int(max(0, live_qty)))),
            highest_px=self._optional_float(state.trail_high_px),
            lowest_px=self._optional_float(state.extras.get("trail_low_px")),
            mode="NATIVE_TRAIL_LIMIT" if use_trail_limit else "NATIVE_TRAIL",
            desired_stop_px=None if trail_stop_px is None else float(trail_stop_px),
            desired_trail_amount=None if trail_amount is None else float(trail_amount),
            desired_trail_percent=None if trail_percent is None else float(trail_percent),
            desired_limit_offset=None if lmt_price_offset is None else float(lmt_price_offset),
            close_only=True,
            reduce_only=True,
        )

    @staticmethod
    def _is_terminal_order_state(order: Any) -> bool:
        return terminal_order_state(order)

    def _protection_fingerprint(
        self,
        *,
        state: ReplicaTradeState,
        live_qty: int,
        stop_px: float,
        reason: str,
        status_prefix: str,
    ) -> str:
        return "|".join(
            [
                str(state.active_exec_symbol or ""),
                str(state.active_side or ""),
                str(int(max(0, live_qty))),
                f"{float(stop_px):.2f}",
                str(reason or ""),
                str(status_prefix or ""),
            ]
        )

    def _kill_switch_fingerprint(
        self,
        *,
        state: ReplicaTradeState,
        live_qty: int,
        reason: str,
        status_prefix: str,
    ) -> str:
        return "|".join(
            [
                str(state.active_exec_symbol or ""),
                str(state.active_side or ""),
                str(int(max(0, live_qty))),
                str(reason or ""),
                str(status_prefix or ""),
            ]
        )

    def _skip_duplicate_protection_action(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        live_qty: int,
        stop_px: float,
        reason: str,
        status_prefix: str,
    ) -> bool:
        fingerprint = self._protection_fingerprint(
            state=state,
            live_qty=live_qty,
            stop_px=stop_px,
            reason=reason,
            status_prefix=status_prefix,
        )
        if str(state.extras.get("last_protection_fingerprint") or "") != fingerprint:
            return False
        if not bool(state.extras.get("protection_pending", False)):
            return False
        submit_sig = stop_submit_signature(
            exec_symbol=str(state.active_exec_symbol or ""),
            qty=int(max(0, live_qty)),
            stop_px=float(stop_px),
        )
        now_mono = self._loop_time()
        last_sig = str(state.extras.get("last_protection_submit_sig") or submit_sig)
        last_submit_mono = float(state.extras.get("last_protection_submit_mono") or now_mono)
        dedupe = should_submit_stop(
            last_sig=last_sig,
            submit_sig=submit_sig,
            last_submit_mono=last_submit_mono,
            now_mono=now_mono,
            dedupe_sec=max(0.0, self._extra_float("stop_intent_dedupe_sec", 3.0)),
        )
        if not dedupe.is_duplicate:
            return False
        self._last_preview_by_symbol[symbol_u] = {
            "status": f"{status_prefix}_DEDUPED",
            "reason": reason,
            "exec_symbol": state.active_exec_symbol,
            "live_qty": live_qty,
            "stop_px": stop_px,
        }
        self._emit_local_audit(
            stage=f"{status_prefix.lower()}_deduped",
            status="deduped",
            reason=reason,
            symbol=symbol_u,
            payload={"exec_symbol": state.active_exec_symbol, "live_qty": live_qty, "stop_px": stop_px},
        )
        return True

    @staticmethod
    def _loop_time() -> float:
        try:
            return float(asyncio.get_running_loop().time())
        except RuntimeError:
            return 0.0

    def _skip_duplicate_kill_switch_action(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        live_qty: int,
        reason: str,
        status_prefix: str,
    ) -> bool:
        fingerprint = self._kill_switch_fingerprint(
            state=state,
            live_qty=live_qty,
            reason=reason,
            status_prefix=status_prefix,
        )
        if str(state.extras.get("last_kill_switch_fingerprint") or "") != fingerprint:
            return False
        if not bool(state.extras.get("kill_switch_pending", False)):
            return False
        submit_sig = action_submit_signature(
            exec_symbol=str(state.active_exec_symbol or ""),
            qty=int(max(0, live_qty)),
            action=f"{status_prefix}_KILL_SWITCH",
        )
        now_mono = self._loop_time()
        last_sig = str(state.extras.get("last_kill_switch_submit_sig") or submit_sig)
        last_submit_mono = float(state.extras.get("last_kill_switch_submit_mono") or now_mono)
        dedupe = should_submit_action(
            last_sig=last_sig,
            submit_sig=submit_sig,
            last_submit_mono=last_submit_mono,
            now_mono=now_mono,
            dedupe_sec=max(0.0, self._extra_float("stop_intent_dedupe_sec", 3.0)),
        )
        if not dedupe.is_duplicate:
            return False
        self._last_preview_by_symbol[symbol_u] = {
            "status": f"{status_prefix}_KILL_SWITCH_DEDUPED",
            "reason": reason,
            "exec_symbol": state.active_exec_symbol,
            "live_qty": live_qty,
        }
        self._emit_local_audit(
            stage=f"{status_prefix.lower()}_kill_switch_deduped",
            severity="warning",
            status="deduped",
            reason=reason,
            symbol=symbol_u,
            payload={"exec_symbol": state.active_exec_symbol, "live_qty": live_qty},
        )
        return True

    @staticmethod
    def _is_session_end_flatten_reason(reason: str | None) -> bool:
        reason_s = str(reason or "").strip().lower()
        return reason_s.startswith("force_flatten_") or reason_s == "session_end_flatten"

    def _session_handover_payload(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        extra: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "signal_symbol": symbol_u,
            "exec_symbol": state.active_exec_symbol,
            "session_name": self._session_name or None,
            "prev_session_strategy_id": self._prev_session_strategy_id,
        }
        if extra:
            payload.update(extra)
        return payload

    def _skip_duplicate_session_flatten_action(
        self,
        *,
        symbol_u: str,
        state: ReplicaTradeState,
        live_qty: int,
        reason: str,
    ) -> bool:
        if not self._is_session_end_flatten_reason(reason):
            return False
        if not bool(state.extras.get("session_end_flatten_pending", False)):
            return False
        submit_sig = action_submit_signature(
            exec_symbol=str(state.active_exec_symbol or ""),
            qty=int(max(0, live_qty)),
            action="SESSION_END_FLATTEN",
        )
        now_mono = self._loop_time()
        last_sig = str(state.extras.get("session_end_flatten_submit_sig") or submit_sig)
        last_submit_mono = float(state.extras.get("session_end_flatten_submit_mono") or now_mono)
        dedupe = should_submit_action(
            last_sig=last_sig,
            submit_sig=submit_sig,
            last_submit_mono=last_submit_mono,
            now_mono=now_mono,
            dedupe_sec=max(0.0, self._extra_float("stop_intent_dedupe_sec", 3.0)),
        )
        if not dedupe.is_duplicate:
            return False
        self._last_preview_by_symbol[symbol_u] = {
            "status": "SESSION_END_FLATTEN_DEDUPED",
            "reason": reason,
            "exec_symbol": state.active_exec_symbol,
            "live_qty": live_qty,
        }
        self._emit_local_audit(
            stage="session_end_flatten_deduped",
            domain="recon",
            severity="warning",
            status="deduped",
            reason=reason,
            symbol=symbol_u,
            payload=self._session_handover_payload(
                symbol_u=symbol_u,
                state=state,
                extra={"live_qty": live_qty},
            ),
        )
        return True

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
        decision_id: str | None = None,
        trigger_id: str | None = None,
        trace_id: str | None = None,
        order_ref: str | None = None,
    ) -> dict[str, Any]:
        return remember_runtime_audit_context(
            self._audit_context_by_symbol,
            symbol=symbol,
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            order_ref=order_ref,
        )

    def _ensure_audit_context(
        self,
        *,
        symbol: str,
        trigger_id: str | None = None,
        order_ref: str | None = None,
    ) -> dict[str, Any]:
        return ensure_runtime_audit_context(
            self._audit_context_by_symbol,
            symbol=symbol,
            make_decision_id=make_decision_id,
            trigger_id=trigger_id,
            order_ref=order_ref,
        )

    def _emit_local_audit(
        self,
        *,
        stage: str,
        domain: str = "strategy",
        severity: str = "info",
        status: str | None = None,
        reason: str | None = None,
        symbol: str | None = None,
        decision_id: str | None = None,
        trigger_id: str | None = None,
        trace_id: str | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> None:
        if not self._local_audit_enabled:
            return
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        symbol_u = str(symbol or "").strip().upper()
        audit_ctx = dict(self._audit_context_by_symbol.get(symbol_u) or {})
        decision_id_val, trigger_id_val, trace_id_val = resolve_audit_ids(
            audit_ctx,
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
        )
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        try:
            writer.emit(
                domain=domain,
                component="index_super_win_replica_strategy",
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
                "index_super_win_replica_local_audit_emit_failed",
                strategy_id=self.strategy_id,
                stage=stage,
                symbol=symbol_u or None,
                error=str(exc),
            )

    def _emit_gate_evaluated(
        self,
        *,
        symbol: str,
        exec_symbol: str | None,
        side: str | None,
        qty: int,
        status: str,
        reason: str,
        gate_allowed: bool,
        gate_details: Dict[str, Any] | None = None,
        trader_reason_code: str | None = None,
        block_stage: str | None = None,
    ) -> dict[str, Any]:
        audit_ctx = self._ensure_audit_context(symbol=symbol)
        payload = gate_evaluated_payload(
            symbol=symbol,
            exec_symbol=exec_symbol,
            side=side,
            qty=qty,
            gate_allowed=gate_allowed,
            gate_details=gate_details,
            block_stage=block_stage,
            trader_reason_code=trader_reason_code,
        )
        self._emit_local_audit(
            stage="gate_evaluated",
            status=status,
            reason=reason,
            symbol=symbol,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload=payload,
        )
        return audit_ctx

    def _record_entry_block(
        self,
        *,
        symbol: str,
        exec_symbol: str | None,
        side: str | None,
        qty: int,
        reason: str,
        trader_reason_code: str,
        gate_details: Dict[str, Any] | None,
        block_stage: str,
    ) -> None:
        gate_payload = normalize_gate_details(
            gate_details,
            block_stage=block_stage,
            reason=reason,
            trader_reason_code=trader_reason_code,
        )
        audit_ctx = self._emit_gate_evaluated(
            symbol=symbol,
            exec_symbol=exec_symbol,
            side=side,
            qty=qty,
            status="blocked",
            reason=reason,
            gate_allowed=False,
            gate_details=gate_payload,
            trader_reason_code=trader_reason_code,
            block_stage=block_stage,
        )
        block_payload = self._entry_block_payload(
            symbol=symbol,
            exec_symbol=exec_symbol,
            reason=reason,
            trader_reason_code=trader_reason_code,
            gate_details=gate_payload,
        )
        symbol_u = str(symbol or "").strip().upper()
        self._last_preview_by_symbol[symbol_u] = blocked_preview_payload(
            reason=reason,
            trader_reason_code=trader_reason_code,
            gate_details=gate_payload,
            exec_symbol=exec_symbol,
        )
        self._emit_local_audit(
            stage=block_stage,
            domain=block_stage_domain(block_stage),
            severity="warning",
            status="blocked",
            reason=reason,
            symbol=symbol_u,
            decision_id=audit_ctx.get("decision_id"),
            trigger_id=audit_ctx.get("trigger_id"),
            trace_id=audit_ctx.get("trace_id"),
            payload=block_payload,
        )
