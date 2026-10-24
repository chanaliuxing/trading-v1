"""Experimental V2 MA-structure continuation strategy with dual-direction entries.

Replay-only strategy:
- Keeps the same execution / position management framework.
- Extends v2 PUT logic with a symmetric CALL branch tuned from call-candidate stats.
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


class MACrossStackContinuationV2DualExperimentalStrategy(OptionReplayStrategy):
    """Dual-direction v2 structure continuation.

    PUT side keeps v2 behavior.
    CALL side adds mirrored structure with dedicated quality thresholds.
    """

    # Shared anti-chase guard retained only for observability / secondary risk.
    max_dist_ma10_atr: float = 1.8
    cross_retest_lookback_bars: int = 20
    retest_window_bars: int = 3
    retest_touch_tolerance_abs: float = 0.15
    enable_vwap_as_soft_filter: bool = False

    # PUT guards (same as v2 defaults).
    max_close_pos_for_put: float = 1.0
    min_vol_ratio_for_put: float = 0.0
    max_ma10_slope_for_put: float = 999.0
    min_close_pos_for_put_breakdown: float = 0.55
    min_body_frac_for_put_pullback: float = 0.55
    min_vol_ratio_for_put_pullback: float = 0.80
    max_ma10_slope_for_put_pullback: float = 0.06

    # CALL guards (from call-candidate portrait).
    min_close_pos_for_call_breakout: float = 0.70
    min_body_frac_for_call_pullback: float = 0.45
    min_vol_ratio_for_call_pullback: float = 0.90
    min_ma10_slope_for_call_pullback: float = 0.08

    # Supplemental momentum branch, evaluated only when cross+retest has no signal.
    enable_momentum_no_retest_dual: bool = False
    momentum_breakout_lookback_bars: int = 5
    momentum_higher_low_bars: int = 3
    momentum_lower_high_bars: int = 3
    momentum_ma10_slope_min_abs: float = 0.02
    momentum_close_pos_min_call: float = 0.70
    momentum_close_pos_max_put: float = 0.30
    momentum_body_frac_min: float = 0.50
    momentum_vol_ratio_min: float = 0.80
    momentum_dist_ma10_atr_max: float = 2.2

    @staticmethod
    def _recent_cross(df: pd.DataFrame, idx: int, side: str, lookback: int) -> bool:
        if idx <= 0:
            return False
        start = max(1, idx - max(1, int(lookback)) + 1)
        sub = df.iloc[start - 1 : idx + 1]
        rel = sub["ma10"] - sub["ma200"]
        prev_rel = rel.shift(1)
        if side == "PUT":
            mask = (prev_rel >= 0.0) & (rel < 0.0)
        else:
            mask = (prev_rel <= 0.0) & (rel > 0.0)
        return bool(mask.fillna(False).any())

    @staticmethod
    def _higher_lows(df: pd.DataFrame, idx: int, bars: int) -> bool:
        bars = max(2, int(bars))
        start = idx - bars + 1
        if start < 0:
            return False
        lows = pd.to_numeric(df.iloc[start : idx + 1]["low"], errors="coerce")
        if len(lows) < bars or lows.isna().any():
            return False
        return bool((lows.diff().iloc[1:] > 0.0).all())

    @staticmethod
    def _lower_highs(df: pd.DataFrame, idx: int, bars: int) -> bool:
        bars = max(2, int(bars))
        start = idx - bars + 1
        if start < 0:
            return False
        highs = pd.to_numeric(df.iloc[start : idx + 1]["high"], errors="coerce")
        if len(highs) < bars or highs.isna().any():
            return False
        return bool((highs.diff().iloc[1:] < 0.0).all())

    @staticmethod
    def _breaks_recent_high(df: pd.DataFrame, idx: int, lookback: int) -> bool:
        if idx <= 0:
            return False
        start = max(0, idx - max(1, int(lookback)))
        recent = pd.to_numeric(df.iloc[start:idx]["high"], errors="coerce")
        if recent.empty or recent.isna().all():
            return False
        current_high = _safe_float(df.iloc[idx].get("high"))
        return bool(current_high > float(recent.max()))

    @staticmethod
    def _breaks_recent_low(df: pd.DataFrame, idx: int, lookback: int) -> bool:
        if idx <= 0:
            return False
        start = max(0, idx - max(1, int(lookback)))
        recent = pd.to_numeric(df.iloc[start:idx]["low"], errors="coerce")
        if recent.empty or recent.isna().all():
            return False
        current_low = _safe_float(df.iloc[idx].get("low"))
        return bool(current_low < float(recent.min()))

    def describe_setup(self, df: pd.DataFrame, idx: int, row: pd.Series) -> dict[str, Any]:
        close = _safe_float(row.get("close"))
        open_px = _safe_float(row.get("open"))
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
        upper_wick = max(0.0, high - max(open_px, close))
        lower_wick = max(0.0, min(open_px, close) - low)
        upper_wick_frac = upper_wick / rng
        lower_wick_frac = lower_wick / rng
        abs_close_ma10 = abs(close - ma10)
        pct_close_ma10 = abs_close_ma10 / max(close, 1e-9)
        dist_ma10_atr = abs_close_ma10 / atr
        min_idx = max(0, idx - max(1, int(self.retest_window_bars)) + 1)
        recent = df.iloc[min_idx : idx + 1]
        recent_high = _safe_float(recent["high"].max()) if not recent.empty else high
        recent_low = _safe_float(recent["low"].min()) if not recent.empty else low
        tol = max(0.0, float(self.retest_touch_tolerance_abs))
        put_cross_recent = self._recent_cross(df, idx, "PUT", int(self.cross_retest_lookback_bars))
        call_cross_recent = self._recent_cross(df, idx, "CALL", int(self.cross_retest_lookback_bars))
        put_vwap_ok = (close < vwap) if bool(self.enable_vwap_as_soft_filter) else True
        call_vwap_ok = (close > vwap) if bool(self.enable_vwap_as_soft_filter) else True
        put_retest_touch = recent_high >= (ma10 - tol)
        call_retest_touch = recent_low <= (ma10 + tol)
        put_quality = (
            close <= ma10
            and (1.0 - close_pos) >= self.min_close_pos_for_put_breakdown
            and body_frac >= self.min_body_frac_for_put_pullback
            and vol_ratio >= self.min_vol_ratio_for_put
            and ma10_slope <= self.max_ma10_slope_for_put
            and upper_wick_frac >= 0.10
        )
        call_quality = (
            close >= ma10
            and close_pos >= self.min_close_pos_for_call_breakout
            and body_frac >= self.min_body_frac_for_call_pullback
            and vol_ratio >= self.min_vol_ratio_for_call_pullback
            and ma10_slope >= self.min_ma10_slope_for_call_pullback
            and lower_wick_frac >= 0.10
        )
        put_trigger = ""
        call_trigger = ""
        if ma10 < ma200 and put_cross_recent and put_retest_touch and put_vwap_ok and put_quality:
            put_trigger = "cross_retest_put"
        if ma10 > ma200 and call_cross_recent and call_retest_touch and call_vwap_ok and call_quality:
            call_trigger = "cross_retest_call"
        put_momentum_structure = (
            ma10 < ma200
            and close < vwap
            and ma10_slope <= -abs(float(self.momentum_ma10_slope_min_abs))
            and self._lower_highs(df, idx, int(self.momentum_lower_high_bars))
            and close_pos <= float(self.momentum_close_pos_max_put)
            and body_frac >= float(self.momentum_body_frac_min)
            and vol_ratio >= float(self.momentum_vol_ratio_min)
            and dist_ma10_atr <= float(self.momentum_dist_ma10_atr_max)
            and self._breaks_recent_low(df, idx, int(self.momentum_breakout_lookback_bars))
        )
        call_momentum_structure = (
            ma10 > ma200
            and close > vwap
            and ma10_slope >= abs(float(self.momentum_ma10_slope_min_abs))
            and self._higher_lows(df, idx, int(self.momentum_higher_low_bars))
            and close_pos >= float(self.momentum_close_pos_min_call)
            and body_frac >= float(self.momentum_body_frac_min)
            and vol_ratio >= float(self.momentum_vol_ratio_min)
            and dist_ma10_atr <= float(self.momentum_dist_ma10_atr_max)
            and self._breaks_recent_high(df, idx, int(self.momentum_breakout_lookback_bars))
        )
        if not put_trigger and not call_trigger and bool(self.enable_momentum_no_retest_dual):
            if put_momentum_structure:
                put_trigger = "momentum_no_retest_put"
            if call_momentum_structure:
                call_trigger = "momentum_no_retest_call"
        return {
            "put_trigger": put_trigger,
            "call_trigger": call_trigger,
            "put_cross_recent": bool(put_cross_recent),
            "call_cross_recent": bool(call_cross_recent),
            "put_retest_touch": bool(put_retest_touch),
            "call_retest_touch": bool(call_retest_touch),
            "put_vwap_ok": bool(put_vwap_ok),
            "call_vwap_ok": bool(call_vwap_ok),
            "close_lt_vwap": bool(close < vwap),
            "close_gt_vwap": bool(close > vwap),
            "ma10_lt_ma200": bool(ma10 < ma200),
            "ma10_gt_ma200": bool(ma10 > ma200),
            "abs_close_ma10": float(abs_close_ma10),
            "pct_close_ma10": float(pct_close_ma10),
            "dist_ma10_atr": float(dist_ma10_atr),
            "body_frac": float(body_frac),
            "vol_ratio": float(vol_ratio),
            "close_pos": float(close_pos),
            "upper_wick_frac": float(upper_wick_frac),
            "lower_wick_frac": float(lower_wick_frac),
            "put_momentum_structure": bool(put_momentum_structure),
            "call_momentum_structure": bool(call_momentum_structure),
        }

    def detect_signal(self, df: pd.DataFrame, idx: int, row: pd.Series, cfg: ReplayConfig) -> Optional[StrategySignal]:
        if idx < 210:
            return None

        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        atr = max(0.01, _safe_float(row.get("atr_14")))
        if min(high, low, atr) <= 0:
            return None
        setup = self.describe_setup(df, idx, row)
        if setup["put_trigger"]:
            signal_kind = "momentum_no_retest_dual" if setup["put_trigger"] == "momentum_no_retest_put" else "ma_cross_stack_cont_v2_dual"
            regime = "STACK_BEAR_MOMO_NO_RETEST_V2" if setup["put_trigger"] == "momentum_no_retest_put" else "STACK_BEAR_RETEST_V2"
            return StrategySignal(
                side="PUT",
                regime=regime,
                grade="A",
                high=high,
                low=low,
                atr=atr,
                cross_idx=None,
                signal_kind=signal_kind,
            )
        if setup["call_trigger"]:
            signal_kind = "momentum_no_retest_dual" if setup["call_trigger"] == "momentum_no_retest_call" else "ma_cross_stack_cont_v2_dual"
            regime = "STACK_BULL_MOMO_NO_RETEST_V2" if setup["call_trigger"] == "momentum_no_retest_call" else "STACK_BULL_RETEST_V2"
            return StrategySignal(
                side="CALL",
                regime=regime,
                grade="A",
                high=high,
                low=low,
                atr=atr,
                cross_idx=None,
                signal_kind=signal_kind,
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
        # Same stop/target model as baseline for fair A/B comparison.
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


__all__ = ["MACrossStackContinuationV2DualExperimentalStrategy"]
