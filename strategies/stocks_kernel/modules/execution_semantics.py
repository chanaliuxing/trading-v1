from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from strategies.stocks_kernel.types import ExitPlan


@dataclass(frozen=True)
class EntryConflictDecision:
    blocked: bool
    reason: str
    payload: dict[str, Any]


class ExecutionSemanticsModule:
    """
    Execution semantics module.

    Centralizes runtime behavior constraints around entry/exit ordering:
    - flatten/exit candidate filtering
    - active-lot entry dedupe
    - stale pending-entry quarantine checks
    """

    def __init__(self, *, strategy) -> None:
        self._strategy = strategy

    def entry_pending_timeout_sec(self) -> float:
        params = getattr(self._strategy, "params", None)
        ttl_ms = int(getattr(params, "ttl_ms", 30_000) or 30_000)
        return max(15.0, float(ttl_ms) / 1000.0)

    @staticmethod
    def infer_submit_failure_reason(result: Any) -> str | None:
        if isinstance(result, dict):
            if result.get("success") is False:
                return str(result.get("reason") or "submit_not_success")
            status = str(result.get("status") or "").strip().upper()
            if status and status not in {"APPROVED", "OK", "SUCCESS"}:
                reasons = result.get("reasons") or []
                if isinstance(reasons, list) and reasons:
                    return str(reasons[0])
                return str(result.get("reason") or f"submit_status_{status.lower()}")
            if result.get("ok") is False:
                return str(result.get("reason") or "submit_not_ok")
        metadata = getattr(result, "metadata", None)
        if isinstance(metadata, dict):
            execution_result = metadata.get("execution_result")
            if isinstance(execution_result, dict) and execution_result.get("success") is False:
                return str(execution_result.get("reason") or "execution_not_success")
        success = getattr(result, "success", None)
        if success is False:
            return str(getattr(result, "reason", "") or "submit_not_success")
        status = str(getattr(result, "status", "") or "").strip().upper()
        if status and status != "APPROVED":
            reasons = getattr(result, "reasons", None) or []
            if isinstance(reasons, list) and reasons:
                return str(reasons[0])
            return f"submit_status_{status.lower()}"
        return None

    @staticmethod
    def should_activate_kill_switch(reason: str | None) -> bool:
        reason_v = str(reason or "").strip()
        if not reason_v:
            return False
        return (
            reason_v.startswith("entry_isolation_blocked")
            or reason_v.startswith("broker_")
            or reason_v == "reservation_failed"
        )

    @staticmethod
    def should_skip_exit_for_lot(lot: Any) -> bool:
        if str(getattr(lot, "state", "") or "") not in {"live", "pending_exit"}:
            return True
        if bool(getattr(lot, "flatten_order_refs", None)):
            return True
        return float(getattr(lot, "live_qty", 0.0) or 0.0) <= 0.0

    @staticmethod
    def evaluate_entry_conflict(
        *,
        entry_symbol: str,
        active_binding_lots: list[Any],
        strategy_id: str | None = None,
        entry_lane_id: str | None = None,
        entry_ownership_scope: str = "strategy",
    ) -> EntryConflictDecision:
        if not active_binding_lots:
            return EntryConflictDecision(blocked=False, reason="ready", payload={})
        entry_symbol_u = str(entry_symbol or "").strip().upper()
        strategy_id_v = str(strategy_id or "").strip()
        lane_id_v = str(entry_lane_id or "").strip() or None
        entry_scope = str(entry_ownership_scope or "strategy").strip() or "strategy"
        same_symbol_lots: list[Any] = []
        cross_symbol_lots: list[Any] = []
        for lot in active_binding_lots:
            lot_symbol = str(getattr(lot, "symbol", "") or "").strip().upper()
            lot_scope = str(getattr(lot, "ownership_scope", "") or "strategy").strip() or "strategy"
            lot_strategy_id = str(getattr(lot, "strategy_id", "") or "").strip()
            lot_lane_id = str(getattr(lot, "lane_id", "") or "").strip() or None
            if (
                entry_scope == "lane"
                and lot_scope == "lane"
                and strategy_id_v
                and lot_strategy_id
                and lot_strategy_id != strategy_id_v
            ):
                continue
            if (
                entry_scope == "lane"
                and lot_scope == "lane"
                and strategy_id_v
                and lot_strategy_id == strategy_id_v
                and lane_id_v
                and lot_lane_id
                and lot_lane_id != lane_id_v
            ):
                if lot_symbol == entry_symbol_u:
                    same_symbol_lots.append(lot)
                else:
                    cross_symbol_lots.append(lot)
                continue
            if lot_symbol == entry_symbol_u:
                same_symbol_lots.append(lot)
            else:
                cross_symbol_lots.append(lot)
        same_symbol_conflict = bool(same_symbol_lots)
        conflict_lots: list[Any] = same_symbol_lots if same_symbol_conflict else cross_symbol_lots
        if not conflict_lots:
            return EntryConflictDecision(blocked=False, reason="ready", payload={})
        active_symbols = sorted({str(getattr(lot, "symbol", "") or "").strip().upper() for lot in active_binding_lots if lot is not None})
        active_lot_ids = [
            str(getattr(lot, "position_lot_id", "") or "")
            for lot in conflict_lots
            if lot is not None and str(getattr(lot, "position_lot_id", "") or "").strip()
        ]
        reason = (
            "existing_exec_lot_present"
            if same_symbol_conflict
            else "binding_conflict_active_lot"
        )
        return EntryConflictDecision(
            blocked=True,
            reason=reason,
            payload={
                "active_symbols": active_symbols,
                "active_position_lot_ids": active_lot_ids,
                "entry_ownership_scope": entry_scope,
                "entry_lane_id": lane_id_v,
            },
        )

    @staticmethod
    def pending_entry_quarantine_candidate(*, lot: Any, now_ts_epoch_s: float, timeout_sec: float) -> tuple[bool, dict[str, float]]:
        if str(getattr(lot, "state", "") or "") != "pending_entry":
            return False, {}
        extras = dict(getattr(lot, "extras", {}) or {})
        requested_ts = float(extras.get("entry_requested_ts_epoch_s") or 0.0)
        if requested_ts <= 0.0:
            return False, {}
        age_sec = max(0.0, float(now_ts_epoch_s or 0.0) - requested_ts)
        if age_sec < float(timeout_sec or 0.0):
            return False, {"age_sec": age_sec, "timeout_sec": float(timeout_sec or 0.0)}
        return True, {"age_sec": age_sec, "timeout_sec": float(timeout_sec or 0.0)}

    @staticmethod
    def select_exit_plan_for_lot(*, lot: Any, snapshot: Any, feature_context: Any) -> ExitPlan | None:
        if getattr(snapshot, "last_price", None) is None or getattr(lot, "avg_entry_price", None) is None:
            return None
        last_price = float(snapshot.last_price)
        avg_entry = float(lot.avg_entry_price)
        stop_payload = dict(getattr(lot, "extras", {}).get("stop_plan") or {})
        tp_payload = dict(getattr(lot, "extras", {}).get("take_profit_plan") or {})
        retreat_payload = dict(getattr(lot, "extras", {}).get("retreat_plan") or {})

        if stop_payload:
            stop_offset = float(stop_payload.get("logic_stop_offset") or 0.0)
            if stop_offset > 0.0:
                if lot.side == "LONG" and last_price <= avg_entry - stop_offset:
                    return ExitPlan(
                        symbol=lot.symbol,
                        order_role="STOP",
                        qty=float(lot.live_qty),
                        position_lot_id=lot.position_lot_id,
                        payload={"reason": "hard_stop_triggered", "trigger_price": last_price, **stop_payload},
                    )
                if lot.side == "SHORT" and last_price >= avg_entry + stop_offset:
                    return ExitPlan(
                        symbol=lot.symbol,
                        order_role="STOP",
                        qty=float(lot.live_qty),
                        position_lot_id=lot.position_lot_id,
                        payload={"reason": "hard_stop_triggered", "trigger_price": last_price, **stop_payload},
                    )

        if tp_payload:
            atr_eff = float(tp_payload.get("atr_eff") or 0.0)
            target_offset_atr = float(tp_payload.get("target_offset_atr") or 0.0)
            target_offset = atr_eff * target_offset_atr
            if target_offset > 0.0:
                if lot.side == "LONG" and last_price >= avg_entry + target_offset:
                    return ExitPlan(
                        symbol=lot.symbol,
                        order_role="TAKE_PROFIT",
                        qty=float(lot.live_qty),
                        position_lot_id=lot.position_lot_id,
                        payload={"reason": "take_profit_triggered", "trigger_price": last_price, **tp_payload},
                    )
                if lot.side == "SHORT" and last_price <= avg_entry - target_offset:
                    return ExitPlan(
                        symbol=lot.symbol,
                        order_role="TAKE_PROFIT",
                        qty=float(lot.live_qty),
                        position_lot_id=lot.position_lot_id,
                        payload={"reason": "take_profit_triggered", "trigger_price": last_price, **tp_payload},
                    )

        if retreat_payload:
            entry_ts_epoch_s = float(getattr(lot, "extras", {}).get("entry_ts_epoch_s") or 0.0)
            min_hold_sec = float(retreat_payload.get("logic_retreat_min_hold_sec") or 0.0)
            held_sec = max(0.0, float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0) - entry_ts_epoch_s)
            v3m = getattr(feature_context, "values", {}).get("v3m")
            try:
                v3m_f = float(v3m)
            except Exception:
                v3m_f = 0.0
            if held_sec >= min_hold_sec:
                if lot.side == "LONG" and v3m_f <= 0.0:
                    return ExitPlan(
                        symbol=lot.symbol,
                        order_role="RETREAT",
                        qty=float(lot.live_qty),
                        position_lot_id=lot.position_lot_id,
                        payload={"reason": "retreat_triggered", "held_sec": held_sec, **retreat_payload},
                    )
                if lot.side == "SHORT" and v3m_f >= 0.0:
                    return ExitPlan(
                        symbol=lot.symbol,
                        order_role="RETREAT",
                        qty=float(lot.live_qty),
                        position_lot_id=lot.position_lot_id,
                        payload={"reason": "retreat_triggered", "held_sec": held_sec, **retreat_payload},
                    )
        return None
