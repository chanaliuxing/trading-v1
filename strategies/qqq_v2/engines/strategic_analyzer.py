"""
StrategicAnalyzer Engine (QQQ V2)
Calculates dynamic strategy parameters:
- Toxic-Day detection (VWAP crosses)
- Amplitude (AMP) locking at 09:45
- Dynamic thresholds (dev_threshold, target)
- Deadwater detection
"""
import asyncio
from datetime import datetime, time
from typing import Any, Dict, Optional, List
from zoneinfo import ZoneInfo
from libs.strategy.components.indicator_lib import IndicatorLib

ET = ZoneInfo("America/New_York")

class StrategicAnalyzer:
    def __init__(self, ctx: Any, state: Dict[str, Any], state_lock: asyncio.Lock, params: Any):
        self.ctx = ctx
        self.state = state
        self.state_lock = state_lock
        self.params = params
        self.logger = ctx.log
        
        # Load constants from extras (V1 Port)
        extras = params.extras or {}
        self.t_chop = float(extras.get("t_chop", 900.0))
        self.t_add = float(extras.get("t_add", 1500.0))
        self.t_v = float(extras.get("t_v", 120.0))
        self.v_limit = float(extras.get("v_limit", 20.0))
        self.ibkr_cooldown = float(extras.get("price_base_ibkr_retry_sec", 300.0))
        
        # Internal tracking (V1 non-persistent state)
        self._last_side: Optional[int] = None
        self._v_samples: List[float] = []
        self._last_ibkr_attempt = 0.0

        if "cross_count" not in self.state: self.state["cross_count"] = 0
        if "toxic_day" not in self.state: self.state["toxic_day"] = False
        if "amp" not in self.state: self.state["amp"] = 0.0
        if "deadwater" not in self.state: self.state["deadwater"] = False
        if "seed_ready" not in self.state: self.state["seed_ready"] = False
        if "orh" not in self.state: self.state["orh"] = None
        if "orl" not in self.state: self.state["orl"] = None

    def reset_daily(self) -> None:
        """Midnight reset for internal counters (V1 L1000)."""
        self._last_side = None
        self._v_samples = []
        self._last_ibkr_attempt = 0.0
        self.state["cross_count"] = 0
        self.state["toxic_day"] = False
        self.state["amp_locked"] = False
        self.state["deadwater"] = False
        self.state["seed_ready"] = False
        self.state["orh"] = None
        self.state["orl"] = None

    def can_attempt_ibkr(self, now_ts: float) -> bool:
        """Rate limit IBKR recovery requests (V1 L1430)."""
        if (now_ts - self._last_ibkr_attempt) < self.ibkr_cooldown:
            return False
        self._last_ibkr_attempt = now_ts
        return True

    def update_toxic_count(self, q_last: float, q_vwap: float) -> None:
        """Counts crossings and tracks ORH/ORL (09:30-09:45)."""
        if q_last <= 0 or q_vwap <= 0: return
        
        # 1. ORH/ORL Tracking
        self.state["orh"] = q_last if self.state["orh"] is None else max(self.state["orh"], q_last)
        self.state["orl"] = q_last if self.state["orl"] is None else min(self.state["orl"], q_last)

        # 2. Cross Counting
        upper, lower = q_vwap * 1.0008, q_vwap * 0.9992
        current_side = 1 if q_last > upper else (-1 if q_last < lower else 0)
        
        if current_side != 0 and self._last_side is not None and current_side != self._last_side:
            self.state["cross_count"] += 1
            if self.state["cross_count"] >= 3:
                self.state["toxic_day"] = True
                self.logger.warning("TOXIC_DAY_LOCKED", reason="cross_count_threshold_reached")
        if current_side != 0: self._last_side = current_side

    def collect_seed_sample(self, v3m: float) -> None:
        """Collects v3m samples for MAD-based seed quality check."""
        self._v_samples.append(v3m)

    def lock_at_0945(self, q_last: float, price_base: float) -> None:
        """V1 Port: Complete AMP locking and Seed Quality validation."""
        if price_base <= 0: return
        
        # 1. Seed Quality Check (MAD)
        sigma = IndicatorLib.mad_sigma(self._v_samples)
        if len(self._v_samples) >= 5 and sigma > 0 and self.state.get("orh") and self.state.get("orl"):
            self.state["seed_ready"] = True
            self.logger.info("SEED_QUALITY_OK", sigma=round(sigma, 4), samples=len(self._v_samples))
        else:
            self.state["seed_ready"] = False
            self.logger.error("SEED_QUALITY_FAILURE", samples=len(self._v_samples), sigma=sigma, orh=self.state.get("orh"))

        # 2. AMP Mapping (1:1 V1 Port - Live Params Support)
        amp = abs(q_last / price_base - 1.0)
        self.state["amp"] = amp; self.state["amp_locked"] = True
        
        if amp < 0.001:
            self.state["deadwater"] = True
            self.logger.warning("DEADWATER_LOCKED", amp=round(amp, 5)); return

        # V1 EXACT VALUES (Ratios for dev_threshold)
        if amp < 0.002: # 0.1% - 0.2%
            target, dev_threshold = 1.2, 0.0012
        elif amp < 0.0045: # 0.2% - 0.45%
            target, dev_threshold = 2.0, 0.0010
        else: # > 0.45%
            target, dev_threshold = 3.0, 0.0008
            
        self.state["target"] = target
        self.state["dev_threshold"] = dev_threshold
        self.logger.info("STRATEGIC_PARAMS_LOCKED", amp=round(amp, 5), target=target, dev=dev_threshold)
