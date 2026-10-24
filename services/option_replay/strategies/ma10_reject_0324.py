"""0324 MA10 reject strategy plugin."""

from __future__ import annotations

import math
from datetime import datetime, time
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from services.option_replay.core.types import OptionReplayStrategy, PendingSetup, ReplayConfig, StrategySignal


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


def is_fresh_cross_window(df: pd.DataFrame, idx: int, side: str) -> Tuple[bool, Optional[int], Optional[int]]:
    if idx <= 0:
        return False, None, None
    start = max(1, idx - 20)
    sub = df.iloc[start : idx + 1]
    prev_rel = (sub["ma10"] - sub["ma200"]).shift(1)
    rel = sub["ma10"] - sub["ma200"]
    if side == "CALL":
        crosses = sub[(prev_rel <= 0) & (rel > 0)]
    else:
        crosses = sub[(prev_rel >= 0) & (rel < 0)]
    if crosses.empty:
        return False, None, None
    cross_idx = int(crosses.index[-1])
    bars_since = idx - cross_idx
    return bars_since <= 20, cross_idx, bars_since


def classify_signal_regime(df: pd.DataFrame, idx: int) -> Tuple[str, Optional[int]]:
    row = df.iloc[idx]
    close = _safe_float(row.get("close"))
    vwap = _safe_float(row.get("vwap_day"))
    ma10 = _safe_float(row.get("ma10"))
    ma20 = _safe_float(row.get("ma20"))
    ma200 = _safe_float(row.get("ma200"))
    slope = _safe_float(row.get("ma10_slope"))
    if min(close, vwap, ma10, ma20, ma200) <= 0:
        return "NONE", None
    if ma10 > ma200 and close > vwap:
        ok, cross_idx, _ = is_fresh_cross_window(df, idx, "CALL")
        if ok:
            return "FRESH_BULL", cross_idx
    if ma10 < ma200 and close < vwap:
        ok, cross_idx, _ = is_fresh_cross_window(df, idx, "PUT")
        if ok:
            return "FRESH_BEAR", cross_idx
    if ma10 > ma20 and slope > 0 and close > vwap:
        return "TREND_BULL", None
    if ma10 < ma20 and slope < 0 and close < vwap:
        return "TREND_BEAR", None
    return "NONE", None


def detect_setup(row: pd.Series, cfg: ReplayConfig, regime: str) -> Optional[str]:
    if regime == "NONE":
        return None
    ma10 = _safe_float(row.get("ma10"))
    atr = _safe_float(row.get("atr_14"))
    vol = _safe_float(row.get("volume"))
    vol_ma = _safe_float(row.get("vol_ma20"))
    if min(ma10, atr, vol, vol_ma) <= 0:
        return None
    vol_ok = vol >= vol_ma * cfg.volume_mult_min
    if not vol_ok:
        return None
    body = _safe_float(row.get("body"))
    rng = max(_safe_float(row.get("range")), 1e-9)
    close_pos = _safe_float(row.get("close_pos"), 0.5)
    lower_wick = _safe_float(row.get("lower_wick"))
    upper_wick = _safe_float(row.get("upper_wick"))
    open_px = _safe_float(row.get("open"))
    close_px = _safe_float(row.get("close"))
    low = _safe_float(row.get("low"))
    high = _safe_float(row.get("high"))
    if regime in {"FRESH_BULL", "TREND_BULL"}:
        if low > ma10:
            return None
        if close_px <= ma10:
            return None
        if close_px < open_px and close_pos < 0.75:
            return None
        if close_pos < cfg.close_strength_min:
            return None
        if lower_wick < max(body, rng * cfg.lower_wick_min_frac):
            return None
        return "CALL"
    if regime in {"FRESH_BEAR", "TREND_BEAR"}:
        if high < ma10:
            return None
        if close_px >= ma10:
            return None
        if close_px > open_px and close_pos > 0.25:
            return None
        if close_pos > (1.0 - cfg.close_strength_min):
            return None
        if upper_wick < max(body, rng * cfg.upper_wick_min_frac):
            return None
        return "PUT"
    return None


