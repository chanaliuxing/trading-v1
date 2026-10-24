# -*- coding: utf-8 -*-
"""
QQQ Intraday Strategy (doc/策略/qqq.md)

Data source assumptions:
- All required numbers are computed from SFS-1s bars (Redis, key: sfs:1s:<symbol>).
- Strategy subscribes to QQQ/TQQQ/SQQQ via MarketDataService.
- If SFS 09:30 price_base window is missing, strategy may fallback to IBKR historical bars (09:30 ET).
- If required data cannot be obtained in time, the strategy triggers a process shutdown (fail-fast).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import signal
import subprocess
import statistics
import time as time_mod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional, Deque
from collections import deque
from pathlib import Path
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.dal.sfs_facade import SFS1sReader
from libs.strategy import StrategyAlgorithm, DecisionEnvelope, OrderStyle, Freshness, make_decision_id
from services.exec.execution_state_model import FlattenLifecycleState, ProtectionState
from services.production.strategy_tradability import write_strategy_tradability_heartbeat
from services.strategy_runtime_v2 import OrderLifecycleState, RuntimeLifecycle, build_state_transition_event
from strategies.shared_data import shared_underlying_data_plane
from strategies.qqq_shared_signal_producer import (
    QQQSharedEntryCandidate,
    publish_qqq_shared_entry_signal,
)
from strategies.qqq_signal_authority import QQQSignalAuthority
from strategies.qqq_stock_executor import enforce_stock_execution_blockers
from strategies.runtime_path_guard import claim_paths, release_paths

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")
QQEW_SYMBOL = "QQEW"
EOD_FLATTEN_TIME = time(15, 59) # 15:59 NY Time


@dataclass
class _OhlcvBar:
    ts_start: datetime
    ts_end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _floor_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def _floor_5m(dt: datetime) -> datetime:
    m = (dt.minute // 5) * 5
    return dt.replace(minute=m, second=0, microsecond=0)


def _safe_float(x: Any) -> Optional[float]:
    try:
        v = float(x)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


class QQQIntradayStrategy(StrategyAlgorithm):
    def __init__(self, ctx, params):
        super().__init__(ctx, params)
        self._sfs = None
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._last_seen_ts_ms: Dict[str, int] = {}
        self._rewind_drop_count_by_symbol: Dict[str, int] = {}
        self._rewind_last_warn_mono_by_symbol: Dict[str, float] = {}
        self._rewind_last_incoming_ts_ms_by_symbol: Dict[str, int] = {}
        self._signal_authority = QQQSignalAuthority()

        self._day: Optional[str] = None
        self._is_traded: bool = False
        self._success_count: int = 0
        self._day_realized_pnl: float = 0.0
        self._day_closed_trade_pnls: list[float] = []
        self._realized_pnl_snapshot_total: Optional[float] = None
        self._pending_decision_id: Optional[str] = None
        self._pending_decision_set_mono: float = 0.0
        self._pending_decision_set_at_et: Optional[datetime] = None
        self._pending_decision_flat_probe_count: int = 0
        self._pending_decision_last_probe_mono: float = 0.0
        self._round_live_confirmed: bool = False
        self._side: Optional[str] = None
        self._exec_symbol: Optional[str] = None
        self._broker_positions_ready: bool = False
        self._broker_positions_hard_confirmed: bool = False
        self._last_broker_positions_refresh_mono: float = 0.0
        self._last_broker_positions_refresh_warn_mono: float = 0.0
        self._last_broker_positions_fallback_log_mono: float = 0.0
        self._startup_positions_ready: bool = False
        self._startup_positions_deadline_mono: float = 0.0
        self._last_startup_gate_warn_mono: float = 0.0
        self._last_startup_force_sync_mono: float = 0.0
        self._startup_reconciled_persisted: bool = False
        self._last_startup_reconciled_warn_mono: float = 0.0
        self._last_flatten_zero_qty_log_mono: float = 0.0
        self._flattening_started_mono: float = 0.0
        self._last_dual_conflict_action_mono: float = 0.0
        self._dual_conflict_active: bool = False
        self._dual_conflict_clear_probe_count: int = 0

        self._entry_px: Optional[float] = None
        self._entry_time_et: Optional[datetime] = None
        self._stop_px: Optional[float] = None
        self._stop_dist_pre: Optional[float] = None
        self._stop_dist_max: Optional[float] = None # 1.5x cap
        self._stop_dist_cur: Optional[float] = None
        self._breakeven_done: bool = False # Spec name
        self._break_even_armed: bool = False
        self._audit_miss_count: int = 0 # For Kill Switch (4 misses)
        self._last_stop_update_5m_end: Optional[datetime] = None
        self._entry_atr_used: Optional[float] = None
        self._tp1_hit: bool = False
        self._trail_high_px: Optional[float] = None
        self._trail_floor_px: Optional[float] = None

        self._price_base: Optional[float] = None
        self._price_base_source: Optional[str] = None
        self._price_0945: Optional[float] = None
        self._amp_locked: bool = False
        self._toxic_day: bool = False
        self._deadwater: bool = False

        self._cross_state: str = "MIDDLE"  # ABOVE | BELOW | MIDDLE
        self._cross_count: int = 0

        self._vwap_pv: float = 0.0
        self._vwap_v: float = 0.0
        self._vwap_last_ts_ms: int = 0
        self._qqew_vwap_pv: float = 0.0
        self._qqew_vwap_v: float = 0.0
        self._qqew_vwap_last_ts_ms: int = 0
        self._vwap_minute: Dict[datetime, float] = {}
        self._vwap_minute_keys: Deque[datetime] = deque()
        self._qqew_minute: Dict[datetime, float] = {}
        self._qqew_minute_keys: Deque[datetime] = deque()
        self._qqew_last_price: Optional[float] = None
        self._price_base_pv: float = 0.0
        self._price_base_v: float = 0.0

        self._min_bar: Optional[_OhlcvBar] = None
        self._minute_bars: Deque[_OhlcvBar] = deque(maxlen=4000)
        self._rolling_vol_queue: Deque[float] = deque(maxlen=10)
        self._today_vol_series: list[float] = []
        self._min_closes: Dict[datetime, float] = {}  # ts_end -> close
        self._min_close_keys: Deque[datetime] = deque()
        self._min_closes_maxlen: int = 3000

        self._bars_5m: Dict[str, Deque[_OhlcvBar]] = {
            "TQQQ": deque(maxlen=200),
            "SQQQ": deque(maxlen=200),
        }
        self._exec_min_closes: Dict[str, Dict[datetime, float]] = {"TQQQ": {}, "SQQQ": {}}
        self._exec_min_close_keys: Dict[str, Deque[datetime]] = {"TQQQ": deque(), "SQQQ": deque()}
        self._cur_5m: Dict[str, Optional[_OhlcvBar]] = {"TQQQ": None, "SQQQ": None}
        self._atr_pre: Dict[str, Optional[float]] = {"TQQQ": None, "SQQQ": None}
        self._last_atr_pre_retry_minute: Optional[datetime] = None
        self._atr_emergency_active: Dict[str, bool] = {"TQQQ": False, "SQQQ": False}
        self._stop_audit_started: bool = False
        self._stop_audit_task: Optional[asyncio.Task] = None
        self._loop_watchdog_started: bool = False
        self._loop_watchdog_task: Optional[asyncio.Task] = None

        self._qqq_buffer: Deque[tuple[int, float]] = deque(maxlen=2000)
        self._qqew_buffer: Deque[tuple[int, float]] = deque(maxlen=2000)
        self._stream_invalid: bool = True
        self._seed_ready: bool = False
        self._seed_locked_day: Optional[str] = None
        self._orh: Optional[float] = None
        self._orl: Optional[float] = None
        self._seed_velocity_samples: list[float] = []
        self._v_seed_sigma: Optional[float] = None
        self._last_seed_retry_minute: Optional[datetime] = None
        self._last_seed_warmup_retry_minute: Optional[datetime] = None
        self._last_breakout_ts: Optional[datetime] = None
        self._pending_breakout: Optional[Dict[str, Any]] = None
        self._last_minute_check: Optional[datetime] = None
        self._last_logic_retreat_minute: Optional[datetime] = None
        self._force_flatten_done_day: Optional[str] = None
        self._last_position_sync_at: float = 0.0
        self._ff_miss_count: int = 0
        self._ff_last_trigger_ts: float = 0.0
        self._last_missing_debug_minute: Optional[datetime] = None
        self._entry_sync_grace_until_mono: float = 0.0
        self._last_protection_ensure_mono: float = 0.0
        self._last_nonzero_ctx_qty_by_symbol: Dict[str, float] = {"TQQQ": 0.0, "SQQQ": 0.0}
        self._last_ctx_missing_warn_mono_by_symbol: Dict[str, float] = {"TQQQ": 0.0, "SQQQ": 0.0}
        self._last_ctx_broker_mismatch_warn_mono_by_symbol: Dict[str, float] = {"TQQQ": 0.0, "SQQQ": 0.0}
        self._last_unexpected_short_action_mono_by_symbol: Dict[str, float] = {"TQQQ": 0.0, "SQQQ": 0.0}
        self._ctx_nonzero_broker_flat_streak_by_symbol: Dict[str, int] = {"TQQQ": 0, "SQQQ": 0}
        self._ctx_flat_broker_nonzero_streak_by_symbol: Dict[str, int] = {"TQQQ": 0, "SQQQ": 0}
        self._ctx_stale_quarantined_by_symbol: Dict[str, bool] = {"TQQQ": False, "SQQQ": False}
        self._foreign_broker_quarantined_by_symbol: Dict[str, bool] = {"TQQQ": False, "SQQQ": False}
        self._last_ctx_stale_reset_mono_by_symbol: Dict[str, float] = {"TQQQ": 0.0, "SQQQ": 0.0}
        self._last_foreign_quarantine_mono_by_symbol: Dict[str, float] = {"TQQQ": 0.0, "SQQQ": 0.0}
        self._last_offhours_broker_only_sync_log_mono: float = 0.0

        self._state_path: Optional[Path] = None
        self._atr_seed_path: Optional[Path] = None
        self._atr_seed_saved_day: Optional[str] = None
        self._obs_path: Optional[Path] = None
        self._qqq_daily_atr_path: Optional[Path] = None
        self._atr_d_prev_day: Optional[float] = None
        self._atr_rank_prev_day: Optional[float] = None
        self._last_obs_write_minute: Optional[datetime] = None
        self._diag_path: Optional[Path] = None
        self._last_heartbeat_minute: Optional[datetime] = None
        self._code_version: str = "unknown"
        self._initialized_at_et: Optional[datetime] = None
        self._loop_iteration_seq: int = 0
        self._loop_in_iteration: bool = False
        self._loop_current_started_mono: float = 0.0
        self._loop_current_started_et: Optional[datetime] = None
        self._loop_last_finished_mono: float = 0.0
        self._loop_last_duration_ms: float = 0.0
        self._loop_last_stage: str = "idle"
        self._loop_last_stall_warn_mono: float = 0.0
        self._loop_last_slow_warn_mono: float = 0.0
        self._no_new_entries_until_et: Optional[datetime] = None
        self._restart_degrade_started_at_et: Optional[datetime] = None
        self._restart_degrade_unlock_reason: Optional[str] = None
        self._price_base_window_missed_day: Optional[str] = None
        self._price_base_recovery_last_minute: Optional[datetime] = None
        self._price_base_ibkr_last_attempt_ts: float = 0.0
        self._ibkr_qqq_contract: Optional[Any] = None

        self._locked_add_threshold: Optional[float] = None
        self._locked_dev_threshold: Optional[float] = None
        self._locked_target: Optional[float] = None
        self._trade_target_px: Optional[float] = None
        self._locked_velocity_sign: Optional[int] = None  # +1 or -1 for direction check
        self._last_entry_block_minute: Optional[datetime] = None
        self._last_entry_eval_second: Optional[datetime] = None
        self._last_entry_eval_minute: Optional[datetime] = None
        self._last_tp1_eod_no_exit_minute: Optional[datetime] = None
        self._last_stop_exit_at: Optional[datetime] = None
        self._last_stop_exit_side: Optional[str] = None
        self._expected_entry_qty: int = 0
        self._stop_order_ref_seq: int = 0
        self._stop_submit_lock: asyncio.Lock = asyncio.Lock()
        self._last_stop_submit_sig: Optional[str] = None
        self._last_stop_submit_mono: float = 0.0
        self._flatten_submit_lock: asyncio.Lock = asyncio.Lock()
        self._last_flatten_submit_sig: Optional[str] = None
        self._last_flatten_submit_mono: float = 0.0
        self._last_orphan_cleanup_mono: float = 0.0
        self._calibration_day: Optional[str] = None
        self._calibration_mode: str = "FALLBACK"
        self._s_add_abs: list[float] = []
        self._s_v_abs: list[float] = []
        self._t_chop: float = 900.0
        self._t_add: float = 1500.0
        self._t_v: float = 120.0
        self._v_limit: float = 20.0
        self._missing_debug_streak: int = 0
        self._missing_debug_signature: str = ""
        self._last_open_orders_check_warn_ts: float = 0.0
        self._last_open_orders_block_warn_ts: float = 0.0
        self._last_real_position_block_warn_ts: float = 0.0
        self._last_fill_confirm_sig: Optional[str] = None
        self._last_fill_confirm_mono: float = 0.0
        self._last_untrusted_fill_warn_mono: float = 0.0
        self._runtime_lifecycle: RuntimeLifecycle = RuntimeLifecycle.FLAT
        self._flatten_lifecycle_state: Optional[FlattenLifecycleState] = None
        self._protection_lifecycle_state: Optional[ProtectionState] = None
        self._order_lifecycle_states: Dict[str, OrderLifecycleState] = {}
        self._active_trigger_id: Optional[str] = None
        self._entry_timeout_block_until_mono: float = 0.0
        self._last_entry_timeout_block_warn_mono: float = 0.0
        self._elite_overext_mark_ts: Optional[datetime] = None
        self._elite_overext_mark_direction: Optional[str] = None
        self._prev_vol_surge_ratio: Optional[float] = None
        self._fast_cooldown_until: Optional[datetime] = None
        self._fast_cooldown_trigger_cross: int = 0
        self._ab_a_state: str = "WAIT_MARK"
        self._ab_a_armed_ts: Optional[datetime] = None
        self._ab_volatile_veto_direction: Optional[str] = None
        self._ab_volatile_anchor_px: Optional[float] = None
        self._ab_volatile_extreme_px: Optional[float] = None
        self._ab_volatile_probe_anchor_px: Optional[float] = None
        self._ab_volatile_probe_extreme_px: Optional[float] = None
        self._ab_volatile_probe_armed: bool = False
        self._planned_total_qty: int = 0
        self._planned_probe_qty: int = 0
        self._planned_add_qty: int = 0
        self._add_done: bool = False
        self._add_deadline_et: Optional[datetime] = None
        self._last_add_check_minute: Optional[datetime] = None

    def __del__(self) -> None:
        try:
            if self._loop_watchdog_task and not self._loop_watchdog_task.done():
                self._loop_watchdog_task.cancel()
            if self._stop_audit_task and not self._stop_audit_task.done():
                self._stop_audit_task.cancel()
            if self._task and not self._task.done():
                self._task.cancel()
        except Exception:
            pass

    @staticmethod
    def _normalize_trigger_id(trigger_id: Optional[str]) -> Optional[str]:
        raw = str(trigger_id or "").strip()
        if not raw:
            return None
        normalized = "".join(ch for ch in raw if ch.isalnum()).lower()
        if not normalized:
            return None
        return normalized[-16:]

    @classmethod
    def _trigger_ref_tag(cls, trigger_id: Optional[str]) -> Optional[str]:
        return cls._normalize_trigger_id(trigger_id)

    @classmethod
    def _extract_trigger_id_from_order(cls, order_ref: Any, note: Any) -> Optional[str]:
        ref_up = str(order_ref or "").upper()
        marker = ":TRG:"
        if marker in ref_up:
            token = ref_up.split(marker, 1)[1].split(":", 1)[0].strip()
            normalized = cls._normalize_trigger_id(token)
            if normalized:
                return normalized
        note_raw = str(note or "")
        if "\"trigger_id\"" in note_raw or "'trigger_id'" in note_raw or "trigger_id" in note_raw:
            m = re.search(r"trigger_id['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9\-_:]+)", note_raw)
            if m:
                normalized = cls._normalize_trigger_id(m.group(1))
                if normalized:
                    return normalized
        return None

    def _make_order_ref(self, symbol: str, kind: str, trigger_id: Optional[str] = None) -> str:
        sym = str(symbol or "").strip().upper()
        k = str(kind or "").strip().upper()
        if not sym:
            sym = "UNKNOWN"
        if not k:
            k = "GEN"
        # Keep order_ref short and strategy-scoped for safe broker-side cancellation.
        base = f"qqqv1:{self.strategy_id}:{sym}:{k}"
        trig_tag = self._trigger_ref_tag(trigger_id)
        if trig_tag:
            return f"{base}:TRG:{trig_tag}"
        return base

    def _mark_pending_decision(self, decision_id: str) -> None:
        self._pending_decision_id = str(decision_id or "").strip() or None
        now_et = datetime.now(ET)
        self._pending_decision_set_at_et = now_et
        try:
            self._pending_decision_set_mono = asyncio.get_running_loop().time()
        except RuntimeError:
            self._pending_decision_set_mono = 0.0
        self._pending_decision_flat_probe_count = 0
        self._pending_decision_last_probe_mono = 0.0

    def _pending_decision_age_sec(self, now_mono: Optional[float] = None) -> float:
        if self._pending_decision_id is None:
            return 0.0
        if self._pending_decision_set_mono > 0.0:
            ref = now_mono if now_mono is not None else asyncio.get_running_loop().time()
            return max(0.0, float(ref - self._pending_decision_set_mono))
        if self._pending_decision_set_at_et is not None:
            return max(0.0, float((datetime.now(ET) - self._pending_decision_set_at_et).total_seconds()))
        return 0.0

    def _clear_pending_decision(self, reason: str) -> None:
        if self._pending_decision_id is None:
            return
        decision_id = self._pending_decision_id
        age_sec = self._pending_decision_age_sec()
        self._pending_decision_id = None
        self._pending_decision_set_mono = 0.0
        self._pending_decision_set_at_et = None
        self._pending_decision_flat_probe_count = 0
        self._pending_decision_last_probe_mono = 0.0
        self._entry_sync_grace_until_mono = 0.0
        logger.info(
            "qqq_pending_decision_cleared",
            strategy_id=self.strategy_id,
            decision_id=decision_id,
            reason=reason,
            age_sec=round(float(age_sec), 3),
        )

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

    def _make_entry_audit_ids(self) -> tuple[str, str, str]:
        decision_id = str(make_decision_id() or "").strip()
        trigger_id = self._normalize_trigger_id(make_decision_id()) or self._normalize_trigger_id(decision_id) or decision_id
        trace_id = decision_id
        return trigger_id, decision_id, trace_id

    def _emit_local_audit(
        self,
        *,
        stage: str,
        severity: str = "info",
        status: Optional[str] = None,
        reason: Optional[str] = None,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        trigger_id_val = self._normalize_trigger_id(trigger_id) or self._normalize_trigger_id(self._active_trigger_id)
        decision_id_val = str(decision_id or self._pending_decision_id or "").strip() or None
        trace_id_val = str(trace_id or decision_id_val or trigger_id_val or "").strip() or None
        try:
            writer.emit(
                domain="strategy",
                component="qqq_intraday_strategy",
                stage=stage,
                severity=severity,
                strategy_id=self.strategy_id,
                strategy_run_id=strategy_run_id,
                trigger_id=trigger_id_val,
                decision_id=decision_id_val,
                trace_id=trace_id_val,
                symbol=symbol or self._exec_symbol,
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            logger.warning("qqq_local_audit_emit_failed", strategy_id=self.strategy_id, stage=stage, error=str(exc))

    def _derive_runtime_lifecycle(self) -> RuntimeLifecycle:
        if any(bool(flag) for flag in self._foreign_broker_quarantined_by_symbol.values()) or any(
            bool(flag) for flag in self._ctx_stale_quarantined_by_symbol.values()
        ):
            return RuntimeLifecycle.QUARANTINE
        if bool(self.state.get("is_flattening")):
            return RuntimeLifecycle.PENDING_EXIT
        if bool(self._is_traded):
            return RuntimeLifecycle.LIVE
        if self._pending_decision_id is not None:
            return RuntimeLifecycle.INTENT_SUBMITTED
        return RuntimeLifecycle.FLAT

    @staticmethod
    def _runtime_lifecycle_transition_path(
        before_state: RuntimeLifecycle,
        after_state: RuntimeLifecycle,
    ) -> list[tuple[RuntimeLifecycle, RuntimeLifecycle, str, str]]:
        if before_state == after_state:
            return []
        direct: dict[RuntimeLifecycle, set[RuntimeLifecycle]] = {
            RuntimeLifecycle.FLAT: {
                RuntimeLifecycle.PREVIEW,
                RuntimeLifecycle.INTENT_SUBMITTED,
                RuntimeLifecycle.QUARANTINE,
                RuntimeLifecycle.BROKER_UNKNOWN,
            },
            RuntimeLifecycle.PREVIEW: {
                RuntimeLifecycle.FLAT,
                RuntimeLifecycle.INTENT_SUBMITTED,
                RuntimeLifecycle.QUARANTINE,
            },
            RuntimeLifecycle.INTENT_SUBMITTED: {
                RuntimeLifecycle.PENDING_ENTRY,
                RuntimeLifecycle.LIVE,
                RuntimeLifecycle.FLAT,
                RuntimeLifecycle.QUARANTINE,
            },
            RuntimeLifecycle.PENDING_ENTRY: {
                RuntimeLifecycle.LIVE,
                RuntimeLifecycle.FLAT,
                RuntimeLifecycle.QUARANTINE,
                RuntimeLifecycle.BROKER_UNKNOWN,
            },
            RuntimeLifecycle.LIVE: {
                RuntimeLifecycle.PENDING_EXIT,
                RuntimeLifecycle.FLAT,
                RuntimeLifecycle.QUARANTINE,
                RuntimeLifecycle.BROKER_UNKNOWN,
            },
            RuntimeLifecycle.PENDING_EXIT: {
                RuntimeLifecycle.LIVE,
                RuntimeLifecycle.FLAT,
                RuntimeLifecycle.QUARANTINE,
                RuntimeLifecycle.BROKER_UNKNOWN,
            },
            RuntimeLifecycle.QUARANTINE: {
                RuntimeLifecycle.FLAT,
                RuntimeLifecycle.BROKER_UNKNOWN,
            },
            RuntimeLifecycle.BROKER_UNKNOWN: {
                RuntimeLifecycle.FLAT,
                RuntimeLifecycle.QUARANTINE,
                RuntimeLifecycle.PENDING_ENTRY,
                RuntimeLifecycle.LIVE,
            },
        }
        if after_state in direct.get(before_state, set()):
            return [(before_state, after_state, "runtime_lifecycle_transition", "runtime_lifecycle_state_changed")]

        ladder = [
            RuntimeLifecycle.FLAT,
            RuntimeLifecycle.INTENT_SUBMITTED,
            RuntimeLifecycle.PENDING_ENTRY,
            RuntimeLifecycle.LIVE,
            RuntimeLifecycle.PENDING_EXIT,
        ]
        if before_state in ladder and after_state in ladder:
            src_idx = ladder.index(before_state)
            dst_idx = ladder.index(after_state)
            step = 1 if dst_idx > src_idx else -1
            transitions: list[tuple[RuntimeLifecycle, RuntimeLifecycle, str, str]] = []
            for idx in range(src_idx, dst_idx, step):
                src = ladder[idx]
                dst = ladder[idx + step]
                terminal = (idx + step) == dst_idx
                transitions.append(
                    (
                        src,
                        dst,
                        "runtime_lifecycle_transition" if terminal else "runtime_lifecycle_backfill",
                        "runtime_lifecycle_state_changed" if terminal else "runtime_lifecycle_backfill",
                    )
                )
            return transitions

        return [(before_state, after_state, "runtime_lifecycle_transition", "runtime_lifecycle_state_changed")]

    def _emit_runtime_lifecycle_transition(self, *, reason: str) -> None:
        writer = self._resolve_local_audit_writer()
        after_state = self._derive_runtime_lifecycle()
        if writer is None:
            self._runtime_lifecycle = after_state
            return
        before_state = self._runtime_lifecycle
        if before_state == after_state:
            return
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        trigger_id = self._normalize_trigger_id(self._active_trigger_id)
        decision_id = str(self._pending_decision_id or "").strip() or None
        trace_id = decision_id or trigger_id or f"{self.strategy_id}:runtime_lifecycle"
        symbol = str(self._exec_symbol or "").strip().upper() or None
        base_payload = {
            "reason": reason,
            "runtime_lifecycle_before": before_state.value,
            "runtime_lifecycle_after": after_state.value,
            "is_traded": bool(self._is_traded),
            "pending_decision_id": decision_id,
            "flattening": bool(self.state.get("is_flattening")),
            "quarantine_active": bool(
                any(bool(flag) for flag in self._foreign_broker_quarantined_by_symbol.values())
                or any(bool(flag) for flag in self._ctx_stale_quarantined_by_symbol.values())
            ),
        }
        try:
            for src, dst, event_type, event_reason in self._runtime_lifecycle_transition_path(before_state, after_state):
                event = build_state_transition_event(
                    trace_id=trace_id,
                    strategy_id=self.strategy_id,
                    strategy_run_id=strategy_run_id,
                    trigger_id=trigger_id,
                    decision_id=decision_id,
                    entity_type="runtime",
                    entity_id=f"{self.strategy_id}:QQQ_RUNTIME",
                    state_machine="runtime",
                    before_state=src.value,
                    event_type=event_type,
                    after_state=dst.value,
                    reason=reason if event_reason == "runtime_lifecycle_state_changed" else event_reason,
                    source_module="qqq_intraday_strategy",
                    symbol=symbol,
                    payload=dict(base_payload),
                )
                writer.emit(
                    domain="state",
                    component="qqq_intraday_strategy",
                    stage="runtime_transition",
                    severity="info",
                    strategy_id=self.strategy_id,
                    strategy_run_id=strategy_run_id,
                    trigger_id=event.trigger_id,
                    decision_id=event.decision_id,
                    trace_id=event.trace_id,
                    symbol=event.symbol,
                    status=event.after_state,
                    reason=event.reason,
                    payload=event.to_dict(),
                )
        except Exception as exc:
            logger.warning("qqq_runtime_lifecycle_audit_emit_failed", strategy_id=self.strategy_id, reason=reason, error=str(exc))
        self._runtime_lifecycle = after_state

    def _emit_state_transition_event(
        self,
        *,
        state_machine: str,
        entity_type: str,
        entity_id: str,
        before_state: str,
        event_type: str,
        after_state: str,
        reason: str,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        order_ref: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        event = build_state_transition_event(
            trace_id=str(trace_id or decision_id or trigger_id or f"{self.strategy_id}:{entity_id}").strip(),
            strategy_id=self.strategy_id,
            strategy_run_id=strategy_run_id,
            trigger_id=self._normalize_trigger_id(trigger_id) or self._normalize_trigger_id(self._active_trigger_id),
            decision_id=str(decision_id or self._pending_decision_id or "").strip() or None,
            entity_type=entity_type,
            entity_id=entity_id,
            state_machine=state_machine,
            before_state=before_state,
            event_type=event_type,
            after_state=after_state,
            reason=reason,
            source_module="qqq_intraday_strategy",
            symbol=symbol,
            order_ref=order_ref,
            payload=dict(payload or {}),
        )
        writer.emit(
            domain="state",
            component="qqq_intraday_strategy",
            stage=f"{state_machine}_transition",
            severity="info",
            strategy_id=self.strategy_id,
            strategy_run_id=strategy_run_id,
            trigger_id=event.trigger_id,
            decision_id=event.decision_id,
            trace_id=event.trace_id,
            symbol=event.symbol,
            status=event.after_state,
            reason=event.reason,
            payload=event.to_dict(),
        )

    def _emit_order_lifecycle_transition(
        self,
        *,
        order_ref: Optional[str],
        symbol: Optional[str],
        after_state: OrderLifecycleState,
        reason: str,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        ref = str(order_ref or "").strip()
        if not ref:
            return
        before_state = self._order_lifecycle_states.get(ref, OrderLifecycleState.CREATED)
        if before_state == after_state:
            return
        self._emit_state_transition_event(
            state_machine="order",
            entity_type="order",
            entity_id=ref,
            before_state=before_state.value,
            event_type=f"qqq_order_lifecycle_{after_state.value}",
            after_state=after_state.value,
            reason=reason,
            symbol=str(symbol or self._exec_symbol or "").strip().upper() or None,
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            order_ref=ref,
            payload=payload,
        )
        self._order_lifecycle_states[ref] = after_state

    def _emit_flatten_lifecycle_transition(
        self,
        *,
        after_state: FlattenLifecycleState,
        reason: str,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        order_ref: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        before_state = self._flatten_lifecycle_state or FlattenLifecycleState.CREATED
        if self._flatten_lifecycle_state is None and after_state == FlattenLifecycleState.CREATED:
            self._flatten_lifecycle_state = after_state
            return
        if before_state == after_state:
            return
        self._emit_state_transition_event(
            state_machine="flatten",
            entity_type="flatten",
            entity_id=f"{self.strategy_id}:QQQ_FLATTEN",
            before_state=before_state.value,
            event_type=f"qqq_flatten_lifecycle_{after_state.value}",
            after_state=after_state.value,
            reason=reason,
            symbol=str(symbol or self._exec_symbol or "").strip().upper() or None,
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            order_ref=order_ref,
            payload=payload,
        )
        self._flatten_lifecycle_state = after_state

    def _emit_protection_lifecycle_transition(
        self,
        *,
        after_state: ProtectionState,
        reason: str,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        order_ref: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        before_state = self._protection_lifecycle_state or ProtectionState.RECOVERY
        if self._protection_lifecycle_state is None and after_state == ProtectionState.RECOVERY:
            self._protection_lifecycle_state = after_state
            return
        if before_state == after_state:
            return
        self._emit_state_transition_event(
            state_machine="protection",
            entity_type="protection",
            entity_id=f"{self.strategy_id}:QQQ_PROTECTION",
            before_state=before_state.value,
            event_type=f"qqq_protection_lifecycle_{after_state.value}",
            after_state=after_state.value,
            reason=reason,
            symbol=str(symbol or self._exec_symbol or "").strip().upper() or None,
            decision_id=decision_id,
            trigger_id=trigger_id,
            trace_id=trace_id,
            order_ref=order_ref,
            payload=payload,
        )
        self._protection_lifecycle_state = after_state

    def _clear_trade_runtime_state(self) -> None:
        self._is_traded = False
        self.state["is_flattening"] = False
        self._flattening_started_mono = 0.0
        self._flatten_lifecycle_state = None
        self._protection_lifecycle_state = None
        self._dual_conflict_active = False
        self._dual_conflict_clear_probe_count = 0
        self._active_trigger_id = None
        self._round_live_confirmed = False
        self._side = None
        self._exec_symbol = None
        self._entry_px = None
        self._entry_time_et = None
        self._entry_atr_used = None
        self._stop_px = None
        self._stop_dist_pre = None
        self._stop_dist_max = None
        self._stop_dist_cur = None
        self._break_even_armed = False
        self._trade_target_px = None
        self._locked_velocity_sign = None
        self._tp1_hit = False
        self._trail_high_px = None
        self._trail_floor_px = None
        self._planned_total_qty = 0
        self._planned_probe_qty = 0
        self._planned_add_qty = 0
        self._add_done = False

    def _register_ctx_nonzero_broker_flat(self, symbol: str, *, qty_ctx: float, qty_broker: float) -> None:
        symbol_u = str(symbol or "").strip().upper()
        if symbol_u not in self._ctx_nonzero_broker_flat_streak_by_symbol:
            return
        self._ctx_nonzero_broker_flat_streak_by_symbol[symbol_u] += 1
        self._ctx_flat_broker_nonzero_streak_by_symbol[symbol_u] = 0
        extras = self.params.extras or {}
        probe_need = max(2, int(extras.get("ctx_nonzero_broker_flat_self_heal_probe_need", 3)))
        cooldown_sec = max(10.0, float(extras.get("ctx_nonzero_broker_flat_self_heal_cooldown_sec", 30.0)))
        try:
            now_mono = asyncio.get_running_loop().time()
        except RuntimeError:
            now_mono = time_mod.monotonic()
        last_action = float(self._last_ctx_stale_reset_mono_by_symbol.get(symbol_u, 0.0) or 0.0)
        if self._ctx_nonzero_broker_flat_streak_by_symbol[symbol_u] < probe_need:
            return
        if (now_mono - last_action) < cooldown_sec:
            return
        self._ctx_stale_quarantined_by_symbol[symbol_u] = True
        self._last_ctx_stale_reset_mono_by_symbol[symbol_u] = now_mono
        self._last_nonzero_ctx_qty_by_symbol[symbol_u] = 0.0
        self._pending_decision_flat_probe_count = 0
        if str(self._exec_symbol or "").strip().upper() == symbol_u or bool(self._is_traded):
            self._clear_trade_runtime_state()
        self._clear_pending_decision(f"ctx_reset_from_broker_flat:{symbol_u}")
        self._persist_runtime_state(reason=f"ctx_reset_from_broker_flat:{symbol_u}")
        logger.warning(
            "qqq_ctx_reset_from_broker_flat",
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            qty_ctx=round(float(qty_ctx), 6),
            qty_broker=round(float(qty_broker), 6),
            probe_need=probe_need,
        )
        self._emit_local_audit(
            stage="ctx_reset_from_broker_flat",
            severity="warning",
            status="applied",
            reason="ctx_nonzero_broker_flat",
            symbol=symbol_u,
            payload={
                "qty_ctx": round(float(qty_ctx), 6),
                "qty_broker": round(float(qty_broker), 6),
                "streak": int(self._ctx_nonzero_broker_flat_streak_by_symbol[symbol_u]),
                "probe_need": probe_need,
            },
        )

    def _register_ctx_flat_broker_nonzero(self, symbol: str, *, qty_ctx: float, qty_broker: float) -> None:
        symbol_u = str(symbol or "").strip().upper()
        if symbol_u not in self._ctx_flat_broker_nonzero_streak_by_symbol:
            return
        self._ctx_flat_broker_nonzero_streak_by_symbol[symbol_u] += 1
        self._ctx_nonzero_broker_flat_streak_by_symbol[symbol_u] = 0
        extras = self.params.extras or {}
        probe_need = max(2, int(extras.get("ctx_flat_broker_nonzero_quarantine_probe_need", 3)))
        cooldown_sec = max(10.0, float(extras.get("ctx_flat_broker_nonzero_quarantine_cooldown_sec", 30.0)))
        try:
            now_mono = asyncio.get_running_loop().time()
        except RuntimeError:
            now_mono = time_mod.monotonic()
        last_action = float(self._last_foreign_quarantine_mono_by_symbol.get(symbol_u, 0.0) or 0.0)
        if self._ctx_flat_broker_nonzero_streak_by_symbol[symbol_u] < probe_need:
            return
        if (now_mono - last_action) < cooldown_sec:
            return
        self._foreign_broker_quarantined_by_symbol[symbol_u] = True
        self._last_foreign_quarantine_mono_by_symbol[symbol_u] = now_mono
        logger.warning(
            "qqq_foreign_broker_residual_quarantined",
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            qty_ctx=round(float(qty_ctx), 6),
            qty_broker=round(float(qty_broker), 6),
            probe_need=probe_need,
        )
        self._emit_local_audit(
            stage="foreign_residual_quarantined",
            severity="warning",
            status="applied",
            reason="ctx_flat_broker_nonzero",
            symbol=symbol_u,
            payload={
                "qty_ctx": round(float(qty_ctx), 6),
                "qty_broker": round(float(qty_broker), 6),
                "streak": int(self._ctx_flat_broker_nonzero_streak_by_symbol[symbol_u]),
                "probe_need": probe_need,
            },
        )

    def _reset_position_mismatch_streaks(self, symbol: str) -> None:
        symbol_u = str(symbol or "").strip().upper()
        if symbol_u not in self._ctx_nonzero_broker_flat_streak_by_symbol:
            return
        self._ctx_nonzero_broker_flat_streak_by_symbol[symbol_u] = 0
        self._ctx_flat_broker_nonzero_streak_by_symbol[symbol_u] = 0
        if abs(float(self._last_nonzero_ctx_qty_by_symbol.get(symbol_u, 0.0) or 0.0)) <= 1e-6:
            self._ctx_stale_quarantined_by_symbol[symbol_u] = False
        self._foreign_broker_quarantined_by_symbol[symbol_u] = False
        self._add_deadline_et = None
        self._last_add_check_minute = None

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

    def _is_flattening_active(self, *, reason: str) -> bool:
        if not self.state.get("is_flattening"):
            return False
        stale_sec = max(1.0, float((self.params.extras or {}).get("flattening_stale_sec", 8.0)))
        try:
            now_mono = asyncio.get_running_loop().time()
        except RuntimeError:
            now_mono = 0.0
        age_sec = (now_mono - self._flattening_started_mono) if (now_mono > 0 and self._flattening_started_mono > 0) else (stale_sec + 1.0)
        if age_sec > stale_sec:
            self.state["is_flattening"] = False
            self._flattening_started_mono = 0.0
            logger.warning(
                "qqq_flattening_stale_cleared",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                reason=reason,
                age_sec=round(float(age_sec), 3),
                stale_sec=stale_sec,
            )
            return False
        return True

    def _all_day_test_mode(self) -> bool:
        return bool((self.params.extras or {}).get("all_day_test_mode", False))

    @staticmethod
    def _parse_hms_config(raw: Any, default: time) -> time:
        try:
            val = str(raw or "").strip()
            if not val:
                return default
            return time.fromisoformat(val)
        except Exception:
            return default

    @staticmethod
    def _is_regular_session(dt: datetime) -> bool:
        if dt.weekday() > 4:
            return False
        return time(9, 30) <= dt.time() <= time(16, 0)

    @staticmethod
    def _stable_entry_guard_enabled(extras: Dict[str, Any]) -> bool:
        return bool(extras.get("stable_entry_guard_enabled", False))

    @staticmethod
    def _entry_seed_min_samples(extras: Dict[str, Any]) -> int:
        return max(1, int(extras.get("first_entry_seed_min_samples", 600)))

    @staticmethod
    def _third_trade_requires_positive_day_pnl(extras: Dict[str, Any]) -> bool:
        return bool(extras.get("third_trade_requires_positive_day_pnl", True))

    @staticmethod
    def _parse_gate_time(raw: Any, default: time) -> time:
        if raw is None:
            return default
        text = str(raw).strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(text, fmt).time()
            except Exception:
                continue
        return default

    def _anti_chase_enabled(self, extras: Dict[str, Any]) -> bool:
        if "anti_chase_chop_enable" in extras:
            return bool(extras.get("anti_chase_chop_enable"))
        return str(self.strategy_id) == "qqq_intraday_001"

    def _r1_fourth_short_small_green_enabled(self, extras: Dict[str, Any]) -> bool:
        if "r1_fourth_short_small_green_enable" in extras:
            return bool(extras.get("r1_fourth_short_small_green_enable"))
        return str(self.strategy_id) == "qqq_intraday_001"

    def _legacy_trader_parity_mode(self, extras: Dict[str, Any]) -> bool:
        if "legacy_trader_parity_mode" in extras:
            return bool(extras.get("legacy_trader_parity_mode"))
        return str(self.strategy_id) == "qqq_intraday_001"

    def _enforce_stock_execution_blockers_enabled(self, extras: Dict[str, Any]) -> bool:
        if "enforce_stock_execution_blockers" in extras:
            return bool(extras.get("enforce_stock_execution_blockers"))
        return not self._legacy_trader_parity_mode(extras)

    def _startup_reconciled_stale_new_grace_sec(self, extras: Dict[str, Any]) -> float:
        raw = extras.get("startup_reconciled_stale_new_grace_sec")
        if raw is None:
            return 0.0 if self._legacy_trader_parity_mode(extras) else 180.0
        try:
            return max(0.0, float(raw))
        except Exception:
            return 0.0 if self._legacy_trader_parity_mode(extras) else 180.0

    async def _legacy_pre_entry_execution_gate(self, extras: Dict[str, Any]) -> bool:
        """Restore the 10-day-old trader-facing entry blockers for the primary strategy."""
        if (not self._legacy_trader_parity_mode(extras)) or self._enforce_stock_execution_blockers_enabled(extras):
            return False

        try:
            open_orders = await self._ctx_open_orders_with_timeout()
            active_count = sum(
                1
                for order in open_orders
                if (
                    (order.get("strategy_id") if isinstance(order, dict) else getattr(order, "strategy_id", None))
                    == self.strategy_id
                )
                and (not self._is_terminal_order_state(order))
            )
            if active_count > 0:
                warn_interval_sec = max(10.0, float(extras.get("open_orders_block_warn_interval_sec", 60.0)))
                loop_now = asyncio.get_running_loop().time()
                if (loop_now - self._last_open_orders_block_warn_ts) >= warn_interval_sec:
                    self._last_open_orders_block_warn_ts = loop_now
                    logger.warning(
                        "qqq_entry_blocked_by_open_orders",
                        strategy_id=self.strategy_id,
                        active_same_strategy_open_orders=int(active_count),
                        warn_interval_sec=warn_interval_sec,
                    )
                payload = {
                    "strategy_id": self.strategy_id,
                    "reason": "legacy_open_orders_block",
                    "active_same_strategy_open_orders": int(active_count),
                    "legacy_trader_parity_mode": True,
                }
                self._append_diag_event("qqq_entry_blocked_by_legacy_open_orders", payload)
                self._emit_local_audit(
                    stage="entry_blocked_legacy_open_orders",
                    severity="warning",
                    status="blocked",
                    reason="legacy_open_orders_block",
                    payload=payload,
                )
                return True
        except Exception as exc:
            warn_interval_sec = max(10.0, float(extras.get("open_orders_check_warn_interval_sec", 300.0)))
            loop_now = asyncio.get_running_loop().time()
            if (loop_now - self._last_open_orders_check_warn_ts) >= warn_interval_sec:
                self._last_open_orders_check_warn_ts = loop_now
                logger.warning(
                    "qqq_open_orders_check_failed",
                    strategy_id=self.strategy_id,
                    error=str(exc),
                    warn_interval_sec=warn_interval_sec,
                )
            else:
                logger.debug("qqq_open_orders_check_failed_suppressed", strategy_id=self.strategy_id, error=str(exc))

        try:
            qty_t = abs(await self._current_exec_qty_for_symbol("TQQQ"))
            qty_s = abs(await self._current_exec_qty_for_symbol("SQQQ"))
            if qty_t > 0 or qty_s > 0:
                if not self._is_traded:
                    warn_interval_sec = max(10.0, float(extras.get("real_position_block_warn_interval_sec", 60.0)))
                    loop_now = asyncio.get_running_loop().time()
                    if (loop_now - self._last_real_position_block_warn_ts) >= warn_interval_sec:
                        self._last_real_position_block_warn_ts = loop_now
                        logger.warning(
                            "qqq_entry_blocked_by_real_position",
                            strategy_id=self.strategy_id,
                            tqqq_qty=qty_t,
                            sqqq_qty=qty_s,
                            warn_interval_sec=warn_interval_sec,
                        )
                    self._is_traded = True
                    self._round_live_confirmed = True
                    self._persist_runtime_state(reason="sync_from_real_position")
                payload = {
                    "strategy_id": self.strategy_id,
                    "reason": "legacy_real_position_block",
                    "tqqq_qty": float(qty_t),
                    "sqqq_qty": float(qty_s),
                    "is_traded": bool(self._is_traded),
                    "legacy_trader_parity_mode": True,
                }
                self._append_diag_event("qqq_entry_blocked_by_legacy_real_position", payload)
                self._emit_local_audit(
                    stage="entry_blocked_legacy_real_position",
                    severity="warning",
                    status="blocked",
                    reason="legacy_real_position_block",
                    payload=payload,
                )
                return True
        except Exception as exc:
            logger.warning("qqq_real_position_check_failed", strategy_id=self.strategy_id, error=str(exc))
            self._append_diag_event(
                "qqq_real_position_check_failed",
                {"strategy_id": self.strategy_id, "error": str(exc), "legacy_trader_parity_mode": True},
            )
        return False

    @staticmethod
    def _hard_stop_atr_mult(extras: Dict[str, Any]) -> float:
        raw = extras.get("hard_stop_atr_mult", extras.get("stop_atr_mult", 0.8))
        try:
            value = float(raw)
        except Exception:
            value = 0.8
        return max(0.0, value)

    def _is_first_entry_guard_context(self) -> bool:
        if self._success_count != 0:
            return False
        if self._is_traded:
            return False
        if self._side is not None or self._exec_symbol is not None:
            return False
        return True

    def _entry_data_integrity_failure(self, now: datetime, extras: Dict[str, Any]) -> Optional[str]:
        max_data_age_sec = float(extras.get("entry_max_data_age_sec", 3.0))
        max_data_age_qqq_sec = float(extras.get("entry_max_data_age_sec_qqq", max_data_age_sec))
        age_qqq = self._symbol_age_sec("QQQ", now)
        if age_qqq is None or age_qqq > max_data_age_qqq_sec:
            return "qqq_age"
        qqew_decoupled = self._qqew_decoupled()
        if not qqew_decoupled:
            max_data_age_qqew_sec = float(extras.get("entry_max_data_age_sec_qqew", max_data_age_sec))
            age_qqew = self._symbol_age_sec(QQEW_SYMBOL, now)
            if age_qqew is None or age_qqew > max_data_age_qqew_sec:
                return "qqew_age"
        require_continuity = bool(extras.get("entry_require_continuity_180s", False))
        if require_continuity and (not self._has_continuous_180s(now, include_qqew=(not qqew_decoupled))):
            return "continuity_180s"
        # Opening window without a known base source is treated as incomplete data.
        if now.time() < time(10, 0):
            src = str(self._price_base_source or "").strip().lower()
            if src in {"", "unknown"}:
                return "price_base_source"
        return None

    def _entry_data_integrity_ready(self, now: datetime, extras: Dict[str, Any]) -> bool:
        return self._entry_data_integrity_failure(now, extras) is None

    def _arm_restart_degrade_window(self, now: Optional[datetime], extras: Dict[str, Any]) -> None:
        self._no_new_entries_until_et = None
        self._restart_degrade_started_at_et = None
        self._restart_degrade_unlock_reason = None
        if not self._stable_entry_guard_enabled(extras):
            return
        if not bool(extras.get("restart_degrade_enabled", False)):
            return
        if now is None:
            return
        if self._all_day_test_mode():
            return
        if not self._is_regular_session(now):
            return
        if now.time() < time(9, 30):
            return
        until_t = self._parse_hms_config(extras.get("restart_degrade_default_until", "10:00:00"), time(10, 0))
        until_dt = now.replace(hour=until_t.hour, minute=until_t.minute, second=until_t.second, microsecond=0)
        if now >= until_dt:
            return
        self._no_new_entries_until_et = until_dt
        self._restart_degrade_started_at_et = now
        logger.warning(
            "qqq_restart_degrade_armed",
            strategy_id=self.strategy_id,
            started_at=now.isoformat(),
            until_et=until_dt.isoformat(),
        )

    def _can_unlock_restart_degrade_early(self, now: datetime, extras: Dict[str, Any]) -> bool:
        if not self._seed_ready:
            return False
        if len(self._seed_velocity_samples) < self._entry_seed_min_samples(extras):
            return False
        src = str(self._price_base_source or "").strip().lower()
        if src == "startup_anchor_10s":
            return False
        if bool(extras.get("fail_closed_on_data_integrity", False)):
            if not self._entry_data_integrity_ready(now, extras):
                return False
        return True

    def _refresh_restart_degrade_state(self, now: datetime, extras: Dict[str, Any]) -> None:
        until = self._no_new_entries_until_et
        if until is None:
            return
        if now >= until:
            self._no_new_entries_until_et = None
            self._restart_degrade_unlock_reason = "time_fallback_elapsed"
            return
        if not bool(extras.get("restart_degrade_allow_early_unlock", True)):
            return
        if self._can_unlock_restart_degrade_early(now, extras):
            self._no_new_entries_until_et = None
            self._restart_degrade_unlock_reason = "early_unlock_seed_ready"

    def _restart_degrade_active(self, now: datetime) -> bool:
        return self._no_new_entries_until_et is not None and now < self._no_new_entries_until_et

    def _stable_entry_block_reason(self, now: datetime, extras: Dict[str, Any]) -> Optional[str]:
        if not self._stable_entry_guard_enabled(extras):
            return None
        system_block_reason = self._system_entry_block_reason(extras)
        if system_block_reason is not None:
            self._publish_entry_tradability_block(system_block_reason, active=True)
            return system_block_reason
        self._refresh_restart_degrade_state(now, extras)
        if bool(extras.get("fail_closed_on_data_integrity", False)) and (not self._entry_data_integrity_ready(now, extras)):
            self._publish_entry_tradability_block("data_integrity_fail_closed", active=True)
            return "data_integrity_fail_closed"
        if bool(extras.get("first_entry_require_seed_ready", False)) and self._is_first_entry_guard_context():
            min_samples = self._entry_seed_min_samples(extras)
            if (not self._seed_ready) or (len(self._seed_velocity_samples) < min_samples):
                self._publish_entry_tradability_block("first_entry_seed_not_ready", active=True)
                return "first_entry_seed_not_ready"
        if bool(extras.get("startup_anchor_entry_block_enabled", False)):
            embargo_start = self._parse_hms_config(extras.get("startup_anchor_entry_block_start", "09:30:00"), time(9, 30))
            embargo_end = self._parse_hms_config(extras.get("startup_anchor_entry_block_end", "10:00:00"), time(10, 0))
            src = str(self._price_base_source or "").strip().lower()
            if src == "startup_anchor_10s" and (embargo_start <= now.time() < embargo_end):
                self._publish_entry_tradability_block("startup_anchor_embargo", active=True)
                return "startup_anchor_embargo"
        if self._restart_degrade_active(now):
            self._publish_entry_tradability_block("restart_degrade_active", active=True)
            return "restart_degrade_active"
        self._publish_entry_tradability_block("", active=False)
        return None

    def _publish_entry_tradability_block(self, reason: str, *, active: bool) -> None:
        try:
            from services.data_availability import get_data_availability_service

            service = get_data_availability_service()
            if active and reason in {
                "data_integrity_fail_closed",
                "first_entry_seed_not_ready",
                "history_missing",
                "feature_seed_missing",
            }:
                service.mark_unavailable(
                    dataset="strategy_tradability",
                    symbol="QQQ",
                    resolution="entry",
                    strategy_id=str(self.strategy_id),
                    stage="stable_entry_guard",
                    reason=reason,
                    source="qqq_intraday_strategy",
                    severity="entry_blocking",
                )
            else:
                service.clear(
                    dataset="strategy_tradability",
                    symbol="QQQ",
                    resolution="entry",
                    strategy_id=str(self.strategy_id),
                    stage="stable_entry_guard",
                )
        except Exception:
            return

    def _system_entry_block_reason(self, extras: Dict[str, Any]) -> Optional[str]:
        engine = getattr(self.ctx, "engine", None)
        if bool(extras.get("entry_require_decision_pipeline_healthy", True)):
            decision_pipeline = getattr(engine, "decision_pipeline", None)
            if decision_pipeline is None:
                return "decision_pipeline_unavailable"

        if bool(extras.get("entry_require_market_data_healthy", True)):
            market_data_service = getattr(engine, "market_data_service", None)
            connected = True
            if market_data_service is None:
                connected = False
            else:
                ib_adapter = getattr(market_data_service, "ib_adapter", None)
                if ib_adapter is None:
                    connected = False
                else:
                    is_connected = getattr(ib_adapter, "is_connected", None)
                    if callable(is_connected):
                        try:
                            connected = bool(is_connected())
                        except Exception:
                            connected = False
                    else:
                        ib = getattr(ib_adapter, "ib", None)
                        if ib is not None and hasattr(ib, "isConnected"):
                            try:
                                connected = bool(ib.isConnected())
                            except Exception:
                                connected = False
                        else:
                            connected = False
            if not connected:
                return "market_data_unavailable"
        return None

    def _use_confirm_symbol(self) -> bool:
        # QQEW has been retired from the live qqq_intraday_001 path.
        return False

    def _resolve_subscription_symbols(self, extras: Dict[str, Any]) -> list[str]:
        raw_symbols = extras.get("symbols")
        required = ["QQQ", "TQQQ", "SQQQ"]
        if self._use_confirm_symbol():
            required.insert(1, QQEW_SYMBOL)
        base_symbols = raw_symbols or required
        configured = [str(s).strip().upper() for s in base_symbols if str(s).strip()]
        # ADD feed is fully retired from this strategy; never subscribe it here.
        configured = [sym for sym in configured if sym not in {"AD-NASD", "AD-NDX"}]

        merged: list[str] = []
        seen: set[str] = set()
        for sym in configured + required:
            if sym in seen:
                continue
            seen.add(sym)
            merged.append(sym)

        if raw_symbols is not None:
            missing = [sym for sym in required if sym not in configured]
            if missing:
                logger.warning(
                    "qqq_subscription_symbols_autofilled",
                    strategy_id=self.strategy_id,
                    missing=missing,
                    configured=configured,
                    resolved=merged,
                )
        return merged

    async def initialize(self) -> None:
        await self.subscribe_system_health()
        self._running = True
        extras = self.params.extras or {}
        self._validate_shared_underlying_mode()
        symbols = self._resolve_subscription_symbols(extras)
        self._min_closes_maxlen = max(300, int(extras.get("minute_closes_maxlen", 3000)))
        state_dir = Path(str(extras.get("state_dir", "runtime/state"))).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = state_dir / f"qqq_intraday_{self.strategy_id}.json"
        atr_seed_cfg = extras.get("atr_seed_path", str(state_dir / f"qqq_intraday_atr_seed_{self.strategy_id}.json"))
        self._atr_seed_path = Path(str(atr_seed_cfg)).expanduser()
        self._atr_seed_path.parent.mkdir(parents=True, exist_ok=True)
        obs_cfg = extras.get("obs_snapshot_path", str(state_dir / f"qqq_intraday_observability_{self.strategy_id}.json"))
        self._obs_path = Path(str(obs_cfg)).expanduser()
        self._obs_path.parent.mkdir(parents=True, exist_ok=True)
        qqq_daily_atr_cfg = extras.get("qqq_daily_atr_path", str(state_dir / f"qqq_daily_atr_context_{self.strategy_id}.json"))
        self._qqq_daily_atr_path = Path(str(qqq_daily_atr_cfg)).expanduser()
        self._qqq_daily_atr_path.parent.mkdir(parents=True, exist_ok=True)
        diag_cfg = extras.get("diag_log_path", str(state_dir / f"qqq_intraday_diag_{self.strategy_id}.jsonl"))
        self._diag_path = Path(str(diag_cfg)).expanduser()
        self._diag_path.parent.mkdir(parents=True, exist_ok=True)
        claim_paths(
            strategy_id=self.strategy_id,
            paths=[
                self._state_path,
                self._atr_seed_path,
                self._qqq_daily_atr_path,
                self._obs_path,
                self._diag_path,
            ],
        )
        self._code_version = self._resolve_code_version()
        self._sfs = None
        self._initialized_at_et = datetime.now(ET)
        self._daily_reset_if_needed(self._initialized_at_et)

        self._restore_runtime_state()
        self._restore_atr_seed_into_pre()
        self._refresh_daily_atr_context(datetime.now(ET))
        self._arm_restart_degrade_window(self._initialized_at_et, extras)

        # Startup readiness: keep entry/position management inert until we have
        # at least one successful broker positions refresh (prevents restart races).
        try:
            now_mono = asyncio.get_running_loop().time()
        except RuntimeError:
            now_mono = 0.0
        timeout_sec = float(extras.get("startup_positions_ready_timeout_sec", 30.0))
        self._startup_positions_ready = False
        self._startup_positions_deadline_mono = now_mono + max(1.0, timeout_sec)
        self._last_startup_gate_warn_mono = 0.0
        self._last_startup_force_sync_mono = 0.0

        # Rebuild startup state before subscribing live ticks. If the live stream
        # advances _last_seen_ts_ms first, historical warmup rows are dropped as
        # rewinds and OR/seed reconstruction never happens after a mid-session restart.
        min_bars = max(20, int(extras.get("atr_pre_min_bars", 50)))
        warmup_n = int(extras.get("warmup_1s_n", max(6000, min_bars * 300 + 900)))
        await self._warmup_entry_seed_data()
        for sym in ("TQQQ", "SQQQ"):
            await self._warmup_5m(sym, warmup_n=warmup_n)
            self._refresh_atr_pre(sym, period=int(extras.get("atr_period", 14)))
        self._restore_atr_seed_into_pre()

        # Trigger IB/Kafka subscriptions after historical warmup to avoid the
        # startup replay racing the live stream.
        try:
            results = await self.ctx.subscribe_tick_data(symbols)
            logger.info("tick_subscribe_results", strategy_id=self.strategy_id, results=results)
        except Exception as exc:
            logger.warning("tick_subscribe_failed", strategy_id=self.strategy_id, error=str(exc))

        await self._refresh_ib_positions_best_effort(force=True)
        await self._sync_position_state(force=True)
        self._maybe_write_observability_file(datetime.now(ET))
        behavior_payload = {
            "strategy_id": self.strategy_id,
            "code_version": self._code_version,
            "legacy_trader_parity_mode": bool(self._legacy_trader_parity_mode(extras)),
            "enforce_stock_execution_blockers": bool(self._enforce_stock_execution_blockers_enabled(extras)),
            "startup_reconciled_stale_new_grace_sec": float(self._startup_reconciled_stale_new_grace_sec(extras)),
        }
        self._append_diag_event("qqq_intraday_initialized", behavior_payload)
        self._emit_local_audit(
            stage="strategy_behavior_profile",
            severity="info",
            status="configured",
            reason="strategy_behavior_profile",
            payload=behavior_payload,
        )

        loop_interval = float(extras.get("loop_interval_sec", 0.5))
        self._task = asyncio.create_task(self._run_loop(loop_interval), name=f"qqq_intraday_loop:{self.strategy_id}")
        if not self._loop_watchdog_started:
            self._loop_watchdog_task = asyncio.create_task(
                self._loop_watchdog(),
                name=f"qqq_intraday_loop_watchdog:{self.strategy_id}",
            )
            self._loop_watchdog_started = True
        logger.info(
            "qqq_intraday_initialized",
            strategy_id=self.strategy_id,
            symbols=symbols,
            loop_interval=loop_interval,
            code_version=self._code_version,
        )

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        if self._system_submit_blocked("on_features"):
            return
        # This strategy is self-driven from SFS-1s; ignore bar-based callbacks.
        return

    def _system_submit_blocked(self, reason: str) -> bool:
        if bool(getattr(self, "_system_paused", False)):
            return True
        controller = getattr(getattr(self, "ctx", None), "system_health", None)
        state = str(getattr(controller, "state", "") or "").strip().lower()
        return state in {"paused", "recovering", "degraded", "halted", "system_paused", "broker_degraded"}

    def _audit_system_submit_blocked(self, *, stage: str, reason: str, symbol: str = "", decision_id: str = "") -> None:
        self._emit_local_audit(
            stage=stage,
            severity="warning",
            status="blocked",
            reason="system_health_paused",
            symbol=str(symbol or self._exec_symbol or ""),
            decision_id=str(decision_id or ""),
            payload={"submit_reason": str(reason or ""), "system_paused": bool(getattr(self, "_system_paused", False))},
        )

    def _shared_underlying_data_plane_enabled(self) -> bool:
        return bool((self.params.extras or {}).get("enable_shared_underlying_data_plane", True))

    def _shared_underlying_namespace(self) -> str:
        configured = str((self.params.extras or {}).get("shared_underlying_namespace") or "").strip().lower()
        if configured:
            return configured
        return "qqq"

    def _shared_underlying_namespace_candidates(self) -> list[str]:
        primary = self._shared_underlying_namespace()
        # SFS-1s publisher defaults to "global". Keep backward-compatible qqq
        # namespace first, then fall back to global when the primary namespace
        # has no rows (common after runtime config drift/restarts).
        if primary == "global":
            return [primary]
        return [primary, "global"]

    def _shared_underlying_read_mode(self) -> str:
        raw = str((self.params.extras or {}).get("shared_underlying_read_mode") or "prefer").strip().lower()
        if raw in {"off", "disabled", "redis_only"}:
            return "off"
        if raw in {"plane_only", "shared_plane_only"}:
            return "plane_only"
        if raw in {"fallback", "redis_fallback"}:
            return "fallback"
        return "prefer"

    def _uses_sfs_reader(self) -> bool:
        return self._shared_underlying_read_mode() in {"prefer", "fallback"}

    def _validate_shared_underlying_mode(self) -> None:
        if self._shared_underlying_read_mode() == "plane_only" and (not self._shared_underlying_data_plane_enabled()):
            raise RuntimeError("qqq_shared_underlying_plane_required_for_plane_only_mode")

    def _shared_underlying_recent_rows(self, *, symbol: str, limit: int) -> list[Dict[str, Any]]:
        if not self._shared_underlying_data_plane_enabled():
            return []
        now_epoch_s = datetime.now(ET).timestamp()
        max_staleness_sec = max(1.0, float(self._freshness_limit_ms(symbol)) / 1000.0)
        for namespace in self._shared_underlying_namespace_candidates():
            try:
                rows = shared_underlying_data_plane().recent_bars(
                    symbol=symbol,
                    namespace=namespace,
                    limit=max(1, int(limit)),
                    max_staleness_sec=max_staleness_sec,
                    now_epoch_s=now_epoch_s,
                )
            except Exception:
                rows = ()
            if not rows:
                continue
            out: list[Dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    ts_ms = int(float(row.get("ts_epoch_s") or 0.0) * 1000.0)
                except Exception:
                    ts_ms = 0
                if ts_ms <= 0:
                    continue
                out.append(
                    {
                        "ts": ts_ms,
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "close": row.get("close"),
                        "volume": row.get("volume"),
                        "vwap": row.get("vwap"),
                    }
                )
            if out:
                if namespace != self._shared_underlying_namespace():
                    logger.debug(
                        "qqq_shared_underlying_namespace_fallback",
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        from_namespace=self._shared_underlying_namespace(),
                        to_namespace=namespace,
                        rows=len(out),
                    )
                return list(reversed(out))
        return []

    async def _recent_1s_rows(self, symbol: str, limit: int, *, force_sfs: bool = False) -> list[Dict[str, Any]]:
        mode = self._shared_underlying_read_mode()
        if mode == "off":
            return []
        if force_sfs and self._uses_sfs_reader():
            if self._sfs is None:
                self._sfs = SFS1sReader()
            try:
                rows = await self._sfs.last_n(symbol, max(1, int(limit)))
            except Exception:
                rows = []
            if rows:
                return [dict(r) for r in rows if isinstance(r, dict)]
        plane_rows = self._shared_underlying_recent_rows(symbol=symbol, limit=limit)
        if plane_rows or mode == "plane_only":
            return plane_rows
        if not self._uses_sfs_reader():
            return []
        if self._sfs is None:
            self._sfs = SFS1sReader()
        try:
            rows = await self._sfs.last_n(symbol, max(1, int(limit)))
        except Exception:
            rows = []
        return [dict(r) for r in rows if isinstance(r, dict)]

    def _publish_shared_underlying_data(self, *, symbol: str, ts_ms: int, close_px: float, vol: float) -> None:
        if not self._shared_underlying_data_plane_enabled():
            return
        try:
            plane = shared_underlying_data_plane()
            ts_epoch_s = float(ts_ms) / 1000.0
            features = {
                "ts_epoch_s": ts_epoch_s,
                "close": float(close_px),
                "last_price": float(close_px),
                "volume": float(vol or 0.0),
            }
            vwap_now = self._qqq_vwap if symbol == "QQQ" else self._qqew_vwap if symbol == QQEW_SYMBOL else None
            plane.publish_underlying(
                symbol=str(symbol or "").strip().upper(),
                namespace=self._shared_underlying_namespace(),
                ts_epoch_s=ts_epoch_s,
                last_price=float(close_px),
                vwap=vwap_now,
                features=features,
                bar={
                    "ts_epoch_s": ts_epoch_s,
                    "open": float(close_px),
                    "high": float(close_px),
                    "low": float(close_px),
                    "close": float(close_px),
                    "volume": float(vol or 0.0),
                    "vwap": vwap_now,
                },
            )
        except Exception:
            logger.debug(
                "qqq_shared_underlying_publish_failed",
                strategy_id=self.strategy_id,
                symbol=symbol,
                exc_info=True,
            )

    async def on_order_update(self, update: OrderUpdate) -> None:
        """
        Called when order status changes in IBKR.
        We use this for 'Ultimate Confirmation' of our entry.
        """
        if update.strategy_id != self.strategy_id:
            return

        order_ref = str(getattr(update, "order_ref", "") or "").upper()
        note = str(getattr(update, "note", "") or "").lower()
        update_trigger_id = self._extract_trigger_id_from_order(
            getattr(update, "order_ref", None),
            getattr(update, "note", None),
        )
        if update_trigger_id and (self._active_trigger_id is None):
            self._active_trigger_id = update_trigger_id
        broker_id_raw = getattr(update, "broker_order_id", None)
        broker_id = str(broker_id_raw or "").strip()
        has_broker_id = broker_id not in {"", "0", "None", "none", "null", "NULL"}
        state_norm = str(getattr(update, "state", "") or "").strip().replace(" ", "").replace("_", "").upper()
        is_terminal_state = state_norm in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED"}
        is_entry_ref = ":ENTRY" in order_ref
        is_entry_note = "qqq_intraday_entry" in note
        pending_age_sec = self._pending_decision_age_sec()
        pending_alive = (self._pending_decision_id is not None) and (pending_age_sec <= 120.0)
        is_entry_update = is_entry_ref or is_entry_note or pending_alive

        # Check if our entry order was filled
        if state_norm == "FILLED":
            # Ignore untrusted FILLED updates that have neither a broker order id
            # nor explicit entry identity. This prevents stale/replayed order updates
            # from being treated as real entry confirmations.
            if (not has_broker_id) and (not is_entry_ref) and (not is_entry_note):
                now_mono = asyncio.get_running_loop().time()
                if (now_mono - self._last_untrusted_fill_warn_mono) >= 10.0:
                    self._last_untrusted_fill_warn_mono = now_mono
                    logger.warning(
                        "qqq_untrusted_filled_update_ignored",
                        strategy_id=self.strategy_id,
                        symbol=update.symbol,
                        state=update.state,
                        broker_id=broker_id_raw,
                        order_ref=getattr(update, "order_ref", None),
                        trigger_id=update_trigger_id,
                        pending_decision_id=self._pending_decision_id,
                        pending_age_sec=round(float(pending_age_sec), 3),
                    )
                return
            if update.symbol in ("TQQQ", "SQQQ") and is_entry_update:
                confirm_sig = f"{update.symbol}|{broker_id or 'NA'}|{order_ref}|{state_norm}|{self._pending_decision_id or 'NA'}"
                now_mono = asyncio.get_running_loop().time()
                # Deduplicate bursty duplicate order updates from broker adapters.
                if self._last_fill_confirm_sig == confirm_sig and (now_mono - self._last_fill_confirm_mono) < 2.0:
                    return
                self._last_fill_confirm_sig = confirm_sig
                self._last_fill_confirm_mono = now_mono
                logger.info(
                    "qqq_ultimate_confirmation_received",
                    strategy_id=self.strategy_id,
                    symbol=update.symbol,
                    broker_id=broker_id_raw,
                    state=update.state,
                    trigger_id=update_trigger_id or self._active_trigger_id,
                )
                self._emit_local_audit(
                    stage="entry_confirmed",
                    status="filled",
                    symbol=update.symbol,
                    trigger_id=update_trigger_id or self._active_trigger_id,
                    trace_id=self._pending_decision_id or update_trigger_id or self._active_trigger_id,
                    payload={
                        "broker_order_id": broker_id_raw,
                        "order_ref": getattr(update, "order_ref", None),
                        "state": update.state,
                    },
                )
                self._is_traded = True
                self._clear_pending_decision("order_update_filled")
                self._persist_runtime_state(reason="fill_confirmed")
                self._emit_order_lifecycle_transition(
                    order_ref=getattr(update, "order_ref", None),
                    symbol=update.symbol,
                    after_state=OrderLifecycleState.FILLED,
                    reason="order_update:filled",
                    trigger_id=update_trigger_id or self._active_trigger_id,
                    trace_id=self._pending_decision_id or update_trigger_id or self._active_trigger_id,
                    payload={
                        "broker_order_id": broker_id_raw,
                        "state": update.state,
                    },
                )
            return

        if is_terminal_state and self._pending_decision_id is not None and is_entry_update:
            self._clear_pending_decision(f"order_update_terminal:{state_norm}")
            if not self._is_traded:
                self._active_trigger_id = None
            self._persist_runtime_state(reason=f"pending_cleared_terminal_{state_norm.lower()}")

        state_map = {
            "FILLED": OrderLifecycleState.FILLED,
            "CANCELLED": OrderLifecycleState.CANCELLED,
            "CANCELED": OrderLifecycleState.CANCELLED,
            "REJECTED": OrderLifecycleState.REJECTED,
            "INACTIVE": OrderLifecycleState.FAILED,
            "APICANCELLED": OrderLifecycleState.CANCELLED,
            "SUBMITTED": OrderLifecycleState.SUBMITTED,
            "PRESUBMITTED": OrderLifecycleState.SUBMITTED,
            "PENDINGSUBMIT": OrderLifecycleState.SUBMITTED,
            "APIPENDING": OrderLifecycleState.SUBMITTED,
            "PARTIAL": OrderLifecycleState.ACCEPTED,
        }
        mapped = state_map.get(state_norm)
        if mapped is not None:
            self._emit_order_lifecycle_transition(
                order_ref=getattr(update, "order_ref", None),
                symbol=getattr(update, "symbol", None),
                after_state=mapped,
                reason=f"order_update:{state_norm.lower()}",
                trigger_id=update_trigger_id or self._active_trigger_id,
                trace_id=self._pending_decision_id or update_trigger_id or self._active_trigger_id,
                payload={"broker_order_id": broker_id_raw, "state": update.state},
            )

    async def on_position_update(self, position: Any) -> None:
        try:
            await self._sync_position_state(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("qqq_on_position_update_failed", strategy_id=self.strategy_id, error=str(exc))

    async def on_shutdown(self, reason: str) -> None:
        self._running = False
        now_et = datetime.now(ET)
        day = now_et.strftime("%Y-%m-%d")
        self._persist_daily_atr_context(now_et)
        period = int((self.params.extras or {}).get("atr_period", 14))
        snapshot, snapshot_src = self._build_atr_seed_snapshot(period)
        has_today_seed = self._has_atr_seed_for_day(day)
        # Never overwrite/add ATR seed after 16:00 ET; keep RTH-close snapshot for next day.
        if now_et.time() > time(16, 0):
            logger.info(
                "qqq_atr_seed_shutdown_skip_afterhours",
                strategy_id=self.strategy_id,
                day=day,
                reason=reason,
            )
        # Avoid downgrading an existing EOD seed with stale preheat values during shutdown.
        elif snapshot is not None and not (snapshot_src == "pre" and has_today_seed):
            self._persist_atr_seed(
                day=day,
                atr_pre=snapshot,
                reason=f"shutdown:{reason}",
            )
        elif snapshot is not None and snapshot_src == "pre" and has_today_seed:
            logger.info(
                "qqq_atr_seed_shutdown_skip_pre_override",
                strategy_id=self.strategy_id,
                day=day,
                reason=reason,
            )
        self._persist_runtime_state(reason=f"shutdown:{reason}")
        if self._stop_audit_task:
            self._stop_audit_task.cancel()
            try:
                await self._stop_audit_task
            except Exception:
                pass
            self._stop_audit_task = None
            self._stop_audit_started = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        if self._loop_watchdog_task:
            self._loop_watchdog_task.cancel()
            try:
                await self._loop_watchdog_task
            except Exception:
                pass
            self._loop_watchdog_task = None
            self._loop_watchdog_started = False
        self._sfs = None
        release_paths(
            strategy_id=self.strategy_id,
            paths=[
                p
                for p in (
                    self._state_path,
                    self._atr_seed_path,
                    self._qqq_daily_atr_path,
                    self._obs_path,
                    self._diag_path,
                )
                if p is not None
            ],
        )
        logger.info("qqq_intraday_shutdown", strategy_id=self.strategy_id, reason=reason)

    # ----------------- Core loop -----------------

    async def _run_loop(self, interval_sec: float) -> None:
        extras = self.params.extras or {}
        timeout_sec = float(extras.get("data_timeout_sec", 60.0))
        slow_warn_sec = max(0.2, float(extras.get("loop_slow_warn_sec", 2.0)))
        slow_warn_interval_sec = max(5.0, float(extras.get("loop_slow_warn_interval_sec", 30.0)))
        start_ts = asyncio.get_running_loop().time()

        while True:
            if self._system_paused:
                paused_now = datetime.now(ET)
                self._loop_last_stage = "system_paused_ingest_required"
                try:
                    # Keep the self-driven data plane warm while submissions are
                    # paused so the strategy can recover automatically once the
                    # system health controller resumes trading.
                    await self._ingest_required(paused_now)
                    await self._maybe_retry_seed_warmup(paused_now)
                    self._maybe_lock_seed(paused_now)
                    self._refresh_atr_pre_by_schedule(paused_now)
                    self._maybe_persist_daily_atr_seed(paused_now)
                    self._refresh_stream_health_flag(paused_now)
                    if self._stable_entry_guard_enabled(extras):
                        data_failure = (
                            self._entry_data_integrity_failure(paused_now, extras)
                            if bool(extras.get("fail_closed_on_data_integrity", False))
                            else None
                        )
                        self._publish_entry_tradability_block(
                            "data_integrity_fail_closed" if data_failure else "",
                            active=bool(data_failure),
                        )
                except Exception as exc:
                    logger.warning(
                        "qqq_intraday_paused_ingest_failed",
                        strategy_id=self.strategy_id,
                        error=str(exc),
                    )
                self._loop_last_stage = "system_paused"
                self._maybe_write_observability_file(paused_now)
                self._maybe_emit_heartbeat(paused_now)
                await asyncio.sleep(interval_sec)
                continue
            loop_now_mono = asyncio.get_running_loop().time()
            now = datetime.now(ET)
            self._loop_iteration_seq += 1
            self._loop_in_iteration = True
            self._loop_current_started_mono = loop_now_mono
            self._loop_current_started_et = now
            self._loop_last_stage = "loop_begin"
            try:
                self._loop_last_stage = "daily_reset_if_needed"
                self._daily_reset_if_needed(now)

                self._loop_last_stage = "ingest_required"
                await self._ingest_required(now)
                self._loop_last_stage = "maybe_retry_seed_warmup"
                await self._maybe_retry_seed_warmup(now)
                self._loop_last_stage = "maybe_lock_seed"
                self._maybe_lock_seed(now)
                self._loop_last_stage = "refresh_atr_pre_by_schedule"
                self._refresh_atr_pre_by_schedule(now)
                self._loop_last_stage = "maybe_persist_daily_atr_seed"
                self._maybe_persist_daily_atr_seed(now)
                self._loop_last_stage = "maybe_write_observability_file"
                self._maybe_write_observability_file(now)
                self._loop_last_stage = "maybe_emit_heartbeat"
                self._maybe_emit_heartbeat(now)

                # Fail-fast readiness (after timeout, if still missing required numbers during RTH).
                if (asyncio.get_running_loop().time() - start_ts) > timeout_sec:
                    self._loop_last_stage = "fail_fast_if_missing"
                    self._fail_fast_if_missing(now)

                self._loop_last_stage = "update_crosscount"
                self._update_crosscount(now)
                self._loop_last_stage = "lock_amp_params"
                await self._lock_amp_params(now)

                self._loop_last_stage = "startup_positions_gate"
                ready = await self._startup_positions_gate(now)
                if ready:
                    self._loop_last_stage = "maybe_enter"
                    await self._maybe_enter(now)
                    self._loop_last_stage = "manage_position"
                    await self._manage_position(now)
                self._loop_last_stage = "force_flatten_1559"
                await self._force_flatten_1559(now)
                self._loop_last_stage = "sync_position_state"
                await self._sync_position_state()
                self._loop_last_stage = "persist_startup_reconciled_state"
                await self._maybe_persist_startup_reconciled_state()
            except asyncio.CancelledError:
                cancel_now = datetime.now(ET)
                elapsed_ms = max(0.0, (asyncio.get_running_loop().time() - self._loop_current_started_mono) * 1000.0)
                payload = {
                    "strategy_id": self.strategy_id,
                    "iteration_seq": int(self._loop_iteration_seq),
                    "loop_stage": str(self._loop_last_stage or "unknown"),
                    "elapsed_ms": round(float(elapsed_ms), 3),
                    "pending_decision_id": self._pending_decision_id,
                    "active_trigger_id": self._normalize_trigger_id(self._active_trigger_id),
                    "ts_et": cancel_now.isoformat(),
                }
                self._append_diag_event("qqq_loop_cancelled", payload)
                self._emit_local_audit(
                    stage="strategy_loop_cancelled",
                    severity="warning",
                    status="cancelled",
                    reason="loop_cancelled",
                    trace_id=(self._pending_decision_id or self._active_trigger_id),
                    payload=payload,
                )
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("qqq_intraday_loop_error", strategy_id=self.strategy_id, error=str(exc), exc_info=True)
                err_now = datetime.now(ET)
                elapsed_ms = max(0.0, (asyncio.get_running_loop().time() - self._loop_current_started_mono) * 1000.0)
                payload = {
                    "strategy_id": self.strategy_id,
                    "iteration_seq": int(self._loop_iteration_seq),
                    "loop_stage": str(self._loop_last_stage or "unknown"),
                    "error_class": exc.__class__.__name__,
                    "error": str(exc),
                    "elapsed_ms": round(float(elapsed_ms), 3),
                    "pending_decision_id": self._pending_decision_id,
                    "active_trigger_id": self._normalize_trigger_id(self._active_trigger_id),
                    "ts_et": err_now.isoformat(),
                }
                self._append_diag_event("qqq_loop_exception", payload)
                self._emit_local_audit(
                    stage="strategy_loop_exception",
                    severity="error",
                    status="failed",
                    reason=exc.__class__.__name__,
                    trace_id=(self._pending_decision_id or self._active_trigger_id),
                    payload=payload,
                )
                self._maybe_write_observability_file(datetime.now(ET))
            finally:
                done_mono = asyncio.get_running_loop().time()
                self._loop_last_finished_mono = done_mono
                self._loop_last_duration_ms = max(0.0, (done_mono - self._loop_current_started_mono) * 1000.0)
                if self._loop_last_duration_ms >= (slow_warn_sec * 1000.0) and (
                    (done_mono - self._loop_last_slow_warn_mono) >= slow_warn_interval_sec
                ):
                    self._loop_last_slow_warn_mono = done_mono
                    payload = {
                        "strategy_id": self.strategy_id,
                        "iteration_seq": int(self._loop_iteration_seq),
                        "loop_stage": str(self._loop_last_stage or "unknown"),
                        "elapsed_ms": round(float(self._loop_last_duration_ms), 3),
                        "slow_warn_sec": round(float(slow_warn_sec), 3),
                    }
                    self._append_diag_event("qqq_loop_slow_iteration", payload)
                    self._emit_local_audit(
                        stage="strategy_loop_slow_iteration",
                        severity="warning",
                        status="slow",
                        reason="loop_iteration_slow",
                        trace_id=(self._pending_decision_id or self._active_trigger_id),
                        payload=payload,
                    )
                self._loop_in_iteration = False
                self._loop_current_started_mono = 0.0
                self._loop_current_started_et = None

            await asyncio.sleep(max(0.1, interval_sec))

    async def _refresh_ib_positions_best_effort(self, *, force: bool = False) -> bool:
        extras = self.params.extras or {}
        interval_sec = max(0.1, float(extras.get("broker_positions_refresh_interval_sec", 2.0)))
        timeout_sec = max(0.5, float(extras.get("broker_positions_refresh_timeout_sec", 3.0)))
        now_mono = asyncio.get_running_loop().time()
        if (not force) and (now_mono - self._last_broker_positions_refresh_mono) < interval_sec:
            return bool(self._broker_positions_ready)

        try:
            engine = getattr(self.ctx, "engine", None)
            market_data_service = getattr(engine, "market_data_service", None)
            ib_adapter = getattr(market_data_service, "ib_adapter", None)
            ib = getattr(ib_adapter, "ib", None)
        except Exception:
            self._broker_positions_hard_confirmed = False
            return False
        if ib is None or (not hasattr(ib, "reqPositionsAsync")):
            self._broker_positions_hard_confirmed = False
            return False
        try:
            is_connected = getattr(ib, "isConnected", None)
            if callable(is_connected) and (not bool(is_connected())):
                self._broker_positions_hard_confirmed = False
                return False
        except Exception:
            self._broker_positions_hard_confirmed = False
            return False

        try:
            refresh_ret = ib.reqPositionsAsync()
            if inspect.isawaitable(refresh_ret):
                await asyncio.wait_for(refresh_ret, timeout=timeout_sec)
            elif hasattr(ib, "positions"):
                raw_positions = await self._ib_positions_with_timeout(source="refresh_sync_positions")
                if raw_positions is None:
                    self._broker_positions_hard_confirmed = False
                    return False
            self._broker_positions_ready = True
            self._broker_positions_hard_confirmed = True
            self._last_broker_positions_refresh_mono = now_mono
            return True
        except asyncio.TimeoutError as exc:
            # reqPositionsAsync() can occasionally stall; fall back to local positions cache
            # so strategy state sync can continue without noisy false alarms.
            fallback_ok = False
            fallback_error = ""
            try:
                if hasattr(ib, "positions"):
                    raw_positions = await self._ib_positions_with_timeout(source="refresh_timeout_fallback")
                    fallback_ok = raw_positions is not None
            except Exception as fb_exc:  # noqa: BLE001
                fallback_error = repr(fb_exc)

            if fallback_ok:
                self._broker_positions_ready = True
                self._broker_positions_hard_confirmed = False
                self._last_broker_positions_refresh_mono = now_mono
                fallback_log_every_sec = max(
                    1.0, float(extras.get("broker_positions_refresh_fallback_log_interval_sec", 30.0))
                )
                if (now_mono - self._last_broker_positions_fallback_log_mono) >= fallback_log_every_sec:
                    self._last_broker_positions_fallback_log_mono = now_mono
                    logger.info(
                        "qqq_ib_positions_refresh_timeout_fallback_used",
                        strategy_id=self.strategy_id,
                        timeout_sec=timeout_sec,
                    )
                return True

            warn_every_sec = max(2.0, float(extras.get("broker_positions_refresh_warn_interval_sec", 10.0)))
            if (now_mono - self._last_broker_positions_refresh_warn_mono) >= warn_every_sec:
                self._last_broker_positions_refresh_warn_mono = now_mono
                logger.warning(
                    "qqq_ib_positions_refresh_failed",
                    strategy_id=self.strategy_id,
                    error_type=type(exc).__name__,
                    error_repr=repr(exc),
                    timeout_sec=timeout_sec,
                    fallback_error=fallback_error,
                )
            return False
        except Exception as exc:
            self._broker_positions_hard_confirmed = False
            warn_every_sec = max(2.0, float(extras.get("broker_positions_refresh_warn_interval_sec", 10.0)))
            if (now_mono - self._last_broker_positions_refresh_warn_mono) >= warn_every_sec:
                self._last_broker_positions_refresh_warn_mono = now_mono
                logger.warning(
                    "qqq_ib_positions_refresh_failed",
                    strategy_id=self.strategy_id,
                    error_type=type(exc).__name__,
                    error_repr=repr(exc),
                    timeout_sec=timeout_sec,
                )
            return False

    async def _startup_positions_gate(self, now: datetime) -> bool:
        if self._startup_positions_ready:
            return True
        extras = self.params.extras or {}
        now_mono = asyncio.get_running_loop().time()

        await self._refresh_ib_positions_best_effort(force=True)
        if self._broker_positions_ready:
            self._startup_positions_ready = True
            logger.info("qqq_startup_positions_ready", strategy_id=self.strategy_id)
            return True

        force_sync_every_sec = max(0.5, float(extras.get("startup_force_sync_interval_sec", 1.0)))
        if (now_mono - self._last_startup_force_sync_mono) >= force_sync_every_sec:
            self._last_startup_force_sync_mono = now_mono
            await self._sync_position_state(force=True)

        warn_every_sec = max(2.0, float(extras.get("startup_gate_warn_interval_sec", 10.0)))
        if (now_mono - self._last_startup_gate_warn_mono) >= warn_every_sec:
            self._last_startup_gate_warn_mono = now_mono
            remaining = max(0.0, float(self._startup_positions_deadline_mono - now_mono))
            logger.warning(
                "qqq_startup_positions_not_ready",
                strategy_id=self.strategy_id,
                remaining_sec=round(remaining, 3),
            )

        # Degrade-open mode after startup deadline to avoid permanent "no-trade" lock.
        # Can be disabled via extras.startup_positions_gate_hard_block=true.
        hard_block = bool(extras.get("startup_positions_gate_hard_block", False))
        if now_mono >= float(self._startup_positions_deadline_mono):
            if hard_block:
                return False
            self._startup_positions_ready = True
            logger.error(
                "qqq_startup_positions_gate_degraded_open",
                strategy_id=self.strategy_id,
                reason="broker_positions_refresh_timeout",
            )
            return True
        return False

    async def _maybe_persist_startup_reconciled_state(self) -> None:
        if self._startup_reconciled_persisted:
            return
        if self._state_path is None:
            return
        if not self._startup_positions_ready:
            return
        if not self._broker_positions_ready:
            return
        if not self._broker_positions_hard_confirmed:
            return
        now_mono = asyncio.get_running_loop().time()
        try:
            open_orders = await self._ctx_open_orders_with_timeout()
        except Exception as exc:
            warn_interval_sec = max(
                5.0,
                float((self.params.extras or {}).get("startup_reconciled_warn_interval_sec", 30.0)),
            )
            if (now_mono - self._last_startup_reconciled_warn_mono) >= warn_interval_sec:
                self._last_startup_reconciled_warn_mono = now_mono
                logger.warning(
                    "qqq_startup_reconciled_skip_open_orders_unavailable",
                    strategy_id=self.strategy_id,
                    error=str(exc),
                    warn_interval_sec=warn_interval_sec,
                )
            return
        stale_new_grace_sec = float(
            self._startup_reconciled_stale_new_grace_sec(self.params.extras or {})
        )
        now_et = datetime.now(ET)

        def _is_stale_unsent_new(order: Any) -> bool:
            def _order_value(key: str) -> Any:
                if isinstance(order, dict):
                    return order.get(key)
                return getattr(order, key, None)

            try:
                if self._normalize_order_state(order) != "NEW":
                    return False
                broker_order_id = str(_order_value("broker_order_id") or "").strip()
                if broker_order_id:
                    return False
                submitted_at = _order_value("submitted_at")
                if submitted_at is not None:
                    return False
                created_at = _order_value("created_at")
                if created_at is None:
                    # OrderUpdate snapshots only carry `ts`; use it for stale-age fallback.
                    created_at = _order_value("ts")
                if created_at is None:
                    return False
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at)
                    except Exception:
                        return False
                if not isinstance(created_at, datetime):
                    return False
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=ET)
                age_sec = max(0.0, (now_et - created_at.astimezone(ET)).total_seconds())
                return stale_new_grace_sec > 0.0 and age_sec >= stale_new_grace_sec
            except Exception:
                return False

        has_active = any(
            ((o.get("strategy_id") if isinstance(o, dict) else getattr(o, "strategy_id", None)) == self.strategy_id)
            and (not self._is_terminal_order_state(o))
            and (not _is_stale_unsent_new(o))
            for o in open_orders
        )
        if has_active:
            warn_interval_sec = max(
                5.0,
                float((self.params.extras or {}).get("startup_reconciled_warn_interval_sec", 30.0)),
            )
            if (now_mono - self._last_startup_reconciled_warn_mono) >= warn_interval_sec:
                self._last_startup_reconciled_warn_mono = now_mono
                logger.warning(
                    "qqq_startup_reconciled_skip_active_orders",
                    strategy_id=self.strategy_id,
                    warn_interval_sec=warn_interval_sec,
                )
            return
        self._startup_reconciled_persisted = True
        self._persist_runtime_state(reason="startup_reconciled")

    async def _maybe_retry_seed_warmup(self, now: datetime) -> None:
        if (not self._all_day_test_mode()) and (
            now.weekday() > 4 or now.time() < time(9, 45) or now.time() > time(16, 0)
        ):
            return
        if self._seed_ready:
            return
        # Startup replay is only useful before this process has observed any live
        # QQQ bars. After that point, re-playing the full session just pushes old
        # rows back through intraday state and creates rewind churn without ever
        # improving seed recovery.
        if int(self._last_seen_ts_ms.get("QQQ", 0) or 0) > 0:
            return
        minute_key = _floor_minute(now)
        if self._last_seed_warmup_retry_minute == minute_key:
            return
        self._last_seed_warmup_retry_minute = minute_key
        await self._warmup_entry_seed_data(now=now)

    # ----------------- Fail-fast -----------------

    def _fatal(self, reason: str, **details: Any) -> None:
        logger.error("qqq_intraday_fatal", strategy_id=self.strategy_id, reason=reason, **details)
        extras = self.params.extras or {}
        if bool(extras.get("fail_fast_enabled", False)):
            # Ask the system to shutdown gracefully.
            signal.raise_signal(signal.SIGTERM)
        else:
            # Debug mode: do not stop the system; just mark degraded.
            self.state["degraded"] = True
            self.state["degraded_reason"] = reason
            self.state["degraded_details"] = details

    def _fail_fast_if_missing(self, now: datetime) -> None:
        # During debug, missing data should not stop the system unless explicitly enabled.
        extras = self.params.extras or {}
        if not bool(extras.get("fail_fast_enabled", False)):
            # In debug mode, suppress missing-data noise outside RTH.
            if now.weekday() > 4:
                return
            # Start debug missing checks near entry window to avoid 09:30~09:45 ATR warmup noise.
            if now.time() < time(9, 45) or now.time() > time(16, 0):
                return
            missing = self._missing_required(now)
            if missing:
                debug_warn_streak = max(1, int(extras.get("debug_missing_warn_streak", 3)))
                minute_ts = _floor_minute(now)
                signature = ",".join(sorted(str(x) for x in missing))
                if signature == self._missing_debug_signature:
                    self._missing_debug_streak += 1
                else:
                    self._missing_debug_signature = signature
                    self._missing_debug_streak = 1
                if self._last_missing_debug_minute != minute_ts:
                    self._last_missing_debug_minute = minute_ts
                    if self._missing_debug_streak >= debug_warn_streak:
                        logger.warning(
                            "qqq_required_data_missing(debug)",
                            strategy_id=self.strategy_id,
                            missing=missing,
                            streak=self._missing_debug_streak,
                            threshold=debug_warn_streak,
                        )
                    else:
                        logger.info(
                            "qqq_required_data_missing(debug_suppressed)",
                            strategy_id=self.strategy_id,
                            missing=missing,
                            streak=self._missing_debug_streak,
                            threshold=debug_warn_streak,
                        )
            else:
                self._ff_miss_count = 0
                self._missing_debug_streak = 0
                self._missing_debug_signature = ""
            return

        # Only enforce during RTH window where the strategy needs data.
        if now.weekday() > 4:
            return
        # With rth_only=true upstream, ticks start at/after 09:30:00 ET.
        # Do not fail-fast before the open window has had a chance to produce data.
        if now.time() < time(9, 30, 15) or now.time() > time(16, 0):
            return

        missing = self._missing_required(now)
        if missing:
            self._ff_miss_count += 1
            miss_n = max(1, int((self.params.extras or {}).get("fail_fast_consecutive_misses", 6)))
            cool_sec = max(1.0, float((self.params.extras or {}).get("fail_fast_cooldown_sec", 120.0)))
            loop_now = asyncio.get_running_loop().time()
            if self._ff_miss_count >= miss_n and (loop_now - self._ff_last_trigger_ts) >= cool_sec:
                self._ff_last_trigger_ts = loop_now
                self._fatal("required_data_missing", missing=missing, miss_count=self._ff_miss_count)
        else:
            self._ff_miss_count = 0

    def _missing_required(self, now: datetime) -> list[str]:
        missing: list[str] = []
        now_ms = int(now.timestamp() * 1000)
        extras = self.params.extras or {}
        elite_decouple_qqew = self._qqew_decoupled()

        def _is_fresh(symbol: str) -> bool:
            ts = int(self._last_seen_ts_ms.get(symbol, 0) or 0)
            if ts <= 0:
                return False
            fresh_limit_ms = self._freshness_limit_ms(symbol)
            return (now_ms - ts) <= fresh_limit_ms

        q_last = self._last_price("QQQ")
        if not q_last:
            missing.append("QQQ:last")
        elif not _is_fresh("QQQ"):
            missing.append("QQQ:last_stale")
        if self._vwap() <= 0:
            missing.append("QQQ:vwap")
        if not elite_decouple_qqew:
            qqew_last = self._last_price(QQEW_SYMBOL)
            if not qqew_last:
                missing.append(f"{QQEW_SYMBOL}:last")
            elif not _is_fresh(QQEW_SYMBOL):
                missing.append(f"{QQEW_SYMBOL}:last_stale")
            if self._qqew_vwap() <= 0:
                missing.append(f"{QQEW_SYMBOL}:vwap")
        # ATR is only required when strategy is near/within entry-management window.
        require_atr = now.time() >= time(9, 45) or self._is_traded
        if require_atr:
            atr_p = int((self.params.extras or {}).get("atr_period", 14))
            if self._effective_atr("TQQQ", atr_p, now=now) is None:
                missing.append(f"TQQQ:atr5m{atr_p}")
            if self._effective_atr("SQQQ", atr_p, now=now) is None:
                missing.append(f"SQQQ:atr5m{atr_p}")

        return missing

    # ----------------- Ingestion / aggregation -----------------

    async def _warmup_5m(self, symbol: str, warmup_n: int) -> None:
        if not self._shared_underlying_data_plane_enabled():
            return
        rows = await self._recent_1s_rows(symbol, warmup_n, force_sfs=True)
        if not rows:
            return
        for r in reversed(rows):
            await self._ingest_1s(symbol, r)
        logger.info("qqq_intraday_warmup_5m_done", strategy_id=self.strategy_id, symbol=symbol, n=len(rows))

    async def _warmup_entry_seed_data(self, now: Optional[datetime] = None) -> None:
        """Best-effort intraday warmup for QQQ to survive mid-session restarts."""
        if not self._shared_underlying_data_plane_enabled():
            return
        extras = self.params.extras or {}
        now = now or datetime.now(ET)
        day = now.date()
        session_start = datetime.combine(day, time(9, 30), tzinfo=ET)
        # Pull enough 1s rows to include the full RTH session so 09:30~09:45 seed
        # can be reconstructed after mid-session restarts.
        elapsed_sec = max(0, int((now - session_start).total_seconds()))
        dynamic_n = elapsed_sec + 600  # add 10-minute safety margin
        warmup_floor = int(extras.get("seed_warmup_1s_floor", 1200))
        warmup_cap = int(extras.get("seed_warmup_1s_cap", 50000))
        warmup_override = extras.get("seed_warmup_1s_n")
        if warmup_override is not None:
            warmup_n = max(warmup_floor, int(warmup_override))
        else:
            warmup_n = max(warmup_floor, dynamic_n)
        warmup_n = min(warmup_n, warmup_cap)
        seed_symbols = ["QQQ"]
        if self._use_confirm_symbol():
            seed_symbols.append(QQEW_SYMBOL)
        loaded: Dict[str, int] = {sym: 0 for sym in seed_symbols}

        for sym in seed_symbols:
            rows = await self._recent_1s_rows(sym, warmup_n, force_sfs=True)
            if not rows:
                continue
            for r in reversed(rows):
                ts_ms = int(r.get("ts") or 0)
                if ts_ms <= 0:
                    continue
                dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
                if dt.date() != day:
                    continue
                if (not self._all_day_test_mode()) and dt.time() < time(9, 30):
                    continue
                await self._ingest_1s(sym, r, now_hint=now)
                loaded[sym] += 1

        # If process starts after 09:45 and seed is recoverable from SFS, lock immediately.
        self._maybe_lock_seed(now)
        logger.info(
            "qqq_entry_seed_warmup_done",
            strategy_id=self.strategy_id,
            warmup_n=warmup_n,
            elapsed_sec=elapsed_sec,
            loaded=loaded,
            seed_ready=self._seed_ready,
            seed_n=len(self._seed_velocity_samples),
            orh=self._orh,
            orl=self._orl,
            v_seed_sigma=self._v_seed_sigma,
        )

    async def _ingest_required(self, now: datetime) -> None:
        if not self._shared_underlying_data_plane_enabled():
            return
        stream_symbols = ["QQQ", "TQQQ", "SQQQ"]
        if self._use_confirm_symbol():
            stream_symbols.insert(1, QQEW_SYMBOL)
        for sym in stream_symbols:
            # Pull a small tail and ingest unseen seconds (handles loop jitter).
            rows = await self._recent_1s_rows(sym, 5)
            for r in reversed(rows):
                await self._ingest_1s(sym, r, now_hint=now)

    async def _ingest_1s(self, symbol: str, row: Dict[str, Any], now_hint: Optional[datetime] = None) -> None:
        ts_ms = int(row.get("ts") or 0)
        if ts_ms <= 0:
            return
        last = self._last_seen_ts_ms.get(symbol, 0)
        if ts_ms <= last:
            # Self-heal after feed/service restarts that can rewind timestamps.
            rewind_ms = int((self.params.extras or {}).get("stale_rewind_recover_ms", 120_000))
            if (last - ts_ms) <= rewind_ms:
                return
            # Deduplicate repeated severe rewind rows carrying the same stale ts.
            # This frequently happens when polling Redis tail off-hours with sparse ticks.
            if int(self._rewind_last_incoming_ts_ms_by_symbol.get(symbol, 0) or 0) == ts_ms:
                return
            self._rewind_last_incoming_ts_ms_by_symbol[symbol] = ts_ms
            # Off-hours stale rewind rows are expected on sparse feeds; keep hard-drop
            # protection but suppress warning noise outside regular session.
            now_ref = now_hint or datetime.now(ET)
            if (not self._all_day_test_mode()) and (not self._is_regular_session(now_ref)):
                return
            now_mono = asyncio.get_running_loop().time()
            warn_interval_sec = max(2.0, float((self.params.extras or {}).get("stale_rewind_warn_interval_sec", 10.0)))
            dropped_count = int(self._rewind_drop_count_by_symbol.get(symbol, 0) or 0) + 1
            self._rewind_drop_count_by_symbol[symbol] = dropped_count
            last_warn = float(self._rewind_last_warn_mono_by_symbol.get(symbol, 0.0) or 0.0)
            if (now_mono - last_warn) >= warn_interval_sec:
                self._rewind_last_warn_mono_by_symbol[symbol] = now_mono
                logger.warning(
                    "qqq_ts_rewind_recovered",
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    last_ts_ms=last,
                    incoming_ts_ms=ts_ms,
                    rewind_ms=(last - ts_ms),
                    dropped_count=dropped_count,
                    warn_interval_sec=warn_interval_sec,
                )
                self._rewind_drop_count_by_symbol[symbol] = 0
            # Hard drop severe rewind rows. Accepting an old bar here can pollute
            # minute/VWAP state and trigger false exits (e.g. logic_retreat).
            return
        self._rewind_last_incoming_ts_ms_by_symbol.pop(symbol, None)
        self._last_seen_ts_ms[symbol] = ts_ms

        close_px = _safe_float(row.get("close"))
        vol = _safe_float(row.get("volume")) or 0.0
        if close_px is None:
            return
        if close_px <= 0:
            return

        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
        if (not self._all_day_test_mode()) and (not self._is_regular_session(dt)):
            # Keep freshness timestamp, but ignore non-RTH bars to prevent overnight drift
            # from polluting intraday gates/aggregates.
            return

        self._publish_shared_underlying_data(
            symbol=symbol,
            ts_ms=ts_ms,
            close_px=float(close_px),
            vol=float(vol or 0.0),
        )

        if symbol == "QQQ":
            self._update_minute_close(ts_ms, close_px, vol)
            self._update_vwap(ts_ms, close_px, vol)
            self._qqq_buffer.append((ts_ms, float(close_px)))
            if time(9, 30) <= dt.time() < time(9, 45):
                self._orh = float(close_px) if self._orh is None else max(self._orh, float(close_px))
                self._orl = float(close_px) if self._orl is None else min(self._orl, float(close_px))
                qqq_ago = self._series_value_at_or_before(self._qqq_buffer, ts_ms - 180_000)
                if qqq_ago is not None:
                    self._seed_velocity_samples.append(float(close_px - qqq_ago))
        elif symbol == QQEW_SYMBOL:
            self._update_qqew_vwap(ts_ms, close_px, vol)
            self._qqew_last_price = float(close_px)
            self._qqew_buffer.append((ts_ms, float(close_px)))
            minute_end = _floor_minute(dt) + timedelta(minutes=1)
            if minute_end not in self._qqew_minute:
                self._qqew_minute_keys.append(minute_end)
            self._qqew_minute[minute_end] = float(close_px)
            while len(self._qqew_minute_keys) > self._min_closes_maxlen:
                old = self._qqew_minute_keys.popleft()
                self._qqew_minute.pop(old, None)
        elif symbol in ("TQQQ", "SQQQ"):
            self._update_5m_bar(symbol, ts_ms, close_px, vol)
            minute_end = _floor_minute(dt) + timedelta(minutes=1)
            if minute_end not in self._exec_min_closes[symbol]:
                self._exec_min_close_keys[symbol].append(minute_end)
            self._exec_min_closes[symbol][minute_end] = float(close_px)
            while len(self._exec_min_close_keys[symbol]) > self._min_closes_maxlen:
                old = self._exec_min_close_keys[symbol].popleft()
                self._exec_min_closes[symbol].pop(old, None)

    def _update_vwap(self, ts_ms: int, price: float, vol: float) -> None:
        # VWAP starts accumulating from 09:30 ET each day.
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
        if (not self._all_day_test_mode()) and (dt.time() < time(9, 30) or dt.time() > time(16, 0)):
            return
        if self._vwap_last_ts_ms and ts_ms < self._vwap_last_ts_ms:
            return
        self._vwap_last_ts_ms = ts_ms
        if vol > 0:
            self._vwap_pv += price * vol
            self._vwap_v += vol

        # Compute Price_base window (09:30:00 - 09:30:10) using 1s closes weighted by 1s volume.
        if self._price_base is None and time(9, 30) <= dt.time() < time(9, 30, 11):
            if vol > 0 and dt.time() < time(9, 30, 10):
                self._price_base_pv += price * vol
                self._price_base_v += vol
            if dt.time() >= time(9, 30, 10) and self._price_base_v > 0:
                self._price_base = self._price_base_pv / self._price_base_v
                self._price_base_source = "sfs_0930_window"
                logger.info("qqq_price_base_locked", strategy_id=self.strategy_id, price_base=round(self._price_base, 4))
                # Persist immediately so mid-session restart can restore price_base.
                self._persist_runtime_state(reason="price_base_locked")

    def _update_qqew_vwap(self, ts_ms: int, price: float, vol: float) -> None:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
        if (not self._all_day_test_mode()) and (dt.time() < time(9, 30) or dt.time() > time(16, 0)):
            return
        if self._qqew_vwap_last_ts_ms and ts_ms < self._qqew_vwap_last_ts_ms:
            return
        self._qqew_vwap_last_ts_ms = ts_ms
        if vol > 0:
            self._qqew_vwap_pv += price * vol
            self._qqew_vwap_v += vol

    @staticmethod
    def _weighted_price_from_rows(
        rows: list[Dict[str, Any]],
        *,
        start_ts_ms: Optional[int] = None,
        end_ts_ms: Optional[int] = None,
    ) -> tuple[Optional[float], int]:
        pv = 0.0
        vv = 0.0
        closes: list[float] = []
        used = 0
        for r in rows:
            ts_ms = int(r.get("ts") or 0)
            if ts_ms <= 0:
                continue
            if start_ts_ms is not None and ts_ms < start_ts_ms:
                continue
            if end_ts_ms is not None and ts_ms >= end_ts_ms:
                continue
            close_px = _safe_float(r.get("close"))
            if close_px is None or close_px <= 0:
                continue
            vol = _safe_float(r.get("volume")) or 0.0
            closes.append(float(close_px))
            used += 1
            if vol > 0:
                pv += float(close_px) * vol
                vv += vol
        if vv > 0:
            return float(pv / vv), used
        if closes:
            return float(sum(closes) / len(closes)), used
        return None, 0

    async def _recover_price_base(self, now: datetime, *, init_after_window: bool) -> bool:
        minute_ts = _floor_minute(now)
        if self._price_base_recovery_last_minute == minute_ts:
            return False
        self._price_base_recovery_last_minute = minute_ts

        if not self._shared_underlying_data_plane_enabled():
            return False

        extras = self.params.extras or {}
        scan_n = max(60, int(extras.get("price_base_recovery_scan_n", 12000)))
        rows = await self._recent_1s_rows("QQQ", scan_n)
        if not rows:
            rows = []

        reason: Optional[str] = None
        price_base: Optional[float] = None
        used = 0

        # Preferred: reconstruct from exact 09:30:00-09:30:10 ET window.
        window_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        window_end = now.replace(hour=9, minute=30, second=10, microsecond=0)
        price_base, used = self._weighted_price_from_rows(
            rows,
            start_ts_ms=int(window_start.timestamp() * 1000),
            end_ts_ms=int(window_end.timestamp() * 1000),
        )
        if price_base is not None:
            reason = "sfs_0930_window"

        # Fallback: startup anchor (10s before process init).
        if price_base is None and init_after_window and self._initialized_at_et is not None:
            init_end_ms = int(self._initialized_at_et.timestamp() * 1000)
            init_start_ms = init_end_ms - 10_000
            price_base, used = self._weighted_price_from_rows(
                rows,
                start_ts_ms=init_start_ms,
                end_ts_ms=init_end_ms,
            )
            if price_base is not None:
                reason = "startup_anchor_10s"

        # Final fallback: IBKR historical (1s preferred, then 1m 09:30 open).
        if price_base is None:
            ib_price, ib_reason, ib_samples = await self._recover_price_base_from_ibkr(now)
            if ib_price is not None:
                price_base = ib_price
                reason = ib_reason
                used = ib_samples

        if price_base is None:
            return False

        self._price_base = float(price_base)
        self._price_base_source = reason
        logger.warning(
            "qqq_price_base_recovered",
            strategy_id=self.strategy_id,
            price_base=round(self._price_base, 4),
            reason=reason,
            samples=used,
        )
        self._append_diag_event(
            "price_base_recovered",
            {
                "strategy_id": self.strategy_id,
                "price_base": self._price_base,
                "reason": reason,
                "samples": used,
            },
        )
        self._persist_runtime_state(reason=f"price_base_recovered:{reason}")
        return True

    @staticmethod
    def _bar_dt_et(raw_dt: Any) -> Optional[datetime]:
        if isinstance(raw_dt, datetime):
            return raw_dt.astimezone(ET) if raw_dt.tzinfo else raw_dt.replace(tzinfo=ET)
        if isinstance(raw_dt, str):
            dt = None
            try:
                dt = datetime.fromisoformat(raw_dt)
            except Exception:
                dt = None
            if dt is None:
                return None
            return dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)
        return None

    async def _recover_price_base_from_ibkr(self, now: datetime) -> tuple[Optional[float], str, int]:
        extras = self.params.extras or {}
        cooldown_sec = max(60.0, float(extras.get("price_base_ibkr_retry_sec", 300.0)))
        now_ts = asyncio.get_running_loop().time()
        if (now_ts - self._price_base_ibkr_last_attempt_ts) < cooldown_sec:
            return None, "ibkr_cooldown", 0
        self._price_base_ibkr_last_attempt_ts = now_ts

        ib = None
        try:
            eng = getattr(self.ctx, "engine", None)
            md = getattr(eng, "market_data_service", None)
            ib_adapter = getattr(md, "ib_adapter", None)
            ib = getattr(ib_adapter, "ib", None)
        except Exception:
            ib = None
        if ib is None or (not bool(getattr(ib, "isConnected", lambda: False)())):
            return None, "ibkr_unavailable", 0

        conn_manager = None
        try:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            conn_manager = get_ib_connection_manager()
        except Exception:
            conn_manager = None
        if conn_manager is not None and hasattr(conn_manager, "allow_market_request"):
            decision = conn_manager.allow_market_request(
                kind="historical_fallback",
                key="QQQ:price_base",
                requester=str(self.strategy_id),
            )
            if not decision.allowed:
                return None, f"ibkr_governor_blocked:{decision.reason}", 0

        try:
            from ib_insync import Stock
        except Exception:
            return None, "ib_insync_missing", 0

        contract = self._ibkr_qqq_contract or Stock("QQQ", "SMART", "USD")
        try:
            if self._ibkr_qqq_contract is None:
                qualified = await ib.qualifyContractsAsync(contract)
                if qualified:
                    contract = qualified[0]
                self._ibkr_qqq_contract = contract
        except Exception:
            self._ibkr_qqq_contract = contract

        window_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        window_end = now.replace(hour=9, minute=30, second=10, microsecond=0)
        ws_ms = int(window_start.timestamp() * 1000)
        we_ms = int(window_end.timestamp() * 1000)

        # 1) Preferred IBKR 1s bars in exact 09:30:00-09:30:10 ET window.
        try:
            bars_1s = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime=window_end,
                durationStr=str(extras.get("price_base_ibkr_1s_duration", "20 S")),
                barSizeSetting="1 secs",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
            )
            rows_1s: list[Dict[str, Any]] = []
            for b in list(bars_1s or []):
                dt_et = self._bar_dt_et(getattr(b, "date", None))
                if dt_et is None:
                    continue
                rows_1s.append(
                    {
                        "ts": int(dt_et.timestamp() * 1000),
                        "close": _safe_float(getattr(b, "close", None)),
                        "volume": _safe_float(getattr(b, "volume", 0.0)) or 0.0,
                    }
                )
            px_1s, used_1s = self._weighted_price_from_rows(rows_1s, start_ts_ms=ws_ms, end_ts_ms=we_ms)
            if px_1s is not None:
                if conn_manager is not None and hasattr(conn_manager, "record_market_request_result"):
                    conn_manager.record_market_request_result(
                        kind="historical_fallback",
                        key="QQQ:price_base",
                        ok=True,
                    )
                return float(px_1s), "ibkr_1s_0930_window", int(used_1s)
        except Exception as exc:
            logger.warning("qqq_price_base_ibkr_1s_failed", strategy_id=self.strategy_id, error=str(exc))

        # 2) Secondary fallback: 1m 09:30 bar open.
        try:
            one_min_end = now.replace(hour=9, minute=31, second=0, microsecond=0)
            bars_1m = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime=one_min_end,
                durationStr=str(extras.get("price_base_ibkr_1m_duration", "180 S")),
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
            )
            for b in list(bars_1m or []):
                dt_et = self._bar_dt_et(getattr(b, "date", None))
                if dt_et is None:
                    continue
                if dt_et.hour == 9 and dt_et.minute == 30:
                    px_open = _safe_float(getattr(b, "open", None))
                    if px_open and px_open > 0:
                        if conn_manager is not None and hasattr(conn_manager, "record_market_request_result"):
                            conn_manager.record_market_request_result(
                                kind="historical_fallback",
                                key="QQQ:price_base",
                                ok=True,
                            )
                        return float(px_open), "ibkr_1m_0930_open", 1
                    px_close = _safe_float(getattr(b, "close", None))
                    if px_close and px_close > 0:
                        if conn_manager is not None and hasattr(conn_manager, "record_market_request_result"):
                            conn_manager.record_market_request_result(
                                kind="historical_fallback",
                                key="QQQ:price_base",
                                ok=True,
                            )
                        return float(px_close), "ibkr_1m_0930_close", 1
        except Exception as exc:
            logger.warning("qqq_price_base_ibkr_1m_failed", strategy_id=self.strategy_id, error=str(exc))

        if conn_manager is not None and hasattr(conn_manager, "record_market_request_result"):
            conn_manager.record_market_request_result(
                kind="historical_fallback",
                key="QQQ:price_base",
                ok=False,
                reason="ibkr_no_data",
            )
        return None, "ibkr_no_data", 0

    def _update_minute_close(self, ts_ms: int, price: float, vol: float) -> None:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
        minute_start = _floor_minute(dt)
        if self._min_bar is None or self._min_bar.ts_start != minute_start:
            # finalize previous minute bar (store close by ts_end)
            if self._min_bar is not None:
                self._minute_bars.append(self._min_bar)
                vol_val = float(max(0.0, self._min_bar.volume))
                self._rolling_vol_queue.append(vol_val)
                self._today_vol_series.append(vol_val)
                key = self._min_bar.ts_end
                if key not in self._min_closes:
                    self._min_close_keys.append(key)
                self._min_closes[key] = float(self._min_bar.close)
                if key not in self._vwap_minute:
                    self._vwap_minute_keys.append(key)
                self._vwap_minute[key] = float(self._vwap())
                while len(self._min_close_keys) > self._min_closes_maxlen:
                    old = self._min_close_keys.popleft()
                    self._min_closes.pop(old, None)
                while len(self._vwap_minute_keys) > self._min_closes_maxlen:
                    old_v = self._vwap_minute_keys.popleft()
                    self._vwap_minute.pop(old_v, None)
            self._min_bar = _OhlcvBar(
                ts_start=minute_start,
                ts_end=minute_start + timedelta(minutes=1),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=float(max(0.0, vol)),
            )
        else:
            self._min_bar.high = max(self._min_bar.high, price)
            self._min_bar.low = min(self._min_bar.low, price)
            self._min_bar.close = price
            if vol > 0:
                self._min_bar.volume += vol

    def _update_5m_bar(self, symbol: str, ts_ms: int, price: float, vol: float) -> None:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
        start = _floor_5m(dt)
        cur = self._cur_5m.get(symbol)
        if cur is None or cur.ts_start != start:
            if cur is not None:
                self._bars_5m[symbol].append(cur)
            self._cur_5m[symbol] = _OhlcvBar(
                ts_start=start,
                ts_end=start + timedelta(minutes=5),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=float(max(0.0, vol)),
            )
            return
        cur.high = max(cur.high, price)
        cur.low = min(cur.low, price)
        cur.close = price
        if vol > 0:
            cur.volume += vol

    def _last_price(self, symbol: str) -> Optional[float]:
        ts = self._last_seen_ts_ms.get(symbol)
        if not ts:
            return None
        # We don't keep last price explicitly for all symbols; infer from last ingested row by series/deques.
        if symbol == "QQQ":
            if self._min_bar is not None:
                return float(self._min_bar.close)
            return None
        if symbol == QQEW_SYMBOL:
            return float(self._qqew_last_price) if self._qqew_last_price is not None else None
        if symbol in ("TQQQ", "SQQQ"):
            cur = self._cur_5m.get(symbol)
            return float(cur.close) if cur else None
        return None

    def _vwap(self) -> float:
        if self._vwap_v <= 0:
            return 0.0
        return float(self._vwap_pv / self._vwap_v)

    def _qqew_vwap(self) -> float:
        if self._qqew_vwap_v <= 0:
            return 0.0
        return float(self._qqew_vwap_pv / self._qqew_vwap_v)

    def _effective_atr_observed(self, symbol: str, period: int, now: datetime) -> tuple[Optional[float], str, Optional[float], Optional[float]]:
        atr_live = self._atr_5m(symbol, period)
        if atr_live is not None and atr_live > 0:
            return float(atr_live), "live_5m", float(atr_live), self._atr_pre.get(symbol)
        atr_pre = self._atr_pre.get(symbol)
        if atr_pre is not None and atr_pre > 0:
            return float(atr_pre), "atr_pre", None, float(atr_pre)
        if now.time() >= time(9, 45) or self._all_day_test_mode():
            fallback = 0.35 if symbol == "TQQQ" else 0.30
            return float(fallback), "fallback", None, None
        return None, "missing", None, None

    def _missing_required_observed(self, now: datetime) -> list[str]:
        missing: list[str] = []
        now_ms = int(now.timestamp() * 1000)
        extras = self.params.extras or {}
        elite_decouple_qqew = self._qqew_decoupled()

        def _is_fresh(symbol: str) -> bool:
            ts = int(self._last_seen_ts_ms.get(symbol, 0) or 0)
            if ts <= 0:
                return False
            fresh_limit_ms = self._freshness_limit_ms(symbol)
            return (now_ms - ts) <= fresh_limit_ms

        if not self._last_price("QQQ"):
            missing.append("QQQ:last")
        elif not _is_fresh("QQQ"):
            missing.append("QQQ:last_stale")
        if self._vwap() <= 0:
            missing.append("QQQ:vwap")
        if not elite_decouple_qqew:
            if not self._last_price(QQEW_SYMBOL):
                missing.append(f"{QQEW_SYMBOL}:last")
            elif not _is_fresh(QQEW_SYMBOL):
                missing.append(f"{QQEW_SYMBOL}:last_stale")
            if self._qqew_vwap() <= 0:
                missing.append(f"{QQEW_SYMBOL}:vwap")
        require_atr = now.time() >= time(9, 45) or self._is_traded
        if require_atr:
            atr_p = int((self.params.extras or {}).get("atr_period", 14))
            t_eff, _, _, _ = self._effective_atr_observed("TQQQ", atr_p, now)
            s_eff, _, _, _ = self._effective_atr_observed("SQQQ", atr_p, now)
            if t_eff is None:
                missing.append(f"TQQQ:atr5m{atr_p}")
            if s_eff is None:
                missing.append(f"SQQQ:atr5m{atr_p}")
        return missing

    def _latest_minute_close(self) -> tuple[Optional[float], Optional[datetime]]:
        if self._min_close_keys:
            key = self._min_close_keys[-1]
            return self._min_closes.get(key), key
        if self._min_bar is not None:
            return float(self._min_bar.close), self._min_bar.ts_start
        return None, None

    def _symbol_age_sec(self, symbol: str, now: datetime) -> Optional[float]:
        ts_ms = int(self._last_seen_ts_ms.get(symbol, 0) or 0)
        if ts_ms <= 0:
            return None
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
        return max(0.0, (now - dt).total_seconds())

    def _freshness_limit_ms(self, symbol: str) -> int:
        extras = self.params.extras or {}
        base_sec = float(extras.get("freshness_max_sec", 5.0))
        key = f"freshness_max_sec_{symbol.lower().replace('-', '_')}"
        sec = float(extras.get(key, base_sec))
        return int(max(1.0, sec) * 1000)

    def _qqew_decoupled(self) -> bool:
        extras = self.params.extras or {}
        return (
            (not self._use_confirm_symbol())
            or bool(extras.get("elite_decouple_qqew", False))
            or bool(extras.get("elite_ignore_qqew_resonance", False))
        )

    def _current_entry_metrics(self, now: datetime) -> Dict[str, float]:
        now_ms = int(now.timestamp() * 1000)
        qqq_now = self._last_price("QQQ")
        qqq_ago = self._qqq_value_at_or_before(now_ms - 180_000)
        qqew_decoupled = self._qqew_decoupled()
        qqew_now = None if qqew_decoupled else self._last_price(QQEW_SYMBOL)
        qqew_vwap = 0.0 if qqew_decoupled else self._qqew_vwap()

        v3m_price = 0.0
        if qqq_now is not None and qqq_ago is not None:
            v3m_price = float(qqq_now - qqq_ago)

        qqew_vs_vwap = 0.0
        if qqew_now is not None and qqew_vwap > 0:
            qqew_vs_vwap = float((float(qqew_now) - float(qqew_vwap)) / float(qqew_vwap))

        vol_t = float(self._min_bar.volume) if self._min_bar is not None else 0.0
        vol_ma10 = float(sum(self._rolling_vol_queue) / len(self._rolling_vol_queue)) if self._rolling_vol_queue else 0.0
        mu_today = float(sum(self._today_vol_series) / len(self._today_vol_series)) if self._today_vol_series else 0.0
        cv_today = 0.0
        if mu_today > 0 and len(self._today_vol_series) > 1:
            cv_today = min(2.0, float(statistics.pstdev(self._today_vol_series)) / mu_today)
        vol_threshold = vol_ma10 * (1.0 + cv_today) if vol_ma10 > 0 else 0.0
        vol_surge_ratio = float(vol_t / vol_threshold) if vol_threshold > 0 else 0.0

        return {
            "v3m_price": v3m_price,
            "qqew_vs_vwap": qqew_vs_vwap,
            "vol_surge_ratio": vol_surge_ratio,
            "vol_t": vol_t,
            "vol_ma10": vol_ma10,
            "cv_today": cv_today,
            "vol_threshold": vol_threshold,
        }

    def _resolve_code_version(self) -> str:
        try:
            repo_root = Path(__file__).resolve().parents[1]
            sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(repo_root),
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                text=True,
            ).strip()
            return sha or "unknown"
        except Exception:
            return "unknown"

    def _append_diag_event(self, event: str, payload: Dict[str, Any]) -> None:
        if self._diag_path is None:
            return
        try:
            self._rotate_diag_if_needed()
            rec = {"ts_now_et": datetime.now(ET).isoformat(), "event": event}
            rec.update(payload)
            with self._diag_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("qqq_diag_write_failed", strategy_id=self.strategy_id, error=str(exc))

    def _rotate_diag_if_needed(self) -> None:
        if self._diag_path is None:
            return
        extras = self.params.extras or {}
        max_bytes = int(extras.get("diag_max_bytes", 20 * 1024 * 1024))
        backup_count = max(1, int(extras.get("diag_backup_count", 3)))
        if max_bytes <= 0 or (not self._diag_path.exists()):
            return
        if self._diag_path.stat().st_size < max_bytes:
            return

        oldest = self._diag_path.parent / f"{self._diag_path.name}.{backup_count}"
        if oldest.exists():
            oldest.unlink()
        for idx in range(backup_count - 1, 0, -1):
            src = self._diag_path.parent / f"{self._diag_path.name}.{idx}"
            if src.exists():
                dst = self._diag_path.parent / f"{self._diag_path.name}.{idx + 1}"
                src.replace(dst)
        self._diag_path.replace(self._diag_path.parent / f"{self._diag_path.name}.1")

    def _check_loop_stall(self, *, now: datetime, now_mono: float) -> None:
        if (not self._running) or (not self._loop_in_iteration) or self._loop_current_started_mono <= 0.0:
            return
        extras = self.params.extras or {}
        threshold_sec = max(1.0, float(extras.get("loop_stall_threshold_sec", 20.0)))
        warn_interval_sec = max(5.0, float(extras.get("loop_stall_warn_interval_sec", 30.0)))
        age_sec = max(0.0, float(now_mono - self._loop_current_started_mono))
        if age_sec < threshold_sec:
            return
        if (now_mono - self._loop_last_stall_warn_mono) < warn_interval_sec:
            return
        self._loop_last_stall_warn_mono = now_mono
        payload: Dict[str, Any] = {
            "strategy_id": self.strategy_id,
            "iteration_seq": int(self._loop_iteration_seq),
            "loop_stage": str(self._loop_last_stage or "unknown"),
            "age_sec": round(float(age_sec), 3),
            "threshold_sec": round(float(threshold_sec), 3),
            "pending_decision_id": self._pending_decision_id,
            "active_trigger_id": self._normalize_trigger_id(self._active_trigger_id),
        }
        self._append_diag_event("qqq_loop_stall_detected", payload)
        self._emit_local_audit(
            stage="strategy_loop_stall",
            severity="warning",
            status="stalled",
            reason="loop_stall_detected",
            trace_id=(self._pending_decision_id or self._active_trigger_id),
            payload=payload,
        )
        logger.warning(
            "qqq_loop_stall_detected",
            strategy_id=self.strategy_id,
            iteration_seq=self._loop_iteration_seq,
            loop_stage=self._loop_last_stage,
            age_sec=round(float(age_sec), 3),
            threshold_sec=round(float(threshold_sec), 3),
        )

    async def _loop_watchdog(self) -> None:
        extras = self.params.extras or {}
        interval_sec = max(0.2, float(extras.get("loop_watchdog_interval_sec", 1.0)))
        while self._running:
            try:
                now = datetime.now(ET)
                now_mono = asyncio.get_running_loop().time()
                self._check_loop_stall(now=now, now_mono=now_mono)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("qqq_loop_watchdog_error", strategy_id=self.strategy_id, error=str(exc))
            await asyncio.sleep(interval_sec)

    async def _collect_stock_execution_blocker_snapshot(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "strategy_id": self.strategy_id,
            "active_same_strategy_open_orders": None,
            "tqqq_qty": None,
            "sqqq_qty": None,
            "would_block": False,
            "errors": [],
        }
        try:
            open_orders = await self._ctx_open_orders_with_timeout()
            payload["active_same_strategy_open_orders"] = int(
                sum(
                    1
                    for order in open_orders
                    if getattr(order, "strategy_id", None) == self.strategy_id
                    and (not self._is_terminal_order_state(order))
                )
            )
        except Exception as exc:
            payload["errors"].append(f"open_orders:{exc}")
        try:
            qty_t = abs(await self._current_exec_qty_for_symbol("TQQQ"))
            qty_s = abs(await self._current_exec_qty_for_symbol("SQQQ"))
            payload["tqqq_qty"] = float(qty_t)
            payload["sqqq_qty"] = float(qty_s)
        except Exception as exc:
            payload["errors"].append(f"position_qty:{exc}")
        active_open = int(payload["active_same_strategy_open_orders"] or 0)
        tqqq_qty = float(payload["tqqq_qty"] or 0.0)
        sqqq_qty = float(payload["sqqq_qty"] or 0.0)
        payload["would_block"] = bool(active_open > 0 or (((tqqq_qty > 0.0) or (sqqq_qty > 0.0)) and (not self._is_traded)))
        payload["legacy_trader_parity_mode"] = bool(self._legacy_trader_parity_mode(extras))
        return payload

    def _maybe_emit_heartbeat(self, now: datetime) -> None:
        try:
            now_mono = asyncio.get_running_loop().time()
            self._check_loop_stall(now=now, now_mono=now_mono)
        except RuntimeError:
            now_mono = 0.0
        self._refresh_stream_health_flag(now)
        minute_ts = _floor_minute(now)
        if self._last_heartbeat_minute == minute_ts:
            return
        self._last_heartbeat_minute = minute_ts
        minute_metrics = self._current_entry_metrics(now)
        age_qqq = self._symbol_age_sec("QQQ", now)
        payload: Dict[str, Any] = {
            "strategy_id": self.strategy_id,
            "code_version": self._code_version,
            "is_traded": self._is_traded,
            "amp_locked": self._amp_locked,
            "deadwater": self._deadwater,
            "toxic_day": self._toxic_day,
            "data_age_qqq": age_qqq,
            "data_age_qqq_sec": age_qqq,
            "v3m_price": minute_metrics["v3m_price"],
            "qqew_vs_vwap": minute_metrics["qqew_vs_vwap"],
            "vol_surge_ratio": minute_metrics["vol_surge_ratio"],
            "loop_iteration_seq": int(self._loop_iteration_seq),
            "loop_last_stage": str(self._loop_last_stage or "unknown"),
            "loop_last_duration_ms": round(float(self._loop_last_duration_ms), 3),
            "loop_current_age_sec": round(
                float(max(0.0, now_mono - self._loop_current_started_mono)) if now_mono > 0.0 and self._loop_current_started_mono > 0.0 else 0.0,
                3,
            ),
            "startup_positions_ready": bool(self._startup_positions_ready),
            "broker_positions_ready": bool(self._broker_positions_ready),
            "stream_invalid": bool(self._stream_invalid),
            "seed_ready": bool(self._seed_ready),
            "seed_n": int(len(self._seed_velocity_samples)),
        }
        if not self._qqew_decoupled():
            age_qqew = self._symbol_age_sec(QQEW_SYMBOL, now)
            payload.update(
                {
                    "last_price_qqew": self._last_price(QQEW_SYMBOL),
                    "data_age_qqew": age_qqew,
                    "data_age_qqew_sec": age_qqew,
                    "qqew_vs_vwap": minute_metrics["qqew_vs_vwap"],
                }
            )
        self._append_diag_event("qqq_loop_heartbeat", payload)
        session_open = bool(self._all_day_test_mode() or self._is_regular_session(now))
        payload["strategy_session_open"] = bool(session_open)
        blockers: list[str] = []
        if session_open:
            if bool(self._system_paused):
                blockers.append("system_paused")
            if bool(self._stream_invalid):
                blockers.append("stream_invalid")
            if not bool(self._seed_ready):
                blockers.append("seed_not_ready")
            if not bool(self._startup_positions_ready):
                blockers.append("startup_positions_not_ready")
        write_strategy_tradability_heartbeat(
            strategy_id=str(self.strategy_id),
            tradable=len(blockers) == 0,
            blockers=blockers,
            source="qqq_loop_heartbeat",
            details=payload,
        )
        self._emit_local_audit(
            stage="strategy_loop_heartbeat",
            severity="info",
            status="alive",
            reason=str(self._loop_last_stage or "loop_heartbeat"),
            symbol="QQQ",
            trace_id=str(self._pending_decision_id or self._active_trigger_id or f"{self.strategy_id}:heartbeat"),
            payload={
                **payload,
                "system_paused": bool(self._system_paused),
            },
        )

    def _compute_stream_invalid(self, now: datetime) -> bool:
        extras = self.params.extras or {}
        max_data_age_sec = float(extras.get("entry_max_data_age_sec", 3.0))
        max_data_age_qqq_sec = float(extras.get("entry_max_data_age_sec_qqq", max_data_age_sec))
        max_data_age_qqew_sec = float(extras.get("entry_max_data_age_sec_qqew", max_data_age_sec))
        elite_decouple_qqew = self._qqew_decoupled()

        qqq_now = self._last_price("QQQ")
        age_qqq = self._symbol_age_sec("QQQ", now)
        if qqq_now is None or age_qqq is None or age_qqq > max_data_age_qqq_sec:
            return True

        if not elite_decouple_qqew:
            qqew_now = self._last_price(QQEW_SYMBOL)
            age_qqew = self._symbol_age_sec(QQEW_SYMBOL, now)
            if qqew_now is None or age_qqew is None or age_qqew > max_data_age_qqew_sec:
                return True

        require_continuity = bool(extras.get("entry_require_continuity_180s", False))
        if require_continuity and (not self._has_continuous_180s(now, include_qqew=(not elite_decouple_qqew))):
            return True

        return False

    def _refresh_stream_health_flag(self, now: datetime) -> None:
        self._stream_invalid = self._compute_stream_invalid(now)

    def _build_observability_snapshot(self, now: datetime) -> Dict[str, Any]:
        self._refresh_stream_health_flag(now)
        atr_p = int((self.params.extras or {}).get("atr_period", 14))
        t_eff, t_src, t_live, t_pre = self._effective_atr_observed("TQQQ", atr_p, now)
        s_eff, s_src, s_live, s_pre = self._effective_atr_observed("SQQQ", atr_p, now)
        q_close_1m, q_close_1m_ts = self._latest_minute_close()
        missing = self._missing_required_observed(now)
        qqew_decoupled = self._qqew_decoupled()

        required_values: Dict[str, Any] = {
            "QQQ": {
                "close_1m": q_close_1m,
                "close_1m_ts_et": q_close_1m_ts.isoformat() if q_close_1m_ts else None,
                "vwap": self._vwap(),
                "last": self._last_price("QQQ"),
                "age_sec": self._symbol_age_sec("QQQ", now),
                "price_base": self._price_base,
                "price_base_source": self._price_base_source,
            },
            "TQQQ": {
                "last": self._last_price("TQQQ"),
                "age_sec": self._symbol_age_sec("TQQQ", now),
                f"atr5m{atr_p}_live": t_live,
                f"atr5m{atr_p}_pre": t_pre,
                f"atr5m{atr_p}_effective": t_eff,
                "atr_source": t_src,
                "bars_5m_count": len(self._bars_5m.get("TQQQ", [])) + (1 if self._cur_5m.get("TQQQ") else 0),
            },
            "SQQQ": {
                "last": self._last_price("SQQQ"),
                "age_sec": self._symbol_age_sec("SQQQ", now),
                f"atr5m{atr_p}_live": s_live,
                f"atr5m{atr_p}_pre": s_pre,
                f"atr5m{atr_p}_effective": s_eff,
                "atr_source": s_src,
                "bars_5m_count": len(self._bars_5m.get("SQQQ", [])) + (1 if self._cur_5m.get("SQQQ") else 0),
            },
        }
        if not qqew_decoupled:
            required_values[QQEW_SYMBOL] = {
                "vwap": self._qqew_vwap(),
                "last": self._last_price(QQEW_SYMBOL),
                "age_sec": self._symbol_age_sec(QQEW_SYMBOL, now),
            }

        return {
            "ts_now_et": now.isoformat(),
            "strategy_id": self.strategy_id,
            "ready": len(missing) == 0,
            "missing_required": missing,
            "qqew_decoupled": qqew_decoupled,
            "mode": {
                "degraded": bool(self.state.get("degraded", False)),
                "degraded_reason": self.state.get("degraded_reason"),
                "degraded_details": self.state.get("degraded_details"),
            },
            "required_values": required_values,
            "strategy_state": {
                "is_traded": self._is_traded,
                "side": self._side,
                "exec_symbol": self._exec_symbol,
                "entry_px": self._entry_px,
                "entry_time_et": self._entry_time_et.isoformat() if self._entry_time_et else None,
                "stop_px": self._stop_px,
                "stop_dist_pre": self._stop_dist_pre,
                "stop_dist_cur": self._stop_dist_cur,
                "stop_dist_cap": self._stop_dist_max,
                "break_even_armed": self._break_even_armed,
                "amp_locked": self._amp_locked,
                "seed_ready": self._seed_ready,
                "seed_n": int(len(self._seed_velocity_samples)),
                "orh": self._orh,
                "orl": self._orl,
                "v_seed_sigma": self._v_seed_sigma,
                "seed_locked_day": self._seed_locked_day,
                "trade_target_px": self._trade_target_px,
                "price_base": self._price_base,
                "price_base_source": self._price_base_source,
                "toxic_day": self._toxic_day,
                "deadwater": self._deadwater,
                "cross_count": self._cross_count,
                "locked_add_threshold": self._locked_add_threshold,
                "locked_dev_threshold": self._locked_dev_threshold,
                "locked_target": self._locked_target,
                "calibration_mode": self._calibration_mode,
                "t_chop": self._t_chop,
                "t_add": self._t_add,
                "t_v": self._t_v,
                "v_limit": self._v_limit,
            },
            "internal_counters": {
                "min_closes_count": len(self._min_closes),
                "qqq_buffer_count": len(self._qqq_buffer),
                "qqew_buffer_count": (0 if qqew_decoupled else len(self._qqew_buffer)),
                "vwap_volume": self._vwap_v,
            },
        }

    def _maybe_write_observability_file(self, now: datetime) -> None:
        extras = self.params.extras or {}
        if not bool(extras.get("enable_observability_file", True)):
            return
        if self._obs_path is None:
            return
        minute_ts = _floor_minute(now)
        if self._last_obs_write_minute == minute_ts:
            return
        self._last_obs_write_minute = minute_ts
        try:
            payload = self._build_observability_snapshot(now)
            tmp = self._obs_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._obs_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("qqq_observability_write_failed", strategy_id=self.strategy_id, error=str(exc))

    def _atr_5m(self, symbol: str, period: int) -> Optional[float]:
        bars = list(self._bars_5m.get(symbol, []))
        cur = self._cur_5m.get(symbol)
        if cur is not None:
            bars = bars + [cur]
        if period <= 0:
            return None
        if len(bars) < period + 1:
            return None
        def tr(curr: _OhlcvBar, prev_close: float) -> float:
            return max(
                curr.high - curr.low,
                abs(curr.high - prev_close),
                abs(curr.low - prev_close),
            )

        prev_close = bars[0].close
        trs = []
        for b in bars[1:]:
            trs.append(tr(b, prev_close))
            prev_close = b.close

        if len(trs) < period:
            return None

        atr = sum(trs[:period]) / float(period)
        for x in trs[period:]:
            atr = (atr * (period - 1) + x) / float(period)
        return float(atr)

    def _refresh_atr_pre(self, symbol: str, period: int) -> None:
        atr = self._atr_5m(symbol, period)
        if atr is not None and atr > 0:
            self._atr_pre[symbol] = float(atr)

    def _refresh_atr_pre_by_schedule(self, now: datetime) -> None:
        # Retry preheat every minute until both ATR_pre are available.
        minute_ts = _floor_minute(now)
        if self._last_atr_pre_retry_minute == minute_ts:
            return
        self._last_atr_pre_retry_minute = minute_ts
        period = int((self.params.extras or {}).get("atr_period", 14))
        for sym in ("TQQQ", "SQQQ"):
            if self._atr_pre.get(sym) is None:
                self._refresh_atr_pre(sym, period)

    def _parse_atr_seed_record(self, data: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(data, dict):
            return None

        day = str(data.get("day") or "")
        atr_pre = data.get("atr_pre")
        if isinstance(atr_pre, dict):
            t = _safe_float(atr_pre.get("TQQQ"))
            s = _safe_float(atr_pre.get("SQQQ"))
            if day and t is not None and s is not None and t > 0 and s > 0:
                return {"day": day, "TQQQ": float(t), "SQQQ": float(s)}

        t = _safe_float(data.get("TQQQ"))
        s = _safe_float(data.get("SQQQ"))
        if day and t is not None and s is not None and t > 0 and s > 0:
            return {"day": day, "TQQQ": float(t), "SQQQ": float(s)}
        return None

    def _read_atr_seed_candidates(self) -> list[Dict[str, Any]]:
        if self._atr_seed_path is None or (not self._atr_seed_path.exists()):
            return []
        try:
            raw = json.loads(self._atr_seed_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("qqq_atr_seed_read_failed", strategy_id=self.strategy_id, error=str(exc))
            return []

        candidates: list[Dict[str, Any]] = []
        if isinstance(raw, dict):
            days = raw.get("days")
            if isinstance(days, dict):
                for k, v in days.items():
                    rec_data = {"day": str(k)}
                    if isinstance(v, dict):
                        rec_data.update(v)
                    rec = self._parse_atr_seed_record(rec_data)
                    if rec:
                        candidates.append(rec)
            latest = raw.get("latest")
            if isinstance(latest, dict):
                rec = self._parse_atr_seed_record(latest)
                if rec:
                    candidates.append(rec)
            top = self._parse_atr_seed_record(raw)
            if top:
                candidates.append(top)
        return candidates

    def _restore_atr_seed_into_pre(self) -> None:
        if self._atr_pre.get("TQQQ") and self._atr_pre.get("SQQQ"):
            return
        candidates = self._read_atr_seed_candidates()
        if not candidates:
            return
        candidates.sort(key=lambda x: str(x.get("day") or ""), reverse=True)
        chosen = candidates[0]

        loaded = []
        for sym in ("TQQQ", "SQQQ"):
            if self._atr_pre.get(sym) is None:
                v = _safe_float(chosen.get(sym))
                if v is not None and v > 0:
                    self._atr_pre[sym] = float(v)
                    loaded.append(sym)
        if loaded:
            logger.info(
                "qqq_atr_seed_loaded",
                strategy_id=self.strategy_id,
                day=chosen.get("day"),
                loaded=loaded,
                tqqq_atr=self._atr_pre.get("TQQQ"),
                sqqq_atr=self._atr_pre.get("SQQQ"),
            )

    def _build_atr_seed_snapshot(self, period: int) -> tuple[Optional[Dict[str, float]], str]:
        snapshot: Dict[str, float] = {}
        source = "live_5m"
        for sym in ("TQQQ", "SQQQ"):
            v = self._atr_5m(sym, period)
            if v is None or v <= 0:
                source = "pre"
                v = _safe_float(self._atr_pre.get(sym))
            if v is None or v <= 0:
                return None, "missing"
            snapshot[sym] = float(v)
        return snapshot, source

    def _has_atr_seed_for_day(self, day: str) -> bool:
        if self._atr_seed_path is None or (not self._atr_seed_path.exists()):
            return False
        try:
            raw = json.loads(self._atr_seed_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict):
            return False
        days = raw.get("days")
        if isinstance(days, dict) and isinstance(days.get(day), dict):
            t = _safe_float(days[day].get("TQQQ"))
            s = _safe_float(days[day].get("SQQQ"))
            return t is not None and t > 0 and s is not None and s > 0
        latest = raw.get("latest")
        if isinstance(latest, dict):
            latest_day = str(latest.get("day") or "")
            atr_pre = latest.get("atr_pre")
            if latest_day == day and isinstance(atr_pre, dict):
                t = _safe_float(atr_pre.get("TQQQ"))
                s = _safe_float(atr_pre.get("SQQQ"))
                return t is not None and t > 0 and s is not None and s > 0
        return False

    def _maybe_persist_daily_atr_seed(self, now: datetime) -> None:
        if now.weekday() > 4:
            return
        # Only persist EOD snapshot in RTH close window.
        if now.time() < time(15, 59) or now.time() > time(16, 0):
            return
        day = now.strftime("%Y-%m-%d")
        if self._atr_seed_saved_day == day:
            return

        period = int((self.params.extras or {}).get("atr_period", 14))
        snapshot, _ = self._build_atr_seed_snapshot(period)
        if snapshot is None:
            return
        self._persist_atr_seed(day=day, atr_pre=snapshot, reason="eod_snapshot")
        self._atr_seed_saved_day = day

    def _persist_atr_seed(self, day: str, atr_pre: Dict[str, float], *, reason: str) -> None:
        if self._atr_seed_path is None:
            return
        t = _safe_float(atr_pre.get("TQQQ"))
        s = _safe_float(atr_pre.get("SQQQ"))
        if t is None or s is None or t <= 0 or s <= 0:
            return
        try:
            existing: Dict[str, Any] = {}
            if self._atr_seed_path.exists():
                try:
                    raw = json.loads(self._atr_seed_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        existing = raw
                except Exception:
                    existing = {}
            days = existing.get("days")
            if not isinstance(days, dict):
                days = {}
            days[str(day)] = {"TQQQ": float(t), "SQQQ": float(s)}
            keep_days = max(3, int((self.params.extras or {}).get("atr_seed_keep_days", 30)))
            keep_keys = sorted(days.keys())[-keep_days:]
            pruned_days = {k: days[k] for k in keep_keys}
            payload = {
                "version": 1,
                "updated_at": datetime.now(ET).isoformat(),
                "latest": {"day": str(day), "atr_pre": {"TQQQ": float(t), "SQQQ": float(s)}},
                "days": pruned_days,
                "reason": reason,
            }
            tmp = self._atr_seed_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._atr_seed_path)
            logger.info(
                "qqq_atr_seed_persisted",
                strategy_id=self.strategy_id,
                day=day,
                tqqq_atr=round(float(t), 6),
                sqqq_atr=round(float(s), 6),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("qqq_atr_seed_persist_failed", strategy_id=self.strategy_id, error=str(exc))

    def _effective_atr(self, symbol: str, period: int, now: Optional[datetime] = None) -> Optional[float]:
        """
        Calculates ATR with 3 priorities:
        1. Live 5m bars from SFS (Backfilled or current session)
        2. Pre-market ATR_pre from saved seed
        3. Emergency Fallback (0.35 for TQQQ, 0.30 for SQQQ)
        """
        # Priority 1: Real 5m bar data (needs at least 14 bars)
        atr_live = self._atr_5m(symbol, period)
        if atr_live is not None and atr_live > 0:
            return float(atr_live)

        # Priority 2: Pre-market ATR_pre (from 09:20 warmup or previous day close)
        atr_pre = self._atr_pre.get(symbol)
        if atr_pre is not None and atr_pre > 0:
            return float(atr_pre)

        # Priority 3: Emergency Fallback (only at/after 09:45)
        ref_now = now or datetime.now(ET)
        if ref_now.time() >= time(9, 45) or self._all_day_test_mode():
            fallback = 0.35 if symbol == "TQQQ" else 0.30
            if not self._atr_emergency_active.get(symbol, False):
                self._atr_emergency_active[symbol] = True
                logger.warning(
                    "EMERGENCY_ATR_ACTIVE",
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    fallback_atr=fallback,
                    reason="Priority 3 fallback triggered at entry window"
                )
            return fallback
        return None

    @staticmethod
    def _series_value_at_or_before(series: Deque[tuple[int, float]], target_ts_ms: int) -> Optional[float]:
        if not series:
            return None
        # Walk from newest backwards; deque is small (<= 20k).
        for ts_ms, v in reversed(series):
            if ts_ms <= target_ts_ms:
                return float(v)
        return None

    def _qqq_value_at_or_before(self, target_ts_ms: int) -> Optional[float]:
        return self._series_value_at_or_before(self._qqq_buffer, target_ts_ms)

    def _session_pos_day(self, now: datetime, qqq_now: float) -> Optional[float]:
        """Return current QQQ position in today's session range [0,1]."""
        session_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now <= session_start:
            return None
        prices: list[float] = []
        for bar in self._minute_bars:
            bt = bar.ts_end
            if bt.date() != now.date():
                continue
            if bt < session_start or bt > now:
                continue
            prices.append(float(bar.close))
        if not prices:
            return None
        prices.append(float(qqq_now))
        p_min = min(prices)
        p_max = max(prices)
        if p_max - p_min <= 1e-9:
            return 0.5
        return float((float(qqq_now) - p_min) / max(p_max - p_min, 1e-9))

    def _window_pos_rank(self, now: datetime, price_now: float, window_min: int = 15) -> Optional[float]:
        window_start = now - timedelta(minutes=max(1, int(window_min)))
        prices: list[float] = []
        for bar in self._minute_bars:
            bt = bar.ts_end
            if bt.date() != now.date():
                continue
            if bt < window_start or bt > now:
                continue
            prices.append(float(bar.close))
        if not prices:
            return None
        prices.append(float(price_now))
        p_min = min(prices)
        p_max = max(prices)
        if p_max - p_min <= 1e-9:
            return 0.5
        return float((float(price_now) - p_min) / max(p_max - p_min, 1e-9))

    def _anti_chase_reason(
            self,
            *,
            now: datetime,
            direction: str,
            mode: str,
            qqq_now: float,
            vwap_now: float,
            distance_from_vwap_atr: float,
            extras: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self._anti_chase_enabled(extras):
            return None
        start_t = self._parse_gate_time(extras.get("anti_chase_window_start", "09:45"), time(9, 45))
        end_t = self._parse_gate_time(extras.get("anti_chase_window_end", "14:00"), time(14, 0))
        if not (start_t <= now.time() <= end_t):
            return None
        apply_to = str(extras.get("anti_chase_apply_to", "all") or "all").strip().lower()
        mode_key = str(mode or "").strip().lower()
        mode_match = (
            apply_to == "all"
            or (apply_to == "fast" and mode_key in {"fast", "fast_drift"})
            or (apply_to == "drift" and mode_key in {"drift", "fast_drift", "low_atr_release"})
            or mode_key == apply_to
        )
        if not mode_match:
            return None
        rank15 = self._window_pos_rank(now, float(qqq_now), window_min=15)
        if rank15 is None:
            return None
        rank15_min = float(extras.get("anti_chase_rank15_min", 0.93))
        dist_vwap_min = float(extras.get("anti_chase_dist_vwap_atr_d_min", 0.12))
        if float(distance_from_vwap_atr) < dist_vwap_min:
            return None
        if direction == "LONG" and rank15 >= rank15_min and float(qqq_now) > float(vwap_now):
            return {
                "reason": "anti_chase_blocked",
                "rank15": float(rank15),
                "distance_from_vwap_atr": float(distance_from_vwap_atr),
                "tv": float(rank15_min),
                "dist_tv": float(dist_vwap_min),
            }
        return None

    def _r1_fourth_short_small_green_block_reason(
            self,
            *,
            direction: str,
            extras: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self._r1_fourth_short_small_green_enabled(extras):
            return None
        projected_trade_seq = int(self._success_count) + 1
        day_realized_pnl = float(self._day_realized_pnl)
        if projected_trade_seq == 4 and direction == "SHORT" and 0.0 <= day_realized_pnl < 100.0:
            return {
                "reason": "r1_fourth_short_small_green_blocked",
                "projected_trade_seq": int(projected_trade_seq),
                "day_realized_pnl_before": float(day_realized_pnl),
            }
        return None

    def _v47_chase_guard_reason(
            self,
            *,
            now: datetime,
            direction: str,
            qqq_now: float,
            vwap_now: float,
            extras: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        def _parse_hms_local(raw: Any, default: time) -> time:
            if raw is None:
                return default
            try:
                return datetime.strptime(str(raw), "%H:%M:%S").time()
            except Exception:
                return default

        if not bool(extras.get("enable_v47_chase_guard", False)):
            return None
        start_t = _parse_hms_local(extras.get("v47_chase_guard_start", "09:45:00"), time(9, 45))
        end_t = _parse_hms_local(extras.get("v47_chase_guard_end", "11:50:00"), time(11, 50))
        if not (start_t <= now.time() <= end_t):
            return None
        pos_day = self._session_pos_day(now, float(qqq_now))
        if pos_day is None or vwap_now <= 0:
            return None
        dev_vwap_pct = (float(qqq_now) - float(vwap_now)) / max(float(vwap_now), 1e-9)
        long_pos_day_min = float(extras.get("v47_long_pos_day_min", 0.80))
        short_pos_day_max = float(extras.get("v47_short_pos_day_max", 0.20))
        long_dev_vwap_min = float(extras.get("v47_long_dev_vwap_min", 0.0020))
        short_dev_vwap_min = float(extras.get("v47_short_dev_vwap_min", 0.0020))
        if direction == "LONG" and pos_day >= long_pos_day_min and dev_vwap_pct >= long_dev_vwap_min:
            return {
                "reason": "v47_long_chase_guard",
                "pos_day": float(pos_day),
                "dev_vwap_pct": float(dev_vwap_pct),
                "tv": float(long_dev_vwap_min),
            }
        if direction == "SHORT" and pos_day <= short_pos_day_max and dev_vwap_pct <= -short_dev_vwap_min:
            return {
                "reason": "v47_short_chase_guard",
                "pos_day": float(pos_day),
                "dev_vwap_pct": float(dev_vwap_pct),
                "tv": float(short_dev_vwap_min),
            }
        return None

    def _qqew_value_at_or_before(self, target_ts_ms: int) -> Optional[float]:
        return self._series_value_at_or_before(self._qqew_buffer, target_ts_ms)

    def _v_seed_sigma_from_samples(self) -> Optional[float]:
        if len(self._seed_velocity_samples) < 5:
            return None
        med = statistics.median(self._seed_velocity_samples)
        mad = statistics.median([abs(x - med) for x in self._seed_velocity_samples])
        sigma = 1.4826 * float(mad)
        return float(max(0.0, sigma))

    def _maybe_lock_seed(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if (not self._all_day_test_mode()) and (
            now.weekday() > 4 or now.time() < time(9, 45) or now.time() > time(16, 0)
        ):
            return

        if self._seed_locked_day == day and self._seed_ready:
            return

        self._v_seed_sigma = self._v_seed_sigma_from_samples()
        self._seed_ready = bool(
            self._v_seed_sigma is not None
            and self._v_seed_sigma > 0
            and self._orh is not None
            and self._orl is not None
            and len(self._seed_velocity_samples) >= 5
        )
        if self._seed_ready:
            self._seed_locked_day = day
            logger.info(
                "qqq_seed_locked",
                strategy_id=self.strategy_id,
                seed_ready=self._seed_ready,
                orh=self._orh,
                orl=self._orl,
                v_seed_sigma=self._v_seed_sigma,
                n_samples=len(self._seed_velocity_samples),
            )
            return

        # Do not lock an unready seed; keep retrying while market is open.
        self._seed_locked_day = None
        minute_key = _floor_minute(now)
        if self._last_seed_retry_minute != minute_key:
            self._last_seed_retry_minute = minute_key
            logger.warning(
                "qqq_seed_lock_deferred",
                strategy_id=self.strategy_id,
                reason="seed_insufficient",
                n_samples=len(self._seed_velocity_samples),
                orh=self._orh,
                orl=self._orl,
                v_seed_sigma=self._v_seed_sigma,
            )

    @staticmethod
    def _max_gap_ms(buf: Deque[tuple[int, float]]) -> Optional[int]:
        if len(buf) < 2:
            return None
        prev = None
        max_gap = 0
        for ts_ms, _ in buf:
            if prev is not None:
                max_gap = max(max_gap, ts_ms - prev)
            prev = ts_ms
        return max_gap

    def _has_continuous_180s(self, now: datetime, *, include_qqew: bool = True) -> bool:
        now_ms = int(now.timestamp() * 1000)
        min_ts = now_ms - 180_000
        qqq_seg = [(ts, v) for ts, v in self._qqq_buffer if ts >= min_ts]
        qqq_limit_ms = self._freshness_limit_ms("QQQ")
        min_qqq_points = max(20, int(180_000 / max(1, qqq_limit_ms)))
        if len(qqq_seg) < min_qqq_points:
            return False
        qqq_gap = self._max_gap_ms(deque(qqq_seg))
        if qqq_gap is None:
            return False
        if qqq_gap > qqq_limit_ms:
            return False
        if not include_qqew:
            return True
        qqew_seg = [(ts, v) for ts, v in self._qqew_buffer if ts >= min_ts]
        qqew_limit_ms = self._freshness_limit_ms(QQEW_SYMBOL)
        min_qqew_points = max(20, int(180_000 / max(1, qqew_limit_ms)))
        if len(qqew_seg) < min_qqew_points:
            return False
        qqew_gap = self._max_gap_ms(deque(qqew_seg))
        if qqew_gap is None:
            return False
        return qqew_gap <= qqew_limit_ms

    def _atr_1m_qqq(self, period: int = 14) -> Optional[float]:
        if period <= 0:
            return None
        bars = list(self._minute_bars)
        if self._min_bar is not None:
            bars.append(self._min_bar)
        if len(bars) < period + 1:
            return None
        prev_close = bars[0].close
        trs: list[float] = []
        for b in bars[1:]:
            tr = max(
                b.high - b.low,
                abs(b.high - prev_close),
                abs(b.low - prev_close),
            )
            trs.append(float(tr))
            prev_close = b.close
        if len(trs) < period:
            return None
        atr = sum(trs[:period]) / float(period)
        for x in trs[period:]:
            atr = (atr * (period - 1) + x) / float(period)
        return float(atr)

    def _load_daily_atr_rows(self) -> list[dict]:
        if self._qqq_daily_atr_path is None or (not self._qqq_daily_atr_path.exists()):
            return []
        try:
            raw = json.loads(self._qqq_daily_atr_path.read_text(encoding="utf-8"))
            rows = raw.get("days", [])
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
            return []
        except Exception:
            return []

    def _save_daily_atr_rows(self, rows: list[dict]) -> None:
        if self._qqq_daily_atr_path is None:
            return
        payload = {"days": rows, "updated_at": datetime.now(ET).isoformat()}
        tmp = self._qqq_daily_atr_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self._qqq_daily_atr_path)

    def _refresh_daily_atr_context(self, now: datetime) -> None:
        rows = self._load_daily_atr_rows()
        today = now.strftime("%Y-%m-%d")
        history = [r for r in rows if str(r.get("day", "")) < today and _safe_float(r.get("atr14")) and _safe_float(r.get("atr14")) > 0]
        history.sort(key=lambda x: str(x.get("day", "")))
        if not history:
            self._atr_d_prev_day = None
            self._atr_rank_prev_day = None
            return
        atr_prev = float(history[-1]["atr14"])
        win = [float(r["atr14"]) for r in history[-30:]]
        leq = sum(1 for v in win if v <= atr_prev)
        rank = float(leq / max(1, len(win)))
        self._atr_d_prev_day = float(atr_prev)
        self._atr_rank_prev_day = float(rank)

    def _persist_daily_atr_context(self, now: datetime) -> None:
        if now.time() < time(15, 59):
            return
        bars = list(self._minute_bars)
        if self._min_bar is not None:
            bars.append(self._min_bar)
        if not bars:
            return
        day = now.strftime("%Y-%m-%d")
        open_px = float(bars[0].open)
        high_px = max(float(b.high) for b in bars)
        low_px = min(float(b.low) for b in bars)
        close_px = float(bars[-1].close)

        rows = self._load_daily_atr_rows()
        rows = [r for r in rows if str(r.get("day", "")) != day]
        rows.sort(key=lambda x: str(x.get("day", "")))
        prev_close = _safe_float(rows[-1].get("close")) if rows else None
        tr = max(
            high_px - low_px,
            abs(high_px - prev_close) if prev_close is not None else 0.0,
            abs(low_px - prev_close) if prev_close is not None else 0.0,
        )
        tr = float(max(tr, high_px - low_px))
        tr_series = [float(_safe_float(r.get("tr")) or 0.0) for r in rows if _safe_float(r.get("tr")) is not None]
        atr_series = [float(_safe_float(r.get("atr14")) or 0.0) for r in rows if _safe_float(r.get("atr14")) is not None]
        atr14 = None
        if len(tr_series) >= 13:
            if atr_series and atr_series[-1] > 0:
                atr14 = (atr_series[-1] * 13.0 + tr) / 14.0
            else:
                seed = tr_series[-13:] + [tr]
                atr14 = sum(seed) / 14.0
        row = {
            "day": day,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "tr": tr,
            "atr14": float(atr14) if atr14 is not None else None,
        }
        rows.append(row)
        rows = rows[-90:]
        self._save_daily_atr_rows(rows)

    # ----------------- Daily state -----------------

    def _daily_reset_if_needed(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self._day == day:
            return
        self._day = day
        self._is_traded = False
        self._success_count = 0
        self._day_realized_pnl = 0.0
        self._day_closed_trade_pnls = []
        self._realized_pnl_snapshot_total = None
        self._pending_decision_id = None
        self._pending_decision_set_mono = 0.0
        self._pending_decision_set_at_et = None
        self._pending_decision_flat_probe_count = 0
        self._pending_decision_last_probe_mono = 0.0
        self._active_trigger_id = None
        self._round_live_confirmed = False
        self._side = None
        self._exec_symbol = None
        self._entry_px = None
        self._entry_time_et = None
        self._stop_px = None
        self._stop_dist_pre = None
        self._stop_dist_max = None
        self._stop_dist_cur = None
        self._break_even_armed = False
        self._last_stop_update_5m_end = None
        self._entry_atr_used = None
        self._tp1_hit = False
        self._trail_high_px = None
        self._trail_floor_px = None
        self._price_base = None
        self._price_base_source = None
        self._price_0945 = None
        self._amp_locked = False
        self._toxic_day = False
        self._deadwater = False
        self._cross_state = "MIDDLE"
        self._cross_count = 0
        self._vwap_pv = 0.0
        self._vwap_v = 0.0
        self._vwap_last_ts_ms = 0
        self._qqew_vwap_pv = 0.0
        self._qqew_vwap_v = 0.0
        self._qqew_vwap_last_ts_ms = 0
        self._vwap_minute.clear()
        self._vwap_minute_keys.clear()
        self._qqew_minute.clear()
        self._qqew_minute_keys.clear()
        self._qqew_last_price = None
        self._price_base_pv = 0.0
        self._price_base_v = 0.0
        self._min_closes.clear()
        self._min_close_keys.clear()
        self._exec_min_closes = {"TQQQ": {}, "SQQQ": {}}
        self._exec_min_close_keys = {"TQQQ": deque(), "SQQQ": deque()}
        self._minute_bars.clear()
        self._rolling_vol_queue.clear()
        self._today_vol_series.clear()
        self._min_bar = None
        self._qqq_buffer.clear()
        self._qqew_buffer.clear()
        self._stream_invalid = True
        self._seed_ready = False
        self._seed_locked_day = None
        self._orh = None
        self._orl = None
        self._seed_velocity_samples = []
        self._v_seed_sigma = None
        self._last_seed_retry_minute = None
        self._last_seed_warmup_retry_minute = None
        self._elite_overext_mark_ts = None
        self._elite_overext_mark_direction = None
        self._prev_vol_surge_ratio = None
        self._fast_cooldown_until = None
        self._fast_cooldown_trigger_cross = 0
        self._ab_a_state = "WAIT_MARK"
        self._ab_a_armed_ts = None
        self._ab_volatile_veto_direction = None
        self._ab_volatile_anchor_px = None
        self._ab_volatile_extreme_px = None
        self._ab_volatile_probe_anchor_px = None
        self._ab_volatile_probe_extreme_px = None
        self._ab_volatile_probe_armed = False
        self._planned_total_qty = 0
        self._planned_probe_qty = 0
        self._planned_add_qty = 0
        self._add_done = False
        self._add_deadline_et = None
        self._last_add_check_minute = None
        self._last_breakout_ts = None
        self._pending_breakout = None
        self._locked_add_threshold = None
        self._locked_dev_threshold = None
        self._locked_target = None
        self._trade_target_px = None
        self._locked_velocity_sign = None
        self._last_minute_check = None
        self._last_logic_retreat_minute = None
        self._force_flatten_done_day = None
        self._ff_miss_count = 0
        self._missing_debug_streak = 0
        self._missing_debug_signature = ""
        self._last_open_orders_check_warn_ts = 0.0
        self._last_entry_timeout_block_warn_mono = 0.0
        self._rewind_drop_count_by_symbol.clear()
        self._rewind_last_warn_mono_by_symbol.clear()
        self._rewind_last_incoming_ts_ms_by_symbol.clear()
        self._last_unexpected_short_action_mono_by_symbol = {"TQQQ": 0.0, "SQQQ": 0.0}
        self._entry_timeout_block_until_mono = 0.0
        self._last_entry_eval_second = None
        self._last_entry_eval_minute = None
        self._last_tp1_eod_no_exit_minute = None
        self._last_stop_exit_at = None
        self._last_stop_exit_side = None
        self._expected_entry_qty = 0
        self._stop_order_ref_seq = 0
        self._last_stop_submit_sig = None
        self._last_stop_submit_mono = 0.0
        self._atr_pre = {"TQQQ": None, "SQQQ": None}
        self._last_atr_pre_retry_minute = None
        self._atr_emergency_active = {"TQQQ": False, "SQQQ": False}
        self._atr_d_prev_day = None
        self._atr_rank_prev_day = None
        self._stop_audit_started = False
        self._atr_seed_saved_day = None
        self._price_base_window_missed_day = None
        self._price_base_recovery_last_minute = None
        self._price_base_ibkr_last_attempt_ts = 0.0
        self._ibkr_qqq_contract = None
        self._no_new_entries_until_et = None
        self._restart_degrade_started_at_et = None
        self._restart_degrade_unlock_reason = None
        self._calibration_day = None
        self._calibration_mode = "FALLBACK"
        self._s_add_abs = []
        self._s_v_abs = []
        self._t_chop = 900.0
        self._t_add = 1500.0
        self._t_v = 120.0
        self._v_limit = 20.0
        self._restore_atr_seed_into_pre()
        self._refresh_daily_atr_context(now)
        logger.info("qqq_intraday_daily_reset", strategy_id=self.strategy_id, day=day)

    # ----------------- Toxic-day filter -----------------

    def _update_crosscount(self, now: datetime) -> None:
        if now.weekday() > 4:
            return
        if not (time(9, 30) <= now.time() <= time(16, 0)):
            return

        px = self._last_price("QQQ")
        vwap = self._vwap()
        if not px or vwap <= 0:
            return

        upper = vwap * 1.0008
        lower = vwap * 0.9992
        new_state = "MIDDLE"
        if px > upper:
            new_state = "ABOVE"
        elif px < lower:
            new_state = "BELOW"

        if (self._cross_state == "ABOVE" and new_state == "BELOW") or (self._cross_state == "BELOW" and new_state == "ABOVE"):
            self._cross_count += 1
            logger.info("qqq_cross", strategy_id=self.strategy_id, cross_count=self._cross_count, from_state=self._cross_state, to_state=new_state)

        if new_state != "MIDDLE":
            self._cross_state = new_state

    # ----------------- 09:45 lock params -----------------

    async def _lock_amp_params(self, now: datetime) -> None:
        if self._amp_locked:
            return
        if now.weekday() > 4 and (not self._all_day_test_mode()):
            return
        if self._all_day_test_mode():
            self._locked_add_threshold = float((self.params.extras or {}).get("all_day_add_threshold", 120))
            self._locked_dev_threshold = float((self.params.extras or {}).get("all_day_dev_threshold", 0.0010))
            self._locked_target = float((self.params.extras or {}).get("all_day_target", 2.0))
            self._amp_locked = True
            logger.info(
                "qqq_amp_locked",
                strategy_id=self.strategy_id,
                amp=None,
                add_threshold=self._locked_add_threshold,
                dev_threshold=self._locked_dev_threshold,
                target=self._locked_target,
                mode="all_day_test_mode",
            )
            self._persist_runtime_state(reason="amp_locked_all_day_test")
            return
        if now.time() < time(9, 45):
            return

        # Toxic-day lock-in happens at/after 09:45 based on the 09:30–09:45 cross count.
        if self._cross_count >= 3:
            self._toxic_day = True
            self._persist_runtime_state(reason="toxic_day_locked")

        if self._price_0945 is None:
            self._price_0945 = self._last_price("QQQ")
            if self._price_0945:
                logger.info("qqq_price_0945_locked", strategy_id=self.strategy_id, price_0945=round(self._price_0945, 4))
            return

        if self._price_base is None:
            day = now.strftime("%Y-%m-%d")
            init_time = self._initialized_at_et
            init_after_window = False
            if init_time is not None:
                init_after_window = init_time.time() > time(9, 30, 10)
            recovered = await self._recover_price_base(now, init_after_window=init_after_window)
            if (not recovered) and init_after_window and self._price_base_window_missed_day != day:
                self._price_base_window_missed_day = day
                window_end = now.replace(hour=9, minute=30, second=10, microsecond=0)
                self._append_diag_event(
                    "price_base_window_missed",
                    {
                        "strategy_id": self.strategy_id,
                        "day": day,
                        "init_ts_et": init_time.isoformat() if init_time else None,
                        "window_end_et": window_end.isoformat(),
                        "reason": "init_after_price_base_window",
                    },
                )
            if self._price_base is None:
                return

        amp = abs(self._price_0945 - self._price_base) / self._price_base if self._price_base > 0 else 0.0
        target_low_amp = float((self.params.extras or {}).get("target_low_amp", 1.2))
        target_mid_amp = float((self.params.extras or {}).get("target_mid_amp", 2.0))
        target_high_amp = float((self.params.extras or {}).get("target_high_amp", 3.0))
        # deadwater removed: keep normal calibration path even on very low 09:45 amplitude.
        self._deadwater = False
        if amp < 0.002:
            self._locked_add_threshold = 50
            self._locked_dev_threshold = 0.0012
            self._locked_target = target_low_amp
        elif 0.002 <= amp < 0.0045:
            self._locked_add_threshold = 120
            self._locked_dev_threshold = 0.0010
            self._locked_target = target_mid_amp
        else:
            self._locked_add_threshold = 180
            self._locked_dev_threshold = 0.0008
            self._locked_target = target_high_amp
        self._amp_locked = True
        logger.info(
            "qqq_amp_locked",
            strategy_id=self.strategy_id,
            amp=round(amp, 6),
            add_threshold=self._locked_add_threshold,
            dev_threshold=self._locked_dev_threshold,
            target=self._locked_target,
        )
        self._persist_runtime_state(reason="amp_locked")

    # ----------------- Entry -----------------

    async def _maybe_enter(self, now: datetime) -> None:
        extras = self.params.extras or {}

        def _emit_gate(
            decision: str,
            reason: str,
            *,
            direction: str = "NA",
            instrument: str = "NA",
            mode: str = "NA",
            qqq_3m_ago: Optional[float] = None,
            qqew_3m_ago: Optional[float] = None,
            v3m_price: Optional[float] = None,
            v3m_signed: Optional[float] = None,
            qqew_vs_vwap: Optional[float] = None,
            qqq_close_now: Optional[float] = None,
            qqew_close_now: Optional[float] = None,
            vwap_now: Optional[float] = None,
            qqew_vwap_now: Optional[float] = None,
            atr_live: Optional[float] = None,
            atr_seed: Optional[float] = None,
            atr_eff: Optional[float] = None,
            atr_cap: Optional[float] = None,
            k: Optional[float] = None,
            pulse: Optional[bool] = None,
            vol_ratio: Optional[float] = None,
            tv: Optional[float] = None,
            vol_t: Optional[float] = None,
            vol_ma10: Optional[float] = None,
            vol_surge_ratio: Optional[float] = None,
            distance_from_vwap_atr: Optional[float] = None,
            data_age_qqq_sec: Optional[float] = None,
            data_age_qqew_sec: Optional[float] = None,
            integrity_failure_detail: Optional[str] = None,
            audit_decision_id: Optional[str] = None,
            audit_trigger_id: Optional[str] = None,
            audit_trace_id: Optional[str] = None,
        ) -> None:
            now_ms = int(now.timestamp() * 1000)
            qqq_now_fb = qqq_close_now if qqq_close_now is not None else self._last_price("QQQ")
            qqew_now_fb = qqew_close_now if qqew_close_now is not None else self._last_price(QQEW_SYMBOL)
            vwap_now_fb = vwap_now if vwap_now is not None else self._vwap()
            qqew_vwap_now_fb = qqew_vwap_now if qqew_vwap_now is not None else self._qqew_vwap()
            qqq_ago_fb = qqq_3m_ago if qqq_3m_ago is not None else self._qqq_value_at_or_before(now_ms - 180_000)
            qqew_ago_fb = qqew_3m_ago if qqew_3m_ago is not None else self._qqew_value_at_or_before(now_ms - 180_000)
            v3m_signed_fb = v3m_signed
            if v3m_signed_fb is None and qqq_now_fb is not None and qqq_ago_fb is not None:
                v3m_signed_fb = float(qqq_now_fb - qqq_ago_fb)
            v3m_price_fb = v3m_price
            if v3m_price_fb is None and v3m_signed_fb is not None:
                v3m_price_fb = abs(float(v3m_signed_fb))
            qqew_vs_vwap_fb = qqew_vs_vwap
            if (
                qqew_vs_vwap_fb is None
                and qqew_now_fb is not None
                and qqew_vwap_now_fb is not None
                and float(qqew_vwap_now_fb) > 0
            ):
                qqew_vs_vwap_fb = float((float(qqew_now_fb) - float(qqew_vwap_now_fb)) / float(qqew_vwap_now_fb))

            metrics = self._current_entry_metrics(now)
            vol_t_fb = vol_t if vol_t is not None else metrics.get("vol_t", 0.0)
            vol_ma10_fb = vol_ma10 if vol_ma10 is not None else metrics.get("vol_ma10", 0.0)
            vol_surge_ratio_fb = vol_surge_ratio if vol_surge_ratio is not None else metrics.get("vol_surge_ratio", 0.0)
            vol_ratio_fb = vol_ratio if vol_ratio is not None else metrics.get("vol_surge_ratio", 0.0)

            age_qqq_fb = data_age_qqq_sec if data_age_qqq_sec is not None else self._symbol_age_sec("QQQ", now)
            age_qqew_fb = data_age_qqew_sec if data_age_qqew_sec is not None else self._symbol_age_sec(QQEW_SYMBOL, now)

            atr_period = int((self.params.extras or {}).get("atr_period", 14))
            atr_cap_mult = float((self.params.extras or {}).get("atr_cap_mult", 1.8))
            symbol_for_atr = instrument if instrument in {"TQQQ", "SQQQ"} else ("TQQQ" if direction == "LONG" else "SQQQ")
            atr_live_fb = atr_live if atr_live is not None else self._atr_5m(symbol_for_atr, atr_period)
            atr_seed_fb = atr_seed if atr_seed is not None else self._atr_pre.get(symbol_for_atr)
            atr_cap_fb = atr_cap
            if atr_cap_fb is None and atr_seed_fb is not None and float(atr_seed_fb) > 0:
                atr_cap_fb = atr_cap_mult * float(atr_seed_fb)
            atr_eff_fb = atr_eff
            if atr_eff_fb is None:
                if atr_live_fb is not None and atr_cap_fb is not None:
                    atr_eff_fb = min(float(atr_live_fb), float(atr_cap_fb))
                else:
                    atr_eff_fb = self._effective_atr(symbol_for_atr, atr_period, now=now)
            distance_from_vwap_atr_fb = distance_from_vwap_atr
            if (
                distance_from_vwap_atr_fb is None
                and qqq_now_fb is not None
                and vwap_now_fb is not None
                and atr_eff_fb is not None
                and float(atr_eff_fb) > 0
            ):
                distance_from_vwap_atr_fb = abs(float(qqq_now_fb) - float(vwap_now_fb)) / float(atr_eff_fb)

            self._append_diag_event(
                "entry_gate",
                {
                    "ts_et": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "event": "entry_gate",
                    "decision": decision,
                    "reason": reason,
                    "direction": direction,
                    "instrument": instrument,
                    "mode": mode,
                    "qqq_3m_ago": float(qqq_ago_fb) if qqq_ago_fb is not None else 0.0,
                    "qqew_3m_ago": float(qqew_ago_fb) if qqew_ago_fb is not None else 0.0,
                    "v3m_price": float(v3m_price_fb) if v3m_price_fb is not None else 0.0,
                    "v3m_signed": float(v3m_signed_fb) if v3m_signed_fb is not None else 0.0,
                    "qqew_vs_vwap": float(qqew_vs_vwap_fb) if qqew_vs_vwap_fb is not None else 0.0,
                    "qqq_close_now": float(qqq_now_fb) if qqq_now_fb is not None else 0.0,
                    "qqew_close_now": float(qqew_now_fb) if qqew_now_fb is not None else 0.0,
                    "vwap_now": float(vwap_now_fb) if vwap_now_fb is not None else 0.0,
                    "qqew_vwap_now": float(qqew_vwap_now_fb) if qqew_vwap_now_fb is not None else 0.0,
                    "atr_live": float(atr_live_fb) if atr_live_fb is not None else 0.0,
                    "atr_seed": float(atr_seed_fb) if atr_seed_fb is not None else 0.0,
                    "atr_eff": float(atr_eff_fb) if atr_eff_fb is not None else 0.0,
                    "atr_cap": float(atr_cap_fb) if atr_cap_fb is not None else 0.0,
                    "k": float(k) if k is not None else 0.0,
                    "pulse": bool(pulse) if pulse is not None else False,
                    "vol_ratio": float(vol_ratio_fb) if vol_ratio_fb is not None else 0.0,
                    "tv": float(tv) if tv is not None else 0.0,
                    "vol_t": float(vol_t_fb) if vol_t_fb is not None else 0.0,
                    "vol_ma10": float(vol_ma10_fb) if vol_ma10_fb is not None else 0.0,
                    "vol_surge_ratio": float(vol_surge_ratio_fb) if vol_surge_ratio_fb is not None else 0.0,
                    "distance_from_vwap_atr": float(distance_from_vwap_atr_fb) if distance_from_vwap_atr_fb is not None else 0.0,
                    "data_age_qqq": float(age_qqq_fb) if age_qqq_fb is not None else 0.0,
                    "data_age_qqew": float(age_qqew_fb) if age_qqew_fb is not None else 0.0,
                    "data_age_qqq_sec": float(age_qqq_fb) if age_qqq_fb is not None else 0.0,
                    "data_age_qqew_sec": float(age_qqew_fb) if age_qqew_fb is not None else 0.0,
                    "integrity_failure_detail": str(integrity_failure_detail or ""),
                    "stream_invalid": bool(self._stream_invalid),
                    "seed_ready": bool(self._seed_ready),
                    "seed_n": int(len(self._seed_velocity_samples)),
                    "openclose_base": float(self._price_base) if self._price_base is not None else None,
                    "openclose_base_source": str(self._price_base_source or "unknown"),
                    "first_entry_guard_applied": bool(
                        self._stable_entry_guard_enabled(extras)
                        and self._is_first_entry_guard_context()
                        and bool(extras.get("first_entry_require_seed_ready", False))
                    ),
                    "restart_degrade_active": bool(self._restart_degrade_active(now)),
                    "restart_degrade_until_et": self._no_new_entries_until_et.isoformat() if self._no_new_entries_until_et else None,
                    "restart_degrade_unlock_reason": self._restart_degrade_unlock_reason,
                },
            )
            self._emit_local_audit(
                stage="gate_blocked" if str(decision).upper() == "BLOCKED" else "gate_evaluated",
                status=str(decision or "").lower(),
                reason=reason,
                symbol=(instrument if instrument not in {"", "NA"} else None),
                decision_id=audit_decision_id,
                trigger_id=audit_trigger_id,
                trace_id=audit_trace_id,
                payload={
                    "direction": direction,
                    "instrument": instrument,
                    "mode": mode,
                    "v3m_price": float(v3m_price_fb) if v3m_price_fb is not None else None,
                    "v3m_signed": float(v3m_signed_fb) if v3m_signed_fb is not None else None,
                    "qqq_close_now": float(qqq_now_fb) if qqq_now_fb is not None else None,
                    "vwap_now": float(vwap_now_fb) if vwap_now_fb is not None else None,
                    "atr_eff": float(atr_eff_fb) if atr_eff_fb is not None else None,
                    "vol_surge_ratio": float(vol_surge_ratio_fb) if vol_surge_ratio_fb is not None else None,
                    "distance_from_vwap_atr": float(distance_from_vwap_atr_fb) if distance_from_vwap_atr_fb is not None else None,
                    "seed_ready": bool(self._seed_ready),
                    "seed_n": int(len(self._seed_velocity_samples)),
                    "integrity_failure_detail": str(integrity_failure_detail or ""),
                },
            )
            try:
                eval_trace_id = str(audit_trace_id or audit_decision_id or f"{self.strategy_id}:{now.strftime('%Y%m%d%H%M')}:{instrument}:{direction}")
                eval_decision_id = str(audit_decision_id or eval_trace_id)
                eval_trigger_id = str(audit_trigger_id or "")
                self._signal_authority.publish_evaluation_snapshot(
                    strategy_id=str(self.strategy_id),
                    decision_id=eval_decision_id,
                    trigger_id=eval_trigger_id,
                    trace_id=eval_trace_id,
                    ts=now,
                    allow=str(decision).upper() != "BLOCKED",
                    side=str(direction or "FLAT"),
                    reason=str(reason or ""),
                    exec_symbol=str(instrument if instrument not in {"", "NA"} else ""),
                    signal_state={
                        "mode": str(mode),
                        "decision": str(decision),
                        "gate_reason": str(reason or ""),
                        "v3m_signed": float(v3m_signed_fb) if v3m_signed_fb is not None else None,
                        "atr_eff": float(atr_eff_fb) if atr_eff_fb is not None else None,
                        "vol_surge_ratio": float(vol_surge_ratio_fb) if vol_surge_ratio_fb is not None else None,
                        "distance_from_vwap_atr": float(distance_from_vwap_atr_fb) if distance_from_vwap_atr_fb is not None else None,
                        "data_age_qqq_sec": float(age_qqq_fb) if age_qqq_fb is not None else None,
                        "data_age_qqew_sec": float(age_qqew_fb) if age_qqew_fb is not None else None,
                        "integrity_failure_detail": str(integrity_failure_detail or ""),
                    },
                )
            except Exception as exc:
                logger.warning(
                    "qqq_signal_authority_evaluation_snapshot_failed",
                    strategy_id=str(self.strategy_id),
                    error=str(exc),
                )
        if now.weekday() > 4 and (not self._all_day_test_mode()):
            return

        max_trades_per_day = int(extras.get("max_trades_per_day", 3))
        # GATE: day max trades + GateA for third trade
        if self._success_count >= max(1, max_trades_per_day):
            return
        if (
            self._third_trade_requires_positive_day_pnl(extras)
            and self._success_count >= 2
            and self._day_realized_pnl <= 0
        ):
            return

        now_mono = asyncio.get_running_loop().time()
        if now_mono < self._entry_timeout_block_until_mono:
            warn_interval_sec = max(5.0, float(extras.get("entry_timeout_block_warn_interval_sec", 15.0)))
            if (now_mono - self._last_entry_timeout_block_warn_mono) >= warn_interval_sec:
                self._last_entry_timeout_block_warn_mono = now_mono
                logger.warning(
                    "qqq_entry_blocked_by_timeout_cooldown",
                    strategy_id=self.strategy_id,
                    remaining_sec=round(float(self._entry_timeout_block_until_mono - now_mono), 3),
                    warn_interval_sec=warn_interval_sec,
                )
            return

        # GATE: Memory lock for in-flight decisions with timeout (60s)
        if self._pending_decision_id is not None:
            # If the decision is too old, something might have gone wrong, allow clearing it
            # But for now, we stay safe and block.
            return

        # Evaluate cadence: default once per second; optional once per minute.
        sec_ts = _floor_second(now)
        if self._last_entry_eval_second == sec_ts:
            return
        self._last_entry_eval_second = sec_ts
        if bool((self.params.extras or {}).get("entry_check_on_new_minute_only", False)):
            minute_ts = _floor_minute(now)
            if self._last_entry_eval_minute == minute_ts:
                return
            self._last_entry_eval_minute = minute_ts

        entry_model_preview = str(extras.get("entry_model", "baseline")).strip().lower()
        elite_ab_dual_mode = bool(extras.get("elite_ab_dual_mode", False))

        def _parse_hms(raw: Any, default: time) -> time:
            try:
                val = str(raw or "").strip()
                if not val:
                    return default
                return time.fromisoformat(val)
            except Exception:
                return default

        if entry_model_preview == "v47_fast_drift":
            gate_start = _parse_hms(extras.get("ab_time_start", "09:30:00"), time(9, 30))
            gate_end = _parse_hms(extras.get("ab_time_end", "14:00:00"), time(14, 0))
            # Keep v47 entry window open until at least 14:30 ET.
            if gate_end < time(14, 30):
                gate_end = time(14, 30)
        elif entry_model_preview in {"v46_ab"} or (entry_model_preview == "v44_elite" and elite_ab_dual_mode):
            gate_start = _parse_hms(extras.get("ab_time_start", "09:32:00"), time(9, 32))
            gate_end = _parse_hms(extras.get("ab_time_end", "10:30:00"), time(10, 30))
        else:
            gate_start = time(9, 45)
            gate_end = time(15, 30)

        # Non-trading hours: skip entry checks entirely to avoid per-second diagnostics/log churn.
        if (not self._all_day_test_mode()) and (now.time() < gate_start or now.time() > gate_end):
            return

        if (not self._all_day_test_mode()) and now.time() >= time(9, 45) and (not self._amp_locked):
            _emit_gate("BLOCKED", "amp_not_locked")
            return

        stable_block_reason = self._stable_entry_block_reason(now, extras)
        if stable_block_reason:
            gate_kwargs: Dict[str, Any] = {}
            if stable_block_reason == "data_integrity_fail_closed":
                gate_kwargs["integrity_failure_detail"] = self._entry_data_integrity_failure(now, extras)
            _emit_gate("BLOCKED", stable_block_reason, **gate_kwargs)
            return

        if await self._legacy_pre_entry_execution_gate(extras):
            return

        # Day lock is controlled by success_count (completed round-trips), not tentative in-memory traded state.
        # Keep evaluating while success_count==0 so rejected/missed intents do not lock the day.

        eps = 1e-9
        max_data_age_sec = float(extras.get("entry_max_data_age_sec", 3.0))
        max_data_age_qqq_sec = float(extras.get("entry_max_data_age_sec_qqq", max_data_age_sec))
        trade_allow_age_qqq_sec = float(extras.get("entry_trade_allow_data_age_sec_qqq", 10.0))
        max_data_age_qqew_sec = float(extras.get("entry_max_data_age_sec_qqew", max_data_age_sec))
        elite_decouple_qqew = self._qqew_decoupled()
        vol_pulse_on = float(extras.get("vol_pulse_on", 1.6))
        k_normal = float(extras.get("k_normal", 0.40))
        k_pulse = float(extras.get("k_pulse", 0.30))
        atr_cap_mult = float(extras.get("atr_cap_mult", 1.8))
        tv_min_usd = float(extras.get("tv_min_usd", 0.20))
        high_vol_guard = bool(extras.get("enable_high_vol_guard", True))
        high_vol_ratio = float(extras.get("high_vol_ratio", 2.2))
        hv_k = float(extras.get("hv_k", 0.50))
        hv_tv_min = float(extras.get("hv_tv_min_usd", 0.60))
        hv_min_vol_surge = float(extras.get("hv_min_vol_surge", 1.2))
        entry_model = entry_model_preview

        qqq_now = self._last_price("QQQ")
        qqew_now = self._last_price(QQEW_SYMBOL)
        qqq_ago = self._qqq_value_at_or_before(int(now.timestamp() * 1000) - 180_000)
        qqew_ago = self._qqew_value_at_or_before(int(now.timestamp() * 1000) - 180_000)
        age_qqq = self._symbol_age_sec("QQQ", now)
        age_qqew = self._symbol_age_sec(QQEW_SYMBOL, now)
        if qqq_now is None or age_qqq is None:
            self._stream_invalid = True
            _emit_gate("BLOCKED", "data_not_ready", qqq_3m_ago=qqq_ago, qqew_3m_ago=qqew_ago, data_age_qqq_sec=age_qqq, data_age_qqew_sec=age_qqew)
            return
        if age_qqq > max_data_age_qqq_sec:
            self._stream_invalid = True
            _emit_gate("BLOCKED", "data_not_ready", qqq_3m_ago=qqq_ago, qqew_3m_ago=qqew_ago, data_age_qqq_sec=age_qqq, data_age_qqew_sec=age_qqew)
            return
        trade_age_ok = age_qqq <= trade_allow_age_qqq_sec
        if (not elite_decouple_qqew) and (qqew_now is None or age_qqew is None or age_qqew > max_data_age_qqew_sec):
            self._stream_invalid = True
            _emit_gate("BLOCKED", "data_not_ready", qqq_3m_ago=qqq_ago, qqew_3m_ago=qqew_ago, data_age_qqq_sec=age_qqq, data_age_qqew_sec=age_qqew)
            return
        require_continuity = bool(extras.get("entry_require_continuity_180s", False))
        if require_continuity and (not self._has_continuous_180s(now, include_qqew=(not elite_decouple_qqew))):
            self._stream_invalid = True
            _emit_gate("BLOCKED", "data_not_ready", qqq_3m_ago=qqq_ago, qqew_3m_ago=qqew_ago, data_age_qqq_sec=age_qqq, data_age_qqew_sec=age_qqew)
            return
        continuity_ok = True
        self._stream_invalid = False

        if qqq_ago is None or ((not elite_decouple_qqew) and qqew_ago is None):
            _emit_gate("BLOCKED", "data_not_ready", qqq_3m_ago=qqq_ago, qqew_3m_ago=qqew_ago, qqq_close_now=qqq_now, qqew_close_now=qqew_now, data_age_qqq_sec=age_qqq, data_age_qqew_sec=age_qqew)
            return
        v3m_signed = float(qqq_now - qqq_ago)
        v3m_q = abs(v3m_signed)
        vwap_now = self._vwap()
        qqew_vwap_now = self._qqew_vwap()
        qqew_vs_vwap = float((float(qqew_now) - float(qqew_vwap_now)) / float(qqew_vwap_now)) if (qqew_now is not None and qqew_vwap_now > 0) else 0.0
        atr_live = self._atr_1m_qqq(int(extras.get("atr_period_1m", 14)))
        atr_seed = self._atr_pre.get("TQQQ")
        vol_t = float(self._min_bar.volume) if self._min_bar is not None else 0.0
        vol_ma10 = float(sum(self._rolling_vol_queue) / len(self._rolling_vol_queue)) if len(self._rolling_vol_queue) >= 10 else None
        if (
            vwap_now <= 0
            or ((not elite_decouple_qqew) and qqew_vwap_now <= 0)
            or atr_live is None
            or atr_live <= 0
            or atr_seed is None
            or atr_seed <= 0
            or self._min_bar is None
            or vol_ma10 is None
        ):
            _emit_gate(
                "BLOCKED",
                "data_not_ready",
                qqq_3m_ago=qqq_ago,
                qqew_3m_ago=qqew_ago,
                v3m_price=v3m_q,
                v3m_signed=v3m_signed,
                qqew_vs_vwap=qqew_vs_vwap,
                qqq_close_now=qqq_now,
                qqew_close_now=qqew_now,
                vwap_now=vwap_now,
                qqew_vwap_now=qqew_vwap_now,
                atr_live=atr_live,
                atr_seed=atr_seed,
                vol_t=vol_t,
                vol_ma10=vol_ma10,
                data_age_qqq_sec=age_qqq,
                data_age_qqew_sec=age_qqew,
            )
            return

        prev_vol_surge_ratio = float(self._prev_vol_surge_ratio) if self._prev_vol_surge_ratio is not None else None
        vol_surge_ratio = float(vol_t / max(float(vol_ma10), eps))
        self._prev_vol_surge_ratio = float(vol_surge_ratio)
        atr_cap = atr_cap_mult * float(atr_seed)
        atr_eff = min(float(atr_live), float(atr_cap))
        if atr_eff <= 0:
            _emit_gate("BLOCKED", "data_not_ready", atr_live=atr_live, atr_seed=atr_seed, atr_cap=atr_cap)
            return
        distance_from_vwap_atr = abs(float(qqq_now) - float(vwap_now)) / max(float(atr_eff), eps)

        direction = "NA"
        exec_symbol = "NA"
        mode = "NORMAL"
        pulse = False
        k = 0.0
        tv = 0.0

        if entry_model == "v47_fast_drift":
            mode = "FAST_DRIFT"
            if float(qqq_now) > float(vwap_now):
                direction = "LONG"
                exec_symbol = "TQQQ"
            elif float(qqq_now) < float(vwap_now):
                direction = "SHORT"
                exec_symbol = "SQQQ"
            else:
                _emit_gate("BLOCKED", "direction_mismatch", qqq_close_now=qqq_now, vwap_now=vwap_now)
                return

            now_ms = int(now.timestamp() * 1000)
            atr_rank_src = extras.get("atr_rank_today", self._atr_rank_prev_day)
            atr_rank = _safe_float(atr_rank_src)
            atr_rank_skip_max = float(extras.get("atr_rank_skip_max", 0.15))
            early_override_window_start = _parse_hms(extras.get("early_override_window_start", "09:39:00"), time(9, 39))
            early_override_window_end = _parse_hms(extras.get("early_override_window_end", "09:45:00"), time(9, 45))

            def _early_override_ready() -> bool:
                early_move_mult = float(extras.get("early_override_move_mult", 0.70))
                early_vol_min = float(extras.get("early_override_vol_min", 1.50))
                # Need full 09:38 minute bar to evaluate 09:30-09:38 structure.
                if now.time() < time(9, 39):
                    return False
                open_0930 = self._price_base
                px_0938 = self._qqq_value_at_or_before(int(now.replace(hour=9, minute=38, second=0, microsecond=0).timestamp() * 1000))
                if open_0930 is None or px_0938 is None:
                    return False
                atr_d_src = extras.get("atr_d_override", self._atr_d_prev_day)
                atr_d = _safe_float(atr_d_src)
                if atr_d is None or atr_d <= 0:
                    return False
                early_move = abs(float(px_0938) - float(open_0930))
                if early_move < early_move_mult * atr_d:
                    return False
                side_sign: Optional[int] = None
                # Minute-end keys: 09:31..09:39 correspond to 09:30..09:38 minutes.
                for mm in range(31, 40):
                    minute_end = now.replace(hour=9, minute=mm, second=0, microsecond=0)
                    c = self._minute_close_for_gate(minute_end, now)
                    v = self._vwap_minute.get(minute_end)
                    if c is None or v is None or float(v) <= 0:
                        return False
                    s = 1 if float(c) > float(v) else (-1 if float(c) < float(v) else 0)
                    if s == 0:
                        return False
                    if side_sign is None:
                        side_sign = s
                    elif side_sign != s:
                        return False
                vols = []
                vol_0938 = None
                for bar in list(self._minute_bars):
                    bt = bar.ts_end.timetz().replace(tzinfo=None)
                    if time(9, 31) <= bt <= time(9, 39):
                        if bt == time(9, 39):
                            vol_0938 = float(bar.volume)
                        else:
                            vols.append(float(bar.volume))
                if vol_0938 is None or len(vols) < 7:
                    return False
                base_vol = sum(vols) / max(1, len(vols))
                if base_vol <= 0:
                    return False
                if (vol_0938 / base_vol) < early_vol_min:
                    return False
                return True

            in_early_override_window = early_override_window_start <= now.time() <= early_override_window_end
            early_override = _early_override_ready() if in_early_override_window else False
            # Priority: opening window only. After window end, always fall back to atr-rank gate.
            if (not early_override) and (atr_rank is None):
                _emit_gate("BLOCKED", "atr_rank_not_ready", direction=direction, instrument=exec_symbol, mode=mode)
                return
            if (not early_override) and (atr_rank is not None) and (atr_rank <= atr_rank_skip_max):
                _emit_gate("BLOCKED", "atr_rank_skip", direction=direction, instrument=exec_symbol, mode=mode, tv=atr_rank_skip_max, vol_ratio=atr_rank)
                return
            if now.time() < time(9, 45) and not early_override:
                _emit_gate("BLOCKED", "pre_0945_no_early_override", direction=direction, instrument=exec_symbol, mode=mode)
                return

            delayed_confirm_end = _parse_hms(extras.get("delayed_confirm_end", "10:00:00"), time(10, 0))
            delayed_confirm_move5m_min = float(extras.get("delayed_confirm_move5m_min", 0.155))
            early_lock_mode = str(extras.get("early_lock_mode", "openclose")).strip().lower()
            if time(9, 45) <= now.time() <= delayed_confirm_end:
                minute_ts = _floor_minute(now)
                close_now_1m = self._minute_close_for_gate(minute_ts, now)
                close_5m_ago_1m = self._minute_close_for_gate(minute_ts - timedelta(minutes=5), now)
                atr_d_src = extras.get("atr_d_override", self._atr_d_prev_day)
                atr_d = _safe_float(atr_d_src)
                if close_now_1m is None or close_5m_ago_1m is None or atr_d is None or atr_d <= 0:
                    _emit_gate("BLOCKED", "delayed_confirm_not_ready", direction=direction, instrument=exec_symbol, mode=mode)
                    return
                move5m = abs(float(close_now_1m) - float(close_5m_ago_1m)) / max(float(atr_d), eps)
                if move5m < delayed_confirm_move5m_min:
                    _emit_gate("BLOCKED", "delayed_confirm_not_met", direction=direction, instrument=exec_symbol, mode=mode, tv=delayed_confirm_move5m_min, v3m_price=move5m)
                    return
                if early_lock_mode == "openclose" and self._price_base is not None:
                    dir_oc = 1 if float(qqq_now) > float(self._price_base) else (-1 if float(qqq_now) < float(self._price_base) else 0)
                    if (direction == "LONG" and dir_oc <= 0) or (direction == "SHORT" and dir_oc >= 0):
                        _emit_gate("BLOCKED", "early_lock_openclose_mismatch", direction=direction, instrument=exec_symbol, mode=mode)
                        return

            deadzone = float(extras.get("momentum_deadzone", 0.05))
            sign_ok = (direction == "LONG" and v3m_signed > deadzone) or (direction == "SHORT" and v3m_signed < -deadzone)
            v3m_atr = float(v3m_q / max(float(atr_eff), eps))

            veto_start = _parse_hms(extras.get("veto_start", "09:40:00"), time(9, 40))
            veto_end = _parse_hms(extras.get("veto_end", "11:50:00"), time(11, 50))
            veto_rebound_live_atr = float(extras.get("veto_rebound_live_atr", 0.25))
            enable_v47_veto = bool(extras.get("enable_v47_veto", False))
            if enable_v47_veto and veto_start <= now.time() <= veto_end:
                ref_30s = self._qqq_value_at_or_before(now_ms - 30_000)
                ref_120s = self._qqq_value_at_or_before(now_ms - 120_000)
                if ref_30s is not None:
                    if direction == "LONG" and float(qqq_now) < float(ref_30s):
                        _emit_gate("BLOCKED", "long_rollback_veto", direction=direction, instrument=exec_symbol, mode=mode, qqq_close_now=qqq_now)
                        return
                    if direction == "SHORT" and float(qqq_now) > float(ref_30s):
                        _emit_gate("BLOCKED", "short_rollback_veto", direction=direction, instrument=exec_symbol, mode=mode, qqq_close_now=qqq_now)
                        return
                if ref_120s is not None:
                    move_120s_live_atr = (float(qqq_now) - float(ref_120s)) / max(float(atr_live), eps)
                    if direction == "LONG" and move_120s_live_atr < -veto_rebound_live_atr:
                        _emit_gate("BLOCKED", "long_rebound_fail_veto", direction=direction, instrument=exec_symbol, mode=mode, v3m_price=v3m_q, vol_ratio=move_120s_live_atr)
                        return
                    if direction == "SHORT" and move_120s_live_atr > veto_rebound_live_atr:
                        _emit_gate("BLOCKED", "short_rebound_fail_veto", direction=direction, instrument=exec_symbol, mode=mode, v3m_price=v3m_q, vol_ratio=move_120s_live_atr)
                        return

            fast_enabled = bool(extras.get("enable_fast", False))
            fast_v3m_atr_min = float(extras.get("fast_v3m_atr_min", 0.60))
            fast_vol_min = float(extras.get("fast_vol_min", 999.0))
            fast_crosses_threshold = int(extras.get("fast_crosses_threshold", 3))
            fast_cooldown_min = float(extras.get("fast_cooldown_min", 15.0))

            if self._cross_count > fast_crosses_threshold and self._cross_count > self._fast_cooldown_trigger_cross:
                self._fast_cooldown_trigger_cross = int(self._cross_count)
                self._fast_cooldown_until = now + timedelta(minutes=max(1.0, fast_cooldown_min))
                logger.warning(
                    "qqq_fast_cooldown_armed",
                    strategy_id=self.strategy_id,
                    cross_count=self._cross_count,
                    cooldown_until_et=self._fast_cooldown_until.isoformat() if self._fast_cooldown_until else None,
                )

            fast_in_cooldown = self._fast_cooldown_until is not None and now < self._fast_cooldown_until
            fast_ok = (
                fast_enabled
                and sign_ok
                and (v3m_atr >= fast_v3m_atr_min)
                and (vol_surge_ratio >= fast_vol_min)
                and (not fast_in_cooldown)
            )

            drift_bar_min_atr = float(extras.get("drift_bar_min_atr", 0.10))
            drift_cum_atr_min = float(extras.get("drift_cum_atr_min", 0.45))
            minute_end = _floor_minute(now)
            c0 = self._minute_close_for_gate(minute_end, now)
            c1 = self._minute_close_for_gate(minute_end - timedelta(minutes=1), now)
            c2 = self._minute_close_for_gate(minute_end - timedelta(minutes=2), now)
            c3 = self._minute_close_for_gate(minute_end - timedelta(minutes=3), now)
            drift_ok = False
            if c0 is not None and c1 is not None and c2 is not None and c3 is not None and sign_ok:
                bar_th = drift_bar_min_atr * float(atr_eff)
                d1 = float(c0 - c1)
                d2 = float(c1 - c2)
                d3 = float(c2 - c3)
                cum = float(c0 - c3)
                if direction == "LONG":
                    drift_ok = (d1 >= bar_th and d2 >= bar_th and d3 >= bar_th and cum >= (drift_cum_atr_min * float(atr_eff)))
                else:
                    drift_ok = (-d1 >= bar_th and -d2 >= bar_th and -d3 >= bar_th and -cum >= (drift_cum_atr_min * float(atr_eff)))

            if fast_ok:
                mode = "FAST"
                tv = fast_v3m_atr_min
            elif drift_ok:
                mode = "DRIFT"
                tv = drift_cum_atr_min
                # Optional anti-fake-break gate for opening DRIFT moves.
                # Causal version: confirm current minute close still extends
                # in trend direction vs N minutes ago and remains on trend-side VWAP.
                long_drift_second_confirm = bool(extras.get("long_drift_second_confirm", False))
                long_drift_second_confirm_side = str(extras.get("long_drift_second_confirm_side", "long")).strip().lower()
                long_drift_second_confirm_window_start = _parse_hms(
                    extras.get("long_drift_second_confirm_window_start", "09:45:00"),
                    time(9, 45),
                )
                long_drift_second_confirm_window_end = _parse_hms(
                    extras.get("long_drift_second_confirm_window_end", "10:29:00"),
                    time(10, 29),
                )
                long_drift_second_confirm_delay_min = max(
                    1,
                    int(extras.get("long_drift_second_confirm_delay_min", 1)),
                )
                confirm_side_ok = (direction == "LONG") if long_drift_second_confirm_side == "long" else (direction in {"LONG", "SHORT"})
                if (
                    long_drift_second_confirm
                    and confirm_side_ok
                    and (long_drift_second_confirm_window_start <= now.time() <= long_drift_second_confirm_window_end)
                ):
                    c_now = self._minute_close_for_gate(minute_end, now)
                    c_prev = self._minute_close_for_gate(
                        minute_end - timedelta(minutes=long_drift_second_confirm_delay_min),
                        now,
                    )
                    if c_now is None or c_prev is None or float(vwap_now) <= 0:
                        _emit_gate(
                            "BLOCKED",
                            "long_drift_second_confirm_missing",
                            direction=direction,
                            instrument=exec_symbol,
                            mode=mode,
                        )
                        return
                    if direction == "LONG":
                        confirm_ok = (float(c_now) >= float(c_prev)) and (float(c_now) > float(vwap_now))
                    else:
                        confirm_ok = (float(c_now) <= float(c_prev)) and (float(c_now) < float(vwap_now))
                    if not confirm_ok:
                        _emit_gate(
                            "BLOCKED",
                            "long_drift_second_confirm_failed",
                            direction=direction,
                            instrument=exec_symbol,
                            mode=mode,
                            qqq_close_now=qqq_now,
                            vwap_now=vwap_now,
                        )
                        return
                chase_guard = self._v47_chase_guard_reason(
                    now=now,
                    direction=direction,
                    qqq_now=float(qqq_now),
                    vwap_now=float(vwap_now),
                    extras=extras,
                )
                if chase_guard:
                    _emit_gate(
                        "BLOCKED",
                        str(chase_guard.get("reason")),
                        direction=direction,
                        instrument=exec_symbol,
                        mode=mode,
                        qqq_close_now=qqq_now,
                        vwap_now=vwap_now,
                        v3m_signed=v3m_signed,
                        vol_ratio=float(chase_guard.get("pos_day", 0.0)),
                        tv=float(chase_guard.get("tv", 0.0)),
                    )
                    return
            else:
                if fast_in_cooldown and sign_ok and v3m_atr >= fast_v3m_atr_min and vol_surge_ratio >= fast_vol_min:
                    _emit_gate("BLOCKED", "fast_cooldown_active", direction=direction, instrument=exec_symbol, mode="FAST", tv=fast_cooldown_min, vol_surge_ratio=vol_surge_ratio, v3m_price=v3m_q)
                elif not sign_ok:
                    _emit_gate("BLOCKED", "sign_mismatch", direction=direction, instrument=exec_symbol, mode=mode, tv=deadzone, v3m_signed=v3m_signed)
                else:
                    _emit_gate("BLOCKED", "fast_drift_not_ready", direction=direction, instrument=exec_symbol, mode=mode, tv=drift_cum_atr_min, v3m_price=v3m_q, vol_surge_ratio=vol_surge_ratio)
                return
        elif entry_model == "v46_ab":
            mode = "ELITE_AB"
            if float(qqq_now) > float(vwap_now):
                direction = "LONG"
                exec_symbol = "TQQQ"
            elif float(qqq_now) < float(vwap_now):
                direction = "SHORT"
                exec_symbol = "SQQQ"
            else:
                _emit_gate("BLOCKED", "direction_mismatch", qqq_close_now=qqq_now, vwap_now=vwap_now)
                return

            ab_deadzone_mult = float(extras.get("elite_deadzone_mult", 0.15))
            ab_v3m_min_mult = float(extras.get("elite_v3m_min_mult", 0.35))
            ab_decay_limit_b = float(extras.get("b_decay_limit", 0.95))
            ab_decay_limit_a = float(extras.get("a_decay_limit", 0.95))
            ab_overext_mult = float(extras.get("a_overext_mult", 1.60))
            ab_reentry_window_sec = float(extras.get("a_reentry_window_sec", 90.0))
            ab_pull_open = float(extras.get("a_pullback_mult_open", 1.50))
            ab_pull_late = float(extras.get("a_pullback_mult_late", 1.40))
            ab_pull_switch_t = _parse_hms(extras.get("a_pullback_switch_time", "10:00:00"), time(10, 0))
            ab_a_latest_entry_t = _parse_hms(extras.get("a_latest_entry_time", "10:30:00"), time(10, 30))
            ab_a_entry_max_dev_live_atr = float(extras.get("a_entry_max_dev_live_atr", 999.0))
            ab_a_armed_ttl_sec = float(extras.get("a_armed_ttl_sec", 99999.0))
            ab_a_recent_runup_sec = max(10, int(extras.get("a_recent_runup_sec", 90)))
            ab_a_recent_runup_max_live_atr = float(extras.get("a_recent_runup_max_live_atr", 999.0))
            ab_volatile_skip_enable = bool(extras.get("ab_volatile_day_skip_enable", False))
            ab_volatile_check_end_t = _parse_hms(extras.get("ab_volatile_check_end", "10:05:00"), time(10, 5))
            ab_volatile_range_live_atr_min = float(extras.get("ab_volatile_range_live_atr_min", 4.5))
            ab_volatile_dir_min_live_atr = float(extras.get("ab_volatile_directionality_live_atr_min", 1.0))
            ab_volatile_reversal_washout_live_atr_min = float(extras.get("ab_volatile_reversal_washout_live_atr_min", 1.5))
            ab_volatile_reversal_rebound_live_atr_min = float(extras.get("ab_volatile_reversal_rebound_live_atr_min", 0.8))
            ab_volatile_allow_early_reversal = bool(extras.get("ab_volatile_allow_early_reversal", True))
            ab_volatile_reversal_vol_min = float(extras.get("ab_volatile_reversal_vol_min", 1.2))
            ab_volatile_reversal_require_two = bool(extras.get("ab_volatile_reversal_require_two_positive", True))
            ab_volatile_probe_enable = bool(extras.get("ab_volatile_probe_enable", True))
            ab_volatile_probe_break_live_atr = float(extras.get("ab_volatile_probe_break_live_atr", 0.6))
            ab_volatile_probe_reclaim_live_atr = float(extras.get("ab_volatile_probe_reclaim_live_atr", 0.2))
            opening_mode_end_t = time(9, 40)
            opening_breakout_vol_min = 1.20
            opening_breakout_ext_mult = 1.60

            deadzone = ab_deadzone_mult * float(atr_eff)
            v3m_min = ab_v3m_min_mult * float(atr_eff)
            v3m_fast = 0.50 * float(atr_eff)
            tv = v3m_min
            dev_abs = abs(float(qqq_now) - float(vwap_now))
            dev_atr = dev_abs / max(float(atr_eff), eps)
            dev_live_atr = dev_abs / max(float(atr_live), eps)
            now_ms = int(now.timestamp() * 1000)
            is_opening_mode = now.time() < opening_mode_end_t

            volatile_reversal_ready = False
            early_reversal_direction: Optional[str] = None
            # Two-Phase fixed mode: keep logic simple and disable volatile-day veto layer.
            if ab_volatile_skip_enable and False:
                if now.time() > ab_volatile_check_end_t:
                    session_start = now.replace(hour=9, minute=32, second=0, microsecond=0)
                    check_end_dt = now.replace(
                        hour=ab_volatile_check_end_t.hour,
                        minute=ab_volatile_check_end_t.minute,
                        second=ab_volatile_check_end_t.second,
                        microsecond=0,
                    )
                    session_start_ms = int(session_start.timestamp() * 1000)
                    check_end_ms = int(check_end_dt.timestamp() * 1000)
                    session_prices = [
                        float(px)
                        for ts_ms, px in self._qqq_buffer
                        if session_start_ms <= int(ts_ms) <= check_end_ms
                    ]
                    if len(session_prices) >= 30:
                        day_range_live_atr = (max(session_prices) - min(session_prices)) / max(float(atr_live), eps)
                        opening_move_live_atr = abs(float(session_prices[-1]) - float(session_prices[0])) / max(float(atr_live), eps)
                        if (
                            day_range_live_atr >= ab_volatile_range_live_atr_min
                            and opening_move_live_atr >= ab_volatile_dir_min_live_atr
                        ):
                            self._ab_volatile_veto_direction = "SHORT" if float(session_prices[-1]) < float(session_prices[0]) else "LONG"
                            self._ab_volatile_anchor_px = float(session_prices[-1])
                            self._ab_volatile_extreme_px = float(session_prices[-1])
                            self._ab_volatile_probe_anchor_px = None
                            self._ab_volatile_probe_extreme_px = None
                            self._ab_volatile_probe_armed = False
                        if self._ab_volatile_veto_direction is not None and direction == self._ab_volatile_veto_direction:
                            _emit_gate(
                                "BLOCKED",
                                "ab_volatile_wrong_side_veto",
                                direction=direction,
                                instrument=exec_symbol,
                                mode=mode,
                                tv=float(ab_volatile_range_live_atr_min),
                                v3m_price=float(day_range_live_atr),
                            )
                            return
                if (
                    self._ab_volatile_veto_direction is not None
                    and self._ab_volatile_extreme_px is not None
                ):
                    now_px = float(qqq_now)
                    if self._ab_volatile_veto_direction == "SHORT":
                        self._ab_volatile_extreme_px = min(float(self._ab_volatile_extreme_px), now_px)
                    else:
                        self._ab_volatile_extreme_px = max(float(self._ab_volatile_extreme_px), now_px)
                if (
                    self._ab_volatile_veto_direction is not None
                    and direction != self._ab_volatile_veto_direction
                    and ab_volatile_probe_enable
                ):
                    now_px = float(qqq_now)
                    if self._ab_volatile_probe_anchor_px is None:
                        self._ab_volatile_probe_anchor_px = now_px
                        self._ab_volatile_probe_extreme_px = now_px
                        self._ab_volatile_probe_armed = False
                        _emit_gate(
                            "BLOCKED",
                            "ab_volatile_probe_wait",
                            direction=direction,
                            instrument=exec_symbol,
                            mode=mode,
                            tv=float(ab_volatile_probe_break_live_atr),
                        )
                        return
                    probe_anchor = float(self._ab_volatile_probe_anchor_px)
                    probe_extreme = float(self._ab_volatile_probe_extreme_px or now_px)
                    if self._ab_volatile_veto_direction == "SHORT":
                        probe_extreme = min(probe_extreme, now_px)
                        if probe_extreme <= (probe_anchor - ab_volatile_probe_break_live_atr * float(atr_live)):
                            self._ab_volatile_probe_armed = True
                        ready = self._ab_volatile_probe_armed and now_px >= (probe_anchor + ab_volatile_probe_reclaim_live_atr * float(atr_live))
                    else:
                        probe_extreme = max(probe_extreme, now_px)
                        if probe_extreme >= (probe_anchor + ab_volatile_probe_break_live_atr * float(atr_live)):
                            self._ab_volatile_probe_armed = True
                        ready = self._ab_volatile_probe_armed and now_px <= (probe_anchor - ab_volatile_probe_reclaim_live_atr * float(atr_live))
                    self._ab_volatile_probe_extreme_px = probe_extreme
                    if not ready:
                        _emit_gate(
                            "BLOCKED",
                            "ab_volatile_probe_wait",
                            direction=direction,
                            instrument=exec_symbol,
                            mode=mode,
                            tv=float(ab_volatile_probe_break_live_atr),
                            v3m_price=float(abs(probe_extreme - probe_anchor) / max(float(atr_live), eps)),
                        )
                        return

                if self._ab_volatile_veto_direction is not None and direction != self._ab_volatile_veto_direction and (not ab_volatile_probe_enable):
                    if self._ab_volatile_anchor_px is not None and self._ab_volatile_extreme_px is not None:
                        now_px = float(qqq_now)
                        if self._ab_volatile_veto_direction == "SHORT":
                            washout_live_atr = (float(self._ab_volatile_anchor_px) - float(self._ab_volatile_extreme_px)) / max(float(atr_live), eps)
                            rebound_live_atr = (now_px - float(self._ab_volatile_extreme_px)) / max(float(atr_live), eps)
                        else:
                            washout_live_atr = (float(self._ab_volatile_extreme_px) - float(self._ab_volatile_anchor_px)) / max(float(atr_live), eps)
                            rebound_live_atr = (float(self._ab_volatile_extreme_px) - now_px) / max(float(atr_live), eps)
                        if (
                            washout_live_atr < ab_volatile_reversal_washout_live_atr_min
                            or rebound_live_atr < ab_volatile_reversal_rebound_live_atr_min
                        ):
                            _emit_gate(
                                "BLOCKED",
                                "ab_volatile_reversal_not_ready",
                                direction=direction,
                                instrument=exec_symbol,
                                mode=mode,
                                tv=float(ab_volatile_reversal_washout_live_atr_min),
                                v3m_price=float(washout_live_atr),
                                vol_ratio=float(rebound_live_atr),
                            )
                            return
                        volatile_reversal_ready = True
                if (
                    self._ab_volatile_veto_direction is not None
                    and direction == self._ab_volatile_veto_direction
                    and ab_volatile_allow_early_reversal
                    and self._ab_volatile_anchor_px is not None
                    and self._ab_volatile_extreme_px is not None
                ):
                    prev_v3m_signed = None
                    q_prev_now = self._qqq_value_at_or_before(now_ms - 1_000)
                    q_prev_ago = self._qqq_value_at_or_before(now_ms - 181_000)
                    if q_prev_now is not None and q_prev_ago is not None:
                        prev_v3m_signed = float(q_prev_now - q_prev_ago)
                    if self._ab_volatile_veto_direction == "SHORT":
                        washout_live_atr = (float(self._ab_volatile_anchor_px) - float(self._ab_volatile_extreme_px)) / max(float(atr_live), eps)
                        rebound_live_atr = (float(qqq_now) - float(self._ab_volatile_extreme_px)) / max(float(atr_live), eps)
                        two_ok = (prev_v3m_signed is not None and prev_v3m_signed > 0 and v3m_signed > 0)
                        if (
                            washout_live_atr >= ab_volatile_reversal_washout_live_atr_min
                            and rebound_live_atr >= ab_volatile_reversal_rebound_live_atr_min
                            and v3m_signed > deadzone
                            and vol_surge_ratio >= ab_volatile_reversal_vol_min
                            and ((not ab_volatile_reversal_require_two) or two_ok)
                        ):
                            early_reversal_direction = "LONG"
                    else:
                        washout_live_atr = (float(self._ab_volatile_extreme_px) - float(self._ab_volatile_anchor_px)) / max(float(atr_live), eps)
                        rebound_live_atr = (float(self._ab_volatile_extreme_px) - float(qqq_now)) / max(float(atr_live), eps)
                        two_ok = (prev_v3m_signed is not None and prev_v3m_signed < 0 and v3m_signed < 0)
                        if (
                            washout_live_atr >= ab_volatile_reversal_washout_live_atr_min
                            and rebound_live_atr >= ab_volatile_reversal_rebound_live_atr_min
                            and v3m_signed < -deadzone
                            and vol_surge_ratio >= ab_volatile_reversal_vol_min
                            and ((not ab_volatile_reversal_require_two) or two_ok)
                        ):
                            early_reversal_direction = "SHORT"
                if self._ab_volatile_veto_direction is not None and direction == self._ab_volatile_veto_direction:
                    if early_reversal_direction is not None:
                        direction = early_reversal_direction
                        exec_symbol = "TQQQ" if direction == "LONG" else "SQQQ"
                        mode = "ELITE_AB_REV"
                    else:
                        _emit_gate(
                            "BLOCKED",
                            "ab_volatile_wrong_side_veto",
                            direction=direction,
                            instrument=exec_symbol,
                            mode=mode,
                            tv=float(ab_volatile_range_live_atr_min),
                        )
                        return

            if self._ab_a_state not in ("WAIT_MARK", "WAIT_PULLBACK", "ARMED"):
                self._ab_a_state = "WAIT_MARK"
            if self._ab_a_armed_ts is not None and self._ab_a_state != "ARMED":
                self._ab_a_armed_ts = None

            def _v3m_abs_at(offset_sec: int) -> Optional[float]:
                ts_ref = now_ms - max(0, int(offset_sec)) * 1000
                q_now_ref = self._qqq_value_at_or_before(ts_ref)
                q_ago_ref = self._qqq_value_at_or_before(ts_ref - 180_000)
                if q_now_ref is None or q_ago_ref is None:
                    return None
                return abs(float(q_now_ref - q_ago_ref))

            def _decay_ok(limit: float) -> tuple[bool, float]:
                prev: list[float] = []
                for offset in (1, 2, 3):
                    val = _v3m_abs_at(offset)
                    if val is not None:
                        prev.append(float(val))
                if len(prev) < 2:
                    return False, 0.0
                threshold = max(prev) * float(limit)
                return (v3m_q >= threshold), threshold

            def _a_vol_threshold() -> float:
                if now.time() < time(11, 30):
                    th = 1.35
                elif now.time() < time(14, 0):
                    th = 1.55
                else:
                    th = 1.45
                if v3m_q >= v3m_fast:
                    th = max(1.10, th - 0.10)
                return th

            def _opening_1m_3m_aligned() -> Optional[bool]:
                minute_end = _floor_minute(now)
                c_now = self._minute_close_for_gate(minute_end, now)
                c_prev = self._minute_close_for_gate(minute_end - timedelta(minutes=1), now)
                c_3ago = self._minute_close_for_gate(minute_end - timedelta(minutes=3), now)
                if c_now is None or c_prev is None or c_3ago is None:
                    return None
                dir1m = 0
                if c_now > vwap_now:
                    dir1m = 1
                elif c_now < vwap_now:
                    dir1m = -1
                elif c_now > c_prev:
                    dir1m = 1
                elif c_now < c_prev:
                    dir1m = -1
                dir3m = 0
                if c_now > c_3ago:
                    dir3m = 1
                elif c_now < c_3ago:
                    dir3m = -1
                if direction == "LONG":
                    return dir1m > 0 and dir3m > 0
                return dir1m < 0 and dir3m < 0

            entry_path: Optional[str] = None

            # Two-Phase Entry:
            # Phase 1 (09:30-09:40): breakout-only with 1m/3m hard alignment + vol>=1.2.
            if is_opening_mode:
                aligned = _opening_1m_3m_aligned()
                if aligned is None:
                    _emit_gate("BLOCKED", "ab_opening_direction_not_ready", direction=direction, instrument=exec_symbol, mode=mode)
                    return
                if not aligned:
                    _emit_gate("BLOCKED", "ab_opening_direction_hard_gate", direction=direction, instrument=exec_symbol, mode=mode)
                    return

                b_sign_ok = (direction == "LONG" and v3m_signed > deadzone) or (direction == "SHORT" and v3m_signed < -deadzone)
                if not b_sign_ok:
                    _emit_gate("BLOCKED", "ab_opening_sign_mismatch", direction=direction, instrument=exec_symbol, mode=mode, tv=deadzone, v3m_signed=v3m_signed)
                    return
                if v3m_q < v3m_min:
                    _emit_gate("BLOCKED", "ab_opening_velocity_too_low", direction=direction, instrument=exec_symbol, mode=mode, tv=v3m_min, v3m_price=v3m_q)
                    return

                decay_ok, decay_th = _decay_ok(ab_decay_limit_b)
                if not decay_ok:
                    _emit_gate("BLOCKED", "ab_opening_momentum_decay", direction=direction, instrument=exec_symbol, mode=mode, tv=decay_th, v3m_price=v3m_q)
                    return
                if vol_surge_ratio < opening_breakout_vol_min:
                    _emit_gate("BLOCKED", "ab_opening_volume_too_low", direction=direction, instrument=exec_symbol, mode=mode, tv=opening_breakout_vol_min, vol_surge_ratio=vol_surge_ratio, v3m_price=v3m_q)
                    return
                ext_b = opening_breakout_ext_mult * float(atr_eff)
                if dev_abs > ext_b:
                    _emit_gate("BLOCKED", "ab_opening_overextended", direction=direction, instrument=exec_symbol, mode=mode, tv=ext_b, v3m_price=v3m_q, vol_surge_ratio=vol_surge_ratio)
                    return
                entry_path = "OPEN_BREAKOUT"

            # A state machine if B not fired.
            if (entry_path is None) and (not is_opening_mode):
                if now.time() > ab_a_latest_entry_t:
                    _emit_gate("BLOCKED", "ab_a_time_cutoff", direction=direction, instrument=exec_symbol, mode=mode)
                    return
                if self._ab_a_state == "WAIT_PULLBACK":
                    if self._elite_overext_mark_ts is None or self._elite_overext_mark_direction is None:
                        self._ab_a_state = "WAIT_MARK"
                    else:
                        age_sec = (now - self._elite_overext_mark_ts).total_seconds()
                        if age_sec > ab_reentry_window_sec:
                            self._ab_a_state = "WAIT_MARK"
                            self._elite_overext_mark_ts = None
                            self._elite_overext_mark_direction = None
                        elif self._elite_overext_mark_direction != direction:
                            self._ab_a_state = "WAIT_MARK"
                            self._elite_overext_mark_ts = None
                            self._elite_overext_mark_direction = None
                        else:
                            pullback_mult_now = ab_pull_open if now.time() < ab_pull_switch_t else ab_pull_late
                            if dev_atr <= pullback_mult_now:
                                self._ab_a_state = "ARMED"
                                self._ab_a_armed_ts = now
                            else:
                                _emit_gate("BLOCKED", "ab_a_wait_pullback", direction=direction, instrument=exec_symbol, mode=mode, tv=(pullback_mult_now * float(atr_eff)), v3m_price=v3m_q)
                elif self._ab_a_state == "WAIT_MARK":
                    if v3m_q >= v3m_min and dev_atr > ab_overext_mult:
                        self._elite_overext_mark_ts = now
                        self._elite_overext_mark_direction = direction
                        self._ab_a_state = "WAIT_PULLBACK"
                        self._ab_a_armed_ts = None
                        _emit_gate("BLOCKED", "ab_a_mark_overextended", direction=direction, instrument=exec_symbol, mode=mode, tv=(ab_overext_mult * float(atr_eff)), v3m_price=v3m_q)
                    else:
                        _emit_gate("BLOCKED", "ab_a_wait_mark", direction=direction, instrument=exec_symbol, mode=mode, tv=(ab_overext_mult * float(atr_eff)), v3m_price=v3m_q)

                if self._ab_a_state == "ARMED":
                    if (
                        self._ab_a_armed_ts is not None
                        and (now - self._ab_a_armed_ts).total_seconds() > ab_a_armed_ttl_sec
                    ):
                        self._ab_a_state = "WAIT_MARK"
                        self._ab_a_armed_ts = None
                        self._elite_overext_mark_ts = None
                        self._elite_overext_mark_direction = None
                        _emit_gate("BLOCKED", "ab_a_armed_expired", direction=direction, instrument=exec_symbol, mode=mode, tv=ab_a_armed_ttl_sec, v3m_price=v3m_q)
                        return
                    if dev_live_atr > ab_a_entry_max_dev_live_atr:
                        self._ab_a_state = "WAIT_PULLBACK"
                        self._ab_a_armed_ts = None
                        self._elite_overext_mark_ts = now
                        self._elite_overext_mark_direction = direction
                        _emit_gate("BLOCKED", "ab_a_entry_overextended", direction=direction, instrument=exec_symbol, mode=mode, tv=ab_a_entry_max_dev_live_atr, qqq_close_now=qqq_now, vwap_now=vwap_now, atr_live=atr_live)
                        return
                    runup_ref = self._qqq_value_at_or_before(now_ms - (ab_a_recent_runup_sec * 1000))
                    if runup_ref is not None and atr_live > 0:
                        runup_signed = float(qqq_now - runup_ref)
                        runup_abs_live_atr = abs(runup_signed) / max(float(atr_live), eps)
                        if direction == "LONG" and runup_signed > 0 and runup_abs_live_atr > ab_a_recent_runup_max_live_atr:
                            self._ab_a_state = "WAIT_PULLBACK"
                            self._ab_a_armed_ts = None
                            self._elite_overext_mark_ts = now
                            self._elite_overext_mark_direction = direction
                            _emit_gate("BLOCKED", "ab_a_runup_overextended", direction=direction, instrument=exec_symbol, mode=mode, tv=ab_a_recent_runup_max_live_atr, atr_live=atr_live, v3m_price=v3m_q)
                            return
                        if direction == "SHORT" and runup_signed < 0 and runup_abs_live_atr > ab_a_recent_runup_max_live_atr:
                            self._ab_a_state = "WAIT_PULLBACK"
                            self._ab_a_armed_ts = None
                            self._elite_overext_mark_ts = now
                            self._elite_overext_mark_direction = direction
                            _emit_gate("BLOCKED", "ab_a_runup_overextended", direction=direction, instrument=exec_symbol, mode=mode, tv=ab_a_recent_runup_max_live_atr, atr_live=atr_live, v3m_price=v3m_q)
                            return
                    a_sign_ok = (direction == "LONG" and v3m_signed > deadzone) or (direction == "SHORT" and v3m_signed < -deadzone)
                    if not a_sign_ok:
                        _emit_gate("BLOCKED", "ab_a_sign_mismatch", direction=direction, instrument=exec_symbol, mode=mode, tv=deadzone, v3m_signed=v3m_signed)
                    elif v3m_q < v3m_min:
                        _emit_gate("BLOCKED", "ab_a_velocity_too_low", direction=direction, instrument=exec_symbol, mode=mode, tv=v3m_min, v3m_price=v3m_q)
                    else:
                        decay_ok, decay_th = _decay_ok(ab_decay_limit_a)
                        if not decay_ok:
                            _emit_gate("BLOCKED", "ab_a_momentum_decay", direction=direction, instrument=exec_symbol, mode=mode, tv=decay_th, v3m_price=v3m_q)
                        else:
                            t_vol_a = _a_vol_threshold()
                            if vol_surge_ratio < t_vol_a:
                                _emit_gate("BLOCKED", "ab_a_volume_too_low", direction=direction, instrument=exec_symbol, mode=mode, tv=t_vol_a, vol_surge_ratio=vol_surge_ratio, v3m_price=v3m_q)
                            else:
                                entry_path = "A"

            if entry_path is None:
                return
            if not trade_age_ok:
                _emit_gate("BLOCKED", "data_calc_only_qqq_stale", direction=direction, instrument=exec_symbol, mode=mode, data_age_qqq_sec=age_qqq, tv=trade_allow_age_qqq_sec)
                return
            mode = f"ELITE_AB_{entry_path}"
            self._ab_a_state = "WAIT_MARK"
            self._ab_a_armed_ts = None
            self._elite_overext_mark_ts = None
            self._elite_overext_mark_direction = None
        elif entry_model == "v44_elite":
            mode = "ELITE"
            if float(qqq_now) > float(vwap_now):
                direction = "LONG"
                exec_symbol = "TQQQ"
            elif float(qqq_now) < float(vwap_now):
                direction = "SHORT"
                exec_symbol = "SQQQ"
            else:
                _emit_gate("BLOCKED", "direction_mismatch", qqq_close_now=qqq_now, vwap_now=vwap_now)
                return

            elite_max_ext_mult = float(extras.get("elite_max_ext_mult", 1.30))
            elite_deadzone_mult = float(extras.get("elite_deadzone_mult", 0.15))
            elite_v3m_min_mult = float(extras.get("elite_v3m_min_mult", 0.35))
            elite_decay_limit = float(extras.get("elite_decay_limit", 0.95))
            elite_high_vol_ratio = float(extras.get("elite_high_vol_ratio", 1.5))
            elite_high_vol_samples = max(1, int(extras.get("elite_high_vol_confirm_samples", 2)))
            elite_vol_discount_mult = float(extras.get("elite_vol_discount_v3m_mult", 0.50))
            elite_vol_discount = float(extras.get("elite_vol_discount", 0.10))
            elite_enable_pullback_reentry = bool(extras.get("elite_enable_pullback_reentry", True))
            elite_reentry_window_sec = float(extras.get("elite_reentry_window_sec", 90.0))
            elite_reentry_ext_mult = float(extras.get("elite_reentry_ext_mult", 1.10))
            elite_ignore_qqew_resonance = bool(extras.get("elite_ignore_qqew_resonance", False))
            elite_persist_curr_min = float(extras.get("elite_persist_curr_min", 1.35))
            elite_persist_prev_min = float(extras.get("elite_persist_prev_min", 1.25))
            elite_burst_vol_min = float(extras.get("elite_burst_vol_min", 1.8))
            elite_burst_ext_mult = float(extras.get("elite_burst_ext_mult", 2.0))
            if elite_decouple_qqew:
                elite_ignore_qqew_resonance = True

            max_ext = elite_max_ext_mult * float(atr_eff)
            if prev_vol_surge_ratio is not None:
                if vol_surge_ratio > elite_burst_vol_min and prev_vol_surge_ratio > elite_burst_vol_min:
                    max_ext = max(max_ext, elite_burst_ext_mult * float(atr_eff))
                elif vol_surge_ratio >= elite_persist_curr_min and prev_vol_surge_ratio >= elite_persist_prev_min:
                    max_ext = max(max_ext, elite_max_ext_mult * float(atr_eff))

            if abs(float(qqq_now) - float(vwap_now)) > max_ext:
                self._elite_overext_mark_ts = now
                self._elite_overext_mark_direction = direction
                _emit_gate(
                    "BLOCKED",
                    "elite_overextended",
                    direction=direction,
                    instrument=exec_symbol,
                    mode=mode,
                    v3m_price=v3m_q,
                    v3m_signed=v3m_signed,
                    qqq_close_now=qqq_now,
                    vwap_now=vwap_now,
                    atr_live=atr_live,
                    atr_seed=atr_seed,
                    atr_eff=atr_eff,
                    atr_cap=atr_cap,
                    tv=max_ext,
                    vol_surge_ratio=vol_surge_ratio,
                )
                return
            if elite_enable_pullback_reentry and self._elite_overext_mark_ts is not None:
                same_direction = self._elite_overext_mark_direction == direction
                within_window = (now - self._elite_overext_mark_ts).total_seconds() <= elite_reentry_window_sec
                if same_direction and within_window:
                    reentry_ext = elite_reentry_ext_mult * float(atr_eff)
                    if abs(float(qqq_now) - float(vwap_now)) > reentry_ext:
                        _emit_gate(
                            "BLOCKED",
                            "elite_reentry_wait_pullback",
                            direction=direction,
                            instrument=exec_symbol,
                            mode=mode,
                            qqq_close_now=qqq_now,
                            vwap_now=vwap_now,
                            atr_eff=atr_eff,
                            tv=reentry_ext,
                        )
                        return
                elif not within_window:
                    self._elite_overext_mark_ts = None
                    self._elite_overext_mark_direction = None

            deadzone = elite_deadzone_mult * float(atr_eff)
            v3m_min = elite_v3m_min_mult * float(atr_eff)
            tv = v3m_min
            if direction == "LONG" and v3m_signed <= deadzone:
                _emit_gate("BLOCKED", "elite_sign_mismatch", direction=direction, instrument=exec_symbol, mode=mode, v3m_signed=v3m_signed, tv=deadzone)
                return
            if direction == "SHORT" and v3m_signed >= -deadzone:
                _emit_gate("BLOCKED", "elite_sign_mismatch", direction=direction, instrument=exec_symbol, mode=mode, v3m_signed=v3m_signed, tv=deadzone)
                return
            if v3m_q < v3m_min:
                _emit_gate("BLOCKED", "velocity_too_low", direction=direction, instrument=exec_symbol, mode=mode, v3m_price=v3m_q, v3m_signed=v3m_signed, tv=v3m_min)
                return

            now_ms = int(now.timestamp() * 1000)

            def _v3m_abs_at(offset_sec: int) -> Optional[float]:
                ts_ref = now_ms - max(0, int(offset_sec)) * 1000
                q_now_ref = self._qqq_value_at_or_before(ts_ref)
                q_ago_ref = self._qqq_value_at_or_before(ts_ref - 180_000)
                if q_now_ref is None or q_ago_ref is None:
                    return None
                return abs(float(q_now_ref - q_ago_ref))

            decay_prev: list[float] = []
            for offset in (1, 2, 3):
                val = _v3m_abs_at(offset)
                if val is not None:
                    decay_prev.append(float(val))
            if len(decay_prev) < 2:
                _emit_gate("BLOCKED", "elite_decay_window_insufficient", direction=direction, instrument=exec_symbol, mode=mode)
                return
            if v3m_q < (max(decay_prev) * elite_decay_limit):
                _emit_gate("BLOCKED", "elite_momentum_decay", direction=direction, instrument=exec_symbol, mode=mode, v3m_price=v3m_q, tv=max(decay_prev) * elite_decay_limit)
                return

            if now.time() < time(11, 30):
                t_vol = 1.35
            elif now.time() < time(14, 0):
                t_vol = 1.55
            else:
                t_vol = 1.45
            if v3m_q >= (elite_vol_discount_mult * float(atr_eff)):
                t_vol = max(1.0, t_vol - elite_vol_discount)
            if vol_surge_ratio < t_vol:
                _emit_gate("BLOCKED", "elite_volume_too_low", direction=direction, instrument=exec_symbol, mode=mode, vol_surge_ratio=vol_surge_ratio, tv=t_vol, v3m_price=v3m_q)
                return

            if not elite_ignore_qqew_resonance:
                if direction == "LONG" and not (float(qqew_now) > float(qqew_vwap_now)):
                    _emit_gate("BLOCKED", "direction_mismatch", direction=direction, instrument=exec_symbol, mode=mode, qqew_close_now=qqew_now, qqew_vwap_now=qqew_vwap_now)
                    return
                if direction == "SHORT" and not (float(qqew_now) < float(qqew_vwap_now)):
                    _emit_gate("BLOCKED", "direction_mismatch", direction=direction, instrument=exec_symbol, mode=mode, qqew_close_now=qqew_now, qqew_vwap_now=qqew_vwap_now)
                    return

            vol_ratio = float(atr_live) / max(float(atr_seed), eps)
            if vol_ratio > elite_high_vol_ratio:
                hv_signs: list[float] = []
                for offset in range(elite_high_vol_samples):
                    ts_ref = now_ms - offset * 1000
                    q_now_ref = self._qqq_value_at_or_before(ts_ref)
                    q_ago_ref = self._qqq_value_at_or_before(ts_ref - 180_000)
                    if q_now_ref is None or q_ago_ref is None:
                        _emit_gate("BLOCKED", "elite_high_vol_sample_missing", direction=direction, instrument=exec_symbol, mode=mode)
                        return
                    hv_signs.append(float(q_now_ref - q_ago_ref))
                if direction == "LONG" and not all(x > 0 for x in hv_signs):
                    _emit_gate("BLOCKED", "elite_high_vol_sign_not_confirmed", direction=direction, instrument=exec_symbol, mode=mode, v3m_signed=v3m_signed)
                    return
                if direction == "SHORT" and not all(x < 0 for x in hv_signs):
                    _emit_gate("BLOCKED", "elite_high_vol_sign_not_confirmed", direction=direction, instrument=exec_symbol, mode=mode, v3m_signed=v3m_signed)
                    return
        else:
            # Baseline gate path (legacy).
            if elite_decouple_qqew:
                long_dir = float(qqq_now) > float(vwap_now)
                short_dir = float(qqq_now) < float(vwap_now)
            else:
                long_dir = float(qqq_now) > float(vwap_now) and float(qqew_now) > float(qqew_vwap_now)
                short_dir = float(qqq_now) < float(vwap_now) and float(qqew_now) < float(qqew_vwap_now)
            if not (long_dir or short_dir):
                _emit_gate(
                    "BLOCKED",
                    "direction_mismatch",
                    qqq_3m_ago=qqq_ago,
                    qqew_3m_ago=qqew_ago,
                    v3m_price=v3m_q,
                    v3m_signed=v3m_signed,
                    qqew_vs_vwap=qqew_vs_vwap,
                    qqq_close_now=qqq_now,
                    qqew_close_now=qqew_now,
                    vwap_now=vwap_now,
                    qqew_vwap_now=qqew_vwap_now,
                    atr_live=atr_live,
                    atr_seed=atr_seed,
                    vol_t=vol_t,
                    vol_ma10=vol_ma10,
                    data_age_qqq_sec=age_qqq,
                    data_age_qqew_sec=age_qqew,
                )
                return
            direction = "LONG" if long_dir else "SHORT"
            exec_symbol = "TQQQ" if direction == "LONG" else "SQQQ"

            pulse = vol_surge_ratio >= vol_pulse_on
            k = k_pulse if pulse else k_normal
            mode = "PULSE" if pulse else "NORMAL"
            tv = max(k * atr_eff, tv_min_usd)
            if v3m_q < tv:
                _emit_gate(
                    "BLOCKED",
                    "velocity_too_low",
                    direction=direction,
                    instrument=exec_symbol,
                    mode="PULSE" if pulse else "NORMAL",
                    qqq_3m_ago=qqq_ago,
                    qqew_3m_ago=qqew_ago,
                    v3m_price=v3m_q,
                    v3m_signed=v3m_signed,
                    qqew_vs_vwap=qqew_vs_vwap,
                    qqq_close_now=qqq_now,
                    qqew_close_now=qqew_now,
                    vwap_now=vwap_now,
                    qqew_vwap_now=qqew_vwap_now,
                    atr_live=atr_live,
                    atr_seed=atr_seed,
                    atr_eff=atr_eff,
                    atr_cap=atr_cap,
                    k=k,
                    pulse=pulse,
                    tv=tv,
                    vol_t=vol_t,
                    vol_ma10=vol_ma10,
                    vol_surge_ratio=vol_surge_ratio,
                    data_age_qqq_sec=age_qqq,
                    data_age_qqew_sec=age_qqew,
                )
                return

            if high_vol_guard:
                vol_ratio = float(atr_live) / max(float(atr_seed), eps)
                if vol_ratio >= high_vol_ratio:
                    if vol_surge_ratio < hv_min_vol_surge:
                        _emit_gate(
                            "BLOCKED",
                            "high_vol_no_volume",
                            direction=direction,
                            instrument=exec_symbol,
                            mode="PULSE" if pulse else "NORMAL",
                            qqq_3m_ago=qqq_ago,
                            qqew_3m_ago=qqew_ago,
                            v3m_price=v3m_q,
                            v3m_signed=v3m_signed,
                            qqew_vs_vwap=qqew_vs_vwap,
                            qqq_close_now=qqq_now,
                            qqew_close_now=qqew_now,
                            vwap_now=vwap_now,
                            qqew_vwap_now=qqew_vwap_now,
                            atr_live=atr_live,
                            atr_seed=atr_seed,
                            atr_eff=atr_eff,
                            atr_cap=atr_cap,
                            k=k,
                            pulse=pulse,
                            vol_ratio=vol_ratio,
                            tv=tv,
                            vol_t=vol_t,
                            vol_ma10=vol_ma10,
                            vol_surge_ratio=vol_surge_ratio,
                            data_age_qqq_sec=age_qqq,
                            data_age_qqew_sec=age_qqew,
                        )
                        return
                    tv_hv = max(hv_k * atr_eff, hv_tv_min)
                    if v3m_q < tv_hv:
                        _emit_gate(
                            "BLOCKED",
                            "high_vol_protection",
                            direction=direction,
                            instrument=exec_symbol,
                            mode="PULSE" if pulse else "NORMAL",
                            qqq_3m_ago=qqq_ago,
                            qqew_3m_ago=qqew_ago,
                            v3m_price=v3m_q,
                            v3m_signed=v3m_signed,
                            qqew_vs_vwap=qqew_vs_vwap,
                            qqq_close_now=qqq_now,
                            qqew_close_now=qqew_now,
                            vwap_now=vwap_now,
                            qqew_vwap_now=qqew_vwap_now,
                            atr_live=atr_live,
                            atr_seed=atr_seed,
                            atr_eff=atr_eff,
                            atr_cap=atr_cap,
                            k=k,
                            pulse=pulse,
                            vol_ratio=vol_ratio,
                            tv=tv_hv,
                            vol_t=vol_t,
                            vol_ma10=vol_ma10,
                            vol_surge_ratio=vol_surge_ratio,
                            distance_from_vwap_atr=distance_from_vwap_atr,
                            data_age_qqq_sec=age_qqq,
                            data_age_qqew_sec=age_qqew,
                        )
                        return
                    tv = tv_hv

        # Entry gate: block extreme overextension entries.
        extreme_filter_enabled = bool(extras.get("entry_extreme_filter_enabled", False))
        extreme_dist_min = float(extras.get("entry_extreme_distance_from_vwap_atr_min", 8.1))
        extreme_v3m_signed_min = float(extras.get("entry_extreme_v3m_signed_min", 0.64))
        extreme_seed_n_min = int(extras.get("entry_extreme_seed_n_min", 700))
        if (
            extreme_filter_enabled
            and distance_from_vwap_atr > extreme_dist_min
            and float(v3m_signed) > extreme_v3m_signed_min
            and int(len(self._seed_velocity_samples)) > extreme_seed_n_min
        ):
            _emit_gate(
                "BLOCKED",
                "extreme_filter_block",
                direction=direction,
                instrument=exec_symbol,
                mode=mode,
                qqq_3m_ago=qqq_ago,
                qqew_3m_ago=qqew_ago,
                v3m_price=v3m_q,
                v3m_signed=v3m_signed,
                qqew_vs_vwap=qqew_vs_vwap,
                qqq_close_now=qqq_now,
                qqew_close_now=qqew_now,
                vwap_now=vwap_now,
                qqew_vwap_now=qqew_vwap_now,
                atr_live=atr_live,
                atr_seed=atr_seed,
                atr_eff=atr_eff,
                atr_cap=atr_cap,
                k=k,
                pulse=pulse,
                tv=extreme_dist_min,
                vol_t=vol_t,
                vol_ma10=vol_ma10,
                vol_surge_ratio=vol_surge_ratio,
                distance_from_vwap_atr=distance_from_vwap_atr,
                data_age_qqq_sec=age_qqq,
                data_age_qqew_sec=age_qqew,
            )
            return

        anti_chase_reason = self._anti_chase_reason(
            now=now,
            direction=direction,
            mode=mode,
            qqq_now=float(qqq_now),
            vwap_now=float(vwap_now),
            distance_from_vwap_atr=float(distance_from_vwap_atr),
            extras=extras,
        )
        if anti_chase_reason is not None:
            self._emit_local_audit(
                stage="entry_blocked_anti_chase",
                severity="warning",
                status="blocked",
                reason=str(anti_chase_reason["reason"]),
                symbol=exec_symbol,
                payload={
                    "direction": direction,
                    "mode": mode,
                    "rank15": anti_chase_reason.get("rank15"),
                    "distance_from_vwap_atr": anti_chase_reason.get("distance_from_vwap_atr"),
                    "projected_trade_seq": int(self._success_count) + 1,
                    "day_realized_pnl_before": float(self._day_realized_pnl),
                },
            )
            _emit_gate(
                "BLOCKED",
                str(anti_chase_reason["reason"]),
                direction=direction,
                instrument=exec_symbol,
                mode=mode,
                qqq_close_now=qqq_now,
                vwap_now=vwap_now,
                distance_from_vwap_atr=distance_from_vwap_atr,
                tv=anti_chase_reason.get("tv"),
                vol_ratio=anti_chase_reason.get("rank15"),
            )
            return

        r1_block_reason = self._r1_fourth_short_small_green_block_reason(direction=direction, extras=extras)
        if r1_block_reason is not None:
            self._emit_local_audit(
                stage="entry_blocked_r1_fourth_short_small_green",
                severity="warning",
                status="blocked",
                reason=str(r1_block_reason["reason"]),
                symbol=exec_symbol,
                payload={
                    "direction": direction,
                    "mode": mode,
                    "projected_trade_seq": r1_block_reason.get("projected_trade_seq"),
                    "day_realized_pnl_before": r1_block_reason.get("day_realized_pnl_before"),
                },
            )
            _emit_gate(
                "BLOCKED",
                str(r1_block_reason["reason"]),
                direction=direction,
                instrument=exec_symbol,
                mode=mode,
                tv=r1_block_reason.get("projected_trade_seq"),
                vol_ratio=r1_block_reason.get("day_realized_pnl_before"),
            )
            return

        entry_trigger_id, entry_decision_id, entry_trace_id = self._make_entry_audit_ids()
        signal_authority_payload: Dict[str, Any] = {}
        _emit_gate(
            "ENTRY_ALLOWED",
            "entry_allowed",
            direction=direction,
            instrument=exec_symbol,
            mode=mode,
            qqq_3m_ago=qqq_ago,
            qqew_3m_ago=qqew_ago,
            v3m_price=v3m_q,
            v3m_signed=v3m_signed,
            qqew_vs_vwap=qqew_vs_vwap,
            qqq_close_now=qqq_now,
            qqew_close_now=qqew_now,
            vwap_now=vwap_now,
            qqew_vwap_now=qqew_vwap_now,
            atr_live=atr_live,
            atr_seed=atr_seed,
            atr_eff=atr_eff,
            atr_cap=atr_cap,
            k=k,
            pulse=pulse,
            tv=tv,
            vol_t=vol_t,
            vol_ma10=vol_ma10,
            vol_surge_ratio=vol_surge_ratio,
            distance_from_vwap_atr=distance_from_vwap_atr,
            data_age_qqq_sec=age_qqq,
            data_age_qqew_sec=age_qqew,
            audit_decision_id=entry_decision_id,
            audit_trigger_id=entry_trigger_id,
            audit_trace_id=entry_trace_id,
        )
        try:
            signal_authority_payload = self._signal_authority.publish_entry_snapshot(
                strategy_id=str(self.strategy_id),
                decision_id=str(entry_decision_id or ""),
                trigger_id=str(entry_trigger_id or ""),
                trace_id=str(entry_trace_id or ""),
                ts=now,
                direction=str(direction),
                exec_symbol=str(exec_symbol),
                signal_state={
                    "mode": str(mode),
                    "v3m_signed": float(v3m_signed),
                    "atr_eff": float(atr_eff),
                    "atr_live": float(atr_live),
                    "atr_seed": float(atr_seed),
                    "atr_cap": float(atr_cap),
                    "vol_surge_ratio": float(vol_surge_ratio),
                    "distance_from_vwap_atr": float(distance_from_vwap_atr),
                    "data_age_qqq_sec": age_qqq,
                    "data_age_qqew_sec": age_qqew,
                    "qqq_now": float(qqq_now),
                    "vwap_now": float(vwap_now),
                    "qqew_now": float(qqew_now) if qqew_now is not None else None,
                    "qqew_vwap_now": float(qqew_vwap_now) if qqew_vwap_now is not None else None,
                    "entry_symbol": str(exec_symbol),
                    "entry_direction": str(direction),
                },
            )
            self._emit_local_audit(
                stage="qqq_signal_authority_snapshot",
                severity="info",
                status="published",
                reason="signal_authority_snapshot_published",
                symbol=exec_symbol,
                decision_id=entry_decision_id,
                trigger_id=entry_trigger_id,
                trace_id=entry_trace_id,
                payload=dict(signal_authority_payload),
            )
        except Exception as exc:
            logger.warning(
                "qqq_signal_authority_snapshot_failed",
                strategy_id=str(self.strategy_id),
                error=str(exc),
            )
        # Re-evaluate continuity at publication time instead of relying on an outer
        # local name, so the shared signal path cannot fail on a refactor/scoping bug.
        publish_continuity_ok = bool(
            self._has_continuous_180s(now, include_qqew=(not elite_decouple_qqew))
        )
        try:
            artifact, health = publish_qqq_shared_entry_signal(
                strategy_id=str(self.strategy_id),
                trigger_ts=now,
                direction=str(direction),
                instrument=str(exec_symbol),
                trigger_id=str(entry_trigger_id or ""),
                decision_id=str(entry_decision_id or ""),
                trace_id=str(entry_trace_id or ""),
                candidate=QQQSharedEntryCandidate(
                    mode=str(mode),
                    v3m_signed=float(v3m_signed),
                    atr_eff=float(atr_eff),
                    atr_live=float(atr_live),
                    atr_seed=float(atr_seed),
                    atr_cap=float(atr_cap),
                    vol_surge_ratio=float(vol_surge_ratio),
                    distance_from_vwap_atr=float(distance_from_vwap_atr),
                    data_age_qqq_sec=age_qqq,
                    data_age_qqew_sec=age_qqew,
                    continuity_180s_ok=publish_continuity_ok,
                    qqq_now=float(qqq_now),
                    vwap_now=float(vwap_now),
                    qqew_now=float(qqew_now) if qqew_now is not None else None,
                    qqew_vwap_now=float(qqew_vwap_now) if qqew_vwap_now is not None else None,
                ),
            )
            self._emit_local_audit(
                stage="stock_signal_publish_ok",
                severity="info",
                status="published",
                reason="stock_signal_plane_publish_ok",
                symbol=exec_symbol,
                decision_id=entry_decision_id,
                trigger_id=entry_trigger_id,
                trace_id=entry_trace_id,
                payload={
                    "signal_family": str(artifact.signal_family),
                    "source_strategy_id": str(artifact.strategy_id),
                    "direction": str(artifact.direction),
                    "option_side": str(artifact.option_side),
                    "trigger_ts": artifact.trigger_ts.isoformat(),
                    "signal_plane_backend_config": str(health.get("backend_config") or ""),
                    "signal_plane_state": str(health.get("state") or ""),
                    "signal_plane_using_redis": bool(health.get("using_redis")),
                    "signal_plane_redis_ok": bool(health.get("redis_ok")),
                    "signal_plane_error_streak": int(health.get("error_streak") or 0),
                    "signal_plane_retry_in_sec": float(health.get("retry_in_sec") or 0.0),
                    "signal_plane_last_write_ok": health.get("last_write_ok"),
                    "signal_plane_last_write_error": str(health.get("last_write_error") or ""),
                    "signal_plane_last_write_ts": str(health.get("last_write_ts") or ""),
                },
            )
        except Exception as exc:
            logger.warning(
                "qqq_stock_signal_publish_failed",
                strategy_id=str(self.strategy_id),
                error=str(exc),
            )
            self._emit_local_audit(
                stage="stock_signal_publish_failed",
                severity="error",
                status="failed",
                reason="stock_signal_plane_publish_failed",
                symbol=exec_symbol,
                decision_id=entry_decision_id,
                trigger_id=entry_trigger_id,
                trace_id=entry_trace_id,
                payload={
                    "error": str(exc),
                },
            )

        if self._enforce_stock_execution_blockers_enabled(extras):
            if await enforce_stock_execution_blockers(
                strategy=self,
                extras=extras,
                logger=logger,
            ):
                return
        else:
            blocker_payload = await self._collect_stock_execution_blocker_snapshot(extras)
            blocker_payload.update(
                {
                    "decision_id": str(entry_decision_id or ""),
                    "trigger_id": str(entry_trigger_id or ""),
                    "trace_id": str(entry_trace_id or ""),
                    "direction": str(direction),
                    "symbol": str(exec_symbol),
                }
            )
            self._append_diag_event("qqq_stock_execution_blockers_legacy_bypass", blocker_payload)
            self._emit_local_audit(
                stage="stock_execution_blockers_legacy_bypass",
                severity="warning" if bool(blocker_payload.get("would_block")) else "info",
                status="bypassed",
                reason="legacy_trader_parity_mode",
                symbol=exec_symbol,
                decision_id=entry_decision_id,
                trigger_id=entry_trigger_id,
                trace_id=entry_trace_id,
                payload=blocker_payload,
            )

        self._append_diag_event(
            "qqq_entry_minute_metrics",
            {
                "ts_et": now.strftime("%Y-%m-%d %H:%M:%S"),
                "strategy_id": self.strategy_id,
                "v3m_price": v3m_q,
                "qqew_vs_vwap": qqew_vs_vwap,
                "vol_surge_ratio": vol_surge_ratio,
            },
        )

        side = "LONG" if direction == "LONG" else "SHORT"
        cool_min = float((self.params.extras or {}).get("stop_reentry_cooldown_min", 0.0))
        if (
            cool_min > 0.0
            and
            self._last_stop_exit_at is not None
            and self._last_stop_exit_side == side
            and (now - self._last_stop_exit_at) < timedelta(minutes=max(0.0, cool_min))
        ):
            self._emit_local_audit(
                stage="entry_blocked_reentry_cooldown",
                severity="warning",
                status="blocked",
                reason="stop_reentry_cooldown",
                symbol=exec_symbol,
                decision_id=entry_decision_id,
                trigger_id=entry_trigger_id,
                trace_id=entry_trace_id,
                payload={
                    "side": side,
                    "cooldown_min": float(cool_min),
                    "last_stop_exit_at": self._last_stop_exit_at.isoformat() if self._last_stop_exit_at else None,
                },
            )
            return
        
        minute_ts = _floor_minute(now)
        signal_bar_close = self._exec_minute_close_for_gate(exec_symbol, minute_ts, now)
        if signal_bar_close is None or signal_bar_close <= 0:
            self._emit_local_audit(
                stage="entry_blocked_signal_bar_unavailable",
                severity="warning",
                status="blocked",
                reason="entry_wait_signal_bar_close",
                symbol=exec_symbol,
                decision_id=entry_decision_id,
                trigger_id=entry_trigger_id,
                trace_id=entry_trace_id,
                payload={"minute_ts_et": minute_ts.isoformat()},
            )
            _emit_gate("BLOCKED", "entry_wait_signal_bar_close", direction=direction, instrument=exec_symbol, mode=mode)
            return
        last_exec = float(signal_bar_close)
        if last_exec <= 0:
            logger.warning("qqq_entry_aborted_bad_price", symbol=exec_symbol, price=last_exec)
            return

        # ATR for stop calculation must be available at entry
        atr_p = int((self.params.extras or {}).get("atr_period", 14))
        atr = self._effective_atr(exec_symbol, atr_p, now=now)
        if atr is None or atr <= 0:
            self._emit_local_audit(
                stage="entry_blocked_atr_unavailable",
                severity="warning",
                status="blocked",
                reason="atr_unavailable_at_entry",
                symbol=exec_symbol,
                decision_id=entry_decision_id,
                trigger_id=entry_trigger_id,
                trace_id=entry_trace_id,
                payload={"period": int(atr_p)},
            )
            self._fatal("atr_unavailable_at_entry", symbol=exec_symbol, period=atr_p)
            return

        qty_total = self._calc_qty(last_exec)
        if qty_total <= 0:
            self._emit_local_audit(
                stage="entry_blocked_zero_qty",
                severity="warning",
                status="blocked",
                reason="qty_total_non_positive",
                symbol=exec_symbol,
                decision_id=entry_decision_id,
                trigger_id=entry_trigger_id,
                trace_id=entry_trace_id,
                payload={"last_exec": float(last_exec), "qty_total": int(qty_total)},
            )
            return
        probe_ratio = float((self.params.extras or {}).get("probe_ratio", 0.70))
        probe_ratio = min(1.0, max(0.0, probe_ratio))
        probe_qty = int(math.floor(float(qty_total) * probe_ratio))
        if probe_qty <= 0:
            probe_qty = qty_total
        add_qty = max(0, int(qty_total - probe_qty))

        extras = self.params.extras or {}
        max_day_gross_notional_usd = float(
            extras.get(
                "max_day_gross_notional_usd",
                extras.get("notional_usd", self.params.per_symbol_max_usd),
            )
        )
        try:
            qty_t_live = abs(await self._current_exec_qty_for_symbol("TQQQ"))
            qty_s_live = abs(await self._current_exec_qty_for_symbol("SQQQ"))
            px_t = float(self._last_price("TQQQ") or 0.0)
            px_s = float(self._last_price("SQQQ") or 0.0)
            existing_gross_notional = (qty_t_live * px_t) + (qty_s_live * px_s)
            planned_notional = float(probe_qty) * float(last_exec)
            if (existing_gross_notional + planned_notional) > (max_day_gross_notional_usd + 1e-6):
                self._emit_local_audit(
                    stage="entry_blocked_day_gross_notional_cap",
                    severity="warning",
                    status="blocked",
                    reason="day_gross_notional_cap",
                    symbol=exec_symbol,
                    decision_id=entry_decision_id,
                    trigger_id=entry_trigger_id,
                    trace_id=entry_trace_id,
                    payload={
                        "cap": float(max_day_gross_notional_usd),
                        "existing_gross_notional": float(existing_gross_notional),
                        "planned_notional": float(planned_notional),
                    },
                )
                logger.warning(
                    "qqq_entry_blocked_by_day_gross_notional_cap",
                    strategy_id=self.strategy_id,
                    cap=max_day_gross_notional_usd,
                    existing_gross_notional=round(existing_gross_notional, 4),
                    planned_notional=round(planned_notional, 4),
                    symbol=exec_symbol,
                )
                return
        except Exception as exc:
            logger.warning("qqq_day_notional_cap_check_failed", strategy_id=self.strategy_id, error=str(exc))

        self._emit_local_audit(
            stage="entry_submit_ready",
            status="ready",
            reason="entry_prechecks_passed",
            symbol=exec_symbol,
            decision_id=entry_decision_id,
            trigger_id=entry_trigger_id,
            trace_id=entry_trace_id,
            payload={
                "side": side,
                "qty_total": int(qty_total),
                "probe_qty": int(probe_qty),
                "add_qty": int(add_qty),
                "last_exec": float(last_exec),
                "atr": float(atr),
            },
        )
        try:
            submit_ok = await self._submit_target(
                exec_symbol,
                probe_qty,
                reason="qqq_intraday_entry",
                kind="ENTRY",
                trigger_id=entry_trigger_id,
                decision_id_override=entry_decision_id,
                trace_id_override=entry_trace_id,
                ref_px=float(last_exec),
                ref_px_source="sfs_signal_close",
            )
        except TypeError:
            submit_ok = await self._submit_target(exec_symbol, probe_qty)
        if not submit_ok:
            return

        self._is_traded = True
        self._side = side
        self._exec_symbol = exec_symbol
        self._entry_px = float(last_exec)
        self._entry_time_et = now
        self._breakeven_done = False

        stop_atr_mult = self._hard_stop_atr_mult(self.params.extras or {})
        stop_min_abs = float((self.params.extras or {}).get("stop_min_abs", 0.50))
        dist = max(stop_atr_mult * atr, stop_min_abs)
        self._stop_dist_pre = float(dist)
        self._stop_dist_max = float(dist) * 1.5
        self._stop_dist_cur = float(dist)
        # SPEC RULE 3: Stop is ALWAYS subtraction (Entry - Dist) even for SQQQ (treat as LONG)
        self._stop_px = round(self._entry_px - dist, 2)
        self._locked_velocity_sign = +1 if side == "LONG" else -1
        self._entry_atr_used = float(atr)
        atr_lock = float(atr)
        self._trade_target_px = round(self._entry_px + (1.0 * atr_lock), 2)
        self._tp1_hit = False
        self._trail_high_px = None
        self._trail_floor_px = round(self._entry_px + (0.2 * atr_lock), 2)
        self._planned_total_qty = int(qty_total)
        self._planned_probe_qty = int(probe_qty)
        self._planned_add_qty = int(add_qty)
        self._add_done = add_qty <= 0
        self._last_add_check_minute = None
        self._add_deadline_et = (now + timedelta(minutes=2))
        self._elite_overext_mark_ts = None
        self._elite_overext_mark_direction = None

        logger.info(
            "qqq_entered",
            strategy_id=self.strategy_id,
            side=side,
            exec_symbol=exec_symbol,
            qty=probe_qty,
            entry_px=round(self._entry_px, 4),
            stop_px=round(self._stop_px or 0.0, 4),
            target_px=round(self._trade_target_px or 0.0, 4),
            atr=round(float(atr), 4),
            velocity=round(v3m_q, 2),
            qqq_vwap=round(float(vwap_now), 4),
            openclose_base=round(float(self._price_base), 4) if self._price_base is not None else None,
            openclose_base_source=self._price_base_source,
            tv=round(float(tv), 4),
            mode=mode,
        )
        self._pending_breakout = None
        self._persist_runtime_state(reason="entered")
        await self._submit_protective_stop_intent("entry_protective_stop")
        if not self._stop_audit_started and self._running:
            self._stop_audit_task = asyncio.create_task(self._audit_stop_order_loop(), name=f"qqq_stop_audit:{self.strategy_id}")
            self._stop_audit_started = True

    def _calc_qty(self, last_price: float) -> int:
        extras = self.params.extras or {}
        explicit_qty = extras.get("qty")
        if explicit_qty is not None:
            try:
                q = int(explicit_qty)
                return max(0, q)
            except Exception:
                pass
        notional = float(extras.get("notional_usd", self.params.per_symbol_max_usd))
        cap = float(getattr(self.params, "per_symbol_max_usd", notional) or notional)
        notional = min(notional, cap)
        if last_price <= 0:
            return 0
        return max(0, int(math.floor(notional / last_price)))

    async def _current_exec_qty_for_symbol(self, symbol: str) -> float:
        symbol_u = str(symbol or "").strip().upper()
        extras = self.params.extras or {}
        mismatch_warn_interval_sec = max(5.0, float(extras.get("position_mismatch_warn_interval_sec", 15.0)))

        def _should_warn_mismatch() -> bool:
            now_mono = asyncio.get_running_loop().time()
            last = float(self._last_ctx_broker_mismatch_warn_mono_by_symbol.get(symbol_u, 0.0) or 0.0)
            if (now_mono - last) < mismatch_warn_interval_sec:
                return False
            self._last_ctx_broker_mismatch_warn_mono_by_symbol[symbol_u] = now_mono
            return True

        try:
            ctx_timed_out = False
            try:
                pos = await self._ctx_position_with_timeout(symbol)
            except RuntimeError as exc:
                if str(exc).startswith(f"ctx_position_timeout:{symbol_u}:"):
                    pos = None
                    ctx_timed_out = True
                else:
                    raise
            if pos is None:
                # Strategy ledger can be stale/missing (restart window). Fall back to broker inventory
                # for safety/continuity; downstream logic still avoids "hijacking" inventory when ctx is flat.
                qty_broker = await self._broker_position_qty(symbol)
                if qty_broker is None:
                    logger.warning(
                        "qqq_ctx_position_missing_assume_flat",
                        strategy_id=self.strategy_id,
                        symbol=symbol_u,
                        ctx_timed_out=bool(ctx_timed_out),
                    )
                    return 0.0
                logger.warning(
                    "qqq_ctx_position_missing_using_broker_qty",
                    strategy_id=self.strategy_id,
                    symbol=symbol_u,
                    qty_broker=round(float(qty_broker), 6),
                    ctx_timed_out=bool(ctx_timed_out),
                )
                return float(qty_broker)
            val = getattr(pos, "pos_qty", None)
            if val is None:
                val = getattr(pos, "qty", None)
            if val is None:
                logger.warning(
                    "qqq_ctx_position_qty_missing_assume_flat",
                    strategy_id=self.strategy_id,
                    symbol=symbol_u,
                )
                return 0.0
            qty_ctx = float(val)
            qty_broker = await self._broker_position_qty(symbol)
            if qty_broker is None:
                if abs(qty_ctx) > 1e-6:
                    self._last_nonzero_ctx_qty_by_symbol[symbol_u] = float(qty_ctx)
                return float(qty_ctx)
            if abs(qty_ctx) > 0:
                self._last_nonzero_ctx_qty_by_symbol[symbol_u] = float(qty_ctx)
                mismatch_abs = max(0.5, float(extras.get("position_qty_mismatch_abs", 0.5)))
                if abs(qty_broker) <= 1e-6:
                    if _should_warn_mismatch():
                        logger.warning(
                            "qqq_position_ctx_broker_mismatch_using_broker",
                            strategy_id=self.strategy_id,
                            symbol=symbol,
                            qty_ctx=round(float(qty_ctx), 6),
                            qty_broker=round(float(qty_broker), 6),
                        )
                    return float(qty_broker)
                if (qty_ctx > 0 > qty_broker) or (qty_ctx < 0 < qty_broker):
                    if _should_warn_mismatch():
                        logger.warning(
                            "qqq_position_ctx_broker_sign_mismatch_using_broker",
                            strategy_id=self.strategy_id,
                            symbol=symbol,
                            qty_ctx=round(float(qty_ctx), 6),
                            qty_broker=round(float(qty_broker), 6),
                        )
                    return float(qty_broker)
                if abs(float(qty_ctx) - float(qty_broker)) > mismatch_abs:
                    if _should_warn_mismatch():
                        logger.warning(
                            "qqq_position_ctx_broker_qty_mismatch_using_broker",
                            strategy_id=self.strategy_id,
                            symbol=symbol,
                            qty_ctx=round(float(qty_ctx), 6),
                            qty_broker=round(float(qty_broker), 6),
                            mismatch_abs=mismatch_abs,
                        )
                    return float(qty_broker)
                return float(qty_ctx)
            return float(self._resolve_ctx_missing_qty(symbol_u, qty_ctx=float(qty_ctx), qty_broker=float(qty_broker)))
        except Exception as exc:
            logger.warning(
                "qqq_ctx_position_fetch_failed_assume_flat",
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                error=str(exc),
            )
            return 0.0

    async def _ctx_position_with_timeout(self, symbol: str) -> Any:
        symbol_u = str(symbol or "").strip().upper()
        extras = self.params.extras or {}
        timeout_sec = max(0.2, float(extras.get("ctx_position_timeout_sec", 2.0)))
        try:
            result = self.ctx.position(symbol)
            if inspect.isawaitable(result):
                return await asyncio.wait_for(result, timeout=timeout_sec)
            return result
        except asyncio.TimeoutError as exc:
            logger.warning(
                "qqq_ctx_position_timeout",
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                timeout_sec=round(float(timeout_sec), 3),
            )
            self._emit_local_audit(
                stage="ctx_position_timeout",
                severity="warning",
                status="timeout",
                symbol=symbol_u,
                reason="ctx_position_timeout",
                payload={"timeout_sec": round(float(timeout_sec), 3)},
            )
            raise RuntimeError(f"ctx_position_timeout:{symbol_u}:{timeout_sec}") from exc

    async def _ctx_open_orders_with_timeout(self) -> list[Any]:
        extras = self.params.extras or {}
        timeout_sec = max(0.2, float(extras.get("ctx_open_orders_timeout_sec", 2.0)))
        try:
            result = self.ctx.open_orders()
            if inspect.isawaitable(result):
                try:
                    result = await asyncio.wait_for(result, timeout=timeout_sec)
                except asyncio.TimeoutError as exc:
                    if self._legacy_trader_parity_mode(extras):
                        logger.warning(
                            "qqq_ctx_open_orders_timeout_legacy_fallback",
                            strategy_id=self.strategy_id,
                            timeout_sec=round(float(timeout_sec), 3),
                        )
                        self._emit_local_audit(
                            stage="ctx_open_orders_timeout_legacy_fallback",
                            severity="warning",
                            status="fallback",
                            reason="legacy_trader_parity_mode",
                            payload={"timeout_sec": round(float(timeout_sec), 3)},
                        )
                        retry_result = self.ctx.open_orders()
                        if inspect.isawaitable(retry_result):
                            retry_result = await retry_result
                        return list(retry_result or [])
                    raise exc
            return list(result or [])
        except asyncio.TimeoutError as exc:
            logger.warning(
                "qqq_ctx_open_orders_timeout",
                strategy_id=self.strategy_id,
                timeout_sec=round(float(timeout_sec), 3),
            )
            self._emit_local_audit(
                stage="ctx_open_orders_timeout",
                severity="warning",
                status="timeout",
                reason="ctx_open_orders_timeout",
                payload={"timeout_sec": round(float(timeout_sec), 3)},
            )
            raise RuntimeError(f"ctx_open_orders_timeout:{timeout_sec}") from exc

    async def _ib_positions_with_timeout(self, *, source: str) -> Optional[list[Any]]:
        extras = self.params.extras or {}
        timeout_sec = max(0.2, float(extras.get("broker_positions_snapshot_timeout_sec", 2.0)))
        try:
            engine = getattr(self.ctx, "engine", None)
            market_data_service = getattr(engine, "market_data_service", None)
            ib_adapter = getattr(market_data_service, "ib_adapter", None)
            ib = getattr(ib_adapter, "ib", None)
            positions_fn = getattr(ib, "positions", None)
            if not callable(positions_fn):
                return None
            try:
                raw_positions = await asyncio.wait_for(asyncio.to_thread(positions_fn), timeout=timeout_sec)
            except asyncio.TimeoutError:
                if self._legacy_trader_parity_mode(extras):
                    logger.warning(
                        "qqq_ib_positions_snapshot_timeout_legacy_fallback",
                        strategy_id=self.strategy_id,
                        source=str(source),
                        timeout_sec=round(float(timeout_sec), 3),
                    )
                    self._emit_local_audit(
                        stage="ib_positions_snapshot_timeout_legacy_fallback",
                        severity="warning",
                        status="fallback",
                        reason="legacy_trader_parity_mode",
                        payload={
                            "source": str(source),
                            "timeout_sec": round(float(timeout_sec), 3),
                        },
                    )
                    raw_positions = await asyncio.to_thread(positions_fn)
                else:
                    raise
            if isinstance(raw_positions, (list, tuple)):
                return list(raw_positions)
            return None
        except asyncio.TimeoutError:
            logger.warning(
                "qqq_ib_positions_snapshot_timeout",
                strategy_id=self.strategy_id,
                source=str(source),
                timeout_sec=round(float(timeout_sec), 3),
            )
            self._emit_local_audit(
                stage="ib_positions_snapshot_timeout",
                severity="warning",
                status="timeout",
                reason="ib_positions_snapshot_timeout",
                payload={
                    "source": str(source),
                    "timeout_sec": round(float(timeout_sec), 3),
                },
            )
            return None
        except Exception as exc:
            logger.warning(
                "qqq_ib_positions_snapshot_failed",
                strategy_id=self.strategy_id,
                source=str(source),
                error=str(exc),
            )
            return None

    async def _submit_intent_with_timeout(self, envelope: DecisionEnvelope, *, timeout_key: str) -> Any:
        if self._system_submit_blocked(timeout_key):
            symbol = str(getattr(envelope, "symbol", "") or "").strip().upper()
            self._audit_system_submit_blocked(
                stage="submit_intent_blocked_system_health",
                reason=timeout_key,
                symbol=symbol,
                decision_id=str(getattr(envelope, "decision_id", "") or ""),
            )
            raise RuntimeError(f"system_health_paused_submit_blocked:{symbol}:{timeout_key}")
        extras = self.params.extras or {}
        timeout_sec = max(0.2, float(extras.get(timeout_key, 2.0)))
        try:
            return await asyncio.wait_for(self.ctx.submit_intent(envelope), timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            symbol = str(getattr(envelope, "symbol", "") or "").strip().upper()
            logger.warning(
                "qqq_submit_intent_timeout",
                strategy_id=self.strategy_id,
                symbol=symbol,
                timeout_sec=round(float(timeout_sec), 3),
                timeout_key=timeout_key,
                order_ref=str((getattr(envelope, "extras", {}) or {}).get("order_ref") or ""),
            )
            self._emit_local_audit(
                stage="submit_intent_timeout",
                severity="warning",
                status="timeout",
                symbol=symbol,
                reason="submit_intent_timeout",
                decision_id=str(getattr(envelope, "decision_id", "") or "") or None,
                trigger_id=(getattr(envelope, "extras", {}) or {}).get("trigger_id"),
                trace_id=str(getattr(envelope, "decision_id", "") or "") or None,
                payload={
                    "timeout_sec": round(float(timeout_sec), 3),
                    "timeout_key": str(timeout_key),
                    "order_ref": str((getattr(envelope, "extras", {}) or {}).get("order_ref") or ""),
                    "order_style": str(getattr(getattr(envelope, "order_style", None), "kind", "") or ""),
                },
            )
            raise RuntimeError(f"submit_intent_timeout:{symbol}:{timeout_key}:{timeout_sec}") from exc

    async def _submit_protective_stop_without_cancelling_pipeline(self, envelope: DecisionEnvelope) -> Any:
        """
        Submit a protective stop without allowing a strategy-side timeout to cancel
        the execution pipeline after broker submission has started.

        A timeout here is an uncertain state, not a failed order. The background
        submit task remains shielded so broker ack/db persistence can still finish.
        """
        symbol = str(getattr(envelope, "symbol", "") or "").strip().upper()
        extras = self.params.extras or {}
        hard_timeout_sec = float(extras.get("protective_stop_submit_hard_timeout_sec", 30.0) or 30.0)
        if hard_timeout_sec <= 0.0:
            return await self.ctx.submit_intent(envelope)
        task = asyncio.create_task(
            self.ctx.submit_intent(envelope),
            name=f"qqq_protective_stop_submit:{self.strategy_id}:{symbol}",
        )
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=max(1.0, hard_timeout_sec))
        except asyncio.TimeoutError as exc:
            legacy_timeout = extras.get("protective_stop_submit_timeout_sec", 2.0)
            self._emit_local_audit(
                stage="protective_stop_submit_uncertain",
                severity="warning",
                status="uncertain",
                symbol=symbol,
                reason="submit_pipeline_still_running",
                decision_id=str(getattr(envelope, "decision_id", "") or "") or None,
                trigger_id=(getattr(envelope, "extras", {}) or {}).get("trigger_id"),
                trace_id=str(getattr(envelope, "decision_id", "") or "") or None,
                payload={
                    "hard_timeout_sec": round(float(hard_timeout_sec), 3),
                    "legacy_timeout_sec": legacy_timeout,
                    "order_ref": str((getattr(envelope, "extras", {}) or {}).get("order_ref") or ""),
                    "order_style": str(getattr(getattr(envelope, "order_style", None), "kind", "") or ""),
                    "pipeline_cancelled": False,
                },
            )
            raise RuntimeError(f"protective_stop_submit_uncertain:{symbol}:{hard_timeout_sec}") from exc

    @staticmethod
    def _is_mock_object(obj: Any) -> bool:
        return str(getattr(type(obj), "__module__", "") or "").startswith("unittest.mock")

    def _resolve_pg_client_for_order_cleanup(self) -> Any:
        engine = getattr(self.ctx, "engine", None)
        candidates = [
            getattr(engine, "pg", None),
            getattr(getattr(engine, "execution_service", None), "pg", None),
            getattr(getattr(engine, "decision_pipeline", None), "pg", None),
            getattr(getattr(getattr(engine, "decision_pipeline", None), "execution_service", None), "pg", None),
        ]
        for candidate in candidates:
            if candidate is None or self._is_mock_object(candidate):
                continue
            if callable(getattr(candidate, "fetchone", None)) and callable(getattr(candidate, "execute", None)):
                return candidate
        return None

    async def _mark_unsubmitted_timeout_order_terminal(
        self,
        envelope: DecisionEnvelope,
        *,
        timeout_key: str,
        timeout_error: str,
    ) -> bool:
        """
        Narrow cleanup for submit-intent timeouts.

        The execution service persists core_order=NEW before broker submit. If the
        strategy-side wait_for times out after that insert but before broker ack,
        the DB row can remain NEW forever. Only close the row when it is still
        unsubmitted and no live broker/open order with the same order_ref is visible.
        """
        extras = dict((getattr(envelope, "extras", {}) or {}))
        order_ref = str(extras.get("order_ref") or "").strip()
        symbol = str(getattr(envelope, "symbol", "") or "").strip().upper()
        if not order_ref or not symbol:
            return False

        open_orders: Optional[list[Any]] = None
        open_orders_fn = self._resolve_broker_open_orders_fn()
        try:
            if open_orders_fn is not None:
                result = await open_orders_fn()
                open_orders = list(result or [])
            else:
                open_orders = await self._ctx_open_orders_with_timeout()
        except Exception as exc:
            logger.warning(
                "qqq_submit_timeout_cleanup_open_order_check_failed",
                strategy_id=self.strategy_id,
                symbol=symbol,
                order_ref=order_ref,
                error=str(exc),
            )
            return False
        if open_orders is None:
            return False
        for order in open_orders:
            if self._order_ref(order) != order_ref:
                continue
            if self._is_terminal_order_state(order):
                continue
            if self._cancel_id_from_open_order(order) is not None:
                self._emit_local_audit(
                    stage="submit_timeout_cleanup_skipped",
                    severity="warning",
                    status="skipped",
                    reason="matching_live_order_visible",
                    symbol=symbol,
                    decision_id=str(getattr(envelope, "decision_id", "") or "") or None,
                    trigger_id=extras.get("trigger_id"),
                    trace_id=str(getattr(envelope, "decision_id", "") or "") or None,
                    payload={"order_ref": order_ref, "timeout_key": timeout_key},
                )
                return False

        pg = self._resolve_pg_client_for_order_cleanup()
        if pg is None:
            return False

        row = await pg.fetchone(
            """
            SELECT order_id::text AS order_id, note
            FROM core.core_order
            WHERE strategy_id = $1
              AND symbol = $2
              AND state = 'NEW'
              AND broker_order_id IS NULL
              AND submitted_at IS NULL
              AND position($3 in note) > 0
            ORDER BY created_at DESC
            LIMIT 1
            """,
            self.strategy_id,
            symbol,
            order_ref,
        )
        if not row:
            return False

        note_obj: Dict[str, Any] = {}
        try:
            raw_note = row["note"]
        except Exception:
            raw_note = getattr(row, "note", None)
        if isinstance(raw_note, str) and raw_note.strip().startswith("{"):
            try:
                parsed = json.loads(raw_note)
                if isinstance(parsed, dict):
                    note_obj = parsed
            except Exception:
                note_obj = {}
        note_obj["timeout_cleanup"] = {
            "reason": "submit_intent_timeout_unsubmitted_order",
            "timeout_key": str(timeout_key),
            "timeout_error": str(timeout_error),
            "order_ref": order_ref,
            "cleaned_at": datetime.now(ET).isoformat(),
        }
        try:
            order_id = row["order_id"]
        except Exception:
            order_id = getattr(row, "order_id", None)
        result = await pg.execute(
            """
            UPDATE core.core_order
            SET state = 'REJECTED',
                order_ref = COALESCE(NULLIF(order_ref, ''), $1),
                note = $2,
                updated_at = now()
            WHERE order_id = $3::uuid
              AND state = 'NEW'
              AND broker_order_id IS NULL
              AND submitted_at IS NULL
            """,
            order_ref,
            json.dumps(note_obj, ensure_ascii=False),
            str(order_id),
        )
        self._emit_local_audit(
            stage="submit_timeout_unsubmitted_order_cleaned",
            severity="warning",
            status="rejected",
            reason="submit_intent_timeout_unsubmitted_order",
            symbol=symbol,
            decision_id=str(getattr(envelope, "decision_id", "") or "") or None,
            trigger_id=extras.get("trigger_id"),
            trace_id=str(getattr(envelope, "decision_id", "") or "") or None,
            payload={"order_ref": order_ref, "order_id": str(order_id), "db_result": str(result)},
        )
        return True

    def _resolve_ctx_missing_qty(self, symbol: str, *, qty_ctx: float, qty_broker: float) -> float:
        """
        Resolve strategy-local quantity when DB/ctx is flat but broker shows inventory.
        In multi-strategy same-symbol setups, strategy execution must not claim broker net inventory.
        """
        symbol_u = str(symbol or "").strip().upper()
        if abs(qty_ctx) > 1e-6:
            self._last_nonzero_ctx_qty_by_symbol[symbol_u] = float(qty_ctx)
            return float(qty_ctx)
        if abs(qty_broker) <= 1e-6:
            return 0.0
        # Safety: if broker shows a short position while ctx is flat, preserve it so downstream
        # close-only protection does not accidentally open/extend risk due to stale ctx.
        if float(qty_broker) < 0:
            return float(qty_broker)

        # If this strategy believes it owns an open position for this symbol but ctx is stale/flat,
        # clamp to the strategy's last known claim instead of "hijacking" the full broker net qty.
        claim_qty = 0.0
        try:
            if bool(getattr(self, "_is_traded", False)) and str(getattr(self, "_exec_symbol", "") or "").strip().upper() == symbol_u:
                claim_qty = float(max(float(getattr(self, "_planned_total_qty", 0.0) or 0.0), float(getattr(self, "_expected_entry_qty", 0.0) or 0.0)))
        except Exception:
            claim_qty = 0.0
        if claim_qty > 0:
            return float(min(float(qty_broker), claim_qty))

        extras = self.params.extras or {}
        warn_interval = max(5.0, float(extras.get("position_ctx_missing_warn_interval_sec", 30.0)))
        now_mono = asyncio.get_running_loop().time()
        last = float(self._last_ctx_missing_warn_mono_by_symbol.get(symbol_u, 0.0) or 0.0)
        if (now_mono - last) >= warn_interval:
            self._last_ctx_missing_warn_mono_by_symbol[symbol_u] = now_mono
            logger.warning(
                "qqq_ctx_flat_broker_nonzero_ownership_blocked",
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                qty_ctx=round(float(qty_ctx), 6),
                qty_broker=round(float(qty_broker), 6),
            )
        return 0.0

    async def _broker_position_qty(self, symbol: str) -> Optional[float]:
        snap = await self._broker_position_snapshot(symbol)
        if snap is None:
            return None
        return float(snap[0])

    async def _broker_position_snapshot(self, symbol: str) -> Optional[tuple[float, Optional[float]]]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        await self._refresh_ib_positions_best_effort(force=False)
        try:
            engine = getattr(self.ctx, "engine", None)
            market_data_service = getattr(engine, "market_data_service", None)
            ib_adapter = getattr(market_data_service, "ib_adapter", None)
            ib = getattr(ib_adapter, "ib", None)
            if ib and hasattr(ib, "positions"):
                raw_positions = await self._ib_positions_with_timeout(source=f"snapshot_{sym}")
                if raw_positions is None:
                    return None
                qty = 0.0
                avg_cost_notional = 0.0
                account = str(getattr(ib_adapter, "account", "") or "")
                for p in raw_positions:
                    try:
                        if account and getattr(p, "account", "") and getattr(p, "account", "") != account:
                            continue
                        contract = getattr(p, "contract", None)
                        if not contract:
                            continue
                        if str(getattr(contract, "symbol", "") or "").strip().upper() != sym:
                            continue
                        p_qty = float(getattr(p, "position", 0.0) or 0.0)
                        p_avg = float(getattr(p, "avgCost", 0.0) or 0.0)
                        qty += p_qty
                        if abs(p_qty) > 0 and p_avg > 0:
                            avg_cost_notional += abs(p_qty) * p_avg
                    except Exception:
                        continue
                avg_cost = (avg_cost_notional / max(abs(qty), 1e-12)) if abs(qty) > 0 else None
                return float(qty), (float(avg_cost) if avg_cost and avg_cost > 0 else None)
        except Exception:
            pass
        return None

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

    def _resolve_broker_open_orders_fn(self):
        engine = getattr(self.ctx, "engine", None)
        candidates = [
            getattr(getattr(getattr(engine, "execution_service", None), "kernel", None), "broker", None),
            getattr(engine, "broker_adapter", None),
            getattr(getattr(engine, "market_data_service", None), "ib_adapter", None),
        ]
        for adapter in candidates:
            open_orders_fn = getattr(adapter, "open_orders", None)
            if not callable(open_orders_fn):
                continue
            fn_mod = str(type(open_orders_fn).__module__ or "")
            if fn_mod.startswith("unittest.mock") and type(open_orders_fn).__name__ != "AsyncMock":
                continue

            async def _open_orders(_fn=open_orders_fn):
                result = _fn()
                if inspect.isawaitable(result):
                    return await result
                return result

            return _open_orders
        return None

    @staticmethod
    def _normalize_order_state(order: Any) -> str:
        if isinstance(order, dict):
            raw = order.get("state")
        else:
            raw = getattr(order, "state", None)
        if raw is None:
            raw = getattr(getattr(order, "orderStatus", None), "status", None)
        state = str(raw or "")
        return state.strip().replace(" ", "").replace("_", "").upper()

    @classmethod
    def _is_terminal_order_state(cls, order: Any) -> bool:
        return cls._normalize_order_state(order) in {
            "FILLED",
            "CANCELLED",
            "CANCELED",
            "REJECTED",
            "INACTIVE",
            "APICANCELLED",
        }

    @staticmethod
    def _order_symbol(order: Any) -> str:
        symbol_raw = order.get("symbol") if isinstance(order, dict) else getattr(order, "symbol", "")
        symbol = str(symbol_raw or "").strip().upper()
        if symbol:
            return symbol
        contract = getattr(order, "contract", None)
        if contract is not None:
            symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
            if symbol:
                return symbol
        return ""

    @staticmethod
    def _order_type(order: Any) -> str:
        order_type = str(getattr(order, "order_type", "") or "").strip().upper()
        if order_type:
            return order_type
        ib_order = getattr(order, "order", None)
        if ib_order is not None:
            order_type = str(getattr(ib_order, "orderType", "") or "").strip().upper()
            if order_type:
                return order_type
        return ""

    @staticmethod
    def _order_ref(order: Any) -> str:
        ref = str(getattr(order, "order_ref", "") or "").strip()
        if ref:
            return ref
        ib_order = getattr(order, "order", None)
        if ib_order is not None:
            ref = str(getattr(ib_order, "orderRef", "") or "").strip()
            if ref:
                return ref
        return ""

    @staticmethod
    def _order_note(order: Any) -> str:
        note = str(getattr(order, "note", "") or "").strip()
        if note:
            return note
        return ""

    def _is_order_for_strategy_symbol(self, order: Any, symbol: str) -> bool:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return False
        if self._order_symbol(order) != symbol_u:
            return False
        strategy_id = str(getattr(order, "strategy_id", "") or "").strip()
        if strategy_id == self.strategy_id:
            return True
        order_ref = self._order_ref(order).upper()
        return order_ref.startswith(f"QQQV1:{self.strategy_id.upper()}:{symbol_u}:")

    def _is_protective_stop_like_order(self, order: Any) -> bool:
        order_type = self._order_type(order)
        order_ref = self._order_ref(order).upper()
        note = self._order_note(order).lower()
        is_stop_type = order_type in {"STP", "STP_LMT", "STOP", "STOP_LIMIT"}
        is_stop_ref = ":STOP" in order_ref
        is_stop_note = ("protective_stop" in note) or ("stop" in note and "qqq" in note)
        return bool(is_stop_type or is_stop_ref or is_stop_note)

    async def _open_orders_for_protection_audit(self) -> list[Any]:
        ctx_open_orders_fn = getattr(self.ctx, "open_orders", None)
        prefer_ctx_first = False
        if callable(ctx_open_orders_fn):
            fn_mod = str(getattr(ctx_open_orders_fn, "__module__", "") or "")
            if fn_mod.startswith("unittest.mock"):
                prefer_ctx_first = True
            elif getattr(ctx_open_orders_fn, "side_effect", None) is not None:
                prefer_ctx_first = True

        if prefer_ctx_first:
            try:
                return await self._ctx_open_orders_with_timeout()
            except Exception:
                pass

        open_orders_fn = self._resolve_broker_open_orders_fn()
        if open_orders_fn is not None:
            try:
                orders = await open_orders_fn()
                return list(orders or [])
            except Exception as exc:
                logger.debug(
                    "qqq_broker_open_orders_failed_for_stop_audit",
                    strategy_id=self.strategy_id,
                    symbol=self._exec_symbol,
                    error=str(exc),
                )
        try:
            return await self._ctx_open_orders_with_timeout()
        except Exception:
            return []

    @staticmethod
    def _cancel_id_from_open_order(order: Any) -> Optional[int]:
        fields = ("broker_order_id", "order_id", "id")
        for field in fields:
            raw = getattr(order, field, None)
            if raw is None:
                continue
            try:
                val = str(raw).strip()
                if not val:
                    continue
                return int(val)
            except Exception:
                continue
        return None

    async def _cancel_strategy_open_orders_best_effort(
        self,
        reason: str,
        symbol: Optional[str] = None,
        *,
        only_protective_stop: bool = False,
    ) -> int:
        try:
            orders = await self._open_orders_for_protection_audit()
        except Exception as exc:
            logger.warning("qqq_open_orders_fetch_failed_for_cleanup", strategy_id=self.strategy_id, reason=reason, error=str(exc))
            return 0

        active: list[Any] = []
        for order in orders or []:
            order_strategy_id = str(getattr(order, "strategy_id", "") or "").strip()
            order_ref = self._order_ref(order).upper()
            if order_strategy_id != self.strategy_id and not order_ref.startswith(f"QQQV1:{self.strategy_id.upper()}:"):
                continue
            if symbol and self._order_symbol(order) != str(symbol).strip().upper():
                continue
            if self._is_terminal_order_state(order):
                continue
            if only_protective_stop and (not self._is_protective_stop_like_order(order)):
                continue
            active.append(order)

        if not active:
            return 0

        cancel_fn = self._resolve_broker_cancel_fn()
        if cancel_fn is None:
            logger.warning(
                "qqq_orphan_cleanup_cancel_unavailable",
                strategy_id=self.strategy_id,
                reason=reason,
                symbol=symbol,
                active_orders=len(active),
            )
            return 0

        cancelled = 0
        for order in active:
            cancel_id = self._cancel_id_from_open_order(order)
            if cancel_id is None:
                logger.warning(
                    "qqq_orphan_cleanup_skip_missing_cancel_id",
                    strategy_id=self.strategy_id,
                    reason=reason,
                    symbol=getattr(order, "symbol", None),
                    order_ref=getattr(order, "order_ref", None),
                    state=getattr(order, "state", None),
                )
                continue
            try:
                result = await cancel_fn(int(cancel_id))
                if result is False:
                    logger.warning(
                        "qqq_orphan_cleanup_cancel_failed",
                        strategy_id=self.strategy_id,
                        reason=reason,
                        cancel_id=cancel_id,
                        symbol=getattr(order, "symbol", None),
                    )
                    continue
                cancelled += 1
            except Exception as exc:
                logger.warning(
                    "qqq_orphan_cleanup_cancel_error",
                    strategy_id=self.strategy_id,
                    reason=reason,
                    cancel_id=cancel_id,
                    symbol=getattr(order, "symbol", None),
                    error=str(exc),
                )

        if cancelled > 0:
            logger.warning(
                "qqq_orphan_open_orders_cancelled",
                strategy_id=self.strategy_id,
                reason=reason,
                symbol=symbol,
                cancelled=cancelled,
                scanned=len(active),
            )
        return cancelled

    def _order_belongs_to_trigger(self, order: Any, trigger_id: str) -> bool:
        target = self._normalize_trigger_id(trigger_id)
        if not target:
            return False
        from_ref = self._extract_trigger_id_from_order(
            getattr(order, "order_ref", None),
            getattr(order, "note", None),
        )
        if from_ref and from_ref == target:
            return True
        ref_up = str(getattr(order, "order_ref", "") or "").upper()
        if f":TRG:{target.upper()}" in ref_up:
            return True
        return False

    async def _cancel_trigger_open_orders_best_effort(
        self,
        trigger_id: str,
        *,
        reason: str,
        symbol: Optional[str] = None,
    ) -> int:
        try:
            orders = await self._ctx_open_orders_with_timeout()
        except Exception as exc:
            logger.warning(
                "qqq_trigger_orders_fetch_failed_for_cleanup",
                strategy_id=self.strategy_id,
                trigger_id=trigger_id,
                reason=reason,
                error=str(exc),
            )
            return 0

        active: list[Any] = []
        for order in orders or []:
            if getattr(order, "strategy_id", None) != self.strategy_id:
                continue
            if symbol and getattr(order, "symbol", None) != symbol:
                continue
            if not self._order_belongs_to_trigger(order, trigger_id):
                continue
            if self._is_terminal_order_state(order):
                continue
            active.append(order)

        if not active:
            return 0

        cancel_fn = self._resolve_broker_cancel_fn()
        if cancel_fn is None:
            logger.warning(
                "qqq_trigger_cleanup_cancel_unavailable",
                strategy_id=self.strategy_id,
                trigger_id=trigger_id,
                reason=reason,
                symbol=symbol,
                active_orders=len(active),
            )
            return 0

        cancelled = 0
        for order in active:
            cancel_id = self._cancel_id_from_open_order(order)
            if cancel_id is None:
                logger.warning(
                    "qqq_trigger_cleanup_skip_missing_cancel_id",
                    strategy_id=self.strategy_id,
                    trigger_id=trigger_id,
                    reason=reason,
                    symbol=getattr(order, "symbol", None),
                    order_ref=getattr(order, "order_ref", None),
                    state=getattr(order, "state", None),
                )
                continue
            try:
                result = await cancel_fn(int(cancel_id))
                if result is False:
                    logger.warning(
                        "qqq_trigger_cleanup_cancel_failed",
                        strategy_id=self.strategy_id,
                        trigger_id=trigger_id,
                        reason=reason,
                        cancel_id=cancel_id,
                        symbol=getattr(order, "symbol", None),
                    )
                    continue
                cancelled += 1
            except Exception as exc:
                logger.warning(
                    "qqq_trigger_cleanup_cancel_error",
                    strategy_id=self.strategy_id,
                    trigger_id=trigger_id,
                    reason=reason,
                    cancel_id=cancel_id,
                    symbol=getattr(order, "symbol", None),
                    error=str(exc),
                )
        if cancelled > 0:
            logger.warning(
                "qqq_trigger_open_orders_cancelled",
                strategy_id=self.strategy_id,
                trigger_id=trigger_id,
                reason=reason,
                symbol=symbol,
                cancelled=cancelled,
                scanned=len(active),
            )
        return cancelled

    async def _submit_target(
        self,
        symbol: str,
        qty: int,
        *,
        reason: str = "qqq_intraday_entry",
        kind: str = "ENTRY",
        lock_entry: bool = True,
        trigger_id: Optional[str] = None,
        decision_id_override: Optional[str] = None,
        trace_id_override: Optional[str] = None,
        ref_px: Optional[float] = None,
        ref_px_source: Optional[str] = None,
    ) -> bool:
        now_us = int(datetime.now(ET).timestamp() * 1_000_000)
        if lock_entry:
            active_trigger_id = self._normalize_trigger_id(trigger_id) or self._normalize_trigger_id(make_decision_id())
            self._active_trigger_id = active_trigger_id
        else:
            active_trigger_id = self._normalize_trigger_id(trigger_id) or self._normalize_trigger_id(self._active_trigger_id)
        decision_id = str(decision_id_override or make_decision_id() or "").strip()
        trace_id = str(trace_id_override or decision_id or active_trigger_id or "").strip() or decision_id
        ref_px_val = _safe_float(ref_px)
        if ref_px_val is None or ref_px_val <= 0:
            ref_px_val = _safe_float(self._last_price(symbol))
            if ref_px_val is not None and ref_px_val > 0 and not ref_px_source:
                ref_px_source = "sfs_last"
        extras = {
            "reason": reason,
            "disable_oco": True,
            "force_order_style": True,
            "history_ready": bool(self._seed_ready),
            "trigger_id": active_trigger_id,
            "order_ref": self._make_order_ref(symbol, kind, active_trigger_id),
        }
        if ref_px_val is not None and ref_px_val > 0:
            extras["ref_px"] = float(ref_px_val)
            extras["ref_px_source"] = ref_px_source or "sfs_last"
        dlb_ms = int((self.params.extras or {}).get("dlb_ms", 500))
        envelope = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=symbol,
            ts_bar_end_us=now_us,
            target_qty=float(qty),
            freshness=Freshness(ttl_ms=int(getattr(self.params, "ttl_ms", 2000) or 2000), dlb_ms=dlb_ms),
            order_style=OrderStyle(kind="MKT"),
            extras=extras,
        )
        if lock_entry:
            # Lock entry by setting pending decision ID.
            self._mark_pending_decision(decision_id)
            grace_sec = float((self.params.extras or {}).get("entry_sync_grace_sec", 3.0))
            self._entry_sync_grace_until_mono = asyncio.get_running_loop().time() + max(0.0, grace_sec)
        self._emit_local_audit(
            stage="entry_intent_requested",
            status="requested",
            symbol=symbol,
            decision_id=decision_id,
            trigger_id=active_trigger_id,
            trace_id=trace_id,
            payload={
                "qty": int(qty),
                "reason": reason,
                "kind": kind,
                "lock_entry": bool(lock_entry),
                "order_ref": extras.get("order_ref"),
                "ref_px": extras.get("ref_px"),
                "ref_px_source": extras.get("ref_px_source"),
            },
        )
        if self._system_submit_blocked(reason):
            if lock_entry:
                self._clear_pending_decision("system_health_paused")
                self._active_trigger_id = None
            self._audit_system_submit_blocked(
                stage="entry_intent_blocked_system_health",
                reason=reason,
                symbol=symbol,
                decision_id=decision_id,
            )
            return False
        try:
            submit_result = await self.ctx.submit_intent(envelope)
        except Exception as exc:
            if lock_entry:
                self._clear_pending_decision("submit_intent_exception")
                self._active_trigger_id = None
            logger.error(
                "qqq_entry_intent_submit_error",
                strategy_id=self.strategy_id,
                symbol=symbol,
                decision_id=decision_id,
                qty=qty,
                reason=reason,
                error=str(exc),
            )
            self._emit_local_audit(
                stage="entry_intent_submit_failed",
                severity="warning",
                status="failed",
                reason="submit_intent_exception",
                symbol=symbol,
                decision_id=decision_id,
                trigger_id=active_trigger_id,
                trace_id=trace_id,
                payload={"qty": int(qty), "submit_reason": reason, "error": str(exc)},
            )
            self._persist_runtime_state(reason="submit_target_rejected_submit_exception")
            return False
        status, reasons = self._extract_submit_status(submit_result)

        if status and status != "APPROVED":
            if lock_entry:
                self._clear_pending_decision(f"submit_intent_{status.lower()}")
                self._entry_sync_grace_until_mono = 0.0
            if lock_entry:
                self._active_trigger_id = None
            logger.info(
                "qqq_entry_intent_rejected",
                strategy_id=self.strategy_id,
                symbol=symbol,
                decision_id=decision_id,
                status=status,
                reasons=reasons,
                qty=qty,
                reason=reason,
            )
            self._emit_local_audit(
                stage="entry_intent_rejected",
                severity="warning",
                status=str(status).lower(),
                reason=reason,
                symbol=symbol,
                decision_id=decision_id,
                trigger_id=active_trigger_id,
                trace_id=trace_id,
                payload={"qty": int(qty), "reasons": list(reasons or [])},
            )
            self._persist_runtime_state(reason=f"submit_target_rejected_{status.lower()}")
            return False

        self._persist_runtime_state(reason="submit_target")
        if lock_entry:
            self._expected_entry_qty = int(max(0, qty))
        self._emit_order_lifecycle_transition(
            order_ref=str(extras.get("order_ref") or ""),
            symbol=symbol,
            after_state=OrderLifecycleState.SUBMITTED,
            reason=reason,
            decision_id=decision_id,
            trigger_id=active_trigger_id,
            trace_id=trace_id,
            payload={"qty": int(qty), "kind": kind},
        )
        self._emit_local_audit(
            stage="entry_intent_submitted",
            status="submitted",
            symbol=symbol,
            decision_id=decision_id,
            trigger_id=active_trigger_id,
            trace_id=trace_id,
            payload={"qty": int(qty), "reason": reason, "kind": kind, "order_ref": extras.get("order_ref")},
        )
        return True

    async def _submit_tp1_partial_exit(self, qty: int) -> bool:
        if not self._exec_symbol:
            return False
        reduce_qty = int(max(0, qty))
        if reduce_qty <= 0:
            return False
        now_us = int(datetime.now(ET).timestamp() * 1_000_000)
        envelope = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=self.strategy_id,
            symbol=self._exec_symbol,
            ts_bar_end_us=now_us,
            target_qty=float(-reduce_qty),
            freshness=Freshness(ttl_ms=int(getattr(self.params, "ttl_ms", 2000) or 2000), dlb_ms=500),
            order_style=OrderStyle(kind="MKT"),
            extras={
                "reason": "tp1_partial",
                "disable_oco": True,
                "force_order_style": True,
                "close_only": True,
                "trigger_id": self._normalize_trigger_id(self._active_trigger_id),
                "order_ref": self._make_order_ref(self._exec_symbol, "TP1", self._active_trigger_id),
            },
        )
        try:
            submit_result = await self.ctx.submit_intent(envelope)
            status, reasons = self._extract_submit_status(submit_result)
            if status and status != "APPROVED":
                logger.warning(
                    "qqq_tp1_partial_rejected",
                    strategy_id=self.strategy_id,
                    symbol=self._exec_symbol,
                    qty=reduce_qty,
                    status=status,
                    reasons=reasons,
                )
                return False
            self._persist_runtime_state(reason="tp1_partial_submitted")
            self._emit_order_lifecycle_transition(
                order_ref=str(envelope.extras.get("order_ref") or ""),
                symbol=self._exec_symbol,
                after_state=OrderLifecycleState.SUBMITTED,
                reason="tp1_partial",
                decision_id=envelope.decision_id,
                trigger_id=self._active_trigger_id,
                trace_id=envelope.decision_id,
                payload={"qty": reduce_qty},
            )
            logger.info(
                "qqq_tp1_partial_submitted",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                qty=reduce_qty,
            )
            return True
        except Exception as exc:
            logger.warning(
                "qqq_tp1_partial_submit_failed",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                qty=reduce_qty,
                error=str(exc),
            )
            return False

    async def _current_exec_qty(self) -> float:
        if not self._exec_symbol:
            return 0.0
        return await self._current_exec_qty_for_symbol(self._exec_symbol)

    async def _submit_protective_stop_intent(self, reason: str) -> None:
        if not self._exec_symbol or self._stop_px is None:
            return
        if not bool((self.params.extras or {}).get("submit_exchange_stop_intent", True)):
            return
        if self._is_flattening_active(reason=f"protective_stop:{reason}"):
            age_sec = 0.0
            try:
                now_mono = asyncio.get_running_loop().time()
                if self._flattening_started_mono > 0:
                    age_sec = max(0.0, now_mono - self._flattening_started_mono)
            except RuntimeError:
                pass
            logger.info(
                "qqq_protective_stop_skipped_flattening",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                reason=reason,
                flattening_age_sec=round(float(age_sec), 3),
            )
            return
        qty_signed = await self._current_exec_qty()
        qty = int(max(0.0, float(qty_signed)))
        if qty <= 0:
            logger.info(
                "qqq_protective_stop_skipped_no_long_qty",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                reason=reason,
                qty_signed=round(float(qty_signed), 6),
            )
            return
        stop_px = round(float(self._stop_px), 2)
        now_mono = asyncio.get_running_loop().time()
        dedupe_sec = float((self.params.extras or {}).get("stop_intent_dedupe_sec", 3.0))
        submit_sig = f"{self._exec_symbol}:{qty}:{stop_px}"

        async with self._stop_submit_lock:
            if (
                self._last_stop_submit_sig == submit_sig
                and dedupe_sec > 0.0
                and (now_mono - self._last_stop_submit_mono) < dedupe_sec
            ):
                logger.warning(
                    "qqq_protective_stop_deduped",
                    strategy_id=self.strategy_id,
                    symbol=self._exec_symbol,
                    stop_px=stop_px,
                    qty=qty,
                    reason=reason,
                    dedupe_window_sec=dedupe_sec,
                )
                return

            self._stop_order_ref_seq += 1
            stop_order_ref = f"{self._make_order_ref(self._exec_symbol, 'STOP', self._active_trigger_id)}:{self._stop_order_ref_seq}"
            now_us = int(datetime.now(ET).timestamp() * 1_000_000)
            envelope = DecisionEnvelope(
                decision_id=make_decision_id(),
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                ts_bar_end_us=now_us,
                target_qty=float(-qty),
                freshness=Freshness(ttl_ms=int(getattr(self.params, "ttl_ms", 2000) or 2000), dlb_ms=500),
                order_style=OrderStyle(kind="STP", stop_px=stop_px),
                extras={
                    "reason": reason,
                    "trigger_id": self._normalize_trigger_id(self._active_trigger_id),
                    "protective_stop": True,
                    "disable_oco": True,
                    "force_order_style": True,
                    "close_only": True,
                    "position_qty_hint": float(qty_signed),
                    "position_qty_hint_source": "strategy_runtime",
                    "order_ref": stop_order_ref,
                },
            )
            try:
                submit_result = await self._submit_protective_stop_without_cancelling_pipeline(envelope)
            except RuntimeError as exc:
                if str(exc).startswith("protective_stop_submit_uncertain:"):
                    self._last_stop_submit_sig = submit_sig
                    self._last_stop_submit_mono = now_mono
                    self._emit_local_audit(
                        stage="protective_stop_recovery_armed",
                        severity="warning",
                        status="recovery",
                        reason=reason,
                        symbol=self._exec_symbol,
                        decision_id=envelope.decision_id,
                        trigger_id=self._active_trigger_id,
                        trace_id=envelope.decision_id,
                        payload={
                            "stop_px": stop_px,
                            "qty": qty,
                            "order_ref": stop_order_ref,
                            "submit_error": str(exc),
                            "broker_truth_required": True,
                        },
                    )
                    self._emit_protection_lifecycle_transition(
                        after_state=ProtectionState.RECOVERY,
                        reason=f"{reason}_submit_uncertain",
                        symbol=self._exec_symbol,
                        decision_id=envelope.decision_id,
                        trigger_id=self._active_trigger_id,
                        trace_id=envelope.decision_id,
                        order_ref=stop_order_ref,
                        payload={"stop_px": stop_px, "qty": qty, "submit_error": str(exc)},
                    )
                    self._ensure_stop_audit_task_started()
                    return
                raise
            status, reasons = self._extract_submit_status(submit_result)
            if status and status != "APPROVED":
                logger.warning(
                    "qqq_protective_stop_rejected",
                    strategy_id=self.strategy_id,
                    symbol=self._exec_symbol,
                    stop_px=stop_px,
                    qty=qty,
                    reason=reason,
                    status=status,
                    reasons=reasons,
                )
                self._emit_local_audit(
                    stage="protective_stop_rejected",
                    severity="warning",
                    status=str(status).lower(),
                    reason=reason,
                    symbol=self._exec_symbol,
                    decision_id=envelope.decision_id,
                    trigger_id=self._active_trigger_id,
                    trace_id=envelope.decision_id,
                    payload={
                        "stop_px": stop_px,
                        "qty": qty,
                        "submit_reasons": list(reasons or []),
                        "order_ref": stop_order_ref,
                    },
                )
                self._emit_order_lifecycle_transition(
                    order_ref=stop_order_ref,
                    symbol=self._exec_symbol,
                    after_state=OrderLifecycleState.REJECTED,
                    reason=reason,
                    decision_id=envelope.decision_id,
                    trigger_id=self._active_trigger_id,
                    trace_id=envelope.decision_id,
                    payload={"stop_px": stop_px, "qty": qty, "submit_reasons": list(reasons or [])},
                )
                self._emit_protection_lifecycle_transition(
                    after_state=ProtectionState.FAILED,
                    reason=reason,
                    symbol=self._exec_symbol,
                    decision_id=envelope.decision_id,
                    trigger_id=self._active_trigger_id,
                    trace_id=envelope.decision_id,
                    order_ref=stop_order_ref,
                    payload={"stop_px": stop_px, "qty": qty, "submit_reasons": list(reasons or [])},
                )
                return
            self._last_stop_submit_sig = submit_sig
            self._last_stop_submit_mono = now_mono
            logger.info(
                "qqq_protective_stop_submitted",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                stop_px=stop_px,
                reason=reason,
            )
            self._emit_local_audit(
                stage="protective_stop_submitted",
                status="submitted",
                symbol=self._exec_symbol,
                decision_id=envelope.decision_id,
                trigger_id=self._active_trigger_id,
                trace_id=envelope.decision_id,
                payload={"stop_px": stop_px, "qty": qty, "reason": reason, "order_ref": stop_order_ref},
            )
            self._emit_order_lifecycle_transition(
                order_ref=stop_order_ref,
                symbol=self._exec_symbol,
                after_state=OrderLifecycleState.SUBMITTED,
                reason=reason,
                decision_id=envelope.decision_id,
                trigger_id=self._active_trigger_id,
                trace_id=envelope.decision_id,
                payload={"stop_px": stop_px, "qty": qty},
            )
            self._emit_protection_lifecycle_transition(
                after_state=ProtectionState.PROTECTED,
                reason=reason,
                symbol=self._exec_symbol,
                decision_id=envelope.decision_id,
                trigger_id=self._active_trigger_id,
                trace_id=envelope.decision_id,
                order_ref=stop_order_ref,
                payload={"stop_px": stop_px, "qty": qty},
            )

    async def _submit_flatten_intent_for_symbol(self, symbol: str, qty_signed: float, reason: str) -> bool:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return False
        qty_abs = abs(float(qty_signed))
        if qty_abs <= 1e-6:
            return False
        now_mono = asyncio.get_running_loop().time()
        now_us = int(datetime.now(ET).timestamp() * 1_000_000)
        flatten_qty = float(-qty_signed)
        dedupe_sec = float((self.params.extras or {}).get("flatten_intent_dedupe_sec", 3.0))
        submit_sig = f"{symbol_u}:{round(float(qty_signed), 6)}:{round(float(flatten_qty), 6)}"
        async with self._flatten_submit_lock:
            if (
                self._last_flatten_submit_sig == submit_sig
                and dedupe_sec > 0.0
                and (now_mono - self._last_flatten_submit_mono) < dedupe_sec
            ):
                logger.warning(
                    "qqq_flatten_deduped",
                    strategy_id=self.strategy_id,
                    symbol=symbol_u,
                    reason=reason,
                    qty_signed=round(float(qty_signed), 6),
                    submitted_target_qty=round(float(flatten_qty), 6),
                    dedupe_window_sec=dedupe_sec,
                )
                self._emit_local_audit(
                    stage="flatten_deduped",
                    severity="warning",
                    status="deduped",
                    reason=reason,
                    symbol=symbol_u,
                    payload={
                        "current_qty": float(qty_signed),
                        "submitted_target_qty": float(flatten_qty),
                        "dedupe_window_sec": float(dedupe_sec),
                    },
                )
                return False
        envelope = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            ts_bar_end_us=now_us,
            target_qty=flatten_qty,
            freshness=Freshness(ttl_ms=int(getattr(self.params, "ttl_ms", 2000) or 2000), dlb_ms=500),
            order_style=OrderStyle(kind="MKT"),
            extras={
                "reason": reason,
                "trigger_id": self._normalize_trigger_id(self._active_trigger_id),
                "flatten_action": True,
                "disable_oco": True,
                "force_order_style": True,
                "close_only": True,
                "position_qty_hint": float(qty_signed),
                "position_qty_hint_source": "strategy_runtime",
                "order_ref": self._make_order_ref(symbol_u, "FLATTEN", self._active_trigger_id),
            },
        )
        self._emit_local_audit(
            stage="flatten_intent_requested",
            status="requested",
            symbol=symbol_u,
            decision_id=envelope.decision_id,
            trigger_id=self._active_trigger_id,
            trace_id=envelope.decision_id,
            payload={
                "reason": reason,
                "current_qty": float(qty_signed),
                "submitted_target_qty": float(flatten_qty),
                "order_ref": envelope.extras.get("order_ref"),
            },
        )
        self._emit_flatten_lifecycle_transition(
            after_state=FlattenLifecycleState.SUBMITTED,
            reason=reason,
            symbol=symbol_u,
            decision_id=envelope.decision_id,
            trigger_id=self._active_trigger_id,
            trace_id=envelope.decision_id,
            order_ref=str(envelope.extras.get("order_ref") or ""),
            payload={"current_qty": float(qty_signed), "submitted_target_qty": float(flatten_qty)},
        )
        submit_result = await self.ctx.submit_intent(envelope)
        status, reasons = self._extract_submit_status(submit_result)
        if status and status != "APPROVED":
            logger.warning(
                "qqq_flatten_rejected",
                strategy_id=self.strategy_id,
                symbol=symbol_u,
                reason=reason,
                current_qty=qty_signed,
                submitted_target_qty=flatten_qty,
                status=status,
                reasons=reasons,
            )
            self._emit_local_audit(
                stage="flatten_rejected",
                severity="warning",
                status=str(status).lower(),
                reason=reason,
                symbol=symbol_u,
                decision_id=envelope.decision_id,
                trigger_id=self._active_trigger_id,
                trace_id=envelope.decision_id,
                payload={
                    "current_qty": float(qty_signed),
                    "submitted_target_qty": float(flatten_qty),
                    "submit_reasons": list(reasons or []),
                },
            )
            self._emit_flatten_lifecycle_transition(
                after_state=FlattenLifecycleState.REJECTED,
                reason=reason,
                symbol=symbol_u,
                decision_id=envelope.decision_id,
                trigger_id=self._active_trigger_id,
                trace_id=envelope.decision_id,
                order_ref=str(envelope.extras.get("order_ref") or ""),
                payload={"submit_reasons": list(reasons or [])},
            )
            self._emit_order_lifecycle_transition(
                order_ref=str(envelope.extras.get("order_ref") or ""),
                symbol=symbol_u,
                after_state=OrderLifecycleState.REJECTED,
                reason=reason,
                decision_id=envelope.decision_id,
                trigger_id=self._active_trigger_id,
                trace_id=envelope.decision_id,
                payload={"submit_reasons": list(reasons or [])},
            )
            return False
        self._last_flatten_submit_sig = submit_sig
        self._last_flatten_submit_mono = now_mono
        logger.info(
            "qqq_flatten_submitted",
            strategy_id=self.strategy_id,
            symbol=symbol_u,
            reason=reason,
            current_qty=qty_signed,
            submitted_target_qty=flatten_qty,
        )
        self._emit_local_audit(
            stage="flatten_submitted",
            status="submitted",
            symbol=symbol_u,
            decision_id=envelope.decision_id,
            trigger_id=self._active_trigger_id,
            trace_id=envelope.decision_id,
            payload={"reason": reason, "current_qty": float(qty_signed), "submitted_target_qty": float(flatten_qty)},
        )
        self._emit_order_lifecycle_transition(
            order_ref=str(envelope.extras.get("order_ref") or ""),
            symbol=symbol_u,
            after_state=OrderLifecycleState.SUBMITTED,
            reason=reason,
            decision_id=envelope.decision_id,
            trigger_id=self._active_trigger_id,
            trace_id=envelope.decision_id,
            payload={"current_qty": float(qty_signed), "submitted_target_qty": float(flatten_qty)},
        )
        return True

    async def _flatten(self, reason: str) -> None:
        if not self._exec_symbol:
            return
        
        # LOCK: If we are already flattening, don't trigger again
        if self._is_flattening_active(reason=f"flatten:{reason}"):
            return
        self.state["is_flattening"] = True
        self._flattening_started_mono = asyncio.get_running_loop().time()
        self._emit_local_audit(
            stage="flatten_requested",
            status="requested",
            reason=reason,
            symbol=self._exec_symbol,
            trace_id=self._pending_decision_id or self._active_trigger_id,
            payload={"is_traded": bool(self._is_traded)},
        )

        try:
            await self._refresh_ib_positions_best_effort(force=True)
            qty_signed = float(await self._current_exec_qty())
            qty_abs = abs(qty_signed)
            # Never submit a flatten intent with zero quantity.
            # This prevents repeated -0.0 "flatten" spam when state is stale.
            if qty_abs <= 1e-6:
                now_mono = asyncio.get_running_loop().time()
                extras = self.params.extras or {}
                log_interval_sec = max(0.5, float(extras.get("flatten_zero_qty_log_interval_sec", 10.0)))
                if (now_mono - self._last_flatten_zero_qty_log_mono) >= log_interval_sec:
                    self._last_flatten_zero_qty_log_mono = now_mono
                    logger.info(
                        "qqq_flatten_skipped_zero_qty",
                        strategy_id=self.strategy_id,
                        symbol=self._exec_symbol,
                        reason=reason,
                        is_traded=bool(self._is_traded),
                    )
                    self._emit_local_audit(
                        stage="flatten_skipped_zero_qty",
                        status="skipped",
                        reason=reason,
                        symbol=self._exec_symbol,
                        payload={"is_traded": bool(self._is_traded)},
                    )
                # Try to reconcile state immediately after a zero-qty flatten request.
                await self._sync_position_state(force=True)
                self.state["is_flattening"] = False
                self._flattening_started_mono = 0.0
                return

            if reason == "stop_hit":
                self._last_stop_exit_at = datetime.now(ET)
                self._last_stop_exit_side = self._side

            await self._cancel_strategy_open_orders_best_effort(
                reason=f"flatten_pre_cancel:{reason}",
                symbol=None,
            )
            flatten_ok = await self._submit_flatten_intent_for_symbol(self._exec_symbol, qty_signed, reason)
            if not flatten_ok:
                self.state["is_flattening"] = False
                self._flattening_started_mono = 0.0
                self._persist_runtime_state(reason=f"flatten_submit_rejected:{reason}")
                return
            if bool((self.params.extras or {}).get("flatten_residual_retry_enable", True)):
                retry_delay_sec = max(0.1, float((self.params.extras or {}).get("flatten_residual_retry_delay_sec", 0.8)))
                await asyncio.sleep(retry_delay_sec)
                await self._refresh_ib_positions_best_effort(force=True)
                qty_after = float(await self._current_exec_qty())
                if abs(qty_after) > 1e-6:
                    retry_ok = await self._submit_flatten_intent_for_symbol(
                        self._exec_symbol,
                        qty_after,
                        f"{reason}_residual_retry",
                    )
                    if retry_ok:
                        logger.warning(
                            "qqq_flatten_residual_retry_submitted",
                            strategy_id=self.strategy_id,
                            symbol=self._exec_symbol,
                            reason=reason,
                            residual_qty=qty_after,
                            submitted_target_qty=float(-qty_after),
                        )
                        self._emit_local_audit(
                            stage="flatten_residual_retry_submitted",
                            severity="warning",
                            status="submitted",
                            reason=reason,
                            symbol=self._exec_symbol,
                            payload={"residual_qty": float(qty_after), "submitted_target_qty": float(-qty_after)},
                        )
            if bool((self.params.extras or {}).get("flatten_post_cancel_enable", True)):
                post_cancel_delay_sec = max(0.0, float((self.params.extras or {}).get("flatten_post_cancel_delay_sec", 0.35)))
                if post_cancel_delay_sec > 0:
                    await asyncio.sleep(post_cancel_delay_sec)
                await self._cancel_strategy_open_orders_best_effort(
                    reason=f"flatten_post_cancel:{reason}",
                    symbol=self._exec_symbol,
                    only_protective_stop=True,
                )
            self._persist_runtime_state(reason=f"flatten:{reason}")
        finally:
            # We keep is_flattening = True until sync_position or on_order_update confirms it's flat
            pass

    # ----------------- Position management -----------------

    async def _maybe_update_stop_by_atr(self, now: datetime) -> None:
        """
        Dynamically widens stop based on ATR. 
        Rules: Only widen, Never tighten. Cap at 1.5x Initial Distance.
        """
        if not self._is_traded or not self._exec_symbol or self._entry_px is None:
            return
        if self._stop_dist_pre is None:
            return
        
        # If breakeven shield is already active, we stop ATR widening.
        if self._breakeven_done:
            return

        current_5m_end = _floor_5m(now) + timedelta(minutes=5)
        if self._last_stop_update_5m_end == current_5m_end:
            return
        self._last_stop_update_5m_end = current_5m_end

        atr_p = int((self.params.extras or {}).get("atr_period", 14))
        atr_live = self._atr_5m(self._exec_symbol, atr_p)
        if atr_live is None or atr_live <= 0:
            return

        # Calculate new live distance
        stop_min_abs = float((self.params.extras or {}).get("stop_min_abs", 0.50))
        stop_atr_mult = self._hard_stop_atr_mult(self.params.extras or {})
        dist_live = max(stop_atr_mult * atr_live, stop_min_abs)

        # SPEC RULE 1: StopDist_final = min(max(StopDist_pre, StopDist_live), 1.5 * StopDist_pre)
        dist_final = max(self._stop_dist_pre, dist_live)
        hard_cap = 1.5 * self._stop_dist_pre
        if dist_final > hard_cap:
            dist_final = hard_cap
            logger.debug("qqq_stop_widening_at_hard_cap", strategy_id=self.strategy_id, cap=hard_cap)

        # If significant widening detected, modify order.
        prev_dist = float(self._stop_dist_cur or self._stop_dist_pre)
        if dist_final > prev_dist + 0.005:
            self._stop_dist_cur = float(dist_final)
            self._stop_px = round(self._entry_px - dist_final, 2)
            logger.info(
                "qqq_stop_widened",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                stop_dist=round(dist_final, 4),
                stop_px=round(self._stop_px, 4),
                reason="ATR_live_increased"
            )
            self._persist_runtime_state(reason="stop_widened")
            await self._submit_protective_stop_intent("stop_widened")

    async def _manage_position(self, now: datetime) -> None:
        if not self._is_traded or not self._exec_symbol or not self._side or self._entry_px is None:
            return
        if (not self._all_day_test_mode()) and (not self._is_regular_session(now)):
            return

        live_qty_signed = float(await self._current_exec_qty())
        if abs(live_qty_signed) <= 1e-6:
            # Position is already flat in broker reality; do not evaluate stop/flatten again.
            await self._sync_position_state(force=True)
            return

        last_px = self._last_price(self._exec_symbol)
        if not last_px or last_px <= 0:
            return

        # 1. Hard-stop check (only before TP1, and only after minimum hold time)
        hard_stop_delay_min = float((self.params.extras or {}).get("hard_stop_delay_min", 3.0))
        enable_hard_stop = bool((self.params.extras or {}).get("enable_hard_stop", True))
        if enable_hard_stop and (not self._tp1_hit) and self._entry_time_et is not None:
            held_min = (now - self._entry_time_et).total_seconds() / 60.0
            if held_min >= max(0.0, hard_stop_delay_min):
                if self._stop_px is not None and float(self._stop_px) > 0 and last_px <= float(self._stop_px):
                    await self._flatten("stop_hit")
                    return

        # 2. Optional broker stop line check (also only before TP1)
        if (not self._tp1_hit) and bool((self.params.extras or {}).get("enable_exchange_stop_check", False)):
            if self._stop_px is not None and last_px <= self._stop_px:
                await self._flatten("stop_hit")
                return

        # 3. TP1 + trailing for remaining shares
        target_px = _safe_float(self._trade_target_px)
        if target_px is not None and target_px > 0:
            if not self._tp1_hit:
                if last_px >= target_px:
                    live_qty = int(max(0.0, await self._current_exec_qty()))
                    if live_qty <= 1:
                        await self._flatten("target_hit")
                        return
                    tp1_qty = max(1, int(math.floor(live_qty * 0.5)))
                    if tp1_qty >= live_qty:
                        tp1_qty = max(1, live_qty - 1)
                    submitted = await self._submit_tp1_partial_exit(tp1_qty)
                    if submitted:
                        atr_entry = float(self._entry_atr_used or 0.0)
                        self._tp1_hit = True
                        self._trail_high_px = float(last_px)
                        if atr_entry > 0:
                            self._trail_floor_px = round(float(self._entry_px) + (0.2 * atr_entry), 2)
                        self._persist_runtime_state(reason="tp1_hit")
                    return
            else:
                atr_entry = float(self._entry_atr_used or 0.0)
                if atr_entry > 0:
                    self._trail_high_px = max(float(self._trail_high_px or last_px), float(last_px))
                    trail_line = float(self._trail_high_px) - (0.5 * atr_entry)
                    floor_line = float(self._trail_floor_px or (float(self._entry_px) + (0.2 * atr_entry)))
                    effective_stop = max(floor_line, trail_line)
                    if last_px <= effective_stop:
                        await self._flatten("tp1_then_trailing_stop")
                        return
                if now.time() >= EOD_FLATTEN_TIME:
                    await self._flatten("tp1_then_eod")
                    return
                minute_ts = _floor_minute(now)
                if self._last_tp1_eod_no_exit_minute != minute_ts:
                    self._last_tp1_eod_no_exit_minute = minute_ts
                    logger.debug(
                        "tp1_then_eod_no_exit",
                        strategy_id=self.strategy_id,
                        symbol=self._exec_symbol,
                        last_px=round(float(last_px), 4),
                    )

        # 4. Move Shield (optional; default off in v47 baseline)
        if not self._breakeven_done:
            unrealized = last_px - self._entry_px
            if bool((self.params.extras or {}).get("enable_breakeven_shield", False)) and unrealized >= 1.0:
                new_stop = round(self._entry_px + 0.20, 2)
                # SAFETY CHECK: NewStop < current price - 0.05
                if new_stop < last_px - 0.05:
                    self._stop_px = new_stop
                    self._breakeven_done = True
                    logger.info("qqq_breakeven_shield_armed", strategy_id=self.strategy_id, stop_px=new_stop, price=last_px)
                    self._persist_runtime_state(reason="breakeven_shield")
                    await self._submit_protective_stop_intent("breakeven_shield")
                    return # Skip ATR update this loop if we just did breakeven

        # 5. ATR-based Widening (only if enabled)
        if not self._breakeven_done:
            if bool((self.params.extras or {}).get("enable_dynamic_stop_widen", False)):
                await self._maybe_update_stop_by_atr(now)

        # 6. Logic retreat pre-TP1 only: evaluate in loop cadence (second-level)
        if not self._tp1_hit:
            await self._maybe_logic_retreat(now)

        # 7. Add-on after exit checks; if an exit already happened this loop, add is skipped.
        await self._maybe_add_position(now=now)

    async def _maybe_add_position(self, now: datetime) -> None:
        if self._add_done or self._planned_add_qty <= 0 or self._entry_px is None or self._entry_time_et is None or not self._exec_symbol:
            return
        if self._add_deadline_et is not None and now > self._add_deadline_et:
            self._add_done = True
            return
        minute_ts = _floor_minute(now)
        if self._last_add_check_minute == minute_ts:
            return
        self._last_add_check_minute = minute_ts
        close_now = self._exec_minute_close_for_gate(self._exec_symbol, minute_ts, now)
        if close_now is None or self._entry_atr_used is None:
            return
        if minute_ts < _floor_minute(self._entry_time_et):
            return
        if (minute_ts - _floor_minute(self._entry_time_et)) > timedelta(minutes=2):
            self._add_done = True
            return
        if self._side == "LONG":
            threshold = float(self._entry_px) + (0.08 * float(self._entry_atr_used))
            trigger = close_now >= threshold
            add_fill_est = max(float(close_now), float(threshold))
        else:
            threshold = float(self._entry_px) - (0.08 * float(self._entry_atr_used))
            trigger = close_now <= threshold
            add_fill_est = min(float(close_now), float(threshold))
        if not trigger:
            return
        prev_qty = int(max(0, self._planned_probe_qty))
        add_qty = int(max(0, self._planned_add_qty))
        total_qty = prev_qty + add_qty
        if self._entry_px is not None and prev_qty > 0 and add_qty > 0 and total_qty > 0:
            self._entry_px = ((float(self._entry_px) * float(prev_qty)) + (float(add_fill_est) * float(add_qty))) / float(total_qty)
        try:
            submit_ok = await self._submit_target(
                self._exec_symbol,
                int(add_qty),
                reason="qqq_intraday_add",
                kind="ADD",
                lock_entry=False,
            )
        except TypeError:
            submit_ok = await self._submit_target(self._exec_symbol, int(add_qty))
            self._clear_pending_decision("add_submit_fallback")
        if not submit_ok:
            return
        self._add_done = True
        self._persist_runtime_state(reason="add_submitted")

    async def _maybe_logic_retreat(self, now: datetime) -> None:
        if not self._exec_symbol or not self._side:
            return
        extras = self.params.extras or {}
        min_hold_sec = float((self.params.extras or {}).get("logic_retreat_min_hold_sec", 420.0))
        if self._entry_time_et is not None and min_hold_sec > 0:
            held_sec = (now - self._entry_time_et).total_seconds()
            if held_sec < min_hold_sec:
                return
        vwap = self._vwap()
        if vwap <= 0:
            return
        qqq_now = self._last_price("QQQ")
        if qqq_now is None:
            return
        qqq_ts_ms = int(self._last_seen_ts_ms.get("QQQ") or 0)
        if qqq_ts_ms > 0:
            max_age_sec = max(1.0, float(extras.get("logic_retreat_max_qqq_age_sec", 15.0)))
            qqq_age_sec = max(0.0, now.timestamp() - (qqq_ts_ms / 1000.0))
            if qqq_age_sec > max_age_sec:
                minute_ts = _floor_minute(now)
                if self._last_logic_retreat_minute != minute_ts:
                    self._last_logic_retreat_minute = minute_ts
                    logger.warning(
                        "qqq_logic_retreat_skipped_stale_qqq",
                        strategy_id=self.strategy_id,
                        qqq_age_sec=round(float(qqq_age_sec), 3),
                        max_age_sec=max_age_sec,
                        qqq_ts_ms=qqq_ts_ms,
                    )
                return

        # Trading-spec semantics:
        # - 判定只看 QQQ 实时价 vs QQQ VWAP（秒级结构失效）
        # - 执行平仓价格来自执行标的（TQQQ/SQQQ），由 _flatten 下单实现
        wrong_side = (self._side == "LONG" and float(qqq_now) < vwap) or (self._side == "SHORT" and float(qqq_now) > vwap)
        if wrong_side:
            await self._flatten("logic_retreat")

    async def _force_flatten_1559(self, now: datetime) -> None:
        """
        Final Exit: 15:59 NY Time. 
        Cancel everything, flatten all, go to sleep.
        """
        if now.weekday() > 4:
            return
        if now.time() >= EOD_FLATTEN_TIME:
            day = now.strftime("%Y-%m-%d")
            if self._force_flatten_done_day == day:
                return
            self._force_flatten_done_day = day
            if self._is_traded and self._exec_symbol:
                logger.info("qqq_EOD_FORCE_FLATTEN", strategy_id=self.strategy_id, symbol=self._exec_symbol)
                reason = "tp1_then_eod" if self._tp1_hit else "force_flatten_1559_EOD"
                await self._flatten(reason)

    def _minute_close_for_gate(self, minute_end: datetime, now: datetime) -> Optional[float]:
        val = self._min_closes.get(minute_end)
        if val is not None:
            return float(val)
        # Fallback: if current in-progress minute bar has already ended but hasn't been rolled yet
        # (e.g., delayed first tick of the next minute), use its close as provisional finalized close.
        if self._min_bar is not None and self._min_bar.ts_end == minute_end and now >= minute_end:
            return float(self._min_bar.close)
        return None

    def _exec_minute_close_for_gate(self, symbol: str, minute_end: datetime, now: datetime) -> Optional[float]:
        if symbol not in ("TQQQ", "SQQQ"):
            return None
        val = self._exec_min_closes.get(symbol, {}).get(minute_end)
        if val is not None:
            return float(val)
        cur = self._cur_5m.get(symbol)
        if cur is not None and now >= minute_end:
            return float(cur.close)
        return None

    async def _sync_position_state(self, force: bool = False) -> None:
        now_ts = asyncio.get_running_loop().time()
        if not force and (now_ts - self._last_position_sync_at) < 5.0:
            return
        self._last_position_sync_at = now_ts
        ctx_position_timed_out = {"TQQQ": False, "SQQQ": False}
        now_et = datetime.now(ET)
        extras = self.params.extras or {}
        offhours_ctx_sync_enabled = bool(extras.get("offhours_ctx_position_sync_enabled", False))
        use_broker_only_offhours = (
            (not force)
            and (not self._all_day_test_mode())
            and (not self._is_regular_session(now_et))
            and (not offhours_ctx_sync_enabled)
        )
        if use_broker_only_offhours:
            broker_only_log_interval_sec = max(
                5.0,
                float(extras.get("offhours_broker_only_sync_log_interval_sec", 300.0)),
            )
            if (now_ts - self._last_offhours_broker_only_sync_log_mono) >= broker_only_log_interval_sec:
                self._last_offhours_broker_only_sync_log_mono = now_ts
                logger.info(
                    "qqq_position_sync_offhours_broker_only",
                    strategy_id=self.strategy_id,
                    force=bool(force),
                    is_traded=bool(self._is_traded),
                    exec_symbol=self._exec_symbol,
                    pending_decision_id=self._pending_decision_id,
                )
            p_tqqq = None
            p_sqqq = None
        else:
            try:
                try:
                    p_tqqq = await self._ctx_position_with_timeout("TQQQ")
                except RuntimeError as exc:
                    if str(exc).startswith("ctx_position_timeout:TQQQ:"):
                        p_tqqq = None
                        ctx_position_timed_out["TQQQ"] = True
                    else:
                        raise
                try:
                    p_sqqq = await self._ctx_position_with_timeout("SQQQ")
                except RuntimeError as exc:
                    if str(exc).startswith("ctx_position_timeout:SQQQ:"):
                        p_sqqq = None
                        ctx_position_timed_out["SQQQ"] = True
                    else:
                        raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("qqq_position_sync_failed", strategy_id=self.strategy_id, error=str(exc))
                self._append_diag_event(
                    "qqq_position_sync_failed",
                    {
                        "strategy_id": self.strategy_id,
                        "error": str(exc),
                    },
                )
                self._emit_local_audit(
                    stage="position_sync_failed",
                    severity="warning",
                    status="failed",
                    reason="position_sync_failed",
                    payload={"error": str(exc)},
                )
                return

        def _pos_field(pos: Any, *names: str, default: float = 0.0) -> float:
            if pos is None:
                return float(default)
            if isinstance(pos, dict):
                for name in names:
                    if name in pos and pos.get(name) is not None:
                        return float(pos.get(name) or 0.0)
                return float(default)
            for name in names:
                if hasattr(pos, name):
                    val = getattr(pos, name)
                    if val is not None:
                        return float(val or 0.0)
            return float(default)

        def _pos_opt_field(pos: Any, *names: str) -> Optional[float]:
            if pos is None:
                return None
            if isinstance(pos, dict):
                for name in names:
                    if name in pos and pos.get(name) is not None:
                        return float(pos.get(name) or 0.0)
                return None
            for name in names:
                if hasattr(pos, name):
                    val = getattr(pos, name)
                    if val is not None:
                        return float(val or 0.0)
            return None

        realized_parts: list[float] = []
        rp_t = _pos_opt_field(p_tqqq, "realized_pnl_usd", "realized_pnl")
        rp_s = _pos_opt_field(p_sqqq, "realized_pnl_usd", "realized_pnl")
        if rp_t is not None:
            realized_parts.append(float(rp_t))
        if rp_s is not None:
            realized_parts.append(float(rp_s))
        if realized_parts:
            realized_total = float(sum(realized_parts))
            if self._realized_pnl_snapshot_total is None:
                self._realized_pnl_snapshot_total = realized_total
            else:
                delta = realized_total - float(self._realized_pnl_snapshot_total)
                if abs(delta) > 1e-6:
                    self._day_realized_pnl += float(delta)
                    self._day_closed_trade_pnls.append(float(delta))
                self._realized_pnl_snapshot_total = realized_total

        qty_t = _pos_field(p_tqqq, "pos_qty", "qty")
        qty_s = _pos_field(p_sqqq, "pos_qty", "qty")
        qty_t_ctx = float(qty_t)
        qty_s_ctx = float(qty_s)
        qty_t_source = "ctx"
        qty_s_source = "ctx"
        if use_broker_only_offhours:
            qty_t_source = "broker_only_offhours"
            qty_s_source = "broker_only_offhours"
        broker_t = await self._broker_position_snapshot("TQQQ")
        broker_s = await self._broker_position_snapshot("SQQQ")
        broker_t_available = broker_t is not None
        broker_s_available = broker_s is not None
        broker_t_qty = float(broker_t[0]) if broker_t is not None else 0.0
        broker_s_qty = float(broker_s[0]) if broker_s is not None else 0.0
        broker_t_avg = float(broker_t[1]) if (broker_t is not None and broker_t[1] is not None) else None
        broker_s_avg = float(broker_s[1]) if (broker_s is not None and broker_s[1] is not None) else None
        if abs(qty_t) > 1e-6 and broker_t_available:
            mismatch_abs = max(0.5, float((self.params.extras or {}).get("position_qty_mismatch_abs", 0.5)))
            if abs(broker_t_qty) <= 1e-6:
                qty_t_source = (
                    "broker_ctx_nonzero_broker_flat_quarantined"
                    if self._ctx_stale_quarantined_by_symbol.get("TQQQ")
                    else "broker_ctx_nonzero_broker_flat"
                )
                if not self._ctx_stale_quarantined_by_symbol.get("TQQQ"):
                    logger.warning(
                        "qqq_position_sync_ctx_nonzero_broker_flat_using_broker",
                        strategy_id=self.strategy_id,
                        symbol="TQQQ",
                        qty_ctx=round(float(qty_t), 6),
                        qty_broker=round(float(broker_t_qty), 6),
                    )
                self._register_ctx_nonzero_broker_flat("TQQQ", qty_ctx=float(qty_t), qty_broker=float(broker_t_qty))
                qty_t = float(broker_t_qty)
            elif (qty_t > 0 > broker_t_qty) or (qty_t < 0 < broker_t_qty):
                self._reset_position_mismatch_streaks("TQQQ")
                qty_t_source = "broker_sign_mismatch"
                logger.warning(
                    "qqq_position_sync_ctx_broker_sign_mismatch_using_broker",
                    strategy_id=self.strategy_id,
                    symbol="TQQQ",
                    qty_ctx=round(float(qty_t), 6),
                    qty_broker=round(float(broker_t_qty), 6),
                )
                qty_t = float(broker_t_qty)
            elif abs(float(qty_t) - float(broker_t_qty)) > mismatch_abs:
                self._reset_position_mismatch_streaks("TQQQ")
                qty_t_source = "broker_qty_mismatch"
                logger.warning(
                    "qqq_position_sync_ctx_broker_qty_mismatch_using_broker",
                    strategy_id=self.strategy_id,
                    symbol="TQQQ",
                    qty_ctx=round(float(qty_t), 6),
                    qty_broker=round(float(broker_t_qty), 6),
                    mismatch_abs=mismatch_abs,
                )
                qty_t = float(broker_t_qty)
            else:
                self._reset_position_mismatch_streaks("TQQQ")
                self._last_nonzero_ctx_qty_by_symbol["TQQQ"] = float(qty_t)
        elif abs(qty_t) <= 1e-6 and broker_t_available and abs(broker_t_qty) > 1e-6:
            qty_t_source = "broker_only_offhours" if use_broker_only_offhours else "broker_ctx_missing"
            qty_t_resolved = self._resolve_ctx_missing_qty("TQQQ", qty_ctx=float(qty_t), qty_broker=float(broker_t_qty))
            self._register_ctx_flat_broker_nonzero("TQQQ", qty_ctx=float(qty_t), qty_broker=float(broker_t_qty))
            now_mono = asyncio.get_running_loop().time()
            extras = self.params.extras or {}
            warn_interval_sec = max(5.0, float(extras.get("position_ctx_missing_warn_interval_sec", 60.0)))
            if (now_mono - float(self._last_ctx_missing_warn_mono_by_symbol.get("TQQQ", 0.0) or 0.0)) >= warn_interval_sec:
                self._last_ctx_missing_warn_mono_by_symbol["TQQQ"] = now_mono
                logger.warning(
                    "qqq_position_sync_ctx_missing_using_broker",
                    strategy_id=self.strategy_id,
                    symbol="TQQQ",
                    qty_ctx=round(float(qty_t), 6),
                    qty_broker=round(float(broker_t_qty), 6),
                    qty_resolved=round(float(qty_t_resolved), 6),
                )
            qty_t = qty_t_resolved
        else:
            self._reset_position_mismatch_streaks("TQQQ")
        if abs(qty_s) > 1e-6 and broker_s_available:
            mismatch_abs = max(0.5, float((self.params.extras or {}).get("position_qty_mismatch_abs", 0.5)))
            if abs(broker_s_qty) <= 1e-6:
                qty_s_source = (
                    "broker_ctx_nonzero_broker_flat_quarantined"
                    if self._ctx_stale_quarantined_by_symbol.get("SQQQ")
                    else "broker_ctx_nonzero_broker_flat"
                )
                if not self._ctx_stale_quarantined_by_symbol.get("SQQQ"):
                    logger.warning(
                        "qqq_position_sync_ctx_nonzero_broker_flat_using_broker",
                        strategy_id=self.strategy_id,
                        symbol="SQQQ",
                        qty_ctx=round(float(qty_s), 6),
                        qty_broker=round(float(broker_s_qty), 6),
                    )
                self._register_ctx_nonzero_broker_flat("SQQQ", qty_ctx=float(qty_s), qty_broker=float(broker_s_qty))
                qty_s = float(broker_s_qty)
            elif (qty_s > 0 > broker_s_qty) or (qty_s < 0 < broker_s_qty):
                self._reset_position_mismatch_streaks("SQQQ")
                qty_s_source = "broker_sign_mismatch"
                logger.warning(
                    "qqq_position_sync_ctx_broker_sign_mismatch_using_broker",
                    strategy_id=self.strategy_id,
                    symbol="SQQQ",
                    qty_ctx=round(float(qty_s), 6),
                    qty_broker=round(float(broker_s_qty), 6),
                )
                qty_s = float(broker_s_qty)
            elif abs(float(qty_s) - float(broker_s_qty)) > mismatch_abs:
                self._reset_position_mismatch_streaks("SQQQ")
                qty_s_source = "broker_qty_mismatch"
                logger.warning(
                    "qqq_position_sync_ctx_broker_qty_mismatch_using_broker",
                    strategy_id=self.strategy_id,
                    symbol="SQQQ",
                    qty_ctx=round(float(qty_s), 6),
                    qty_broker=round(float(broker_s_qty), 6),
                    mismatch_abs=mismatch_abs,
                )
                qty_s = float(broker_s_qty)
            else:
                self._reset_position_mismatch_streaks("SQQQ")
                self._last_nonzero_ctx_qty_by_symbol["SQQQ"] = float(qty_s)
        elif abs(qty_s) <= 1e-6 and broker_s_available and abs(broker_s_qty) > 1e-6:
            qty_s_source = "broker_only_offhours" if use_broker_only_offhours else "broker_ctx_missing"
            qty_s_resolved = self._resolve_ctx_missing_qty("SQQQ", qty_ctx=float(qty_s), qty_broker=float(broker_s_qty))
            self._register_ctx_flat_broker_nonzero("SQQQ", qty_ctx=float(qty_s), qty_broker=float(broker_s_qty))
            now_mono = asyncio.get_running_loop().time()
            extras = self.params.extras or {}
            warn_interval_sec = max(5.0, float(extras.get("position_ctx_missing_warn_interval_sec", 60.0)))
            if (now_mono - float(self._last_ctx_missing_warn_mono_by_symbol.get("SQQQ", 0.0) or 0.0)) >= warn_interval_sec:
                self._last_ctx_missing_warn_mono_by_symbol["SQQQ"] = now_mono
                logger.warning(
                    "qqq_position_sync_ctx_missing_using_broker",
                    strategy_id=self.strategy_id,
                    symbol="SQQQ",
                    qty_ctx=round(float(qty_s), 6),
                    qty_broker=round(float(broker_s_qty), 6),
                    qty_resolved=round(float(qty_s_resolved), 6),
                )
            qty_s = qty_s_resolved
        else:
            self._reset_position_mismatch_streaks("SQQQ")
        t_live = abs(qty_t) > 1e-6
        s_live = abs(qty_s) > 1e-6
        position_sync_payload = {
            "force": bool(force),
            "broker_only_offhours": bool(use_broker_only_offhours),
            "ctx_position_timed_out": {
                "TQQQ": bool(ctx_position_timed_out["TQQQ"]),
                "SQQQ": bool(ctx_position_timed_out["SQQQ"]),
            },
            "qty_ctx": {
                "TQQQ": round(float(qty_t_ctx), 6),
                "SQQQ": round(float(qty_s_ctx), 6),
            },
            "qty_broker": {
                "TQQQ": round(float(broker_t_qty), 6),
                "SQQQ": round(float(broker_s_qty), 6),
            },
            "qty_resolved": {
                "TQQQ": round(float(qty_t), 6),
                "SQQQ": round(float(qty_s), 6),
            },
            "qty_source": {
                "TQQQ": qty_t_source,
                "SQQQ": qty_s_source,
            },
            "quarantine": {
                "ctx_stale": {
                    "TQQQ": bool(self._ctx_stale_quarantined_by_symbol.get("TQQQ")),
                    "SQQQ": bool(self._ctx_stale_quarantined_by_symbol.get("SQQQ")),
                },
                "foreign_broker": {
                    "TQQQ": bool(self._foreign_broker_quarantined_by_symbol.get("TQQQ")),
                    "SQQQ": bool(self._foreign_broker_quarantined_by_symbol.get("SQQQ")),
                },
            },
            "broker_available": {
                "TQQQ": bool(broker_t_available),
                "SQQQ": bool(broker_s_available),
            },
            "avg_px_broker": {
                "TQQQ": broker_t_avg,
                "SQQQ": broker_s_avg,
            },
            "exec_symbol_before": self._exec_symbol,
            "is_traded_before": bool(self._is_traded),
            "pending_decision_id": self._pending_decision_id,
        }
        if (
            qty_t_source != "ctx"
            or qty_s_source != "ctx"
            or t_live
            or s_live
            or self._is_traded
            or ctx_position_timed_out["TQQQ"]
            or ctx_position_timed_out["SQQQ"]
        ):
            self._emit_local_audit(
                stage="position_sync_qty_resolution",
                status="resolved",
                symbol="TQQQ" if t_live else ("SQQQ" if s_live else None),
                payload=position_sync_payload,
            )
        if t_live and s_live:
            now_mono = asyncio.get_running_loop().time()
            dual_conflict_cooldown_sec = max(
                0.5,
                float((self.params.extras or {}).get("dual_conflict_action_cooldown_sec", 8.0)),
            )
            if (now_mono - self._last_dual_conflict_action_mono) < dual_conflict_cooldown_sec:
                logger.warning(
                    "qqq_dual_position_conflict_throttled",
                    strategy_id=self.strategy_id,
                    qty_tqqq=round(float(qty_t), 6),
                    qty_sqqq=round(float(qty_s), 6),
                    cooldown_sec=dual_conflict_cooldown_sec,
                )
                return
            if self._dual_conflict_active:
                logger.warning(
                    "qqq_dual_position_conflict_retrying",
                    strategy_id=self.strategy_id,
                    qty_tqqq=round(float(qty_t), 6),
                    qty_sqqq=round(float(qty_s), 6),
                    cooldown_sec=dual_conflict_cooldown_sec,
                )
            self._last_dual_conflict_action_mono = now_mono
            self._dual_conflict_active = True
            self._dual_conflict_clear_probe_count = 0
            self.state["is_flattening"] = True
            if self._flattening_started_mono <= 0.0:
                self._flattening_started_mono = now_mono
            logger.error(
                "qqq_dual_position_conflict_detected",
                strategy_id=self.strategy_id,
                qty_tqqq=round(float(qty_t), 6),
                qty_sqqq=round(float(qty_s), 6),
            )
            try:
                await self._cancel_strategy_open_orders_best_effort(
                    reason="dual_position_conflict_pre_cancel",
                    symbol=None,
                )
                await self._submit_flatten_intent_for_symbol("TQQQ", qty_t, "dual_position_conflict")
                await self._submit_flatten_intent_for_symbol("SQQQ", qty_s, "dual_position_conflict")
            except Exception as exc:
                logger.error(
                    "qqq_dual_position_conflict_flatten_failed",
                    strategy_id=self.strategy_id,
                    error=str(exc),
                )
            self._clear_pending_decision("dual_position_conflict")
            self._persist_runtime_state(reason="dual_position_conflict")
            return
        if self._dual_conflict_active:
            clear_need = max(
                1,
                int((self.params.extras or {}).get("dual_conflict_clear_probe_need", 2)),
            )
            self._dual_conflict_clear_probe_count += 1
            if self._dual_conflict_clear_probe_count >= clear_need:
                logger.info(
                    "qqq_dual_position_conflict_resolved",
                    strategy_id=self.strategy_id,
                    qty_tqqq=round(float(qty_t), 6),
                    qty_sqqq=round(float(qty_s), 6),
                    clear_probe_need=clear_need,
                )
                self._dual_conflict_active = False
                self._dual_conflict_clear_probe_count = 0
        live_symbol = "TQQQ" if t_live else ("SQQQ" if s_live else None)
        
        # CLEAR PENDING LOCK: only clear after repeated "flat + no active order" probes.
        if live_symbol is None and self._pending_decision_id is not None:
            extras = self.params.extras or {}
            entry_fill_timeout_sec = max(5.0, float(extras.get("entry_fill_timeout_sec", 60.0)))
            entry_timeout_reentry_cooldown_sec = max(
                0.0, float(extras.get("entry_timeout_reentry_cooldown_sec", 75.0))
            )
            min_hold_sec = max(
                float(extras.get("pending_decision_min_hold_sec", 8.0)),
                float(extras.get("entry_sync_grace_sec", 3.0)) + 2.0,
            )
            probe_interval_sec = max(0.5, float(extras.get("pending_decision_probe_interval_sec", 1.0)))
            probe_need = max(2, int(extras.get("pending_decision_flat_probe_need", 2)))
            try:
                age_sec = self._pending_decision_age_sec(now_mono=now_ts)
                if age_sec >= entry_fill_timeout_sec:
                    active_trigger_id = self._normalize_trigger_id(self._active_trigger_id)
                    cancelled = 0
                    if active_trigger_id:
                        cancelled = await self._cancel_trigger_open_orders_best_effort(
                            active_trigger_id,
                            reason="entry_fill_timeout_cancel_trigger",
                            symbol=None,
                        )
                    if cancelled <= 0:
                        cancelled = await self._cancel_strategy_open_orders_best_effort(
                            reason="entry_fill_timeout_cancel_all_fallback",
                            symbol=None,
                        )
                    if self.state.get("is_flattening"):
                        self.state["is_flattening"] = False
                        self._flattening_started_mono = 0.0
                    self._clear_pending_decision("entry_fill_timeout")
                    self._entry_sync_grace_until_mono = 0.0
                    self._entry_timeout_block_until_mono = now_ts + entry_timeout_reentry_cooldown_sec
                    self._active_trigger_id = None
                    if self._is_traded or self._exec_symbol:
                        self._clear_trade_runtime_state()
                    self._persist_runtime_state(reason="entry_fill_timeout")
                    logger.warning(
                        "qqq_entry_fill_timeout_cancelled_all",
                        strategy_id=self.strategy_id,
                        trigger_id=active_trigger_id,
                        age_sec=round(float(age_sec), 3),
                        timeout_sec=entry_fill_timeout_sec,
                        cancelled_orders=int(cancelled),
                        cooldown_sec=entry_timeout_reentry_cooldown_sec,
                    )
                    return
                if age_sec >= min_hold_sec and (now_ts - self._pending_decision_last_probe_mono) >= probe_interval_sec:
                    self._pending_decision_last_probe_mono = now_ts
                    open_orders = await self._ctx_open_orders_with_timeout()
                    has_active = False
                    for o in open_orders:
                        if o.strategy_id != self.strategy_id:
                            continue
                        state_norm = str(getattr(o, "state", "") or "").strip().replace(" ", "").replace("_", "").upper()
                        if state_norm not in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED"}:
                            has_active = True
                            break
                    if has_active:
                        self._pending_decision_flat_probe_count = 0
                    else:
                        qty_t_live = abs(await self._current_exec_qty_for_symbol("TQQQ"))
                        qty_s_live = abs(await self._current_exec_qty_for_symbol("SQQQ")) if self._short_enabled else 0.0
                        if qty_t_live <= 1e-6 and qty_s_live <= 1e-6:
                            self._pending_decision_flat_probe_count += 1
                            if self._pending_decision_flat_probe_count >= probe_need:
                                self._clear_pending_decision("flat_no_active_orders")
                                self._persist_runtime_state(reason="pending_cleared_flat_no_active_orders")
                        else:
                            self._pending_decision_flat_probe_count = 0
            except Exception:
                self._pending_decision_flat_probe_count = 0

        if live_symbol is None:
            # Entry/fill event and portfolio snapshot can arrive out-of-order for ~1-2s.
            # Hold state briefly after submit to avoid false "reconciled_flat".
            if self._is_traded and self._exec_symbol and now_ts < self._entry_sync_grace_until_mono:
                return
            if self.state.get("is_flattening"):
                self.state["is_flattening"] = False
                self._flattening_started_mono = 0.0
                logger.info("qqq_flattening_confirmed_flat", strategy_id=self.strategy_id)
                self._emit_flatten_lifecycle_transition(
                    after_state=FlattenLifecycleState.CONFIRMED,
                    reason="position_reconciled_flat",
                    symbol=self._exec_symbol,
                    trace_id=self._pending_decision_id or self._active_trigger_id,
                )

            # GRACE PERIOD: On startup, wait 120s before trusting a 'None' position
            # only while broker position sync is not ready. Once startup position
            # readiness is confirmed, stale runtime state should be reconciled
            # immediately to avoid open-window drift.
            init_at = self._initialized_at_et or datetime.now(ET)
            startup_elapsed = (datetime.now(ET) - init_at).total_seconds()
            if startup_elapsed < 120.0 and (not self._startup_positions_ready):
                logger.debug("qqq_sync_skipping_flat_during_warmup", elapsed=round(startup_elapsed, 1))
                return

            if self._is_traded and self._exec_symbol:
                await self._cancel_strategy_open_orders_best_effort(
                    reason="position_reconciled_flat",
                    symbol=self._exec_symbol,
                )
                if self._round_live_confirmed:
                    max_trades_per_day = int((self.params.extras or {}).get("max_trades_per_day", 3))
                    self._success_count = min(self._success_count + 1, max(1, max_trades_per_day))
                    logger.info(
                        "qqq_trade_round_completed",
                        strategy_id=self.strategy_id,
                        symbol=self._exec_symbol,
                        success_count=self._success_count,
                    )
                logger.info("qqq_position_reconciled_flat", strategy_id=self.strategy_id, symbol=self._exec_symbol)
                # clear current execution info; keep day counters for multi-trade gating
                self._clear_trade_runtime_state()
                self._clear_pending_decision("position_reconciled_flat")
                self._persist_runtime_state(reason="position_reconciled_flat")
            else:
                now_mono = asyncio.get_running_loop().time()
                if (now_mono - self._last_orphan_cleanup_mono) >= 30.0:
                    self._last_orphan_cleanup_mono = now_mono
                    await self._cancel_strategy_open_orders_best_effort(reason="flat_orphan_sweep", symbol=None)
            return

        pos = p_tqqq if live_symbol == "TQQQ" else p_sqqq
        qty_live_signed = float(qty_t if live_symbol == "TQQQ" else qty_s)
        self._round_live_confirmed = True
        avg_px = _pos_field(pos, "avg_px", default=0.0)
        if avg_px <= 0:
            if live_symbol == "TQQQ" and broker_t_avg is not None:
                avg_px = float(broker_t_avg)
            elif live_symbol == "SQQQ" and broker_s_avg is not None:
                avg_px = float(broker_s_avg)
        inferred_side = "LONG" if live_symbol == "TQQQ" else "SHORT"
        changed = (
            (not self._is_traded)
            or (self._exec_symbol != live_symbol)
            or (self._side != inferred_side)
            or (self._entry_px is None and avg_px > 0)
            or (self._entry_px is not None and avg_px > 0 and abs(float(avg_px) - float(self._entry_px)) > 1e-6)
        )
        if changed:
            self._is_traded = True
            if self._active_trigger_id is None:
                self._active_trigger_id = self._normalize_trigger_id(make_decision_id())
            self._clear_pending_decision("position_reconciled_live")
            self._entry_sync_grace_until_mono = 0.0
            self._exec_symbol = live_symbol
            self._side = inferred_side
            if avg_px > 0:
                self._entry_px = avg_px
            if self._entry_time_et is None:
                self._entry_time_et = datetime.now(ET)
            if self._entry_px and (self._entry_atr_used is None or self._entry_atr_used <= 0):
                atr_period = int((self.params.extras or {}).get("atr_period", 14))
                atr_now = self._effective_atr(self._exec_symbol, atr_period, now=datetime.now(ET))
                if atr_now is not None and atr_now > 0:
                    stop_atr_mult = self._hard_stop_atr_mult(self.params.extras or {})
                    stop_min_abs = float((self.params.extras or {}).get("stop_min_abs", 0.50))
                    dist = max(stop_atr_mult * float(atr_now), stop_min_abs)
                    self._entry_atr_used = float(atr_now)
                    self._stop_dist_pre = float(dist)
                    self._stop_dist_max = float(dist) * 1.5
                    self._stop_dist_cur = float(dist)
                    if self._stop_px is None:
                        self._stop_px = round(float(self._entry_px) - float(dist), 2)
            logger.info(
                "qqq_position_reconciled_live",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                side=self._side,
                entry_px=self._entry_px,
            )
            self._persist_runtime_state(reason="position_reconciled_live")
        if qty_live_signed < -1e-6:
            if self._exec_symbol != live_symbol:
                self._exec_symbol = live_symbol
            now_mono = asyncio.get_running_loop().time()
            short_action_interval_sec = max(
                2.0,
                float((self.params.extras or {}).get("unexpected_short_action_interval_sec", 10.0)),
            )
            last_action = float(self._last_unexpected_short_action_mono_by_symbol.get(live_symbol, 0.0) or 0.0)
            if (now_mono - last_action) < short_action_interval_sec:
                logger.warning(
                    "qqq_unexpected_short_inventory_throttled",
                    strategy_id=self.strategy_id,
                    symbol=live_symbol,
                    qty_signed=qty_live_signed,
                    throttle_sec=short_action_interval_sec,
                )
                return
            self._last_unexpected_short_action_mono_by_symbol[live_symbol] = now_mono
            logger.error(
                "qqq_unexpected_short_inventory_detected",
                strategy_id=self.strategy_id,
                symbol=live_symbol,
                qty_signed=qty_live_signed,
            )
            self._emit_local_audit(
                stage="unexpected_short_inventory_detected",
                severity="error",
                status="detected",
                reason="unexpected_short_inventory",
                symbol=live_symbol,
                payload={
                    **position_sync_payload,
                    "live_symbol": live_symbol,
                    "qty_live_signed": round(float(qty_live_signed), 6),
                    "side": self._side,
                    "entry_px": self._entry_px,
                },
            )
            await self._flatten("unexpected_short_inventory")
            return
        await self._ensure_live_protection(now=datetime.now(ET))

    async def _has_live_protective_stop_order(self) -> bool:
        if not self._exec_symbol:
            return False
        orders = await self._open_orders_for_protection_audit()
        for o in orders:
            if self._is_terminal_order_state(o):
                continue
            if not self._is_order_for_strategy_symbol(o, self._exec_symbol):
                continue
            if self._is_protective_stop_like_order(o):
                return True
        return False

    def _ensure_stop_audit_task_started(self) -> None:
        if self._stop_audit_started or not self._running:
            return
        self._stop_audit_task = asyncio.create_task(
            self._audit_stop_order_loop(),
            name=f"qqq_stop_audit:{self.strategy_id}",
        )
        self._stop_audit_started = True

    async def _ensure_live_protection(self, now: datetime) -> None:
        """
        Ensure a live position always has protective stop flow active, including after restart.

        If stop cannot be reconstructed safely, flatten to avoid unprotected carry.
        """
        if not self._is_traded or not self._exec_symbol or not self._side:
            return
        if not bool((self.params.extras or {}).get("submit_exchange_stop_intent", True)):
            return
        qty_signed = await self._current_exec_qty()
        if qty_signed <= 0:
            logger.info(
                "qqq_live_protection_skipped_no_long_qty",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                qty_signed=round(float(qty_signed), 6),
            )
            return

        now_mono = asyncio.get_running_loop().time()
        # Avoid flooding repeated stop intents from the 0.5s main loop.
        if (now_mono - self._last_protection_ensure_mono) < 10.0:
            return
        self._last_protection_ensure_mono = now_mono

        if self._stop_px is None:
            if self._entry_px is None or self._entry_px <= 0:
                qty_live = abs(await self._current_exec_qty())
                if qty_live > 0:
                    logger.error(
                        "qqq_unprotected_position_no_entry_px",
                        strategy_id=self.strategy_id,
                        symbol=self._exec_symbol,
                        qty_live=qty_live,
                    )
                    await self._flatten("restart_unprotected_no_entry_px")
                return

            atr_p = int((self.params.extras or {}).get("atr_period", 14))
            atr = self._effective_atr(self._exec_symbol, atr_p, now=now)
            stop_min_abs = float((self.params.extras or {}).get("stop_min_abs", 0.50))
            stop_atr_mult = self._hard_stop_atr_mult(self.params.extras or {})

            if atr is None or atr <= 0:
                qty_live = abs(await self._current_exec_qty())
                if qty_live > 0:
                    logger.error(
                        "qqq_unprotected_position_no_atr",
                        strategy_id=self.strategy_id,
                        symbol=self._exec_symbol,
                        qty_live=qty_live,
                        entry_px=self._entry_px,
                    )
                    await self._flatten("restart_unprotected_no_atr")
                return

            dist = max(stop_atr_mult * atr, stop_min_abs)
            self._stop_dist_pre = float(dist)
            self._stop_dist_max = float(dist) * 1.5
            self._stop_dist_cur = float(dist)
            # Strategy spec: stop always at entry - distance for traded instrument.
            self._stop_px = round(float(self._entry_px) - float(dist), 2)
            self._emit_protection_lifecycle_transition(
                after_state=ProtectionState.RECOVERY,
                reason="rebuild_stop_after_restart",
                symbol=self._exec_symbol,
                trace_id=self._active_trigger_id or self._pending_decision_id,
                payload={"entry_px": self._entry_px, "stop_px": self._stop_px, "atr": atr},
            )
            self._persist_runtime_state(reason="rebuild_stop_after_restart")
            logger.warning(
                "qqq_rebuilt_stop_after_restart",
                strategy_id=self.strategy_id,
                symbol=self._exec_symbol,
                side=self._side,
                entry_px=round(float(self._entry_px), 4),
                stop_px=round(float(self._stop_px), 4),
                atr=round(float(atr), 4),
            )

        has_stop = await self._has_live_protective_stop_order()
        if not has_stop:
            await self._submit_protective_stop_intent("position_reconcile_protection")
        self._ensure_stop_audit_task_started()

    async def _audit_stop_order_loop(self) -> None:
        """
        High-frequency Audit Loop (1.2s rhythm)
        Ensures the stop loss order exists in IBKR at all times.
        If missing for 4 consecutive checks -> Kill Switch.
        """
        logger.info("qqq_stop_audit_started", strategy_id=self.strategy_id, symbol=self._exec_symbol)
        
        interval_sec = 1.2 # 1200ms rhythm
        self._audit_miss_count = 0
        loop = asyncio.get_running_loop()
        # Entry submit and position-sync can momentarily mark state as flat before the
        # entry fill arrives. Keep the audit loop alive briefly so protective stop
        # self-healing is not lost in this transient window.
        startup_grace_until_mono = loop.time() + max(
            2.0,
            float((self.params.extras or {}).get("stop_audit_startup_grace_sec", 6.0)),
        )

        while self._running:
            try:
                if (not self._is_traded) or (not self._exec_symbol):
                    if loop.time() < startup_grace_until_mono:
                        await asyncio.sleep(interval_sec)
                        continue
                    break
                if self._is_flattening_active(reason="stop_audit_loop"):
                    await asyncio.sleep(interval_sec)
                    continue
                if not bool((self.params.extras or {}).get("submit_exchange_stop_intent", True)):
                    break
                qty_live = abs(await self._current_exec_qty())
                # If no live position yet, do not count misses (avoid pre-fill false kill switch).
                if qty_live <= 0:
                    await asyncio.sleep(interval_sec)
                    continue

                orders = await self._open_orders_for_protection_audit()
                
                # Verify if our specific Stop Loss order exists
                found = False
                for o in orders:
                    if self._is_terminal_order_state(o):
                        continue
                    if not self._is_order_for_strategy_symbol(o, self._exec_symbol):
                        continue
                    if self._is_protective_stop_like_order(o):
                        found = True
                        break
                
                if found:
                    if self._audit_miss_count > 0:
                        logger.info("qqq_stop_audit_recovered", strategy_id=self.strategy_id, symbol=self._exec_symbol)
                    self._audit_miss_count = 0
                else:
                    recent_submit_grace_sec = max(
                        0.0,
                        float((self.params.extras or {}).get("stop_audit_recent_submit_grace_sec", 4.0)),
                    )
                    if recent_submit_grace_sec > 0.0 and (now_mono := loop.time()) > 0.0:
                        since_submit_sec = max(0.0, now_mono - float(self._last_stop_submit_mono or 0.0))
                        if 0.0 < since_submit_sec < recent_submit_grace_sec:
                            logger.info(
                                "qqq_stop_audit_grace_recent_submit",
                                strategy_id=self.strategy_id,
                                symbol=self._exec_symbol,
                                since_submit_sec=round(float(since_submit_sec), 3),
                                grace_sec=round(float(recent_submit_grace_sec), 3),
                            )
                            self._emit_local_audit(
                                stage="stop_audit_grace_recent_submit",
                                severity="info",
                                status="deferred",
                                symbol=self._exec_symbol,
                                payload={
                                    "since_submit_sec": round(float(since_submit_sec), 3),
                                    "grace_sec": round(float(recent_submit_grace_sec), 3),
                                },
                            )
                            self._audit_miss_count = 0
                            await asyncio.sleep(interval_sec)
                            continue
                    self._audit_miss_count += 1
                    logger.warning("qqq_stop_order_missing_audit", 
                                   strategy_id=self.strategy_id, 
                                   symbol=self._exec_symbol, 
                                   miss_count=self._audit_miss_count)
                    self._emit_local_audit(
                        stage="stop_audit_warning",
                        severity="warning",
                        status="missing",
                        symbol=self._exec_symbol,
                        payload={"miss_count": int(self._audit_miss_count)},
                    )
                    
                    # SELF-HEALING: Try to re-emit stop order after 3 misses
                    if self._audit_miss_count == 3:
                        logger.warning("qqq_stop_audit_self_healing_attempt", strategy_id=self.strategy_id)
                        self._emit_local_audit(
                            stage="stop_audit_self_heal",
                            severity="warning",
                            status="attempted",
                            symbol=self._exec_symbol,
                            payload={"miss_count": int(self._audit_miss_count)},
                        )
                        self._emit_protection_lifecycle_transition(
                            after_state=ProtectionState.RECOVERY,
                            reason="audit_self_heal",
                            symbol=self._exec_symbol,
                            trace_id=self._active_trigger_id or self._pending_decision_id,
                            payload={"miss_count": int(self._audit_miss_count)},
                        )
                        await self._submit_protective_stop_intent("audit_self_heal")

                # KILL SWITCH: 8 consecutive misses (~10 seconds)
                # Relaxed from 4 to 8 to handle transient network/Kafka lag.
                if self._audit_miss_count >= 8:
                    logger.error("qqq_KILL_SWITCH_TRIGGERED", 
                                 strategy_id=self.strategy_id, 
                                 symbol=self._exec_symbol, 
                                 reason="Stop loss missing from exchange for 10s")
                    self._emit_protection_lifecycle_transition(
                        after_state=ProtectionState.FAILED,
                        reason="STOP_LOSS_MISSING_KILL_SWITCH",
                        symbol=self._exec_symbol,
                        trace_id=self._active_trigger_id or self._pending_decision_id,
                        payload={"miss_count": int(self._audit_miss_count)},
                    )
                    await self._flatten("STOP_LOSS_MISSING_KILL_SWITCH")
                    break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("qqq_audit_loop_exception", error=str(exc))
            
            await asyncio.sleep(interval_sec)

        self._stop_audit_started = False
        logger.info("qqq_stop_audit_stopped", strategy_id=self.strategy_id)

    def _persist_runtime_state(self, *, reason: str) -> None:
        self._emit_runtime_lifecycle_transition(reason=reason)
        if self._state_path is None:
            return
        try:
            now_iso = datetime.now(ET).isoformat()
            payload = {
                "day": self._day,
                "is_traded": self._is_traded,
                "success_count": self._success_count,
                "day_realized_pnl": self._day_realized_pnl,
                "day_closed_trade_pnls": self._day_closed_trade_pnls,
                "realized_pnl_snapshot_total": self._realized_pnl_snapshot_total,
                "pending_decision_id": self._pending_decision_id,
                "pending_decision_set_at_et": self._pending_decision_set_at_et.isoformat() if self._pending_decision_set_at_et else None,
                "active_trigger_id": self._normalize_trigger_id(self._active_trigger_id),
                "round_live_confirmed": bool(self._round_live_confirmed),
                "side": self._side,
                "exec_symbol": self._exec_symbol,
                "entry_px": self._entry_px,
                "entry_time_et": self._entry_time_et.isoformat() if self._entry_time_et else None,
                "stop_px": self._stop_px,
                "stop_dist_pre": self._stop_dist_pre,
                "stop_dist_cap": self._stop_dist_max,
                "stop_dist_cur": self._stop_dist_cur,
                "entry_atr_used": self._entry_atr_used,
                "tp1_hit": self._tp1_hit,
                "trail_high_px": self._trail_high_px,
                "trail_floor_px": self._trail_floor_px,
                "break_even_armed": self._break_even_armed,
                "price_base": self._price_base,
                "price_base_source": self._price_base_source,
                "price_0945": self._price_0945,
                "amp_locked": self._amp_locked,
                "toxic_day": self._toxic_day,
                "deadwater": self._deadwater,
                "cross_state": self._cross_state,
                "cross_count": self._cross_count,
                "locked_add_threshold": self._locked_add_threshold,
                "locked_dev_threshold": self._locked_dev_threshold,
                "locked_target": self._locked_target,
                "trade_target_px": self._trade_target_px,
                "locked_velocity_sign": self._locked_velocity_sign,
                "planned_total_qty": self._planned_total_qty,
                "planned_probe_qty": self._planned_probe_qty,
                "planned_add_qty": self._planned_add_qty,
                "add_done": self._add_done,
                "add_deadline_et": self._add_deadline_et.isoformat() if self._add_deadline_et else None,
                "last_stop_exit_at": self._last_stop_exit_at.isoformat() if self._last_stop_exit_at else None,
                "last_stop_exit_side": self._last_stop_exit_side,
                "atr_pre": self._atr_pre,
                "force_flatten_done_day": self._force_flatten_done_day,
                "updated_at": now_iso,
                "last_state_change_at": now_iso,
                "updated_at_semantics": "event_driven_state_snapshot",
                "realtime_status_path": str(self._obs_path) if self._obs_path is not None else None,
                "realtime_status_field": "ts_now_et",
                "reason": reason,
            }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("qqq_state_persist_failed", strategy_id=self.strategy_id, error=str(exc))

    def _restore_runtime_state(self) -> None:
        if self._state_path is None or (not self._state_path.exists()):
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("qqq_state_restore_failed", strategy_id=self.strategy_id, error=str(exc))
            return
        day = str(raw.get("day") or "")
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if day != today:
            return
        self._day = day
        self._is_traded = bool(raw.get("is_traded", False))
        self._success_count = int(raw.get("success_count", 0))
        self._day_realized_pnl = float(raw.get("day_realized_pnl", 0.0) or 0.0)
        closed = raw.get("day_closed_trade_pnls")
        if isinstance(closed, list):
            self._day_closed_trade_pnls = [float(x) for x in closed if _safe_float(x) is not None]
        else:
            self._day_closed_trade_pnls = []
        self._realized_pnl_snapshot_total = _safe_float(raw.get("realized_pnl_snapshot_total"))
        self._pending_decision_id = raw.get("pending_decision_id")
        raw_pending_set_at = raw.get("pending_decision_set_at_et")
        if isinstance(raw_pending_set_at, str) and raw_pending_set_at:
            try:
                self._pending_decision_set_at_et = datetime.fromisoformat(raw_pending_set_at)
            except Exception:
                self._pending_decision_set_at_et = None
        else:
            self._pending_decision_set_at_et = None
        self._pending_decision_set_mono = 0.0
        self._pending_decision_flat_probe_count = 0
        self._pending_decision_last_probe_mono = 0.0
        self._active_trigger_id = self._normalize_trigger_id(raw.get("active_trigger_id"))
        self._round_live_confirmed = bool(raw.get("round_live_confirmed", False))
        if not self._is_traded and self._pending_decision_id is None:
            self._active_trigger_id = None
            self._round_live_confirmed = False
        self._side = raw.get("side")
        self._exec_symbol = raw.get("exec_symbol")
        self._runtime_lifecycle = self._derive_runtime_lifecycle()
        self._entry_px = _safe_float(raw.get("entry_px"))
        raw_entry_time = raw.get("entry_time_et")
        if isinstance(raw_entry_time, str) and raw_entry_time:
            try:
                self._entry_time_et = datetime.fromisoformat(raw_entry_time)
            except Exception:
                self._entry_time_et = None
        else:
            self._entry_time_et = None
        self._stop_px = _safe_float(raw.get("stop_px"))
        if self._is_traded and self._exec_symbol and self._stop_px is not None:
            self._protection_lifecycle_state = ProtectionState.PROTECTED
        else:
            self._protection_lifecycle_state = None
        self._stop_dist_pre = _safe_float(raw.get("stop_dist_pre"))
        self._stop_dist_max = _safe_float(raw.get("stop_dist_cap"))
        self._stop_dist_cur = _safe_float(raw.get("stop_dist_cur"))
        self._entry_atr_used = _safe_float(raw.get("entry_atr_used"))
        self._tp1_hit = bool(raw.get("tp1_hit", False))
        self._trail_high_px = _safe_float(raw.get("trail_high_px"))
        self._trail_floor_px = _safe_float(raw.get("trail_floor_px"))
        self._break_even_armed = bool(raw.get("break_even_armed", False))
        self._price_base = _safe_float(raw.get("price_base"))
        pbs = raw.get("price_base_source")
        self._price_base_source = str(pbs) if isinstance(pbs, str) and pbs else None
        self._price_0945 = _safe_float(raw.get("price_0945"))
        self._amp_locked = bool(raw.get("amp_locked", False))
        self._toxic_day = bool(raw.get("toxic_day", False))
        self._deadwater = bool(raw.get("deadwater", False))
        self._cross_state = str(raw.get("cross_state") or "MIDDLE")
        self._cross_count = int(raw.get("cross_count", 0) or 0)
        self._locked_add_threshold = _safe_float(raw.get("locked_add_threshold"))
        self._locked_dev_threshold = _safe_float(raw.get("locked_dev_threshold"))
        self._locked_target = _safe_float(raw.get("locked_target"))
        self._trade_target_px = _safe_float(raw.get("trade_target_px"))
        if (
            self._trade_target_px is None
            and self._is_traded
            and self._entry_px is not None
            and self._entry_atr_used is not None
            and float(self._entry_atr_used) > 0
        ):
            self._trade_target_px = round(float(self._entry_px) + float(self._entry_atr_used), 2)
        sign = raw.get("locked_velocity_sign")
        self._locked_velocity_sign = int(sign) if sign in (1, -1) else None
        self._planned_total_qty = int(raw.get("planned_total_qty", 0) or 0)
        self._planned_probe_qty = int(raw.get("planned_probe_qty", 0) or 0)
        self._planned_add_qty = int(raw.get("planned_add_qty", 0) or 0)
        self._add_done = bool(raw.get("add_done", False))
        raw_add_deadline = raw.get("add_deadline_et")
        if isinstance(raw_add_deadline, str) and raw_add_deadline:
            try:
                self._add_deadline_et = datetime.fromisoformat(raw_add_deadline)
            except Exception:
                self._add_deadline_et = None
        else:
            self._add_deadline_et = None
        last_stop_exit_at = raw.get("last_stop_exit_at")
        if isinstance(last_stop_exit_at, str) and last_stop_exit_at:
            try:
                self._last_stop_exit_at = datetime.fromisoformat(last_stop_exit_at)
            except Exception:
                self._last_stop_exit_at = None
        else:
            self._last_stop_exit_at = None
        last_stop_exit_side = raw.get("last_stop_exit_side")
        self._last_stop_exit_side = str(last_stop_exit_side) if last_stop_exit_side in ("LONG", "SHORT") else None
        atr_pre = raw.get("atr_pre")
        if isinstance(atr_pre, dict):
            self._atr_pre["TQQQ"] = _safe_float(atr_pre.get("TQQQ"))
            self._atr_pre["SQQQ"] = _safe_float(atr_pre.get("SQQQ"))
        self._force_flatten_done_day = raw.get("force_flatten_done_day")
        logger.info(
            "qqq_state_restored",
            strategy_id=self.strategy_id,
            is_traded=self._is_traded,
            side=self._side,
            exec_symbol=self._exec_symbol,
            entry_px=self._entry_px,
        )
