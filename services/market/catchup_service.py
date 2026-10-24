# services/market/catchup_service.py
"""
CatchupService
--------------
Periodic intraday/yesterday gap scanner that enqueues small backfill jobs to Kafka.

- Reads from ClickHouse `trading.bars_{res}_current` (views created in P2)
- For each symbol & day (today / yesterday), builds the theoretical RTH timeline and
  diffs against existing bars. Missing windows are coalesced into short ranges
  (<= max_range_minutes) and sent to BackfillService via request topic.
- BackfillService (1m) must be started with request_topic="md.backfill.request.1m.v1",
  bar_topic="md.bar.1m.ibkr", default_bar_size="1 min".
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from typing import Iterable, List, Dict, Tuple, Optional

from aiokafka import AIOKafkaProducer
from zoneinfo import ZoneInfo

from libs.db import ch_pool
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)

NY_TZ = ZoneInfo(getattr(config, "timezone", "America/New_York"))

def _parse_bar_size_to_seconds(bar_size: str) -> int:
    s = bar_size.strip().lower()
    if s in ("5 sec", "5 secs", "5 second", "5 seconds"):
        return 5
    if s in ("1 min", "1 mins", "1 minute", "1 minutes"):
        return 60
    if s in ("5 min", "5 mins", "5 minutes"):
        return 300
    if s in ("10 min", "10 mins", "10 minutes"):
        return 600
    if s in ("15 min", "15 mins", "15 minutes"):
        return 900
    if s in ("30 min", "30 mins", "30 minutes"):
        return 1800
    if s in ("1 hour", "1 hr", "60 min", "60 mins"):
        return 3600
    if s in ("2 hour", "2 hours", "2 hr"):
        return 7200
    if s in ("3 hour", "3 hours", "3 hr"):
        return 10800
    if s in ("4 hour", "4 hours", "4 hr"):
        return 14400
    if s in ("1 day", "1 d"):
        return 86400
    raise ValueError(f"Unsupported bar_size: {bar_size}")

def _res_token(bar_size: str) -> str:
    sec = _parse_bar_size_to_seconds(bar_size)
    if sec == 5: return "5s"
    if sec == 60: return "1m"
    if sec == 300: return "5m"
    if sec == 600: return "10m"
    if sec == 900: return "15m"
    if sec == 1800: return "30m"
    if sec == 3600: return "1h"
    if sec == 7200: return "2h"
    if sec == 10800: return "3h"
    if sec == 14400: return "4h"
    if sec == 86400: return "1d"
    return f"{sec}s"

def _rth_bounds(d: datetime) -> Tuple[datetime, datetime]:
    """Return regular trading hours window (09:30–16:00 ET) as naive NY datetimes."""
    local_day = d.astimezone(NY_TZ).date()
    start_local = datetime.combine(local_day, time(9, 30), tzinfo=NY_TZ)
    end_local = datetime.combine(local_day, time(16, 0), tzinfo=NY_TZ)
    return (
        start_local.replace(tzinfo=None),
        end_local.replace(tzinfo=None),
    )


def _full_day_bounds(d: datetime) -> Tuple[datetime, datetime]:
    """Return full session window (00:00:00 → 24h) as naive NY datetimes."""
    local_day = d.astimezone(NY_TZ).date()
    start_local = datetime.combine(local_day, time(0, 0), tzinfo=NY_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.replace(tzinfo=None),
        end_local.replace(tzinfo=None),
    )


def _bounds_for_bar(d: datetime, bar_seconds: int) -> Tuple[datetime, datetime]:
    """Pick appropriate scan bounds for the requested resolution."""
    if bar_seconds >= 86400:
        return _full_day_bounds(d)
    return _rth_bounds(d)

def _daterange_days(days: Iterable[int]) -> List[datetime]:
    # 0=today, -1=yesterday (in NY timezone, not UTC)
    now_ny = datetime.now(NY_TZ)
    today_ny = now_ny.date()
    return [datetime.combine(today_ny + timedelta(days=o), time(0,0,0)) for o in days]

def _chunk_ranges(indices: List[datetime], step: timedelta, max_range: timedelta):
    if not indices: return []
    shards = []
    cur_start = indices[0]
    prev = indices[0]
    max_end = cur_start + max_range
    for ts in indices[1:]:
        if (ts - prev) == step and ts < max_end:
            prev = ts
            continue
        shards.append((cur_start, prev + step))
        cur_start, prev, max_end = ts, ts, ts + max_range
    shards.append((cur_start, prev + step))
    return shards

@dataclass
class CatchupConfig:
    enabled: bool = True
    interval_sec: int = 120          # Scan frequency
    bar_size: str = "1 min"          # Currently only 1m; can add 5m/15m later
    request_topic: str = "md.backfill.request.1m.v1"
    use_rth: bool = True
    lag_grace_sec: int = 30          # Buffer from "now" to avoid false gaps on latest bar
    max_range_minutes: int = 60      # Small catchup only handles <= 60 min gaps
    symbols: List[str] = None        # If not configured, dynamically discover from 5s today
    view: str = None                 # Override view name (defaults to bars_{res}_current)

class CatchupService:
    def __init__(self, cfg: Optional[CatchupConfig] = None) -> None:
        self.cfg = cfg or CatchupConfig()
        self.db = None  # Will be initialized in start() with async client
        kafka_cfg = getattr(config, "kafka_config", {}) or {}
        self.bootstrap = kafka_cfg.get("bootstrap_servers")
        self.producer: Optional[AIOKafkaProducer] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        self.bar_sec = _parse_bar_size_to_seconds(self.cfg.bar_size)
        self.res_token = _res_token(self.cfg.bar_size)
        # Use explicit view if provided, otherwise auto-generate from bar_size
        if self.cfg.view:
            self.view_name = self.cfg.view if "." in self.cfg.view else f"trading.{self.cfg.view}"
        else:
            self.view_name = f"trading.bars_{self.res_token}_current"

    async def start(self) -> None:
        logger.info("CatchupService starting...", request_topic=self.cfg.request_topic, bar_size=self.cfg.bar_size)
        if not self.bootstrap:
            raise RuntimeError("Kafka bootstrap_servers missing in config")

        # Initialize async ClickHouse client (uses shared connection pool)
        self.db = await ch_pool.get_async_client()

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda v: v.encode("utf-8") if isinstance(v, str) else v,
            linger_ms=5,
        )
        await self.producer.start()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"catchup_scan_{self.res_token}")
        logger.info("CatchupService started", view=self.view_name)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.producer:
            try:
                await asyncio.wait_for(self.producer.stop(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Catchup producer stop timed out (degraded Kafka), forcing close")
            except Exception as e:
                logger.warning("Catchup producer stop failed", error=str(e))
        logger.info("CatchupService stopped")

    async def _loop(self) -> None:
        while self._running and self.cfg.enabled:
            try:
                symbols = self.cfg.symbols or await self._discover_symbols()
                if symbols:
                    await self._scan(symbols)
            except Exception as exc:
                logger.error("Catchup loop error", error=str(exc), exc_info=True)
            await asyncio.sleep(self.cfg.interval_sec)

    async def _discover_symbols(self, limit: int = 200) -> List[str]:
        """
        Discover symbols, prioritizing the central universe list from config.

        Priority order:
        1. Central config.universe (single source of truth)
        2. Instance-level self.cfg.symbols (if explicitly configured)
        3. Database auto-discovery (fallback for edge cases)
        """
        # 1. Prioritize the central universe list (same as OfflineDataReconciler)
        universe = getattr(config, 'universe', None) or []
        if universe:
            logger.info("Using symbols from central config.universe", count=len(universe))
            return universe[:limit] if limit else universe

        # 2. Fallback to symbols specified in the service instance (if any)
        if self.cfg.symbols:
            logger.info("Using symbols from catchup service instance config", count=len(self.cfg.symbols))
            return self.cfg.symbols[:limit] if limit else self.cfg.symbols

        # 3. Last resort: auto-discover from the database
        logger.info("No symbols configured in universe or instance, auto-discovering from bars_5s...")
        sql = """
        SELECT DISTINCT symbol
        FROM trading.bars_5s
        WHERE d = today()
        LIMIT %(limit)s
        """
        try:
            result = await self.db.query(sql, {"limit": limit})
            rows = result.result_rows if hasattr(result, 'result_rows') else result
            symbols = [r[0] if isinstance(r, (list, tuple)) else r["symbol"] for r in rows]
            logger.info("Auto-discovered symbols from database", count=len(symbols))
            return symbols
        except Exception as exc:
            logger.error("Symbol discovery from database failed", error=str(exc))
            return []

    async def _scan(self, symbols: List[str]) -> None:
        if self.bar_sec >= 86400:
            lookback_days = max(1, int(self.cfg.max_range_minutes // (24 * 60)) or 1)
            day_offsets = [-(i + 1) for i in range(lookback_days)]
        else:
            day_offsets = [0, -1]

        days = _daterange_days(day_offsets)
        step = timedelta(seconds=self.bar_sec)
        max_range = timedelta(minutes=self.cfg.max_range_minutes)
        lag_cutoff = datetime.now(NY_TZ).replace(tzinfo=None) - timedelta(seconds=self.cfg.lag_grace_sec)

        for day0 in days:
            window_start, window_end = _bounds_for_bar(day0, self.bar_sec)
            today_ny = datetime.now(NY_TZ).date()
            local_day = day0.astimezone(NY_TZ).date()

            if self.bar_sec >= 86400:
                if local_day >= today_ny:
                    # Skip incomplete (today) or future days – wait for full session close.
                    continue
                if local_day.weekday() >= 5:
                    # Markets closed on weekends; avoid generating empty backfills.
                    continue
            else:
                # Don't scan "future" for today, leave buffer
                if local_day == today_ny:
                    window_end = min(window_end, lag_cutoff)

            if window_start >= window_end:
                continue

            for symbol in symbols:
                try:
                    missing_ts = await self._missing_windows(symbol, window_start, window_end, step)
                    if not missing_ts:
                        continue
                    shards = _chunk_ranges(missing_ts, step, max_range)
                    for (start_dt, end_dt) in shards:
                        await self._enqueue(symbol, start_dt, end_dt)
                except Exception as exc:
                    logger.error("Scan symbol error", symbol=symbol, error=str(exc))

    async def _missing_windows(
        self,
        symbol: str,
        rth_start: datetime,
        rth_end: datetime,
        step: timedelta,
    ) -> List[datetime]:
        # Read current view, get existing windows
        # CRITICAL: Add memory settings to prevent OOM (code 241) on large scans
        sql = f"""
        SELECT ts_start
        FROM {self.view_name}
        WHERE symbol = %(symbol)s
          AND ts_start >= %(start)s
          AND ts_start <  %(end)s
        ORDER BY ts_start
        SETTINGS max_memory_usage = 1000000000,
                 max_bytes_before_external_sort = 100000000,
                 max_bytes_before_external_group_by = 100000000
        """
        result = await self.db.query(sql, {"symbol": symbol, "start": rth_start, "end": rth_end})
        rows = result.result_rows if hasattr(result, 'result_rows') else result
        have = {r[0] if isinstance(r, (list, tuple)) else r["ts_start"] for r in rows}

        out = []
        cur = rth_start
        while cur < rth_end:
            if cur not in have:
                out.append(cur)
            cur += step
        return out

    async def _enqueue(self, symbol: str, start_dt: datetime, end_dt: datetime) -> None:
        if not self.producer:
            return

        # CRITICAL: Never request data beyond current time
        now_ny = datetime.now(NY_TZ).replace(tzinfo=None)
        if end_dt > now_ny:
            end_dt = now_ny

        # Skip if range is invalid after capping
        if start_dt >= end_dt:
            return

        payload = {
            "data": {
                "symbol": symbol,
                "from_ts": int(start_dt.replace(tzinfo=NY_TZ).timestamp()),
                "to_ts": int(end_dt.replace(tzinfo=NY_TZ).timestamp()),
                "use_rth": bool(self.cfg.use_rth),
                "bar_size": self.cfg.bar_size,
                "reason": "catchup",
            }
        }
        try:
            await self.producer.send(self.cfg.request_topic, key=symbol, value=payload)
            logger.info("Catchup enqueued",
                        symbol=symbol,
                        from_ts=payload["data"]["from_ts"],
                        to_ts=payload["data"]["to_ts"],
                        bar_size=self.cfg.bar_size,
                        topic=self.cfg.request_topic)
        except Exception as exc:
            logger.error("Catchup enqueue failed", symbol=symbol, error=str(exc))
