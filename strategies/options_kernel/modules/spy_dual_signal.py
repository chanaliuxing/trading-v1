from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from services.option_replay.core.types import StrategySignal
from services.option_replay.strategies import get_strategy as get_replay_strategy
from services.option_replay.strategies.ma10_reject_0324 import classify_signal_regime
from strategies.options_kernel.types import EntryPlan, Signal
from strategies.options_plugins.ma_1dte_reject_shared import build_feature_frame_from_history, build_replay_cfg_from_extras, minute_floor, safe_float

from .shared import bars_since_setup, is_in_split_entry_window
from .signal import BaseSignalModule, SignalContext


class SPYDualSignalModule(BaseSignalModule):
    module_id = "ma_cross_stack_cont_v2_dual_exp"

    def __init__(self, replay_strategy_id: str = "ma_cross_stack_cont_v2_dual_exp") -> None:
        self.replay_strategy_id = str(replay_strategy_id).strip().lower() or "ma_cross_stack_cont_v2_dual_exp"
        self._replay_strategy = get_replay_strategy(self.replay_strategy_id)
        self._pending_setup = None
        self._entry_plan: Optional[Dict[str, Any]] = None
        self._last_day = None
        self.last_diag: Dict[str, Any] = {}
        self.last_block: tuple[str, str] = ("", "")

    @staticmethod
    def _dual_diag(setup: Dict[str, Any]) -> Dict[str, Any]:
        put_trigger = str(setup.get("put_trigger") or "")
        call_trigger = str(setup.get("call_trigger") or "")
        put_momentum = put_trigger == "momentum_no_retest_put"
        call_momentum = call_trigger == "momentum_no_retest_call"
        return {
            "dual_put_side": "PUT" if put_trigger else "",
            "dual_call_side": "CALL" if call_trigger else "",
            "dual_put_trigger": put_trigger,
            "dual_call_trigger": call_trigger,
            "cross_context_put": "momentum_breakout" if put_momentum else ("recent_cross" if bool(setup.get("put_cross_recent")) else ""),
            "cross_context_call": "momentum_breakout" if call_momentum else ("recent_cross" if bool(setup.get("call_cross_recent")) else ""),
            "retest_state_put": "bypassed" if put_momentum else ("touched" if bool(setup.get("put_retest_touch")) else ""),
            "retest_state_call": "bypassed" if call_momentum else ("touched" if bool(setup.get("call_retest_touch")) else ""),
            "close_lt_vwap": bool(setup.get("close_lt_vwap")),
            "close_gt_vwap": bool(setup.get("close_gt_vwap")),
            "ma10_lt_ma200": bool(setup.get("ma10_lt_ma200")),
            "ma10_gt_ma200": bool(setup.get("ma10_gt_ma200")),
            "abs_close_ma10": float(setup.get("abs_close_ma10", 0.0) or 0.0),
            "pct_close_ma10": float(setup.get("pct_close_ma10", 0.0) or 0.0),
            "dist_ma10_atr": float(setup.get("dist_ma10_atr", 0.0) or 0.0),
            "body_frac": float(setup.get("body_frac", 0.0) or 0.0),
            "vol_ratio": float(setup.get("vol_ratio", 0.0) or 0.0),
            "close_pos": float(setup.get("close_pos", 0.0) or 0.0),
            "upper_wick_frac": float(setup.get("upper_wick_frac", 0.0) or 0.0),
            "lower_wick_frac": float(setup.get("lower_wick_frac", 0.0) or 0.0),
        }

    def _resolve_signal_block(self, diag: Dict[str, Any], signal: Optional[Any]) -> tuple[str, str]:
        if signal is not None:
            return "signal_ready", "SIGNAL_READY"
        if str(diag.get("dual_put_side") or "").strip() or str(diag.get("dual_call_side") or "").strip():
            return "setup", "DUAL_SETUP_READY"
        return "setup", "NO_DUAL_SETUP_MATCH"

    def _prepare_strategy(self, extras: Dict[str, Any]) -> Any:
        setattr(self._replay_strategy, "max_dist_ma10_atr", float(extras.get("v2_max_dist_ma10_atr", getattr(self._replay_strategy, "max_dist_ma10_atr", 1.8)) or 1.8))
        setattr(self._replay_strategy, "cross_retest_lookback_bars", int(extras.get("cross_retest_lookback_bars", getattr(self._replay_strategy, "cross_retest_lookback_bars", 20)) or 20))
        setattr(self._replay_strategy, "retest_window_bars", int(extras.get("retest_window_bars", getattr(self._replay_strategy, "retest_window_bars", 3)) or 3))
        setattr(self._replay_strategy, "retest_touch_tolerance_abs", float(extras.get("retest_touch_tolerance_abs", getattr(self._replay_strategy, "retest_touch_tolerance_abs", 0.15)) or 0.15))
        setattr(self._replay_strategy, "enable_vwap_as_soft_filter", bool(extras.get("enable_vwap_as_soft_filter", getattr(self._replay_strategy, "enable_vwap_as_soft_filter", False))))
        dual_enabled = extras.get("enable_momentum_no_retest_dual", None)
        # Backward compatibility: older configs may only toggle fresh momentum branch.
        # Treat that as the dual momentum switch when explicit dual key is absent.
        if dual_enabled is None and "enable_fresh_momentum_branch" in extras:
            dual_enabled = extras.get("enable_fresh_momentum_branch")
        if dual_enabled is None:
            dual_enabled = getattr(self._replay_strategy, "enable_momentum_no_retest_dual", False)
        setattr(self._replay_strategy, "enable_momentum_no_retest_dual", bool(dual_enabled))
        setattr(self._replay_strategy, "momentum_breakout_lookback_bars", int(extras.get("momentum_breakout_lookback_bars", getattr(self._replay_strategy, "momentum_breakout_lookback_bars", 5)) or 5))
        setattr(self._replay_strategy, "momentum_higher_low_bars", int(extras.get("momentum_higher_low_bars", getattr(self._replay_strategy, "momentum_higher_low_bars", 3)) or 3))
        setattr(self._replay_strategy, "momentum_lower_high_bars", int(extras.get("momentum_lower_high_bars", getattr(self._replay_strategy, "momentum_lower_high_bars", 3)) or 3))
        setattr(self._replay_strategy, "momentum_ma10_slope_min_abs", float(extras.get("momentum_ma10_slope_min_abs", getattr(self._replay_strategy, "momentum_ma10_slope_min_abs", 0.02)) or 0.02))
        setattr(self._replay_strategy, "momentum_close_pos_min_call", float(extras.get("momentum_close_pos_min_call", getattr(self._replay_strategy, "momentum_close_pos_min_call", 0.70)) or 0.70))
        setattr(self._replay_strategy, "momentum_close_pos_max_put", float(extras.get("momentum_close_pos_max_put", getattr(self._replay_strategy, "momentum_close_pos_max_put", 0.30)) or 0.30))
        setattr(self._replay_strategy, "momentum_body_frac_min", float(extras.get("momentum_body_frac_min", getattr(self._replay_strategy, "momentum_body_frac_min", 0.50)) or 0.50))
        setattr(self._replay_strategy, "momentum_vol_ratio_min", float(extras.get("momentum_vol_ratio_min", getattr(self._replay_strategy, "momentum_vol_ratio_min", 0.80)) or 0.80))
        setattr(self._replay_strategy, "momentum_dist_ma10_atr_max", float(extras.get("momentum_dist_ma10_atr_max", getattr(self._replay_strategy, "momentum_dist_ma10_atr_max", 2.2)) or 2.2))
        setattr(self._replay_strategy, "max_close_pos_for_put", float(extras.get("v2_put_close_pos_max", getattr(self._replay_strategy, "max_close_pos_for_put", 1.0)) or 1.0))
        setattr(self._replay_strategy, "min_vol_ratio_for_put", float(extras.get("v2_min_vol_ratio", getattr(self._replay_strategy, "min_vol_ratio_for_put", 0.0)) or 0.0))
        setattr(self._replay_strategy, "max_ma10_slope_for_put", float(extras.get("v2_max_ma10_slope", getattr(self._replay_strategy, "max_ma10_slope_for_put", 999.0)) or 999.0))
        setattr(self._replay_strategy, "min_close_pos_for_put_breakdown", float(extras.get("v2_put_breakdown_close_pos_min", getattr(self._replay_strategy, "min_close_pos_for_put_breakdown", 0.45)) or 0.45))
        setattr(self._replay_strategy, "min_body_frac_for_put_pullback", float(extras.get("v2_put_pullback_body_frac_min", getattr(self._replay_strategy, "min_body_frac_for_put_pullback", 0.55)) or 0.55))
        setattr(self._replay_strategy, "min_vol_ratio_for_put_pullback", float(extras.get("v2_put_pullback_vol_ratio_min", getattr(self._replay_strategy, "min_vol_ratio_for_put_pullback", 0.80)) or 0.80))
        setattr(self._replay_strategy, "max_ma10_slope_for_put_pullback", float(extras.get("v2_put_pullback_ma10_slope_max", getattr(self._replay_strategy, "max_ma10_slope_for_put_pullback", 0.06)) or 0.06))
        setattr(self._replay_strategy, "min_close_pos_for_call_breakout", float(extras.get("v2_call_breakout_close_pos_min", getattr(self._replay_strategy, "min_close_pos_for_call_breakout", 0.70)) or 0.70))
        setattr(self._replay_strategy, "min_body_frac_for_call_pullback", float(extras.get("v2_call_pullback_body_frac_min", getattr(self._replay_strategy, "min_body_frac_for_call_pullback", 0.45)) or 0.45))
        setattr(self._replay_strategy, "min_vol_ratio_for_call_pullback", float(extras.get("v2_call_pullback_vol_ratio_min", getattr(self._replay_strategy, "min_vol_ratio_for_call_pullback", 0.90)) or 0.90))
        setattr(self._replay_strategy, "min_ma10_slope_for_call_pullback", float(extras.get("v2_call_pullback_ma10_slope_min", getattr(self._replay_strategy, "min_ma10_slope_for_call_pullback", 0.08)) or 0.08))
        return self._replay_strategy

    def reset_for_new_day(self, ctx: SignalContext) -> None:
        day = ctx.runtime.now.date()
        if self._last_day != day:
            self._last_day = day
            self._pending_setup = None
            self._entry_plan = None
            self.last_diag = {}
            self.last_block = ("", "")
            self._replay_strategy.on_new_day()

    def build_signal(self, ctx: SignalContext) -> Optional[Signal]:
        runtime = ctx.runtime
        extras = dict(ctx.extras or {})
        self.reset_for_new_day(ctx)
        if runtime.active_position is not None:
            self._pending_setup = None
            self.last_diag = {}
            self.last_block = ("position", "POSITION_OPEN")
            return None
        if int((runtime.payload or {}).get("pending_entry_count") or 0) > 0:
            self._pending_setup = None
            self._entry_plan = None
            self.last_diag = {}
            self.last_block = ("entry", "PENDING_ENTRY_OPEN")
            return None
        if not is_in_split_entry_window(runtime.now, extras):
            self._pending_setup = None
            self._entry_plan = None
            self.last_diag = {}
            self.last_block = ("time", "ENTRY_WINDOW_CLOSED")
            return None
        cfg = build_replay_cfg_from_extras(extras)
        feature_frame = build_feature_frame_from_history(runtime.symbol, list(runtime.payload.get("bar_history") or []), extras=extras)
        if feature_frame.empty or len(feature_frame) < 210:
            self.last_diag = {}
            self.last_block = ("warmup", "WARMUP_NOT_READY")
            return None
        idx = len(feature_frame) - 1
        row = feature_frame.iloc[idx]
        replay_strategy = self._prepare_strategy(extras)
        regime, _cross_idx = classify_signal_regime(feature_frame, idx)
        setup = replay_strategy.describe_setup(feature_frame, idx, row)
        diag = {
            "regime": regime,
            "setup_side": "",
            "fresh_momentum_side": "",
            "daily_best_side": "",
            "trend_cont_side": "",
            **self._dual_diag(setup),
        }
        self.last_diag = diag
        if self._pending_setup is not None:
            pending = self._pending_setup
            setup_ts = getattr(pending, "setup_ts", None)
            since = bars_since_setup(runtime.now, setup_ts) if isinstance(setup_ts, datetime) else idx - int(getattr(pending, "setup_idx", idx))
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
            if not replay_strategy.confirm_signal(feature_frame, idx, row, pending, cfg):
                self.last_block = ("confirm", "CONFIRM_NOT_MET")
                return None
            structure_stop, target_1r, target_1p2r = replay_strategy.build_position_targets(feature_frame, idx, row, pending, close_px)
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
        signal = replay_strategy.detect_signal(feature_frame, idx, row, cfg)
        if signal is None:
            self.last_block = self._resolve_signal_block(diag, signal)
            return None
        self._pending_setup = replay_strategy.build_pending_setup(signal, minute_floor(runtime.now), idx, row)
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
