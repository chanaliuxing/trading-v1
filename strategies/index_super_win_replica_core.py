from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from libs.strategy import DecisionEnvelope, Freshness, OrderStyle, OrderUpdate
from libs.strategy import make_decision_id

from strategies.index_super_win_replica_types import (
    ReplicaEntryIntent,
    ReplicaExitIntent,
    ReplicaManageDecision,
    ReplicaOrderUpdateDecision,
    ReplicaTradeState,
)
from strategies.qqq_intraday_manage_logic import (
    add_position_decision,
    breakeven_stop_price,
    hard_stop_triggered,
    logic_retreat_should_flatten,
    target_hit,
    tp1_exit_decision,
    trailing_stop_decision,
)
from strategies.qqq_intraday_execution_intent_logic import build_close_only_extras, exit_target_qty


ET = ZoneInfo("America/New_York")


def normalize_trigger_id(trigger_id: Optional[str]) -> Optional[str]:
    value = str(trigger_id or "").strip()
    return value or None


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def make_replica_order_ref(
    *,
    namespace: str,
    strategy_id: str,
    symbol: str,
    kind: str,
    trigger_id: Optional[str],
) -> str:
    base = f"{namespace}:{strategy_id}:{str(symbol or '').strip().upper()}:{str(kind or 'ENTRY').strip().upper()}"
    normalized = normalize_trigger_id(trigger_id)
    if normalized:
        return f"{base}:TRG:{normalized}"
    return base


