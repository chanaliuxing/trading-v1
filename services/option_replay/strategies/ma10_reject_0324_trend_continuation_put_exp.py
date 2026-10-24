"""Experimental replay-only bearish continuation extension of the 0324 strategy."""

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
    is_fresh_cross_window,
)
from services.option_replay.strategies.ma10_reject_0324_trend_continuation_exp import detect_trend_continuation_setup


def _put_structure_ok(df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> bool:
    if bool(getattr(cfg, "trend_cont_put_require_recent_cross", False)):
        ok, _, bars_since = is_fresh_cross_window(df, idx, "PUT")
        if not ok or bars_since is None:
            return False
        if bars_since > int(getattr(cfg, "trend_cont_put_recent_cross_max_bars", 8)):
            return False

    below_ma10_n = int(getattr(cfg, "trend_cont_put_consecutive_bars_below_ma10", 0) or 0)
    below_ma200_n = int(getattr(cfg, "trend_cont_put_consecutive_bars_below_ma200", 0) or 0)
    need_n = max(below_ma10_n, below_ma200_n)
    if need_n > 0:
        start = idx - need_n + 1
        if start < 0:
            return False
        recent = df.iloc[start : idx + 1]
        if len(recent) < need_n:
            return False
        if below_ma10_n > 0:
            recent_ma10 = recent.tail(below_ma10_n)
            if not bool((recent_ma10["close"] < recent_ma10["ma10"]).all()):
                return False
        if below_ma200_n > 0:
            recent_ma200 = recent.tail(below_ma200_n)
            if not bool((recent_ma200["close"] < recent_ma200["ma200"]).all()):
                return False
        max_high_over_ma10_atr = float(getattr(cfg, "trend_cont_put_max_high_over_ma10_atr", 0.0) or 0.0)
        if max_high_over_ma10_atr > 0:
            atr = max(0.01, _safe_float(row.get("atr_14")))
            recent_high = recent["high"].astype(float)
            recent_ma10_vals = recent["ma10"].astype(float)
            if bool(((recent_high - recent_ma10_vals) > (atr * max_high_over_ma10_atr)).any()):
                return False
    return True


class MA10Reject0324TrendContinuationPutExperimentalStrategy(MA10Reject0324Strategy):
    """Baseline 0324 strategy plus replay-only bearish continuation branch."""

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
        if side is None and regime == "TREND_BEAR":
            side = detect_trend_continuation_setup(df, idx, row, cfg, regime)
            if side == "PUT":
                if not _put_structure_ok(df, idx, row, cfg):
                    side = None
                else:
                    signal_kind = "trend_continuation_put"
            else:
                side = None
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
