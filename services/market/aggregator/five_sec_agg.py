# services/market/aggregator/five_sec_agg.py
"""
5-Second Bar Aggregator (RTH filtering + Gap detection + Redis/Kafka state)

功能
----
- 接收 IBKR tick (last/lastSize) → 5 秒 OHLCV（含简易 VWAP，trades 计数）
- 对扩展时段缺失 lastSize 的快照，允许仅用 last 更新 OHLC，volume 保持 0
- 源头 RTH 过滤（America/New_York：周一~周五 09:30~16:00；可通过 include_ah 开关包含盘前/盘后）
- 迟到处理：窗口结束后延迟 emit_buffer_ms 再发射
- 缺口检测（仅 RTH 内，以避免夜盘/周末误报）：
    * SEQ_GAP：bar 序列号不连续
    * TIME_GAP：窗口起始跳变（跳过 ≥1 个窗口）
- Redis 状态持久化：watermark / last_bar_seq / last_emitted_seq / last_emitted_ts
- Kafka 输出：
    * Bars: md.bar.5s.v2
    * Gaps: md.gap.v1（含 from_ts / to_ts，单位秒，供 BackfillService 使用）

依赖
----
- aiokafka
- redis.asyncio
- zoneinfo
- libs.logger / libs.config

注意
----
- 本文件不生成空 bar（无成交则无 bar）
- vwap 采用成交额/成交量，若无则回退 close/open
"""

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, List, Tuple

from aiokafka import AIOKafkaProducer
import redis.asyncio as aioredis

from zoneinfo import ZoneInfo
from datetime import datetime, time as dtime

from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)

# ---------------------- Time helpers ----------------------

NY = ZoneInfo("America/New_York")


def now_us() -> int:
    return int(time.time() * 1e6)


