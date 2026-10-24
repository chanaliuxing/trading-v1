from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from strategies.qqq_intraday_manage_logic import trailing_stop_decision

from .position_policy import PositionContext
from .risk_policy import payload_float
from .shared import best_fav_stats, find_entry_index, safe_float
from ..types import ExitPlan


@dataclass(frozen=True)
class ProfitDecision:
    exit_plan: Optional[ExitPlan]
    reason: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


class BaseProfitManagementPolicy(Protocol):
    policy_id: str

    def evaluate(self, ctx: PositionContext) -> ProfitDecision:
        ...


class NullProfitManagementPolicy:
    def __init__(self, policy_id: str = "null_profit_management") -> None:
        self.policy_id = policy_id

    def evaluate(self, ctx: PositionContext) -> ProfitDecision:
        return ProfitDecision(exit_plan=None)


class QQQIntradayProfitManagementPolicy:
    def __init__(self, policy_id: str = "qqq_intraday_profit_management") -> None:
        self.policy_id = policy_id

    def evaluate(self, ctx: PositionContext) -> ProfitDecision:
        runtime = ctx.runtime
        pos = runtime.active_position
        if pos is None:
            return ProfitDecision(exit_plan=None)

        extras = dict(ctx.extras or {})
        payload = dict(pos.payload or {})
        spot = float(runtime.spot or runtime.bar.close or 0.0)
        low_px = float(runtime.bar.low or spot)
        high_px = float(runtime.bar.high or spot)
        side = "LONG" if str(pos.side).upper() == "CALL" else "SHORT"
        last_px = float(spot)

        target_1r = float(pos.target_1r or 0.0)
        initial_qty = max(1.0, payload_float(payload, "initial_qty", pos.qty))
        partial_taken = float(pos.remaining_qty) < initial_qty
        if (not partial_taken) and float(pos.remaining_qty) >= 2.0 and target_1r > 0.0:
            hit_1r = spot >= target_1r if pos.side == "CALL" else spot <= target_1r
            if hit_1r:
                return ProfitDecision(
                    exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=min(1.0, float(pos.remaining_qty)), reason="UNDERLYING_TP1_1R"),
                    reason="underlying_tp1_1r",
                )

        trail_enabled_pre_tp1 = bool(extras.get("enable_trailing_before_tp1", False))
        if partial_taken or trail_enabled_pre_tp1:
            entry_underlying = payload_float(payload, "entry_underlying", 0.0)
            atr_eff = max(
                0.0,
                payload_float(payload, "entry_atr_used", 0.0),
                payload_float(payload.get("entry_plan_payload") or {}, "entry_atr_used", 0.0),
            )
            trail_decision = trailing_stop_decision(
                side=side,
                last_px=last_px,
                entry_px=(entry_underlying if entry_underlying > 0 else None),
                entry_atr_used=(atr_eff if atr_eff > 0 else None),
                existing_trail_high_px=payload.get("trail_high_px"),
                existing_trail_floor_px=payload.get("trail_floor_px"),
            )
            if trail_decision is not None and trail_decision.trailing_hit:
                return ProfitDecision(
                    exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_TRAILING_STOP"),
                    reason="underlying_trailing_stop",
                )

        target_1p2r = float(pos.target_1p2r or 0.0)
        if target_1p2r > 0.0:
            hit_1p2r = spot >= target_1p2r if pos.side == "CALL" else spot <= target_1p2r
            if hit_1p2r:
                return ProfitDecision(
                    exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_TP_FINAL"),
                    reason="underlying_tp_final",
                )

        opened_at = pos.opened_at
        held_sec = max(0.0, (runtime.now - opened_at).total_seconds())
        v3m = runtime.features.get("v3m", runtime.features.get("v3m_signed", runtime.bar.payload.get("v3m", 0.0)))
        try:
            v3m_f = float(v3m or 0.0)
        except Exception:
            v3m_f = 0.0
        atr_eff = max(
            0.0,
            payload_float(payload, "entry_atr_used", 0.0),
            payload_float(payload.get("entry_plan_payload") or {}, "entry_atr_used", 0.0),
        )
        retreat_mult = max(
            0.0,
            payload_float(payload, "logic_retreat_v3m_atr_mult", 0.10),
            payload_float(payload.get("entry_plan_payload") or {}, "logic_retreat_v3m_atr_mult", 0.10),
        )
        retreat_threshold = atr_eff * retreat_mult
        soft_min = max(0.0, float(extras.get("time_stop_soft_min", 0.0) or 0.0))
        hard_min = max(0.0, float(extras.get("time_stop_hard_min", 0.0) or 0.0))
        if hard_min > 0.0 and held_sec >= hard_min * 60.0:
            return ProfitDecision(
                exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_HARD_TIME_STOP"),
                reason="underlying_hard_time_stop",
            )
        if soft_min > 0.0 and held_sec >= soft_min * 60.0 and abs(v3m_f) <= max(0.0, retreat_threshold):
            return ProfitDecision(
                exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_SOFT_TIME_STOP"),
                reason="underlying_soft_time_stop",
            )
        return ProfitDecision(exit_plan=None)


