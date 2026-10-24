from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from strategies.stocks_kernel.data.ctx_history import load_history_from_ctx
from strategies.stocks_kernel.data.time_utils import (
    coerce_feature_timestamp_seconds,
    resolve_session_label,
    session_label_from_epoch_seconds,
)
from strategies.stocks_kernel.types import HistoryBundle, MarketSnapshot


class HistoryProvider(Protocol):
    async def load(self, *, symbol: str, resolution: str, limit: int) -> HistoryBundle:
        ...


@dataclass(frozen=True)
class HistoryRequest:
    symbol: str
    resolution: str
    limit: int


class CtxHistoryProvider:
    def __init__(self, *, ctx: Any) -> None:
        self._ctx = ctx

    def cache_scope_key(self) -> tuple[str, Any]:
        engine = getattr(self._ctx, "engine", None)
        return ("ctx_history", engine if engine is not None else self._ctx)

    async def load(self, *, symbol: str, resolution: str, limit: int) -> HistoryBundle:
        return await load_history_from_ctx(ctx=self._ctx, symbol=symbol, resolution=resolution, limit=limit)


class FeatureRowSnapshotProvider:
    @staticmethod
    def build_snapshot(*, symbol: str, features: dict[str, Any]) -> MarketSnapshot:
        symbol_u = str(symbol).strip().upper()
        ts_epoch_s = FeatureRowSnapshotProvider.feature_timestamp_s(features)
        last_price = FeatureRowSnapshotProvider.feature_price(features)
        extras: dict[str, Any] = {
            "planned_qty": features.get("planned_qty"),
            "trigger_id": features.get("trigger_id"),
        }
        # Keep snapshot extras small and explicit. Gates/policies can rely on these keys
        # being present when upstream services provide them.
        for key in (
            "same_strategy_pending_orders",
            "foreign_position_qty",
            "current_notional",
            "planned_notional",
            "seed_n",
            "seed_ready",
            "amp_locked",
            "stable_entry_block_reason",
            "restart_degrade_active",
            "startup_anchor_entry_block_active",
            "data_integrity_fail_closed",
            "pending_decision_sync",
            "ownership_conflict",
            "anti_chase_enabled",
            "anti_chase_rank15",
            "anti_chase_distance_from_vwap_atr_max",
            "last_stop_exit_side",
            "last_stop_exit_at",
            "stop_reentry_cooldown_min",
            "enable_high_vol_guard",
            "high_vol_ratio",
            "hv_min_vol_surge",
            "hv_k",
            "hv_tv_min_usd",
            "vol_ratio_override",
            "day_shape_overlay_enabled",
            "day_shape_long_rank_max",
            "day_shape_short_rank_min",
            "v3m",
            "v3m_signed",
            "v3m_units",
            "atr_eff",
            "atr_live",
            "atr_seed",
            "atr_cap",
            "vol_surge_ratio",
            "volume_surge_ratio",
            "distance_from_vwap_atr",
            "data_age_qqq_sec",
            "data_age_qqew_sec",
            "continuity_180s_ok",
            "qqq_now",
            "vwap_now",
            "opening_range_high",
            "opening_range_low",
            "pos_day",
            "day_position_rank",
        ):
            if key in features:
                extras[key] = features.get(key)
        if extras.get("planned_notional") is None and last_price is not None:
            try:
                qty = float(extras.get("planned_qty") or 0.0)
                extras["planned_notional"] = abs(qty) * float(last_price)
            except Exception:
                pass
        return MarketSnapshot(
            symbol=symbol_u,
            ts_epoch_s=ts_epoch_s,
            last_price=last_price,
            session=FeatureRowSnapshotProvider.feature_session(features, ts_epoch_s=ts_epoch_s),
            vwap=FeatureRowSnapshotProvider.feature_float(features, "vwap", "vwap_day", "vwap_bar"),
            minute_close=FeatureRowSnapshotProvider.feature_float(features, "close", "signal_price", "last_price", "price", "last_px"),
            minute_open=FeatureRowSnapshotProvider.feature_float(features, "open", "o"),
            minute_high=FeatureRowSnapshotProvider.feature_float(features, "high", "h"),
            minute_low=FeatureRowSnapshotProvider.feature_float(features, "low", "l"),
            minute_volume=FeatureRowSnapshotProvider.feature_float(features, "volume", "v"),
            extras=extras,
        )

    @staticmethod
    def feature_timestamp_s(features: dict[str, Any]) -> float:
        for key in (
            "ts_epoch_s",
            "ts",
            "ts_end",
            "ts_start",
            "now_et",
            "feature_time_et",
            "ts_us",
            "timestamp_us",
            "feature_ts_us",
            "ts_end_us",
            "ts_start_us",
            "bar_end_ts_us",
            "ts_bar_end_us",
        ):
            value = coerce_feature_timestamp_seconds(features.get(key), default=0.0)
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def now_timestamp_s(features: dict[str, Any], *, default: float) -> float:
        for key in ("now_epoch_s", "now_et", "feature_time_et"):
            value = coerce_feature_timestamp_seconds(features.get(key), default=0.0)
            if value > 0:
                return value
        return float(default)

    @staticmethod
    def feature_session(features: dict[str, Any], *, ts_epoch_s: float) -> str | None:
        for key in ("session", "session_phase", "market_session"):
            session = resolve_session_label(features.get(key))
            if session:
                return session
        return session_label_from_epoch_seconds(ts_epoch_s)

    @staticmethod
    def feature_price(features: dict[str, Any]) -> float | None:
        value = FeatureRowSnapshotProvider.feature_float(
            features,
            "last_price",
            "signal_price",
            "exec_price",
            "close",
            "price",
            "last_px",
        )
        if value is not None and value > 0:
            return value
        return None

    @staticmethod
    def feature_float(features: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            try:
                value = features.get(key)
                if value is None:
                    continue
                return float(value)
            except (TypeError, ValueError):
                continue
        return None
