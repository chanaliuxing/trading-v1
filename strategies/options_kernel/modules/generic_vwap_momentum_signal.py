from __future__ import annotations

from typing import Any, Optional

from .shared import is_in_split_entry_window
from .signal import BaseSignalModule, SignalContext
from ..types import EntryPlan, Signal


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return float(default)


class GenericVwapMomentumSignalModule(BaseSignalModule):
    def __init__(self, module_id: str = "generic_vwap_momentum_v1") -> None:
        self.module_id = module_id

    def reset_for_new_day(self, ctx: SignalContext) -> None:
        return None

    def build_signal(self, ctx: SignalContext) -> Optional[Signal]:
        runtime = ctx.runtime
        if runtime.active_position is not None or runtime.active_positions:
            return None
        extras = dict(ctx.extras or {})
        if not is_in_split_entry_window(runtime.now, extras):
            return None
        spot = _safe_float(runtime.spot or runtime.bar.close)
        vwap = _safe_float(runtime.features.get("vwap", runtime.bar.vwap), spot)
        v3m = _safe_float(runtime.features.get("v3m", runtime.bar.payload.get("v3m")), 0.0)
        min_abs_v3m = max(0.01, float(extras.get("signal_v3m_abs_min", 0.08) or 0.08))
        if spot <= 0.0 or vwap <= 0.0 or abs(v3m) < min_abs_v3m:
            return None
        side = "LONG" if (spot >= vwap and v3m > 0.0) else "SHORT" if (spot <= vwap and v3m < 0.0) else ""
        if not side:
            return None
        return Signal(
            ts=runtime.now,
            side=side,
            signal_kind="generic_vwap_momentum",
            regime="BULL" if side == "LONG" else "BEAR",
            payload={
                "signal_source": self.module_id,
                "spot": spot,
                "vwap": vwap,
                "v3m": v3m,
            },
        )

    def build_entry_plan(self, ctx: SignalContext, signal: Signal) -> Optional[EntryPlan]:
        runtime = ctx.runtime
        extras = dict(ctx.extras or {})
        spot = _safe_float(runtime.spot or runtime.bar.close)
        atr_eff = max(0.01, _safe_float(runtime.features.get("atr_eff", runtime.bar.atr), 1.0))
        hard_stop_atr_mult = max(0.1, float(extras.get("hard_stop_atr_mult", 0.7) or 0.7))
        target_1r_atr_mult = max(hard_stop_atr_mult, float(extras.get("target_1r_atr_mult", 0.9) or 0.9))
        target_1p2r_atr_mult = max(target_1r_atr_mult, float(extras.get("target_1p2r_atr_mult", 1.4) or 1.4))
        stop_offset = atr_eff * hard_stop_atr_mult
        if signal.side == "LONG":
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
            contracts=int(extras.get("contracts", 1) or 1),
            payload={
                **dict(signal.payload or {}),
                "entry_underlying": float(spot),
                "entry_atr_used": float(atr_eff),
                "logic_retreat_min_hold_sec": int(extras.get("logic_retreat_min_hold_sec", 180) or 180),
                "logic_retreat_v3m_atr_mult": float(extras.get("logic_retreat_v3m_atr_mult", 0.12) or 0.12),
            },
        )