class ReplicaEntryCore:
    """
    Single-symbol runtime container for the first replica slice:

    1. entry intent building
    2. submit result handling
    3. order-update convergence for entry lifecycle
    """

    def __init__(
        self,
        *,
        strategy_id: str,
        order_namespace: str,
        ttl_ms: int = 2000,
        dlb_ms: int = 500,
    ) -> None:
        self.strategy_id = str(strategy_id)
        self.order_namespace = str(order_namespace)
        self.ttl_ms = int(ttl_ms)
        self.dlb_ms = int(dlb_ms)

    def build_entry_intent(
        self,
        *,
        state: ReplicaTradeState,
        side: str,
        qty: int,
        reason: str,
        trigger_id: Optional[str],
        kind: str = "ENTRY",
        lock_state: bool = True,
        ref_px: Optional[float] = None,
        ref_px_source: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> tuple[ReplicaTradeState, ReplicaEntryIntent, DecisionEnvelope]:
        side_u = str(side or "").strip().upper()
        if side_u not in {"LONG", "SHORT"}:
            raise ValueError(f"unsupported side: {side}")
        qty_i = int(max(0, qty))
        if qty_i <= 0:
            raise ValueError("qty must be positive")
        ts_now = (now or datetime.now(ET)).astimezone(ET)
        active_trigger_id = normalize_trigger_id(trigger_id) or make_decision_id()
        decision_id = make_decision_id()
        exec_symbol = state.exec_symbol_for_side(side_u)
        order_ref = make_replica_order_ref(
            namespace=self.order_namespace,
            strategy_id=self.strategy_id,
            symbol=exec_symbol,
            kind=kind,
            trigger_id=active_trigger_id,
        )
        payload: Dict[str, Any] = {
            "reason": reason,
            "trigger_id": active_trigger_id,
            "trace_id": decision_id,
            "strategy_run_id": f"{self.strategy_id}:replica",
            "intent_source": "index_super_win_replica",
            "order_ref": order_ref,
        }
        if ref_px is not None:
            payload["ref_px"] = float(ref_px)
        if ref_px_source:
            payload["ref_px_source"] = str(ref_px_source)
        envelope = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=exec_symbol,
            ts_bar_end_us=int(ts_now.timestamp() * 1_000_000),
            target_qty=float(qty_i if side_u == "LONG" else -qty_i),
            freshness=Freshness(ttl_ms=self.ttl_ms, dlb_ms=self.dlb_ms),
            order_style=OrderStyle(kind="MKT"),
            extras=payload,
        )
        if lock_state:
            state.pending_decision_id = decision_id
            state.active_trigger_id = active_trigger_id
            state.entry_order_ref = order_ref
            state.expected_entry_qty = qty_i
            state.active_side = side_u
            state.active_exec_symbol = exec_symbol
        intent = ReplicaEntryIntent(
            symbol=exec_symbol,
            side=side_u,
            qty=qty_i,
            reason=reason,
            trigger_id=active_trigger_id,
            decision_id=decision_id,
            trace_id=decision_id,
            order_ref=order_ref,
            ref_px=ref_px,
            ref_px_source=ref_px_source,
        )
        return state, intent, envelope

    @staticmethod
    def extract_submit_status(submit_result: Any) -> tuple[str, list[str]]:
        status = ""
        reasons: list[str] = []
        allowed = {"APPROVED", "REJECTED", "DEFERRED", "ERROR", "UNKNOWN"}
        try:
            if submit_result is None:
                return status, reasons
            if isinstance(submit_result, dict):
                raw_status = str(submit_result.get("status") or "").strip().upper()
                raw_reasons = submit_result.get("reasons")
            else:
                raw_status = str(getattr(submit_result, "status", "") or "").strip().upper()
                raw_reasons = getattr(submit_result, "reasons", None)
            status = raw_status if raw_status in allowed else ""
            if isinstance(raw_reasons, list):
                reasons = [str(item) for item in raw_reasons if str(item).strip()]
        except Exception:
            return "", []
        return status, reasons

    def handle_submit_result(
        self,
        *,
        state: ReplicaTradeState,
        submit_result: Any,
    ) -> tuple[ReplicaTradeState, ReplicaOrderUpdateDecision]:
        status, reasons = self.extract_submit_status(submit_result)
        if status and status != "APPROVED":
            state.pending_decision_id = None
            state.expected_entry_qty = 0
            state.entry_order_ref = None
            event = ReplicaOrderUpdateDecision(
                accepted=False,
                event="entry_intent_rejected",
                reason=f"submit:{status}",
                runtime_transition="pending->flat",
            )
            state.extras["last_submit_reasons"] = list(reasons)
            return state, event
        event = ReplicaOrderUpdateDecision(
            accepted=True,
            event="entry_intent_submitted",
            reason="submitted",
            runtime_transition="pending->intent_submitted",
        )
        return state, event

    def handle_entry_order_update(
        self,
        *,
        state: ReplicaTradeState,
        update: OrderUpdate,
    ) -> tuple[ReplicaTradeState, ReplicaOrderUpdateDecision]:
        state_norm = str(getattr(update, "state", "") or "").strip().replace(" ", "").replace("_", "").upper()
        order_ref = str(getattr(update, "order_ref", "") or "").strip()
        if not order_ref:
            return state, ReplicaOrderUpdateDecision(False, "ignored", "missing_order_ref")
        owned_prefix = f"{self.order_namespace}:{self.strategy_id}:{state.exec_symbol_for_side(state.active_side)}:"
        if not order_ref.startswith(owned_prefix):
            return state, ReplicaOrderUpdateDecision(False, "ignored", "foreign_order_ref")
        order_ref_u = order_ref.upper()
        if ":ENTRY" not in order_ref_u and ":ADD" not in order_ref_u:
            return state, ReplicaOrderUpdateDecision(False, "ignored", "non_entry_order_ref")

        if state_norm in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "APIPENDING"}:
            return state, ReplicaOrderUpdateDecision(
                True,
                "entry_pending_confirmed",
                f"order_update:{state_norm.lower()}",
                runtime_transition="intent_submitted->pending_entry",
            )

        if state_norm == "FILLED":
            state.is_traded = True
            state.pending_decision_id = None
            return state, ReplicaOrderUpdateDecision(
                True,
                "entry_confirmed",
                "order_update:filled",
                runtime_transition="pending_entry->live",
            )

        if state_norm in {"CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED"}:
            state.pending_decision_id = None
            if not state.is_traded:
                state.active_trigger_id = None
                state.entry_order_ref = None
                state.expected_entry_qty = 0
            return state, ReplicaOrderUpdateDecision(
                True,
                "entry_terminal",
                f"order_update_terminal:{state_norm.lower()}",
                runtime_transition="pending_entry->flat" if not state.is_traded else "live->live",
            )

        return state, ReplicaOrderUpdateDecision(True, "noop", f"order_update:{state_norm.lower() or 'unknown'}")

    def handle_exit_order_update(
        self,
        *,
        state: ReplicaTradeState,
        update: OrderUpdate,
        tp1_qty_hint: int = 0,
    ) -> tuple[ReplicaTradeState, ReplicaOrderUpdateDecision]:
        state_norm = str(getattr(update, "state", "") or "").strip().replace(" ", "").replace("_", "").upper()
        order_ref = str(getattr(update, "order_ref", "") or "").strip()
        if not order_ref:
            return state, ReplicaOrderUpdateDecision(False, "ignored", "missing_order_ref")
        active_symbol = str(state.active_exec_symbol or state.exec_symbol_for_side(state.active_side)).strip().upper()
        owned_prefix = f"{self.order_namespace}:{self.strategy_id}:{active_symbol}:"
        if not order_ref.startswith(owned_prefix):
            return state, ReplicaOrderUpdateDecision(False, "ignored", "foreign_order_ref")

        order_ref_u = order_ref.upper()
        if ":STOP" in order_ref_u or ":TRAIL" in order_ref_u or ":TRAIL_LIMIT" in order_ref_u:
            if state_norm in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "APIPENDING"}:
                return state, ReplicaOrderUpdateDecision(True, "protective_stop_live", f"order_update:{state_norm.lower()}")
            if state_norm == "FILLED":
                self._clear_live_state(state)
                return state, ReplicaOrderUpdateDecision(
                    True,
                    "protective_stop_filled",
                    "order_update:filled",
                    runtime_transition="live->flat",
                )
            if state_norm in {"CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED"}:
                return state, ReplicaOrderUpdateDecision(
                    True,
                    "protective_stop_terminal",
                    f"order_update_terminal:{state_norm.lower()}",
                )
            return state, ReplicaOrderUpdateDecision(True, "noop", f"order_update:{state_norm.lower() or 'unknown'}")

        if ":TP1" in order_ref_u:
            if state_norm in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "APIPENDING"}:
                return state, ReplicaOrderUpdateDecision(True, "tp1_pending", f"order_update:{state_norm.lower()}")
            if state_norm in {"FILLED", "PARTIAL"}:
                live_qty = int(max(0, state.extras.get("live_qty") or state.expected_entry_qty or 0))
                reduce_qty = int(max(0, tp1_qty_hint))
                if reduce_qty > 0 and live_qty > 0:
                    state.extras["live_qty"] = max(0, live_qty - reduce_qty)
                return state, ReplicaOrderUpdateDecision(
                    True,
                    "tp1_exit_confirmed",
                    f"order_update:{state_norm.lower()}",
                    runtime_transition="live->live",
                )
            if state_norm in {"CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED"}:
                return state, ReplicaOrderUpdateDecision(
                    True,
                    "tp1_terminal",
                    f"order_update_terminal:{state_norm.lower()}",
                )
            return state, ReplicaOrderUpdateDecision(True, "noop", f"order_update:{state_norm.lower() or 'unknown'}")

        if ":FLATTEN" in order_ref_u:
            if state_norm in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "APIPENDING"}:
                return state, ReplicaOrderUpdateDecision(True, "flatten_pending", f"order_update:{state_norm.lower()}")
            if state_norm == "FILLED":
                self._clear_live_state(state)
                return state, ReplicaOrderUpdateDecision(
                    True,
                    "flatten_confirmed",
                    "order_update:filled",
                    runtime_transition="live->flat",
                )
            if state_norm in {"CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED"}:
                state.is_flattening = False
                return state, ReplicaOrderUpdateDecision(
                    True,
                    "flatten_terminal",
                    f"order_update_terminal:{state_norm.lower()}",
                )
            return state, ReplicaOrderUpdateDecision(True, "noop", f"order_update:{state_norm.lower() or 'unknown'}")

        return state, ReplicaOrderUpdateDecision(False, "ignored", "non_exit_order_ref")

    @staticmethod
    def _clear_live_state(state: ReplicaTradeState) -> None:
        state.is_traded = False
        state.is_flattening = False
        state.pending_decision_id = None
        state.active_trigger_id = None
        state.expected_entry_qty = 0
        state.entry_order_ref = None
        state.tp1_hit = False
        state.trail_high_px = None
        state.trail_floor_px = None
        state.breakeven_done = False
        state.extras["live_qty"] = 0
        state.extras["protection_active"] = False
        state.extras["native_trail_active"] = False
        state.extras["native_trail_pending"] = False

    @staticmethod
    def snapshot(state: ReplicaTradeState) -> Dict[str, Any]:
        return asdict(state)

    def build_protective_stop_intent(
        self,
        *,
        state: ReplicaTradeState,
        qty_signed: float,
        reason: str,
        stop_px: float,
        now: Optional[datetime] = None,
    ) -> tuple[ReplicaTradeState, ReplicaExitIntent, DecisionEnvelope]:
        if not state.active_exec_symbol:
            raise ValueError("active_exec_symbol missing")
        qty_float = float(qty_signed)
        qty = int(max(0.0, abs(qty_float)))
        if qty <= 0:
            raise ValueError("qty_signed must be non-zero for protective stop")
        stop_px_rounded = round(float(stop_px), 2)
        ts_now = (now or datetime.now(ET)).astimezone(ET)
        decision_id = make_decision_id()
        order_ref = f"{make_replica_order_ref(namespace=self.order_namespace, strategy_id=self.strategy_id, symbol=state.active_exec_symbol, kind='STOP', trigger_id=state.active_trigger_id)}:1"
        target_qty = exit_target_qty(qty_signed=qty_float, qty_to_exit=qty)
        envelope = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=state.active_exec_symbol,
            ts_bar_end_us=int(ts_now.timestamp() * 1_000_000),
            target_qty=target_qty,
            freshness=Freshness(ttl_ms=self.ttl_ms, dlb_ms=self.dlb_ms),
            order_style=OrderStyle(kind="STP", stop_px=stop_px_rounded),
            extras=build_close_only_extras(
                reason=reason,
                trigger_id=normalize_trigger_id(state.active_trigger_id),
                trace_id=decision_id,
                order_ref=order_ref,
                position_qty_hint=float(qty_signed),
                extra_flags={"protective_stop": True},
            ),
        )
        state.stop_px = stop_px_rounded
        intent = ReplicaExitIntent(
            symbol=state.active_exec_symbol,
            reason=reason,
            decision_id=decision_id,
            trace_id=decision_id,
            order_ref=order_ref,
            target_qty=target_qty,
            kind="STOP",
            stop_px=stop_px_rounded,
        )
        return state, intent, envelope

    def build_protective_trailing_intent(
        self,
        *,
        state: ReplicaTradeState,
        qty_signed: float,
        reason: str,
        trail_amount: float | None = None,
        trail_percent: float | None = None,
        trail_stop_px: float | None = None,
        lmt_price_offset: float | None = None,
        now: Optional[datetime] = None,
    ) -> tuple[ReplicaTradeState, ReplicaExitIntent, DecisionEnvelope]:
        if not state.active_exec_symbol:
            raise ValueError("active_exec_symbol missing")
        qty_float = float(qty_signed)
        qty = int(max(0.0, abs(qty_float)))
        if qty <= 0:
            raise ValueError("qty_signed must be non-zero for protective trailing stop")
        has_amount = trail_amount is not None
        has_percent = trail_percent is not None
        if has_amount == has_percent:
            raise ValueError("protective trailing stop requires exactly one of trail_amount or trail_percent")
        ts_now = (now or datetime.now(ET)).astimezone(ET)
        decision_id = make_decision_id()
        order_kind = "TRAIL_LIMIT" if lmt_price_offset is not None else "TRAIL"
        order_ref = f"{make_replica_order_ref(namespace=self.order_namespace, strategy_id=self.strategy_id, symbol=state.active_exec_symbol, kind=order_kind, trigger_id=state.active_trigger_id)}:1"
        target_qty = exit_target_qty(qty_signed=qty_float, qty_to_exit=qty)
        trail_stop_px_rounded = None if trail_stop_px is None else round(float(trail_stop_px), 2)
        trail_amount_rounded = None if trail_amount is None else round(float(trail_amount), 2)
        lmt_offset_rounded = None if lmt_price_offset is None else round(float(lmt_price_offset), 2)
        extras = build_close_only_extras(
            reason=reason,
            trigger_id=normalize_trigger_id(state.active_trigger_id),
            trace_id=decision_id,
            order_ref=order_ref,
            position_qty_hint=float(qty_signed),
            extra_flags={
                "protective_stop": True,
                "runner_trail": True,
                "native_trail": True,
                "previous_stop_px": _optional_float(state.stop_px),
                "reduce_only": True,
            },
        )
        envelope = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=state.active_exec_symbol,
            ts_bar_end_us=int(ts_now.timestamp() * 1_000_000),
            target_qty=target_qty,
            freshness=Freshness(ttl_ms=self.ttl_ms, dlb_ms=self.dlb_ms),
            order_style=OrderStyle(
                kind=order_kind,
                trail_amount=trail_amount_rounded,
                trail_percent=None if trail_percent is None else float(trail_percent),
                trail_stop_px=trail_stop_px_rounded,
                lmt_price_offset=lmt_offset_rounded,
                protective_stop=True,
                close_only=True,
                reduce_only=True,
                role="runner_trail",
            ),
            extras=extras,
        )
        intent = ReplicaExitIntent(
            symbol=state.active_exec_symbol,
            reason=reason,
            decision_id=decision_id,
            trace_id=decision_id,
            order_ref=order_ref,
            target_qty=target_qty,
            kind=order_kind,
            stop_px=trail_stop_px_rounded,
            trail_amount=trail_amount_rounded,
            trail_percent=None if trail_percent is None else float(trail_percent),
            trail_stop_px=trail_stop_px_rounded,
            lmt_price_offset=lmt_offset_rounded,
        )
        return state, intent, envelope

    def build_flatten_intent(
        self,
        *,
        state: ReplicaTradeState,
        qty_signed: float,
        reason: str,
        now: Optional[datetime] = None,
    ) -> tuple[ReplicaTradeState, ReplicaExitIntent, DecisionEnvelope]:
        if not state.active_exec_symbol:
            raise ValueError("active_exec_symbol missing")
        qty_abs = abs(float(qty_signed))
        if qty_abs <= 1e-6:
            raise ValueError("qty_signed must be non-zero for flatten")
        ts_now = (now or datetime.now(ET)).astimezone(ET)
        flatten_qty = exit_target_qty(qty_signed=float(qty_signed))
        decision_id = make_decision_id()
        order_ref = make_replica_order_ref(
            namespace=self.order_namespace,
            strategy_id=self.strategy_id,
            symbol=state.active_exec_symbol,
            kind="FLATTEN",
            trigger_id=state.active_trigger_id,
        )
        envelope = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=state.active_exec_symbol,
            ts_bar_end_us=int(ts_now.timestamp() * 1_000_000),
            target_qty=flatten_qty,
            freshness=Freshness(ttl_ms=self.ttl_ms, dlb_ms=self.dlb_ms),
            order_style=OrderStyle(kind="MKT"),
            extras=build_close_only_extras(
                reason=reason,
                trigger_id=normalize_trigger_id(state.active_trigger_id),
                trace_id=decision_id,
                order_ref=order_ref,
                position_qty_hint=float(qty_signed),
                extra_flags={"flatten_action": True},
            ),
        )
        state.is_flattening = True
        intent = ReplicaExitIntent(
            symbol=state.active_exec_symbol,
            reason=reason,
            decision_id=decision_id,
            trace_id=decision_id,
            order_ref=order_ref,
            target_qty=flatten_qty,
            kind="FLATTEN",
        )
        return state, intent, envelope

    def build_tp1_partial_exit_intent(
        self,
        *,
        state: ReplicaTradeState,
        qty_signed: float,
        qty_to_exit: int,
        reason: str,
        now: Optional[datetime] = None,
    ) -> tuple[ReplicaTradeState, ReplicaExitIntent, DecisionEnvelope]:
        if not state.active_exec_symbol:
            raise ValueError("active_exec_symbol missing")
        live_qty = int(max(0.0, abs(float(qty_signed))))
        tp1_qty = int(max(0, qty_to_exit))
        if live_qty <= 1:
            raise ValueError("live qty too small for partial exit")
        if tp1_qty <= 0 or tp1_qty >= live_qty:
            raise ValueError("tp1 qty must be between 1 and live_qty-1")
        ts_now = (now or datetime.now(ET)).astimezone(ET)
        target_qty = exit_target_qty(qty_signed=float(qty_signed), qty_to_exit=tp1_qty)
        decision_id = make_decision_id()
        order_ref = make_replica_order_ref(
            namespace=self.order_namespace,
            strategy_id=self.strategy_id,
            symbol=state.active_exec_symbol,
            kind="TP1",
            trigger_id=state.active_trigger_id,
        )
        envelope = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=state.active_exec_symbol,
            ts_bar_end_us=int(ts_now.timestamp() * 1_000_000),
            target_qty=target_qty,
            freshness=Freshness(ttl_ms=self.ttl_ms, dlb_ms=self.dlb_ms),
            order_style=OrderStyle(kind="MKT"),
            extras=build_close_only_extras(
                reason=reason,
                trigger_id=normalize_trigger_id(state.active_trigger_id),
                trace_id=decision_id,
                order_ref=order_ref,
                position_qty_hint=float(qty_signed),
                extra_flags={"tp1_partial_exit": True},
            ),
        )
        intent = ReplicaExitIntent(
            symbol=state.active_exec_symbol,
            reason=reason,
            decision_id=decision_id,
            trace_id=decision_id,
            order_ref=order_ref,
            target_qty=target_qty,
            kind="TP1",
        )
        return state, intent, envelope

    def evaluate_position_management(
        self,
        *,
        state: ReplicaTradeState,
        now: datetime,
        last_px: float,
        live_qty: int,
        primary_px: float | None = None,
        primary_vwap: float | None = None,
        primary_age_sec: float | None = None,
        qqq_px: float | None = None,
        qqq_vwap: float | None = None,
        qqq_age_sec: float | None = None,
        atr_live: float | None = None,
        hard_stop_delay_min: float = 3.0,
        enable_hard_stop: bool = True,
        enable_exchange_stop_check: bool = False,
        enable_breakeven_shield: bool = False,
        enable_dynamic_stop_widen: bool = False,
        enable_logic_retreat: bool = False,
        logic_retreat_min_hold_sec: float = 420.0,
        logic_retreat_max_primary_age_sec: float = 15.0,
        logic_retreat_max_qqq_age_sec: float | None = None,
        enable_add_on: bool = False,
        eod_flatten_time_hhmm: tuple[int, int] | tuple[int, int, int] = (15, 59),
    ) -> tuple[ReplicaTradeState, ReplicaManageDecision]:
        if (not state.is_traded) or (not state.active_exec_symbol) or (not state.active_side) or state.entry_px is None:
            return state, ReplicaManageDecision(action="noop", reason="not_live")
        if int(max(0, live_qty)) <= 0:
            return state, ReplicaManageDecision(action="noop", reason="qty_zero")
        last_px_f = float(last_px)
        side_u = str(state.active_side or "").strip().upper()
        is_short = side_u == "SHORT"
        eod_tuple = tuple(int(x) for x in eod_flatten_time_hhmm)
        if len(eod_tuple) >= 3:
            eod_h, eod_m, eod_s = eod_tuple[0], eod_tuple[1], eod_tuple[2]
        else:
            eod_h, eod_m, eod_s = eod_tuple[0], eod_tuple[1], 0
        eod_h = max(0, min(23, int(eod_h)))
        eod_m = max(0, min(59, int(eod_m)))
        eod_s = max(0, min(59, int(eod_s)))

        def at_or_after_eod() -> bool:
            now_tuple = (now.hour, now.minute, now.second)
            return now_tuple >= (eod_h, eod_m, eod_s)

        primary_px_f = _optional_float(primary_px)
        if primary_px_f is None:
            primary_px_f = _optional_float(qqq_px)
        primary_vwap_f = _optional_float(primary_vwap)
        if primary_vwap_f is None:
            primary_vwap_f = _optional_float(qqq_vwap)
        primary_age_f = _optional_float(primary_age_sec)
        if primary_age_f is None:
            primary_age_f = _optional_float(qqq_age_sec)
        max_primary_age_sec = float(logic_retreat_max_primary_age_sec)
        if logic_retreat_max_qqq_age_sec is not None and primary_px is None and primary_vwap is None and primary_age_sec is None:
            max_primary_age_sec = float(logic_retreat_max_qqq_age_sec)

        if enable_hard_stop and (not state.tp1_hit) and state.extras.get("entry_time_et") is not None:
            entry_time = state.extras.get("entry_time_et")
            held_min = (now - entry_time).total_seconds() / 60.0
            if held_min >= max(0.0, float(hard_stop_delay_min)):
                if hard_stop_triggered(side=side_u, stop_px=_optional_float(state.stop_px), last_px=last_px_f):
                    return state, ReplicaManageDecision(action="flatten", reason="stop_hit")

        if (not state.tp1_hit) and enable_exchange_stop_check:
            if hard_stop_triggered(side=side_u, stop_px=_optional_float(state.stop_px), last_px=last_px_f):
                return state, ReplicaManageDecision(action="flatten", reason="stop_hit")

        target_px = state.extras.get("trade_target_px")
        if target_px is not None and float(target_px) > 0:
            if not state.tp1_hit:
                if target_hit(side=side_u, target_px=_optional_float(target_px), last_px=last_px_f):
                    if int(live_qty) <= 1:
                        return state, ReplicaManageDecision(action="flatten", reason="target_hit")
                    tp1 = tp1_exit_decision(
                        side=side_u,
                        live_qty=int(live_qty),
                        last_px=last_px_f,
                        entry_px=_optional_float(state.entry_px),
                        entry_atr_used=_optional_float(state.entry_atr_used),
                    )
                    if tp1 is None:
                        return state, ReplicaManageDecision(action="flatten", reason="target_hit")
                    state.tp1_hit = True
                    state.trail_high_px = float(tp1.updated_trail_high_px)
                    state.trail_floor_px = tp1.updated_trail_floor_px
                    return state, ReplicaManageDecision(
                        action="tp1_exit",
                        reason="tp1_hit",
                        tp1_qty=int(tp1.tp1_qty),
                        updated_trail_high_px=state.trail_high_px,
                        updated_trail_floor_px=state.trail_floor_px,
                    )
            else:
                trailing = trailing_stop_decision(
                    side=side_u,
                    last_px=last_px_f,
                    entry_px=_optional_float(state.entry_px),
                    entry_atr_used=_optional_float(state.entry_atr_used),
                    existing_trail_high_px=_optional_float(state.trail_high_px),
                    existing_trail_floor_px=_optional_float(state.trail_floor_px),
                )
                if trailing is not None:
                    state.trail_high_px = trailing.updated_trail_high_px
                    state.trail_floor_px = trailing.updated_trail_floor_px
                    if trailing.trailing_hit:
                        return state, ReplicaManageDecision(
                            action="flatten",
                            reason="tp1_then_trailing_stop",
                            updated_trail_high_px=state.trail_high_px,
                            updated_trail_floor_px=state.trail_floor_px,
                        )
                if at_or_after_eod():
                    return state, ReplicaManageDecision(action="flatten", reason="tp1_then_eod")
                state.extras["last_tp1_eod_no_exit_minute"] = now.replace(second=0, microsecond=0)

        if (not state.breakeven_done) and enable_breakeven_shield and state.entry_px is not None:
            new_stop = breakeven_stop_price(
                side=side_u,
                entry_px=_optional_float(state.entry_px),
                last_px=last_px_f,
                enable_breakeven_shield=enable_breakeven_shield,
            )
            if new_stop is not None:
                state.stop_px = new_stop
                state.breakeven_done = True
                return state, ReplicaManageDecision(
                    action="move_stop",
                    reason="breakeven_shield",
                    updated_stop_px=new_stop,
                )

        if (not state.tp1_hit) and enable_logic_retreat:
            entry_time = state.extras.get("entry_time_et")
            if isinstance(entry_time, datetime):
                retreat, stale = logic_retreat_should_flatten(
                    side=side_u,
                    now=now,
                    entry_time=entry_time,
                    primary_px=primary_px_f,
                    primary_vwap=primary_vwap_f,
                    primary_age_sec=primary_age_f,
                    min_hold_sec=float(logic_retreat_min_hold_sec),
                    max_primary_age_sec=float(max_primary_age_sec),
                )
                if retreat:
                    return state, ReplicaManageDecision(action="flatten", reason="logic_retreat")
                if stale:
                    state.extras["last_logic_retreat_minute"] = now.replace(second=0, microsecond=0)

        if (not state.tp1_hit) and enable_add_on:
            planned_add_qty = int(max(0, int(state.extras.get("planned_add_qty") or 0)))
            planned_probe_qty = int(max(0, int(state.extras.get("planned_probe_qty") or 0)))
            add_done = bool(state.extras.get("add_done", planned_add_qty <= 0))
            entry_time = state.extras.get("entry_time_et")
            if (not add_done) and planned_add_qty > 0 and isinstance(entry_time, datetime) and state.entry_atr_used is not None and state.entry_px is not None:
                entry_minute = entry_time.replace(second=0, microsecond=0)
                now_minute = now.replace(second=0, microsecond=0)
                last_add_check_minute = state.extras.get("last_add_check_minute")
                if isinstance(last_add_check_minute, datetime) and last_add_check_minute == now_minute:
                    return state, ReplicaManageDecision(action="noop", reason="hold")
                state.extras["last_add_check_minute"] = now_minute
                add_decision = add_position_decision(
                    side=side_u,
                    now=now,
                    entry_time=entry_time,
                    entry_px=_optional_float(state.entry_px),
                    entry_atr_used=_optional_float(state.entry_atr_used),
                    last_px=last_px_f,
                    planned_probe_qty=planned_probe_qty,
                    planned_add_qty=planned_add_qty,
                    add_done=add_done,
                )
                minute_delta = int((now_minute - entry_minute).total_seconds() // 60)
                if add_decision is not None and add_decision.should_add:
                    return state, ReplicaManageDecision(
                        action="add_position",
                        reason="primary_intraday_add",
                        add_qty=planned_add_qty,
                        updated_entry_px=add_decision.updated_entry_px,
                    )
                elif minute_delta > 2:
                    state.extras["add_done"] = True

        if at_or_after_eod() and state.is_traded:
            reason = "tp1_then_eod" if state.tp1_hit else "force_flatten_1559_EOD"
            return state, ReplicaManageDecision(action="flatten", reason=reason)

        if enable_dynamic_stop_widen and (not state.breakeven_done):
            current_5m_end = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0) + timedelta(minutes=5)
            last_update = state.extras.get("last_stop_update_5m_end")
            if isinstance(last_update, datetime) and last_update == current_5m_end:
                return state, ReplicaManageDecision(action="noop", reason="hold")
            state.extras["last_stop_update_5m_end"] = current_5m_end
            atr_live_f = _optional_float(atr_live)
            entry_atr_f = _optional_float(state.entry_atr_used)
            entry_px_f = _optional_float(state.entry_px)
            stop_px_f = _optional_float(state.stop_px)
            if atr_live_f is not None and atr_live_f > 0 and entry_atr_f is not None and entry_atr_f > 0 and entry_px_f is not None and stop_px_f is not None and stop_px_f > 0:
                stop_min_abs = float(state.extras.get("stop_min_abs") or 0.50)
                stop_atr_mult = float(state.extras.get("stop_atr_mult") or 2.5)
                dist_live = max(stop_atr_mult * atr_live_f, stop_min_abs)
                stop_dist_pre = _optional_float(state.extras.get("stop_dist_pre"))
                if stop_dist_pre is None or stop_dist_pre <= 0:
                    stop_dist_pre = abs(entry_px_f - stop_px_f)
                if stop_dist_pre and stop_dist_pre > 0:
                    dist_final = max(stop_dist_pre, dist_live)
                    hard_cap = 1.5 * stop_dist_pre
                    if dist_final > hard_cap:
                        dist_final = hard_cap
                    prev_dist = _optional_float(state.extras.get("stop_dist_cur"))
                    if prev_dist is None or prev_dist <= 0:
                        prev_dist = stop_dist_pre
                    if dist_final > (prev_dist + 0.005):
                        state.extras["stop_dist_pre"] = float(stop_dist_pre)
                        state.extras["stop_dist_cur"] = float(dist_final)
                        new_stop = round(entry_px_f + dist_final, 2) if is_short else round(entry_px_f - dist_final, 2)
                        state.stop_px = new_stop
                        return state, ReplicaManageDecision(action="move_stop", reason="stop_widened", updated_stop_px=new_stop)

        return state, ReplicaManageDecision(action="noop", reason="hold")


# Backward-compatible alias while downstream imports are being migrated.
QQQReplicaEntryCore = ReplicaEntryCore
