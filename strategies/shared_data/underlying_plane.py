from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import RLock
from typing import Any

from prometheus_client import Counter


shared_plane_readback_diff_total = Counter(
    "shared_plane_readback_diff_total",
    "Shared underlying data plane readback mismatches",
    ["namespace", "symbol", "field"],
)


@dataclass(frozen=True)
class UnderlyingSharedSnapshot:
    symbol: str
    namespace: str
    ts_epoch_s: float
    last_price: float | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    vwap: float | None = None


class UnderlyingSharedDataPlane:
    """
    Cross-asset shared cache for underlying market data and base features.

    This is intentionally contract-only infrastructure:
    - shares underlying snapshots / bars / base features
    - does not carry execution or position semantics
    """

    def __init__(self, *, max_bars_per_symbol: int = 512) -> None:
        self._lock = RLock()
        self._max_bars_per_symbol = max(64, int(max_bars_per_symbol))
        self._snapshots: dict[tuple[str, str], UnderlyingSharedSnapshot] = {}
        self._features: dict[tuple[str, str], dict[str, Any]] = {}
        self._feature_ts: dict[tuple[str, str], float] = {}
        self._bars: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=self._max_bars_per_symbol))

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        return str(symbol or "").strip().upper()

    @staticmethod
    def _norm_namespace(namespace: str | None) -> str:
        return str(namespace or "global").strip().lower() or "global"

    @staticmethod
    def _f(value: Any) -> float | None:
        if value is None:
            return None
        try:
            out = float(value)
        except Exception:
            return None
        return out if out == out else None

    @staticmethod
    def _is_fresh(*, ts_epoch_s: float, max_staleness_sec: float | None, now_epoch_s: float | None) -> bool:
        if max_staleness_sec is None:
            return True
        try:
            threshold = float(max_staleness_sec)
        except Exception:
            return True
        if threshold <= 0.0:
            return True
        try:
            ts_v = float(ts_epoch_s)
        except Exception:
            return False
        if ts_v <= 0.0:
            return False
        try:
            now_v = float(now_epoch_s) if now_epoch_s is not None else 0.0
        except Exception:
            now_v = 0.0
        if now_v <= 0.0:
            return True
        return (now_v - ts_v) <= threshold

    def publish_underlying(
        self,
        *,
        symbol: str,
        namespace: str | None = None,
        ts_epoch_s: float,
        last_price: Any = None,
        bid_price: Any = None,
        ask_price: Any = None,
        vwap: Any = None,
        features: dict[str, Any] | None = None,
        bar: dict[str, Any] | None = None,
    ) -> None:
        ns = self._norm_namespace(namespace)
        sym = self._norm_symbol(symbol)
        if not sym:
            return
        try:
            ts_v = float(ts_epoch_s)
        except Exception:
            ts_v = 0.0
        key = (ns, sym)
        with self._lock:
            if ts_v > 0.0:
                self._snapshots[key] = UnderlyingSharedSnapshot(
                    symbol=sym,
                    namespace=ns,
                    ts_epoch_s=ts_v,
                    last_price=self._f(last_price),
                    bid_price=self._f(bid_price),
                    ask_price=self._f(ask_price),
                    vwap=self._f(vwap),
                )
            if isinstance(features, dict) and features:
                self._features[key] = dict(features)
                self._feature_ts[key] = ts_v
            if isinstance(bar, dict) and bar:
                self._bars[key].append(dict(bar))

    def latest_snapshot(
        self,
        *,
        symbol: str,
        namespace: str | None = None,
        max_staleness_sec: float | None = None,
        now_epoch_s: float | None = None,
    ) -> UnderlyingSharedSnapshot | None:
        ns = self._norm_namespace(namespace)
        sym = self._norm_symbol(symbol)
        if not sym:
            return None
        key = (ns, sym)
        with self._lock:
            snapshot = self._snapshots.get(key)
        if snapshot is None:
            return None
        if not self._is_fresh(
            ts_epoch_s=float(snapshot.ts_epoch_s or 0.0),
            max_staleness_sec=max_staleness_sec,
            now_epoch_s=now_epoch_s,
        ):
            return None
        return snapshot

    def latest_features(
        self,
        *,
        symbol: str,
        namespace: str | None = None,
        max_staleness_sec: float | None = None,
        now_epoch_s: float | None = None,
    ) -> dict[str, Any]:
        ns = self._norm_namespace(namespace)
        sym = self._norm_symbol(symbol)
        if not sym:
            return {}
        key = (ns, sym)
        with self._lock:
            features = dict(self._features.get(key) or {})
            ts_epoch_s = float(self._feature_ts.get(key) or 0.0)
        if not features:
            return {}
        if not self._is_fresh(ts_epoch_s=ts_epoch_s, max_staleness_sec=max_staleness_sec, now_epoch_s=now_epoch_s):
            return {}
        return features

    def recent_bars(
        self,
        *,
        symbol: str,
        limit: int,
        namespace: str | None = None,
        max_staleness_sec: float | None = None,
        now_epoch_s: float | None = None,
    ) -> tuple[dict[str, Any], ...]:
        ns = self._norm_namespace(namespace)
        sym = self._norm_symbol(symbol)
        if not sym:
            return ()
        n = max(1, int(limit))
        key = (ns, sym)
        with self._lock:
            rows = list(self._bars.get(key) or ())
        if not rows:
            return ()
        if max_staleness_sec is not None and float(max_staleness_sec) > 0.0:
            latest = rows[-1]
            try:
                bar_ts = float(latest.get("ts_epoch_s") or 0.0)
            except Exception:
                bar_ts = 0.0
            if not self._is_fresh(ts_epoch_s=bar_ts, max_staleness_sec=max_staleness_sec, now_epoch_s=now_epoch_s):
                return ()
        return tuple(rows[-n:])

    def assert_latest_matches(
        self,
        *,
        symbol: str,
        namespace: str | None = None,
        expected: dict[str, Any],
        fields: tuple[str, ...] = ("last_price", "ts_epoch_s"),
    ) -> None:
        """CI/runtime gate: verify a consumer reads the same snapshot fields a producer wrote."""
        ns = self._norm_namespace(namespace)
        sym = self._norm_symbol(symbol)
        snapshot = self.latest_snapshot(symbol=sym, namespace=ns)
        if snapshot is None:
            shared_plane_readback_diff_total.labels(namespace=ns, symbol=sym, field="missing_snapshot").inc()
            raise AssertionError(f"shared_plane_snapshot_missing:{ns}:{sym}")
        diffs: list[str] = []
        for field in fields:
            actual = getattr(snapshot, field, None)
            want = expected.get(field)
            if isinstance(actual, float) or isinstance(want, float):
                try:
                    matches = abs(float(actual) - float(want)) <= 1e-9
                except Exception:
                    matches = False
            else:
                matches = actual == want
            if not matches:
                shared_plane_readback_diff_total.labels(namespace=ns, symbol=sym, field=str(field)).inc()
                diffs.append(f"{field}:actual={actual!r}:expected={want!r}")
        if diffs:
            raise AssertionError(f"shared_plane_readback_diff:{ns}:{sym}:{';'.join(diffs)}")


_GLOBAL_UNDERLYING_PLANE = UnderlyingSharedDataPlane()


def shared_underlying_data_plane() -> UnderlyingSharedDataPlane:
    return _GLOBAL_UNDERLYING_PLANE
