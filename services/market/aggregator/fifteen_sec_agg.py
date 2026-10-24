"""
15-Second Bar Aggregator with Gap Detection

Features:
- Tick ingestion (last/size) → 15-second bars
- Late arrival handling with watermark
- Gap detection (SEQ_GAP / TIME_GAP)
- Redis state persistence (watermark / next-seq / emit state)
- Kafka output:
    - Bars: md.bar.15s.v2
    - Gaps: md.gap.v1
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Dict, Optional, List

from ib_insync import IB, Stock
from aiokafka import AIOKafkaProducer
import redis.asyncio as aioredis

from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)

def now_us() -> int:
    return int(time.time() * 1e6)

def floor_15s(ts_ms: int) -> int:
    return (ts_ms // 15000) * 15000

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

def is_rth(ts_epoch_sec: float) -> bool:
    """
    RTH（纽约时区）：
    周一~周五，09:30:00 <= t < 16:00:00
    """
    dt = datetime.fromtimestamp(ts_epoch_sec, tz=NY)
    if dt.weekday() > 4:  # 5/6 = Sat/Sun
        return False
    t = dt.time()
    return dtime(9, 30) <= t < dtime(16, 0)

GAP_TOPIC = "md.gap.v1"
BAR_TOPIC = "md.bar.15s.v2"
AGG15S_REDIS_NS = "agg15s"

@dataclass
class BarBucket:
    symbol: str
    ts_start_ms: int
    ts_end_ms: int
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: float = 0.0
    vwap_sum_pv: float = 0.0
    vwap_sum_v: float = 0.0
    trades: int = 0
    late_count: int = 0
    bar_seq: int = 0

    def on_trade(self, px: float, size: float):
        if px is None or size is None or size <= 0:
            return
        if self.open is None:
            self.open = px
        self.high = px if self.high is None else max(self.high, px)
        self.low = px if self.low is None else min(self.low, px)
        self.close = px
        self.volume += size
        self.vwap_sum_pv += px * size
        self.vwap_sum_v += size
        self.trades += 1

    def to_bar_message(self) -> dict:
        vwap = (self.vwap_sum_pv / self.vwap_sum_v) if self.vwap_sum_v > 0 else (self.close or self.open or 0.0)
        return {
            "schema_v": 2,
            "metadata": {
                "ts_us": now_us(),
                "producer": "agg_15s"
            },
            "data": {
                "symbol": self.symbol,
                "ts_start_us": self.ts_start_ms * 1000,
                "ts_end_us": self.ts_end_ms * 1000,
                "open": round(self.open or 0.0, 4),
                "high": round(self.high or 0.0, 4),
                "low": round(self.low or 0.0, 4),
                "close": round(self.close or 0.0, 4),
                "volume": self.volume,
                "vwap": round(vwap, 4),
                "trades": self.trades,
                "quality": "REPAIRED" if self.late_count > 0 else "GOOD"
            }
        }

class FifteenSecondAggregator:
    def __init__(
        self,
        symbols: List[str],
        emit_buffer_ms: int = 300,
        max_late_ms: int = 2000,
        redis_url: Optional[str] = None,
        kafka_bootstrap: Optional[str] = None
    ):
        self.symbols = symbols
        self.emit_buffer_ms = emit_buffer_ms
        self.max_late_ms = max_late_ms

        self.bar_ms = 15000
        self.buckets: Dict[tuple, BarBucket] = {}
        self.emit_schedule: Dict[tuple, int] = {}   # (symbol, ts_start_ms) -> emit_time_ms
        self.watermark_ms: Dict[str, int] = {}      # symbol -> last emitted ts_end_ms
        self.last_bar_seq: Dict[str, int] = {}      # symbol -> next sequence to assign on creation
        self.last_emitted_seq: Dict[str, int] = {}  # symbol -> last emitted bar_seq
        self.last_emitted_ts: Dict[str, int] = {}   # symbol -> last emitted ts_start_ms

        self.redis_ns = AGG15S_REDIS_NS
        redis_conn = redis_url or getattr(config, "redis_url", None) or getattr(config, "redis_config", None)
        self.redis = aioredis.from_url(redis_conn, decode_responses=True)

        kcfg = config.kafka_config
        self._kafka_bootstrap = kafka_bootstrap or kcfg["bootstrap_servers"]
        self._producer_kwargs = {
            "bootstrap_servers": self._kafka_bootstrap,
            "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
        }
        self.producer = self._build_producer()
        self._producer_restart_lock = asyncio.Lock()
        self._producer_last_restart_ms = 0
        self._producer_restart_backoff_sec = float(kcfg.get("producer_restart_backoff_sec", 1.0))
        self._producer_restart_min_interval_ms = int(kcfg.get("producer_restart_min_interval_ms", 2000))
        self._emit_retry_delay_ms = int(kcfg.get("emit_retry_delay_ms", 1000))
        self._emit_fail_streak = 0
        self._running = False

    async def start(self):
        await self.producer.start()
        await self._restore_state()
        self._running = True
        asyncio.create_task(self._flusher_loop())
        logger.info("FifteenSecondAggregator started", symbols=self.symbols)

    async def stop(self):
        self._running = False
        try:
            await asyncio.wait_for(self.producer.stop(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("15s aggregator producer stop timed out (degraded Kafka), forcing close")
        except Exception as e:
            logger.warning("15s aggregator producer stop failed", error=str(e))
        finally:
            try:
                await self.redis.close()
            except Exception:
                pass
        logger.info("FifteenSecondAggregator stopped")

    async def _on_tickers(self, tickers):
        asyncio.create_task(self._ingest_tickers(tickers))

    async def _ingest_tickers(self, tickers):
        now_ms = int(time.time() * 1000)
        for ticker in tickers:
            symbol = ticker.contract.symbol
            px = float(ticker.last) if ticker.last and ticker.last > 0 else None
            size = float(ticker.lastSize) if ticker.lastSize and ticker.lastSize > 0 else None
            if px is None or size is None or size <= 0:
                continue

            ts_ms = now_ms  # IB does not provide precise trade time
            win_start = floor_15s(ts_ms)
            win_end = win_start + self.bar_ms
            key = (symbol, win_start)

            bucket = self.buckets.get(key)
            if not bucket:
                seq = self.last_bar_seq.get(symbol, 0) + 1
                self.last_bar_seq[symbol] = seq
                bucket = BarBucket(symbol=symbol, ts_start_ms=win_start, ts_end_ms=win_end, bar_seq=seq)
                self.buckets[key] = bucket
                self.emit_schedule[key] = win_end + self.emit_buffer_ms

            bucket.on_trade(px, size)

            watermark = self.watermark_ms.get(symbol, 0)
            if win_start < watermark:
                bucket.late_count += 1

    async def _flusher_loop(self):
        while self._running:
            try:
                now_ms = int(time.time() * 1000)
                ready_keys = [k for k, t in self.emit_schedule.items() if t <= now_ms]

                for key in ready_keys:
                    bucket = self.buckets.pop(key, None)
                    if not bucket:
                        continue

                    # Emit bar first; on failure requeue for retry to avoid data loss.
                    emitted = await self._emit_bar(bucket)
                    if not emitted:
                        self.buckets[key] = bucket
                        self.emit_schedule[key] = now_ms + self._emit_retry_delay_ms
                        logger.warning(
                            "15s bar emit deferred for retry",
                            symbol=bucket.symbol,
                            ts_start=bucket.ts_start_ms,
                            retry_after_ms=self._emit_retry_delay_ms,
                        )
                        continue

                    # Gap detection after successful bar emit
                    await self._check_and_emit_gap(bucket)
                    del self.emit_schedule[key]

                    # Update state
                    self.last_emitted_seq[bucket.symbol] = bucket.bar_seq
                    self.last_emitted_ts[bucket.symbol] = bucket.ts_start_ms
                    self.watermark_ms[bucket.symbol] = bucket.ts_end_ms

                    await self._save_state(bucket.symbol)

            except Exception as e:
                logger.error("Flusher loop error", error=str(e))

            await asyncio.sleep(0.1)

    async def _emit_bar(self, bucket: BarBucket) -> bool:
        try:
            ok = await self._send_with_recovery(topic=BAR_TOPIC, key=bucket.symbol, value=bucket.to_bar_message())
            if not ok:
                self._emit_fail_streak += 1
                if self._emit_fail_streak == 1 or self._emit_fail_streak % 10 == 0:
                    logger.error(
                        "Failed to emit 15s bar",
                        error="send_failed_after_recovery",
                        symbol=bucket.symbol,
                        failure_streak=self._emit_fail_streak,
                    )
                return False
            self._emit_fail_streak = 0
            logger.debug("15s bar emitted", symbol=bucket.symbol, ts_start=bucket.ts_start_ms,
                         trades=bucket.trades, volume=bucket.volume)
            return True
        except Exception as e:
            logger.error("Failed to emit 15s bar", error=str(e), symbol=bucket.symbol)
            return False

    async def _check_and_emit_gap(self, bucket: BarBucket):
        symbol = bucket.symbol
        if not is_rth(bucket.ts_end_ms / 1000.0):
            return

        expected_seq = self.last_emitted_seq.get(symbol, 0) + 1
        if bucket.bar_seq != expected_seq:
            await self._emit_gap(symbol, "SEQ_GAP",
                                 expected_seq=expected_seq,
                                 got_seq=bucket.bar_seq,
                                 expected_start_ms=self.last_emitted_ts.get(symbol, None) + self.bar_ms
                                    if symbol in self.last_emitted_ts else None,
                                 actual_start_ms=bucket.ts_start_ms)

        if symbol in self.last_emitted_ts:
            expected_start_ms = self.last_emitted_ts[symbol] + self.bar_ms
            if bucket.ts_start_ms > expected_start_ms:
                await self._emit_gap(symbol, "TIME_GAP",
                                     expected_seq=expected_seq, got_seq=bucket.bar_seq,
                                     expected_start_ms=expected_start_ms,
                                     actual_start_ms=bucket.ts_start_ms)

    async def _emit_gap(self, symbol: str, reason: str,
                        expected_seq: int = None, got_seq: int = None,
                        expected_start_ms: int = None, actual_start_ms: int = None):
        try:
            msg = {
                "schema_v": 1,
                "metadata": {"ts_us": now_us(), "producer": "agg_15s"},
                "data": {
                    "symbol": symbol,
                    "bar_sec": 15,
                    "reason": reason,
                    "from_ts": (expected_start_ms // 1000) if expected_start_ms is not None else None,
                    "to_ts": (actual_start_ms // 1000) if actual_start_ms is not None else None,
                    "expected_seq": expected_seq,
                    "got_seq": got_seq,
                    "expected_ts_start_us": (expected_start_ms * 1000) if expected_start_ms is not None else None,
                    "actual_ts_start_us": (actual_start_ms * 1000) if actual_start_ms is not None else None,
                }
            }
            ok = await self._send_with_recovery(topic=GAP_TOPIC, key=symbol, value=msg)
            if not ok:
                logger.warning("Failed to emit 15s gap event after recovery", symbol=symbol, reason=reason)
                return
            # Lowered to debug to reduce log noise; event still published to GAP topic.
            logger.debug("15s GAP event emitted", symbol=symbol, reason=reason,
                         expected_seq=expected_seq, got_seq=got_seq,
                         expected_ms=expected_start_ms, actual_ms=actual_start_ms)
        except Exception as e:
            logger.error("Failed to emit 15s gap event", error=str(e), symbol=symbol)

    def _build_producer(self) -> AIOKafkaProducer:
        return AIOKafkaProducer(**self._producer_kwargs)

    async def _send_with_recovery(self, *, topic: str, key: str, value: dict) -> bool:
        try:
            await self.producer.send(topic=topic, key=key, value=value)
            return True
        except Exception as exc:
            if not self._running:
                return False
            logger.warning("15s kafka send failed, trying producer recovery", topic=topic, key=key, error=str(exc))
            recovered = await self._recover_producer()
            if not recovered:
                return False
            try:
                await self.producer.send(topic=topic, key=key, value=value)
                logger.info("15s kafka send recovered after producer restart", topic=topic, key=key)
                return True
            except Exception as retry_exc:
                logger.warning("15s kafka send failed after producer recovery", topic=topic, key=key, error=str(retry_exc))
                return False

    async def _recover_producer(self) -> bool:
        if not self._running:
            return False
        async with self._producer_restart_lock:
            if not self._running:
                return False
            now_ms = int(time.time() * 1000)
            wait_ms = self._producer_restart_min_interval_ms - (now_ms - self._producer_last_restart_ms)
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000.0)

            old = self.producer
            self.producer = self._build_producer()
            try:
                if old is not None:
                    try:
                        await asyncio.wait_for(old.stop(), timeout=3.0)
                    except Exception:
                        pass
                if self._producer_restart_backoff_sec > 0:
                    await asyncio.sleep(self._producer_restart_backoff_sec)
                await asyncio.wait_for(self.producer.start(), timeout=10.0)
                self._producer_last_restart_ms = int(time.time() * 1000)
                logger.info("15s kafka producer recovered", producer_id=id(self.producer))
                return True
            except Exception as exc:
                logger.warning("15s kafka producer recovery failed", error=str(exc))
                return False

    async def _restore_state(self):
        try:
            for symbol in self.symbols:
                wkey = f"{self.redis_ns}:watermark:{symbol}"
                wm = await self.redis.get(wkey)
                if wm: self.watermark_ms[symbol] = int(wm)

                next_seq_key = f"{self.redis_ns}:seq:{symbol}"
                ns = await self.redis.get(next_seq_key)
                if ns: self.last_bar_seq[symbol] = int(ns)

                emit_seq_key = f"{self.redis_ns}:emit_seq:{symbol}"
                es = await self.redis.get(emit_seq_key)
                if es:
                    self.last_emitted_seq[symbol] = int(es)
                elif ns:
                    # First run with new logic: initialize emit_seq from next_seq
                    initial_seq = int(ns) - 1
                    self.last_emitted_seq[symbol] = initial_seq
                    logger.info("Initialized 15s last_emitted_seq from existing state", symbol=symbol, seq=initial_seq)

                emit_ts_key = f"{self.redis_ns}:emit_ts:{symbol}"
                ets = await self.redis.get(emit_ts_key)
                if ets: self.last_emitted_ts[symbol] = int(ets)

            logger.info("15s state restored from Redis")
        except Exception as e:
            logger.error("Failed to restore 15s state", error=str(e))

    async def _save_state(self, symbol: str):
        try:
            pipe = self.redis.pipeline()
            pipe.set(f"{self.redis_ns}:watermark:{symbol}", self.watermark_ms.get(symbol, 0))
            pipe.set(f"{self.redis_ns}:seq:{symbol}", self.last_bar_seq.get(symbol, 0))
            pipe.set(f"{self.redis_ns}:emit_seq:{symbol}", self.last_emitted_seq.get(symbol, 0))
            pipe.set(f"{self.redis_ns}:emit_ts:{symbol}", self.last_emitted_ts.get(symbol, 0))
            await pipe.execute()
        except Exception as e:
            logger.error("Failed to save 15s state", error=str(e), symbol=symbol)
