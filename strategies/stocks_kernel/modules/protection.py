from __future__ import annotations

from typing import Any

from strategies.qqq_intraday_manage_logic import (
    breakeven_stop_price,
    hard_stop_triggered,
    target_hit,
    trailing_stop_decision,
)
from strategies.stocks_kernel.types import ExitPlan


class ProtectionModule:
    """
    Protection semantics module.

    Computes protection payload from policy plans and session execution profile.
    """

    def __init__(self, *, strategy) -> None:
        self._strategy = strategy

    def build_entry_protection_payload(self, *, signal_symbol: str, exec_symbol: str, lane_id: str | None = None, snapshot=None) -> dict[str, Any]:
        strategy = self._strategy
        if not strategy._protection_enabled():
            return {}
        instrument = strategy._instrument_for_signal_symbol(str(signal_symbol or "").strip().upper())
        if strategy.ownership_policy is not None:
            authority = strategy.ownership_policy.can_build_protection_for_lane(
                strategy_id=strategy.strategy_id,
                instrument=instrument,
                exec_symbol=exec_symbol,
                lane_id=lane_id,
            )
            if not authority.allowed:
                strategy._audit(
                    stage="protection_payload_blocked_authority",
                    symbol=str(exec_symbol or "").strip().upper(),
                    reason=authority.reason,
                    payload=dict(authority.payload or {}),
                )
                return {}
        state = strategy.kernel_state.get(str(signal_symbol or "").strip().upper()) or {}
        stop_plan = state.get("stop_plan")
        take_profit_plan = state.get("take_profit_plan")
        payload: dict[str, Any] = {}
        if stop_plan is not None:
            stop_payload = dict(getattr(stop_plan, "payload", {}) or {})
            try:
                sl_offset_abs = abs(float(stop_payload.get("logic_stop_offset") or 0.0))
            except Exception:
                sl_offset_abs = 0.0
            if sl_offset_abs > 0.0:
                payload["sl_offset_abs"] = sl_offset_abs
        if take_profit_plan is not None:
            tp_payload = dict(getattr(take_profit_plan, "payload", {}) or {})
            try:
                atr_eff = abs(float(tp_payload.get("atr_eff") or 0.0))
                target_offset_atr = abs(float(tp_payload.get("target_offset_atr") or 0.0))
            except Exception:
                atr_eff = 0.0
                target_offset_atr = 0.0
            tp_offset_abs = atr_eff * target_offset_atr
            if tp_offset_abs > 0.0:
                payload["tp_offset_abs"] = tp_offset_abs
        if payload:
            payload["symbol"] = str(exec_symbol or "").strip().upper()
            payload["tif"] = "DAY"
            payload = self.with_exec_market_ref(payload=payload, exec_symbol=payload["symbol"], snapshot=snapshot)
            market_snapshot = self._exec_market_snapshot(exec_symbol=payload["symbol"], snapshot=snapshot)
            session_snapshot = market_snapshot if market_snapshot is not None else snapshot
            payload.update(strategy.session_module.protective_execution_session_extras(snapshot=session_snapshot) if strategy.session_module is not None else {})
            payload.update(self.protective_offhours_stop_defaults(payload))
        return payload

    def with_exec_market_ref(self, *, payload: dict[str, Any], exec_symbol: str, snapshot=None) -> dict[str, Any]:
        out = dict(payload or {})
        market_snapshot = self._exec_market_snapshot(exec_symbol=exec_symbol, snapshot=snapshot)
        for key in (
            "market_ref_px",
            "quote_px",
            "quote_bid_px",
            "quote_ask_px",
            "last_px",
            "quote_mid_px",
        ):
            out.pop(key, None)
        out.update(self._market_ref_payload(snapshot=market_snapshot))
        return out

    def _exec_market_snapshot(self, *, exec_symbol: str, snapshot):
        exec_symbol_u = str(exec_symbol or "").strip().upper()
        snapshot_symbol = ""
        if snapshot is not None:
            try:
                snapshot_symbol = str(getattr(snapshot, "symbol", "") or "").strip().upper()
            except Exception:
                snapshot_symbol = ""
        if snapshot is not None and (not snapshot_symbol or snapshot_symbol == exec_symbol_u):
            return snapshot
        resolver = getattr(self._strategy, "_resolve_exec_snapshot", None)
        if callable(resolver) and exec_symbol_u:
            try:
                resolved = resolver(exec_symbol=exec_symbol_u)
            except Exception:
                resolved = None
            if resolved is not None:
                return resolved
        if snapshot is not None and snapshot_symbol and snapshot_symbol != exec_symbol_u:
            audit = getattr(self._strategy, "_audit", None)
            if callable(audit):
                try:
                    audit(
                        stage="protection_market_ref_snapshot_mismatch",
                        symbol=exec_symbol_u,
                        reason="signal_snapshot_not_exec_symbol",
                        payload={"snapshot_symbol": snapshot_symbol, "exec_symbol": exec_symbol_u},
                    )
                except Exception:
                    pass
            return None
        return snapshot

    @staticmethod
    def _market_ref_payload(*, snapshot) -> dict[str, Any]:
        if snapshot is None:
            return {}

        def _first_pos(*names: str) -> float:
            for name in names:
                try:
                    value = getattr(snapshot, name, None)
                except Exception:
                    value = None
                try:
                    px = float(value) if value is not None else 0.0
                except Exception:
                    px = 0.0
                if px > 0:
                    return px
            return 0.0

        bid = _first_pos("bid", "bid_price")
        ask = _first_pos("ask", "ask_price")
        last = _first_pos("last_price", "last", "close", "price")
        mid = ((bid + ask) / 2.0) if bid > 0 and ask > 0 and bid <= ask else _first_pos("mid", "mid_price")
        market_ref = mid or last or bid or ask
        if market_ref <= 0:
            return {}
        out = {
            "market_ref_px": float(market_ref),
            "quote_px": float(market_ref),
        }
        if bid > 0:
            out["quote_bid_px"] = float(bid)
        if ask > 0:
            out["quote_ask_px"] = float(ask)
        if last > 0:
            out["last_px"] = float(last)
        if mid > 0:
            out["quote_mid_px"] = float(mid)
        return out

    def protective_offhours_stop_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategy = self._strategy
        config = getattr(strategy, "config", None)
        if config is None:
            return {}
        if not bool(payload.get("outside_rth")):
            return {}
        if "sl_offset_abs" not in payload:
            return {}
        profile = dict(getattr(config, "execution_profile", {}) or {})
        raw_order_type = str(
            profile.get("protection_offhours_stop_order_type")
            or "STP"
        ).strip().upper()
        order_type = "STP_LMT" if raw_order_type in {"STP_LMT", "STOP_LIMIT"} else "STP"
        extras: dict[str, Any] = {"sl_order_type": order_type}
        if order_type != "STP_LMT":
            return extras
        try:
            sl_offset_abs = abs(float(payload.get("sl_offset_abs") or 0.0))
        except Exception:
            sl_offset_abs = 0.0
        raw_limit_offset = profile.get("protection_offhours_sl_limit_offset_abs")
        try:
            limit_offset_abs = abs(float(raw_limit_offset)) if raw_limit_offset is not None else 0.0
        except Exception:
            limit_offset_abs = 0.0
        if limit_offset_abs <= 0.0:
            limit_offset_abs = max(0.02, min(0.25, sl_offset_abs * 0.5 if sl_offset_abs > 0.0 else 0.05))
        extras["sl_limit_offset_abs"] = float(limit_offset_abs)
        return extras

    def build_initial_position_state(
        self,
        *,
        signal_symbol: str,
        exec_symbol: str,
        stop_plan: dict[str, Any] | None,
        take_profit_plan: dict[str, Any] | None,
        retreat_plan: dict[str, Any] | None,
        protective_oco: dict[str, Any] | None,
    ) -> dict[str, Any]:
        strategy = self._strategy
        stop_payload = dict(stop_plan or {})
        tp_payload = dict(take_profit_plan or {})
        retreat_payload = dict(retreat_plan or {})
        oco_payload = dict(protective_oco or {})
        try:
            atr_eff = float(stop_payload.get("atr_eff") or tp_payload.get("atr_eff") or 0.0)
        except Exception:
            atr_eff = 0.0
        try:
            hard_stop_offset = abs(float(stop_payload.get("logic_stop_offset") or oco_payload.get("sl_offset_abs") or 0.0))
        except Exception:
            hard_stop_offset = 0.0
        try:
            target_offset = atr_eff * abs(float(tp_payload.get("target_offset_atr") or 0.0))
        except Exception:
            target_offset = 0.0
        extras = getattr(getattr(strategy, "params", None), "extras", {}) or {}
        return {
            "signal_symbol": str(signal_symbol or "").strip().upper() or None,
            "exec_symbol": str(exec_symbol or "").strip().upper() or None,
            "position_lifecycle": "pending_fill",
            "protection_lifecycle": "pending_attach" if oco_payload else "not_requested",
            "management_phase": "pre_tp1",
            "freeze_required": False,
            "degrade_reason": None,
            "entry_price": None,
            "entry_atr_used": atr_eff if atr_eff > 0.0 else None,
            "hard_stop_offset": hard_stop_offset if hard_stop_offset > 0.0 else None,
            "hard_stop_price": None,
            "active_stop_price": None,
            "target_offset": target_offset if target_offset > 0.0 else None,
            "target_price": None,
            "tp1_done": False,
            "tp1_exit_qty": None,
            "tp2_enabled": bool(extras.get("enable_tp2_lifecycle", False)),
            "tp2_done": False,
            "tp2_target_price": None,
            "pending_exit_reason": None,
            "pending_exit_qty": None,
            "trailing_armed": False,
            "trail_anchor_price": None,
            "trail_floor_price": None,
            "breakeven_armed": False,
            "breakeven_stop_price": None,
            "dynamic_stop_enabled": bool(extras.get("enable_dynamic_stop_widen", False)),
            "dynamic_stop_last_ts_epoch_s": None,
            "dynamic_stop_updates": 0,
            "retreat_enabled": bool(retreat_payload),
        }

    def on_entry_fill(
        self,
        *,
        lot: Any,
        fill_price: float | None,
        now_ts_epoch_s: float,
    ) -> dict[str, Any]:
        state = dict((getattr(lot, "extras", {}) or {}).get("protection_state") or {})
        stop_plan = dict((getattr(lot, "extras", {}) or {}).get("stop_plan") or {})
        tp_plan = dict((getattr(lot, "extras", {}) or {}).get("take_profit_plan") or {})
        side = str(getattr(lot, "side", "") or "").strip().upper()
        try:
            entry_px = float(fill_price) if fill_price is not None else float(getattr(lot, "avg_entry_price", 0.0) or 0.0)
        except Exception:
            entry_px = 0.0
        if entry_px <= 0.0:
            return {}
        try:
            hard_stop_offset = abs(float(state.get("hard_stop_offset") or stop_plan.get("logic_stop_offset") or 0.0))
        except Exception:
            hard_stop_offset = 0.0
        try:
            atr_eff = float(state.get("entry_atr_used") or stop_plan.get("atr_eff") or tp_plan.get("atr_eff") or 0.0)
        except Exception:
            atr_eff = 0.0
        try:
            target_offset = abs(float(state.get("target_offset") or 0.0))
        except Exception:
            target_offset = 0.0
        hard_stop_price = None
        if hard_stop_offset > 0.0:
            hard_stop_price = entry_px + hard_stop_offset if side == "SHORT" else entry_px - hard_stop_offset
        target_price = None
        if target_offset > 0.0:
            target_price = entry_px - target_offset if side == "SHORT" else entry_px + target_offset
        tp2_target_price = None
        if bool(state.get("tp2_enabled")) and target_offset > 0.0:
            tp2_offset = target_offset * 2.0
            tp2_target_price = entry_px - tp2_offset if side == "SHORT" else entry_px + tp2_offset
        return {
            "protection_state": {
                **state,
                "position_lifecycle": "live",
                "protection_lifecycle": "pending_attach" if not getattr(lot, "protection_order_refs", None) else "protected",
                "management_phase": "pre_tp1",
                "freeze_required": False,
                "degrade_reason": None,
                "entry_price": float(entry_px),
                "entry_atr_used": atr_eff if atr_eff > 0.0 else state.get("entry_atr_used"),
                "hard_stop_offset": hard_stop_offset if hard_stop_offset > 0.0 else state.get("hard_stop_offset"),
                "hard_stop_price": round(float(hard_stop_price), 4) if hard_stop_price is not None else state.get("hard_stop_price"),
                "active_stop_price": round(float(hard_stop_price), 4) if hard_stop_price is not None else state.get("active_stop_price"),
                "target_price": round(float(target_price), 4) if target_price is not None else state.get("target_price"),
                "tp2_target_price": round(float(tp2_target_price), 4) if tp2_target_price is not None else state.get("tp2_target_price"),
                "last_transition_ts_epoch_s": float(now_ts_epoch_s or 0.0),
            }
        }

    def on_exit_intent(
        self,
        *,
        lot: Any,
        exit_plan: ExitPlan,
        now_ts_epoch_s: float,
    ) -> dict[str, Any]:
        state = dict((getattr(lot, "extras", {}) or {}).get("protection_state") or {})
        order_role = str(getattr(exit_plan, "order_role", "") or "").strip().upper()
        reason = str(getattr(exit_plan, "payload", {}).get("reason") or order_role.lower())
        next_phase = state.get("management_phase")
        next_lifecycle = "pending_exit"
        if reason == "tp1_hit" or bool(getattr(exit_plan, "payload", {}).get("tp1_partial_exit")):
            next_phase = "tp1_pending"
        elif reason == "tp2_hit" or bool(getattr(exit_plan, "payload", {}).get("tp2_exit")):
            next_phase = "tp2_pending"
        elif reason in {"tp1_then_trailing_stop", "hard_stop_triggered", "logic_retreat"}:
            next_phase = "exit_pending"
        return {
            "protection_state": {
                **state,
                "position_lifecycle": next_lifecycle,
                "management_phase": next_phase,
                "protection_lifecycle": "degraded" if reason == "unsafe_position_without_protection" else state.get("protection_lifecycle"),
                "pending_exit_reason": reason,
                "pending_exit_qty": float(getattr(exit_plan, "qty", 0.0) or 0.0),
                "last_transition_ts_epoch_s": float(now_ts_epoch_s or 0.0),
            }
        }

    def on_exit_fill(
        self,
        *,
        lot: Any,
        fill_qty: float,
        fill_price: float | None,
        now_ts_epoch_s: float,
    ) -> dict[str, Any]:
        state = dict((getattr(lot, "extras", {}) or {}).get("protection_state") or {})
        if not state:
            return {}
        live_qty = float(getattr(lot, "live_qty", 0.0) or 0.0)
        reason = str(state.get("pending_exit_reason") or "")
        next_state = {
            **state,
            "pending_exit_reason": None,
            "pending_exit_qty": None,
            "last_fill_price": float(fill_price) if fill_price is not None else state.get("last_fill_price"),
            "last_transition_ts_epoch_s": float(now_ts_epoch_s or 0.0),
        }
        if reason == "tp2_hit":
            next_state["tp2_done"] = True
        if live_qty <= 0.0:
            next_state["position_lifecycle"] = "closed"
            next_state["management_phase"] = "closed"
            next_state["protection_lifecycle"] = "closed"
            next_state["freeze_required"] = False
            next_state["degrade_reason"] = None
        elif reason == "tp1_hit":
            next_state["position_lifecycle"] = "post_tp1"
            next_state["management_phase"] = "post_tp1"
            next_state["tp1_done"] = True
            next_state["tp1_exit_qty"] = float(fill_qty or 0.0)
            next_state["trailing_armed"] = True
            next_state["protection_lifecycle"] = "protected"
        elif reason == "tp2_hit":
            next_state["position_lifecycle"] = "post_tp2"
            next_state["management_phase"] = "post_tp2"
            next_state["protection_lifecycle"] = "protected"
        else:
            next_state["position_lifecycle"] = "live"
            next_state["management_phase"] = next_state.get("management_phase") or "pre_tp1"
        return {"protection_state": next_state}

    def mark_protection_attached(self, *, lot: Any, now_ts_epoch_s: float) -> dict[str, Any]:
        state = dict((getattr(lot, "extras", {}) or {}).get("protection_state") or {})
        if not state:
            return {}
        return {
            "protection_state": {
                **state,
                "protection_lifecycle": "protected",
                "freeze_required": False,
                "degrade_reason": None,
                "last_transition_ts_epoch_s": float(now_ts_epoch_s or 0.0),
            }
        }

    def mark_protection_missing(self, *, lot: Any, now_ts_epoch_s: float, escalate: bool) -> dict[str, Any]:
        state = dict((getattr(lot, "extras", {}) or {}).get("protection_state") or {})
        if not state:
            return {}
        return {
            "protection_state": {
                **state,
                "protection_lifecycle": "flatten_only" if escalate else "repair_pending",
                "degrade_reason": "unsafe_position_without_protection",
                "freeze_required": True,
                "last_transition_ts_epoch_s": float(now_ts_epoch_s or 0.0),
            }
        }

    def evaluate_live_management(self, *, lot: Any, snapshot, feature_context) -> dict[str, Any]:
        state = dict((getattr(lot, "extras", {}) or {}).get("protection_state") or {})
        if not state:
            return {"updates": {}, "exit_plan": None, "audit_reason": None, "audit_payload": {}}
        try:
            last_px = float(getattr(snapshot, "last_price", None) or 0.0)
        except Exception:
            last_px = 0.0
        if last_px <= 0.0:
            return {"updates": {}, "exit_plan": None, "audit_reason": None, "audit_payload": {}}
        side = str(getattr(lot, "side", "") or "").strip().upper()
        live_qty = float(getattr(lot, "live_qty", 0.0) or 0.0)
        entry_px = float(state.get("entry_price") or getattr(lot, "avg_entry_price", 0.0) or 0.0)
        if live_qty <= 0.0 or entry_px <= 0.0:
            return {"updates": {}, "exit_plan": None, "audit_reason": None, "audit_payload": {}}
        hard_stop_price = state.get("active_stop_price") or state.get("hard_stop_price")
        if hard_stop_triggered(side=side, stop_px=hard_stop_price, last_px=last_px):
            return {
                "updates": {},
                "exit_plan": ExitPlan(
                    symbol=str(getattr(lot, "symbol", "") or ""),
                    order_role="STOP",
                    qty=live_qty,
                    position_lot_id=str(getattr(lot, "position_lot_id", "") or ""),
                    lane_id=getattr(lot, "lane_id", None),
                    ownership_scope=str(getattr(lot, "ownership_scope", "strategy") or "strategy"),
                    payload={"reason": "hard_stop_triggered", "trigger_price": last_px},
                ),
                "audit_reason": "hard_stop_triggered",
                "audit_payload": {"position_lot_id": getattr(lot, "position_lot_id", None)},
            }
        target_price = state.get("target_price")
        if not bool(state.get("tp1_done")) and target_hit(side=side, target_px=target_price, last_px=last_px):
            tp1_qty = max(1.0, float(int(max(1.0, live_qty // 2))))
            if tp1_qty >= live_qty:
                tp1_qty = max(1.0, live_qty - 1.0)
            if tp1_qty > 0.0:
                return {
                    "updates": {},
                    "exit_plan": ExitPlan(
                        symbol=str(getattr(lot, "symbol", "") or ""),
                        order_role="TAKE_PROFIT",
                        qty=tp1_qty,
                        position_lot_id=str(getattr(lot, "position_lot_id", "") or ""),
                        lane_id=getattr(lot, "lane_id", None),
                        ownership_scope=str(getattr(lot, "ownership_scope", "strategy") or "strategy"),
                        payload={"reason": "tp1_hit", "tp1_partial_exit": True, "trigger_price": last_px},
                    ),
                    "audit_reason": "tp1_hit",
                    "audit_payload": {"position_lot_id": getattr(lot, "position_lot_id", None), "tp1_qty": tp1_qty},
                }
        if bool(state.get("tp1_done")) and bool(state.get("tp2_enabled")) and not bool(state.get("tp2_done")):
            tp2_target_price = state.get("tp2_target_price")
            if target_hit(side=side, target_px=tp2_target_price, last_px=last_px):
                return {
                    "updates": {},
                    "exit_plan": ExitPlan(
                        symbol=str(getattr(lot, "symbol", "") or ""),
                        order_role="TAKE_PROFIT",
                        qty=live_qty,
                        position_lot_id=str(getattr(lot, "position_lot_id", "") or ""),
                        lane_id=getattr(lot, "lane_id", None),
                        ownership_scope=str(getattr(lot, "ownership_scope", "strategy") or "strategy"),
                        payload={"reason": "tp2_hit", "tp2_exit": True, "trigger_price": last_px},
                    ),
                    "audit_reason": "tp2_hit",
                    "audit_payload": {"position_lot_id": getattr(lot, "position_lot_id", None), "tp2_qty": live_qty},
                }
        if bool(state.get("tp1_done")):
            trailing = trailing_stop_decision(
                side=side,
                last_px=last_px,
                entry_px=entry_px,
                entry_atr_used=state.get("entry_atr_used"),
                existing_trail_high_px=state.get("trail_anchor_price"),
                existing_trail_floor_px=state.get("trail_floor_price"),
            )
            if trailing is not None:
                updates = {
                    "protection_state": {
                        **state,
                        "trailing_armed": True,
                        "management_phase": "post_tp1_trailing",
                        "trail_anchor_price": float(trailing.updated_trail_high_px),
                        "trail_floor_price": trailing.updated_trail_floor_px,
                        "last_transition_ts_epoch_s": float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0),
                    }
                }
                if trailing.trailing_hit:
                    return {
                        "updates": updates,
                        "exit_plan": ExitPlan(
                            symbol=str(getattr(lot, "symbol", "") or ""),
                            order_role="FLATTEN",
                            qty=live_qty,
                            position_lot_id=str(getattr(lot, "position_lot_id", "") or ""),
                            lane_id=getattr(lot, "lane_id", None),
                            ownership_scope=str(getattr(lot, "ownership_scope", "strategy") or "strategy"),
                            payload={"reason": "tp1_then_trailing_stop", "trigger_price": last_px},
                        ),
                        "audit_reason": "tp1_then_trailing_stop",
                        "audit_payload": {"position_lot_id": getattr(lot, "position_lot_id", None)},
                    }
                return {
                    "updates": updates,
                    "exit_plan": None,
                    "audit_reason": "trailing_tracking",
                    "audit_payload": {
                        "position_lot_id": getattr(lot, "position_lot_id", None),
                        "trail_anchor_price": float(trailing.updated_trail_high_px),
                        "trail_floor_price": trailing.updated_trail_floor_px,
                    },
                }
        strategy_extras = getattr(getattr(self._strategy, "params", None), "extras", {}) or {}
        if not bool(state.get("breakeven_armed")) and bool(strategy_extras.get("enable_breakeven_shield", False)):
            new_stop = breakeven_stop_price(
                side=side,
                entry_px=entry_px,
                last_px=last_px,
                enable_breakeven_shield=True,
            )
            if new_stop is not None:
                return {
                    "updates": {
                        "protection_state": {
                            **state,
                            "breakeven_armed": True,
                            "breakeven_stop_price": float(new_stop),
                            "active_stop_price": float(new_stop),
                            "management_phase": "post_tp1_breakeven" if bool(state.get("tp1_done")) else "pre_tp1_breakeven",
                            "protection_lifecycle": "protected",
                            "last_transition_ts_epoch_s": float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0),
                        }
                    },
                    "exit_plan": None,
                    "audit_reason": "breakeven_shield",
                    "audit_payload": {"position_lot_id": getattr(lot, "position_lot_id", None), "new_stop_price": float(new_stop)},
                }
        if bool(state.get("dynamic_stop_enabled")) and not bool(state.get("breakeven_armed")):
            now_ts = float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0)
            last_update_ts = float(state.get("dynamic_stop_last_ts_epoch_s") or 0.0)
            if now_ts - last_update_ts >= 300.0:
                atr_live = None
                try:
                    atr_live = float(getattr(feature_context, "values", {}).get("atr_eff"))
                except Exception:
                    atr_live = None
                entry_atr = state.get("entry_atr_used")
                stop_price = state.get("active_stop_price") or state.get("hard_stop_price")
                if atr_live and entry_atr and stop_price:
                    stop_min_abs = float(strategy_extras.get("dynamic_stop_min_abs") or 0.50)
                    stop_atr_mult = float(strategy_extras.get("dynamic_stop_atr_mult") or 2.5)
                    dist_live = max(stop_atr_mult * float(atr_live), stop_min_abs)
                    stop_dist_pre = abs(float(entry_px) - float(state.get("hard_stop_price") or stop_price))
                    dist_final = max(stop_dist_pre, dist_live)
                    hard_cap = 1.5 * stop_dist_pre if stop_dist_pre > 0.0 else dist_final
                    dist_final = min(dist_final, hard_cap)
                    prev_dist = abs(float(entry_px) - float(stop_price))
                    if dist_final > (prev_dist + 0.005):
                        widened_stop = round(entry_px + dist_final, 4) if side == "SHORT" else round(entry_px - dist_final, 4)
                        return {
                            "updates": {
                                "protection_state": {
                                    **state,
                                    "active_stop_price": float(widened_stop),
                                    "management_phase": "dynamic_stop_adjusting",
                                    "dynamic_stop_last_ts_epoch_s": now_ts,
                                    "dynamic_stop_updates": int(state.get("dynamic_stop_updates") or 0) + 1,
                                    "last_transition_ts_epoch_s": now_ts,
                                }
                            },
                            "exit_plan": None,
                            "audit_reason": "dynamic_stop_widened",
                            "audit_payload": {"position_lot_id": getattr(lot, "position_lot_id", None), "new_stop_price": float(widened_stop)},
                        }
        return {"updates": {}, "exit_plan": None, "audit_reason": None, "audit_payload": {}}
