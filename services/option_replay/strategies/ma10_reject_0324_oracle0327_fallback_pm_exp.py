"""Replay-only variant: baseline + PM-only oracle fallback (low-frequency)."""

from __future__ import annotations

from datetime import time
from typing import Optional

import pandas as pd

from services.option_replay.core.types import ReplayConfig, StrategySignal
from services.option_replay.strategies.ma10_reject_0324 import MA10Reject0324Strategy, classify_signal_regime
from services.option_replay.strategies.ma10_reject_0324_oracle0327_fallback_exp import detect_oracle0327_fallback_put


class MA10Reject0324Oracle0327FallbackPMExperimentalStrategy(MA10Reject0324Strategy):
    """Only enable oracle fallback in PM, with at most one fallback fill per day."""

    def __init__(self) -> None:
        super().__init__()
        self._day: Optional[str] = None
        self._fallback_used: bool = False

    def on_new_day(self) -> None:
        super().on_new_day()
        self._day = None
        self._fallback_used = False

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        ts = pd.Timestamp(row.get("ts"))
        day = ts.date().isoformat()
        if day != self._day:
            self._day = day
            self._fallback_used = False

        # Baseline first, unchanged.
        base_signal = super().detect_signal(df, idx, row, cfg)
        if base_signal is not None:
            return base_signal

        # PM-only fallback and only one fallback setup per day.
        if self._fallback_used:
            return None
        if ts.to_pydatetime().time() < time(12, 0):
            return None

        regime, _ = classify_signal_regime(df, idx)
        fb = detect_oracle0327_fallback_put(df, idx, row, regime, cfg)
        if fb is not None:
            self._fallback_used = True
        return fb


__all__ = ["MA10Reject0324Oracle0327FallbackPMExperimentalStrategy"]

