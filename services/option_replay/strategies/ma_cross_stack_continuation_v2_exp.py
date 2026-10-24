"""Experimental V2 MA-structure continuation strategy for replay-only A/B.

Design intent:
- Keep the same replay engine/execution and exit management as baseline.
- Entry side focuses on structure commonality learned from recent "best entry"
  review, not time-of-day slicing.
"""

from __future__ import annotations

import math
from typing import Any, Optional

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


class MACrossStackContinuationV2ExperimentalStrategy(OptionReplayStrategy):
    """V2 structure strategy:

    Core bear anchor:
    - close < MA200
    - close < VWAP
    - |close - MA10| / ATR <= 1.8  (anti tail-chase)

    Two bearish trigger morphologies:
    - Breakdown continuation: close <= MA10 and directional close quality.
    - Pullback-trap continuation: close > MA10 but expansion bar + controlled MA10 slope.
    """
    # Replay-only tunable guards (kept as class attrs for fast A/B via subclasses).
    max_dist_ma10_atr: float = 1.8
    max_close_pos_for_put: float = 1.0
    min_vol_ratio_for_put: float = 0.0
    max_ma10_slope_for_put: float = 999.0

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        if idx < 210:
            return None

        close = _safe_float(row.get("close"))
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        ma10 = _safe_float(row.get("ma10"))
        ma200 = _safe_float(row.get("ma200"))
        vwap = _safe_float(row.get("vwap_day"))
        atr = max(0.01, _safe_float(row.get("atr_14")))
        close_pos = _safe_float(row.get("close_pos"), 0.5)
        body = abs(_safe_float(row.get("body")))
        rng = max(1e-9, _safe_float(row.get("range")))
        body_frac = body / rng
        vol = _safe_float(row.get("volume"))
        vol_ma20 = max(1e-9, _safe_float(row.get("vol_ma20")))
        vol_ratio = vol / vol_ma20
        ma10_slope = _safe_float(row.get("ma10_slope"))

        if min(close, high, low, ma10, ma200, vwap, atr) <= 0:
            return None

        dist_ma10_atr = abs(close - ma10) / atr

        max_dist_ma10_atr = float(getattr(cfg, "v2_max_dist_ma10_atr", self.max_dist_ma10_atr))
        max_close_pos_for_put = float(getattr(cfg, "v2_put_close_pos_max", self.max_close_pos_for_put))
        min_vol_ratio_for_put = float(getattr(cfg, "v2_min_vol_ratio", self.min_vol_ratio_for_put))
        max_ma10_slope_for_put = float(getattr(cfg, "v2_max_ma10_slope", self.max_ma10_slope_for_put))

        # Shared bear anchor from commonality analysis.
        if not (close < ma200 and close < vwap and dist_ma10_atr <= max_dist_ma10_atr):
            return None

        if close_pos > max_close_pos_for_put:
            return None
        if vol_ratio < min_vol_ratio_for_put:
            return None
        if ma10_slope > max_ma10_slope_for_put:
            return None

        breakdown_cont = (close <= ma10) and ((1.0 - close_pos) >= 0.55)
        pullback_trap = (
            (close > ma10)
            and (body_frac >= 0.55)
            and (vol_ratio >= 0.80)
            and (ma10_slope <= 0.06)
        )
        if breakdown_cont or pullback_trap:
            return StrategySignal(
                side="PUT",
                regime="STACK_BEAR_CONT_V2",
                grade="A",
                high=high,
                low=low,
                atr=atr,
                cross_idx=None,
                signal_kind="ma_cross_stack_cont_v2",
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
        # Keep same risk model as baseline for apples-to-apples PnL/MDD comparison.
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
        # Reuse baseline 0324 management for a fair A/B.
        return MA10Reject0324Strategy.manage_position(self, engine, idx, row)


class MACrossStackContinuationV2ClosePosGuardExperimentalStrategy(MACrossStackContinuationV2ExperimentalStrategy):
    """V2 + bearish close quality guard."""

    max_close_pos_for_put = 0.35


class MACrossStackContinuationV2ClosePosVolGuardExperimentalStrategy(MACrossStackContinuationV2ExperimentalStrategy):
    """V2 + bearish close quality + volume guard."""

    max_close_pos_for_put = 0.35
    min_vol_ratio_for_put = 0.90


class MACrossStackContinuationV2ClosePosVolSlopeGuardExperimentalStrategy(MACrossStackContinuationV2ExperimentalStrategy):
    """V2 + close-pos + volume + slope guard."""

    max_close_pos_for_put = 0.35
    min_vol_ratio_for_put = 0.90
    max_ma10_slope_for_put = -0.03
