"""
SFS-5s Writer (Sliding 5-second features at 1-second stride)

Design:
- Base on Redis 1s microbars (produced by sfs_1s_service)
- Every second, for each configured symbol, read last 5×1s and compute a 5s sliding bar
- Persist to ClickHouse table `sfs.sfs_5s` (separate schema), TTL=30 days
- Export Prometheus metrics: write rate, latency, errors

Config (config.yml):
services:
  sfs_5s:
    enabled: false
    flush_period_ms: 1000
    lookback_n: 5
    group: sfs_5s_writer   # logical name for metrics grouping

Notes:
- Uses config.universe as symbol list; you can narrow via services.sfs_5s.symbols: [AAPL,TSLA]
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Tuple
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

from libs.config import config
from libs.logger import get_logger
from libs.db.ch_pool import get_async_client
from prometheus_client import Counter, Histogram

logger = get_logger(__name__)


SFS5S_WRITES = Counter(
    "sfs5s_rows_written_total", "Rows written to sfs.sfs_5s", ["group"]
)
SFS5S_ERRORS = Counter(
    "sfs5s_errors_total", "Errors in sfs_5s_writer", ["stage", "group"]
)
SFS5S_LAT_MS = Histogram(
    "sfs5s_flush_latency_ms",
    "Flush latency per cycle (ms)",
    buckets=(10, 25, 50, 100, 200, 400, 800, 1500, 3000),
)


class Sfs5sWriter:
    def __init__(
        self,
        *,
        redis_url: Optional[str] = None,
        flush_period_ms: int = 1000,
        lookback_n: int = 5,
        group: str = "default",
        symbols: Optional[List[str]] = None,
        rth_only: bool = False,
        trading_hours: Optional[dict] = None,
    ) -> None:
        self.redis = aioredis.from_url(redis_url or config.redis_config, decode_responses=True)
        self.flush_period_ms = int(flush_period_ms)
        self.lookback_n = max(1, int(lookback_n))
        self.group = group
        # Symbol universe
        if symbols is None:
            syms = getattr(config, "universe", [])
        else:
            syms = symbols
        self.symbols: List[str] = list(syms or [])
        self.rth_only = bool(rth_only)
        self._rth_window = self._parse_trading_hours(trading_hours or config.get("trading_hours", {}) or {})
        self._tz = ZoneInfo("America/New_York")
        # ClickHouse async client (initialized in run() to avoid blocking __init__)
        self.ch = None
        self._running = False

    def _key(self, symbol: str) -> str:
        return f"sfs:1s:{symbol}"

    @staticmethod
    def _parse_trading_hours(trading_hours: dict) -> Tuple[float, float]:
        """Convert trading hours config (HH:MM strings) to decimal hours."""

        def _to_decimal(val: str, default: float) -> float:
            try:
                hour, minute = str(val).split(":")
                return int(hour) + int(minute) / 60.0
            except Exception:
                return default

        open_dec = _to_decimal(trading_hours.get("open_et", "09:30"), 9.5)
        close_dec = _to_decimal(trading_hours.get("close_et", "16:00"), 16.0)
        return open_dec, close_dec

    def _is_rth(self, ts_ms: int) -> bool:
        """Return True if timestamp (ms) is within configured RTH window."""
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=self._tz)
        if dt.weekday() > 4:
            return False
        time_decimal = dt.hour + dt.minute / 60.0
        open_dec, close_dec = self._rth_window
        return open_dec <= time_decimal < close_dec

    def _to_row(self, symbol: str, items: List[dict]) -> Optional[dict]:
        if not items:
            return None
        # items newest→older; we need oldest→newest for open/close
        win = list(reversed(items))
        try:
            open_px = float(win[0].get("open") or 0)
            close_px = float(win[-1].get("close") or 0)
            if open_px <= 0 or close_px <= 0:
                return None
            high_px = max(float(x.get("high") or 0) for x in win)
            low_px = min(float(x.get("low") or 0) for x in win)
            vol = sum(float(x.get("volume") or 0) for x in win)
            trades = int(sum(int(x.get("trades") or 0) for x in win))
            # vwap approx from 1s closes
            pv = sum(float(x.get("close") or 0) * float(x.get("volume") or 0) for x in win)
            vwap = (pv / vol) if vol > 0 else close_px
            ts_ms = int(win[-1].get("ts") or 0)  # newest 1s ts
            if ts_ms <= 0:
                return None
            if self.rth_only and not self._is_rth(ts_ms):
                return None
            ret_5s = (close_px - open_px) / open_px if open_px > 0 else 0.0
            # ClickHouse expects Python datetime or string? Wrapper converts;
            # We'll pass ts as integer ms and convert in INSERT with toDateTime64.
            return {
                "symbol": symbol,
                "ts_ms": ts_ms,
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "vwap": vwap,
                "volume": vol,
                "trades": trades,
                "ret_5s": ret_5s,
            }
        except Exception:
            return None

    async def _fetch_window(self, symbol: str) -> List[dict]:
        try:
            vals = await self.redis.lrange(self._key(symbol), 0, self.lookback_n - 1)
            return [json.loads(v) for v in vals if v]
        except Exception as e:  # noqa: BLE001
            SFS5S_ERRORS.labels(stage="redis", group=self.group).inc()
            logger.error("sfs_5s_writer: Redis error", error=str(e), symbol=symbol)
            return []

    async def run(self) -> None:
        self._running = True

        # Initialize async ClickHouse client to avoid blocking event loop
        self.ch = await get_async_client()

        logger.info(
            "SFS-5s Writer started",
            group=self.group,
            flush_ms=self.flush_period_ms,
            lookback_n=self.lookback_n,
            symbols=len(self.symbols),
        )
        try:
            while self._running:
                t0 = time.time()
                rows = []
                for sym in self.symbols:
                    items = await self._fetch_window(sym)
                    if len(items) < self.lookback_n:
                        continue
                    row = self._to_row(sym, items)
                    if row:
                        rows.append(row)

                if rows:
                    try:
                        # Convert ts_ms -> Python datetime for clickhouse_connect insert
                        data = [
                            (
                                r["symbol"],
                                datetime.fromtimestamp(r["ts_ms"] / 1000.0, tz=self._tz),
                                r["open"], r["high"], r["low"], r["close"],
                                r["vwap"], r["volume"], int(r["trades"]), r["ret_5s"],
                            ) for r in rows
                        ]
                        cols = [
                            "symbol", "ts", "open", "high", "low", "close",
                            "vwap", "volume", "trades", "ret_5s",
                        ]
                        # Use await with async client to prevent event loop blocking
                        # CRITICAL: Add memory settings to prevent OOM (code 241)
                        await self.ch.insert(
                            "sfs.sfs_5s", 
                            data, 
                            column_names=cols,
                            settings={
                                'max_memory_usage': '2000000000',  # 2GB limit per insert
                                'async_insert': '1',               # Enable async insert
                                'wait_for_async_insert': '1',      # Wait for completion to avoid CH overload / resets
                            }
                        )
                        SFS5S_WRITES.labels(group=self.group).inc(len(rows))
                    except Exception as e:  # noqa: BLE001
                        SFS5S_ERRORS.labels(stage="clickhouse", group=self.group).inc()
                        logger.error("sfs_5s_writer: ClickHouse insert failed", error=str(e))

                # sleep remaining time to align ~1s cadence
                elapsed_ms = (time.time() - t0) * 1000.0
                try:
                    SFS5S_LAT_MS.observe(elapsed_ms)
                except Exception:
                    pass
                to_sleep = max(0.0, (self.flush_period_ms / 1000.0) - (elapsed_ms / 1000.0))
                await asyncio.sleep(to_sleep)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await self.redis.close()
            except Exception:
                pass
            logger.info("SFS-5s Writer stopped")

    async def stop(self) -> None:
        self._running = False
