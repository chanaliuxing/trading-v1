from __future__ import annotations

from typing import Any, Optional

from strategies.qqq_intraday_rules import QQQIntradayRuleEngine, load_entry_recipe

from .signal import BaseSignalModule, SignalContext
from ..types import EntryPlan, Signal


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return float(default)


class QQQIntradayStockSignalModule(BaseSignalModule):
    module_id = "qqq_intraday_stock_signal_v2"

    def __init__(self) -> None:
        self._engine = QQQIntradayRuleEngine()

    def reset_for_new_day(self, ctx: SignalContext) -> None:
        return None

    def _evaluate(self, ctx: SignalContext):
        extras = dict(ctx.extras or {})
        runtime = ctx.runtime
        extras.setdefault("same_strategy_pending_orders", runtime.payload.get("same_strategy_pending_orders", 0))
        extras.setdefault("foreign_position_qty", runtime.payload.get("foreign_position_qty", 0.0))
        extras.setdefault("pending_decision_sync", runtime.payload.get("pending_decision_sync", False))
        recipe = load_entry_recipe(extras)
        return self._engine.evaluate(runtime=runtime, extras=extras, recipe=recipe)

    def build_signal(self, ctx: SignalContext) -> Optional[Signal]:
        runtime = ctx.runtime
        if runtime.active_position is not None or runtime.active_positions:
            return None
        evaluation = self._evaluate(ctx)
        translation = evaluation.translation
        if not translation.allowed or translation.side not in {"CALL", "PUT"}:
            return None
        entry_ctx = evaluation.context
        return Signal(
            ts=runtime.now,
            side=str(translation.side),
            signal_kind="qqq_intraday_stock_signal",
            regime=str(translation.stock_side),
            payload={
                "signal_source": "qqq_intraday_rule_engine",
                "entry_model": str(entry_ctx.entry_model),
                "stock_signal_side": str(translation.stock_side),
                "gate_reason": str(evaluation.gate_reason),
                "v3m": float(entry_ctx.v3m_signed),
                "atr_live": float(entry_ctx.atr_live),
                "atr_seed": float(entry_ctx.atr_seed),
                "atr_eff": float(entry_ctx.atr_eff),
                "atr_cap": float(entry_ctx.atr_cap),
                "vwap": float(entry_ctx.vwap_now),
                "vol_surge_ratio": float(entry_ctx.vol_surge_ratio),
                "distance_from_vwap_atr": float(entry_ctx.distance_from_vwap_atr),
                "data_age_qqq_sec": entry_ctx.data_age_qqq_sec,
                "data_age_qqew_sec": entry_ctx.data_age_qqew_sec,
                "gate_payload": dict(evaluation.gate_payload or {}),
            },
        )

    def build_entry_plan(self, ctx: SignalContext, signal: Signal) -> Optional[EntryPlan]:
        runtime = ctx.runtime
        extras = dict(ctx.extras or {})
        evaluation = self._evaluate(ctx)
        entry_ctx = evaluation.context
        spot = _safe_float(entry_ctx.qqq_now)
        atr_eff = max(0.0, _safe_float(entry_ctx.atr_eff, 0.0))
        if spot <= 0.0 or atr_eff <= 0.0:
            return None

        hard_stop_atr_mult = max(0.1, float(extras.get("hard_stop_atr_mult", 0.75) or 0.75))
        target_1r_atr_mult = max(hard_stop_atr_mult, float(extras.get("target_1r_atr_mult", 0.75) or 0.75))
        target_1p2r_atr_mult = max(target_1r_atr_mult, float(extras.get("target_1p2r_atr_mult", 1.2) or 1.2))
        stop_offset = atr_eff * hard_stop_atr_mult
        if signal.side == "CALL":
            structure_stop = spot - stop_offset
            target_1r = spot + (atr_eff * target_1r_atr_mult)
            target_1p2r = spot + (atr_eff * target_1p2r_atr_mult)
        else:
            structure_stop = spot + stop_offset
            target_1r = spot - (atr_eff * target_1r_atr_mult)
            target_1p2r = spot - (atr_eff * target_1p2r_atr_mult)
        return EntryPlan(
            ts=runtime.now,
            side=str(signal.side),
            signal_kind=str(signal.signal_kind),
            structure_stop=float(structure_stop),
            target_1r=float(target_1r),
            target_1p2r=float(target_1p2r),
            r_per_share=float(stop_offset),
            contracts=int(extras.get("contracts", 2) or 2),
            payload={
                **dict(signal.payload or {}),
                "signal_source": "qqq_intraday_rule_engine",
                "entry_underlying": float(spot),
                "entry_atr_used": float(atr_eff),
                "logic_retreat_min_hold_sec": int(extras.get("logic_retreat_min_hold_sec", 360) or 360),
                "logic_retreat_v3m_atr_mult": float(extras.get("logic_retreat_v3m_atr_mult", 0.10) or 0.10),
                "entry_model": str(entry_ctx.entry_model),
            },
        )
