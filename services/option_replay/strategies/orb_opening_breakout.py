"""Simple opening-range breakout strategy plugin for engine interface validation."""

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


class OpeningRangeBreakoutStrategy(OptionReplayStrategy):
    """A minimal second strategy to validate plugin portability."""

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        ts = pd.Timestamp(row["ts"]).to_pydatetime()
        if ts.time() < datetime.strptime("09:45", "%H:%M").time():
            return None
        day_mask = df["date"] == ts.date()
        day_df = df.loc[day_mask].reset_index(drop=True)
        day_idx = day_df.index[day_df["ts"] == pd.Timestamp(row["ts"])]
        if len(day_idx) == 0:
            return None
        rel_idx = int(day_idx[0])
        opening = day_df.iloc[:15]
        if len(opening) < 15:
            return None
        orb_high = _safe_float(opening["high"].max())
        orb_low = _safe_float(opening["low"].min())
        close_px = _safe_float(row.get("close"))
        vwap = _safe_float(row.get("vwap_day"))
        atr = max(0.01, _safe_float(row.get("atr_14")))
        if close_px > orb_high and close_px > vwap:
            return StrategySignal(side="CALL", regime="ORB_BULL", grade="B", high=_safe_float(row.get("high")), low=_safe_float(row.get("low")), atr=atr)
        if close_px < orb_low and close_px < vwap:
            return StrategySignal(side="PUT", regime="ORB_BEAR", grade="B", high=_safe_float(row.get("high")), low=_safe_float(row.get("low")), atr=atr)
        return None

    def confirm_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, pending: PendingSetup, cfg: ReplayConfig) -> bool:
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        if pending.side == "CALL":
            return high > pending.high
        return low < pending.low

    def build_pending_setup(self, signal: StrategySignal, ts: datetime, idx: int, row: pd.Series) -> PendingSetup:
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
            volume_ratio=1.0,
        )

    def build_position_targets(
        self,
        df: pd.DataFrame,
        idx: int,
        trigger_row: pd.Series,
        setup: PendingSetup,
        trigger_price: float,
    ) -> Tuple[float, float, float]:
        atr = max(0.01, setup.atr)
        if setup.side == "CALL":
            structure_stop = trigger_price - atr
        else:
            structure_stop = trigger_price + atr
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
        if engine.position.side == "CALL" and close_px <= engine.position.structure_stop:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "STRUCTURE_STOP")
            return True
        if engine.position.side == "PUT" and close_px >= engine.position.structure_stop:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "STRUCTURE_STOP")
            return True
        hold_bars = idx - engine.position.entry_idx
        if hold_bars >= 5:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "HARD_TIME_STOP")
            return True
        return False


__all__ = ["OpeningRangeBreakoutStrategy"]
