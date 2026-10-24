from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, Optional

from strategies.options_kernel.modules import ContractSelectionContext, SignalContext
from strategies.options_kernel.modules.shared import is_in_split_entry_window
from strategies.options_kernel.types import EntryPlan, PluginRuntimeState, Selection, Signal
from strategies.options_plugins.ma_option_definition_plugin_base import DefinitionDrivenPureOptionPlugin
from strategies.qqq_intraday_rules import QQQIntradayRuleEngine, load_entry_recipe, resolve_qqq_truth_fabric
from strategies.qqq_stock_signal_artifact import (
    get_pending_stock_entry_signal,
    get_latest_stock_entry_signal,
    record_stock_option_signal_handoff,
    signal_plane_health,
)


class QQQIntradayOptionPlugin(DefinitionDrivenPureOptionPlugin):
    component_name = "qqq_intraday_option_plugin"

    def __init__(self) -> None:
        super().__init__()
        self._pure_consumed_source_keys: set[str] = set()

    def _definition_id(self) -> str:
        return "QQQ_INTRADAY_OPTION_PARITY_V1"

    def _mark_strategy_tradability_blocker(
        self,
        *,
        runtime: PluginRuntimeState,
        reason: str,
        stage: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            from services.data_availability import get_data_availability_service

            get_data_availability_service().mark_unavailable(
                dataset="strategy_tradability",
                symbol=str(self._pure_underlying or runtime.symbol),
                resolution="entry",
                reason=str(reason or "strategy_not_tradable"),
                source="qqq_intraday_option",
                strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                stage=str(stage or "option_entry"),
                severity="entry_blocking",
                error=str((payload or {}).get("error") or ""),
                now=runtime.now,
            )
        except Exception:
            pass

    def _clear_strategy_tradability_blocker(
        self,
        *,
        runtime: PluginRuntimeState,
        stage: str,
    ) -> None:
        try:
            from services.data_availability import get_data_availability_service

            get_data_availability_service().clear(
                dataset="strategy_tradability",
                symbol=str(self._pure_underlying or runtime.symbol),
                resolution="entry",
                strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                stage=str(stage or "option_entry"),
            )
        except Exception:
            pass

    def _source_trigger_payload(self, signal: Optional[Signal]) -> Dict[str, Any]:
        return dict(getattr(signal, "payload", {}) or {}) if signal is not None else {}

    def _record_source_handoff_terminal(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        artifact: Any = None,
        status: str,
        reason: str,
        terminal: bool = True,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        signal_payload = self._source_trigger_payload(signal)
        payload_d = dict(payload or {})
        trigger_key = str(signal_payload.get("source_trigger_key") or payload_d.get("source_trigger_key") or "").strip()
        trigger_id = str(signal_payload.get("source_trigger_id") or payload_d.get("source_trigger_id") or "").strip()
        source_strategy_id = str(
            signal_payload.get("source_strategy_id") or payload_d.get("source_strategy_id") or ""
        ).strip()
        if artifact is not None:
            trigger_id = trigger_id or str(getattr(artifact, "trigger_id", "") or "").strip()
            source_strategy_id = source_strategy_id or str(getattr(artifact, "strategy_id", "") or "").strip()
        try:
            record_stock_option_signal_handoff(
                artifact=artifact,
                trigger_key=trigger_key,
                trigger_id=trigger_id,
                source_strategy_id=source_strategy_id,
                consumer_strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                underlying=str(self._pure_underlying or runtime.symbol),
                signal_family=str(signal_payload.get("source_signal_family") or "qqq_intraday_entry"),
                status=str(status or "unknown"),
                reason=str(reason or ""),
                terminal=terminal,
                ts=runtime.now,
                payload=payload_d,
            )
        except Exception:
            pass

    def apply_default_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        original_extras = dict(extras or {})
        merged = dict(original_extras)
        meta = self._definition_metadata()
        merged.setdefault("definition_id", meta["definition_id"])
        merged.setdefault("audit_namespace", meta["audit_namespace"])
        merged.setdefault("underlying", "QQQ")
        merged.setdefault("risk_symbol", meta["risk_symbol"] or "OPT:QQQ:0DTE")
        merged.setdefault("kernel_mode", "pure_plugin")
        merged.setdefault("strict_scope_validation", True)
        merged.setdefault("enable_live_parity_snapshot", True)
        merged.setdefault("enable_live_shadow_mode", False)
        merged.setdefault("live_parity_profile_id", "qqq_intraday_option_parity_v1")
        merged.setdefault("market_data_profile_id", "standard_close_extended_sma")
        merged.setdefault("contracts", 2)
        merged.setdefault("entry_start_am", "09:40:00")
        merged.setdefault("entry_end_am", "12:00:00")
        merged.setdefault("entry_start_pm", "12:00:00")
        merged.setdefault("entry_end_pm", "15:00:00")
        merged.setdefault("no_new_entry_after", "15:00:00")
        merged.setdefault("force_flatten_time", "15:50:00")
        merged.setdefault("dte_preferred", 0)
        merged.setdefault("dte_fallback_max", 1)
        merged.setdefault("delta_min", 0.35)
        merged.setdefault("delta_max", 0.60)
        merged.setdefault("delta_fallback_min", 0.30)
        merged.setdefault("delta_fallback_max", 0.65)
        merged.setdefault("spread_max_pct", 0.15)
        merged.setdefault("min_open_interest", 0)
        merged.setdefault("hard_stop_atr_mult", 0.75)
        merged.setdefault("target_1r_atr_mult", 0.75)
        merged.setdefault("target_1p2r_atr_mult", 1.20)
        merged.setdefault("logic_retreat_min_hold_sec", 360)
        merged.setdefault("logic_retreat_v3m_atr_mult", 0.10)
        merged.setdefault("entry_model", "v47_fast_drift")
        merged.setdefault("qqew_decoupled", True)
        merged.setdefault("entry_max_data_age_sec", 3.0)
        merged.setdefault("entry_max_data_age_sec_qqq", 3.0)
        merged.setdefault("entry_max_data_age_sec_qqew", 3.0)
        merged.setdefault("entry_require_continuity_180s", False)
        merged.setdefault("volume_guard_require_ma10", False)
        merged.setdefault("source_bar_sec", 5)
        merged.setdefault("signal_source_mode", "upstream_stock_signal")
        merged.setdefault("signal_source_source_strategy_id", "qqq_intraday_plugin_001")
        merged.setdefault("signal_source_signal_family", "qqq_intraday_entry")
        merged.setdefault("signal_source_require_source_alive", True)
        merged.setdefault("signal_source_max_signal_age_sec", 10.0)
        merged.setdefault("signal_source_fallback_mode", "shadow_only")
        # Upstream stock signal already encodes full entry conviction; ensure
        # the envelope carries an edge ≥ expected 0DTE cost (≈55–65bp) so the
        # decision validator's edge-vs-cost check does not block the piggyback
        # intent. 0.85 → 85bp of edge with buffer above typical 0DTE costs.
        merged.setdefault("entry_signal_score", 0.85)
        # Reduce routine log noise in live trading; keep actionable block/result logs.
        merged.setdefault("minute_heartbeat_log_enabled", False)
        # QQQ option intraday should recover from late strategy startup and trade
        # the rest of the day on valid signals instead of inheriting stock-only
        # opening embargo / restart-degrade behavior.
        merged.setdefault("startup_anchor_entry_block_enabled", False)
        merged.setdefault("restart_degrade_enabled", False)
        merged.setdefault(
            "entry_recipe",
            {
                "session_module": "fast_drift_window",
                "direction_module": "fast_drift_direction",
                "guards": [
                    "amp_lock_guard",
                    "stable_entry_guard",
                    "restart_degrade_guard",
                    "startup_anchor_guard",
                    "open_orders_guard",
                    "live_position_guard",
                    "pending_decision_sync_guard",
                    "ownership_guard",
                    "qqq_freshness_guard",
                    "qqew_freshness_guard",
                    "continuity_180s_guard",
                    "atr_ready_guard",
                    "vwap_ready_guard",
                    "volume_ready_guard",
                ],
                "overlays": [
                    "extreme_overlay",
                    "anti_chase_overlay",
                    "reentry_cooldown_overlay",
                    "high_vol_confirm_veto",
                ],
                "translator": "qqq_option_translation",
            },
        )
        merged = self._finalize_definition_extras(original_extras, merged)
        self._signal_source_config(merged)
        self._last_extras = dict(merged)
        return merged

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        raw = str(value).strip().lower()
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off"}:
            return False
        return bool(default)

    def _signal_source_config(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        source_cfg = dict(extras.get("signal_source") or {})
        mode = str(
            extras.get("signal_source_mode")
            or source_cfg.get("mode")
            or "upstream_stock_signal"
        ).strip().lower()
        source_strategy_id = str(
            extras.get("signal_source_source_strategy_id")
            or source_cfg.get("source_strategy_id")
            or ""
        ).strip()
        if mode == "upstream_stock_signal" and not source_strategy_id:
            source_strategy_id = "qqq_intraday_plugin_001"
        source_signal_family = str(
            extras.get("signal_source_signal_family")
            or source_cfg.get("source_signal_family")
            or "qqq_intraday_entry"
        ).strip().lower()
        require_source_alive = self._coerce_bool(
            extras.get("signal_source_require_source_alive", source_cfg.get("require_source_alive")),
            True,
        )
        try:
            max_signal_age_sec = float(
                extras.get("signal_source_max_signal_age_sec")
                if "signal_source_max_signal_age_sec" in extras
                else source_cfg.get("max_signal_age_sec", 5.0)
            )
        except Exception:
            max_signal_age_sec = 5.0
        max_signal_age_sec = max(0.5, max_signal_age_sec)
        fallback_mode = str(
            extras.get("signal_source_fallback_mode")
            or source_cfg.get("fallback_mode")
            or "shadow_only"
        ).strip().lower()
        shadow_only = self._coerce_bool(
            extras.get("signal_source_shadow_only", source_cfg.get("shadow_only")),
            False if mode == "upstream_stock_signal" else True,
        )
        if mode not in {"upstream_stock_signal", "independent_rule_replay"}:
            raise ValueError(f"invalid signal_source_mode={mode}")
        if fallback_mode not in {"shadow_only", "disabled"}:
            raise ValueError(f"invalid signal_source_fallback_mode={fallback_mode}")
        return {
            "mode": mode,
            "source_strategy_id": source_strategy_id,
            "source_signal_family": source_signal_family,
            "require_source_alive": require_source_alive,
            "max_signal_age_sec": max_signal_age_sec,
            "fallback_mode": fallback_mode,
            "shadow_only": shadow_only,
        }

    @staticmethod
    def _normalized_signal_payload(
        *,
        mode: str,
        source_strategy_id: str,
        source_signal_family: str,
        age_sec: float,
        direction: str,
        extra: Dict[str, Any],
    ) -> Dict[str, Any]:
        option_side = "CALL" if str(direction).upper() == "LONG" else "PUT"
        return {
            "signal_source": str(mode),
            "signal_source_mode": str(mode),
            "signal_source_strategy_id": str(source_strategy_id),
            "signal_source_signal_family": str(source_signal_family),
            "signal_source_age_sec": float(age_sec),
            "stock_signal_direction": str(direction),
            "normalized_direction": str(direction),
            "normalized_option_side": option_side,
            **dict(extra or {}),
        }

    def _emit_qqq_intraday_observability(
        self,
        runtime: PluginRuntimeState,
        evaluation,
        *,
        signal: Optional[Signal],
        signal_source_meta: Dict[str, Any],
        emit_entry_block: bool = True,
    ) -> None:
        log = self._pure_log
        if log is None:
            return
        ts = runtime.now
        minute = ts.strftime("%Y-%m-%d %H:%M")
        ctx = evaluation.context
        gate_reason = str(evaluation.gate_reason or "")
        gate_payload = dict(evaluation.gate_payload or {})
        translation = evaluation.translation
        source_status = str(signal_source_meta.get("signal_source_status") or "")
        source_mode = str(signal_source_meta.get("signal_source_mode") or "")
        stock_side_logged = str(translation.stock_side)
        if source_mode == "upstream_stock_signal" and signal is not None:
            upstream_dir = str((signal.payload or {}).get("stock_signal_direction") or "").strip().upper()
            if upstream_dir in {"LONG", "SHORT"}:
                stock_side_logged = upstream_dir
        source_block_reason = {
            "stale": "source_signal_stale",
            "blocked": "source_signal_blocked",
            "invalid": "source_signal_invalid",
            "suppressed": "source_signal_suppressed",
        }.get(source_status)
        if source_block_reason in {"source_signal_stale", "source_signal_blocked", "source_signal_invalid"}:
            self._mark_strategy_tradability_blocker(
                runtime=runtime,
                reason=source_block_reason,
                stage="upstream_signal",
                payload=dict(signal_source_meta or {}),
            )
        elif source_status == "live":
            self._clear_strategy_tradability_blocker(runtime=runtime, stage="upstream_signal")

        # Resolve c0..c3 minute closes for diagnostics
        def _mc(n: int) -> Optional[float]:
            target = ts.replace(second=0, microsecond=0) - timedelta(minutes=n)
            return ctx.close_by_minute.get(target)

        c0, c1, c2, c3 = _mc(0), _mc(1), _mc(2), _mc(3)

        self._emit_structured_source_observability(
            runtime=runtime,
            minute=minute,
            signal=signal,
            signal_source_meta=signal_source_meta,
            source_block_reason=source_block_reason,
            gate_reason=gate_reason,
            translation_allowed=bool(translation.allowed),
        )

        heartbeat_enabled = self._coerce_bool(
            (self._last_extras or {}).get("minute_heartbeat_log_enabled"),
            False,
        )
        if heartbeat_enabled:
            try:
                log.info(
                    "qqq_intraday_option_minute_heartbeat",
                    ts=minute + ":00",
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    underlying=str(self._pure_underlying or runtime.symbol),
                    has_position=bool(runtime.active_position is not None or runtime.active_positions),
                    # price / vwap
                    qqq_now=float(ctx.qqq_now),
                    vwap_now=float(ctx.vwap_now),
                    distance_from_vwap_atr=float(ctx.distance_from_vwap_atr),
                    qqew_now=float(ctx.qqew_now) if ctx.qqew_now is not None else None,
                    qqew_vwap_now=float(ctx.qqew_vwap_now) if ctx.qqew_vwap_now is not None else None,
                    # velocity / ATR
                    v3m_signed=float(ctx.v3m_signed),
                    atr_live=float(ctx.atr_live),
                    atr_seed=float(ctx.atr_seed),
                    atr_eff=float(ctx.atr_eff),
                    atr_cap=float(ctx.atr_cap),
                    # volume
                    vol_surge_ratio=float(ctx.vol_surge_ratio),
                    vol_t=float(ctx.vol_t),
                    vol_ma10=float(ctx.vol_ma10) if ctx.vol_ma10 is not None else None,
                    # 3-bar drift closes
                    c0=float(c0) if c0 is not None else None,
                    c1=float(c1) if c1 is not None else None,
                    c2=float(c2) if c2 is not None else None,
                    c3=float(c3) if c3 is not None else None,
                    # session window
                    gate_start=str(ctx.gate_start),
                    gate_end=str(ctx.gate_end),
                    entry_model=str(ctx.entry_model),
                    # data quality
                    data_age_qqq_sec=ctx.data_age_qqq_sec,
                    data_age_qqew_sec=ctx.data_age_qqew_sec,
                    continuity_180s_ok=bool(ctx.continuity_180s_ok),
                    qqew_decoupled=bool(ctx.qqew_decoupled),
                    session_open_price=float(ctx.session_open_price) if ctx.session_open_price is not None else None,
                    day_position_rank=float(ctx.day_position_rank) if ctx.day_position_rank is not None else None,
                    # outcome
                    gate_reason=gate_reason,
                    allowed=bool(translation.allowed),
                    signal_source_mode=str(signal_source_meta.get("signal_source_mode") or ""),
                    signal_source_status=str(signal_source_meta.get("signal_source_status") or ""),
                    signal_source_age_sec=signal_source_meta.get("signal_source_age_sec"),
                    signal_source_latency_ms=signal_source_meta.get("signal_source_latency_ms"),
                    signal_source_publish_stage=str(signal_source_meta.get("signal_source_publish_stage") or ""),
                    signal_source_strategy_id=str(signal_source_meta.get("signal_source_strategy_id") or ""),
                    signal_plane_backend_config=str(signal_source_meta.get("signal_plane_backend_config") or ""),
                    signal_plane_state=str(signal_source_meta.get("signal_plane_state") or ""),
                    signal_plane_using_redis=bool(signal_source_meta.get("signal_plane_using_redis")),
                    signal_plane_redis_ok=bool(signal_source_meta.get("signal_plane_redis_ok")),
                    signal_plane_error_streak=int(signal_source_meta.get("signal_plane_error_streak") or 0),
                    signal_plane_retry_in_sec=float(signal_source_meta.get("signal_plane_retry_in_sec") or 0.0),
                    signal_plane_last_write_ok=signal_source_meta.get("signal_plane_last_write_ok"),
                    signal_plane_last_write_error=str(signal_source_meta.get("signal_plane_last_write_error") or ""),
                    signal_plane_last_write_ts=str(signal_source_meta.get("signal_plane_last_write_ts") or ""),
                )
            except Exception:
                pass

        should_emit_entry_block = False
        if source_mode == "upstream_stock_signal":
            should_emit_entry_block = signal is None and (source_block_reason is not None)
        else:
            should_emit_entry_block = signal is None and (not translation.allowed)

        if should_emit_entry_block and emit_entry_block:
            try:
                log.info(
                    "qqq_intraday_option_entry_block",
                    ts=minute + ":00",
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    underlying=str(self._pure_underlying or runtime.symbol),
                    gate_reason=str(source_block_reason or gate_reason),
                    gate_payload=gate_payload,
                    qqq_now=float(ctx.qqq_now),
                    vwap_now=float(ctx.vwap_now),
                    v3m_signed=float(ctx.v3m_signed),
                    atr_eff=float(ctx.atr_eff),
                    vol_surge_ratio=float(ctx.vol_surge_ratio),
                    c0=float(c0) if c0 is not None else None,
                    c1=float(c1) if c1 is not None else None,
                    c2=float(c2) if c2 is not None else None,
                    c3=float(c3) if c3 is not None else None,
                    signal_source_mode=str(signal_source_meta.get("signal_source_mode") or ""),
                    signal_source_status=str(signal_source_meta.get("signal_source_status") or ""),
                    signal_source_age_sec=signal_source_meta.get("signal_source_age_sec"),
                    signal_source_latency_ms=signal_source_meta.get("signal_source_latency_ms"),
                    signal_source_publish_stage=str(signal_source_meta.get("signal_source_publish_stage") or ""),
                    signal_source_strategy_id=str(signal_source_meta.get("signal_source_strategy_id") or ""),
                    signal_plane_backend_config=str(signal_source_meta.get("signal_plane_backend_config") or ""),
                    signal_plane_state=str(signal_source_meta.get("signal_plane_state") or ""),
                    signal_plane_using_redis=bool(signal_source_meta.get("signal_plane_using_redis")),
                    signal_plane_redis_ok=bool(signal_source_meta.get("signal_plane_redis_ok")),
                    signal_plane_error_streak=int(signal_source_meta.get("signal_plane_error_streak") or 0),
                    signal_plane_retry_in_sec=float(signal_source_meta.get("signal_plane_retry_in_sec") or 0.0),
                    signal_plane_last_write_ok=signal_source_meta.get("signal_plane_last_write_ok"),
                    signal_plane_last_write_error=str(signal_source_meta.get("signal_plane_last_write_error") or ""),
                    signal_plane_last_write_ts=str(signal_source_meta.get("signal_plane_last_write_ts") or ""),
                )
            except Exception:
                pass
            try:
                log.info(
                    "qqq_intraday_option_downstream_result",
                    ts=minute + ":00",
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    underlying=str(self._pure_underlying or runtime.symbol),
                    result_code=str(source_block_reason or "entry_blocked"),
                    block_reason=str(source_block_reason or gate_reason),
                    signal_source_mode=str(signal_source_meta.get("signal_source_mode") or ""),
                    signal_source_status=str(signal_source_meta.get("signal_source_status") or ""),
                    signal_source_age_sec=signal_source_meta.get("signal_source_age_sec"),
                    signal_source_latency_ms=signal_source_meta.get("signal_source_latency_ms"),
                    signal_source_publish_stage=str(signal_source_meta.get("signal_source_publish_stage") or ""),
                    signal_source_strategy_id=str(signal_source_meta.get("signal_source_strategy_id") or ""),
                    signal_plane_backend_config=str(signal_source_meta.get("signal_plane_backend_config") or ""),
                    signal_plane_state=str(signal_source_meta.get("signal_plane_state") or ""),
                    signal_plane_using_redis=bool(signal_source_meta.get("signal_plane_using_redis")),
                    signal_plane_redis_ok=bool(signal_source_meta.get("signal_plane_redis_ok")),
                    signal_plane_error_streak=int(signal_source_meta.get("signal_plane_error_streak") or 0),
                    signal_plane_retry_in_sec=float(signal_source_meta.get("signal_plane_retry_in_sec") or 0.0),
                    signal_plane_last_write_ok=signal_source_meta.get("signal_plane_last_write_ok"),
                    signal_plane_last_write_error=str(signal_source_meta.get("signal_plane_last_write_error") or ""),
                    signal_plane_last_write_ts=str(signal_source_meta.get("signal_plane_last_write_ts") or ""),
                )
            except Exception:
                pass
            return

        if signal is not None:
            try:
                log.info(
                    "qqq_intraday_option_signal",
                    ts=minute + ":00",
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    underlying=str(self._pure_underlying or runtime.symbol),
                    side=str(signal.side),
                    stock_side=str(stock_side_logged),
                    entry_model=str(ctx.entry_model),
                    gate_reason=gate_reason,
                    gate_payload=gate_payload,
                    v3m_signed=float(ctx.v3m_signed),
                    atr_eff=float(ctx.atr_eff),
                    qqq_now=float(ctx.qqq_now),
                    vwap_now=float(ctx.vwap_now),
                    vol_surge_ratio=float(ctx.vol_surge_ratio),
                    c0=float(c0) if c0 is not None else None,
                    c1=float(c1) if c1 is not None else None,
                    c2=float(c2) if c2 is not None else None,
                    c3=float(c3) if c3 is not None else None,
                    signal_payload=dict(signal.payload or {}),
                    signal_source_mode=str(signal_source_meta.get("signal_source_mode") or ""),
                    signal_source_status=str(signal_source_meta.get("signal_source_status") or ""),
                    signal_source_age_sec=signal_source_meta.get("signal_source_age_sec"),
                    signal_source_latency_ms=signal_source_meta.get("signal_source_latency_ms"),
                    signal_source_publish_stage=str(signal_source_meta.get("signal_source_publish_stage") or ""),
                    signal_source_strategy_id=str(signal_source_meta.get("signal_source_strategy_id") or ""),
                    signal_plane_backend_config=str(signal_source_meta.get("signal_plane_backend_config") or ""),
                    signal_plane_state=str(signal_source_meta.get("signal_plane_state") or ""),
                    signal_plane_using_redis=bool(signal_source_meta.get("signal_plane_using_redis")),
                    signal_plane_redis_ok=bool(signal_source_meta.get("signal_plane_redis_ok")),
                    signal_plane_error_streak=int(signal_source_meta.get("signal_plane_error_streak") or 0),
                    signal_plane_retry_in_sec=float(signal_source_meta.get("signal_plane_retry_in_sec") or 0.0),
                    signal_plane_last_write_ok=signal_source_meta.get("signal_plane_last_write_ok"),
                    signal_plane_last_write_error=str(signal_source_meta.get("signal_plane_last_write_error") or ""),
                    signal_plane_last_write_ts=str(signal_source_meta.get("signal_plane_last_write_ts") or ""),
                )
            except Exception:
                pass
            try:
                log.info(
                    "qqq_intraday_option_downstream_result",
                    ts=minute + ":00",
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    underlying=str(self._pure_underlying or runtime.symbol),
                    result_code="signal_consumed",
                    signal_side=str(signal.side),
                    signal_source_mode=str(signal_source_meta.get("signal_source_mode") or ""),
                    signal_source_status=str(signal_source_meta.get("signal_source_status") or ""),
                    signal_source_age_sec=signal_source_meta.get("signal_source_age_sec"),
                    signal_source_latency_ms=signal_source_meta.get("signal_source_latency_ms"),
                    signal_source_publish_stage=str(signal_source_meta.get("signal_source_publish_stage") or ""),
                    signal_source_strategy_id=str(signal_source_meta.get("signal_source_strategy_id") or ""),
                )
            except Exception:
                pass

    def _emit_structured_source_observability(
        self,
        *,
        runtime: PluginRuntimeState,
        minute: str,
        signal: Optional[Signal],
        signal_source_meta: Dict[str, Any],
        source_block_reason: Optional[str],
        gate_reason: str,
        translation_allowed: bool,
    ) -> None:
        if self._pure_structured_observability_minute == minute:
            return
        self._pure_structured_observability_minute = minute
        kernel = getattr(self, "_pure_kernel", None)
        emit = getattr(kernel, "_emit_strategy_audit", None)
        if not callable(emit):
            return
        source_status = str(signal_source_meta.get("signal_source_status") or "unknown")
        status = "signal" if signal is not None else ("blocked" if source_block_reason else "observed")
        reason = "signal_ready" if signal is not None else str(source_block_reason or gate_reason or source_status or "no_signal")
        payload = {
            "minute": str(minute),
            "underlying": str(self._pure_underlying or runtime.symbol),
            "has_signal": bool(signal is not None),
            "signal_side": str(getattr(signal, "side", "") or ""),
            "source_block_reason": str(source_block_reason or ""),
            "gate_reason": str(gate_reason or ""),
            "translation_allowed": bool(translation_allowed),
            "signal_source_mode": str(signal_source_meta.get("signal_source_mode") or ""),
            "signal_source_status": source_status,
            "signal_source_age_sec": signal_source_meta.get("signal_source_age_sec"),
            "signal_source_latency_ms": signal_source_meta.get("signal_source_latency_ms"),
            "signal_source_publish_stage": str(signal_source_meta.get("signal_source_publish_stage") or ""),
            "signal_source_strategy_id": str(signal_source_meta.get("signal_source_strategy_id") or ""),
            "signal_plane_backend_config": str(signal_source_meta.get("signal_plane_backend_config") or ""),
            "signal_plane_state": str(signal_source_meta.get("signal_plane_state") or ""),
            "signal_plane_using_redis": bool(signal_source_meta.get("signal_plane_using_redis")),
            "signal_plane_redis_ok": bool(signal_source_meta.get("signal_plane_redis_ok")),
            "signal_plane_error_streak": int(signal_source_meta.get("signal_plane_error_streak") or 0),
            "signal_plane_retry_in_sec": float(signal_source_meta.get("signal_plane_retry_in_sec") or 0.0),
            "signal_plane_last_write_ok": signal_source_meta.get("signal_plane_last_write_ok"),
            "signal_plane_last_write_error": str(signal_source_meta.get("signal_plane_last_write_error") or ""),
            "signal_plane_last_write_ts": str(signal_source_meta.get("signal_plane_last_write_ts") or ""),
        }
        try:
            emit(
                stage="plugin_signal_source_observability",
                status=status,
                reason=reason,
                payload=payload,
                severity="info" if status != "blocked" else "warning",
            )
        except Exception:
            pass

    def _fallback_evaluation(self, runtime: PluginRuntimeState, extras: Dict[str, Any], reason: str):
        truth = resolve_qqq_truth_fabric(runtime, extras)
        v3m_signed = float(truth.v3m_signed)
        stock_side = "LONG" if v3m_signed >= 0.0 else "SHORT"
        ctx = SimpleNamespace(
            close_by_minute=dict(truth.close_by_minute),
            qqq_now=float(truth.spot),
            vwap_now=float(truth.vwap),
            distance_from_vwap_atr=float(truth.distance_from_vwap_atr),
            qqew_now=truth.qqew_now,
            qqew_vwap_now=truth.qqew_vwap_now,
            v3m_signed=v3m_signed,
            atr_live=float(truth.atr_live),
            atr_seed=float(truth.atr_seed),
            atr_eff=float(truth.atr_eff),
            atr_cap=float(truth.atr_cap),
            vol_surge_ratio=float(truth.vol_surge_ratio),
            vol_t=float(truth.vol_t),
            vol_ma10=truth.vol_ma10,
            gate_start=str(extras.get("entry_start_am") or ""),
            gate_end=str(extras.get("no_new_entry_after") or ""),
            entry_model=str(extras.get("entry_model") or ""),
            data_age_qqq_sec=truth.data_age_qqq_sec,
            data_age_qqew_sec=truth.data_age_qqew_sec,
            continuity_180s_ok=bool(truth.continuity_180s_ok),
            qqew_decoupled=bool(truth.qqew_decoupled),
            session_open_price=truth.session_open_price,
            day_position_rank=truth.day_position_rank,
        )
        translation = SimpleNamespace(allowed=True, stock_side=stock_side, side=("CALL" if stock_side == "LONG" else "PUT"))
        return SimpleNamespace(context=ctx, gate_reason=reason, gate_payload={}, translation=translation)

    def _minimal_observability_evaluation(
        self,
        runtime: PluginRuntimeState,
        extras: Dict[str, Any],
        *,
        reason: str,
        error: str = "",
    ):
        def _f(value: Any, default: float = 0.0) -> float:
            try:
                return float(value if value is not None else default)
            except Exception:
                return float(default)

        spot = _f(getattr(runtime, "spot", 0.0), 0.0)
        if spot <= 0.0:
            spot = _f(getattr(getattr(runtime, "bar", None), "close", 0.0), 0.0)
        vwap = _f(getattr(getattr(runtime, "bar", None), "vwap", spot), spot)
        atr = _f(getattr(getattr(runtime, "bar", None), "atr", 0.0), 0.0)
        vol_t = _f(getattr(getattr(runtime, "bar", None), "volume", 0.0), 0.0)
        stock_side = "LONG" if spot >= vwap else "SHORT"
        gate_payload: Dict[str, Any] = {}
        if error:
            gate_payload["error"] = str(error)[:256]
        ctx = SimpleNamespace(
            close_by_minute={},
            qqq_now=float(spot),
            vwap_now=float(vwap),
            distance_from_vwap_atr=float(abs(spot - vwap) / max(atr, 1e-9)) if atr > 0 else 0.0,
            qqew_now=None,
            qqew_vwap_now=None,
            v3m_signed=0.0,
            atr_live=float(atr),
            atr_seed=float(atr),
            atr_eff=float(atr),
            atr_cap=float(atr),
            vol_surge_ratio=0.0,
            vol_t=float(vol_t),
            vol_ma10=None,
            gate_start=str(extras.get("entry_start_am") or ""),
            gate_end=str(extras.get("no_new_entry_after") or ""),
            entry_model=str(extras.get("entry_model") or ""),
            data_age_qqq_sec=None,
            data_age_qqew_sec=None,
            continuity_180s_ok=True,
            qqew_decoupled=True,
            session_open_price=None,
            day_position_rank=None,
        )
        translation = SimpleNamespace(
            allowed=True,
            stock_side=stock_side,
            side=("CALL" if stock_side == "LONG" else "PUT"),
        )
        return SimpleNamespace(context=ctx, gate_reason=reason, gate_payload=gate_payload, translation=translation)

    @staticmethod
    def _safe_age_sec(now_ts: datetime, trigger_ts: datetime) -> float:
        try:
            return max(0.0, float((now_ts - trigger_ts).total_seconds()))
        except Exception:
            try:
                return max(0.0, float(now_ts.timestamp() - trigger_ts.timestamp()))
            except Exception:
                return 0.0

    @staticmethod
    def _source_trigger_key(artifact: Any) -> str:
        trigger_id = str(getattr(artifact, "trigger_id", "") or "").strip()
        if trigger_id:
            return trigger_id
        strategy_id = str(getattr(artifact, "strategy_id", "") or "").strip()
        option_side = str(getattr(artifact, "option_side", "") or "").strip().upper()
        trigger_ts = getattr(artifact, "trigger_ts", None)
        trigger_ts_raw = trigger_ts.isoformat() if hasattr(trigger_ts, "isoformat") else str(trigger_ts or "")
        return f"trigger_ts:{strategy_id}:{trigger_ts_raw}:{option_side}"

    @staticmethod
    def _extract_source_trigger_key_from_payload(payload: Dict[str, Any]) -> str:
        data = dict(payload or {})
        direct = str(data.get("source_trigger_key") or "").strip()
        if direct:
            return direct
        plugin_payload = data.get("plugin_payload")
        if isinstance(plugin_payload, dict):
            signal_obj = plugin_payload.get("signal")
            if isinstance(signal_obj, dict):
                signal_payload = signal_obj.get("payload")
                if isinstance(signal_payload, dict):
                    nested = str(signal_payload.get("source_trigger_key") or "").strip()
                    if nested:
                        return nested
        return ""

    def _consume_source_keys_from_runtime(self, runtime: PluginRuntimeState) -> None:
        positions = list(runtime.active_positions or [])
        if runtime.active_position is not None:
            positions.append(runtime.active_position)
        for pos in positions:
            payload = dict(getattr(pos, "payload", {}) or {})
            source_trigger_key = self._extract_source_trigger_key_from_payload(payload)
            if source_trigger_key:
                self._pure_consumed_source_keys.add(source_trigger_key)

    def _terminalize_active_position_source_handoffs(self, runtime: PluginRuntimeState) -> None:
        positions = list(runtime.active_positions or [])
        if runtime.active_position is not None:
            positions.append(runtime.active_position)
        for pos in positions:
            payload = dict(getattr(pos, "payload", {}) or {})
            source_trigger_key = self._extract_source_trigger_key_from_payload(payload)
            if not source_trigger_key:
                continue
            self._pure_consumed_source_keys.add(source_trigger_key)
            self._record_source_handoff_terminal(
                runtime=runtime,
                signal=None,
                status="consumed",
                reason="active_position_confirmed",
                terminal=True,
                payload={"source_trigger_key": source_trigger_key},
            )

    @staticmethod
    def _latency_from_source_trigger(runtime: PluginRuntimeState, payload: Dict[str, Any]) -> Dict[str, Any]:
        source_trigger_ts_raw = str((payload or {}).get("source_trigger_ts") or "").strip()
        if not source_trigger_ts_raw:
            return {}
        try:
            source_trigger_ts = datetime.fromisoformat(source_trigger_ts_raw)
            latency_ms = float(max(0.0, (runtime.now - source_trigger_ts).total_seconds() * 1000.0))
            return {"source_trigger_ts": source_trigger_ts_raw, "source_to_now_ms": latency_ms}
        except Exception:
            return {"source_trigger_ts": source_trigger_ts_raw}

    def build_signal_pure(self, runtime: PluginRuntimeState) -> Optional[Signal]:
        self._pure_reset_if_needed(runtime)
        self._consume_source_keys_from_runtime(runtime)
        extras = dict(self._last_extras or {})
        extras.setdefault("same_strategy_pending_orders", runtime.payload.get("same_strategy_pending_orders", 0))
        extras.setdefault("foreign_position_qty", runtime.payload.get("foreign_position_qty", 0.0))
        extras.setdefault("pending_decision_sync", runtime.payload.get("pending_decision_sync", False))
        source_cfg = self._signal_source_config(extras)
        if str(source_cfg["mode"]) == "upstream_stock_signal":
            try:
                evaluation = self._fallback_evaluation(runtime, extras, reason="upstream_source_mode")
            except Exception as exc:
                evaluation = self._minimal_observability_evaluation(
                    runtime,
                    extras,
                    reason="upstream_source_eval_fallback",
                    error=str(exc),
                )
                if self._pure_log is not None:
                    try:
                        self._pure_log.warning(
                            "qqq_intraday_option_source_eval_fallback",
                            strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                            underlying=str(self._pure_underlying or runtime.symbol),
                            error=str(exc),
                            mode=str(source_cfg["mode"]),
                        )
                    except Exception:
                        pass
        else:
            recipe = load_entry_recipe(extras)
            engine = QQQIntradayRuleEngine()
            evaluation = engine.evaluate(runtime=runtime, extras=extras, recipe=recipe)
        signal_plane = signal_plane_health()
        source_meta: Dict[str, Any] = {
            "signal_source_mode": str(source_cfg["mode"]),
            "signal_source_status": "unknown",
            "signal_source_age_sec": None,
            "signal_source_latency_ms": None,
            "signal_source_publish_stage": "",
            "signal_source_strategy_id": "",
            "signal_plane_backend_config": str(signal_plane.get("backend_config") or ""),
            "signal_plane_state": str(signal_plane.get("state") or ""),
            "signal_plane_using_redis": bool(signal_plane.get("using_redis")),
            "signal_plane_redis_ok": bool(signal_plane.get("redis_ok")),
            "signal_plane_error_streak": int(signal_plane.get("error_streak") or 0),
            "signal_plane_retry_in_sec": float(signal_plane.get("retry_in_sec") or 0.0),
            "signal_plane_last_write_ok": signal_plane.get("last_write_ok"),
            "signal_plane_last_write_error": str(signal_plane.get("last_write_error") or ""),
            "signal_plane_last_write_ts": str(signal_plane.get("last_write_ts") or ""),
        }

        definition = self._resolve_strategy_definition()
        signal: Optional[Signal] = None
        if str(source_cfg["mode"]) == "upstream_stock_signal":
            if runtime.active_position is not None or runtime.active_positions:
                self._terminalize_active_position_source_handoffs(runtime)
                source_meta["signal_source_status"] = "suppressed"
                self._emit_qqq_intraday_observability(runtime, evaluation, signal=None, signal_source_meta=source_meta)
                return None
            if int(runtime.payload.get("same_strategy_pending_orders", 0) or 0) > 0:
                source_meta["signal_source_status"] = "suppressed"
                self._emit_qqq_intraday_observability(runtime, evaluation, signal=None, signal_source_meta=source_meta)
                return None
            artifact = get_pending_stock_entry_signal(
                underlying=str(extras.get("underlying") or runtime.symbol),
                signal_family=str(source_cfg["source_signal_family"]),
                source_strategy_id=str(source_cfg["source_strategy_id"]),
            )
            if artifact is None and bool(extras.get("allow_latest_signal_fallback")):
                source_meta["latest_signal_fallback_requested"] = True
                source_meta["latest_signal_fallback_status"] = "disabled_for_production"
            source_meta["signal_source_strategy_id"] = str(source_cfg["source_strategy_id"] or "")
            if artifact is None:
                source_meta["signal_source_status"] = "idle"
            else:
                source_meta["signal_source_strategy_id"] = str(artifact.strategy_id)
                age_sec = self._safe_age_sec(runtime.now, artifact.trigger_ts)
                source_meta["signal_source_age_sec"] = float(age_sec)
                source_meta["signal_source_latency_ms"] = float(max(0.0, age_sec * 1000.0))
                artifact_payload = dict(artifact.payload or {})
                source_meta["signal_source_publish_stage"] = str(artifact_payload.get("publish_stage") or "")
                source_trigger_key = self._source_trigger_key(artifact)
                if bool(source_cfg["require_source_alive"]) and age_sec > float(source_cfg["max_signal_age_sec"]):
                    source_meta["signal_source_status"] = "stale"
                    self._record_source_handoff_terminal(
                        runtime=runtime,
                        signal=None,
                        artifact=artifact,
                        status="expired",
                        reason="source_signal_stale",
                        terminal=True,
                        payload={"age_sec": float(age_sec), "max_age_sec": float(source_cfg["max_signal_age_sec"])},
                    )
                elif source_trigger_key in self._pure_consumed_source_keys:
                    source_meta["signal_source_status"] = "suppressed"
                elif not artifact.entry_allowed:
                    source_meta["signal_source_status"] = "blocked"
                elif artifact.option_side not in {"CALL", "PUT"}:
                    source_meta["signal_source_status"] = "invalid"
                else:
                    source_meta["signal_source_status"] = "live"
                    signal = Signal(
                        ts=runtime.now,
                        side=str(artifact.option_side),
                        signal_kind="qqq_intraday_stock_upstream_signal",
                        regime=str(artifact.direction),
                        payload=self._normalized_signal_payload(
                            mode="upstream_stock_signal",
                            source_strategy_id=str(artifact.strategy_id),
                            source_signal_family=str(artifact.signal_family),
                            age_sec=float(age_sec),
                            direction=str(artifact.direction),
                            extra={
                            "source_trigger_id": str(artifact.trigger_id or ""),
                            "source_decision_id": str(artifact.decision_id or ""),
                            "source_trace_id": str(artifact.trace_id or ""),
                            "source_trigger_ts": artifact.trigger_ts.isoformat(),
                            "source_latency_ms": float(max(0.0, age_sec * 1000.0)),
                            "source_trigger_key": str(source_trigger_key),
                            "source_publish_stage": str(artifact_payload.get("publish_stage") or ""),
                            "source_payload": artifact_payload,
                            },
                        ),
                    )
        else:
            signal = definition.signal_module.build_signal(
                SignalContext(runtime=runtime, extras=extras)
            )
            source_meta["signal_source_status"] = "live" if signal is not None else "blocked"
            source_meta["signal_source_age_sec"] = 0.0
            source_meta["signal_source_latency_ms"] = 0.0
            source_meta["signal_source_publish_stage"] = "independent_rule_replay"
            source_meta["signal_source_strategy_id"] = str(runtime.strategy_id)
            if signal is not None:
                direction = "LONG" if str(signal.side).upper() == "CALL" else "SHORT"
                signal = Signal(
                    ts=signal.ts,
                    side=signal.side,
                    signal_kind=signal.signal_kind,
                    grade=signal.grade,
                    regime=signal.regime,
                    payload=self._normalized_signal_payload(
                        mode="independent_rule_replay",
                        source_strategy_id=str(runtime.strategy_id),
                        source_signal_family=str(source_cfg["source_signal_family"]),
                        age_sec=0.0,
                        direction=direction,
                        extra=dict(signal.payload or {}),
                    ),
                )

        emit_entry_block = True
        try:
            emit_entry_block = bool(is_in_split_entry_window(runtime.now, extras))
        except Exception:
            emit_entry_block = True
        self._emit_qqq_intraday_observability(
            runtime,
            evaluation,
            signal=signal,
            signal_source_meta=source_meta,
            emit_entry_block=emit_entry_block,
        )
        return signal

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal) -> Optional[EntryPlan]:
        source_cfg = self._signal_source_config(dict(self._last_extras or {}))
        if str(source_cfg["mode"]) == "independent_rule_replay" and bool(source_cfg["shadow_only"]):
            return None
        definition = self._resolve_strategy_definition()
        return definition.signal_module.build_entry_plan(
            SignalContext(runtime=runtime, extras=dict(self._last_extras or {})),
            signal,
        )

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan) -> Optional[Selection]:
        definition = self._resolve_strategy_definition()
        selection = definition.contract_policy.select_contract(
            ContractSelectionContext(runtime=runtime, entry_plan=entry_plan, extras=dict(self._last_extras or {}))
        )
        if selection is None and self._pure_log is not None:
            reject_reason = str(getattr(definition.contract_policy, "last_reject_reason", "") or "")
            signal_trigger_ts = ""
            try:
                signal_trigger_ts = str(runtime.payload.get("source_trigger_ts") or "")
            except Exception:
                signal_trigger_ts = ""
            if not signal_trigger_ts:
                signal_trigger_ts = str((entry_plan.payload or {}).get("source_trigger_ts") or "")
            timing_payload = self._latency_from_source_trigger(runtime, {"source_trigger_ts": signal_trigger_ts})
            try:
                self._pure_log.info(
                    "qqq_intraday_option_contract_selection_failed",
                    ts=runtime.now.strftime("%Y-%m-%d %H:%M:%S"),
                    strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                    underlying=str(self._pure_underlying or runtime.symbol),
                    side=str(entry_plan.side),
                    signal_kind=str(entry_plan.signal_kind),
                    reject_reason=reject_reason or "unknown_contract_filter",
                    spot=float(runtime.spot or 0.0),
                    source_trigger_ts=str(signal_trigger_ts or ""),
                    source_to_selection_ms=timing_payload.get("source_to_now_ms"),
                )
            except Exception:
                pass
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
        self._record_source_handoff_terminal(
            runtime=runtime,
            signal=signal,
            status="submitted",
            reason="entry_intent_submitted",
            terminal=False,
            payload={"position_lot_id": str(position_lot_id), "opt_symbol": str(selection.opt_symbol)},
        )
        self._clear_strategy_tradability_blocker(runtime=runtime, stage="upstream_signal")
        self._clear_strategy_tradability_blocker(runtime=runtime, stage="contract_selection")
        if self._pure_log is None:
            return
        timing_payload = self._latency_from_source_trigger(runtime, dict(getattr(signal, "payload", {}) or {}))
        try:
            self._pure_log.info(
                "qqq_intraday_option_downstream_result",
                ts=runtime.now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                underlying=str(self._pure_underlying or runtime.symbol),
                result_code="entry_intent_submitted",
                side=str(getattr(signal, "side", "") or ""),
                opt_symbol=str(selection.opt_symbol),
                position_lot_id=str(position_lot_id),
                source_trigger_ts=str(timing_payload.get("source_trigger_ts") or ""),
                source_to_submit_ms=timing_payload.get("source_to_now_ms"),
            )
        except Exception:
            pass

    def on_entry_submit_blocked_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
        reason: str,
        payload: Dict[str, Any],
    ) -> None:
        reason_norm = str(reason or "").strip().lower()
        code_map = {
            "no_contract_selection": "no_contract_selection",
            "source_signal_missing": "source_signal_missing",
            "source_signal_stale": "source_signal_stale",
        }
        result_code = code_map.get(reason_norm, "entry_submit_blocked")
        if reason_norm in {"no_contract_selection", "source_signal_missing", "source_signal_stale"}:
            self._mark_strategy_tradability_blocker(
                runtime=runtime,
                reason=reason_norm,
                stage="contract_selection" if reason_norm == "no_contract_selection" else "upstream_signal",
                payload=dict(payload or {}),
            )
            self._record_source_handoff_terminal(
                runtime=runtime,
                signal=signal,
                status="contract_rejected" if reason_norm == "no_contract_selection" else "expired",
                reason=reason_norm,
                terminal=True,
                payload=dict(payload or {}),
            )
        elif reason_norm:
            self._record_source_handoff_terminal(
                runtime=runtime,
                signal=signal,
                status="risk_blocked",
                reason=reason_norm,
                terminal=True,
                payload=dict(payload or {}),
            )
        timing_payload = self._latency_from_source_trigger(runtime, dict(getattr(signal, "payload", {}) or {}))
        if self._pure_log is None:
            return
        try:
            self._pure_log.info(
                "qqq_intraday_option_downstream_result",
                ts=runtime.now.strftime("%Y-%m-%d %H:%M:%S"),
                strategy_id=str(self._pure_strategy_id or runtime.strategy_id),
                underlying=str(self._pure_underlying or runtime.symbol),
                result_code=result_code,
                block_reason=reason_norm,
                block_payload=dict(payload or {}),
                reject_reason=str((payload or {}).get("reject_reason") or ""),
                side=str(getattr(signal, "side", "") or ""),
                opt_symbol=str(getattr(selection, "opt_symbol", "") or ""),
                source_trigger_ts=str(timing_payload.get("source_trigger_ts") or ""),
                source_to_block_ms=timing_payload.get("source_to_now_ms"),
            )
        except Exception:
            pass

    def _pure_reset_if_needed(self, runtime: PluginRuntimeState) -> None:
        prev_day = self._pure_last_day
        super()._pure_reset_if_needed(runtime)
        if prev_day != self._pure_last_day:
            self._pure_consumed_source_keys = set()
