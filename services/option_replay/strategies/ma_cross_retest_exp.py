"""Experimental MA-cross retest continuation strategy for replay A/B.

Design goal:
- Capture the "cross then retest MA10 without structural break" idea.
- Keep execution/position management on the same replay skeleton for fair comparison.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Tuple

import pandas as pd

from services.option_replay.core.types import OptionReplayStrategy, PendingSetup, ReplayConfig, StrategySignal
from services.option_replay.strategies.ma10_reject_0324 import MA10Reject0324Strategy


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


def _recent_cross(df: pd.DataFrame, idx: int, side: str, lookback: int) -> Tuple[bool, Optional[int]]:
    if idx <= 0:
        return False, None
    start = max(1, idx - max(1, lookback))
    sub = df.iloc[start : idx + 1]
    prev_rel = (sub["ma10"] - sub["ma200"]).shift(1)
    rel = sub["ma10"] - sub["ma200"]
    if side == "CALL":
        crosses = sub[(prev_rel <= 0) & (rel > 0)]
    else:
        crosses = sub[(prev_rel >= 0) & (rel < 0)]
    if crosses.empty:
        return False, None
    cross_idx = int(crosses.index[-1])
    return True, cross_idx


class MACrossRetestExperimentalStrategy(OptionReplayStrategy):
    """Cross + MA10 retest-no-break continuation strategy."""

    cross_lookback_bars: int = 120
    min_vol_ratio: float = 0.60
    max_retest_break_atr: float = 0.35
    call_min_close_pos: float = 0.55
    put_max_close_pos: float = 0.55

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        if idx < 210:
            return None

        close = _safe_float(row.get("close"))
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        ma10 = _safe_float(row.get("ma10"))
        ma20 = _safe_float(row.get("ma20"))
        ma200 = _safe_float(row.get("ma200"))
        vwap = _safe_float(row.get("vwap_day"))
        atr = max(0.01, _safe_float(row.get("atr_14")))
        slope = _safe_float(row.get("ma10_slope"))
        vol = _safe_float(row.get("volume"))
        vol_ma20 = max(1e-9, _safe_float(row.get("vol_ma20")))
        vol_ratio = vol / vol_ma20
        close_pos = _safe_float(row.get("close_pos"), 0.5)
        if min(close, high, low, ma10, ma20, ma200, vwap) <= 0:
            return None
        if vol_ratio < self.min_vol_ratio:
            return None

        # CALL: bullish cross happened recently, trend still aligned, current bar retests MA10 and closes back above it.
        call_cross_ok, call_cross_idx = _recent_cross(df, idx, "CALL", self.cross_lookback_bars)
        if (
            call_cross_ok
            and ma10 > ma200
            and ma10 >= ma20
            and close > vwap
            and slope >= 0
            and low <= ma10
            and close > ma10
            and (ma10 - low) <= (self.max_retest_break_atr * atr)
            and close_pos >= self.call_min_close_pos
        ):
            return StrategySignal(
                side="CALL",
                regime="CROSS_RETEST_BULL",
                grade="A",
                high=high,
                low=low,
                atr=atr,
                cross_idx=call_cross_idx,
                signal_kind="ma_cross_retest",
            )

        # PUT: bearish cross happened recently, trend still aligned, current bar retests MA10 and closes back below it.
        put_cross_ok, put_cross_idx = _recent_cross(df, idx, "PUT", self.cross_lookback_bars)
        if (
            put_cross_ok
            and ma10 < ma200
            and ma10 <= ma20
            and close < vwap
            and slope <= 0
            and high >= ma10
            and close < ma10
            and (high - ma10) <= (self.max_retest_break_atr * atr)
            and close_pos <= self.put_max_close_pos
        ):
            return StrategySignal(
                side="PUT",
                regime="CROSS_RETEST_BEAR",
                grade="A",
                high=high,
                low=low,
                atr=atr,
                cross_idx=put_cross_idx,
                signal_kind="ma_cross_retest",
            )
        return None

    def confirm_signal(
        self,
        df: pd.DataFrame,
        idx: int,
        row: pd.Series,
        pending: PendingSetup,
        cfg: ReplayConfig,
    ) -> bool:
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        if pending.side == "CALL":
            return high > (pending.high + float(cfg.breakout_buffer))
        return low < (pending.low - float(cfg.breakout_buffer))

    def build_pending_setup(self, signal: StrategySignal, ts, idx: int, row: pd.Series) -> PendingSetup:
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
            signal_kind=signal.signal_kind,
        )

    def build_position_targets(
        self,
        df: pd.DataFrame,
        idx: int,
        trigger_row: pd.Series,
        setup: PendingSetup,
        trigger_price: float,
    ):
        # Keep same risk model as 0324 for apples-to-apples replay comparison.
        atr = max(setup.atr, 0.01)
        recent = df.iloc[max(0, idx - 3) : idx + 1]
        if setup.side == "CALL":
            swing_low = _safe_float(recent["low"].min())
            structure_stop = min(setup.low - 0.20 * atr, swing_low)
        else:
            swing_high = _safe_float(recent["high"].max())
            structure_stop = max(setup.high + 0.20 * atr, swing_high)
        r_per_share = abs(trigger_price - structure_stop)
        target_1r = trigger_price + r_per_share if setup.side == "CALL" else trigger_price - r_per_share
        target_1p2r = trigger_price + 1.2 * r_per_share if setup.side == "CALL" else trigger_price - 1.2 * r_per_share
        return structure_stop, target_1r, target_1p2r

    def manage_position(self, engine: "object", idx: int, row: pd.Series) -> bool:
        return MA10Reject0324Strategy.manage_position(self, engine, idx, row)
