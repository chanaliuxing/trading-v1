"""Unified CALL/PUT scoring experiment using MA stack context.

Replay-only experimental strategy:
- One strategy evaluates both directions each bar.
- Uses MA5/20/60/120 + VWAP + candle quality + volume quality.
- Reuses existing execution/position management skeleton.
"""

from __future__ import annotations

import math
from datetime import datetime
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


class UnifiedMAStackScoreExperimentalStrategy(OptionReplayStrategy):
    """Unified dual-direction scoring strategy for replay evaluation."""

    min_score: float = 4.0
    min_score_gap: float = 0.5

    def __init__(self) -> None:
        self._feature_ready = False

    def on_new_day(self) -> None:
        # Nothing day-specific; keep state hook for interface completeness.
        return None

    def _prepare_features(self, df: pd.DataFrame) -> None:
        if self._feature_ready:
            return
        if "ma5" not in df.columns:
            df["ma5"] = df["close"].rolling(5, min_periods=1).mean()
        if "ma60" not in df.columns:
            df["ma60"] = df["close"].rolling(60, min_periods=1).mean()
        if "ma120" not in df.columns:
            df["ma120"] = df["close"].rolling(120, min_periods=1).mean()
        if "ma20_slope" not in df.columns:
            df["ma20_slope"] = df["ma20"].diff().fillna(0.0)
        if "ma60_slope" not in df.columns:
            df["ma60_slope"] = df["ma60"].diff().fillna(0.0)
        if "body_frac" not in df.columns:
            rng = (df["high"] - df["low"]).abs().replace(0.0, 1e-9)
            body = (df["close"] - df["open"]).abs()
            df["body_frac"] = body / rng
        if "vol_ratio" not in df.columns:
            vol_ma = df.get("vol_ma20")
            if vol_ma is None:
                vol_ma = df["volume"].rolling(20, min_periods=1).mean()
                df["vol_ma20"] = vol_ma
            df["vol_ratio"] = df["volume"] / vol_ma.replace(0.0, 1e-9)
        self._feature_ready = True

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        if idx < 210:
            return None
        self._prepare_features(df)
        row_live = df.iloc[idx]

        close = _safe_float(row_live.get("close"))
        high = _safe_float(row_live.get("high"))
        low = _safe_float(row_live.get("low"))
        atr = max(0.01, _safe_float(row_live.get("atr_14")))
        ma5 = _safe_float(row_live.get("ma5"))
        ma20 = _safe_float(row_live.get("ma20"))
        ma60 = _safe_float(row_live.get("ma60"))
        ma120 = _safe_float(row_live.get("ma120"))
        vwap = _safe_float(row_live.get("vwap_day"))
        close_pos = _safe_float(row_live.get("close_pos"), 0.5)
        body_frac = _safe_float(row_live.get("body_frac"), 0.0)
        vol_ratio = _safe_float(row_live.get("vol_ratio"), 0.0)
        ma20_slope = _safe_float(row_live.get("ma20_slope"))
        ma60_slope = _safe_float(row_live.get("ma60_slope"))

        if min(close, high, low, ma5, ma20, ma60, ma120, vwap) <= 0:
            return None

        call_score = 0.0
        put_score = 0.0

        # Trend stack
        if ma5 > ma20 > ma60 > ma120:
            call_score += 2.0
        if ma5 < ma20 < ma60 < ma120:
            put_score += 2.0

        # Location
        if close > ma20 and close > vwap:
            call_score += 1.0
        if close < ma20 and close < vwap:
            put_score += 1.0

        # Slope quality
        if ma20_slope > 0 and ma60_slope > 0:
            call_score += 1.5
        if ma20_slope < 0 and ma60_slope < 0:
            put_score += 1.5

        # Candle quality
        if close_pos >= 0.55:
            call_score += 1.0
        if close_pos <= 0.45:
            put_score += 1.0
        if body_frac >= 0.35:
            call_score += 0.5
            put_score += 0.5
        if vol_ratio >= 0.70:
            call_score += 0.5
            put_score += 0.5

        if max(call_score, put_score) < self.min_score:
            return None
        if abs(call_score - put_score) < self.min_score_gap:
            return None

        if call_score > put_score:
            side = "CALL"
            regime = "UNIFIED_MA_STACK_BULL"
            grade = "A" if call_score >= (self.min_score + 1.0) else "B"
        else:
            side = "PUT"
            regime = "UNIFIED_MA_STACK_BEAR"
            grade = "A" if put_score >= (self.min_score + 1.0) else "B"

        return StrategySignal(
            side=side,
            regime=regime,
            grade=grade,
            high=high,
            low=low,
            atr=atr,
            cross_idx=None,
            signal_kind="unified_ma_stack_score",
        )

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
        close = _safe_float(row.get("close"))
        ma5 = _safe_float(row.get("ma5"))
        if pending.side == "CALL":
            return high > (pending.high + float(cfg.breakout_buffer)) and close >= ma5
        return low < (pending.low - float(cfg.breakout_buffer)) and close <= ma5

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
            volume_ratio=_safe_float(row.get("volume")) / max(_safe_float(row.get("vol_ma20")), 1e-9),
            signal_kind=signal.signal_kind,
        )

    def build_position_targets(
        self,
        df: pd.DataFrame,
        idx: int,
        trigger_row: pd.Series,
        setup: PendingSetup,
        trigger_price: float,
    ) -> Tuple[float, float, float]:
        # Keep identical risk model with 0324 strategy for fair A/B.
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
