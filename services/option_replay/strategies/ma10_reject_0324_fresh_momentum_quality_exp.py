"""Replay-only variant: tighten fresh-momentum confirm with continuation quality gates."""

from __future__ import annotations

import pandas as pd

from services.option_replay.core.types import PendingSetup, ReplayConfig
from services.option_replay.strategies.ma10_reject_0324 import MA10Reject0324Strategy, _safe_float


class MA10Reject0324FreshMomentumQualityExperimentalStrategy(MA10Reject0324Strategy):
    """Baseline 0324 strategy + stricter confirm for fresh_momentum only."""

    def confirm_signal(
        self,
        df: pd.DataFrame,
        idx: int,
        row: pd.Series,
        pending: PendingSetup,
        cfg: ReplayConfig,
    ) -> bool:
        if pending.signal_kind != "fresh_momentum":
            return super().confirm_signal(df, idx, row, pending, cfg)
        if not bool(getattr(cfg, "fresh_momentum_confirm_quality_enabled", False)):
            return super().confirm_signal(df, idx, row, pending, cfg)

        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        close = _safe_float(row.get("close"))
        ma10 = _safe_float(row.get("ma10"))
        body = _safe_float(row.get("body"))
        rng = max(1e-9, _safe_float(row.get("range")))
        close_pos = _safe_float(row.get("close_pos"), 0.5)

        min_body_frac = float(getattr(cfg, "fresh_momentum_confirm_min_body_frac", 0.45))
        min_dir_close_strength = float(getattr(cfg, "fresh_momentum_confirm_min_dir_close_strength", 0.65))
        require_close_break = bool(getattr(cfg, "fresh_momentum_confirm_require_close_break", False))

        if (body / rng) < min_body_frac:
            return False

        if pending.side == "CALL":
            if high <= (pending.high + cfg.breakout_buffer):
                return False
            if close <= ma10:
                return False
            if close_pos < min_dir_close_strength:
                return False
            if require_close_break and close <= pending.high:
                return False
            return True

        if low >= (pending.low - cfg.breakout_buffer):
            return False
        if close >= ma10:
            return False
        if (1.0 - close_pos) < min_dir_close_strength:
            return False
        if require_close_break and close >= pending.low:
            return False
        return True


__all__ = ["MA10Reject0324FreshMomentumQualityExperimentalStrategy"]

