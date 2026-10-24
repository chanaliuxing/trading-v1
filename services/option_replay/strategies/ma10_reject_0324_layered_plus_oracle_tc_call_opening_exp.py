"""Replay-only hybrid: layered+oracle base plus opening-window tc_call branch."""

from __future__ import annotations

from datetime import time
from typing import Optional

import pandas as pd

from services.option_replay.core.types import ReplayConfig, StrategySignal
from services.option_replay.strategies.ma10_reject_0324 import _safe_float
from services.option_replay.strategies.ma10_reject_0324_layered_plus_oracle_fallback_exp import (
    MA10Reject0324LayeredPlusOracleFallbackExperimentalStrategy,
)


def _detect_opening_tc_call(
    df: pd.DataFrame,
    idx: int,
    row: pd.Series,
    cfg: ReplayConfig,
) -> Optional[StrategySignal]:
    if idx < 3:
        return None
    ts = pd.Timestamp(row.get("ts")).to_pydatetime().replace(second=0, microsecond=0)
    if ts.time() < time(9, 40) or ts.time() > time(10, 15):
        return None

    close_5m = _safe_float(row.get("close_5m"))
    ma10_5m = _safe_float(row.get("ma10_5m"))
    ma200_5m = _safe_float(row.get("ma200_5m"))
    ma10_5m_slope = _safe_float(row.get("ma10_5m_slope"))
    if min(close_5m, ma10_5m, ma200_5m) <= 0:
        return None
    if not (close_5m > ma10_5m > ma200_5m):
        return None
    if ma10_5m_slope <= 0:
        return None

    open_px = _safe_float(row.get("open"))
    close_px = _safe_float(row.get("close"))
    high = _safe_float(row.get("high"))
    ma10 = _safe_float(row.get("ma10"))
    vwap = _safe_float(row.get("vwap_day"))
    ma10_slope = _safe_float(row.get("ma10_slope"))
    vol = _safe_float(row.get("volume"))
    vol_ma20 = _safe_float(row.get("vol_ma20"))
    close_pos = _safe_float(row.get("close_pos"), 0.5)
    body = _safe_float(row.get("body"))
    rng = max(1e-9, _safe_float(row.get("range")))
    atr = max(0.01, _safe_float(row.get("atr_14")))
    if min(open_px, close_px, high, ma10, vwap, vol, vol_ma20) <= 0:
        return None

    if vol < (vol_ma20 * float(getattr(cfg, "trend_cont_volume_mult_min", 0.80))):
        return None
    if (body / rng) < float(getattr(cfg, "trend_cont_body_min_frac", 0.50)):
        return None
    if ma10_slope <= 0:
        return None
    if not (close_px > ma10 and close_px > vwap):
        return None
    if close_pos < float(getattr(cfg, "trend_cont_call_close_pos_min", 0.65)):
        return None

    lookback = max(1, int(getattr(cfg, "trend_cont_breakout_lookback", 3)))
    prev = df.iloc[max(0, idx - lookback) : idx]
    prev_high = _safe_float(prev["high"].max()) if not prev.empty else 0.0
    if prev_high > 0 and high <= prev_high:
        return None

    return StrategySignal(
        side="CALL",
        regime="TREND_BULL",
        grade="B",
        high=high,
        low=_safe_float(row.get("low")),
        atr=atr,
        cross_idx=None,
        signal_kind="trend_continuation_call_opening",
    )


class MA10Reject0324LayeredPlusOracleTCcallOpeningExperimentalStrategy(
    MA10Reject0324LayeredPlusOracleFallbackExperimentalStrategy
):
    """Keep layered+oracle behavior; add opening-window tc_call continuation branch."""

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        base_signal = super().detect_signal(df, idx, row, cfg)
        if base_signal is not None:
            return base_signal
        return _detect_opening_tc_call(df, idx, row, cfg)


__all__ = ["MA10Reject0324LayeredPlusOracleTCcallOpeningExperimentalStrategy"]

