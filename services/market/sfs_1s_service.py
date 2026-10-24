"""
SFS-1s (Redis-only) Service

Purpose:
- Build 1-second OHLCV bars and minimal micro features from tick stream
- Keep a per-symbol Redis ring buffer with TTL (30–60 minutes)
- Do NOT persist to ClickHouse by default to avoid storage explosion

Inputs:
- Kafka topic: md.tick.trade.v1 (JSON with symbol, price, size, ts_us)
  Optional future: md.tick.quote.v1 for spread metrics

Outputs (Redis):
- Key: sfs:1s:<symbol>
- Type: LIST (LPUSH newest first), EXPIRE=<ttl_sec>
- Entry JSON schema:
  {
    "ts": <int, ms>,
    "open": <float>, "high": <float>, "low": <float>, "close": <float>,
    "volume": <float>, "trades": <int>
  }

Usage:
  python -m services.market.sfs_1s_service  # or python services/market/sfs_1s_service.py

Config:
- Env:
  KAFKA_BOOTSTRAP_SERVERS (default from config.yml)
  STATEBUS_REDIS (default redis://127.0.0.1:6379/0)
  SFS_1S_TTL_SEC (default 3600)
  SFS_1S_MAX_LEN (default equal to TTL seconds)
  SFS_1S_FLUSH_DELAY_MS (default 250)
  SFS_1S_GROUP_ID (default sfs_1s_agg)
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from collections import deque
from typing import Deque, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError, KafkaConnectionError, CommitFailedError, ConsumerStoppedError
import redis.asyncio as aioredis

from libs.logger import get_logger
from libs.config import config
from prometheus_client import Counter, Histogram, Gauge
from strategies.shared_data import shared_underlying_data_plane

logger = get_logger(__name__)

# Prometheus metrics
SFS1S_TICKS_TOTAL = Counter(
    "sfs1s_ticks_total", "Processed trade ticks for SFS-1s", ["symbol"]
)
SFS1S_BARS_FLUSHED_TOTAL = Counter(
    "sfs1s_bars_flushed_total", "Flushed 1s bars to Redis", ["symbol"]
)
SFS1S_REDIS_ERRORS_TOTAL = Counter(
    "sfs1s_redis_errors_total", "Redis write failures in SFS-1s"
)
SFS1S_FLUSH_LATENCY_MS = Histogram(
    "sfs1s_flush_latency_ms",
    "SFS-1s flush latency in milliseconds",
    buckets=(10, 25, 50, 100, 200, 500, 1000, 2000, 5000),
)
SHARED_PLANE_PUBLISH_LATENCY_MS = Histogram(
    "shared_plane_publish_latency_ms",
    "SFS-1s to shared underlying data plane publish latency in milliseconds",
    ["namespace", "symbol"],
)
SFS1S_RING_LEN = Gauge(
    "sfs1s_ring_len", "Redis list length for SFS-1s ring buffer", ["symbol"]
)

_REDIS_LUA_UPSERT_HEAD = r"""
-- Upsert newest 1s bar into LIST without duplicates/out-of-order.
-- If incoming bar ts is older than current head ts, ignore it.
-- If ts equals head ts, replace head (LSET 0).
-- Else LPUSH as newest, then LTRIM and EXPIRE.
--
-- KEYS[1] = list key
-- ARGV[1] = bar json
-- ARGV[2] = bar ts (ms)
-- ARGV[3] = max_len
-- ARGV[4] = ttl_sec

local key = KEYS[1]
local val = ARGV[1]
local new_ts = tonumber(ARGV[2])
local max_len = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local head = redis.call('LINDEX', key, 0)
if head then
  local ok, obj = pcall(cjson.decode, head)
  if ok and obj and obj['ts'] then
    local head_ts = tonumber(obj['ts'])
    if head_ts and head_ts > new_ts then
      redis.call('EXPIRE', key, ttl)
      return 0
    end
    if head_ts and head_ts == new_ts then
      redis.call('LSET', key, 0, val)
      redis.call('EXPIRE', key, ttl)
      return 1
    end
  end
