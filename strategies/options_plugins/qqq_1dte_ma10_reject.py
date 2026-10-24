from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from typing import Any, Dict, Optional

from libs.strategy.base import StrategyAlgorithm, StrategyContext, StrategyParams
from services.option_replay.strategies.ma10_reject_0324 import (
    classify_signal_regime,
    detect_daily_best_additive_setup,
    detect_fresh_momentum_setup,
    detect_setup,
)
from services.option_replay.strategies.ma10_reject_0324_trend_continuation_exp import detect_trend_continuation_setup
from strategies.options_kernel.execution_profile import build_execution_profile_from_extras
from strategies.options_kernel.modules import (
    ContractSelectionContext,
    ExecutionContext,
    PositionContext,
    SignalContext,
    definition_registry,
)
from strategies.options_kernel.types import EntryPlan, ExecutionProfile, ExitPlan, PluginRuntimeState, Selection, Signal
from strategies.options_plugins.ma_option_definition_plugin_base import DefinitionDrivenPureOptionPlugin
from strategies.options_plugins.ma_1dte_reject_shared import (
    build_feature_frame_from_history,
    build_replay_cfg_from_extras,
    minute_floor,
    select_option_quote_for_underlying,
    safe_float,
)
from strategies.qqq_1dte_ma10_reject_live_strategy import QQQ1DTEMA10RejectLiveStrategy, _safe_float

