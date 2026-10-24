"""
RealtimeFeatureEngine
---------------------

Purpose:
- Centralize real-time market feature assembly for strategies.
- Provide warm-up from historical bars, incremental 1s → multi-timeframe aggregation,
  and a callback interface to push fresh features to strategy logic.

Design notes:
- Dependency-injection friendly: accepts a data fetcher and an aggregator with the expected
  methods instead of importing app-specific modules. This allows reuse across strategies
  and reduces tight coupling.
- Emits callbacks only when a new bucket is completed (e.g., a new 1m bar), with a
  best-effort normalization layer to avoid spurious emissions when upstream aggregators
  expose sliding-window bars.
- Logging uses libs.logger for consistency with the rest of the system.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from libs.logger import get_logger


Callback = Callable[[str, Dict[str, Any]], Awaitable[None] | None]


class RealtimeFeatureEngine:
    """
    Incrementally builds multi-timeframe features from 1s bars and dispatches feature
    snapshots to strategies via callbacks.

    Expected aggregator interface (duck-typed):
      - update_from_1s(symbol: str, bar: dict) -> None
      - seed_with_5s(symbol: str, bars_5s: List[dict]) -> None
      - get_snapshot(symbol: str) -> Dict[str, Any]

    Expected fetcher interface (duck-typed):
      - prefill_5s_from_clickhouse(symbol: str, minutes: int) -> List[dict]
      - get_1s_bars_from_redis(symbol: str, n: int) -> List[dict]
    """

    _BUCKET_SECONDS = {
        "1s": 1,
        "5s": 5,
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
    }

    def __init__(
        self,
        *,
        symbols: List[str],
        fetcher: Optional[Any] = None,
        aggregator: Optional[Any] = None,
        prefill_minutes: int = 20,
        emit_on: str = "1m",
        max_backfill_1s: int = 120,
    ) -> None:
        if emit_on not in self._BUCKET_SECONDS:
            raise ValueError(f"Unsupported emit_on bucket: {emit_on}")

        self.symbols = symbols
        self.fetcher = fetcher
        self.aggregator = aggregator
        self.prefill_minutes = int(prefill_minutes)
        self.emit_on = emit_on
        self.max_backfill_1s = int(max_backfill_1s)

        self._callbacks: List[Callback] = []
        # Track last emitted bucket boundary per symbol for de-duplication
        self._last_emitted_bucket: Dict[str, int] = {}
        self.log = get_logger("RealtimeFeatureEngine").bind(component="realtime_features")

    # ------------------------- Public API -------------------------
    def register_callback(self, fn: Callback) -> None:
        """Register a callback to receive fresh feature snapshots.

        Callback signature: async|sync (symbol: str, snapshot: dict) -> None
        """
        if not callable(fn):
            raise TypeError("callback must be callable")
        self._callbacks.append(fn)

    async def warm_up(self) -> None:
        """
        Prefill the aggregator with recent 5s bars from ClickHouse so that higher-timeframe
        indicators are in a sensible state before live ticks arrive.
        """
        if not self.fetcher or not self.aggregator:
            self.log.warning("warm_up skipped: missing fetcher or aggregator")
            return

        if self.prefill_minutes <= 0:
            return

        for sym in self.symbols:
            try:
                bars_5s = await self.fetcher.prefill_5s_from_clickhouse(sym, minutes=self.prefill_minutes)
                if bars_5s:
                    self.aggregator.seed_with_5s(sym, bars_5s)
                    self.log.info("prefill_ok", symbol=sym, count=len(bars_5s))
                else:
                    self.log.warning("prefill_empty", symbol=sym)
            except Exception as e:  # pragma: no cover - defensive
                self.log.error("prefill_err", symbol=sym, err=str(e))

    async def process_1s_bars(self, symbol: str, bars_1s: List[Dict[str, Any]]) -> None:
        """Ingest a batch of 1s bars for a single symbol and emit callbacks if a new
        bucket (emit_on) completed.
        """
        if not self.aggregator:
            self.log.warning("process_1s_bars skipped: missing aggregator", symbol=symbol)
            return

        if not bars_1s:
            return

        for bar in bars_1s:
            self.aggregator.update_from_1s(symbol, bar)

        await self._maybe_emit(symbol)

    async def on_sfs_bar(self, symbol: str, bar_1s: Dict[str, Any]) -> None:
        """Ingest a single 1s bar and emit callbacks if a new bucket completed.

        Strategies can forward their real-time tick or 1s bar stream directly here.
        """
        if not self.aggregator:
            self.log.warning("on_sfs_bar skipped: missing aggregator", symbol=symbol)
            return
        self.aggregator.update_from_1s(symbol, bar_1s)
        await self._maybe_emit(symbol)

    async def poll_redis_once(self) -> None:
        """
        Convenience helper: fetch last N 1s bars from Redis for all symbols
        and process them. Intended for simple loops; production systems
        should prefer pushing bars from the data plane to reduce latency.
        """
        if not self.fetcher:
            self.log.warning("poll_redis_once skipped: missing fetcher")
            return
        tasks = [self.fetcher.get_1s_bars_from_redis(sym, n=self.max_backfill_1s) for sym in self.symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, res in zip(self.symbols, results):
            if isinstance(res, Exception):
                self.log.error("redis_fetch_err", symbol=sym, err=str(res))
                continue
            await self.process_1s_bars(sym, res or [])

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        """Expose the aggregator snapshot for direct consumption by strategies."""
        if not self.aggregator:
            return {}
        return self.aggregator.get_snapshot(symbol)

    # ------------------------- Internals -------------------------
    async def _maybe_emit(self, symbol: str) -> None:
        """Emit callbacks when a new emit_on bucket boundary is crossed.

        To be robust against sliding-window aggregators (which may expose a new bar every
        second once the first window fills), we normalize timestamps to bucket boundaries.
        """
        snap = self.get_snapshot(symbol) or {}
        bucket = snap.get(self.emit_on) or {}
        bars = bucket.get("bars") or []
        if not bars:
            return

        latest_ts_raw = bars[-1].get("ts")
        bucket_sec = self._BUCKET_SECONDS[self.emit_on]
        norm_ts = self._normalize_to_bucket_boundary(latest_ts_raw, bucket_sec)
        if norm_ts is None:
            # Fallback: do not emit if we cannot normalize timestamp
            return

        last_emitted = self._last_emitted_bucket.get(symbol)
        if last_emitted is not None and norm_ts <= last_emitted:
            return  # Not a new bucket

        # Update state before callbacks to avoid reentrancy issues
        self._last_emitted_bucket[symbol] = norm_ts

        # Dispatch callbacks (support sync/async callbacks)
        for cb in list(self._callbacks):
            try:
                maybe_awaitable = cb(symbol, snap)
                if asyncio.iscoroutine(maybe_awaitable):
                    await maybe_awaitable
            except Exception as e:  # pragma: no cover - defensive
                self.log.error("callback_err", symbol=symbol, err=str(e))

    @staticmethod
    def _normalize_to_bucket_boundary(ts_val: Any, bucket_seconds: int) -> Optional[int]:
        """
        Convert an incoming timestamp (ms int, seconds int, ISO str, or datetime)
        to a UNIX-seconds integer aligned to the start of its bucket.
        Returns None if parsing fails.
        """
        try:
            if ts_val is None:
                return None
            if isinstance(ts_val, (int, float)):
                # Heuristic: treat values > 1e12 as ms, > 1e9 as seconds
                ts_sec = int(ts_val / 1000) if ts_val > 1_000_000_000_000 else int(ts_val)
            elif isinstance(ts_val, str):
                # ISO 8601 parsing; allow timezone info if present
                ts_dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                ts_sec = int(ts_dt.timestamp())
            elif isinstance(ts_val, datetime):
                ts_sec = int(ts_val.timestamp())
            else:
                return None
            return (ts_sec // bucket_seconds) * bucket_seconds
        except Exception:
            return None


__all__ = ["RealtimeFeatureEngine"]

