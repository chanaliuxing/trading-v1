"""Parameter store with hot-reload for V3.1 strategy."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ParamSet:
    breadth_off_a: float = 0.40
    breadth_off_b: float = 0.30
    breadth_ok: float = 0.55
    atr_stop_lane1: float = 1.2
    atr_stop_lane2: float = 0.8
    atr_buffer: float = 0.1
    r_target_1: float = 1.5
    r_target_2: float = 2.5
    trail_lane1_atr: float = 0.3
    trail_lane2_atr: float = 0.2
    no_progress_lane1_days: int = 10
    no_progress_lane2_days: int = 5
    no_progress_mfe_r: float = 1.5
    no_progress_range_atr: float = 1.0
    max_days_lane1: int = 18
    max_days_lane2: int = 8
    hedge_cost_ratio: float = 0.35
    wall_dist_hi: float = 0.07
    wall_dist_lo: float = 0.03
    risk_per_trade_pct: float = 0.005
    daily_risk_cap_pct: float = 0.015
    max_notional_cap: float = 100000.0
    min_liquidity_spread_bp: float = 50.0
    scarcity_target_signals_10d: int = 2
    watchlist_base: List[str] = field(default_factory=list)
    watchlist_extended: List[str] = field(default_factory=list)
    allow_lane2_in_off: bool = False
    lane3_enabled: bool = False
    lane2_weight_boost: float = 0.15
    pullback_band_atr: float = 0.5
    breakout_vol_mult: float = 1.2
    pullback_reclaim_bars_max: int = 4
    trail_buffer_lane1: float = 0.3
    trail_buffer_lane2: float = 0.2
    
    # Engine required params
    cadence_s: int = 5
    capital_limit_usd: float = 100000.0
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "breadth_off_a": self.breadth_off_a,
            "breadth_off_b": self.breadth_off_b,
            "breadth_ok": self.breadth_ok,
            "atr_stop_lane1": self.atr_stop_lane1,
            "atr_stop_lane2": self.atr_stop_lane2,
            "atr_buffer": self.atr_buffer,
            "r_target_1": self.r_target_1,
            "r_target_2": self.r_target_2,
            "trail_lane1_atr": self.trail_lane1_atr,
            "trail_lane2_atr": self.trail_lane2_atr,
            "no_progress_lane1_days": self.no_progress_lane1_days,
            "no_progress_lane2_days": self.no_progress_lane2_days,
            "no_progress_mfe_r": self.no_progress_mfe_r,
            "no_progress_range_atr": self.no_progress_range_atr,
            "max_days_lane1": self.max_days_lane1,
            "max_days_lane2": self.max_days_lane2,
            "hedge_cost_ratio": self.hedge_cost_ratio,
            "wall_dist_hi": self.wall_dist_hi,
            "wall_dist_lo": self.wall_dist_lo,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "daily_risk_cap_pct": self.daily_risk_cap_pct,
            "max_notional_cap": self.max_notional_cap,
            "min_liquidity_spread_bp": self.min_liquidity_spread_bp,
            "scarcity_target_signals_10d": self.scarcity_target_signals_10d,
            "watchlist_base": list(self.watchlist_base),
            "watchlist_extended": list(self.watchlist_extended),
            "allow_lane2_in_off": self.allow_lane2_in_off,
            "lane3_enabled": self.lane3_enabled,
            "lane2_weight_boost": self.lane2_weight_boost,
            "pullback_band_atr": self.pullback_band_atr,
            "breakout_vol_mult": self.breakout_vol_mult,
            "pullback_reclaim_bars_max": self.pullback_reclaim_bars_max,
            "trail_buffer_lane1": self.trail_buffer_lane1,
            "trail_buffer_lane2": self.trail_buffer_lane2,
            "cadence_s": self.cadence_s,
            "capital_limit_usd": self.capital_limit_usd,
            "extras": self.extras,
        }


class ParamsStore:
    def __init__(self, path: str, reload_sec: int = 30):
        self.path = path
        self.reload_sec = int(reload_sec)
        self._last_load_ts = 0.0
        self._mtime = 0.0
        self._params = ParamSet()

    def get(self) -> ParamSet:
        now = time.time()
        if now - self._last_load_ts >= self.reload_sec:
            self._reload_if_needed()
        return self._params

    def _reload_if_needed(self) -> None:
        self._last_load_ts = time.time()
        try:
            st = os.stat(self.path)
            if st.st_mtime <= self._mtime:
                return
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._params = self._from_dict(data)
            self._mtime = st.st_mtime
            logger.info("v3_1_params_reloaded", path=self.path)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("v3_1_params_reload_failed", error=str(exc), path=self.path)

    @staticmethod
    def _from_dict(data: Dict[str, Any]) -> ParamSet:
        base = ParamSet()
        for field_name in base.__dataclass_fields__:
            if field_name in data:
                setattr(base, field_name, data[field_name])
        return base