def detect_fresh_momentum_setup(
    df: pd.DataFrame,
    idx: int,
    row: pd.Series,
    cfg: ReplayConfig,
    regime: str,
    cross_idx: Optional[int],
) -> Optional[str]:
    if not cfg.enable_fresh_momentum_branch:
        return None
    if regime not in {"FRESH_BULL", "FRESH_BEAR"} or cross_idx is None:
        return None
    if (idx - cross_idx) > int(cfg.momentum_max_bars_after_cross):
        return None
    if idx <= 0:
        return None
    prev = df.iloc[idx - 1]
    open_px = _safe_float(row.get("open"))
    close_px = _safe_float(row.get("close"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    ma10 = _safe_float(row.get("ma10"))
    vol = _safe_float(row.get("volume"))
    vol_ma20 = _safe_float(row.get("vol_ma20"))
    body = _safe_float(row.get("body"))
    rng = max(_safe_float(row.get("range")), 1e-9)
    if min(open_px, close_px, high, low, ma10, vol, vol_ma20) <= 0:
        return None
    if vol < (vol_ma20 * cfg.momentum_volume_mult_min):
        return None
    if (body / rng) < cfg.momentum_body_min_frac:
        return None
    prev_high = _safe_float(prev.get("high"))
    prev_low = _safe_float(prev.get("low"))
    if regime == "FRESH_BULL":
        if close_px <= open_px:
            return None
        if close_px <= ma10:
            return None
        if high <= prev_high or close_px <= prev_high:
            return None
        return "CALL"
    if close_px >= open_px:
        return None
    if close_px >= ma10:
        return None
    if low >= prev_low or close_px >= prev_low:
        return None
    return "PUT"


def detect_daily_best_additive_setup(
    df: pd.DataFrame,
    idx: int,
    row: pd.Series,
    cfg: ReplayConfig,
    regime: str,
    cross_idx: Optional[int],
) -> Optional[str]:
    if not bool(getattr(cfg, "enable_daily_best_additive_branch", False)):
        return None
    if regime not in {"FRESH_BULL", "FRESH_BEAR", "TREND_BULL", "TREND_BEAR"}:
        return None
    if regime.startswith("FRESH_"):
        if cross_idx is None:
            return None
        bars_since_cross = idx - cross_idx
        if bars_since_cross <= 0 or bars_since_cross > int(getattr(cfg, "daily_best_max_bars_after_cross", 8)):
            return None
    lookback = max(1, int(getattr(cfg, "daily_best_breakout_lookback", 3)))
    if idx < lookback:
        return None

    open_px = _safe_float(row.get("open"))
    close_px = _safe_float(row.get("close"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    ma10 = _safe_float(row.get("ma10"))
    atr = max(0.01, _safe_float(row.get("atr_14")))
    vwap = _safe_float(row.get("vwap_day"))
    vol = _safe_float(row.get("volume"))
    vol_ma20 = _safe_float(row.get("vol_ma20"))
    close_pos = _safe_float(row.get("close_pos"), 0.5)
    body = _safe_float(row.get("body"))
    rng = max(1e-9, _safe_float(row.get("range")))
    if min(open_px, close_px, high, low, ma10, vol, vol_ma20) <= 0:
        return None
    if vol < (vol_ma20 * float(getattr(cfg, "daily_best_volume_mult_min", 1.05))):
        return None
    if (body / rng) > float(getattr(cfg, "daily_best_max_body_frac", 0.90)):
        return None
    if abs(close_px - ma10) / atr > float(getattr(cfg, "daily_best_max_dist_ma10_atr", 1.20)):
        return None
    if vwap > 0 and abs(close_px - vwap) / atr < float(getattr(cfg, "daily_best_min_dist_vwap_atr", 0.0)):
        return None

    prev = df.iloc[max(0, idx - lookback) : idx]
    prev_high = _safe_float(prev["high"].max()) if not prev.empty else 0.0
    prev_low = _safe_float(prev["low"].min()) if not prev.empty else 0.0
    ma10_slope = _safe_float(row.get("ma10_slope"))
    dir_strength_min = float(getattr(cfg, "daily_best_dir_close_strength_min", 0.70))
    if regime in {"FRESH_BULL", "TREND_BULL"}:
        if ma10_slope <= 0:
            return None
        if close_px <= ma10:
            return None
        if close_px <= open_px:
            return None
        if close_pos < dir_strength_min:
            return None
        if prev_high > 0 and high <= prev_high:
            return None
        return "CALL"
    if bool(getattr(cfg, "daily_best_call_only", False)):
        return None
    if ma10_slope >= 0:
        return None
    if close_px >= ma10:
        return None
    if close_px >= open_px:
        return None
    if (1.0 - close_pos) < dir_strength_min:
        return None
    if prev_low > 0 and low >= prev_low:
        return None
    return "PUT"


def aggressive_entry_allowed(setup: PendingSetup, trigger_row: pd.Series, cfg: ReplayConfig) -> bool:
    if not cfg.allow_aggressive_same_day:
        return False
    if setup.grade != "A":
        return False
    if setup.reject_number != 1:
        return False
    t = pd.Timestamp(trigger_row["ts"]).to_pydatetime().time()
    if t > time(10, 30):
        return False
    trigger_range = _safe_float(trigger_row.get("high")) - _safe_float(trigger_row.get("low"))
    atr = max(setup.atr, 1e-9)
    vol_ratio = _safe_float(trigger_row.get("volume")) / max(_safe_float(trigger_row.get("vol_ma20")), 1e-9)
    return vol_ratio >= 1.5 and trigger_range >= 0.8 * atr


def _passes_entry_quality_gate(row: pd.Series, side: str, cfg: ReplayConfig) -> bool:
    if not cfg.enable_entry_quality_gate:
        return True
    atr = max(0.01, _safe_float(row.get("atr_14")))
    ma10 = _safe_float(row.get("ma10"))
    close = _safe_float(row.get("close"))
    body = _safe_float(row.get("body"))
    rng = max(1e-9, _safe_float(row.get("range")))
    close_pos = _safe_float(row.get("close_pos"), 0.5)

    if ma10 > 0 and abs(close - ma10) / atr > cfg.quality_max_dist_ma10_atr:
        return False
    if (body / rng) > cfg.quality_max_body_frac:
        return False
    dir_close_strength = close_pos if side == "CALL" else (1.0 - close_pos)
    if dir_close_strength < cfg.quality_min_dir_close_strength:
        return False
    return True


class MA10Reject0324Strategy(OptionReplayStrategy):
    """Current 0324 strategy implementation on top of the reusable single-leg replay skeleton."""

    def __init__(self) -> None:
        self.cross_reject_counts: Dict[int, int] = {}

    def on_new_day(self) -> None:
        self.cross_reject_counts = {}

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
            return high > (pending.high + cfg.breakout_buffer)
        return low < (pending.low - cfg.breakout_buffer)

    def build_pending_setup(self, signal: StrategySignal, ts: datetime, idx: int, row: pd.Series) -> PendingSetup:
        reject_number = None
        if signal.cross_idx is not None:
            reject_number = self.cross_reject_counts.get(signal.cross_idx)
        return PendingSetup(
            side=signal.side,
            regime=signal.regime,
            grade=signal.grade,
            setup_ts=ts,
            setup_idx=idx,
            reject_number=reject_number,
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
        if engine.position is None:
            return False
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        quote = engine._mark_quote(ts, engine.position.expiry, engine.position.strike, "C" if engine.position.side == "CALL" else "P")
        if quote is None:
            if ts.time() >= datetime.strptime(engine.cfg.final_flatten, "%H:%M").time():
                fallback = engine._fallback_exit_quote(ts, engine.position.side)
                if fallback is not None:
                    engine._close_position(ts, fallback, _safe_float(row.get("close")), engine.position.remaining, "EOD_FLATTEN_STALE_QUOTE")
                    return True
                engine._record_skip(ts, "missing_exit_quote_unresolved")
            return False
        engine.position.last_quote = quote

        close_px = _safe_float(row.get("close"))
        high_px = _safe_float(row.get("high"))
        low_px = _safe_float(row.get("low"))
        ma10 = _safe_float(row.get("ma10"))
        hold_bars = idx - engine.position.entry_idx

        if engine.position.side == "CALL":
            fav_move_r = (high_px - engine.position.entry_underlying) / max(engine.position.r_per_share, 1e-9)
        else:
            fav_move_r = (engine.position.entry_underlying - low_px) / max(engine.position.r_per_share, 1e-9)
        if fav_move_r > engine.position.best_fav_move_r:
            engine.position.best_fav_move_r = fav_move_r
            engine.position.best_fav_idx = idx

        if ts.time() >= datetime.strptime(engine.cfg.final_flatten, "%H:%M").time():
            engine._close_position(ts, quote, close_px, engine.position.remaining, "EOD_FLATTEN")
            return True

        if engine.position.side == "CALL" and low_px <= engine.position.structure_stop:
            engine._close_position(ts, quote, max(low_px, engine.position.structure_stop), engine.position.remaining, "STRUCTURE_STOP")
            return True
        if engine.position.side == "PUT" and high_px >= engine.position.structure_stop:
            engine._close_position(ts, quote, min(high_px, engine.position.structure_stop), engine.position.remaining, "STRUCTURE_STOP")
            return True

        if hold_bars >= 5:
            if engine.position.best_fav_move_r < 0.3 or (idx - engine.position.best_fav_idx) >= 5:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "SOFT_TIME_STOP")
                return True

        if hold_bars >= 10:
            engine._close_position(ts, quote, close_px, engine.position.remaining, "HARD_TIME_STOP")
            return True

        if engine.position.contracts >= 2 and not engine.position.partial_taken:
            hit_1r = close_px >= engine.position.target_1r if engine.position.side == "CALL" else close_px <= engine.position.target_1r
            if hit_1r:
                close_qty = max(1, engine.position.remaining // 2)
                engine._close_position(ts, quote, close_px, close_qty, "TP1_1R")
                if engine.position is not None:
                    engine.position.partial_taken = True
                return True

        if engine.position.contracts == 1:
            hit_1p2r = close_px >= engine.position.target_1p2r if engine.position.side == "CALL" else close_px <= engine.position.target_1p2r
            ma10_fail = close_px < ma10 if engine.position.side == "CALL" else close_px > ma10
            if hit_1p2r:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "TP_1P2R")
                return True
            if ma10 > 0 and ma10_fail:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "MA10_FAIL")
                return True

        if engine.position.contracts >= 2 and engine.position.partial_taken and ma10 > 0:
            ma10_fail = close_px < ma10 if engine.position.side == "CALL" else close_px > ma10
            if ma10_fail:
                engine._close_position(ts, quote, close_px, engine.position.remaining, "RUNNER_MA10_FAIL")
                return True
        return False


__all__ = [
    "MA10Reject0324Strategy",
    "aggressive_entry_allowed",
    "classify_signal_regime",
    "detect_setup",
]