class MARunnerProfitManagementPolicy:
    def __init__(self, policy_id: str = "ma_runner_profit_management") -> None:
        self.policy_id = policy_id

    def evaluate(self, ctx: PositionContext) -> ProfitDecision:
        runtime = ctx.runtime
        pos = runtime.active_position
        if pos is None:
            return ProfitDecision(exit_plan=None)
        history = list(runtime.payload.get("bar_history") or [])
        if not history:
            return ProfitDecision(exit_plan=None)
        extras = dict(ctx.extras or {})
        current = history[-1]
        close_px = safe_float(current.get("close"))
        ma10 = safe_float(current.get("ma10"))
        target_1r = float(pos.target_1r or 0.0)
        target_1p2r = float(pos.target_1p2r or 0.0)
        payload = dict(pos.payload or {})
        entry_underlying = safe_float(payload.get("entry_underlying"), runtime.spot)
        initial_qty = max(1.0, safe_float(payload.get("initial_qty"), pos.qty))
        partial_taken = bool(payload.get("partial_taken", False)) or float(pos.remaining_qty) < float(initial_qty)

        entry_idx = find_entry_index(history, pos.opened_at)
        if entry_idx is None:
            return ProfitDecision(exit_plan=None)
        hold_bars = max(0, len(history) - 1 - entry_idx)
        r_per_share = max(0.01, abs(entry_underlying - float(pos.payload.get("structure_stop", pos.structure_stop))))
        best_fav_move_r, best_fav_idx = best_fav_stats(
            history,
            entry_idx=entry_idx,
            side=str(pos.side),
            entry_underlying=entry_underlying,
            r_per_share=r_per_share,
        )

        if hold_bars >= 5:
            if best_fav_move_r < 0.3 or (len(history) - 1 - best_fav_idx) >= 5:
                return ProfitDecision(
                    exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="SOFT_TIME_STOP"),
                    reason="soft_time_stop",
                )

        if hold_bars >= 10:
            return ProfitDecision(
                exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="HARD_TIME_STOP"),
                reason="hard_time_stop",
            )

        if (not partial_taken) and float(pos.remaining_qty) >= 2.0 and target_1r > 0.0:
            hit_1r = close_px >= target_1r if pos.side == "CALL" else close_px <= target_1r
            if hit_1r:
                return ProfitDecision(
                    exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=min(2.0, float(pos.remaining_qty)), reason="TP1_1R"),
                    reason="tp1_1r",
                )

        if float(initial_qty) <= 1.0 and target_1p2r > 0.0:
            hit_1p2r = close_px >= target_1p2r if pos.side == "CALL" else close_px <= target_1p2r
            ma10_fail = close_px < ma10 if pos.side == "CALL" else close_px > ma10
            if hit_1p2r:
                return ProfitDecision(
                    exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="TP_1P2R"),
                    reason="tp_1p2r",
                )
            if ma10 > 0 and ma10_fail:
                return ProfitDecision(
                    exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="MA10_FAIL"),
                    reason="ma10_fail",
                )

        if partial_taken and ma10 > 0:
            ma10_fail = close_px < ma10 if pos.side == "CALL" else close_px > ma10
            if ma10_fail:
                return ProfitDecision(
                    exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="RUNNER_MA10_FAIL"),
                    reason="runner_ma10_fail",
                )
        return ProfitDecision(exit_plan=None)
