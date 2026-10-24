from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any, Dict, Optional, Protocol

from strategies.qqq_intraday_manage_logic import breakeven_stop_price

from .position_policy import PositionContext
from .shared import parse_hms
from ..types import ExitPlan


def payload_float(payload: dict, key: str, default: float = 0.0) -> float:
    try:
        value = payload.get(key, default)
        return float(value if value is not None else default)
    except Exception:
        return float(default)


@dataclass(frozen=True)
class RiskDecision:
    exit_plan: Optional[ExitPlan]
    reason: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


class BaseRiskPolicy(Protocol):
    policy_id: str

    def evaluate(self, ctx: PositionContext) -> RiskDecision:
        ...


class NullRiskPolicy:
    def __init__(self, policy_id: str = "null_risk_policy") -> None:
        self.policy_id = policy_id

    def evaluate(self, ctx: PositionContext) -> RiskDecision:
        return RiskDecision(exit_plan=None)


class QQQIntradayUnderlyingRiskPolicy:
    def __init__(self, policy_id: str = "qqq_intraday_underlying_stop") -> None:
        self.policy_id = policy_id

    def evaluate(self, ctx: PositionContext) -> RiskDecision:
        runtime = ctx.runtime
        pos = runtime.active_position
        if pos is None:
            return RiskDecision(exit_plan=None)

        extras = dict(ctx.extras or {})
        payload = dict(pos.payload or {})
        now = runtime.now
        spot = float(runtime.spot or runtime.bar.close or 0.0)
        low_px = float(runtime.bar.low or spot)
        high_px = float(runtime.bar.high or spot)
        side = "LONG" if str(pos.side).upper() == "CALL" else "SHORT"
        last_px = float(spot)

        structure_stop = float(pos.structure_stop or 0.0)
        if pos.side == "CALL" and structure_stop > 0.0 and low_px <= structure_stop:
            return RiskDecision(
                exit_plan=ExitPlan(ts=now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_HARD_STOP"),
                reason="underlying_hard_stop",
            )
        if pos.side == "PUT" and structure_stop > 0.0 and high_px >= structure_stop:
            return RiskDecision(
                exit_plan=ExitPlan(ts=now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_HARD_STOP"),
                reason="underlying_hard_stop",
            )

        opened_at = pos.opened_at
        held_sec = max(0.0, (now - opened_at).total_seconds())
        min_hold_sec = max(0, int(payload.get("logic_retreat_min_hold_sec") or extras.get("logic_retreat_min_hold_sec", 360) or 360))
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
        v3m = runtime.features.get("v3m", runtime.features.get("v3m_signed", runtime.bar.payload.get("v3m", 0.0)))
        try:
            v3m_f = float(v3m or 0.0)
        except Exception:
            v3m_f = 0.0
        retreat_threshold = atr_eff * retreat_mult

        max_primary_age_sec = max(1.0, float(extras.get("logic_retreat_max_primary_age_sec", 8.0) or 8.0))
        primary_age_sec_raw = runtime.features.get("data_age_qqq_sec", runtime.features.get("primary_age_sec"))
        try:
            primary_age_sec = float(primary_age_sec_raw) if primary_age_sec_raw is not None else None
        except Exception:
            primary_age_sec = None
        if held_sec >= float(min_hold_sec) and primary_age_sec is not None and primary_age_sec > max_primary_age_sec:
            return RiskDecision(
                exit_plan=ExitPlan(ts=now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_DATA_STALE_GUARD"),
                reason="underlying_data_stale_guard",
            )

        be_enabled = bool(extras.get("enable_breakeven_shield", True))
        entry_underlying = payload_float(payload, "entry_underlying", 0.0)
        be_stop = breakeven_stop_price(
            side=side,
            entry_px=(entry_underlying if entry_underlying > 0 else None),
            last_px=last_px,
            enable_breakeven_shield=be_enabled,
        )
        if be_stop is not None:
            if side == "LONG" and low_px <= float(be_stop):
                return RiskDecision(
                    exit_plan=ExitPlan(ts=now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_BREAKEVEN_SHIELD"),
                    reason="underlying_breakeven_shield",
                )
            if side == "SHORT" and high_px >= float(be_stop):
                return RiskDecision(
                    exit_plan=ExitPlan(ts=now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_BREAKEVEN_SHIELD"),
                    reason="underlying_breakeven_shield",
                )

        if held_sec >= float(min_hold_sec) and retreat_threshold > 0.0:
            if pos.side == "CALL" and v3m_f <= -retreat_threshold:
                return RiskDecision(
                    exit_plan=ExitPlan(ts=now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_LOGIC_RETREAT"),
                    reason="underlying_logic_retreat",
                )
            if pos.side == "PUT" and v3m_f >= retreat_threshold:
                return RiskDecision(
                    exit_plan=ExitPlan(ts=now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="UNDERLYING_LOGIC_RETREAT"),
                    reason="underlying_logic_retreat",
                )
        return RiskDecision(exit_plan=None)


class MARunnerRiskPolicy:
    def __init__(self, policy_id: str = "ma_runner_underlying_stop") -> None:
        self.policy_id = policy_id

    def evaluate(self, ctx: PositionContext) -> RiskDecision:
        runtime = ctx.runtime
        pos = runtime.active_position
        if pos is None:
            return RiskDecision(exit_plan=None)
        history = list(runtime.payload.get("bar_history") or [])
        if not history:
            return RiskDecision(exit_plan=None)
        current = history[-1]
        close_px = payload_float(current, "close")
        high_px = payload_float(current, "high", close_px)
        low_px = payload_float(current, "low", close_px)
        structure_stop = float(pos.structure_stop or 0.0)

        if pos.side == "CALL" and structure_stop > 0 and low_px <= structure_stop:
            return RiskDecision(
                exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="STRUCTURE_STOP"),
                reason="structure_stop",
            )
        if pos.side == "PUT" and structure_stop > 0 and high_px >= structure_stop:
            return RiskDecision(
                exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="STRUCTURE_STOP"),
                reason="structure_stop",
            )
        return RiskDecision(exit_plan=None)


class TimeCutoffFlattenRiskPolicy:
    def __init__(self, policy_id: str = "time_cutoff_flatten") -> None:
        self.policy_id = policy_id

    def evaluate(self, ctx: PositionContext) -> RiskDecision:
        runtime = ctx.runtime
        pos = runtime.active_position
        if pos is None:
            return RiskDecision(exit_plan=None)
        extras = dict(ctx.extras or {})
        force_flatten_time = parse_hms(extras.get("force_flatten_time", "15:50:00"), time(15, 50))
        if runtime.now.time() >= force_flatten_time:
            return RiskDecision(
                exit_plan=ExitPlan(ts=runtime.now, position_lot_id=str(pos.position_lot_id), qty=float(pos.remaining_qty), reason="EOD_FORCE_FLATTEN"),
                reason="eod_force_flatten",
            )
        return RiskDecision(exit_plan=None)