def floor_5s(ts_ms: int) -> int:
    """Floor timestamp to 5-second boundary (ms)."""
    return (ts_ms // 5000) * 5000


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


# ---------------------- Topics & Namespaces ----------------------

BAR_TOPIC = "md.bar.5s.v2"
GAP_TOPIC = "md.gap.v1"
AGG5S_REDIS_NS = "agg5s"


# ---------------------- Data Model ----------------------

@dataclass
class BarBucket:
    symbol: str
    ts_start_ms: int
    ts_end_ms: int
    bar_seq: int

    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: float = 0.0

    vwap_sum_pv: float = 0.0
    vwap_sum_v: float = 0.0

    trades: int = 0
    late_count: int = 0  # 迟到 tick 计数

    def on_trade(self, px: float, size: float):
        if px is None or size is None or size < 0:
            return
        if self.open is None:
            self.open = px
        self.high = px if self.high is None else max(self.high, px)
        self.low = px if self.low is None else min(self.low, px)
        self.close = px
        if size > 0:
            self.volume += size
            self.vwap_sum_pv += px * size
            self.vwap_sum_v += size
        self.trades += 1

    def to_bar_message(self) -> dict:
        # vwap：若 v=0，则回退 close/open
        vwap = (self.vwap_sum_pv / self.vwap_sum_v) if self.vwap_sum_v > 0 else (self.close or self.open or 0.0)
        return {
            "schema_v": 2,
            "metadata": {
                "ts_us": now_us(),
                "producer": "agg_5s"
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
                "quality": "REPAIRED" if self.late_count > 0 else "GOOD",
            }
        }


# ---------------------- Aggregator ----------------------

class FiveSecondAggregator:
    def __init__(
        self,
        symbols: List[str],
        *,
        emit_buffer_ms: int = 300,
        max_late_ms: int = 2000,
        include_ah: bool = False,
        redis_url: Optional[str] = None,
        kafka_bootstrap: Optional[str] = None,
        enable_trade_sanity_filter: bool = False,
        trade_max_deviation_from_quote_pct: float = 0.02,
        trade_max_jump_from_last_pct: float = 0.05,
        enable_bar_sanity_filter: bool = False,
        bar_max_deviation_pct: float = 0.35,
        bar_max_range_pct: float = 0.20,
        readiness_callback: Optional[Callable[..., None]] = None,
    ):
        """
        :param symbols: 订阅标的
        :param emit_buffer_ms: 窗口结束后延迟多少 ms 再发射（容忍轻微迟到）
        :param max_late_ms: 迟到阈值（预留参数，可用于丢弃过晚 tick）
        :param include_ah: 是否包含盘前/盘后 tick（默认 False）
        :param redis_url: Redis 连接串（默认从 config）
        :param kafka_bootstrap: Kafka bootstrap servers（默认从 config）
        """
        self.symbols = symbols
        self.emit_buffer_ms = int(emit_buffer_ms)
        self.max_late_ms = int(max_late_ms)
        self.include_ah = include_ah
        self.enable_trade_sanity_filter = bool(enable_trade_sanity_filter)
        self.trade_max_deviation_from_quote_pct = float(trade_max_deviation_from_quote_pct)
        self.trade_max_jump_from_last_pct = float(trade_max_jump_from_last_pct)
        self.enable_bar_sanity_filter = bool(enable_bar_sanity_filter)
        self.bar_max_deviation_pct = float(bar_max_deviation_pct)
        self.bar_max_range_pct = float(bar_max_range_pct)
        self._readiness_callback = readiness_callback

        # 窗口步长
        self.bar_ms = 5000

        # 状态
        self.buckets: Dict[Tuple[str, int], BarBucket] = {}
        self.emit_schedule: Dict[Tuple[str, int], int] = {}  # (symbol, win_start_ms) -> emit_time_ms
        self.watermark_ms: Dict[str, int] = {}               # 已发射最后窗口的 end_ms
        self.last_bar_seq: Dict[str, int] = {}               # 已分配的最后序列号（创建桶时自增）
        self.last_emitted_seq: Dict[str, int] = {}           # 已发射的最后序列号（缺口检测用）
        self.last_emitted_ts: Dict[str, int] = {}            # 已发射的最后窗口 ts_start_ms（缺口检测用）
        self.last_good_trade_px: Dict[str, float] = {}
        self.rejected_trade_counts: Dict[Tuple[str, str], int] = {}
        self.quarantined_bars: List[Dict[str, Any]] = []

        # Redis
        self.redis_ns = AGG5S_REDIS_NS
        self.redis = aioredis.from_url(redis_url or config.redis_config, decode_responses=True)

        # Kafka（内置序列化，避免发送 dict 出错）
        kcfg = getattr(config, "kafka_config", None) or {}
        self._kafka_bootstrap = kafka_bootstrap or kcfg.get("bootstrap_servers")
        self._producer_kwargs = {
            "bootstrap_servers": self._kafka_bootstrap,
            "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
            "key_serializer": lambda v: v.encode("utf-8") if isinstance(v, str) else v,
        }
        self.producer = self._build_producer()
        self._producer_restart_lock = asyncio.Lock()
        self._producer_last_restart_ms = 0
        self._producer_restart_backoff_sec = float(kcfg.get("producer_restart_backoff_sec", 1.0))
        self._producer_restart_min_interval_ms = int(kcfg.get("producer_restart_min_interval_ms", 2000))
        self._emit_retry_delay_ms = int(kcfg.get("emit_retry_delay_ms", 1000))
        self._emit_fail_streak = 0
        self._producer_stop_tasks: set[asyncio.Task] = set()
        self._retired_producers: set[AIOKafkaProducer] = set()
        self._last_successful_bar_publish_ms: Dict[str, int] = {}

        self._running = False
        self._flusher_task: Optional[asyncio.Task] = None

    def _mark_ready(self, ready: bool, *, reason: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if not callable(self._readiness_callback):
            return
        try:
            self._readiness_callback(
                "bar_pipeline",
                ready=bool(ready),
                reason=reason,
                metadata=dict(metadata or {}),
            )
        except Exception:
            pass

    async def _stop_producer_best_effort(
        self,
        producer: Optional[AIOKafkaProducer],
        *,
        timeout: float,
        reason: str,
    ) -> None:
        if producer is None:
            return
        self._retired_producers.add(producer)
        stop_task = asyncio.create_task(producer.stop(), name=f"agg5s_producer_stop_now:{reason}")

        def _release_retired(task: asyncio.Task) -> None:
            self._producer_stop_tasks.discard(task)
            self._retired_producers.discard(producer)

        stop_task.add_done_callback(_release_retired)
        try:
            await asyncio.wait_for(asyncio.shield(stop_task), timeout=timeout)
            self._retired_producers.discard(producer)
        except asyncio.TimeoutError:
            logger.warning("Aggregator producer stop timed out", reason=reason)
            self._producer_stop_tasks.add(stop_task)
        except asyncio.CancelledError:
            stop_task.cancel()
            raise
        except Exception as exc:
            self._retired_producers.discard(producer)
            logger.warning("Aggregator producer stop failed", reason=reason, error=str(exc))

    # -------- lifecycle --------

    async def start(self):
        await self.producer.start()
        await self._restore_state()
        self._running = True
        self._mark_ready(
            False,
            reason="five_second_aggregator_producer_started",
            metadata={"symbols": list(self.symbols)},
        )
        self._flusher_task = asyncio.create_task(self._flusher_loop())
        logger.info("FiveSecondAggregator started", symbols=self.symbols, include_ah=self.include_ah)

    async def stop(self):
        self._running = False
        self._mark_ready(False, reason="five_second_aggregator_stopping")

        # Cancel and await flusher task to avoid zombie background tasks
        if self._flusher_task:
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("Flusher task exception during stop", error=str(e))

        await self._stop_producer_best_effort(
            self.producer,
            timeout=3.0,
            reason="aggregator_stop",
        )
        try:
            await self.redis.close()
        except Exception:
            pass

        if self._producer_stop_tasks:
            done, pending = await asyncio.wait(
                self._producer_stop_tasks,
                timeout=0,
                return_when=asyncio.ALL_COMPLETED,
            )
            self._producer_stop_tasks = set(pending)
            for task in done:
                with contextlib.suppress(Exception):
                    _ = task.result()
        logger.info("FiveSecondAggregator stopped")

    # -------- ingestion entry --------

    def on_tickers(self, tickers):
        """
        上游入口：传入 ib_insync.Ticker 列表或等价对象
        """
        asyncio.create_task(self._ingest_tickers(tickers))

    def on_tick(
        self,
        *,
        symbol: str,
        px: Optional[float],
        size: Optional[float],
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        ts_ms: Optional[int] = None,
    ) -> None:
        """Unified ingest path from normalized TickData.

        This should be the primary path so strategies/RFE and 5s bars share the
        same underlying tick source.
        """
        if not symbol:
            return
        asyncio.create_task(
            self._ingest_trade_update(
                symbol=symbol,
                px=px,
                size=size,
                bid=bid,
                ask=ask,
                ts_ms=int(ts_ms or time.time() * 1000),
            )
        )

    async def _ingest_tickers(self, tickers):
        """接收 tickers 并更新桶"""
        now_ms = int(time.time() * 1000)

        for ticker in tickers:
            try:
                # 1) symbol
                symbol = getattr(getattr(ticker, "contract", None), "symbol", None)
                if not symbol:
                    continue

                last = getattr(ticker, "last", None)
                last_size = getattr(ticker, "lastSize", None)
                bid = getattr(ticker, "bid", None)
                ask = getattr(ticker, "ask", None)
                await self._ingest_trade_update(
                    symbol=symbol,
                    px=float(last) if last is not None and last > 0 else None,
                    size=float(last_size) if last_size is not None and last_size > 0 else None,
                    bid=float(bid) if bid is not None and bid > 0 else None,
                    ask=float(ask) if ask is not None and ask > 0 else None,
                    ts_ms=now_ms,
                )

            except Exception as e:
                logger.error("ingest tick error", error=str(e))

    async def _ingest_trade_update(
        self,
        *,
        symbol: str,
        px: Optional[float],
        size: Optional[float],
        bid: Optional[float],
        ask: Optional[float],
        ts_ms: int,
    ) -> None:
        in_rth_now = is_rth(ts_ms / 1000.0)
        synthetic_ah_trade = False
        if px is None:
            return
        if size is None or size <= 0:
            if in_rth_now or not self.include_ah:
                return
            # In extended hours IB often publishes a valid last but omits lastSize.
            # Keep OHLC continuity for MA/VWAP inputs, but preserve zero volume.
            size = 0.0
            synthetic_ah_trade = True

        if self.enable_trade_sanity_filter:
            accepted, reject_reason = self._validate_trade(
                symbol=symbol,
                px=px,
                size=size,
                bid=bid,
                ask=ask,
                allow_zero_size=synthetic_ah_trade,
            )
            if not accepted:
                self._record_rejected_trade(symbol, reject_reason)
                return

        if not self.include_ah and not in_rth_now:
            return

        win_start = floor_5s(ts_ms)
        win_end = win_start + self.bar_ms
        key = (symbol, win_start)

        bucket = self.buckets.get(key)
        if bucket is None:
            seq = self.last_bar_seq.get(symbol, 0) + 1
            self.last_bar_seq[symbol] = seq
            bucket = BarBucket(symbol=symbol, ts_start_ms=win_start, ts_end_ms=win_end, bar_seq=seq)
            self.buckets[key] = bucket
            self.emit_schedule[key] = win_end + self.emit_buffer_ms

        bucket.on_trade(px, size)
        self.last_good_trade_px[symbol] = px

        watermark = self.watermark_ms.get(symbol, 0)
        if win_start < watermark:
            bucket.late_count += 1

    def _record_rejected_trade(self, symbol: str, reason: str) -> None:
        key = (symbol, reason or "UNKNOWN")
        count = self.rejected_trade_counts.get(key, 0) + 1
        self.rejected_trade_counts[key] = count
        if count == 1 or count % 25 == 0:
            logger.warning("Rejected anomalous trade tick", symbol=symbol, reason=reason, count=count)

    def _validate_trade(
        self,
        *,
        symbol: str,
        px: float,
        size: float,
        bid: Optional[float],
        ask: Optional[float],
        allow_zero_size: bool = False,
    ) -> Tuple[bool, str]:
        if px <= 0:
            return False, "NON_POSITIVE_PRICE"
        if size <= 0 and not allow_zero_size:
            return False, "NO_SIZE"

        if bid is not None and ask is not None and ask >= bid > 0:
            mid = (bid + ask) / 2.0
            allowed_dev = max(mid * self.trade_max_deviation_from_quote_pct, (ask - bid) * 4.0)
            if abs(px - mid) > allowed_dev and not (bid <= px <= ask):
                return False, "TOO_FAR_FROM_QUOTE"

        last_good = self.last_good_trade_px.get(symbol)
        if last_good is not None and last_good > 0:
            jump_pct = abs(px - last_good) / last_good
            if jump_pct > self.trade_max_jump_from_last_pct:
                if bid is None or ask is None or not (bid <= px <= ask):
                    return False, "TOO_FAR_FROM_LAST_GOOD_TRADE"
        return True, ""

    def _bar_sanity_reason(self, bucket: BarBucket) -> str:
        if bucket.open is None or bucket.high is None or bucket.low is None or bucket.close is None:
            return "EMPTY_BAR"
        vals = [bucket.open, bucket.high, bucket.low, bucket.close]
        if any(v is None or v <= 0 for v in vals):
            return "NON_POSITIVE_BAR_PRICE"
        if bucket.high < max(bucket.open, bucket.close, bucket.low):
            return "HIGH_INCONSISTENT"
        if bucket.low > min(bucket.open, bucket.close, bucket.high):
            return "LOW_INCONSISTENT"
        ref = self.last_good_trade_px.get(bucket.symbol) or bucket.close
        if ref and ref > 0:
            dev_limit = 1.0 + self.bar_max_deviation_pct
            if any((v / ref) < (1.0 / dev_limit) or (v / ref) > dev_limit for v in vals):
                return "BAR_TOO_FAR_FROM_REF"
            if ((bucket.high - bucket.low) / ref) > self.bar_max_range_pct:
                return "BAR_RANGE_TOO_WIDE"
        return ""

    # -------- flushing --------

    async def _flusher_loop(self):
        """周期检查到期窗口并发射"""
        while self._running:
            try:
                now_ms = int(time.time() * 1000)
                ready_keys = [k for k, t in self.emit_schedule.items() if t <= now_ms]

                for key in ready_keys:
                    bucket = self.buckets.pop(key, None)
                    if bucket is None:
                        # 计划存在但桶被清理（极少见）
                        del self.emit_schedule[key]
                        continue

                    # 1) 发射 bar（失败则回队重试，不推进状态，避免数据丢失）
                    emitted = await self._emit_bar(bucket)
                    if not emitted:
                        self.buckets[key] = bucket
                        self.emit_schedule[key] = now_ms + self._emit_retry_delay_ms
                        logger.warning(
                            "5s bar emit deferred for retry",
                            symbol=bucket.symbol,
                            ts_start=bucket.ts_start_ms,
                            retry_after_ms=self._emit_retry_delay_ms,
                        )
                        continue

                    # 2) bar 发射成功后再做缺口检测（避免重试时重复 gap 事件）
                    await self._check_and_emit_gap(bucket)
                    del self.emit_schedule[key]

                    # 3) 更新发射状态
                    self.last_emitted_seq[bucket.symbol] = bucket.bar_seq
                    self.last_emitted_ts[bucket.symbol] = bucket.ts_start_ms

                    # 4) 水位推进到窗口结束
                    self.watermark_ms[bucket.symbol] = bucket.ts_end_ms

                    # 5) 状态持久化
                    await self._save_state(bucket.symbol)

            except Exception as e:
                logger.error("flusher loop error", error=str(e))

            await asyncio.sleep(0.1)

    # -------- emitters --------

    async def _emit_bar(self, bucket: BarBucket) -> bool:
        """发送 5s bar 到 Kafka（失败时触发 producer 自愈并重试一次）"""
        try:
            if self.enable_bar_sanity_filter:
                reason = self._bar_sanity_reason(bucket)
                if reason:
                    self.quarantined_bars.append(
                        {
                            "symbol": bucket.symbol,
                            "ts_start_ms": bucket.ts_start_ms,
                            "ts_end_ms": bucket.ts_end_ms,
                            "bar_seq": bucket.bar_seq,
                            "reason": reason,
                            "open": bucket.open,
                            "high": bucket.high,
                            "low": bucket.low,
                            "close": bucket.close,
                            "volume": bucket.volume,
                            "trades": bucket.trades,
                        }
                    )
                    logger.warning(
                        "Quarantined anomalous 5s bar",
                        symbol=bucket.symbol,
                        ts_start=bucket.ts_start_ms,
                        reason=reason,
                    )
                    return True
            msg = bucket.to_bar_message()
            ok = await self._send_with_recovery(topic=BAR_TOPIC, key=bucket.symbol, value=msg)
            if not ok:
                self._emit_fail_streak += 1
                if self._emit_fail_streak == 1 or self._emit_fail_streak % 10 == 0:
                    logger.error(
                        "failed to emit 5s bar",
                        error="send_failed_after_recovery",
                        symbol=bucket.symbol,
                        failure_streak=self._emit_fail_streak,
                    )
                return False
            self._emit_fail_streak = 0
            publish_ms = int(time.time() * 1000)
            self._last_successful_bar_publish_ms[bucket.symbol] = publish_ms
            self._mark_ready(
                True,
                reason="five_second_aggregator_bar_published",
                metadata={
                    "symbol": bucket.symbol,
                    "topic": BAR_TOPIC,
                    "ts_start_ms": bucket.ts_start_ms,
                    "ts_end_ms": bucket.ts_end_ms,
                    "published_at_ms": publish_ms,
                    "ttl_sec": 20,
                    "allow_offhours_stale": True,
                    "last_successful_bar_publish_ms": dict(self._last_successful_bar_publish_ms),
                },
            )
            logger.debug("5s bar emitted",
                         symbol=bucket.symbol,
                         ts_start=bucket.ts_start_ms,
                         trades=bucket.trades,
                         volume=bucket.volume)
            return True
        except Exception as e:
            logger.error("failed to emit 5s bar", error=str(e), symbol=bucket.symbol)
            return False

    async def _emit_gap(
        self,
        symbol: str,
        reason: str,
        *,
        expected_seq: Optional[int] = None,
        got_seq: Optional[int] = None,
        expected_start_ms: Optional[int] = None,
        actual_start_ms: Optional[int] = None,
    ):
        """发送缺口事件（BackfillService 消费）"""
        try:
            msg = {
                "schema_v": 1,
                "metadata": {"ts_us": now_us(), "producer": "agg_5s"},
                "data": {
                    "symbol": symbol,
                    "bar_sec": 5,
                    "reason": reason,  # "SEQ_GAP" | "TIME_GAP"
                    # 统一字段：from_ts / to_ts（秒）
                    "from_ts": (expected_start_ms // 1000) if expected_start_ms is not None else None,
                    "to_ts": (actual_start_ms // 1000) if actual_start_ms is not None else None,
                    "expected_seq": expected_seq,
                    "got_seq": got_seq,
                    "expected_ts_start_us": (expected_start_ms * 1000) if expected_start_ms is not None else None,
                    "actual_ts_start_us": (actual_start_ms * 1000) if actual_start_ms is not None else None,
                },
            }
            ok = await self._send_with_recovery(topic=GAP_TOPIC, key=symbol, value=msg)
            if not ok:
                logger.warning("failed to emit gap event after recovery", symbol=symbol, reason=reason)
                return
            # Lowered to debug to reduce noise; still emitted to GAP topic for downstream consumers.
            logger.debug("GAP event emitted",
                         symbol=symbol, reason=reason,
                         expected_seq=expected_seq, got_seq=got_seq,
                         expected_ms=expected_start_ms, actual_ms=actual_start_ms)
        except Exception as e:
            logger.error("failed to emit gap event", error=str(e), symbol=symbol)

    def _build_producer(self) -> AIOKafkaProducer:
        return AIOKafkaProducer(**self._producer_kwargs)

    async def _send_with_recovery(self, *, topic: str, key: str, value: dict) -> bool:
        try:
            await self.producer.send(topic=topic, key=key, value=value)
            return True
        except Exception as exc:
            if not self._running:
                return False
            self._mark_ready(
                False,
                reason="five_second_aggregator_send_failed",
                metadata={"topic": topic, "key": key, "error": str(exc)},
            )
            logger.warning("kafka send failed, trying producer recovery", topic=topic, key=key, error=str(exc))
            recovered = await self._recover_producer()
            if not recovered:
                self._mark_ready(
                    False,
                    reason="five_second_aggregator_producer_recovery_failed",
                    metadata={"topic": topic, "key": key},
                )
                return False
            try:
                await self.producer.send(topic=topic, key=key, value=value)
                self._mark_ready(
                    True,
                    reason="five_second_aggregator_send_recovered",
                    metadata={"topic": topic, "key": key},
                )
                logger.info("kafka send recovered after producer restart", topic=topic, key=key)
                return True
            except Exception as retry_exc:
                self._mark_ready(
                    False,
                    reason="five_second_aggregator_send_failed_after_recovery",
                    metadata={"topic": topic, "key": key, "error": str(retry_exc)},
                )
                logger.warning("kafka send failed after producer recovery", topic=topic, key=key, error=str(retry_exc))
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
                    await self._stop_producer_best_effort(
                        old,
                        timeout=3.0,
                        reason="recovery_old_stop",
                    )
                if self._producer_restart_backoff_sec > 0:
                    await asyncio.sleep(self._producer_restart_backoff_sec)
                await asyncio.wait_for(self.producer.start(), timeout=10.0)
                self._producer_last_restart_ms = int(time.time() * 1000)
                self._mark_ready(
                    False,
                    reason="five_second_aggregator_producer_recovered",
                    metadata={"producer_id": id(self.producer)},
                )
                logger.info("kafka producer recovered", producer_id=id(self.producer))
                return True
            except Exception as exc:
                self._mark_ready(
                    False,
                    reason="five_second_aggregator_producer_recovery_failed",
                    metadata={"error": str(exc)},
                )
                logger.warning("kafka producer recovery failed", error=str(exc))
                return False

    def _defer_producer_stop(self, producer: Optional[AIOKafkaProducer], *, reason: str) -> None:
        if producer is None:
            return

        async def _stop_later(prod: AIOKafkaProducer) -> None:
            with contextlib.suppress(Exception):
                await prod.stop()

        self._retired_producers.add(producer)
        task = asyncio.create_task(_stop_later(producer), name=f"agg5s_producer_stop:{reason}")
        self._producer_stop_tasks.add(task)

        def _release_retired(task: asyncio.Task) -> None:
            self._producer_stop_tasks.discard(task)
            self._retired_producers.discard(producer)

        task.add_done_callback(_release_retired)

    # -------- gap detection --------

    async def _check_and_emit_gap(self, bucket: BarBucket):
        """序列 + 时间的缺口检测（仅 RTH 内）"""
        # 非 RTH 不检测（避免夜盘/周末误报）
        if not is_rth(bucket.ts_end_ms / 1000.0):
            return

        symbol = bucket.symbol
        expected_seq = self.last_emitted_seq.get(symbol, 0) + 1

        # 1) 序列缺口
        if bucket.bar_seq != expected_seq:
            await self._emit_gap(
                symbol,
                "SEQ_GAP",
                expected_seq=expected_seq,
                got_seq=bucket.bar_seq,
                expected_start_ms=(self.last_emitted_ts.get(symbol, None) + self.bar_ms)
                if symbol in self.last_emitted_ts else None,
                actual_start_ms=bucket.ts_start_ms,
            )

        # 2) 时间缺口（跳过了 ≥1 个窗口）
        if symbol in self.last_emitted_ts:
            expected_start_ms = self.last_emitted_ts[symbol] + self.bar_ms
            if bucket.ts_start_ms > expected_start_ms:
                await self._emit_gap(
                    symbol,
                    "TIME_GAP",
                    expected_seq=expected_seq,
                    got_seq=bucket.bar_seq,
                    expected_start_ms=expected_start_ms,
                    actual_start_ms=bucket.ts_start_ms,
                )

    # -------- persistence --------

    async def _restore_state(self):
        """从 Redis 恢复状态（重启后延续序列/水位）"""
        try:
            for symbol in self.symbols:
                wkey = f"{self.redis_ns}:watermark:{symbol}"
                wm = await self.redis.get(wkey)
                if wm:
                    self.watermark_ms[symbol] = int(wm)

                seq_key = f"{self.redis_ns}:seq:{symbol}"
                seq = await self.redis.get(seq_key)
                if seq:
                    self.last_bar_seq[symbol] = int(seq)

                emit_seq_key = f"{self.redis_ns}:emit_seq:{symbol}"
                es = await self.redis.get(emit_seq_key)
                if es:
                    self.last_emitted_seq[symbol] = int(es)

                emit_ts_key = f"{self.redis_ns}:emit_ts:{symbol}"
                ets = await self.redis.get(emit_ts_key)
                if ets:
                    self.last_emitted_ts[symbol] = int(ets)

            logger.info("state restored from redis")
        except Exception as e:
            logger.error("failed to restore state", error=str(e))

    async def _save_state(self, symbol: str):
        """写回 Redis"""
        try:
            pipe = self.redis.pipeline()
            pipe.set(f"{self.redis_ns}:watermark:{symbol}", self.watermark_ms.get(symbol, 0))
            pipe.set(f"{self.redis_ns}:seq:{symbol}", self.last_bar_seq.get(symbol, 0))
            pipe.set(f"{self.redis_ns}:emit_seq:{symbol}", self.last_emitted_seq.get(symbol, 0))
            pipe.set(f"{self.redis_ns}:emit_ts:{symbol}", self.last_emitted_ts.get(symbol, 0))
            await pipe.execute()
        except Exception as e:
            logger.error("failed to save state", error=str(e), symbol=symbol)
