from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from strategies.qqq_shared_signal_producer import QQQSharedEntryCandidate, publish_qqq_shared_entry_signal
from strategies.stocks_kernel.plugin import BaseStockPlugin, StockStrategyManifest
from strategies.stocks_kernel.data.time_utils import ET


class QQQIntradayStockPlugin(BaseStockPlugin):
    """Plugin facade for the QQQ intraday stock strategy running on stocks_kernel."""

    component_name = "qqq_intraday_stock_plugin"

    def __init__(self) -> None:
        self._latest_features_by_symbol: dict[str, Dict[str, Any]] = {}
        self._published_trigger_ids: set[str] = set()
        self._qqq_samples: deque[tuple[float, float]] = deque(maxlen=10000)
        self._seed_velocity_samples: deque[float] = deque(maxlen=20000)
        self._seen_velocity_sample_ts: set[float] = set()
        self._orh: float | None = None
        self._orl: float | None = None
        self._last_v3m_ratio: float | None = None
        self._bootstrap_seed_n: int = 0
        self._bootstrap_seed_ready: bool = False
        self._bootstrap_legacy_seed_state()

    def production_manifest(self) -> StockStrategyManifest:
        return StockStrategyManifest(
            strategy_key="qqq_intraday_stock_plugin",
            symbols=("QQQ", "TQQQ", "SQQQ"),
            template_id="qqq_intraday_core",
            order_namespace="qqq_intraday_plugin",
            data_profile="equity_rth_standard",
            policy_id="qqq_intraday_policy_v1",
            recovery_policy_id="qqq_kernel_close_only",
            session_allowed=("RTH",),
            data_requirements=(
                {"dataset": "ticks", "symbols": ["QQQ", "TQQQ", "SQQQ"], "ttl_sec": 15, "rth": True},
                {"dataset": "bars_5s", "symbols": ["QQQ", "TQQQ", "SQQQ"], "ttl_sec": 20, "rth": True},
                {
                    "dataset": "market_bars",
                    "resolution": "1m",
                    "symbols": ["QQQ"],
                    "ttl_sec": 180,
                    "rth": True,
                    "warmup_required": True,
                    "min_rows": 600,
                },
            ),
            execution_profile={"protection_enabled": True, "quarantine_on_unknown_position": True},
        )

    async def on_init(self, *, strategy: Any, config: Any) -> Optional[Dict[str, Any]]:
        return {
            "strategy_id": str(getattr(strategy, "strategy_id", "") or ""),
            "component": self.component_name,
            "policy_id": str(getattr(config, "policy_id", "") or ""),
            "dry_run": bool(getattr(config, "dry_run", True)),
        }

    async def on_features(self, *, strategy: Any, symbol: str, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        updated = dict(features or {})
        symbol_u = str(symbol or "").strip().upper()
        updated.setdefault("stock_plugin_component", self.component_name)
        updated.setdefault("plugin_signal_family", "qqq_intraday_entry")
        if symbol_u == "QQQ":
            self._update_qqq_legacy_state(strategy=strategy, features=updated)
        if symbol_u:
            self._latest_features_by_symbol[symbol_u] = updated
        return updated

    async def on_intent(self, *, strategy: Any, envelope: Any, symbol: str, side: str) -> Any:
        extras = getattr(envelope, "extras", None)
        if not isinstance(extras, dict):
            return envelope
        extras.setdefault("stock_plugin_component", self.component_name)
        extras.setdefault("signal_family", "qqq_intraday_entry")
        extras.setdefault("source_strategy_id", str(getattr(strategy, "strategy_id", "") or ""))
        if str(extras.get("intent_action") or "").strip().lower() != "entry":
            return envelope

        trigger_id = str(extras.get("trigger_id") or envelope.decision_id or "").strip()
        if not trigger_id or trigger_id in self._published_trigger_ids:
            return envelope
        self._published_trigger_ids.add(trigger_id)

        features = self._latest_features_by_symbol.get("QQQ") or {}
        side_norm = str(side or "").strip().upper()
        direction = "LONG" if side_norm in {"LONG", "BUY"} else "SHORT"
        try:
            trigger_ts = self._trigger_ts_from_features(features)
            candidate = QQQSharedEntryCandidate(
                mode=str(extras.get("mode") or extras.get("entry_mode") or "stocks_kernel_plugin"),
                v3m_signed=self._feature_float(features, "v3m_signed", "v3m", "v3m_price"),
                atr_eff=self._feature_float(features, "atr_eff", "atr", "atr_live"),
                atr_live=self._feature_float(features, "atr_live", "atr_eff", "atr"),
                atr_seed=self._feature_float(features, "atr_seed", "atr_eff", "atr"),
                atr_cap=self._feature_float(features, "atr_cap", "atr_eff", "atr"),
                vol_surge_ratio=self._feature_float(features, "vol_surge_ratio", "volume_surge_ratio", default=0.0),
                distance_from_vwap_atr=self._feature_float(features, "distance_from_vwap_atr", default=0.0),
                data_age_qqq_sec=self._optional_feature_float(features, "data_age_qqq_sec", "data_age_sec"),
                data_age_qqew_sec=self._optional_feature_float(features, "data_age_qqew_sec"),
                continuity_180s_ok=bool(features.get("continuity_180s_ok", True)),
                qqq_now=self._feature_float(features, "qqq_now", "close", "last_price", "price"),
                vwap_now=self._feature_float(features, "vwap_now", "vwap"),
                qqew_now=self._optional_feature_float(features, "qqew_now"),
                qqew_vwap_now=self._optional_feature_float(features, "qqew_vwap_now"),
            )
            artifact, health = publish_qqq_shared_entry_signal(
                strategy_id=str(getattr(strategy, "strategy_id", "") or ""),
                trigger_ts=trigger_ts,
                direction=direction,
                instrument=str(symbol or "").strip().upper(),
                trigger_id=trigger_id,
                decision_id=str(envelope.decision_id or trigger_id),
                trace_id=str(extras.get("trace_id") or envelope.decision_id or trigger_id),
                candidate=candidate,
            )
            extras.setdefault("stock_signal_plane_publish", "ok")
            extras.setdefault("stock_signal_plane_source_strategy_id", str(artifact.strategy_id))
            extras.setdefault("stock_signal_plane_family", str(artifact.signal_family))
            extras.setdefault("stock_signal_plane_state", str(health.get("state") or ""))
            audit = getattr(strategy, "_audit", None)
            if callable(audit):
                audit(
                    stage="stock_signal_publish_ok",
                    symbol=str(symbol or "").strip().upper(),
                    reason="stock_signal_plane_publish_ok",
                    payload={
                        "plugin_component": self.component_name,
                        "signal_family": str(artifact.signal_family),
                        "source_strategy_id": str(artifact.strategy_id),
                        "direction": str(artifact.direction),
                        "option_side": str(artifact.option_side),
                        "trigger_id": str(artifact.trigger_id),
                        "signal_plane_state": str(health.get("state") or ""),
                        "signal_plane_using_redis": bool(health.get("using_redis")),
                        "signal_plane_redis_ok": bool(health.get("redis_ok")),
                    },
                )
        except Exception as exc:
            self._published_trigger_ids.discard(trigger_id)
            extras.setdefault("stock_signal_plane_publish", "failed")
            extras.setdefault("stock_signal_plane_error", str(exc))
            audit = getattr(strategy, "_audit", None)
            if callable(audit):
                audit(
                    stage="stock_signal_publish_failed",
                    symbol=str(symbol or "").strip().upper(),
                    reason=str(exc),
                    payload={"plugin_component": self.component_name, "trigger_id": trigger_id},
                )
        return envelope

    @staticmethod
    def _trigger_ts_from_features(features: Dict[str, Any]) -> datetime:
        raw = features.get("ts") or features.get("bar_ts") or features.get("timestamp") or features.get("ts_et")
        if isinstance(raw, datetime):
            return raw.astimezone(ET)
        if raw:
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return parsed.astimezone(ET)
            except Exception:
                pass
        try:
            ts_epoch_s = float(features.get("ts_epoch_s") or 0.0)
        except Exception:
            ts_epoch_s = 0.0
        if ts_epoch_s > 0.0:
            return datetime.fromtimestamp(ts_epoch_s, tz=ET)
        return datetime.now(ET)

    @staticmethod
    def _feature_float(features: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
        for key in keys:
            try:
                value = float(features.get(key))
            except Exception:
                value = default
            if value != default:
                return value
        return float(default)

    @classmethod
    def _optional_feature_float(cls, features: Dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            if key not in features or features.get(key) is None:
                continue
            try:
                return float(features.get(key))
            except Exception:
                continue
        return None

    def _update_qqq_legacy_state(self, *, strategy: Any, features: Dict[str, Any]) -> None:
        self._warm_seed_from_history(features)
        ts_epoch_s = self._feature_timestamp_s(features)
        price = self._feature_float(features, "qqq_now", "close", "last_price", "price", "last_px")
        if ts_epoch_s > 0.0 and price > 0.0:
            self._record_qqq_sample(ts_epoch_s=ts_epoch_s, price=price)
        seed_min = self._first_entry_seed_min_samples(strategy)
        seed_n = max(int(self._bootstrap_seed_n), len(self._seed_velocity_samples))
        seed_ready = seed_n >= max(1, seed_min)
        if self._bootstrap_seed_ready and seed_n >= max(1, seed_min):
            seed_ready = True
        features.setdefault("seed_n", seed_n)
        features.setdefault("seed_ready", seed_ready)
        if self._last_v3m_ratio is not None:
            features.setdefault("v3m", self._last_v3m_ratio)
            features.setdefault("v3m_signed", self._last_v3m_ratio)
            features.setdefault("v3m_units", "ratio")
        if self._orh is not None:
            features.setdefault("opening_range_high", self._orh)
        if self._orl is not None:
            features.setdefault("opening_range_low", self._orl)

    def _record_qqq_sample(self, *, ts_epoch_s: float, price: float) -> None:
        if self._qqq_samples and ts_epoch_s <= self._qqq_samples[-1][0]:
            if ts_epoch_s == self._qqq_samples[-1][0]:
                return
        self._qqq_samples.append((float(ts_epoch_s), float(price)))
        self._update_opening_range(ts_epoch_s=ts_epoch_s, price=price)
        ago_price = self._price_at_or_before(ts_epoch_s - 180.0)
        if ago_price is None or ago_price <= 0.0:
            return
        if ts_epoch_s in self._seen_velocity_sample_ts:
            return
        self._seen_velocity_sample_ts.add(float(ts_epoch_s))
        if len(self._seen_velocity_sample_ts) > 25000:
            keep = {ts for ts, _ in self._qqq_samples}
            self._seen_velocity_sample_ts.intersection_update(keep)
        delta = float(price) - float(ago_price)
        self._seed_velocity_samples.append(delta)
        self._last_v3m_ratio = delta / float(ago_price)

    def _warm_seed_from_history(self, features: Dict[str, Any]) -> None:
        rows = features.get("bars_5s") or features.get("history_5s") or features.get("bars_1m") or features.get("history_1m")
        if not rows:
            return
        try:
            iterable = list(rows)
        except Exception:
            return
        for row in iterable:
            ts_epoch_s = self._history_bar_ts(row)
            price = self._history_bar_close(row)
            if ts_epoch_s > 0.0 and price > 0.0:
                self._record_qqq_sample(ts_epoch_s=ts_epoch_s, price=price)

    @staticmethod
    def _history_bar_ts(row: Any) -> float:
        for key in ("ts_epoch_s", "timestamp", "ts", "time"):
            value = getattr(row, key, None) if not isinstance(row, dict) else row.get(key)
            if value is None:
                continue
            try:
                if isinstance(value, datetime):
                    return float(value.timestamp())
                return float(value)
            except Exception:
                try:
                    return float(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
                except Exception:
                    continue
        return 0.0

    @staticmethod
    def _history_bar_close(row: Any) -> float:
        for key in ("close", "c", "last_price", "price"):
            value = getattr(row, key, None) if not isinstance(row, dict) else row.get(key)
            try:
                out = float(value)
            except Exception:
                continue
            if out > 0.0:
                return out
        return 0.0

    def _price_at_or_before(self, target_ts: float) -> float | None:
        for sample_ts, price in reversed(self._qqq_samples):
            if sample_ts <= target_ts:
                return price
        return None

    def _update_opening_range(self, *, ts_epoch_s: float, price: float) -> None:
        try:
            dt = datetime.fromtimestamp(float(ts_epoch_s), tz=ET)
        except Exception:
            return
        seconds = dt.hour * 3600 + dt.minute * 60 + dt.second
        if not (9 * 3600 + 30 * 60 <= seconds < 9 * 3600 + 45 * 60):
            return
        self._orh = price if self._orh is None else max(float(self._orh), float(price))
        self._orl = price if self._orl is None else min(float(self._orl), float(price))

    @staticmethod
    def _feature_timestamp_s(features: Dict[str, Any]) -> float:
        for key in ("ts_epoch_s", "ts", "timestamp", "bar_ts", "ts_end", "now_epoch_s"):
            raw = features.get(key)
            if raw is None:
                continue
            try:
                if isinstance(raw, datetime):
                    return float(raw.timestamp())
                return float(raw)
            except Exception:
                try:
                    return float(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
                except Exception:
                    continue
        return 0.0

    @staticmethod
    def _first_entry_seed_min_samples(strategy: Any) -> int:
        config = getattr(strategy, "config", None)
        candidates = (
            getattr(config, "policy_params", None),
            getattr(config, "raw_extras", None),
            getattr(config, "extras", None),
            getattr(strategy, "policy_params", None),
        )
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            raw = candidate.get("first_entry_seed_min_samples")
            if raw is None and isinstance(candidate.get("policy_params"), dict):
                raw = candidate["policy_params"].get("first_entry_seed_min_samples")
            try:
                value = int(raw)
            except Exception:
                continue
            if value > 0:
                return value
        return 600

    def _bootstrap_legacy_seed_state(self) -> None:
        path = Path("runtime/state/qqq_intraday_observability_qqq_intraday_001.json")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        try:
            ts = datetime.fromisoformat(str(payload.get("ts_now_et") or "").replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            return
        if ts.date() != datetime.now(ET).date():
            return
        state = payload.get("strategy_state") if isinstance(payload.get("strategy_state"), dict) else {}
        try:
            self._bootstrap_seed_n = max(0, int(state.get("seed_n") or 0))
        except Exception:
            self._bootstrap_seed_n = 0
        self._bootstrap_seed_ready = bool(state.get("seed_ready", False))
        try:
            orh = float(state.get("orh"))
            self._orh = orh if orh > 0.0 else self._orh
        except Exception:
            pass
        try:
            orl = float(state.get("orl"))
            self._orl = orl if orl > 0.0 else self._orl
        except Exception:
            pass
