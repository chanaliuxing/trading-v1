"""Replay-only variant: filter fresh_momentum fake breakout confirmation failures."""

from __future__ import annotations

import pandas as pd

from services.option_replay.core.types import PendingSetup, ReplayConfig
from services.option_replay.strategies.ma10_reject_0324 import MA10Reject0324Strategy, _safe_float


class MA10Reject0324FreshMomentumFakeoutExperimentalStrategy(MA10Reject0324Strategy):
    """Keep baseline logic, only veto low-quality fresh_momentum confirms."""

    def confirm_signal(
        self,
        df: pd.DataFrame,
        idx: int,
        row: pd.Series,
        pending: PendingSetup,
        cfg: ReplayConfig,
    ) -> bool:
        base_ok = super().confirm_signal(df, idx, row, pending, cfg)
        if not base_ok:
            return False
        if pending.signal_kind != "fresh_momentum":
            return True
        if not bool(getattr(cfg, "fresh_momentum_fakeout_filter_enabled", False)):
            return True

        atr = max(0.01, float(pending.atr))
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        close = _safe_float(row.get("close"))
        body = _safe_float(row.get("body"))
        rng = max(1e-9, _safe_float(row.get("range")))
        upper_wick = _safe_float(row.get("upper_wick"))
        lower_wick = _safe_float(row.get("lower_wick"))
        vol = _safe_float(row.get("volume"))

        setup_vol = 0.0
        if 0 <= int(pending.setup_idx) < len(df):
            setup_vol = _safe_float(df.iloc[int(pending.setup_idx)].get("volume"))

        min_break_atr = float(getattr(cfg, "fresh_momentum_fakeout_min_break_atr", 0.10))
        min_close_break_atr = float(getattr(cfg, "fresh_momentum_fakeout_min_close_break_atr", 0.02))
        max_body_frac = float(getattr(cfg, "fresh_momentum_fakeout_max_body_frac", 0.35))
        min_adverse_wick_frac = float(getattr(cfg, "fresh_momentum_fakeout_min_adverse_wick_frac", 0.30))
        min_vol_vs_setup = float(getattr(cfg, "fresh_momentum_fakeout_min_vol_vs_setup", 1.00))

        body_frac = body / rng
        weak_body = body_frac <= max_body_frac
        no_vol_expand = setup_vol > 0 and vol < (setup_vol * min_vol_vs_setup)

        if pending.side == "CALL":
            break_atr = (high - pending.high) / atr
            close_break_atr = (close - pending.high) / atr
            weak_break = break_atr < min_break_atr or close_break_atr < min_close_break_atr
            adverse_wick = (upper_wick / rng) >= min_adverse_wick_frac
            swept_setup_other_side = low <= pending.low
        else:
            break_atr = (pending.low - low) / atr
            close_break_atr = (pending.low - close) / atr
            weak_break = break_atr < min_break_atr or close_break_atr < min_close_break_atr
            adverse_wick = (lower_wick / rng) >= min_adverse_wick_frac
            swept_setup_other_side = high >= pending.high

        # Veto only the explicit fake-breakout profile (fine-grained, not one-cut strictness):
        # weak break + weak candle, plus at least one failure clue.
        if weak_break and weak_body and (adverse_wick or no_vol_expand or swept_setup_other_side):
            return False
        return True


__all__ = ["MA10Reject0324FreshMomentumFakeoutExperimentalStrategy"]

