"""
EntryGatekeeper Engine (QQQ V2)
Encapsulates all entry filters with 1:1 V1 Parity and Dynamic Parameter Reload.
"""
import json
import asyncio
from datetime import datetime, time
from typing import Any, Dict, Optional
from libs.strategy.components.market_fabric import MarketFabric
from libs.strategy.components.indicator_lib import IndicatorLib

class EntryGatekeeper:
    def __init__(self, ctx: Any, state: Dict[str, Any], fabric: MarketFabric, indicators: IndicatorLib):
        self.ctx = ctx
        self.state = state
        self.fabric = fabric
        self.indicators = indicators
        self.logger = ctx.log
        self._io_lock = asyncio.Lock()

    def check_time_window(self, now: datetime) -> bool:
        current_time = now.time()
        return time(9, 45) <= current_time <= time(15, 30)

    async def can_enter(self, now: datetime) -> tuple[bool, str, Optional[str], float]:
        """Main evaluation logic with full parity. Uses provided 'now'."""
        extras = self.ctx.params.extras or {}
        q_limit = float(extras.get("freshness_max_sec_qqq", 3.0))
        e_limit = float(extras.get("freshness_max_sec_qqew", 3.0))
        
        q_age = self.fabric.get_freshness_age("QQQ", now)
        if q_age > q_limit: return False, "stale_data_qqq", None, 0.0
        e_age = self.fabric.get_freshness_age("QQEW", now)
        if e_age > e_limit: return False, "stale_data_qqew", None, 0.0
        
        if not self.fabric.has_continuous_window("QQQ", 180, 5): return False, "data_gaps_detected_qqq", None, 0.0
        if not self.fabric.has_continuous_window("QQEW", 180, 5): return False, "data_gaps_detected_qqew", None, 0.0

        if self.state.get("data_integrity_warning", False): return False, "integrity_failure", None, 0.0
        if not self.state.get("seed_ready", False): return False, "seed_not_ready", None, 0.0

        qqq_last = self.fabric.get_last_price("QQQ")
        qqew_last = self.fabric.get_last_price("QQEW")
        qqq_vwap = self.fabric.get_anchored_vwap("QQQ")
        qqew_vwap = self.fabric.get_anchored_vwap("QQEW")
        v3m_signed = self.fabric.get_v3m("QQQ")
        v3m_price = abs(v3m_signed)
        
        cur_bar = self.fabric.cur_1m.get("QQQ")
        current_vol = cur_bar.volume if cur_bar else 0.0
        vol_series = self.fabric.today_vol_series.get("QQQ", [])
        cv_today = self.indicators.volume_cv(vol_series) or 0.0
        rolling_bars = self.fabric.get_last_n_bars("QQQ", "1m", 10)
        vol_ma10 = sum(b.volume for b in rolling_bars) / 10 if len(rolling_bars) >= 10 else 0
        
        vol_surge_ratio = 0.0
        if vol_ma10 > 0:
            dynamic_vol_limit = vol_ma10 * (1.0 + cv_today)
            vol_surge_ratio = current_vol / dynamic_vol_limit
            if current_vol < dynamic_vol_limit: return False, "insufficient_volume_surge", None, 0.0
        else: return False, "vol_ma10_not_ready", None, 0.0

        toxic_status = self.state.get("toxic_day", False)
        deadwater_status = self.state.get("deadwater", False)
        direction_hint = "LONG" if qqq_last > qqq_vwap else "SHORT"
        exec_symbol = "TQQQ" if direction_hint == "LONG" else "SQQQ"
        atr_live = self.indicators.atr(list(self.fabric.bars_5m[exec_symbol]))
        atr_pre = self.state.get("atr_pre", {}).get(exec_symbol, 0.35)
        
        if not self.check_time_window(now): return False, "outside_time_window", None, 0.0

        dev_threshold = float(extras.get("dev_threshold", 0.0010))
        actual_dev = abs(qqq_last - qqq_vwap) / qqq_vwap if qqq_vwap > 0 else 0.0
        if actual_dev < dev_threshold: return False, "insufficient_deviation", None, 0.0

        long_dir = qqq_last > qqq_vwap and qqew_last > qqew_vwap
        short_dir = qqq_last < qqq_vwap and qqew_last < qqew_vwap
        if not (long_dir or short_dir): return False, "direction_mismatch", None, 0.0
        
        direction = "LONG" if long_dir else "SHORT"
        orh, orl = self.state.get("orh"), self.state.get("orl")
        if orh and orl:
            if direction == "LONG" and qqq_last < orh: return False, "inside_opening_range_bullish", None, 0.0
            if direction == "SHORT" and qqq_last > orl: return False, "inside_opening_range_bearish", None, 0.0

        if atr_live is None: return False, "atr_not_ready", None, 0.0
        
        # V1 Port: Dynamic K & High Vol Guard (Live Params)
        vol_pulse_on = float(extras.get("vol_pulse_on", 1.6))
        high_vol_on = float(extras.get("high_vol_ratio", 2.2))
        k = 0.40; tv_floor = 0.20
        if vol_surge_ratio > vol_pulse_on: k = 0.30
        if vol_surge_ratio >= high_vol_on: k = 0.50; tv_floor = 0.60
            
        tv = max(k * min(atr_live, atr_pre * 1.8), tv_floor)
        await self.emit_diagnostic(now, qqq_last, qqew_last, qqq_vwap, qqew_vwap, v3m_signed, atr_live, atr_pre, vol_surge_ratio, toxic_status, deadwater_status, q_age, tv)

        if v3m_price < tv: return False, "velocity_too_low", None, 0.0
        if toxic_status: return False, "toxic_day_blocked", None, 0.0
        if deadwater_status: return False, "deadwater_blocked", None, 0.0

        return True, "entry_allowed", direction, qqq_last

    async def emit_diagnostic(self, now, q_last, e_last, q_vwap, e_vwap, v3m_s, atr_l, atr_p, vol_surge, toxic, deadwater, age, tv):
        payload = {
            "event": "heartbeat" if self.state.get("is_traded") else "entry_gate",
            "ts_et": now.isoformat(), "qqq_last": q_last, "qqew_last": e_last,
            "qqq_vwap": q_vwap, "qqew_vwap": e_vwap, "v3m_signed": v3m_s, "v3m_price": abs(v3m_s),
            "atr_live": atr_l, "atr_pre": atr_p, "vol_surge_ratio": vol_surge,
            "toxic_day": toxic, "deadwater": deadwater, "tv": round(tv, 4),
            "amp": self.state.get("amp", 0.0), "cross_count": self.state.get("cross_count", 0),
            "data_age_sec": round(age, 3), "is_traded": self.state.get("is_traded", False)
        }
        diag_path = self.state.get("_diag_path")
        if diag_path:
            from pathlib import Path
            rotated_path = Path(diag_path).parent / f"{Path(diag_path).stem}_{now.strftime('%Y%m%d')}{Path(diag_path).suffix}"
            async with self._io_lock:
                try:
                    with open(rotated_path, "a") as f: f.write(json.dumps(payload, separators=(",", ":")) + "\n")
                except Exception: pass
