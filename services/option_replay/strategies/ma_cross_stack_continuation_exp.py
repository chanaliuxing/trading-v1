"""Experimental MA cross + stack continuation setup for replay-only comparison."""

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


def _ma5(df: pd.DataFrame, idx: int) -> float:
    if idx < 4:
        return 0.0
    win = df.iloc[idx - 4 : idx + 1]
    return _safe_float(win["close"].mean())


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


class MACrossStackContinuationExperimentalStrategy(OptionReplayStrategy):
    """Signal on MA10/MA200 cross followed by 2-bar continuation + MA stack alignment."""

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        if idx < 5:
            return None
        close = _safe_float(row.get("close"))
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        ma10 = _safe_float(row.get("ma10"))
        ma200 = _safe_float(row.get("ma200"))
        atr = max(0.01, _safe_float(row.get("atr_14")))
        ma5 = _ma5(df, idx)
        if min(close, high, low, ma10, ma200, ma5) <= 0:
            return None

        lookback = max(3, int(cfg.momentum_max_bars_after_cross))
        consecutive_bars = max(2, int(cfg.trend_cont_put_consecutive_bars_below_ma10 or 2))

        def _consecutive_below(i: int, n: int) -> bool:
            if i - n + 1 < 0:
                return False
            sub = df.iloc[i - n + 1 : i + 1]
            return bool(((sub["close"] < sub["ma10"]) & (sub["close"] < sub["ma200"])).all())

        def _consecutive_above(i: int, n: int) -> bool:
            if i - n + 1 < 0:
                return False
            sub = df.iloc[i - n + 1 : i + 1]
            return bool(((sub["close"] > sub["ma10"]) & (sub["close"] > sub["ma200"])).all())

        # Bear continuation: MA200 > MA10 > MA5, with two consecutive closes below MA10+MA200 after a fresh bearish cross.
        bear_cross_ok, cross_idx_put = _recent_cross(df, idx, "PUT", lookback)
        if (
            bear_cross_ok
            and ma200 > ma10 > ma5
            and _consecutive_below(idx, consecutive_bars)
        ):
            return StrategySignal(
                side="PUT",
                regime="STACK_BEAR_CONT",
                grade="A",
                high=high,
                low=low,
                atr=atr,
                cross_idx=cross_idx_put,
                signal_kind="ma_cross_stack_cont",
            )

        # Bull continuation: MA200 < MA10 < MA5, with two consecutive closes above MA10+MA200 after a fresh bullish cross.
        bull_cross_ok, cross_idx_call = _recent_cross(df, idx, "CALL", lookback)
        if (
            bull_cross_ok
            and ma200 < ma10 < ma5
            and _consecutive_above(idx, consecutive_bars)
        ):
            return StrategySignal(
                side="CALL",
                regime="STACK_BULL_CONT",
                grade="A",
                high=high,
                low=low,
                atr=atr,
                cross_idx=cross_idx_call,
                signal_kind="ma_cross_stack_cont",
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
        # Require a real breakout within confirm_window:
        # CALL: break above setup high; PUT: break below setup low.
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
        # Reuse proven 0324 position management to keep exits comparable in A/B tests.
        return MA10Reject0324Strategy.manage_position(self, engine, idx, row)
