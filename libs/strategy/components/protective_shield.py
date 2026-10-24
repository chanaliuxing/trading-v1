"""
ProtectiveShield Component
Implements industrial-grade stop-loss management with Tick Size precision.
"""
import asyncio
from typing import Any, Dict, Optional, Tuple
from datetime import datetime
from .base import StrategyComponent

class ProtectiveShield(StrategyComponent):
    def __init__(self, ctx: Any, state: Dict[str, Any], state_lock: asyncio.Lock,
                 stop_atr_mult: float = 2.5, 
                 stop_min_abs: float = 0.50,
                 widening_cap_mult: float = 1.5,
                 be_profit_trigger: float = 1.0,
                 be_offset: float = 0.20,
                 anti_suicide_buffer: float = 0.05):
        super().__init__(ctx, state, state_lock)
        self.stop_atr_mult = stop_atr_mult
        self.stop_min_abs = stop_min_abs
        self.widening_cap_mult = widening_cap_mult
        self.be_profit_trigger = be_profit_trigger
        self.be_offset = be_offset
        self.anti_suicide_buffer = anti_suicide_buffer

        keys = ["stop_px", "stop_dist_pre", "stop_dist_max", "stop_dist_cur", "breakeven_done", "entry_px"]
        for k in keys:
            if k not in self.state: self.state[k] = None
        if "breakeven_done" not in self.state or self.state["breakeven_done"] is None:
            self.state["breakeven_done"] = False

    async def initialize(self) -> None: pass

    def compute_initial_stop(self, entry_px: float, atr_pre: float) -> float:
        dist = max(self.stop_atr_mult * atr_pre, self.stop_min_abs)
        self.state["entry_px"] = entry_px
        self.state["stop_dist_pre"] = float(dist)
        self.state["stop_dist_max"] = float(dist) * self.widening_cap_mult
        self.state["stop_dist_cur"] = float(dist)
        # V1 Port: Strict Tick Size Alignment
        stop_px = round(entry_px - dist, 2)
        self.state["stop_px"] = stop_px
        return stop_px

    async def manage_stop(self, current_price: float, atr_live: Optional[float] = None) -> Tuple[Optional[float], Optional[str]]:
        entry_px = self.state.get("entry_px")
        if entry_px is None or self.state.get("stop_px") is None: return None, None

        if not self.state["breakeven_done"]:
            unrealized = current_price - entry_px
            if unrealized >= self.be_profit_trigger:
                # Keep breakeven shield deterministic and conservative: move to fixed offset.
                offset_candidate = self.be_offset
                # V1 Port: Tick Size Alignment
                new_stop = round(entry_px + offset_candidate, 2)
                if new_stop < current_price - self.anti_suicide_buffer:
                    self.state["stop_px"] = new_stop; self.state["breakeven_done"] = True
                    return new_stop, "breakeven_shield"

        if not self.state["breakeven_done"] and atr_live is not None:
            dist_live = max(self.stop_atr_mult * atr_live, self.stop_min_abs)
            dist_final = min(max(self.state["stop_dist_pre"], dist_live), self.state["stop_dist_max"])
            prev_dist = self.state.get("stop_dist_cur") or self.state["stop_dist_pre"]
            if dist_final > prev_dist + 0.005:
                self.state["stop_dist_cur"] = float(dist_final)
                # V1 Port: Tick Size Alignment
                new_stop = round(entry_px - dist_final, 2)
                self.state["stop_px"] = new_stop
                return new_stop, "atr_widening"
        return None, None