end

redis.call('LPUSH', key, val)
redis.call('LTRIM', key, 0, max_len - 1)
redis.call('EXPIRE', key, ttl)
return 2
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _floor_1s(ms: int) -> int:
    return (ms // 1000) * 1000


@dataclass
class SecBucket:
    symbol: str
    ts_start_ms: int
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: float = 0.0
    trades: int = 0
    last_update_ms: int = 0

    def on_trade(self, px: float, size: float, ts_ms: int) -> None:
        if px is None:
            return
        if size is None or size < 0:
            size = 0.0
        if self.open is None:
            self.open = px
        self.high = px if self.high is None else max(self.high, px)
        self.low = px if self.low is None else min(self.low, px)
        self.close = px
        if size > 0:
            self.volume += float(size)
        self.trades += 1
        self.last_update_ms = ts_ms

    def to_json(self) -> str:
        o = {
            "ts": int(self.ts_start_ms),
            "open": float(self.open or 0.0),
            "high": float(self.high or 0.0),
            "low": float(self.low or 0.0),
            "close": float(self.close or 0.0),
            "volume": float(self.volume),
            "trades": int(self.trades),
        }
        return json.dumps(o, separators=(",", ":"))


class SFS1sService:
    def __init__(self,
                 *,
                 kafka_bootstrap: Optional[str | list] = None,
                 redis_url: Optional[str] = None,
                 ttl_sec: int = 3600,
                 max_len: Optional[int] = None,
                 flush_delay_ms: int = 250,
                 group_id: str = "sfs_1s_agg",
                 topic: str = "md.tick.trade.v1",
                 rth_only: bool = False,
                 trading_hours: Optional[dict] = None) -> None:
        kc = config.kafka_config
        self.bootstrap = kafka_bootstrap or kc.get("bootstrap_servers")
        sfs_cfg = config.get("services.sfs_1s", {}) or {}
        self._redis_url = redis_url or config.redis_config
        self._redis_cmd_timeout_sec = float(sfs_cfg.get("redis_cmd_timeout_sec", 1.5))
        self._redis_lock = asyncio.Lock()
        self._redis_kwargs = dict(
            decode_responses=True,
            socket_timeout=float(sfs_cfg.get("redis_socket_timeout_sec", 1.5)),
            socket_connect_timeout=float(sfs_cfg.get("redis_socket_connect_timeout_sec", 1.5)),
            retry_on_timeout=True,
            health_check_interval=int(sfs_cfg.get("redis_health_check_interval_sec", 30)),
        )
        self.redis = aioredis.from_url(self._redis_url, **self._redis_kwargs)
        self._flush_script = self.redis.register_script(_REDIS_LUA_UPSERT_HEAD)
        self.ttl_sec = int(ttl_sec)
        self.max_len = int(max_len or ttl_sec)
        self.flush_delay_ms = int(flush_delay_ms)
        self.group_id = group_id
        self.topic = topic
        self.rth_only = bool(rth_only)
        self._rth_window = self._parse_trading_hours(trading_hours or config.get("trading_hours", {}) or {})
        self._tz = ZoneInfo("America/New_York")

        self.consumer: Optional[AIOKafkaConsumer] = None
        self._buckets: Dict[str, SecBucket] = {}
        self._flush_queue: Deque[SecBucket] = deque()
        self._last_tick_ts_us: Dict[str, int] = {}
        self._running = False
        self._flusher_task: Optional[asyncio.Task] = None
        self._started_mono: float = 0.0
        self._last_msg_mono: float = 0.0
        self._last_flush_mono: float = 0.0

        # Redis key namespace
        self.ns = "sfs:1s"
        self.shared_underlying_plane_enabled = bool(sfs_cfg.get("shared_underlying_plane_publish_enabled", True))
        self.shared_underlying_namespace = str(sfs_cfg.get("shared_underlying_namespace", "global") or "global").strip().lower()
        self._startup_ready = asyncio.Event()
        # Redis error tracking / circuit breaker
        self._redis_error_streak: int = 0
        self._redis_disabled: bool = False
        self._redis_retry_at_ms: int = 0
        self._redis_breaker_threshold: int = int(sfs_cfg.get("redis_breaker_threshold", 5))
        self._redis_breaker_base_sec: float = float(sfs_cfg.get("redis_breaker_base_sec", 2.0))
        self._redis_breaker_max_sec: float = float(sfs_cfg.get("redis_breaker_max_sec", 30.0))

    def _build_consumer(self) -> AIOKafkaConsumer:
        return AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap,
            group_id=self.group_id,
            enable_auto_commit=True,
            auto_offset_reset="latest",
            value_deserializer=lambda b: json.loads(b.decode("utf-8")),
            key_deserializer=lambda b: b.decode("utf-8") if b else "",
            session_timeout_ms=60000,  # 60s session timeout (increased for stability)
            heartbeat_interval_ms=6000,  # Send heartbeat every 6s
            request_timeout_ms=60000,  # 60s request timeout
            max_poll_interval_ms=300000,  # 5 min max poll interval
            connections_max_idle_ms=540000,  # 9 min idle timeout
        )

    async def start(self) -> None:
        self._running = True
        self._startup_ready.clear()
        now_mono = time.monotonic()
        self._started_mono = now_mono
        self._last_msg_mono = now_mono
        self._last_flush_mono = now_mono
        self._flusher_task = asyncio.create_task(self._flush_loop())
        logger.info(
            "SFS-1s service started",
            topic=self.topic,
            ttl_sec=self.ttl_sec,
            max_len=self.max_len,
            group_id=self.group_id,
            rth_only=self.rth_only,
        )

        attempt = 0
        backoff_sec = 1.0
        max_backoff_sec = 60.0
        try:
            while self._running:
                attempt += 1
                delay_sec = 0.0
                consumer = self._build_consumer()
                self.consumer = consumer
                try:
                    await consumer.start()
                    self._startup_ready.set()
                    backoff_sec = 1.0  # reset after a successful start
                    if attempt > 1:
                        logger.info("SFS-1s consumer restarted", attempt=attempt)

                    async for msg in consumer:
                        try:
                            self._on_trade_msg(msg.value)
                        except Exception as e:  # noqa: BLE001
                            logger.error("SFS-1s message handling error", error=str(e))

                    # If the consumer stops unexpectedly while we still want to run, restart it.
                    if self._running:
                        raise ConsumerStoppedError("SFS-1s consumer stopped unexpectedly")

                except asyncio.CancelledError:
                    break

                except (KafkaError, KafkaConnectionError, CommitFailedError, ConsumerStoppedError) as exc:
                    if not self._running:
                        break
                    delay_sec = backoff_sec
                    logger.warning(
                        "Kafka SFS-1s consumer error, restarting",
                        attempt=attempt,
                        error=str(exc),
                        retry_in_sec=delay_sec,
                    )
                    backoff_sec = min(backoff_sec * 2, max_backoff_sec)

                except Exception as exc:
                    if not self._running:
                        break
                    delay_sec = backoff_sec
                    logger.error(
                        "SFS-1s consumer error (unknown), restarting",
                        attempt=attempt,
                        error=str(exc),
                        exc_info=True,
                        retry_in_sec=delay_sec,
                    )
                    backoff_sec = min(backoff_sec * 2, max_backoff_sec)

                finally:
                    try:
                        await asyncio.wait_for(consumer.stop(), timeout=5.0)
                    except Exception:
                        pass
                    if self.consumer is consumer:
                        self.consumer = None
                    self._startup_ready.clear()

                if self._running and delay_sec > 0:
                    await asyncio.sleep(delay_sec)

        finally:
            await self.stop()

    async def stop(self) -> None:
        self._running = False
        self._startup_ready.clear()

        # Stop flusher loop first to avoid concurrent Redis writes during shutdown.
        if self._flusher_task:
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except asyncio.CancelledError:
                pass

        # Best-effort final flush of remaining buckets.
        try:
            self._redis_disabled = False
            self._redis_error_streak = 0
            await self._flush_all(force=True)
        except Exception:
            pass

        if self.consumer:
            try:
                await asyncio.wait_for(self.consumer.stop(), timeout=10.0)
            except Exception:
                pass
            self.consumer = None
        try:
            async with self._redis_lock:
                await self.redis.close()
        except Exception:
            pass
        logger.info("SFS-1s service stopped")

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

    def _on_trade_msg(self, value: dict) -> None:
        # Support both flat schema and envelope {metadata, data}
        payload = value.get("data") if isinstance(value, dict) and "data" in value else value
        if not isinstance(payload, dict):
            return
        symbol = payload.get("symbol") or payload.get("sym")
        if not symbol:
            return

        px = payload.get("price")
        if px is None:
            px = payload.get("px")
        if px is None:
            px = payload.get("last")

        size = payload.get("size")
        if size is None:
            size = payload.get("last_size")

        ts_us = payload.get("ts_us")
        if ts_us is None:
            ts_us = payload.get("timestamp_us")
        if ts_us is None:
            ts_us = payload.get("tsus")

        ts_us_i = 0
        if ts_us is not None:
            try:
                ts_us_i = int(ts_us)
            except Exception:
                ts_us_i = 0

        ts_ms = payload.get("ts_ms")
        if ts_ms is None:
            ts_ms = int(ts_us_i // 1000) if ts_us_i > 0 else _now_ms()
        else:
            try:
                ts_ms = int(ts_ms)
            except Exception:
                ts_ms = _now_ms()

        # Drop out-of-order/duplicate ticks per-symbol (Kafka key is symbol, so ordering is stable per symbol).
        if ts_us_i > 0:
            last_us = self._last_tick_ts_us.get(symbol, 0)
            if ts_us_i <= last_us:
                return
            self._last_tick_ts_us[symbol] = ts_us_i

        if px is None:
            return

        # Equities should have positive prices; internals (e.g. AD-NASD) can be <= 0.
        try:
            px_f = float(px)
        except Exception:
            return
        is_internal = "-" in symbol
        if (not is_internal) and px_f <= 0:
            return

        try:
            size_f = float(size) if size is not None else 0.0
        except Exception:
            size_f = 0.0

        if self.rth_only and not self._is_rth(int(ts_ms)):
            return

        self._last_msg_mono = time.monotonic()
        sec = _floor_1s(int(ts_ms))
        b = self._buckets.get(symbol)
        if b is None or b.ts_start_ms != sec:
            # Move old bucket to flush queue (do not drop it on Redis outages).
            if b is not None:
                self._flush_queue.append(b)
            b = SecBucket(symbol=symbol, ts_start_ms=sec)
            self._buckets[symbol] = b
        b.on_trade(float(px_f), float(size_f), int(ts_ms))
        try:
            SFS1S_TICKS_TOTAL.labels(symbol=symbol).inc()
        except Exception:
            pass

    async def _reset_redis_client(self) -> None:
        async with self._redis_lock:
            try:
                await self.redis.close()
            except Exception:
                pass
            self.redis = aioredis.from_url(self._redis_url, **self._redis_kwargs)
            self._flush_script = self.redis.register_script(_REDIS_LUA_UPSERT_HEAD)

    async def _flush_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.2)
                await self._flush_all()
        except asyncio.CancelledError:
            pass

    async def _flush_all(self, force: bool = False) -> None:
        # If Redis is in a degraded state, skip flushes until retry_at.
        now_ms = _now_ms()
        if self._redis_disabled and now_ms < self._redis_retry_at_ms and not force:
            return
        if self._redis_disabled and now_ms >= self._redis_retry_at_ms:
            # Allow writes again; next error will re-trip breaker.
            self._redis_disabled = False
            self._redis_error_streak = 0
            try:
                await self._reset_redis_client()
            except Exception as exc:  # noqa: BLE001
                logger.warning("SFS-1s Redis reset failed", error=str(exc))

        # Move ready buckets into flush queue.
        for sym, b in list(self._buckets.items()):
            overdue = (now_ms > (b.ts_start_ms + 1000 + self.flush_delay_ms))
            if force or (b.trades > 0 and overdue):
                self._flush_queue.append(b)
                self._buckets.pop(sym, None)

        # Flush queued buckets sequentially (preserve data in-memory until a write succeeds).
        n = len(self._flush_queue)
        for _ in range(n):
            if not self._flush_queue:
                break
            if self._redis_disabled and not force:
                break
            b = self._flush_queue[0]
            ok = await self._flush_bucket_async(b)
            if ok:
                self._flush_queue.popleft()
            else:
                # On failure, keep the bucket for retry and avoid hot-looping.
                break

    async def _flush_bucket_async(self, b: SecBucket) -> bool:
        key = f"{self.ns}:{b.symbol}"
        val = b.to_json()
        try:
            if self._redis_disabled:
                return False
            start_ms = _now_ms()
            async with self._redis_lock:
                await asyncio.wait_for(
                    self._flush_script(
                        keys=[key],
                        args=[val, int(b.ts_start_ms), int(self.max_len), int(self.ttl_sec)],
                    ),
                    timeout=self._redis_cmd_timeout_sec,
                )
            # Update metrics
            try:
                SFS1S_BARS_FLUSHED_TOTAL.labels(symbol=b.symbol).inc()
                SFS1S_FLUSH_LATENCY_MS.observe(float(_now_ms() - start_ms))
            except Exception:
                pass
            # Reset error streak on success
            self._redis_error_streak = 0
            self._last_flush_mono = time.monotonic()
            self._publish_shared_underlying_bucket(b)
            return True
        except asyncio.TimeoutError as e:
            logger.error("SFS-1s Redis write timed out", error=str(e), symbol=b.symbol)
            try:
                SFS1S_REDIS_ERRORS_TOTAL.inc()
            except Exception:
                pass
            self._redis_error_streak += 1
            if self._redis_error_streak >= self._redis_breaker_threshold and not self._redis_disabled:
                extra = max(0, self._redis_error_streak - self._redis_breaker_threshold)
                backoff_sec = min(self._redis_breaker_max_sec, self._redis_breaker_base_sec * (2 ** extra))
                self._redis_disabled = True
                self._redis_retry_at_ms = _now_ms() + int(backoff_sec * 1000)
                logger.warning(
                    "SFS-1s Redis temporarily disabled due to repeated timeouts",
                    errors=self._redis_error_streak,
                    retry_in_sec=backoff_sec,
                )
            return False

    def _publish_shared_underlying_bucket(self, b: SecBucket) -> None:
        if not self.shared_underlying_plane_enabled:
            return
        start_ms = _now_ms()
        symbol = str(b.symbol or "").strip().upper()
        try:
            shared_underlying_data_plane().publish_underlying(
                symbol=symbol,
                namespace=self.shared_underlying_namespace,
                ts_epoch_s=float(b.ts_start_ms) / 1000.0,
                last_price=float(b.close or 0.0),
                vwap=float(b.close or 0.0),
                features={
                    "close": float(b.close or 0.0),
                    "last_price": float(b.close or 0.0),
                    "volume": float(b.volume or 0.0),
                    "trades": int(b.trades or 0),
                },
                bar={
                    "ts_epoch_s": float(b.ts_start_ms) / 1000.0,
                    "open": float(b.open or 0.0),
                    "high": float(b.high or 0.0),
                    "low": float(b.low or 0.0),
                    "close": float(b.close or 0.0),
                    "volume": float(b.volume or 0.0),
                    "vwap": float(b.close or 0.0),
                },
            )
            try:
                SHARED_PLANE_PUBLISH_LATENCY_MS.labels(
                    namespace=self.shared_underlying_namespace,
                    symbol=symbol,
                ).observe(float(_now_ms() - start_ms))
            except Exception:
                pass
        except Exception:
            logger.debug("sfs_1s_shared_underlying_publish_failed", symbol=b.symbol, exc_info=True)
        except Exception as e:  # noqa: BLE001
            logger.error("SFS-1s Redis write failed", error=str(e), symbol=b.symbol)
            try:
                SFS1S_REDIS_ERRORS_TOTAL.inc()
            except Exception:
                pass
            # Simple circuit breaker: after a few consecutive errors, pause Redis writes
            self._redis_error_streak += 1
            if self._redis_error_streak >= self._redis_breaker_threshold and not self._redis_disabled:
                extra = max(0, self._redis_error_streak - self._redis_breaker_threshold)
                backoff_sec = min(self._redis_breaker_max_sec, self._redis_breaker_base_sec * (2 ** extra))
                self._redis_disabled = True
                self._redis_retry_at_ms = _now_ms() + int(backoff_sec * 1000)
                logger.warning(
                    "SFS-1s Redis temporarily disabled due to repeated errors",
                    errors=self._redis_error_streak,
                    retry_in_sec=backoff_sec,
                )
            return False

    def health_snapshot(self) -> Dict[str, float | bool | int]:
        now_mono = time.monotonic()
        latest_activity_mono = max(
            float(self._started_mono or 0.0),
            float(self._last_msg_mono or 0.0),
            float(self._last_flush_mono or 0.0),
        )
        return {
            "running": bool(self._running),
            "started_age_sec": max(0.0, now_mono - float(self._started_mono or now_mono)),
            "last_msg_age_sec": max(0.0, now_mono - float(self._last_msg_mono or self._started_mono or now_mono)),
            "last_flush_age_sec": max(0.0, now_mono - float(self._last_flush_mono or self._started_mono or now_mono)),
            "latest_activity_age_sec": max(0.0, now_mono - latest_activity_mono),
            "queued_buckets": int(len(self._flush_queue)),
            "open_buckets": int(len(self._buckets)),
            "redis_disabled": bool(self._redis_disabled),
            "redis_error_streak": int(self._redis_error_streak),
        }

    def is_stalled(self, *, max_idle_sec: float, startup_grace_sec: float = 30.0) -> bool:
        if not self._running:
            return False
        snap = self.health_snapshot()
        if float(snap["started_age_sec"]) < max(1.0, float(startup_grace_sec)):
            return False
        if bool(snap["redis_disabled"]):
            return True
        return float(snap["latest_activity_age_sec"]) >= max(1.0, float(max_idle_sec))


async def _main() -> None:
    import os
    ttl_sec = int(os.getenv("SFS_1S_TTL_SEC", "3600"))
    max_len = int(os.getenv("SFS_1S_MAX_LEN", "0") or 0) or ttl_sec
    flush_delay_ms = int(os.getenv("SFS_1S_FLUSH_DELAY_MS", "250"))
    group_id = os.getenv("SFS_1S_GROUP_ID", "sfs_1s_agg")

    svc = SFS1sService(
        ttl_sec=ttl_sec,
        max_len=max_len,
        flush_delay_ms=flush_delay_ms,
        group_id=group_id,
    )

    loop = asyncio.get_running_loop()
    stop_ev = asyncio.Event()

    def _sig(*_):
        stop_ev.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _sig)
        except NotImplementedError:
            pass

    runner = asyncio.create_task(svc.start())
    await stop_ev.wait()
    await svc.stop()
    await runner


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
