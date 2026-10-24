from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from libs.strategy.base import StrategyAlgorithm, StrategyContext, StrategyParams
from strategies.options_kernel.modules import ContractSelectionContext, SignalContext, definition_registry
from services.option_replay.strategies.ma10_reject_0324 import (
    classify_signal_regime,
)
from strategies.options_kernel.types import EntryPlan, PluginRuntimeState, Selection, Signal
from strategies.options_plugins.ma_1dte_reject_shared import (
    build_feature_frame_from_history,
    build_replay_cfg_from_extras,
    minute_floor,
    safe_float,
    select_option_quote_for_underlying,
)
from strategies.options_plugins.ma_option_definition_plugin_base import DefinitionDrivenPureOptionPlugin
from strategies.spy_1dte_ma10_reject_live_strategy import SPY1DTEMA10RejectLiveStrategy


class SPY1DTEMa10RejectPlugin(DefinitionDrivenPureOptionPlugin):
    # Active MA option strategy tuning should be done here (plugin path),
    # not in legacy/non-enabled strategy entries.
    component_name = "spy_1dte_ma10_reject_plugin"

    def __init__(self) -> None:
        super().__init__()
        self._pure_ctx: Any = None

    @staticmethod
    def _default_replay_strategy_id() -> str:
        return "ma_cross_stack_cont_v2_dual_exp"

    def _definition_id(self) -> str:
        return "SPY_MA_CROSS_STACK_CONT_V2_DUAL"

    def apply_default_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        original_extras = dict(extras or {})
        merged = dict(original_extras)
        meta = self._definition_metadata()
        merged.setdefault("definition_id", meta["definition_id"])
        merged.setdefault("audit_namespace", meta["audit_namespace"])
        merged.setdefault("position_namespace", meta["position_namespace"])
        merged.setdefault("ab_time_start", "09:40:00")
        merged.setdefault("ab_time_end", "15:00:00")
        merged.setdefault("entry_start_am", "09:40:00")
        merged.setdefault("entry_end_am", "12:00:00")
        merged.setdefault("entry_start_pm", "13:00:00")
        merged.setdefault("entry_end_pm", "15:00:00")
        merged.setdefault("no_new_entry_after", "15:00:00")
        merged.setdefault("force_flatten_time", "15:50:00")
        merged.setdefault("contracts", 4)
        merged.setdefault("confirm_window_bars", 2)
        merged.setdefault("confirm_decay_from_bar", 99)
        merged.setdefault("confirm_decay_max_distance_atr", 0.30)
        merged.setdefault("ma10_reject_ext_atr_max", 2.2)
        merged.setdefault("enable_fresh_momentum_branch", False)
        merged.setdefault("enable_trend_continuation_branch", False)
        merged.setdefault("enable_daily_best_additive_branch", False)
        merged.setdefault("daily_best_call_only", False)
        merged.setdefault("daily_best_min_dist_vwap_atr", 0.0)
        merged.setdefault("dte_preferred", 1)
        merged.setdefault("dte_fallback_max", 2)
        merged.setdefault("delta_min", 0.45)
        merged.setdefault("delta_max", 0.55)
        merged.setdefault("delta_fallback_min", 0.40)
        merged.setdefault("delta_fallback_max", 0.60)
        merged.setdefault("spread_max_pct", 0.12)
        merged.setdefault("min_open_interest", 0)
        merged.setdefault("continuation_pending_entry_timeout_bars", 2)
        merged.setdefault("continuation_dynamic_reprice_to_ask", True)
        merged.setdefault("continuation_max_premium_expansion_pct", 0.08)
        merged.setdefault("adaptive_entry_improve_pct", 0.0)
        merged.setdefault("urgent_entry_improve_pct", 0.0)
        merged.setdefault("time_stop_soft_bars", 5)
        merged.setdefault("time_stop_hard_bars", 10)
        merged.setdefault("v2_max_dist_ma10_atr", 1.8)
        merged.setdefault("v2_put_close_pos_max", 1.0)
        merged.setdefault("v2_min_vol_ratio", 0.0)
        merged.setdefault("v2_max_ma10_slope", 999.0)
        merged.setdefault("v2_put_breakdown_close_pos_min", 0.45)
        merged.setdefault("v2_put_pullback_body_frac_min", 0.55)
        merged.setdefault("v2_put_pullback_vol_ratio_min", 0.80)
        merged.setdefault("v2_put_pullback_ma10_slope_max", 0.06)
        merged.setdefault("v2_call_breakout_close_pos_min", 0.70)
        merged.setdefault("v2_call_pullback_body_frac_min", 0.45)
        merged.setdefault("v2_call_pullback_vol_ratio_min", 0.90)
        merged.setdefault("v2_call_pullback_ma10_slope_min", 0.08)
        merged.setdefault("cross_retest_lookback_bars", 20)
        merged.setdefault("retest_window_bars", 3)
        merged.setdefault("retest_touch_tolerance_abs", 0.15)
        merged.setdefault("enable_vwap_as_soft_filter", False)
        merged.setdefault("enable_momentum_no_retest_dual", False)
        merged.setdefault("momentum_breakout_lookback_bars", 5)
        merged.setdefault("momentum_higher_low_bars", 3)
        merged.setdefault("momentum_lower_high_bars", 3)
        merged.setdefault("momentum_ma10_slope_min_abs", 0.02)
        merged.setdefault("momentum_close_pos_min_call", 0.70)
        merged.setdefault("momentum_close_pos_max_put", 0.30)
        merged.setdefault("momentum_body_frac_min", 0.50)
        merged.setdefault("momentum_vol_ratio_min", 0.80)
        merged.setdefault("momentum_dist_ma10_atr_max", 2.2)
        merged.setdefault("entry_quote_lookback_min", 2)
        merged.setdefault("entry_quote_max_age_sec", 90)
        merged.setdefault("allow_stale_entry_quotes", True)
        merged.setdefault("entry_retry_enabled", False)
        merged.setdefault("entry_retry_max_attempts", 3)
        merged.setdefault("entry_retry_wait_sec", 2.0)
        merged.setdefault("entry_timeout_sec", 1.8)
        merged.setdefault("mark_quote_lookback_min", 2)
        merged.setdefault("mark_quote_max_age_sec", 90)
        merged.setdefault("exit_quote_lookback_min", 3)
        merged.setdefault("exit_degraded_nbbo_max_age_sec", 5.0)
        merged.setdefault("replay_strategy_id", "ma_cross_stack_cont_v2_dual_exp")
        merged.setdefault("underlying", "SPY")
        merged.setdefault("risk_symbol", meta["risk_symbol"])
        merged.setdefault("enable_live_parity_snapshot", True)
        merged.setdefault("enable_live_shadow_mode", False)
        merged.setdefault("live_parity_profile_id", "spy_option_ma_ext_v1")
        merged.setdefault("market_data_profile_id", "standard_close_extended_sma")
        merged.setdefault("kernel_mode", "pure_plugin")
        merged.setdefault("strict_scope_validation", True)
        merged = self._finalize_definition_extras(original_extras, merged)
        self._last_extras = dict(merged)
        return merged

    def build_delegate(self, ctx: StrategyContext, params: StrategyParams) -> StrategyAlgorithm:
        return SPY1DTEMA10RejectLiveStrategy(ctx, params)

    async def initialize_pure(self, ctx: Any, params: Any, kernel: Any) -> None:
        self._pure_ctx = ctx
        await super().initialize_pure(ctx, params, kernel)

    def _get_replay_strategy_id(self) -> str:
        strategy_id = str(
            (self._last_extras or {}).get("replay_strategy_id")
            or self._pure_replay_strategy_id
            or self._default_replay_strategy_id()
        ).strip().lower()
        self._pure_replay_strategy_id = strategy_id
        return strategy_id

    def _resolve_local_audit_writer(self) -> Any:
        ctx = self._pure_ctx
        if ctx is None:
            return None
        engine = getattr(ctx, "engine", None)
        candidates = [
            getattr(engine, "audit_writer", None),
            getattr(getattr(engine, "_strategy_engine", None), "audit_writer", None),
        ]
        for writer in candidates:
            if writer is not None and hasattr(writer, "emit"):
                return writer
        return None

    def _emit_execution_audit(
        self,
        runtime: PluginRuntimeState,
        *,
        stage: str,
        status: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
        severity: str = "info",
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        try:
            writer.emit(
                domain="strategy",
                component=str(self.component_name),
                stage=stage,
                severity=severity,
                strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                symbol=str(self._pure_underlying or runtime.symbol),
                status=status,
                reason=reason,
                payload={
                    "replay_strategy_id": self._get_replay_strategy_id(),
                    **dict(payload or {}),
                },
            )
        except Exception:
            pass

    def _is_in_entry_window(self, now: datetime, extras: Dict[str, Any]) -> bool:
        now_t = now.time()
        am_start = self._parse_hms(extras.get("entry_start_am", "09:40:00"), self._parse_hms(extras.get("ab_time_start", "09:40:00"), datetime.strptime("09:40:00", "%H:%M:%S").time()))
        am_end = self._parse_hms(extras.get("entry_end_am", "12:00:00"), datetime.strptime("12:00:00", "%H:%M:%S").time())
        pm_start = self._parse_hms(extras.get("entry_start_pm", "13:00:00"), datetime.strptime("13:00:00", "%H:%M:%S").time())
        pm_end = self._parse_hms(extras.get("entry_end_pm", "15:00:00"), self._parse_hms(extras.get("ab_time_end", "15:00:00"), datetime.strptime("15:00:00", "%H:%M:%S").time()))
        no_new_after = self._parse_hms(extras.get("no_new_entry_after", "15:00:00"), datetime.strptime("15:00:00", "%H:%M:%S").time())
        if now_t > no_new_after:
            return False
        in_am = am_start <= now_t <= am_end
        in_pm = pm_start <= now_t <= pm_end
        return bool(in_am or in_pm)

    def _ensure_pure_replay_strategy(self) -> Any:
        strategy_id = self._get_replay_strategy_id()
        if self._pure_replay_strategy is None or strategy_id != str(self._pure_replay_strategy_id or "").strip().lower():
            from services.option_replay.strategies import get_strategy as get_replay_strategy

            self._pure_replay_strategy = get_replay_strategy(strategy_id)
            self._pure_replay_strategy_id = strategy_id
        strategy = self._pure_replay_strategy
        extras = getattr(self, "_last_extras", {}) or {}
        if strategy_id == "ma_cross_stack_cont_v2_dual_exp":
            setattr(strategy, "max_dist_ma10_atr", float(extras.get("v2_max_dist_ma10_atr", getattr(strategy, "max_dist_ma10_atr", 1.8)) or 1.8))
            setattr(strategy, "enable_momentum_no_retest_dual", bool(extras.get("enable_momentum_no_retest_dual", getattr(strategy, "enable_momentum_no_retest_dual", False))))
            setattr(strategy, "momentum_breakout_lookback_bars", int(extras.get("momentum_breakout_lookback_bars", getattr(strategy, "momentum_breakout_lookback_bars", 5)) or 5))
            setattr(strategy, "momentum_higher_low_bars", int(extras.get("momentum_higher_low_bars", getattr(strategy, "momentum_higher_low_bars", 3)) or 3))
            setattr(strategy, "momentum_lower_high_bars", int(extras.get("momentum_lower_high_bars", getattr(strategy, "momentum_lower_high_bars", 3)) or 3))
            setattr(strategy, "momentum_ma10_slope_min_abs", float(extras.get("momentum_ma10_slope_min_abs", getattr(strategy, "momentum_ma10_slope_min_abs", 0.02)) or 0.02))
            setattr(strategy, "momentum_close_pos_min_call", float(extras.get("momentum_close_pos_min_call", getattr(strategy, "momentum_close_pos_min_call", 0.70)) or 0.70))
            setattr(strategy, "momentum_close_pos_max_put", float(extras.get("momentum_close_pos_max_put", getattr(strategy, "momentum_close_pos_max_put", 0.30)) or 0.30))
            setattr(strategy, "momentum_body_frac_min", float(extras.get("momentum_body_frac_min", getattr(strategy, "momentum_body_frac_min", 0.50)) or 0.50))
            setattr(strategy, "momentum_vol_ratio_min", float(extras.get("momentum_vol_ratio_min", getattr(strategy, "momentum_vol_ratio_min", 0.80)) or 0.80))
            setattr(strategy, "momentum_dist_ma10_atr_max", float(extras.get("momentum_dist_ma10_atr_max", getattr(strategy, "momentum_dist_ma10_atr_max", 2.2)) or 2.2))
            setattr(strategy, "max_close_pos_for_put", float(extras.get("v2_put_close_pos_max", getattr(strategy, "max_close_pos_for_put", 1.0)) or 1.0))
            setattr(strategy, "min_vol_ratio_for_put", float(extras.get("v2_min_vol_ratio", getattr(strategy, "min_vol_ratio_for_put", 0.0)) or 0.0))
            setattr(strategy, "max_ma10_slope_for_put", float(extras.get("v2_max_ma10_slope", getattr(strategy, "max_ma10_slope_for_put", 999.0)) or 999.0))
            setattr(strategy, "min_close_pos_for_put_breakdown", float(extras.get("v2_put_breakdown_close_pos_min", getattr(strategy, "min_close_pos_for_put_breakdown", 0.45)) or 0.45))
            setattr(strategy, "min_body_frac_for_put_pullback", float(extras.get("v2_put_pullback_body_frac_min", getattr(strategy, "min_body_frac_for_put_pullback", 0.55)) or 0.55))
            setattr(strategy, "min_vol_ratio_for_put_pullback", float(extras.get("v2_put_pullback_vol_ratio_min", getattr(strategy, "min_vol_ratio_for_put_pullback", 0.80)) or 0.80))
            setattr(strategy, "max_ma10_slope_for_put_pullback", float(extras.get("v2_put_pullback_ma10_slope_max", getattr(strategy, "max_ma10_slope_for_put_pullback", 0.06)) or 0.06))
            setattr(strategy, "min_close_pos_for_call_breakout", float(extras.get("v2_call_breakout_close_pos_min", getattr(strategy, "min_close_pos_for_call_breakout", 0.70)) or 0.70))
            setattr(strategy, "min_body_frac_for_call_pullback", float(extras.get("v2_call_pullback_body_frac_min", getattr(strategy, "min_body_frac_for_call_pullback", 0.45)) or 0.45))
            setattr(strategy, "min_vol_ratio_for_call_pullback", float(extras.get("v2_call_pullback_vol_ratio_min", getattr(strategy, "min_vol_ratio_for_call_pullback", 0.90)) or 0.90))
            setattr(strategy, "min_ma10_slope_for_call_pullback", float(extras.get("v2_call_pullback_ma10_slope_min", getattr(strategy, "min_ma10_slope_for_call_pullback", 0.08)) or 0.08))
        return strategy

    @staticmethod
    def _dual_diag(row: Any, extras: Dict[str, Any]) -> Dict[str, str]:
        close_px = safe_float(row.get("close"))
        ma10 = safe_float(row.get("ma10"))
        ma200 = safe_float(row.get("ma200"))
        vwap = safe_float(row.get("vwap_day"))
        atr = max(0.01, safe_float(row.get("atr_14")))
        close_pos = safe_float(row.get("close_pos"), 0.5)
        body = abs(safe_float(row.get("body")))
        rng = max(1e-9, safe_float(row.get("range")))
        body_frac = body / rng
        vol = safe_float(row.get("volume"))
        vol_ma20 = max(1e-9, safe_float(row.get("vol_ma20")))
        vol_ratio = vol / vol_ma20
        ma10_slope = safe_float(row.get("ma10_slope"))
        if min(close_px, ma10, ma200, vwap, atr) <= 0.0:
            return {
                "dual_put_side": "",
                "dual_call_side": "",
                "dual_put_trigger": "",
                "dual_call_trigger": "",
            }

        max_dist_ma10_atr = float(extras.get("v2_max_dist_ma10_atr", 1.8) or 1.8)
        put_breakdown_close_pos_min = float(extras.get("v2_put_breakdown_close_pos_min", 0.45) or 0.45)
        put_pullback_body_frac_min = float(extras.get("v2_put_pullback_body_frac_min", 0.55) or 0.55)
        put_pullback_vol_ratio_min = float(extras.get("v2_put_pullback_vol_ratio_min", 0.80) or 0.80)
        put_pullback_ma10_slope_max = float(extras.get("v2_put_pullback_ma10_slope_max", 0.06) or 0.06)
        call_breakout_close_pos_min = float(extras.get("v2_call_breakout_close_pos_min", 0.70) or 0.70)
        call_pullback_body_frac_min = float(extras.get("v2_call_pullback_body_frac_min", 0.45) or 0.45)
        call_pullback_vol_ratio_min = float(extras.get("v2_call_pullback_vol_ratio_min", 0.90) or 0.90)
        call_pullback_ma10_slope_min = float(extras.get("v2_call_pullback_ma10_slope_min", 0.08) or 0.08)

        dist_ma10_atr = abs(close_px - ma10) / atr
        if dist_ma10_atr > max_dist_ma10_atr:
            return {
                "dual_put_side": "",
                "dual_call_side": "",
                "dual_put_trigger": "",
                "dual_call_trigger": "",
            }

        put_trigger = ""
        call_trigger = ""
        if close_px < ma200 and close_px < vwap:
            if close_px <= ma10 and (1.0 - close_pos) >= put_breakdown_close_pos_min:
                put_trigger = "breakdown_cont_put"
            elif close_px > ma10 and body_frac >= put_pullback_body_frac_min and vol_ratio >= put_pullback_vol_ratio_min and ma10_slope <= put_pullback_ma10_slope_max:
                put_trigger = "pullback_trap_put"

        if close_px > ma200 and close_px > vwap:
            if close_px >= ma10 and close_pos >= call_breakout_close_pos_min:
                call_trigger = "breakout_cont_call"
            elif close_px < ma10 and body_frac >= call_pullback_body_frac_min and vol_ratio >= call_pullback_vol_ratio_min and ma10_slope >= call_pullback_ma10_slope_min:
                call_trigger = "pullback_reclaim_call"

        return {
            "dual_put_side": "PUT" if put_trigger else "",
            "dual_call_side": "CALL" if call_trigger else "",
            "dual_put_trigger": put_trigger,
            "dual_call_trigger": call_trigger,
        }

    def _resolve_signal_block(self, diag: Dict[str, Any], signal: Optional[Any]) -> tuple[str, str]:
        if signal is not None:
            return "signal_ready", "SIGNAL_READY"
        if str(diag.get("dual_put_side") or "").strip() or str(diag.get("dual_call_side") or "").strip():
            return "setup", "DUAL_SETUP_READY"
        return "setup", "NO_DUAL_SETUP_MATCH"

    def _emit_minute_observability(
        self,
        runtime: PluginRuntimeState,
        *,
        history: list[Dict[str, Any]],
        has_pending: bool,
        reason: Optional[str],
        diag: Optional[Dict[str, Any]] = None,
        feature_row: Optional[Any] = None,
        gate_stage: str = "",
        gate_reason: str = "",
    ) -> None:
        log = self._pure_log
        row = history[-1] if history else {}
        minute = runtime.now.strftime("%Y-%m-%d %H:%M")

        def _feature_value(name: str, fallback: float = 0.0) -> float:
            try:
                if feature_row is not None:
                    return safe_float(feature_row.get(name), fallback)
            except Exception:
                pass
            try:
                return safe_float(row.get(name), fallback)
            except Exception:
                return fallback

        close_px = _feature_value("close")
        if log is not None:
            try:
                log.info(
                    "spy_1dte_minute_heartbeat",
                    ts=minute + ":00",
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    underlying=str(self._pure_underlying or runtime.symbol),
                    bars=int(len(history)),
                    has_pending=bool(has_pending),
                    has_position=bool(runtime.active_position is not None),
                    trades_filled_today=int(self._pure_trades_filled_today),
                    quote_health_block_day=bool(self._pure_quote_health_block_day),
                    close=close_px,
                    vwap=_feature_value("vwap_day", _feature_value("vwap", close_px)),
                    ma10=_feature_value("ma10"),
                    ma20=_feature_value("ma20"),
                    ma200=_feature_value("ma200"),
                    atr=_feature_value("atr_14", _feature_value("atr")),
                    replay_strategy_id=self._get_replay_strategy_id(),
                    component_name=str(self.component_name),
                    regime=str((diag or {}).get("regime") or ""),
                    setup_side=str((diag or {}).get("setup_side") or ""),
                    fresh_momentum_side=str((diag or {}).get("fresh_momentum_side") or ""),
                    daily_best_side=str((diag or {}).get("daily_best_side") or ""),
                    trend_cont_side=str((diag or {}).get("trend_cont_side") or ""),
                    dual_put_side=str((diag or {}).get("dual_put_side") or ""),
                    dual_call_side=str((diag or {}).get("dual_call_side") or ""),
                    dual_put_trigger=str((diag or {}).get("dual_put_trigger") or ""),
                    dual_call_trigger=str((diag or {}).get("dual_call_trigger") or ""),
                    cross_context_put=str((diag or {}).get("cross_context_put") or ""),
                    cross_context_call=str((diag or {}).get("cross_context_call") or ""),
                    retest_state_put=str((diag or {}).get("retest_state_put") or ""),
                    retest_state_call=str((diag or {}).get("retest_state_call") or ""),
                    abs_close_ma10=safe_float((diag or {}).get("abs_close_ma10")),
                    pct_close_ma10=safe_float((diag or {}).get("pct_close_ma10")),
                    dist_ma10_atr=safe_float((diag or {}).get("dist_ma10_atr")),
                    close_lt_vwap=bool((diag or {}).get("close_lt_vwap")),
                    close_gt_vwap=bool((diag or {}).get("close_gt_vwap")),
                    ma10_lt_ma200=bool((diag or {}).get("ma10_lt_ma200")),
                    ma10_gt_ma200=bool((diag or {}).get("ma10_gt_ma200")),
                    close_pos=safe_float((diag or {}).get("close_pos")),
                    body_frac=safe_float((diag or {}).get("body_frac")),
                    vol_ratio=safe_float((diag or {}).get("vol_ratio")),
                    gate_stage=str(gate_stage or ""),
                    gate_reason=str(gate_reason or ""),
                )
            except Exception:
                pass
        writer = self._resolve_local_audit_writer()
        if writer is not None:
            try:
                writer.emit(
                    domain="strategy",
                    component=str(self.component_name),
                    stage="plugin_minute_observability",
                    severity="info",
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    symbol=str(self._pure_underlying or runtime.symbol),
                    status="ok",
                    reason=str(gate_reason or reason or "heartbeat"),
                    payload={
                        "has_pending": bool(has_pending),
                        "has_position": bool(runtime.active_position is not None),
                        "gate_stage": str(gate_stage or ""),
                        "gate_reason": str(gate_reason or ""),
                        "reason": str(reason or ""),
                        "regime": str((diag or {}).get("regime") or ""),
                        "dual_put_side": str((diag or {}).get("dual_put_side") or ""),
                        "dual_call_side": str((diag or {}).get("dual_call_side") or ""),
                        "dual_put_trigger": str((diag or {}).get("dual_put_trigger") or ""),
                        "dual_call_trigger": str((diag or {}).get("dual_call_trigger") or ""),
                        "cross_context_put": str((diag or {}).get("cross_context_put") or ""),
                        "cross_context_call": str((diag or {}).get("cross_context_call") or ""),
                        "retest_state_put": str((diag or {}).get("retest_state_put") or ""),
                        "retest_state_call": str((diag or {}).get("retest_state_call") or ""),
                        "abs_close_ma10": safe_float((diag or {}).get("abs_close_ma10")),
                        "pct_close_ma10": safe_float((diag or {}).get("pct_close_ma10")),
                        "dist_ma10_atr": safe_float((diag or {}).get("dist_ma10_atr")),
                        "close_lt_vwap": bool((diag or {}).get("close_lt_vwap")),
                        "close_gt_vwap": bool((diag or {}).get("close_gt_vwap")),
                        "ma10_lt_ma200": bool((diag or {}).get("ma10_lt_ma200")),
                        "ma10_gt_ma200": bool((diag or {}).get("ma10_gt_ma200")),
                        "close_pos": safe_float((diag or {}).get("close_pos")),
                        "body_frac": safe_float((diag or {}).get("body_frac")),
                        "vol_ratio": safe_float((diag or {}).get("vol_ratio")),
                    },
                )
            except Exception:
                pass
        if not reason:
            return
        day_count = int(self._pure_day_block_counts.get(reason, 0)) + 1
        self._pure_day_block_counts[reason] = day_count
        if log is not None:
            try:
                log.info(
                    "spy_1dte_entry_block",
                    ts=minute + ":00",
                    minute=minute,
                    reason=str(reason),
                    minute_count=1,
                    day_count=day_count,
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    underlying=str(self._pure_underlying or runtime.symbol),
                    replay_strategy_id=self._get_replay_strategy_id(),
                    component_name=str(self.component_name),
                    gate_stage=str(gate_stage or ""),
                    gate_reason=str(gate_reason or ""),
                    regime=str((diag or {}).get("regime") or ""),
                    setup_side=str((diag or {}).get("setup_side") or ""),
                    fresh_momentum_side=str((diag or {}).get("fresh_momentum_side") or ""),
                    daily_best_side=str((diag or {}).get("daily_best_side") or ""),
                    trend_cont_side=str((diag or {}).get("trend_cont_side") or ""),
                    dual_put_side=str((diag or {}).get("dual_put_side") or ""),
                    dual_call_side=str((diag or {}).get("dual_call_side") or ""),
                    dual_put_trigger=str((diag or {}).get("dual_put_trigger") or ""),
                    dual_call_trigger=str((diag or {}).get("dual_call_trigger") or ""),
                    cross_context_put=str((diag or {}).get("cross_context_put") or ""),
                    cross_context_call=str((diag or {}).get("cross_context_call") or ""),
                    retest_state_put=str((diag or {}).get("retest_state_put") or ""),
                    retest_state_call=str((diag or {}).get("retest_state_call") or ""),
                    abs_close_ma10=safe_float((diag or {}).get("abs_close_ma10")),
                    pct_close_ma10=safe_float((diag or {}).get("pct_close_ma10")),
                    dist_ma10_atr=safe_float((diag or {}).get("dist_ma10_atr")),
                    close_lt_vwap=bool((diag or {}).get("close_lt_vwap")),
                    close_gt_vwap=bool((diag or {}).get("close_gt_vwap")),
                    ma10_lt_ma200=bool((diag or {}).get("ma10_lt_ma200")),
                    ma10_gt_ma200=bool((diag or {}).get("ma10_gt_ma200")),
                )
            except Exception:
                pass

    def build_signal_pure(self, runtime: PluginRuntimeState) -> Optional[Signal]:
        self._pure_reset_if_needed(runtime)
        history = list(runtime.payload.get("bar_history") or [])
        extras = getattr(self, "_last_extras", {}) or {}
        signal_module = self._resolve_strategy_definition().signal_module
        feature_row = None
        if history:
            try:
                feature_frame = build_feature_frame_from_history(
                    str(self._pure_underlying or runtime.symbol),
                    history,
                    extras=extras,
                )
                if not feature_frame.empty:
                    feature_row = feature_frame.iloc[-1]
            except Exception:
                feature_row = None
        signal = signal_module.build_signal(SignalContext(runtime=runtime, extras=extras))
        self._pure_pending_setup = getattr(signal_module, "_pending_setup", None)
        self._pure_entry_plan = getattr(signal_module, "_entry_plan", None)
        diag = dict(getattr(signal_module, "last_diag", {}) or {})
        gate_stage, gate_reason = getattr(signal_module, "last_block", ("", ""))
        if gate_reason == "WAIT_NEXT_BAR" and self._pure_pending_setup is not None:
            pending = self._pure_pending_setup
            setup_ts = getattr(pending, "setup_ts", None)
            bars_since_setup = self._bars_since_setup(runtime.now, setup_ts) if isinstance(setup_ts, datetime) else 0
            self._emit_execution_audit(
                runtime,
                stage="plugin_confirm_wait_next_bar",
                status="waiting",
                reason="wait_next_bar",
                payload={
                    "side": str(getattr(pending, "side", "")),
                    "signal_kind": str(getattr(pending, "signal_kind", "")),
                    "setup_idx": int(getattr(pending, "setup_idx", 0)),
                    "setup_ts": setup_ts.isoformat() if isinstance(setup_ts, datetime) else "",
                    "runtime_now": runtime.now.isoformat(),
                    "bars_since_setup": int(bars_since_setup),
                },
            )
        elif gate_reason == "CONFIRM_WINDOW_EXPIRED":
            self._emit_execution_audit(runtime, stage="plugin_confirm_window_expired", status="blocked", reason="confirm_window_expired", severity="warning")
        elif gate_reason == "SETUP_INVALIDATED":
            self._emit_execution_audit(runtime, stage="plugin_confirm_invalidated", status="blocked", reason="setup_invalidated", severity="warning")
        elif gate_reason == "CONFIRM_NOT_MET":
            self._emit_execution_audit(runtime, stage="plugin_confirm_not_met", status="waiting", reason="confirm_not_met")
        elif gate_reason == "SIGNAL_READY" and signal is not None:
            self._emit_execution_audit(
                runtime,
                stage="plugin_signal_ready",
                status="ready",
                reason="signal_ready",
                payload={
                    "side": str(signal.side),
                    "signal_kind": str(signal.signal_kind),
                    "close": float(signal.payload.get("close") or 0.0),
                    "r_per_share": float((self._pure_entry_plan or {}).get("r_per_share", 0.0)),
                },
            )
        elif gate_reason == "SETUP_READY_WAIT_CONFIRM" and self._pure_pending_setup is not None:
            self._emit_execution_audit(
                runtime,
                stage="plugin_setup_pending",
                status="waiting",
                reason="setup_pending_wait_confirm",
                payload={
                    "side": str(getattr(self._pure_pending_setup, "side", "")),
                    "signal_kind": str(getattr(self._pure_pending_setup, "signal_kind", "")),
                    "regime": str(getattr(self._pure_pending_setup, "regime", "")),
                    "setup_idx": int(getattr(self._pure_pending_setup, "setup_idx", 0)),
                },
            )
        reason = None if gate_reason == "SIGNAL_READY" else gate_reason or "NO_SIGNAL"
        order_pending = int((runtime.payload or {}).get("pending_entry_count") or 0) > 0
        self._emit_minute_observability(
            runtime,
            history=history,
            has_pending=bool(self._pure_pending_setup is not None or order_pending),
            reason=reason,
            diag=diag,
            feature_row=feature_row,
            gate_stage=str(gate_stage or ""),
            gate_reason=str(gate_reason or ""),
        )
        return signal

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal) -> Optional[EntryPlan]:
        plan = None
        plan = self._resolve_strategy_definition().signal_module.build_entry_plan(
            SignalContext(runtime=runtime, extras=getattr(self, "_last_extras", {}) or {}),
            signal,
        )
        if plan is None:
            self._emit_execution_audit(
                runtime,
                stage="plugin_entry_plan_missing",
                status="blocked",
                reason="entry_plan_missing_after_signal",
                payload={
                    "signal_side": str(signal.side),
                    "signal_kind": str(signal.signal_kind),
                },
                severity="warning",
            )
            return None
        self._emit_execution_audit(
            runtime,
            stage="plugin_entry_plan_ready",
            status="ready",
            reason="entry_plan_ready",
            payload={
                "side": str(plan.side),
                "signal_kind": str(plan.signal_kind),
                "contracts": int(plan.contracts),
                "structure_stop": float(plan.structure_stop),
                "target_1r": float(plan.target_1r),
                "target_1p2r": float(plan.target_1p2r),
            },
        )
        return plan

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan) -> Optional[Selection]:
        extras = getattr(self, "_last_extras", {}) or {}
        selection = self._resolve_strategy_definition().contract_policy.select_contract(
            ContractSelectionContext(runtime=runtime, entry_plan=entry_plan, extras=extras)
        )
        reject_reason = str(getattr(self._resolve_strategy_definition().contract_policy, "last_reject_reason", "") or "")
        if selection is None:
            self._emit_execution_audit(
                runtime,
                stage="plugin_contract_selection_failed",
                status="blocked",
                reason=str(reject_reason or "unknown_contract_filter"),
                payload={
                    "side": str(entry_plan.side),
                    "signal_kind": str(entry_plan.signal_kind),
                    "spot": float(runtime.spot or 0.0),
                },
                severity="warning",
            )
            return None
        self._emit_execution_audit(
            runtime,
            stage="plugin_contract_selection_ready",
            status="ready",
            reason="contract_selected",
            payload={
                "side": str(selection.side),
                "signal_kind": str(entry_plan.signal_kind),
                "opt_symbol": str(selection.opt_symbol),
                "expiry": str(selection.expiry),
                "strike": float(selection.strike),
                "bid": float(selection.bid),
                "ask": float(selection.ask),
                "mid": float(selection.mid),
                "quote_age_sec": float(selection.quote_age_sec),
            },
        )
        return selection

    def on_entry_submitted_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        position_lot_id: str,
    ) -> None:
        super().on_entry_submitted_pure(
            runtime=runtime,
            signal=signal,
            entry_plan=entry_plan,
            selection=selection,
            position_lot_id=position_lot_id,
        )
        self._emit_execution_audit(
            runtime,
            stage="plugin_entry_submit_dispatched",
            status="submitted",
            reason="submit_intent_dispatched",
            payload={
                "position_lot_id": str(position_lot_id),
                "side": str(entry_plan.side),
                "signal_kind": str(entry_plan.signal_kind),
                "opt_symbol": str(selection.opt_symbol),
                "contracts": int(entry_plan.contracts),
            },
        )

    def on_entry_submit_attempt_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        profile: Any,
    ) -> None:
        self._emit_execution_audit(
            runtime,
            stage="plugin_entry_submit_attempt",
            status="ready",
            reason="submit_intent_attempt",
            payload={
                "side": str(entry_plan.side),
                "signal_kind": str(entry_plan.signal_kind),
                "opt_symbol": str(selection.opt_symbol),
                "contracts": int(entry_plan.contracts),
                "execution_profile_id": str(getattr(profile, "profile_id", "") or ""),
            },
        )

    def on_entry_submit_blocked_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._emit_execution_audit(
            runtime,
            stage="plugin_entry_submit_blocked",
            status="blocked",
            reason=str(reason or "entry_submit_blocked"),
            payload={
                "side": str(entry_plan.side),
                "signal_kind": str(entry_plan.signal_kind),
                "opt_symbol": str(getattr(selection, "opt_symbol", "") or ""),
                **dict(payload or {}),
            },
            severity="warning",
        )
