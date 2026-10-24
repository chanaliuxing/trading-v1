"""Replay-only variant: add Mar-27 oracle commonality fallback (PUT-focused)."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from services.option_replay.core.types import ReplayConfig, StrategySignal
from services.option_replay.strategies.ma10_reject_0324 import (
    MA10Reject0324Strategy,
    _safe_float,
    classify_signal_regime,
)


def detect_oracle0327_fallback_put(
    df: pd.DataFrame,
    idx: int,
    row: pd.Series,
    regime: str,
    cfg: ReplayConfig,
) -> Optional[StrategySignal]:
    if regime not in {"TREND_BEAR", "FRESH_BEAR"}:
        return None
    if idx < 3:
        return None

    close = _safe_float(row.get("close"))
    open_px = _safe_float(row.get("open"))
    low = _safe_float(row.get("low"))
    ma10 = _safe_float(row.get("ma10"))
    ma200 = _safe_float(row.get("ma200"))
    vwap = _safe_float(row.get("vwap_day"))
    ma10_slope = _safe_float(row.get("ma10_slope"))
    body = _safe_float(row.get("body"))
    rng = max(1e-9, _safe_float(row.get("range")))
    close_pos = _safe_float(row.get("close_pos"), 0.5)
    vol = _safe_float(row.get("volume"))
    vol_ma20 = max(1e-9, _safe_float(row.get("vol_ma20")))
    atr = max(0.01, _safe_float(row.get("atr_14")))
    if min(close, open_px, low, ma10, ma200, vwap, vol) <= 0:
        return None

    # Mar-27 top-point commonality mapped into fallback rules:
    # - Directional close and body quality (avoid weak drift bars)
    # - Predominantly below MA/VWAP environment
    # - Real continuation break (new local low)
    body_frac = body / rng
    below_score = 0
    below_score += 1 if close < ma10 else 0
    below_score += 1 if close < ma200 else 0
    below_score += 1 if close < vwap else 0
    below_score += 1 if ma10_slope < 0 else 0

    prev = df.iloc[max(0, idx - 3) : idx]
    prev_low = _safe_float(prev["low"].min()) if not prev.empty else 0.0
    breakout_ok = prev_low > 0 and low < prev_low
    vol_ratio = vol / vol_ma20

    if close >= open_px:
        return None
    if close_pos > 0.35:
        return None
    if body_frac < 0.50:
        return None
    if below_score < 3:
        return None
    if not breakout_ok:
        return None
    if vol_ratio < 0.80:
        return None

    return StrategySignal(
        side="PUT",
        regime=regime,
        grade="B",
        high=_safe_float(row.get("high")),
        low=low,
        atr=atr,
        cross_idx=None,
        signal_kind="oracle0327_fallback_put",
    )


class MA10Reject0324Oracle0327FallbackExperimentalStrategy(MA10Reject0324Strategy):
    """Keep baseline behavior; if no signal, allow PUT fallback from Mar-27 commonality."""

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        base_signal = super().detect_signal(df, idx, row, cfg)
        if base_signal is not None:
            return base_signal
        regime, _ = classify_signal_regime(df, idx)
        return detect_oracle0327_fallback_put(df, idx, row, regime, cfg)


__all__ = [
    "MA10Reject0324Oracle0327FallbackExperimentalStrategy",
    "detect_oracle0327_fallback_put",
]
