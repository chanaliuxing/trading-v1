from __future__ import annotations

from typing import Any, Dict, Optional

from services.option_replay.strategies import get_strategy as get_replay_strategy
from services.option_replay.strategies.ma10_reject_0324 import (
    classify_signal_regime,
    detect_daily_best_additive_setup,
    detect_fresh_momentum_setup,
    detect_setup,
)
from services.option_replay.strategies.ma10_reject_0324_trend_continuation_exp import detect_trend_continuation_setup
from strategies.options_kernel.types import EntryPlan, Signal
from strategies.options_plugins.ma_1dte_reject_shared import build_feature_frame_from_history, build_replay_cfg_from_extras, minute_floor, safe_float

from .shared import bars_since_setup, is_in_split_entry_window
from .signal import BaseSignalModule, SignalContext


class QQQMA10RejectSignalModule(BaseSignalModule):
    module_id = "ma10_reject_0324"

    def __init__(self, replay_strategy_id: str = "ma10_reject_0324") -> None:
        self.replay_strategy_id = str(replay_strategy_id).strip().lower() or "ma10_reject_0324"
        self._replay_strategy = get_replay_strategy(self.replay_strategy_id)
        self._pending_setup = None
        self._entry_plan: Optional[Dict[str, Any]] = None
        self._last_day = None
        self.last_diag: Dict[str, Any] = {}
        self.last_block: tuple[str, str] = ("", "")

    @staticmethod
    def _bool_flag(value: Any) -> bool:
        return bool(str(value or "").strip())

    @staticmethod
    def _reason(label: str, ok: bool) -> str:
        return "" if ok else label

    def _resolve_signal_block(self, diag: Dict[str, Any], signal: Optional[Any]) -> tuple[str, str]:
        if signal is not None:
            return "signal_ready", "SIGNAL_READY"
        regime = str(diag.get("regime") or "")
        if not regime or regime == "NONE":
            return "regime", "NO_REGIME"
        if self._bool_flag(diag.get("setup_side")):
            return "filter", "SETUP_FILTERED"
        if self._bool_flag(diag.get("fresh_momentum_side")):
            return "filter", "FRESH_MOMENTUM_FILTERED"
        if self._bool_flag(diag.get("daily_best_side")):
            return "filter", "DAILY_BEST_FILTERED"
        if self._bool_flag(diag.get("trend_cont_side")):
            return "filter", "TREND_CONT_FILTERED"
        return "setup", "NO_SETUP_MATCH"

    def _build_setup_diagnostics(self, row: Any, cfg: Any, regime: str, feature_frame: Any, idx: int, cross_idx: Optional[int]) -> Dict[str, str]:
        diag: Dict[str, str] = {}
        open_px = safe_float(row.get("open"))
        close_px = safe_float(row.get("close"))
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        ma10 = safe_float(row.get("ma10"))
        vwap = safe_float(row.get("vwap_day"))
        ma10_slope = safe_float(row.get("ma10_slope"))
        vol = safe_float(row.get("volume"))
        vol_ma20 = max(1e-9, safe_float(row.get("vol_ma20")))
        atr = max(1e-9, safe_float(row.get("atr_14")))
        close_pos = safe_float(row.get("close_pos"), 0.5)
        body = safe_float(row.get("body"))
        rng = max(1e-9, safe_float(row.get("range")))
        lower_wick = safe_float(row.get("lower_wick"))
        upper_wick = safe_float(row.get("upper_wick"))
        body_frac = body / rng
        vol_ratio = vol / vol_ma20

        setup_reasons: list[str] = []
        if regime == "NONE":
            setup_reasons.append("regime_none")
        elif min(ma10, atr, vol) <= 0:
            setup_reasons.append("core_feature_missing")
        else:
            setup_reasons.extend(filter(None, [
                self._reason("volume_below_min", vol_ratio >= float(cfg.volume_mult_min)),
                self._reason("bull_low_not_touch_ma10", regime not in {"FRESH_BULL", "TREND_BULL"} or low <= ma10),
                self._reason("bull_close_not_above_ma10", regime not in {"FRESH_BULL", "TREND_BULL"} or close_px > ma10),
                self._reason("bull_close_strength_weak", regime not in {"FRESH_BULL", "TREND_BULL"} or close_pos >= float(cfg.close_strength_min)),
                self._reason("bull_lower_wick_weak", regime not in {"FRESH_BULL", "TREND_BULL"} or lower_wick >= max(body, rng * float(cfg.lower_wick_min_frac))),
                self._reason("bear_high_not_touch_ma10", regime not in {"FRESH_BEAR", "TREND_BEAR"} or high >= ma10),
                self._reason("bear_close_not_below_ma10", regime not in {"FRESH_BEAR", "TREND_BEAR"} or close_px < ma10),
                self._reason("bear_close_strength_weak", regime not in {"FRESH_BEAR", "TREND_BEAR"} or close_pos <= (1.0 - float(cfg.close_strength_min))),
                self._reason("bear_upper_wick_weak", regime not in {"FRESH_BEAR", "TREND_BEAR"} or upper_wick >= max(body, rng * float(cfg.upper_wick_min_frac))),
            ]))
        diag["setup_failures"] = "|".join(setup_reasons[:6])

        fresh_reasons: list[str] = []
        prev_high = prev_low = 0.0
        if idx > 0 and len(feature_frame) > idx - 1:
            prev = feature_frame.iloc[idx - 1]
            prev_high = safe_float(prev.get("high"))
            prev_low = safe_float(prev.get("low"))
        if not bool(getattr(cfg, "enable_fresh_momentum_branch", False)):
            fresh_reasons.append("branch_disabled")
        elif regime not in {"FRESH_BULL", "FRESH_BEAR"}:
            fresh_reasons.append("regime_not_fresh")
        elif cross_idx is None:
            fresh_reasons.append("cross_idx_missing")
        else:
            bars_after_cross = idx - cross_idx
            fresh_reasons.extend(filter(None, [
                self._reason("cross_too_old", bars_after_cross <= int(cfg.momentum_max_bars_after_cross)),
                self._reason("momentum_volume_low", vol_ratio >= float(cfg.momentum_volume_mult_min)),
                self._reason("momentum_body_small", body_frac >= float(cfg.momentum_body_min_frac)),
                self._reason("fresh_bull_not_green", regime != "FRESH_BULL" or close_px > open_px),
                self._reason("fresh_bull_not_above_ma10", regime != "FRESH_BULL" or close_px > ma10),
                self._reason("fresh_bull_no_break_prev_high", regime != "FRESH_BULL" or (high > prev_high and close_px > prev_high)),
                self._reason("fresh_bear_not_red", regime != "FRESH_BEAR" or close_px < open_px),
                self._reason("fresh_bear_not_below_ma10", regime != "FRESH_BEAR" or close_px < ma10),
                self._reason("fresh_bear_no_break_prev_low", regime != "FRESH_BEAR" or (low < prev_low and close_px < prev_low)),
            ]))
        diag["fresh_failures"] = "|".join(fresh_reasons[:6])

        trend_reasons: list[str] = []
        lookback = max(1, int(getattr(cfg, "trend_cont_breakout_lookback", 2)))
        prev_high_tc = prev_low_tc = 0.0
        if idx >= lookback:
            prev_slice = feature_frame.iloc[max(0, idx - lookback):idx]
            if not prev_slice.empty:
                prev_high_tc = safe_float(prev_slice["high"].max())
                prev_low_tc = safe_float(prev_slice["low"].min())
        if not bool(getattr(cfg, "enable_trend_continuation_branch", False)):
            trend_reasons.append("branch_disabled")
        elif regime not in {"TREND_BULL", "TREND_BEAR"}:
            trend_reasons.append("regime_not_trend")
        else:
            trend_reasons.extend(filter(None, [
                self._reason("trend_volume_low", vol_ratio >= float(getattr(cfg, "trend_cont_volume_mult_min", 0.80))),
                self._reason("trend_body_small", body_frac >= float(getattr(cfg, "trend_cont_body_min_frac", 0.50))),
                self._reason("trend_bull_slope_nonpositive", regime != "TREND_BULL" or ma10_slope > 0),
                self._reason("trend_bull_not_above_ma10_vwap", regime != "TREND_BULL" or (close_px > ma10 and close_px > vwap)),
                self._reason("trend_bull_closepos_weak", regime != "TREND_BULL" or close_pos >= float(getattr(cfg, "trend_cont_call_close_pos_min", 0.65))),
                self._reason("trend_bull_no_break_prev_high", regime != "TREND_BULL" or prev_high_tc <= 0 or high > prev_high_tc),
                self._reason("trend_bear_slope_nonnegative", regime != "TREND_BEAR" or ma10_slope < 0),
                self._reason("trend_bear_not_below_ma10_vwap", regime != "TREND_BEAR" or (close_px < ma10 and close_px < vwap)),
                self._reason("trend_bear_closepos_weak", regime != "TREND_BEAR" or close_pos <= float(getattr(cfg, "trend_cont_put_close_pos_max", 0.35))),
                self._reason("trend_bear_no_break_prev_low", regime != "TREND_BEAR" or prev_low_tc <= 0 or low < prev_low_tc),
            ]))
        diag["trend_failures"] = "|".join(trend_reasons[:6])

        daily_reasons: list[str] = []
        if not bool(getattr(cfg, "enable_daily_best_additive_branch", False)):
            daily_reasons.append("branch_disabled")
        elif regime not in {"FRESH_BULL", "FRESH_BEAR", "TREND_BULL", "TREND_BEAR"}:
            daily_reasons.append("regime_not_supported")
        else:
            dist_ma10_atr = abs(close_px - ma10) / atr if ma10 > 0 else 999.0
            dist_vwap_atr = abs(close_px - vwap) / atr if vwap > 0 else 0.0
            daily_reasons.extend(filter(None, [
                self._reason("daily_volume_low", vol_ratio >= float(getattr(cfg, "daily_best_volume_mult_min", 1.05))),
                self._reason("daily_body_too_large", body_frac <= float(getattr(cfg, "daily_best_max_body_frac", 0.90))),
                self._reason("daily_too_far_from_ma10", dist_ma10_atr <= float(getattr(cfg, "daily_best_max_dist_ma10_atr", 1.20))),
                self._reason("daily_too_close_to_vwap", dist_vwap_atr >= float(getattr(cfg, "daily_best_min_dist_vwap_atr", 0.0))),
            ]))
        diag["daily_failures"] = "|".join(daily_reasons[:6])
        return diag

    def reset_for_new_day(self, ctx: SignalContext) -> None:
        day = ctx.runtime.now.date()
        if self._last_day != day:
            self._last_day = day
            self._pending_setup = None
            self._entry_plan = None
            self.last_diag = {}
            self.last_block = ("", "")
            self._replay_strategy.on_new_day()

    @staticmethod
    def _runtime_is_fresh(runtime: Any, extras: Dict[str, Any]) -> bool:
        wall_now = getattr(runtime, "wall_now", None)
        now = getattr(runtime, "now", None)
        if not hasattr(now, "date") or not hasattr(now, "timestamp"):
            return False
        if not hasattr(wall_now, "date") or not hasattr(wall_now, "timestamp"):
            return True
        if now.date() != wall_now.date():
            return False
        max_lag_sec = max(60, int(extras.get("max_runtime_bar_lag_sec", 600) or 600))
        lag_sec = (wall_now - now).total_seconds()
        return lag_sec <= float(max_lag_sec)

    def build_signal(self, ctx: SignalContext) -> Optional[Signal]:
        runtime = ctx.runtime
        extras = dict(ctx.extras or {})
        self.reset_for_new_day(ctx)
        if runtime.active_position is not None:
            self._pending_setup = None
            self.last_diag = {}
            self.last_block = ("position", "POSITION_OPEN")
            return None
        history = list(runtime.payload.get("bar_history") or [])
        if not self._runtime_is_fresh(runtime, extras):
            self._pending_setup = None
            self._entry_plan = None
            self.last_diag = {}
            self.last_block = ("data", "STALE_RUNTIME_BAR")
            return None
        if not is_in_split_entry_window(runtime.now, extras):
            self._pending_setup = None
            self._entry_plan = None
            self.last_diag = {}
            self.last_block = ("time", "ENTRY_WINDOW_CLOSED")
            return None
        feature_frame = build_feature_frame_from_history(runtime.symbol, history)
        if feature_frame.empty or len(feature_frame) < 210:
            self.last_diag = {}
            self.last_block = ("warmup", "WARMUP_NOT_READY")
            return None
        idx = len(feature_frame) - 1
        row = feature_frame.iloc[idx]
        cfg = build_replay_cfg_from_extras(extras)
        regime, cross_idx = classify_signal_regime(feature_frame, idx)
        diag = {
            "regime": regime,
            "setup_side": detect_setup(row, cfg, regime) or "",
            "fresh_momentum_side": detect_fresh_momentum_setup(feature_frame, idx, row, cfg, regime, cross_idx) or "",
            "daily_best_side": detect_daily_best_additive_setup(feature_frame, idx, row, cfg, regime, cross_idx) or "",
            "trend_cont_side": detect_trend_continuation_setup(feature_frame, idx, row, cfg, regime) or "",
        }
        diag.update(self._build_setup_diagnostics(row, cfg, regime, feature_frame, idx, cross_idx))
        self.last_diag = diag
        if self._pending_setup is not None:
            pending = self._pending_setup
            since = bars_since_setup(runtime.now, pending.setup_ts)
            if since <= 0:
                self.last_block = ("confirm", "WAIT_NEXT_BAR")
                return None
            if since > max(1, int(cfg.confirm_window_bars)):
                self._pending_setup = None
                self.last_block = ("confirm", "CONFIRM_WINDOW_EXPIRED")
                return None
            close_px = safe_float(row.get("close"))
            high_px = safe_float(row.get("high"))
            low_px = safe_float(row.get("low"))
            ma10 = safe_float(row.get("ma10"))
            if pending.side == "CALL":
                if low_px < pending.low - 1e-9 or (ma10 > 0 and close_px <= ma10):
                    self._pending_setup = None
                    self.last_block = ("confirm", "SETUP_INVALIDATED")
                    return None
            else:
                if high_px > pending.high + 1e-9 or (ma10 > 0 and close_px >= ma10):
                    self._pending_setup = None
                    self.last_block = ("confirm", "SETUP_INVALIDATED")
                    return None
            if not self._replay_strategy.confirm_signal(feature_frame, idx, row, pending, cfg):
                self.last_block = ("confirm", "CONFIRM_NOT_MET")
                return None
            structure_stop, target_1r, target_1p2r = self._replay_strategy.build_position_targets(feature_frame, idx, row, pending, close_px)
            self._entry_plan = {
                "signal_kind": str(pending.signal_kind),
                "structure_stop": float(structure_stop),
                "target_1r": float(target_1r),
                "target_1p2r": float(target_1p2r),
                "r_per_share": float(abs(close_px - structure_stop)),
                "initial_contracts": int(extras.get("contracts", 4) or 4),
                "side": str(pending.side),
            }
            self._pending_setup = None
            self.last_block = ("signal", "SIGNAL_READY")
            return Signal(
                ts=runtime.now,
                side=str(pending.side),
                signal_kind=str(pending.signal_kind),
                grade=str(pending.grade),
                regime=str(pending.regime),
                payload={"setup_idx": int(idx), "close": close_px, "stage": "confirm"},
            )

        signal = self._replay_strategy.detect_signal(feature_frame, idx, row, cfg)
        if signal is None:
            self.last_block = self._resolve_signal_block(diag, signal)
            return None
        self._pending_setup = self._replay_strategy.build_pending_setup(signal, minute_floor(runtime.now), idx, row)
        self.last_block = ("setup", "SETUP_READY_WAIT_CONFIRM")
        return None

    def build_entry_plan(self, ctx: SignalContext, signal: Signal) -> Optional[EntryPlan]:
        runtime = ctx.runtime
        extras = dict(ctx.extras or {})
        plan = dict(self._entry_plan or {})
        if not plan:
            return None
        self._entry_plan = None
        return EntryPlan(
            ts=runtime.now,
            side=str(plan.get("side") or signal.side),
            signal_kind=str(plan.get("signal_kind") or signal.signal_kind),
            structure_stop=float(plan.get("structure_stop") or 0.0),
            target_1r=float(plan.get("target_1r") or 0.0),
            target_1p2r=float(plan.get("target_1p2r") or 0.0),
            r_per_share=float(plan.get("r_per_share") or 0.0),
            contracts=int(plan.get("initial_contracts") or int(extras.get("contracts", 4) or 4)),
            payload={"stage": "pure_plugin"},
        )
