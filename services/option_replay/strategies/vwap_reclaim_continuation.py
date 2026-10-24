"""VWAP reclaim continuation strategy plugin for interface validation."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional, Tuple

import pandas as pd

from services.option_replay.core.types import OptionReplayStrategy, PendingSetup, ReplayConfig, StrategySignal


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


class VWAPReclaimContinuationStrategy(OptionReplayStrategy):
    """Simple trend continuation strategy using VWAP reclaim/loss and next-bar confirmation."""

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        if idx <= 0:
            return None
        ts = pd.Timestamp(row["ts"]).to_pydatetime()
        if ts.time() < datetime.strptime("09:45", "%H:%M").time():
            return None
        prev = df.iloc[idx - 1]
        close_px = _safe_float(row.get("close"))
        open_px = _safe_float(row.get("open"))
        vwap = _safe_float(row.get("vwap_day"))
        prev_close = _safe_float(prev.get("close"))
        prev_vwap = _safe_float(prev.get("vwap_day"))
        ma10 = _safe_float(row.get("ma10"))
        vol = _safe_float(row.get("volume"))
        vol_ma = _safe_float(row.get("vol_ma20"))
        atr = max(0.01, _safe_float(row.get("atr_14")))
        if min(vwap, ma10, vol, vol_ma) <= 0:
            return None
        if prev_close <= prev_vwap and close_px > vwap and close_px > ma10 and close_px >= open_px and vol >= vol_ma:
            return StrategySignal(
                side="CALL",
                regime="VWAP_RECLAIM_BULL",
                grade="B",
                high=_safe_float(row.get("high")),
                low=_safe_float(row.get("low")),
                atr=atr,
            )
        if prev_close >= prev_vwap and close_px < vwap and close_px < ma10 and close_px <= open_px and vol >= vol_ma:
            return StrategySignal(
                side="PUT",
                regime="VWAP_LOSS_BEAR",
                grade="B",
                high=_safe_float(row.get("high")),
                low=_safe_float(row.get("low")),
                atr=atr,
            )
        return None

    def confirm_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, pending: PendingSetup, cfg: ReplayConfig) -> bool:
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        if pending.side == "CALL":
            return high > pending.high + cfg.breakout_buffer
        return low < pending.low - cfg.breakout_buffer

    def build_pending_setup(self, signal: StrategySignal, ts: datetime, idx: int, row: pd.Series) -> PendingSetup:
        vol_ma = max(_safe_float(row.get("vol_ma20")), 1e-9)
        return PendingSetup(
            side=signal.side,
            regime=signal.regime,
            grade=signal.grade,
            setup_ts=ts,
            setup_idx=idx,
            reject_number=None,
            high=signal.high,
            low=signal.low,
            atr=signal.atr,
            volume_ratio=_safe_float(row.get("volume")) / vol_ma,
        )

    def build_position_targets(
        self,
        df: pd.DataFrame,
        idx: int,
        trigger_row: pd.Series,
        setup: PendingSetup,
        trigger_price: float,
    ) -> Tuple[float, float, float]:
        atr = max(setup.atr, 0.01)
        if setup.side == "CALL":
            structure_stop = min(setup.low, trigger_price - 0.8 * atr)
        else:
            structure_stop = max(setup.high, trigger_price + 0.8 * atr)
        r_per_share = abs(trigger_price - structure_stop)
        target_1r = trigger_price + r_per_share if setup.side == "CALL" else trigger_price - r_per_share
        target_1p2r = trigger_price + 1.2 * r_per_share if setup.side == "CALL" else trigger_price - 1.2 * r_per_share
        return structure_stop, target_1r, target_1p2r

    def manage_position(self, engine: "object", idx: int, row: pd.Series) -> bool:
        if engine.position is None:
            return False
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        quote = engine._mark_quote(ts, engine.position.expiry, engine.position.strike, "C" if engine.position.side == "CALL" else "P")
        if quote is None:
            return False
        engine.position.last_quote = quote
        close_px = _safe_float(row.get("close"))
        vwap = _safe_float(row.get("vwap_day"))
        if engine.position.side == "CALL" and close_px <= engine.position.structure_stop:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "STRUCTURE_STOP")
            return True
        if engine.position.side == "PUT" and close_px >= engine.position.structure_stop:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "STRUCTURE_STOP")
            return True
        if engine.position.side == "CALL" and close_px < vwap:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "VWAP_FAIL")
            return True
        if engine.position.side == "PUT" and close_px > vwap:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "VWAP_FAIL")
            return True
        hold_bars = idx - engine.position.entry_idx
        if hold_bars >= 8:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "HARD_TIME_STOP")
            return True
        return False


__all__ = ["VWAPReclaimContinuationStrategy"]
