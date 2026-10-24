"""Experimental replay-only extension of the 0324 MA10 strategy.

This keeps trend-continuation research outside the baseline 0324 plugin so the
shared base logic remains stable.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from services.option_replay.core.types import ReplayConfig, StrategySignal
from services.option_replay.strategies.ma10_reject_0324 import (
    MA10Reject0324Strategy,
    _passes_entry_quality_gate,
    _safe_float,
    classify_signal_regime,
    detect_daily_best_additive_setup,
    detect_fresh_momentum_setup,
    detect_setup,
)


def detect_trend_continuation_setup(
    df: pd.DataFrame,
    idx: int,
    row: pd.Series,
    cfg: ReplayConfig,
    regime: str,
) -> Optional[str]:
    if not bool(getattr(cfg, "enable_trend_continuation_branch", False)):
        return None
    if regime not in {"TREND_BULL", "TREND_BEAR"}:
        return None
    lookback = max(1, int(getattr(cfg, "trend_cont_breakout_lookback", 2)))
    if idx < lookback:
        return None

    open_px = _safe_float(row.get("open"))
    close_px = _safe_float(row.get("close"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    ma10 = _safe_float(row.get("ma10"))
    ma10_slope = _safe_float(row.get("ma10_slope"))
    vwap = _safe_float(row.get("vwap_day"))
    vol = _safe_float(row.get("volume"))
    vol_ma20 = _safe_float(row.get("vol_ma20"))
    close_pos = _safe_float(row.get("close_pos"), 0.5)
    body = _safe_float(row.get("body"))
    rng = max(1e-9, _safe_float(row.get("range")))
    if min(open_px, close_px, high, low, ma10, vwap, vol, vol_ma20) <= 0:
        return None
    if vol < (vol_ma20 * float(getattr(cfg, "trend_cont_volume_mult_min", 0.80))):
        return None
    if (body / rng) < float(getattr(cfg, "trend_cont_body_min_frac", 0.50)):
        return None

    prev = df.iloc[max(0, idx - lookback) : idx]
    prev_high = _safe_float(prev["high"].max()) if not prev.empty else 0.0
    prev_low = _safe_float(prev["low"].min()) if not prev.empty else 0.0
    if regime == "TREND_BULL":
        if ma10_slope <= 0:
            return None
        if not (close_px > ma10 and close_px > vwap):
            return None
        if close_pos < float(getattr(cfg, "trend_cont_call_close_pos_min", 0.65)):
            return None
        if prev_high > 0 and high <= prev_high:
            return None
        return "CALL"
    if ma10_slope >= 0:
        return None
    if not (close_px < ma10 and close_px < vwap):
        return None
    if close_pos > float(getattr(cfg, "trend_cont_put_close_pos_max", 0.35)):
        return None
    if prev_low > 0 and low >= prev_low:
        return None
    return "PUT"


class MA10Reject0324TrendContinuationExperimentalStrategy(MA10Reject0324Strategy):
    """Baseline 0324 strategy plus replay-only trend-continuation branch."""

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        regime, cross_idx = classify_signal_regime(df, idx)
        signal_kind = "ma10_reject"
        side = detect_setup(row, cfg, regime)
        if side is None:
            side = detect_fresh_momentum_setup(df, idx, row, cfg, regime, cross_idx)
            if side is not None:
                signal_kind = "fresh_momentum"
        if side is None:
            side = detect_daily_best_additive_setup(df, idx, row, cfg, regime, cross_idx)
            if side is not None:
                signal_kind = "daily_best_additive"
        if side is None:
            side = detect_trend_continuation_setup(df, idx, row, cfg, regime)
            if side is not None:
                signal_kind = "trend_continuation"
        if side is None:
            return None
        if not _passes_entry_quality_gate(row, side, cfg):
            return None
        grade = "A" if regime.startswith("FRESH_") else "B"
        if cross_idx is not None and signal_kind != "daily_best_additive":
            used = self.cross_reject_counts.get(cross_idx, 0) + 1
            self.cross_reject_counts[cross_idx] = used
            if used > 2:
                return None
        if signal_kind == "ma10_reject" and cross_idx is not None and cfg.ma10_reject_ext_atr_max > 0:
            cross_close = _safe_float(df.iloc[cross_idx].get("close"))
            close = _safe_float(row.get("close"))
            atr = max(0.01, _safe_float(row.get("atr_14")))
            if cross_close > 0 and abs(close - cross_close) / atr > cfg.ma10_reject_ext_atr_max:
                return None
        return StrategySignal(
            side=side,
            regime=regime,
            grade=grade,
            high=_safe_float(row.get("high")),
            low=_safe_float(row.get("low")),
            atr=max(0.01, _safe_float(row.get("atr_14"))),
            cross_idx=cross_idx,
            signal_kind=signal_kind,
        )