class QQQ1DTEMa10RejectPlugin(DefinitionDrivenPureOptionPlugin):
    # Active MA option strategy tuning should be done here (plugin path),
    # not in legacy/non-enabled strategy entries.
    component_name = "qqq_1dte_ma10_reject_plugin"
    supports_pure_kernel = True

    def __init__(self) -> None:
        super().__init__()
        self._pure_underlying = "QQQ"

    def _definition_id(self) -> str:
        return "QQQ_MA_CROSS_STACK_CONT_V2_DUAL"

    def _ensure_pure_replay_strategy(self) -> Any:
        if self._pure_replay_strategy is None:
            from services.option_replay.strategies import get_strategy as get_replay_strategy

            strategy_id = str((self._last_extras or {}).get("replay_strategy_id") or "ma_cross_stack_cont_v2_dual_exp").strip().lower()
            self._pure_replay_strategy = get_replay_strategy(strategy_id)
            self._pure_replay_strategy_id = strategy_id
        return self._pure_replay_strategy

    def _get_replay_strategy_id(self) -> str:
        strategy_id = str((self._last_extras or {}).get("replay_strategy_id") or self._pure_replay_strategy_id or "ma_cross_stack_cont_v2_dual_exp").strip().lower()
        self._pure_replay_strategy_id = strategy_id
        return strategy_id

    @staticmethod
    def _bool_flag(value: Any) -> bool:
        return bool(str(value or "").strip())

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

    @staticmethod
    def _reason(label: str, ok: bool) -> str:
        return "" if ok else label

    def _build_setup_diagnostics(
        self,
        row: Any,
        cfg: Any,
        regime: str,
        feature_frame: Any,
        idx: int,
        cross_idx: Optional[int],
    ) -> Dict[str, str]:
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
            setup_reasons.extend(
                filter(
                    None,
                    [
                        self._reason("volume_below_min", vol_ratio >= float(cfg.volume_mult_min)),
                        self._reason("bull_low_not_touch_ma10", regime not in {"FRESH_BULL", "TREND_BULL"} or low <= ma10),
                        self._reason("bull_close_not_above_ma10", regime not in {"FRESH_BULL", "TREND_BULL"} or close_px > ma10),
                        self._reason("bull_close_strength_weak", regime not in {"FRESH_BULL", "TREND_BULL"} or close_pos >= float(cfg.close_strength_min)),
                        self._reason("bull_lower_wick_weak", regime not in {"FRESH_BULL", "TREND_BULL"} or lower_wick >= max(body, rng * float(cfg.lower_wick_min_frac))),
                        self._reason("bear_high_not_touch_ma10", regime not in {"FRESH_BEAR", "TREND_BEAR"} or high >= ma10),
                        self._reason("bear_close_not_below_ma10", regime not in {"FRESH_BEAR", "TREND_BEAR"} or close_px < ma10),
                        self._reason("bear_close_strength_weak", regime not in {"FRESH_BEAR", "TREND_BEAR"} or close_pos <= (1.0 - float(cfg.close_strength_min))),
                        self._reason("bear_upper_wick_weak", regime not in {"FRESH_BEAR", "TREND_BEAR"} or upper_wick >= max(body, rng * float(cfg.upper_wick_min_frac))),
                    ],
                )
            )
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
            fresh_reasons.extend(
                filter(
                    None,
                    [
                        self._reason("cross_too_old", bars_after_cross <= int(cfg.momentum_max_bars_after_cross)),
                        self._reason("momentum_volume_low", vol_ratio >= float(cfg.momentum_volume_mult_min)),
                        self._reason("momentum_body_small", body_frac >= float(cfg.momentum_body_min_frac)),
                        self._reason("fresh_bull_not_green", regime != "FRESH_BULL" or close_px > open_px),
                        self._reason("fresh_bull_not_above_ma10", regime != "FRESH_BULL" or close_px > ma10),
                        self._reason("fresh_bull_no_break_prev_high", regime != "FRESH_BULL" or (high > prev_high and close_px > prev_high)),
                        self._reason("fresh_bear_not_red", regime != "FRESH_BEAR" or close_px < open_px),
                        self._reason("fresh_bear_not_below_ma10", regime != "FRESH_BEAR" or close_px < ma10),
                        self._reason("fresh_bear_no_break_prev_low", regime != "FRESH_BEAR" or (low < prev_low and close_px < prev_low)),
                    ],
                )
            )
        diag["fresh_failures"] = "|".join(fresh_reasons[:6])

        trend_reasons: list[str] = []
        lookback = max(1, int(getattr(cfg, "trend_cont_breakout_lookback", 2)))
        prev_high_tc = prev_low_tc = 0.0
        if idx >= lookback:
            prev_slice = feature_frame.iloc[max(0, idx - lookback) : idx]
            if not prev_slice.empty:
                prev_high_tc = safe_float(prev_slice["high"].max())
                prev_low_tc = safe_float(prev_slice["low"].min())
        if not bool(getattr(cfg, "enable_trend_continuation_branch", False)):
            trend_reasons.append("branch_disabled")
        elif regime not in {"TREND_BULL", "TREND_BEAR"}:
            trend_reasons.append("regime_not_trend")
        else:
            trend_reasons.extend(
                filter(
                    None,
                    [
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
                    ],
                )
            )
        diag["trend_failures"] = "|".join(trend_reasons[:6])

        daily_reasons: list[str] = []
        if not bool(getattr(cfg, "enable_daily_best_additive_branch", False)):
            daily_reasons.append("branch_disabled")
        elif regime not in {"FRESH_BULL", "FRESH_BEAR", "TREND_BULL", "TREND_BEAR"}:
            daily_reasons.append("regime_not_supported")
        else:
            dist_ma10_atr = abs(close_px - ma10) / atr if ma10 > 0 else 999.0
            dist_vwap_atr = abs(close_px - vwap) / atr if vwap > 0 else 0.0
            daily_reasons.extend(
                filter(
                    None,
                    [
                        self._reason("daily_volume_low", vol_ratio >= float(getattr(cfg, "daily_best_volume_mult_min", 1.05))),
                        self._reason("daily_body_too_large", body_frac <= float(getattr(cfg, "daily_best_max_body_frac", 0.90))),
                        self._reason("daily_too_far_from_ma10", dist_ma10_atr <= float(getattr(cfg, "daily_best_max_dist_ma10_atr", 1.20))),
                        self._reason("daily_too_close_to_vwap", dist_vwap_atr >= float(getattr(cfg, "daily_best_min_dist_vwap_atr", 0.0))),
                    ],
                )
            )
        diag["daily_failures"] = "|".join(daily_reasons[:6])
        return diag

    def apply_default_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        original_extras = dict(extras or {})
        merged = dict(original_extras)
        meta = self._definition_metadata()
        merged.setdefault("definition_id", meta["definition_id"])
        merged.setdefault("audit_namespace", meta["audit_namespace"])
        merged.setdefault("position_namespace", meta["position_namespace"])
        # QQQ MA strategy no longer uses open interest as a contract gate.
        # Force this at the strategy layer so old config values cannot re-enable it.
        merged["min_open_interest"] = 0
        # Runner-strong continuation filters:
        # 1) short confirmation timeout (2 bars)
        # 2) premium expansion cap 8%
        # 3) require sustained weakness below MA10
        # 4) avoid late/over-extended PUT chases
        merged.setdefault("enable_trend_continuation_branch", True)
        merged.setdefault("continuation_pending_entry_timeout_bars", 2)
        merged.setdefault("continuation_dynamic_reprice_to_ask", True)
        merged.setdefault("continuation_max_premium_expansion_pct", 0.08)
        merged.setdefault("trend_cont_put_consecutive_bars_below_ma10", 2)
        merged.setdefault("put_close_pos_max_for_entry", 0.65)
        merged.setdefault("trend_cont_put_max_high_over_ma10_atr", 0.2)
        merged.setdefault("replay_strategy_id", "ma_cross_stack_cont_v2_dual_exp")
        # Dual setup defaults: keep QQQ v2 aligned with active SPYDualSignalModule
        # even when legacy branch flags are present in config.
        merged.setdefault("enable_momentum_no_retest_dual", True)
        merged.setdefault("momentum_breakout_lookback_bars", 5)
        merged.setdefault("momentum_higher_low_bars", 3)
        merged.setdefault("momentum_lower_high_bars", 3)
        merged.setdefault("momentum_ma10_slope_min_abs", 0.02)
        merged.setdefault("momentum_close_pos_min_call", 0.70)
        merged.setdefault("momentum_close_pos_max_put", 0.30)
        merged.setdefault("momentum_body_frac_min", 0.50)
        merged.setdefault("momentum_vol_ratio_min", 0.85)
        merged.setdefault("momentum_dist_ma10_atr_max", 2.2)
        merged.setdefault("risk_symbol", meta["risk_symbol"])
        merged.setdefault("enable_live_parity_snapshot", True)
        merged.setdefault("enable_live_shadow_mode", False)
        merged.setdefault("live_parity_profile_id", "qqq_option_ma_ext_v1")
        merged.setdefault("market_data_profile_id", "standard_close_extended_sma")
        merged.setdefault("kernel_mode", "pure_plugin")
        merged.setdefault("strict_scope_validation", True)
        merged = self._finalize_definition_extras(original_extras, merged)
        self._last_extras = dict(merged)
        return merged

    def build_delegate(self, ctx: StrategyContext, params: StrategyParams) -> StrategyAlgorithm:
        return QQQ1DTEMA10RejectLiveStrategy(ctx, params)

    async def initialize_pure(self, ctx: StrategyContext, params: StrategyParams, kernel: Any) -> None:
        extras = dict(params.extras or {})
        extras["min_open_interest"] = 0
        self._strategy_definition = definition_registry.get(self._definition_id())
        extras.setdefault("underlying", str(self._strategy_definition.underlying))
        extras.setdefault("risk_symbol", str(self._strategy_definition.risk_symbol))
        extras.setdefault("replay_strategy_id", str(self._strategy_definition.extras.get("replay_strategy_id") or "ma10_reject_0324"))
        underlying = str(extras.get("underlying", "QQQ")).strip().upper()
        self._pure_strategy_id = str(ctx.strategy_id)
        self._pure_underlying = underlying
        self._pure_log = getattr(ctx, "log", None)
        self._pure_day_block_counts = {}
        self._last_extras = dict(extras)
        params.extras = dict(extras)
        replay_strategy_id = self._get_replay_strategy_id()
        preload_bars = max(0, int(extras.get("preload_history_bars", 260) or 260))
        if preload_bars > 0:
            try:
                rows = await ctx.history(underlying, "1m", preload_bars)
                loaded = kernel.preload_bar_history(underlying, list(rows or []))
                ctx.log.info(
                    "option_kernel_pure_preload_ok",
                    strategy_id=str(ctx.strategy_id),
                    plugin=str(self.component_name),
                    underlying=underlying,
                    resolution="1m",
                    loaded_bars=int(loaded),
                )
            except Exception as exc:
                ctx.log.warning(
                    "option_kernel_pure_preload_failed",
                    strategy_id=str(ctx.strategy_id),
                    plugin=str(self.component_name),
                    underlying=underlying,
                    resolution="1m",
                    error=str(exc),
                )
        await ctx.subscribe_features([underlying], bar_sec=60)
        ctx.log.info(
            "option_kernel_pure_subscribed",
            strategy_id=str(ctx.strategy_id),
            plugin=str(self.component_name),
            underlying=underlying,
            bar_sec=60,
        )
        snapshot = {
            "definition_id": self._definition_id(),
            "replay_strategy_id": replay_strategy_id,
            "underlying": underlying,
            "signal_module_id": str(getattr(self._strategy_definition.signal_module, "module_id", "")),
            "contract_policy_id": str(getattr(self._strategy_definition.contract_policy, "policy_id", "")),
            "execution_policy_id": str(getattr(self._strategy_definition.execution_policy, "policy_id", "")),
            "position_policy_id": str(getattr(self._strategy_definition.position_policy, "policy_id", "")),
            "kernel_mode": str(extras.get("kernel_mode") or ""),
            "ab_time_start": str(extras.get("ab_time_start") or ""),
            "ab_time_end": str(extras.get("ab_time_end") or ""),
            "entry_start_am": str(extras.get("entry_start_am") or ""),
            "entry_end_am": str(extras.get("entry_end_am") or ""),
            "entry_start_pm": str(extras.get("entry_start_pm") or ""),
            "entry_end_pm": str(extras.get("entry_end_pm") or ""),
            "no_new_entry_after": str(extras.get("no_new_entry_after") or ""),
            "force_flatten_time": str(extras.get("force_flatten_time") or ""),
            "confirm_window_bars": int(extras.get("confirm_window_bars", 2) or 2),
            "confirm_decay_from_bar": int(extras.get("confirm_decay_from_bar", 99) or 99),
            "confirm_decay_max_distance_atr": float(extras.get("confirm_decay_max_distance_atr", 0.30) or 0.30),
            "continuation_pending_entry_timeout_bars": int(extras.get("continuation_pending_entry_timeout_bars", 2) or 2),
            "continuation_max_premium_expansion_pct": float(extras.get("continuation_max_premium_expansion_pct", 0.08) or 0.08),
            "trend_cont_put_consecutive_bars_below_ma10": int(extras.get("trend_cont_put_consecutive_bars_below_ma10", 2) or 2),
            "put_close_pos_max_for_entry": float(extras.get("put_close_pos_max_for_entry", 0.65) or 0.65),
            "trend_cont_put_max_high_over_ma10_atr": float(extras.get("trend_cont_put_max_high_over_ma10_atr", 0.2) or 0.2),
            "ma10_reject_ext_atr_max": float(extras.get("ma10_reject_ext_atr_max", 2.2) or 2.2),
            "enable_fresh_momentum_branch": bool(extras.get("enable_fresh_momentum_branch", True)),
            "enable_trend_continuation_branch": bool(extras.get("enable_trend_continuation_branch", True)),
            "enable_daily_best_additive_branch": bool(extras.get("enable_daily_best_additive_branch", True)),
            "daily_best_call_only": bool(extras.get("daily_best_call_only", True)),
            "daily_best_min_dist_vwap_atr": float(extras.get("daily_best_min_dist_vwap_atr", 2.0) or 2.0),
            "dte_preferred": int(extras.get("dte_preferred", 1) or 1),
            "dte_fallback_max": int(extras.get("dte_fallback_max", 2) or 2),
            "delta_min": float(extras.get("delta_min", 0.45) or 0.45),
            "delta_max": float(extras.get("delta_max", 0.55) or 0.55),
            "delta_fallback_min": float(extras.get("delta_fallback_min", 0.40) or 0.40),
            "delta_fallback_max": float(extras.get("delta_fallback_max", 0.60) or 0.60),
            "spread_max_pct": float(extras.get("spread_max_pct", 0.12) or 0.12),
            "min_open_interest": int(extras.get("min_open_interest", 100) or 100),
            "adaptive_entry_improve_pct": float(extras.get("adaptive_entry_improve_pct", 0.2) or 0.2),
            "urgent_entry_improve_pct": float(extras.get("urgent_entry_improve_pct", 0.05) or 0.05),
            "time_stop_soft_bars": int(extras.get("time_stop_soft_bars", 5) or 5),
            "time_stop_hard_bars": int(extras.get("time_stop_hard_bars", 10) or 10),
            "v2_max_dist_ma10_atr": float(extras.get("v2_max_dist_ma10_atr", 1.8) or 1.8),
            "v2_put_close_pos_max": float(extras.get("v2_put_close_pos_max", 1.0) or 1.0),
            "v2_min_vol_ratio": float(extras.get("v2_min_vol_ratio", 0.0) or 0.0),
            "v2_max_ma10_slope": float(extras.get("v2_max_ma10_slope", 999.0) or 999.0),
            "v2_put_breakdown_close_pos_min": float(extras.get("v2_put_breakdown_close_pos_min", 0.45) or 0.45),
            "v2_put_pullback_body_frac_min": float(extras.get("v2_put_pullback_body_frac_min", 0.55) or 0.55),
            "v2_put_pullback_vol_ratio_min": float(extras.get("v2_put_pullback_vol_ratio_min", 0.80) or 0.80),
            "v2_put_pullback_ma10_slope_max": float(extras.get("v2_put_pullback_ma10_slope_max", 0.06) or 0.06),
            "v2_call_breakout_close_pos_min": float(extras.get("v2_call_breakout_close_pos_min", 0.70) or 0.70),
            "v2_call_pullback_body_frac_min": float(extras.get("v2_call_pullback_body_frac_min", 0.45) or 0.45),
            "v2_call_pullback_vol_ratio_min": float(extras.get("v2_call_pullback_vol_ratio_min", 0.90) or 0.90),
            "v2_call_pullback_ma10_slope_min": float(extras.get("v2_call_pullback_ma10_slope_min", 0.08) or 0.08),
            "contracts": int(extras.get("contracts", 4) or 4),
        }
        ctx.log.info(
            "option_ma_strategy_snapshot",
            strategy_id=str(ctx.strategy_id),
            plugin=str(self.component_name),
            underlying=underlying,
            replay_strategy_id=replay_strategy_id,
            config_hash=hashlib.md5(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()[:12],
            snapshot=snapshot,
        )

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
        if log is None:
            return
        row = history[-1] if history else {}
        wall_now = runtime.wall_now if isinstance(runtime.wall_now, datetime) else runtime.now
        minute = wall_now.strftime("%Y-%m-%d %H:%M")
        bar_minute = runtime.now.strftime("%Y-%m-%d %H:%M")
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
        try:
            log.info(
                "qqq_1dte_ma_minute_heartbeat",
                ts=minute + ":00",
                bar_ts=bar_minute + ":00",
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
                setup_failures=str((diag or {}).get("setup_failures") or ""),
                fresh_failures=str((diag or {}).get("fresh_failures") or ""),
                trend_failures=str((diag or {}).get("trend_failures") or ""),
                daily_failures=str((diag or {}).get("daily_failures") or ""),
                gate_stage=str(gate_stage or ""),
                gate_reason=str(gate_reason or ""),
                runtime_lag_sec=float((wall_now - runtime.now).total_seconds()) if isinstance(runtime.now, datetime) else 0.0,
            )
        except Exception:
            pass
        if not reason:
            return
        day_count = int(self._pure_day_block_counts.get(reason, 0)) + 1
        self._pure_day_block_counts[reason] = day_count
        try:
            log.info(
                "qqq_1dte_ma_entry_block",
                ts=minute + ":00",
                minute=minute,
                bar_ts=bar_minute + ":00",
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
                setup_failures=str((diag or {}).get("setup_failures") or ""),
                fresh_failures=str((diag or {}).get("fresh_failures") or ""),
                trend_failures=str((diag or {}).get("trend_failures") or ""),
                daily_failures=str((diag or {}).get("daily_failures") or ""),
            )
        except Exception:
            pass

    def build_signal(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> Optional[Signal]:
        if not isinstance(strategy, QQQ1DTEMA10RejectLiveStrategy):
            return None
        df = strategy._build_feature_frame()
        if df.empty or len(df) < 210:
            return None
        idx = len(df) - 1
        row = df.iloc[idx]
        cfg = strategy._replay_cfg(spread_max_pct=float((strategy.params.extras or {}).get("spread_max_pct", 0.12) or 0.12))
        detected = strategy._replay_strategy.detect_signal(df, idx, row, cfg)
        if detected is None:
            return None
        return Signal(
            ts=ctx.get("now") or datetime.now(),
            side=str(detected.side),
            signal_kind=str(detected.signal_kind),
            grade=str(detected.grade),
            regime=str(detected.regime),
            payload={
                "setup_idx": int(idx),
                "close": _safe_float(row.get("close")),
            },
        )

    def build_entry_plan(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any], signal: Signal) -> Optional[EntryPlan]:
        if not isinstance(strategy, QQQ1DTEMA10RejectLiveStrategy):
            return None
        plan = dict(getattr(strategy, "_pending_entry_plan", None) or getattr(strategy, "_active_entry_plan_for_submit", None) or {})
        if not plan:
            return None
        return EntryPlan(
            ts=ctx.get("now") or datetime.now(),
            side=str(signal.side),
            signal_kind=str(plan.get("signal_kind") or signal.signal_kind),
            structure_stop=float(plan.get("structure_stop") or 0.0),
            target_1r=float(plan.get("target_1r") or 0.0),
            target_1p2r=float(plan.get("target_1p2r") or 0.0),
            r_per_share=float(plan.get("r_per_share") or 0.0),
            contracts=int(plan.get("initial_contracts") or int((strategy.params.extras or {}).get("contracts", 4) or 4)),
            payload=plan,
        )

    def select_contract(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any], entry_plan: EntryPlan) -> Optional[Selection]:
        if not isinstance(strategy, QQQ1DTEMA10RejectLiveStrategy):
            return None
        now = ctx.get("now")
        if not isinstance(now, datetime):
            return None
        quote, reason = strategy._select_0dte_quote(
            now,
            side=str(entry_plan.side),
            spot=float(ctx.get("spot") or 0.0),
            spread_max_pct=float((strategy.params.extras or {}).get("spread_max_pct", 0.12) or 0.12),
            now_for_quote_age=now,
        )
        if quote is None:
            return None
        return Selection(
            opt_symbol=str(quote.get("opt_symbol") or ""),
            expiry=str(quote.get("expiry") or ""),
            strike=float(quote.get("strike") or 0.0),
            side=str(entry_plan.side),
            bid=float(quote.get("bid") or 0.0),
            ask=float(quote.get("ask") or 0.0),
            mid=float(quote.get("mid") or 0.0),
            last=float(quote.get("last") or 0.0),
            quote_age_sec=float(quote.get("quote_age_sec") or 0.0),
            payload={"reject_reason": str(reason or "")},
        )

    def select_quote(self, strategy: StrategyAlgorithm, req: Dict[str, Any]) -> Optional[Selection]:
        if not isinstance(strategy, QQQ1DTEMA10RejectLiveStrategy):
            return None
        ctx = dict(req.get("ctx") or {})
        entry_plan = req.get("entry_plan")
        if isinstance(entry_plan, dict):
            plan = EntryPlan(
                ts=ctx.get("now") or datetime.now(),
                side=str(entry_plan.get("side") or ""),
                signal_kind=str(entry_plan.get("signal_kind") or "ma10_reject"),
                structure_stop=float(entry_plan.get("structure_stop") or 0.0),
                target_1r=float(entry_plan.get("target_1r") or 0.0),
                target_1p2r=float(entry_plan.get("target_1p2r") or 0.0),
                r_per_share=float(entry_plan.get("r_per_share") or 0.0),
                contracts=int(entry_plan.get("contracts") or int((strategy.params.extras or {}).get("contracts", 4) or 4)),
                payload=dict(entry_plan.get("payload") or {}),
            )
            return self.select_contract(strategy, ctx, plan)
        return None

    def build_execution_profile(
        self,
        strategy: StrategyAlgorithm,
        ctx: Dict[str, Any],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        extras = getattr(strategy, "params", None).extras if getattr(strategy, "params", None) is not None else {}
        return build_execution_profile_from_extras(
            profile_id=str((extras or {}).get("live_parity_profile_id") or "qqq_option_ma_ext_v1"),
            market_data_profile_id=str((extras or {}).get("market_data_profile_id") or "standard_close_extended_sma"),
            extras=extras,
        )

    def build_exit_plan(
        self,
        strategy: StrategyAlgorithm,
        ctx: Dict[str, Any],
        market_state: Dict[str, Any],
    ) -> Optional[ExitPlan]:
        if not isinstance(strategy, QQQ1DTEMA10RejectLiveStrategy):
            return None
        pos = getattr(strategy, "_pos", None)
        if pos is None:
            return None
        reason = str(getattr(pos, "last_exit_reason", "") or "").strip()
        if not reason:
            return None
        return ExitPlan(
            ts=ctx.get("now") or datetime.now(),
            position_lot_id=str(getattr(pos, "position_id", "") or ""),
            qty=float(market_state.get("qty") or 0.0),
            reason=reason,
            payload={
                "opt_symbol": str(getattr(pos, "opt_symbol", "") or ""),
                "entry_snapshot_id": str(getattr(pos, "entry_snapshot_id", "") or ""),
            },
        )

    async def maybe_enter(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> bool:
        if not isinstance(strategy, QQQ1DTEMA10RejectLiveStrategy):
            return False
        now = ctx.get("now")
        bar = ctx.get("bar")
        if not isinstance(now, datetime) or not isinstance(bar, dict):
            return False
        await strategy._maybe_enter(now, dict(bar), wall_now=ctx.get("wall_now"))
        return True

    async def sync_and_manage_position(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> bool:
        if not isinstance(strategy, QQQ1DTEMA10RejectLiveStrategy):
            return False
        now = ctx.get("now")
        bar = ctx.get("bar")
        if not isinstance(now, datetime) or not isinstance(bar, dict):
            return False
        return bool(await strategy._sync_and_manage_position(now, dict(bar)))

    def _pure_reset_if_needed(self, runtime: PluginRuntimeState) -> None:
        day = runtime.now.date()
        if self._pure_last_day != day:
            self._pure_last_day = day
            self._pure_pending_setup = None
            self._pure_entry_plan = None
            self._pure_trades_filled_today = 0
            self._pure_quote_health_block_day = False
            self._pure_day_block_counts = {}
            if self._strategy_definition is not None:
                self._strategy_definition.signal_module.reset_for_new_day(
                    SignalContext(runtime=runtime, extras=getattr(self, "_last_extras", {}) or {})
                )
            else:
                replay_strategy = self._ensure_pure_replay_strategy()
                replay_strategy.on_new_day()

    @staticmethod
    def _parse_hms(value: Any, default: time) -> time:
        raw = str(value or "").strip()
        if not raw:
            return default
        try:
            hh, mm, ss = [int(part) for part in raw.split(":")]
            return time(hh, mm, ss)
        except Exception:
            return default

    def _is_in_entry_window(self, now: datetime, extras: Dict[str, Any]) -> bool:
        now_t = now.time()
        am_start = self._parse_hms(
            extras.get("entry_start_am", "09:40:00"),
            self._parse_hms(extras.get("ab_time_start", "09:40:00"), time(9, 40)),
        )
        am_end = self._parse_hms(extras.get("entry_end_am", "12:00:00"), time(12, 0))
        pm_start = self._parse_hms(extras.get("entry_start_pm", "13:00:00"), time(13, 0))
        pm_end = self._parse_hms(
            extras.get("entry_end_pm", "15:00:00"),
            self._parse_hms(extras.get("ab_time_end", "15:00:00"), time(15, 0)),
        )
        no_new_after = self._parse_hms(extras.get("no_new_entry_after", "15:00:00"), time(15, 0))
        if now_t > no_new_after:
            return False
        return bool((am_start <= now_t <= am_end) or (pm_start <= now_t <= pm_end))

    @staticmethod
    def _find_entry_index(history: list[Dict[str, Any]], opened_at: datetime) -> Optional[int]:
        for idx, bar in enumerate(history):
            ts = bar.get("ts")
            if isinstance(ts, datetime) and ts >= opened_at:
                return idx
        return None

    @staticmethod
    def _bars_since_setup(now: datetime, setup_ts: datetime) -> int:
        try:
            now_minute = minute_floor(now)
            setup_minute = minute_floor(setup_ts)
            delta_sec = (now_minute - setup_minute).total_seconds()
            return max(0, int(round(delta_sec / 60.0)))
        except Exception:
            return 0

    @staticmethod
    def _best_fav_stats(history: list[Dict[str, Any]], *, entry_idx: int, side: str, entry_underlying: float, r_per_share: float) -> tuple[float, int]:
        best_r = 0.0
        best_idx = entry_idx
        denom = max(0.01, float(r_per_share or 0.0))
        for idx in range(entry_idx, len(history)):
            row = history[idx]
            high_px = safe_float(row.get("high"))
            low_px = safe_float(row.get("low"))
            fav_move_r = (high_px - entry_underlying) / denom if side == "CALL" else (entry_underlying - low_px) / denom
            if fav_move_r > best_r:
                best_r = float(fav_move_r)
                best_idx = idx
        return best_r, best_idx

    def build_signal_pure(self, runtime: PluginRuntimeState) -> Optional[Signal]:
        self._pure_reset_if_needed(runtime)
        history = list(runtime.payload.get("bar_history") or [])
        extras = getattr(self, "_last_extras", {}) or {}
        signal_module = self._resolve_strategy_definition().signal_module
        feature_row = None
        if history:
            try:
                feature_frame = build_feature_frame_from_history(str(self._pure_underlying or runtime.symbol), history)
                if not feature_frame.empty:
                    feature_row = feature_frame.iloc[-1]
            except Exception:
                feature_row = None
        signal = signal_module.build_signal(SignalContext(runtime=runtime, extras=extras))
        self._pure_pending_setup = getattr(signal_module, "_pending_setup", None)
        self._pure_entry_plan = getattr(signal_module, "_entry_plan", None)
        diag = dict(getattr(signal_module, "last_diag", {}) or {})
        gate_stage, gate_reason = getattr(signal_module, "last_block", ("", ""))
        reason = None if gate_reason == "SIGNAL_READY" else gate_reason or "NO_SIGNAL"
        self._emit_minute_observability(
            runtime,
            history=history,
            has_pending=bool(self._pure_pending_setup is not None),
            reason=reason,
            diag=diag,
            feature_row=feature_row,
            gate_stage=str(gate_stage or ""),
            gate_reason=str(gate_reason or ""),
        )
        return signal

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal) -> Optional[EntryPlan]:
        plan = self._resolve_strategy_definition().signal_module.build_entry_plan(
            SignalContext(runtime=runtime, extras=getattr(self, "_last_extras", {}) or {}),
            signal,
        )
        self._pure_entry_plan = None
        return plan

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan) -> Optional[Selection]:
        return self._resolve_strategy_definition().contract_policy.select_contract(
            ContractSelectionContext(runtime=runtime, entry_plan=entry_plan, extras=getattr(self, "_last_extras", {}) or {})
        )

    def build_execution_profile_pure(
        self,
        runtime: PluginRuntimeState,
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        if self._strategy_definition is None:
            extras = getattr(self, "_last_extras", {}) or {}
            return build_execution_profile_from_extras(
                profile_id=str((extras or {}).get("live_parity_profile_id") or "qqq_option_ma_ext_v1"),
                market_data_profile_id=str((extras or {}).get("market_data_profile_id") or "standard_close_extended_sma"),
                extras=extras,
            )
        return self._resolve_strategy_definition().execution_policy.build_execution_profile(
            ExecutionContext(runtime=runtime, entry_plan=entry_plan, selection=selection, extras=getattr(self, "_last_extras", {}) or {})
        )

    def on_entry_submitted_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        position_lot_id: str,
    ) -> None:
        self._pure_trades_filled_today += 1

    def build_exit_plan_pure(self, runtime: PluginRuntimeState) -> Optional[ExitPlan]:
        return self._resolve_strategy_definition().position_policy.build_exit_plan(
            PositionContext(runtime=runtime, extras=getattr(self, "_last_extras", {}) or {})
        )
