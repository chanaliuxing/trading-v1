"""Replay-only hybrid: layered_softstop base + oracle0327 fallback when base has no signal."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from services.option_replay.core.types import ReplayConfig, StrategySignal
from services.option_replay.strategies.ma10_reject_0324 import classify_signal_regime
from services.option_replay.strategies.ma10_reject_0324_oracle0327_fallback_exp import (
    detect_oracle0327_fallback_put,
)
from services.option_replay.strategies.ma10_reject_0324_trend_continuation_put_layered_softstop_exp import (
    MA10Reject0324TrendContinuationPutLayeredSoftStopExperimentalStrategy,
)


class MA10Reject0324LayeredPlusOracleFallbackExperimentalStrategy(
    MA10Reject0324TrendContinuationPutLayeredSoftStopExperimentalStrategy
):
    """Hybrid strategy for replay experiments."""

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        base_signal = super().detect_signal(df, idx, row, cfg)
        if base_signal is not None:
            return base_signal
        regime, _ = classify_signal_regime(df, idx)
        return detect_oracle0327_fallback_put(df, idx, row, regime, cfg)


__all__ = ["MA10Reject0324LayeredPlusOracleFallbackExperimentalStrategy"]

