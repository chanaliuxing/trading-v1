"""
Market Data Service - Phase 2 Integration
Connects IB Market Data → 5s Aggregator → Kafka

Integrates:
- IB Market Data Subscription (doc/36.md)
- 5-Second Aggregator (doc/25.md)
- Backfill Service (doc/29.md)
- Tick Publisher (Phase 4 - Data Lake)
"""
import asyncio
import json
import os
import time
import signal
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable
from zoneinfo import ZoneInfo

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError, KafkaConnectionError

from libs.obs.audit_writer import LocalAuditWriter
from libs.logger import get_logger
from libs.config import config

from services.market.ib_market_data import IBMarketDataAdapter, TickData
from services.market.aggregator.five_sec_agg import FiveSecondAggregator
from services.market.backfill_service import BackfillService
from services.production.recovery_readiness import get_recovery_readiness_service

logger = get_logger(__name__)

_TICK_TOPIC_TRADES = "md.tick.trade.v1"
_TICK_TOPIC_QUOTES = "md.tick.quote.v1"


class MarketDataServiceFatalError(RuntimeError):
    """Raised when MarketDataService enters a fatal degraded state."""


@dataclass(frozen=True)
class _KafkaPublishItem:
    topic: str
    key: Optional[str]
    value: dict


def _json_dumps_bytes(v: dict) -> bytes:
    return json.dumps(v, separators=(",", ":")).encode("utf-8")


class MarketDataService:
    """
    Integrated Market Data Service

    Components:
    1. IB Market Data Adapter - Real-time tick subscription
    2. 5-Second Aggregator - Tick → 5s bar aggregation
    3. Backfill Service - Historical gap filling

    Data Flow:
    IB Ticks → Aggregator → Kafka (md.bar.5s.v2)
            ↓
            → Tick Publisher → Kafka (md.tick.trade.v1, md.tick.quote.v1)
                         ↓
                    Gap Detection
                         ↓
                  Backfill Service → Kafka (md.bar.5s.repair.v1)
    """

    def __init__(
        self,
        symbols: List[str],
        enable_backfill: bool = True,
        enable_tick_publisher: bool = True,
        ib_host: Optional[str] = None,
        ib_port: Optional[int] = None,
        ib: Optional[object] = None,
        kafka_bootstrap: Optional[str] = None,
        redis_url: Optional[str] = None,
        rth_only: bool = False,
        trading_hours: Optional[dict] = None,
        allowed_sessions: Optional[Iterable[str]] = None,
        audit_writer: Optional[LocalAuditWriter] = None,
        use_shared_ib_manager: bool = False,
        required_subscription_symbols: Optional[Iterable[str]] = None,
    ):
        """
        Initialize market data service

        Args:
            symbols: List of symbols to track
            enable_backfill: Enable backfill service
            enable_tick_publisher: Enable raw tick publishing (Phase 4)
            ib_host: IB host override
            ib_port: IB port override
            kafka_bootstrap: Kafka bootstrap override
            redis_url: Redis URL override
            rth_only: Limit tick publish and IB handling to regular trading hours
            trading_hours: Configurable trading hours window (expects open_et/close_et)
            allowed_sessions: Allowed ET session phases when rth_only is False
        """
        self.symbols = symbols
        self.required_subscription_symbols = [
            str(item).strip().upper()
            for item in (required_subscription_symbols or symbols or [])
            if str(item).strip()
        ]
        self.enable_backfill = enable_backfill
        self.enable_tick_publisher = enable_tick_publisher
        self.rth_only = bool(rth_only)
        self._rth_window = self._parse_trading_hours(trading_hours or config.get('trading_hours', {}) or {})
        self.allowed_sessions = {
            str(s).strip().upper() for s in (allowed_sessions or ("PREMARKET", "RTH", "AFTERHOURS", "OVERNIGHT"))
            if str(s).strip()
        }
        self.audit_writer = audit_writer

        # Kafka config
        kafka_cfg = config.kafka_config
        self.kafka_bootstrap = kafka_bootstrap or kafka_cfg['bootstrap_servers']

        # Tick publisher (Phase 4 - Data Lake) hardened with bounded queue + self-heal.
        self.tick_producer: Optional[AIOKafkaProducer] = None
        tick_cfg = config.get("services.market_data_service.tick_publisher", {}) or {}
        self._tick_queue: asyncio.Queue[_KafkaPublishItem] = asyncio.Queue(
            maxsize=int(tick_cfg.get("queue_max", 20000))
        )
        self._tick_sender_tasks: List[asyncio.Task] = []
        self._tick_sender_concurrency: int = max(1, int(tick_cfg.get("send_workers", 1)))
        self._tick_send_timeout_sec: float = float(tick_cfg.get("send_timeout_sec", 3.0))
        self._tick_restart_backoff_base_sec: float = float(tick_cfg.get("restart_backoff_base_sec", 1.0))
        self._tick_restart_backoff_max_sec: float = float(tick_cfg.get("restart_backoff_max_sec", 30.0))
        self._tick_failure_streak: int = 0
        self._tick_last_send_ok_ms: int = 0
        self._tick_restart_lock = asyncio.Lock()
        self._tick_degraded_since_mono: Optional[float] = None
        self._tick_degraded_failure_count: int = 0
        self._fatal_error: Optional[MarketDataServiceFatalError] = None

        # Optional: disk spool for tick publishing when Kafka is down or queue is full.
        self._tick_spool_enabled: bool = bool(tick_cfg.get("spool_enabled", False))
        self._tick_spool_dir: Path = Path(str(tick_cfg.get("spool_dir", "runtime/spool/tick_publisher"))).expanduser()
        self._tick_spool_max_bytes: int = int(float(tick_cfg.get("spool_max_mb", 200))) * 1024 * 1024
        self._tick_spool_segment_bytes: int = int(float(tick_cfg.get("spool_segment_mb", 10))) * 1024 * 1024
        self._tick_spool_queue: asyncio.Queue[Optional[_KafkaPublishItem]] = asyncio.Queue(
            maxsize=int(tick_cfg.get("spool_queue_max", 50000))
        )
        self._tick_spool_writer_task: Optional[asyncio.Task] = None
        self._tick_spool_replay_task: Optional[asyncio.Task] = None
        self._tick_producer_monitor_task: Optional[asyncio.Task] = None
        self._tick_producer_stop_tasks: set[asyncio.Task] = set()
        self._last_recovery_data_plane_heartbeat_ms: int = 0
        self._last_tick_producer_ready_heartbeat_ms: int = 0
        fail_fast_default = not self._tick_spool_enabled
        self._tick_fail_fast_enabled: bool = bool(tick_cfg.get("fail_fast_enabled", fail_fast_default))
        self._tick_fail_fast_after_sec: float = float(tick_cfg.get("fail_fast_after_sec", 180.0))
        self._tick_fail_fast_min_failures: int = max(1, int(tick_cfg.get("fail_fast_min_failures", 12)))

        # Initialize aggregator (will receive ticks)
        agg_cfg = config.get("services.market_data_service.aggregator", {}) or {}
        self.aggregator = FiveSecondAggregator(
            symbols=symbols,
            emit_buffer_ms=300,
            max_late_ms=2000,
            include_ah=not self.rth_only,
            redis_url=redis_url,
            kafka_bootstrap=kafka_bootstrap,
            enable_trade_sanity_filter=bool(agg_cfg.get("enable_trade_sanity_filter", False)),
            trade_max_deviation_from_quote_pct=float(agg_cfg.get("trade_max_deviation_from_quote_pct", 0.02)),
            trade_max_jump_from_last_pct=float(agg_cfg.get("trade_max_jump_from_last_pct", 0.05)),
            enable_bar_sanity_filter=bool(agg_cfg.get("enable_bar_sanity_filter", False)),
            bar_max_deviation_pct=float(agg_cfg.get("bar_max_deviation_pct", 0.35)),
            bar_max_range_pct=float(agg_cfg.get("bar_max_range_pct", 0.20)),
            readiness_callback=self._mark_recovery_component,
        )

        # Initialize IB adapter (will feed aggregator)
        self.ib_adapter = IBMarketDataAdapter(
            symbols=symbols,
            tick_callback=self._on_tick,
            host=ib_host,
            port=ib_port,
            ib=ib,  # shared IB session when provided
            use_shared_manager=use_shared_ib_manager,
            rth_only=self.rth_only,
            trading_hours=self._rth_window,
            allowed_sessions=self.allowed_sessions,
        )

        # Initialize backfill service (optional)
        self.backfill = None
        if enable_backfill:
            self.backfill = BackfillService(
                kafka_bootstrap=kafka_bootstrap,
                redis_url=redis_url,
                ib_host=ib_host,
                ib_port=ib_port
            )

        self._running = False
        self._tick_audit_by_symbol: Dict[str, Dict[str, Any]] = {}

        logger.info("MarketDataService initialized",
                   symbols=symbols,
                   enable_backfill=enable_backfill,
                   enable_tick_publisher=enable_tick_publisher)

    @staticmethod
    def _is_ib_recovery_passive_wait_error_text(error_text: str) -> bool:
        msg = str(error_text or "").lower()
        return (
            "reconnect_gatekeeper_denied:" in msg
            or "ib_recovery_short_circuit_active:" in msg
            or "ib_gateway_connection_circuit_open:" in msg
            or "ib_api_handshake_not_ready" in msg
        )

    @staticmethod
    def _ib_recovery_passive_wait_sleep_sec(default_sec: float = 1.0) -> float:
        retry_sec = max(1.0, float(default_sec))
        try:
            from services.ibkr.ib_gateway_circuit import get_ib_gateway_connection_circuit

            circuit_state = get_ib_gateway_connection_circuit().snapshot()
            if circuit_state.open and float(circuit_state.retry_after_sec or 0.0) > 0.0:
                retry_sec = max(retry_sec, min(float(circuit_state.retry_after_sec), 300.0))
        except Exception:
            pass
        try:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            remaining_fn = getattr(get_ib_connection_manager(), "_recovery_short_circuit_remaining_sec", None)
            if callable(remaining_fn):
                remaining = float(remaining_fn())
                if remaining > 0.0:
                    retry_sec = max(retry_sec, min(remaining, 60.0))
        except Exception:
            pass
        return retry_sec

    def _tick_fail_fast_active(self) -> bool:
        if not self._tick_fail_fast_enabled:
            return False
        if self.rth_only:
            return self._is_rth_time()
        phase = self._current_session_phase()
        return phase in self.allowed_sessions and phase != "OFF"

    def _clear_tick_publisher_degraded(self) -> None:
        self._tick_degraded_since_mono = None
        self._tick_degraded_failure_count = 0
        self._fatal_error = None
        self._mark_recovery_component("tick_producer", ready=True, reason="tick_producer_ready")

    def _mark_tick_publisher_degraded(self, *, reason: str, error: Optional[str] = None) -> None:
        self._mark_recovery_component(
            "tick_producer",
            ready=False,
            reason=reason,
            metadata={"error": str(error or "")},
        )
        now = time.monotonic()
        if self._tick_degraded_since_mono is None:
            self._tick_degraded_since_mono = now
        self._tick_degraded_failure_count += 1

        if self._fatal_error is not None or not self._tick_fail_fast_active():
            return

        degraded_sec = max(0.0, now - self._tick_degraded_since_mono)
        if degraded_sec < self._tick_fail_fast_after_sec:
            return
        if self._tick_degraded_failure_count < self._tick_fail_fast_min_failures:
            return

        self._fatal_error = MarketDataServiceFatalError(
            "Tick publisher degraded too long; failing fast "
            f"(reason={reason}, degraded_sec={degraded_sec:.1f}, failures={self._tick_degraded_failure_count})"
        )
        logger.error(
            "Tick publisher fail-fast triggered",
            reason=reason,
            error=error,
            degraded_sec=round(degraded_sec, 1),
            failures=self._tick_degraded_failure_count,
            queue_depth=self._tick_queue.qsize(),
            spool_enabled=self._tick_spool_enabled,
        )
        self._emit_audit(
            domain="system",
            stage="tick_publisher_fail_fast",
            severity="error",
            status="fatal",
            reason=reason,
            payload={
                "error": error,
                "degraded_sec": round(degraded_sec, 1),
                "failures": self._tick_degraded_failure_count,
                "queue_depth": self._tick_queue.qsize(),
                "spool_enabled": self._tick_spool_enabled,
            },
        )

    def _mark_recovery_component(
        self,
        name: str,
        *,
        ready: bool,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        get_recovery_readiness_service().mark(
            name,
            ready=ready,
            reason=reason,
            metadata=metadata or {},
        )

    def _subscription_readiness_snapshot(self) -> Dict[str, Any]:
        active_subscriptions = []
        try:
            active_subscriptions = list(self.ib_adapter.get_subscribed_symbols())
        except Exception:
            active_subscriptions = []
        deferred_subscriptions = []
        try:
            deferred_subscriptions = list(getattr(self.ib_adapter, "_deferred_symbols", set()) or [])
        except Exception:
            deferred_subscriptions = []
        configured_required = getattr(self, "required_subscription_symbols", None) or self.symbols or []
        required_subscriptions = [str(item) for item in configured_required if str(item)]
        active_subscription_set = {str(item) for item in active_subscriptions if str(item)}
        deferred_subscription_set = {str(item) for item in deferred_subscriptions if str(item)}
        effective_required_subscriptions = [
            item for item in required_subscriptions if item not in deferred_subscription_set
        ]
        missing_subscriptions = [
            item for item in effective_required_subscriptions if item not in active_subscription_set
        ]
        return {
            "required_subscriptions": required_subscriptions,
            "effective_required_subscriptions": effective_required_subscriptions,
            "active_subscriptions": active_subscriptions,
            "deferred_subscriptions": deferred_subscriptions,
            "missing_subscriptions": missing_subscriptions,
            "subscriptions_ready": len(missing_subscriptions) == 0,
        }

    def _refresh_recovery_data_plane_heartbeat(self, *, symbol: str) -> None:
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_recovery_data_plane_heartbeat_ms < 5_000:
            return
        self._last_recovery_data_plane_heartbeat_ms = now_ms
        subscription_snapshot = self._subscription_readiness_snapshot()
        metadata = {
            "symbols": list(self.symbols),
            **subscription_snapshot,
            "last_tick_symbol": str(symbol or ""),
            "last_tick_ms": now_ms,
            "ttl_sec": 30,
            "allow_offhours_stale": True,
        }
        self._mark_recovery_component(
            "market_data_service",
            ready=True,
            reason="market_data_tick_heartbeat",
            metadata=metadata,
        )
        self._mark_recovery_component(
            "market_data_ib",
            ready=bool(self.ib_adapter and self.ib_adapter.is_connected()),
            reason="market_data_tick_heartbeat",
            metadata=metadata,
        )
        self._mark_recovery_component(
            "market_data_subscriptions",
            ready=bool(subscription_snapshot.get("subscriptions_ready")),
            reason="market_data_tick_heartbeat",
            metadata=metadata,
        )

    def _raise_if_fatal(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error

    # ---------------- Tick publisher internals ----------------

    def _tick_queue_put_nowait(self, item: _KafkaPublishItem) -> bool:
        """Best-effort enqueue; returns True if queued, False if it had to be spooled/dropped."""
        try:
            self._tick_queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            if self._tick_spool_enabled:
                try:
                    self._tick_spool_queue.put_nowait(item)
                    return False
                except asyncio.QueueFull:
                    logger.error(
                        "Tick spool queue full; dropping tick message",
                        topic=item.topic,
                        symbol=item.key,
                    )
                    return False

            logger.error("Tick publish queue full; dropping tick message", topic=item.topic, symbol=item.key)
            return False

    def _build_tick_producer(self) -> AIOKafkaProducer:
        kafka_cfg = config.kafka_config
        return AIOKafkaProducer(
            bootstrap_servers=self.kafka_bootstrap,
            value_serializer=_json_dumps_bytes,
            linger_ms=int(kafka_cfg.get("linger_ms", 5)),
            acks=kafka_cfg.get("acks", "all"),
            enable_idempotence=bool(kafka_cfg.get("enable_idempotence", True)),
            compression_type=kafka_cfg.get("compression_type", "lz4"),
            max_batch_size=int(kafka_cfg.get("batch_size", 131072)),
            request_timeout_ms=int(config.get("kafka.message_timeout_ms", 15000)),
            connections_max_idle_ms=int(config.get("kafka.connections_max_idle_ms", 540000)),
        )

    async def _start_tick_publisher(self) -> None:
        """Start (or restart) the Kafka tick publisher with background sender workers."""
        if not self.enable_tick_publisher:
            return

        # Always recreate tasks/producer on (re)start for clean state after Kafka/network issues.
        await self._stop_tick_publisher()

        self._tick_last_send_ok_ms = int(time.time() * 1000)
        self._tick_failure_streak = 0
        self._clear_tick_publisher_degraded()
        self._tick_sender_tasks = [
            asyncio.create_task(self._tick_sender_loop(worker_id=i + 1), name=f"tick_sender:{i+1}")
            for i in range(self._tick_sender_concurrency)
        ]

        if self._tick_spool_enabled:
            self._tick_spool_dir.mkdir(parents=True, exist_ok=True)
            self._tick_spool_writer_task = asyncio.create_task(self._tick_spool_writer_loop(), name="tick_spool_writer")
            self._tick_spool_replay_task = asyncio.create_task(self._tick_spool_replay_loop(), name="tick_spool_replay")

        # Kick an immediate connection attempt. If Kafka is down, monitor will retry with backoff.
        await self._restart_tick_producer(backoff_sec=0.0)

        self._tick_producer_monitor_task = asyncio.create_task(self._tick_producer_monitor_loop(), name="tick_producer_monitor")

    async def _stop_tick_publisher(self) -> None:
        """Stop tick publisher background tasks and Kafka producer (best-effort)."""
        current = asyncio.current_task()
        sender_tasks = list(self._tick_sender_tasks)
        for t in sender_tasks:
            t.cancel()
        await asyncio.gather(*[t for t in sender_tasks if t is not current], return_exceptions=True)
        self._tick_sender_tasks.clear()

        if self._tick_producer_monitor_task:
            self._tick_producer_monitor_task.cancel()
            await asyncio.gather(self._tick_producer_monitor_task, return_exceptions=True)
            self._tick_producer_monitor_task = None

        if self._tick_spool_writer_task:
            try:
                self._tick_spool_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            self._tick_spool_writer_task.cancel()
            await asyncio.gather(self._tick_spool_writer_task, return_exceptions=True)
            self._tick_spool_writer_task = None

        if self._tick_spool_replay_task:
            self._tick_spool_replay_task.cancel()
            await asyncio.gather(self._tick_spool_replay_task, return_exceptions=True)
            self._tick_spool_replay_task = None

        if self.tick_producer:
            logger.info("Stopping tick publisher...")
            await self._stop_tick_producer_best_effort(
                self.tick_producer,
                timeout=5.0,
                reason="publisher_stop",
            )
            logger.info("✅ Tick publisher stopped")
            self.tick_producer = None

        # Reap any background stop tasks from prior timeout paths.
        if self._tick_producer_stop_tasks:
            done, pending = await asyncio.wait(self._tick_producer_stop_tasks, timeout=0, return_when=asyncio.ALL_COMPLETED)
            self._tick_producer_stop_tasks = set(pending)
            for task in done:
                with contextlib.suppress(Exception):
                    _ = task.result()

    async def _tick_producer_monitor_loop(self) -> None:
        """Background monitor that reconnects the Kafka producer when it is unavailable."""
        backoff_sec = self._tick_restart_backoff_base_sec
        while self._running and self.enable_tick_publisher:
            try:
                if self.tick_producer is None:
                    await self._restart_tick_producer(backoff_sec=backoff_sec)
                    backoff_sec = min(backoff_sec * 2, self._tick_restart_backoff_max_sec)
                else:
                    backoff_sec = self._tick_restart_backoff_base_sec
                    await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Tick producer monitor error", error=str(exc))
                await asyncio.sleep(1.0)

    async def _tick_sender_loop(self, *, worker_id: int) -> None:
        backoff_sec = self._tick_restart_backoff_base_sec
        while self._running and self.enable_tick_publisher:
            try:
                item = await self._tick_queue.get()
                prod = self.tick_producer
                if prod is None:
                    if self._tick_spool_enabled:
                        try:
                            self._tick_spool_queue.put_nowait(item)
                        except asyncio.QueueFull:
                            logger.error("Tick spool queue full; dropping tick message", topic=item.topic, symbol=item.key)
                    continue

                key_bytes = item.key.encode("utf-8") if item.key else None
                try:
                    await asyncio.wait_for(
                        prod.send_and_wait(item.topic, key=key_bytes, value=item.value),
                        timeout=self._tick_send_timeout_sec,
                    )
                    self._tick_failure_streak = 0
                    self._tick_last_send_ok_ms = int(time.time() * 1000)
                    if self._tick_last_send_ok_ms - self._last_tick_producer_ready_heartbeat_ms >= 5_000:
                        self._last_tick_producer_ready_heartbeat_ms = self._tick_last_send_ok_ms
                        self._mark_recovery_component(
                            "tick_producer",
                            ready=True,
                            reason="tick_producer_send_ok",
                            metadata={
                                "topic": item.topic,
                                "symbol": item.key,
                                "last_send_ok_ms": self._tick_last_send_ok_ms,
                                "ttl_sec": 30,
                                "allow_offhours_stale": True,
                            },
                        )
                    backoff_sec = self._tick_restart_backoff_base_sec
                except asyncio.TimeoutError as exc:
                    raise KafkaConnectionError(f"Tick publish timeout worker={worker_id}") from exc

            except asyncio.CancelledError:
                break
            except (KafkaError, KafkaConnectionError) as exc:
                # Spool the failed item (best effort), then restart producer with backoff.
                if self._tick_spool_enabled:
                    try:
                        self._tick_spool_queue.put_nowait(item)
                    except Exception:
                        pass
                self._tick_failure_streak += 1
                self._mark_tick_publisher_degraded(reason="send_failed", error=str(exc))
                logger.warning(
                    "Tick publish failed; restarting producer",
                    worker_id=worker_id,
                    failure_streak=self._tick_failure_streak,
                    error=str(exc),
                    backoff_sec=backoff_sec,
                )
                await self._restart_tick_producer(backoff_sec=backoff_sec)
                backoff_sec = min(backoff_sec * 2, self._tick_restart_backoff_max_sec)
            except Exception as exc:
                logger.error("Tick sender loop error", worker_id=worker_id, error=str(exc), exc_info=True)
                await asyncio.sleep(0.2)

    async def _restart_tick_producer(self, *, backoff_sec: float) -> None:
        if not self._running or not self.enable_tick_publisher:
            return
        await asyncio.sleep(max(0.0, float(backoff_sec)))
        async with self._tick_restart_lock:
            if not self._running or not self.enable_tick_publisher:
                return

            old = self.tick_producer
            self.tick_producer = None
            if old:
                await self._stop_tick_producer_best_effort(
                    old,
                    timeout=5.0,
                    reason="restart_old_stop",
                )

            new_prod: Optional[AIOKafkaProducer] = None
            try:
                new_prod = self._build_tick_producer()
                logger.info("Starting tick producer...", producer_id=id(new_prod))
                await asyncio.wait_for(new_prod.start(), timeout=10.0)
                self.tick_producer = new_prod
                self._tick_failure_streak = 0
                self._tick_last_send_ok_ms = int(time.time() * 1000)
                self._clear_tick_publisher_degraded()
                logger.info("✅ Tick producer ready", producer_id=id(new_prod))
            except asyncio.TimeoutError:
                self._mark_tick_publisher_degraded(reason="producer_start_timeout", error="timeout")
                logger.warning("Tick producer start timed out", timeout_sec=10.0)
                try:
                    if new_prod is not None:
                        await asyncio.wait_for(new_prod.stop(), timeout=2.0)
                except asyncio.TimeoutError:
                    if new_prod is not None:
                        self._defer_tick_producer_stop(new_prod, reason="restart_new_stop_timeout")
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                self._mark_tick_publisher_degraded(reason="producer_restart_failed", error=str(exc))
                logger.warning("Tick producer restart failed", error=str(exc))
                try:
                    if new_prod is not None:
                        await asyncio.wait_for(new_prod.stop(), timeout=2.0)
                except asyncio.TimeoutError:
                    if new_prod is not None:
                        self._defer_tick_producer_stop(new_prod, reason="restart_new_stop_timeout")
                except Exception:
                    pass

    def _defer_tick_producer_stop(self, producer: Optional[AIOKafkaProducer], *, reason: str) -> None:
        """Best-effort background close to avoid leaked aiokafka sender tasks after timeout."""
        if producer is None:
            return

        async def _stop_later(prod: AIOKafkaProducer) -> None:
            with contextlib.suppress(Exception):
                await prod.stop()

        task = asyncio.create_task(_stop_later(producer), name=f"tick_producer_stop:{reason}")
        self._tick_producer_stop_tasks.add(task)
        task.add_done_callback(lambda t: self._tick_producer_stop_tasks.discard(t))

    async def _stop_tick_producer_best_effort(
        self,
        producer: Optional[AIOKafkaProducer],
        *,
        timeout: float,
        reason: str,
    ) -> None:
        if producer is None:
            return
        stop_task = asyncio.create_task(producer.stop(), name=f"tick_producer_stop_now:{reason}")
        try:
            await asyncio.wait_for(asyncio.shield(stop_task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Tick producer stop timed out", reason=reason)
            self._tick_producer_stop_tasks.add(stop_task)
            stop_task.add_done_callback(lambda t: self._tick_producer_stop_tasks.discard(t))
        except asyncio.CancelledError:
            stop_task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tick producer stop failed", reason=reason, error=str(exc))

    def _spool_segment_path(self) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()
        return self._tick_spool_dir / f"ticks_{ts}_{pid}.jsonl"

    def _spool_enforce_limits(self) -> None:
        """Delete oldest spool files if total size exceeds configured cap."""
        if self._tick_spool_max_bytes <= 0:
            return
        try:
            files = sorted(self._tick_spool_dir.glob("ticks_*.jsonl*"), key=lambda p: p.name)
            total = 0
            sizes = []
            for p in files:
                try:
                    sz = p.stat().st_size
                except FileNotFoundError:
                    continue
                total += sz
                sizes.append((p, sz))
            if total <= self._tick_spool_max_bytes:
                return
            for p, sz in sizes:
                try:
                    p.unlink(missing_ok=True)
                    total -= sz
                    logger.warning("Deleted old tick spool segment to enforce cap", path=str(p), remaining_bytes=total)
                    if total <= self._tick_spool_max_bytes:
                        break
                except Exception:
                    pass
        except Exception:
            pass

    def _spool_append_batch_sync(self, items: list[_KafkaPublishItem]) -> None:
        """Blocking file append; run in a thread."""
        if not items:
            return
        self._tick_spool_dir.mkdir(parents=True, exist_ok=True)
        segs = sorted(self._tick_spool_dir.glob("ticks_*.jsonl"), key=lambda p: p.name)
        path = segs[-1] if segs else self._spool_segment_path()
        try:
            if path.exists() and path.stat().st_size >= self._tick_spool_segment_bytes:
                path = self._spool_segment_path()
        except Exception:
            path = self._spool_segment_path()

        with open(path, "a", encoding="utf-8") as f:
            for it in items:
                line = json.dumps(
                    {"topic": it.topic, "key": it.key, "value": it.value},
                    separators=(",", ":"),
                )
                f.write(line)
                f.write("\n")
        self._spool_enforce_limits()

    async def _tick_spool_writer_loop(self) -> None:
        buf: list[_KafkaPublishItem] = []
        last_flush = time.monotonic()
        flush_every_items = 200
        flush_every_sec = 0.5
        while self._running and self._tick_spool_enabled:
            try:
                item = await asyncio.wait_for(self._tick_spool_queue.get(), timeout=0.5)
                if item is None:
                    break
                buf.append(item)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Tick spool writer loop error", error=str(exc))

            now = time.monotonic()
            if buf and (len(buf) >= flush_every_items or (now - last_flush) >= flush_every_sec):
                try:
                    await asyncio.to_thread(self._spool_append_batch_sync, list(buf))
                except Exception as exc:
                    logger.error("Failed to write tick spool batch", error=str(exc), exc_info=True)
                buf.clear()
                last_flush = now

        if buf:
            try:
                await asyncio.to_thread(self._spool_append_batch_sync, list(buf))
            except Exception:
                pass

    async def _tick_spool_replay_loop(self) -> None:
        """Replay spooled ticks back into Kafka once producer is healthy."""
        while self._running and self._tick_spool_enabled:
            try:
                await asyncio.sleep(2.0)
                prod = self.tick_producer
                if not prod:
                    continue
                if self._tick_queue.qsize() > int(self._tick_queue.maxsize * 0.8):
                    continue

                files = sorted(self._tick_spool_dir.glob("ticks_*.jsonl"), key=lambda p: p.name)
                replay_files = sorted(self._tick_spool_dir.glob("ticks_*.jsonl.replay"), key=lambda p: p.name)
                all_files = replay_files + files
                if not all_files:
                    continue

                src = all_files[0]
                path = src
                if src.suffix == ".jsonl":
                    path = Path(str(src) + ".replay")
                    try:
                        src.rename(path)
                    except Exception:
                        path = src

                sent = 0
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                            topic = str(o.get("topic") or "")
                            key = o.get("key")
                            val = o.get("value")
                            if not topic or not isinstance(val, dict):
                                continue
                            key_bytes = str(key).encode("utf-8") if key else None
                            await asyncio.wait_for(
                                prod.send_and_wait(topic, key=key_bytes, value=val),
                                timeout=self._tick_send_timeout_sec,
                            )
                            sent += 1
                        except asyncio.TimeoutError:
                            raise KafkaConnectionError("Tick spool replay timeout")
                        except (KafkaError, KafkaConnectionError):
                            raise
                        except Exception:
                            continue

                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                if sent:
                    logger.info("Replayed tick spool segment", path=str(path), sent=sent)

            except asyncio.CancelledError:
                break
            except (KafkaError, KafkaConnectionError) as exc:
                logger.warning("Tick spool replay paused (Kafka unhealthy)", error=str(exc))
                await asyncio.sleep(2.0)
            except Exception as exc:
                logger.warning("Tick spool replay loop error", error=str(exc))
                await asyncio.sleep(2.0)

    async def start(self):
        """Start all components and run continuously"""
        logger.info("=" * 80)
        logger.info("MARKET DATA SERVICE STARTING")
        logger.info("=" * 80)
        start_started_at = time.time()
        logger.info(
            "market_data_service_start_begin",
            symbols=len(self.symbols),
            ib_host=getattr(self.ib_adapter, "ib_host", None),
            ib_port=getattr(self.ib_adapter, "ib_port", None),
            ib_client_id=getattr(self.ib_adapter, "ib_client_id", None),
            ib_connected=bool(getattr(self.ib_adapter, "ib", None) and self.ib_adapter.ib.isConnected()),
            ib_id=id(getattr(self.ib_adapter, "ib", None)),
        )

        self._running = True
        self._mark_recovery_component(
            "market_data_service",
            ready=False,
            reason="market_data_service_starting",
            metadata={"symbols": list(self.symbols)},
        )
        reconnect_delay = 5  # Start with 5 seconds
        max_reconnect_delay = 300  # Max 5 minutes
        last_ib_start_passive_wait_log_ts = 0.0

        while self._running:
            try:
                self._raise_if_fatal()
                # Start tick publisher (Phase 4)
                if self.enable_tick_publisher:
                    logger.info("Starting tick publisher (Phase 4 - Data Lake)...")
                    await self._start_tick_publisher()

                # Start aggregator
                logger.info("Starting 5-second aggregator...")
                await self.aggregator.start()
                logger.info("✅ 5-second aggregator started")

                # Start IB adapter (will feed aggregator)
                logger.info("Starting IB market data adapter...")
                while self._running:
                    try:
                        await self.ib_adapter.start()
                        break
                    except Exception as conn_exc:
                        conn_text = str(conn_exc)
                        if not self._is_ib_recovery_passive_wait_error_text(conn_text):
                            raise
                        retry_in_sec = self._ib_recovery_passive_wait_sleep_sec(default_sec=5.0)
                        now_ts = time.time()
                        if (now_ts - last_ib_start_passive_wait_log_ts) >= 30.0:
                            last_ib_start_passive_wait_log_ts = now_ts
                            logger.warning(
                                "market_data_service_ib_start_passive_wait",
                                retry_in_sec=round(retry_in_sec, 2),
                                error=conn_text,
                            )
                        await asyncio.sleep(retry_in_sec)
                if not self._running:
                    break
                logger.info(
                    "market_data_service_ib_adapter_started",
                    ib_host=getattr(self.ib_adapter, "ib_host", None),
                    ib_port=getattr(self.ib_adapter, "ib_port", None),
                    ib_client_id=getattr(self.ib_adapter, "ib_client_id", None),
                    ib_connected=bool(getattr(self.ib_adapter, "ib", None) and self.ib_adapter.ib.isConnected()),
                    ib_id=id(getattr(self.ib_adapter, "ib", None)),
                    latency_ms=round((time.time() - start_started_at) * 1000.0, 1),
                )
                subscription_snapshot = self._subscription_readiness_snapshot()
                subscriptions_ready = bool(self.ib_adapter.is_connected()) and bool(
                    subscription_snapshot.get("subscriptions_ready")
                )
                self._mark_recovery_component(
                    "market_data_ib",
                    ready=True,
                    reason="ib_market_data_adapter_started",
                    metadata={
                        "symbols": list(self.symbols),
                        **subscription_snapshot,
                    },
                )
                self._mark_recovery_component(
                    "market_data_subscriptions",
                    ready=subscriptions_ready,
                    reason=(
                        "market_data_subscriptions_ready"
                        if subscriptions_ready
                        else "market_data_subscriptions_missing"
                    ),
                    metadata={
                        "symbols": list(self.symbols),
                        **subscription_snapshot,
                    },
                )
                self._emit_audit(
                    domain="system",
                    stage="ib_connected",
                    status="connected",
                    payload={
                        "client_id": getattr(self.ib_adapter, "ib_client_id", None),
                        "symbols": list(self.symbols),
                    },
                )

                logger.info("✅ IB market data adapter started")

                # Start backfill service
                if self.backfill:
                    logger.info("Starting backfill service...")
                    await self.backfill.start()
                    logger.info("✅ Backfill service started")

                logger.info("=" * 80)
                logger.info("MARKET DATA SERVICE RUNNING")
                logger.info("=" * 80)
                self._emit_audit(
                    domain="system",
                    stage="service_started",
                    status="started",
                    payload={"service": "market_data_service", "symbols": list(self.symbols)},
                )
                self._mark_recovery_component(
                    "market_data_service",
                    ready=True,
                    reason="market_data_service_running",
                    metadata={"symbols": list(self.symbols)},
                )
                logger.info("")
                logger.info(f"Subscribed symbols: {', '.join(self.symbols)}")
                logger.info("Data flow: IB → Aggregator → Kafka (md.bar.5s.v2)")
                if self.enable_tick_publisher:
                    logger.info("Tick publisher: Enabled (md.tick.trade.v1, md.tick.quote.v1)")
                if self.backfill:
                    logger.info("Backfill: Enabled (gaps → md.bar.5s.repair.v1)")
                logger.info("")

                # Reset reconnect delay on success
                reconnect_delay = 5

                # Keep-alive loop - check connection every 10 seconds
                while self._running:
                    self._raise_if_fatal()
                    await asyncio.sleep(10)
                    self._raise_if_fatal()

                    # Re-check _running after sleep (shutdown may have started)
                    if not self._running:
                        break

                    # Check if IB is still connected
                    if not self.ib_adapter.is_connected():
                        logger.warning(
                            "market_data_service_ib_connection_lost",
                            ib_id=id(getattr(self.ib_adapter, "ib", None)),
                            owns_ib=getattr(self.ib_adapter, "_owns_ib", None),
                            client_id=getattr(self.ib_adapter, "ib_client_id", None),
                            host=getattr(self.ib_adapter, "ib_host", None),
                            port=getattr(self.ib_adapter, "ib_port", None),
                        )
                        self._emit_audit(
                            domain="system",
                            stage="ib_disconnected",
                            severity="warning",
                            status="disconnected",
                            reason="keepalive_connection_lost",
                            payload={"symbols": list(self.symbols)},
                        )
                        self._mark_recovery_component(
                            "market_data_ib",
                            ready=False,
                            reason="keepalive_connection_lost",
                            metadata={"symbols": list(self.symbols)},
                        )
                        self._mark_recovery_component(
                            "market_data_subscriptions",
                            ready=False,
                            reason="keepalive_connection_lost",
                            metadata={"symbols": list(self.symbols)},
                        )
                        try:
                            from services.ibkr.ib_connection_manager import get_ib_connection_manager
                            ibm = get_ib_connection_manager()
                            gk = ibm.reconnect_gatekeeper
                            ib_cfg = config.ib_config
                            gk_host = str(ib_cfg.get("host", "127.0.0.1"))
                            gk_port = int(ib_cfg.get("port", 4002))
                            gate_attempts = 0
                            require_exec = bool(config.get("execution.enabled", True))
                            require_master = bool(require_exec)
                            while self._running:
                                # Runtime recovery must be API-ready equivalent to cold start.
                                # TCP LISTEN on 4002 is not enough; IB often opens the port
                                # before connectAsync(apiStart) is truly ready.
                                market_ready, exec_ready = await ibm.wait_until_ready(
                                    host=gk_host,
                                    port=gk_port,
                                    account=os.getenv("IB_ACCOUNT", ""),
                                    require_exec=require_exec,
                                    require_master=require_master,
                                    timeout_sec=float(
                                        os.getenv("MARKET_DATA_RUNTIME_API_READY_TIMEOUT_SEC", "30")
                                    ),
                                )
                                master_ready = bool(ibm.master_connected()) if require_master else True
                                master_truth_ready = bool(ibm.master_truth_complete()) if require_master else True
                                fully_ready = bool(
                                    market_ready
                                    and (exec_ready if require_exec else True)
                                    and master_ready
                                    and master_truth_ready
                                )
                                if fully_ready:
                                    logger.info(
                                        "market_data_service_ib_api_ready",
                                        host=gk_host,
                                        port=gk_port,
                                        attempts=gate_attempts,
                                        market_ready=market_ready,
                                        exec_ready=exec_ready,
                                        master_ready=master_ready,
                                        master_truth_complete=master_truth_ready,
                                    )
                                    break
                                logger.warning(
                                    "market_data_service_ib_not_fully_ready_retry",
                                    host=gk_host,
                                    port=gk_port,
                                    attempts=gate_attempts,
                                    market_ready=market_ready,
                                    exec_ready=exec_ready,
                                    master_ready=master_ready,
                                    master_truth_complete=master_truth_ready,
                                )
                                gate_attempts += 1
                                await asyncio.sleep(gk.current_backoff_sec())
                        except Exception as gk_exc:
                            logger.warning(
                                "market_data_service_gatekeeper_wait_failed",
                                error=str(gk_exc),
                            )
                        break

                    # Check market hours and log status
                    now = datetime.now()
                    hour = now.hour
                    is_premarket = 4 <= hour < 9.5  # 4am-9:30am ET
                    is_rth = 9.5 <= hour < 16  # 9:30am-4pm ET
                    is_afterhours = 16 <= hour < 20  # 4pm-8pm ET

                    if is_premarket:
                        logger.debug("Pre-market hours - system active")
                    elif is_rth:
                        logger.debug("Regular trading hours - system active")
                    elif is_afterhours:
                        logger.debug("After hours - system active")
                    else:
                        logger.debug("Outside trading hours - system monitoring")

                # If we broke out of keep-alive loop (IB lost / shutdown), make sure we
                # cleanly stop components before the next reconnect attempt.
                await self._safe_shutdown_components()

            except MarketDataServiceFatalError:
                await self._safe_shutdown_components()
                raise
            except asyncio.CancelledError:
                # Service task cancelled (process shutdown). Ensure Kafka/IB resources stop cleanly.
                logger.info("Market data service cancelled — shutting down streaming components.")
                await self._safe_shutdown_components()
                raise
            except Exception as e:
                logger.error(f"Market data service error: {e}", exc_info=True)
                self._emit_audit(
                    domain="system",
                    stage="market_data_error",
                    severity="warning",
                    status="error",
                    reason="service_exception",
                    payload={"error": str(e)},
                )

                # Clean up before reconnect
                await self._safe_shutdown_components()

                if self._running:
                    try:
                        from services.ibkr.ib_gateway_circuit import get_ib_gateway_connection_circuit

                        circuit_state = get_ib_gateway_connection_circuit().snapshot()
                        if circuit_state.open or "ib_gateway_connection_circuit_open" in str(e):
                            retry_after = max(float(reconnect_delay), min(float(circuit_state.retry_after_sec or 0.0), 300.0))
                            logger.warning(
                                "market_data_service_gateway_circuit_backoff",
                                retry_after_sec=round(retry_after, 2),
                                circuit_reason=circuit_state.reason,
                                circuit_source=circuit_state.source,
                            )
                            await asyncio.sleep(retry_after)
                            reconnect_delay = min(max(retry_after, reconnect_delay) * 2, max_reconnect_delay)
                            continue
                    except Exception:
                        pass
                    logger.info(f"Reconnecting in {reconnect_delay} seconds...")
                    await asyncio.sleep(reconnect_delay)

                    # Exponential backoff
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                else:
                    break

    async def stop(self):
        """Stop all components"""
        logger.info("=" * 80)
        logger.info("MARKET DATA SERVICE STOPPING")
        logger.info("=" * 80)

        self._running = False

        await self._safe_shutdown_components()
        self._flush_all_tick_summaries()
        self._emit_audit(
            domain="system",
            stage="service_stopped",
            status="stopped",
            payload={"service": "market_data_service", "symbols": list(self.symbols)},
        )

        logger.info("=" * 80)
        logger.info("MARKET DATA SERVICE STOPPED")
        logger.info("=" * 80)

    async def _safe_shutdown_components(self):
        """Best-effort stop for all realtime components"""
        self._mark_recovery_component("market_data_service", ready=False, reason="components_shutdown")
        self._mark_recovery_component("market_data_ib", ready=False, reason="components_shutdown")
        self._mark_recovery_component("market_data_subscriptions", ready=False, reason="components_shutdown")
        self._mark_recovery_component("bar_pipeline", ready=False, reason="components_shutdown")
        if self.enable_tick_publisher:
            self._mark_recovery_component("tick_producer", ready=False, reason="components_shutdown")
        self._flush_all_tick_summaries()
        try:
            await self._stop_tick_publisher()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("tick_publisher_stop_error", error=str(exc))

        try:
            if self.backfill:
                logger.info("Stopping backfill service...")
                await self.backfill.stop()
                logger.info("✅ Backfill service stopped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("backfill_stop_error", error=str(exc))

        try:
            logger.info("Stopping IB adapter...")
            await self.ib_adapter.stop()
            logger.info("✅ IB adapter stopped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ib_adapter_stop_error", error=str(exc))

        try:
            logger.info("Stopping aggregator...")
            await self.aggregator.stop()
            logger.info("✅ Aggregator stopped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("aggregator_stop_error", error=str(exc))

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

    def _is_rth_time(self) -> bool:
        """Check if current time is within regular trading hours (RTH) only"""
        et_tz = ZoneInfo('America/New_York')
        now_et = datetime.now(et_tz)
        time_decimal = now_et.hour + now_et.minute / 60.0
        open_dec, close_dec = self._rth_window
        return open_dec <= time_decimal < close_dec

    def _emit_audit(
        self,
        *,
        domain: str,
        stage: str,
        severity: str = "info",
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.audit_writer is None:
            return
        try:
            self.audit_writer.emit(
                domain=domain,
                component="market_data_service",
                stage=stage,
                severity=severity,
                symbol=symbol,
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            logger.warning("market_audit_emit_failed", stage=stage, symbol=symbol, error=str(exc))

    def _record_tick_summary(self, tick: TickData, *, is_rth: int) -> None:
        sym = str(tick.symbol or "").strip().upper()
        if not sym:
            return
        minute_bucket = int(int(tick.ts_us or 0) // 60_000_000)
        if minute_bucket <= 0:
            minute_bucket = int(time.time() // 60)
        state = self._tick_audit_by_symbol.get(sym)
        if state is None or int(state.get("minute_bucket", -1)) != minute_bucket:
            self._flush_tick_summary(sym)
            state = {
                "minute_bucket": minute_bucket,
                "trade_count": 0,
                "quote_count": 0,
                "last_trade_ts_us": None,
                "last_quote_ts_us": None,
                "last_tick_ts_us": None,
                "max_gap_ms": 0.0,
                "is_rth": int(is_rth),
            }
            self._tick_audit_by_symbol[sym] = state

        tick_ts_us = int(tick.ts_us or 0)
        last_tick_ts_us = state.get("last_tick_ts_us")
        if last_tick_ts_us:
            gap_ms = max(0.0, float(tick_ts_us - int(last_tick_ts_us)) / 1000.0)
            state["max_gap_ms"] = max(float(state.get("max_gap_ms", 0.0) or 0.0), gap_ms)
        state["last_tick_ts_us"] = tick_ts_us
        state["is_rth"] = int(is_rth)

        if tick.last is not None and float(tick.last) > 0:
            state["trade_count"] = int(state.get("trade_count", 0) or 0) + 1
            state["last_trade_ts_us"] = tick_ts_us
        if tick.bid is not None and tick.ask is not None and float(tick.bid) > 0 and float(tick.ask) > 0:
            state["quote_count"] = int(state.get("quote_count", 0) or 0) + 1
            state["last_quote_ts_us"] = tick_ts_us

    def _flush_tick_summary(self, symbol: str) -> None:
        sym = str(symbol or "").strip().upper()
        state = self._tick_audit_by_symbol.get(sym)
        if not state:
            return
        minute_bucket = int(state.get("minute_bucket", 0) or 0)
        if minute_bucket <= 0:
            return
        bucket_dt = datetime.fromtimestamp(minute_bucket * 60, tz=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        self._emit_audit(
            domain="market",
            stage="tick_minute_summary",
            symbol=sym,
            status="ok",
            payload={
                "minute_et": bucket_dt.strftime("%Y-%m-%d %H:%M:00"),
                "trade_count": int(state.get("trade_count", 0) or 0),
                "quote_count": int(state.get("quote_count", 0) or 0),
                "last_trade_ts_us": state.get("last_trade_ts_us"),
                "last_quote_ts_us": state.get("last_quote_ts_us"),
                "last_tick_ts_us": state.get("last_tick_ts_us"),
                "max_gap_ms": round(float(state.get("max_gap_ms", 0.0) or 0.0), 3),
                "is_rth": int(state.get("is_rth", 0) or 0),
            },
        )
        self._tick_audit_by_symbol.pop(sym, None)

    def _flush_all_tick_summaries(self) -> None:
        for sym in list(self._tick_audit_by_symbol.keys()):
            self._flush_tick_summary(sym)

    async def _on_tick(self, tick: TickData):
        """
        Receive ticks from IB adapter and publish to Kafka (Phase 4 - Data Lake)

        Publishes:
        - Trades (last, last_size) → md.tick.trade.v1
        - Quotes (bid, ask, bid_size, ask_size) → md.tick.quote.v1
        """
        if not self._running:
            return
        is_rth_now = self._is_rth_time()
        if self.rth_only and not is_rth_now:
            return
        if not self.rth_only and self._current_session_phase() not in self.allowed_sessions:
            return

        ts_us = int(time.time() * 1_000_000)

        # Determine if current time is RTH (for proper data tagging)
        is_rth = 1 if is_rth_now else 0

        try:
            sym = tick.symbol
            self.aggregator.on_tick(
                symbol=sym,
                px=tick.last,
                size=tick.last_size,
                bid=tick.bid,
                ask=tick.ask,
                ts_ms=int((tick.ts_us or ts_us) / 1000),
            )
            self._record_tick_summary(tick, is_rth=is_rth)
            self._refresh_recovery_data_plane_heartbeat(symbol=sym)
            if not self.enable_tick_publisher:
                return

            # Publish trade tick if we have last price
            if tick.last is not None and tick.last > 0:
                trade_msg = {
                    "schema_v": 1,
                    "metadata": {
                        "ts_us": ts_us,
                        "producer": "market_data_service"
                    },
                    "data": {
                        "symbol": tick.symbol,
                        "ts_us": tick.ts_us,
                        "timestamp_us": tick.ts_us,
                        "price": tick.last,
                        "size": tick.last_size or 0,
                        "exchange": "SMART",
                        "is_rth": is_rth,
                        "source": "IBKR"
                    }
                }
                self._tick_queue_put_nowait(_KafkaPublishItem(topic=_TICK_TOPIC_TRADES, key=sym, value=trade_msg))

            # Publish quote tick if we have bid/ask
            # For VIX/VIXY, IB often does not provide bid/ask; synthesize from last
            qb = tick.bid
            qa = tick.ask
            if sym in ("VIX", "VIXY"):
                if (qb is None or qb <= 0) and (tick.last is not None and tick.last > 0):
                    qb = tick.last
                if (qa is None or qa <= 0) and (tick.last is not None and tick.last > 0):
                    qa = tick.last

            if qb is not None and qa is not None and qb > 0 and qa > 0:
                quote_msg = {
                    "schema_v": 1,
                    "metadata": {
                        "ts_us": ts_us,
                        "producer": "market_data_service"
                    },
                    "data": {
                        "symbol": sym,
                        "ts_us": tick.ts_us,
                        "timestamp_us": tick.ts_us,
                        "bid": qb,
                        "ask": qa,
                        "bid_size": tick.bid_size or 0,
                        "ask_size": tick.ask_size or 0,
                        "exchange": "SMART",
                        "is_rth": is_rth,
                        "source": "IBKR"
                    }
                }
                self._tick_queue_put_nowait(_KafkaPublishItem(topic=_TICK_TOPIC_QUOTES, key=sym, value=quote_msg))

        except Exception as e:
            logger.error("Failed to publish tick", symbol=tick.symbol, error=str(e), error_type=type(e).__name__, producer_exists=self.tick_producer is not None)
            self._emit_audit(
                domain="market",
                stage="tick_publish_error",
                severity="warning",
                symbol=getattr(tick, "symbol", None),
                status="error",
                reason="tick_publish_failed",
                payload={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "producer_exists": self.tick_producer is not None,
                },
            )

    def get_status(self) -> dict:
        """Get service status"""
        return {
            "running": self._running,
            "symbols": self.symbols,
            "ib_connected": self.ib_adapter.is_connected() if self.ib_adapter else False,
            "backfill_enabled": self.backfill is not None,
            "backfill_queue": self.backfill.q.qsize() if self.backfill else 0,
            "tick_fail_fast_enabled": self._tick_fail_fast_enabled,
            "tick_degraded_failures": self._tick_degraded_failure_count,
            "tick_fatal": self._fatal_error is not None,
        }

    def _current_session_phase(self) -> str:
        now = datetime.now(ZoneInfo("America/New_York"))
        time_decimal = now.hour + now.minute / 60.0 + now.second / 3600.0
        if time_decimal >= 20.0 or time_decimal < 3.833333:
            return "OVERNIGHT"
        if 4.0 <= time_decimal < 9.5:
            return "PREMARKET"
        if 9.5 <= time_decimal < 16.0:
            return "RTH"
        if 16.0 <= time_decimal < 20.0:
            return "AFTERHOURS"
        return "OFF"

    # ========== Dynamic Subscription API ==========

    async def subscribe_symbol(self, symbol: str) -> bool:
        """
        Dynamically subscribe to a new symbol's tick data.

        This will:
        1. Subscribe to IB tick data (reqMktData)
        2. Add to aggregator's symbol list
        3. Start publishing to Kafka (md.tick.trade.v1, md.tick.quote.v1)
        4. SFS-1s will automatically process the new ticks

        Args:
            symbol: Stock symbol to subscribe (e.g., 'AAPL', 'NVDA')

        Returns:
            bool: True if subscription successful, False otherwise

        Note:
            - Tick data will start flowing after 2-5 seconds (IBKR delay)
            - Subscription persists until restart or explicit unsubscribe
            - IBKR free accounts limited to ~100 concurrent subscriptions
        """
        if not self.ib_adapter:
            logger.error("IB adapter not available", symbol=symbol)
            return False

        symbol = str(symbol).strip().upper()
        if not symbol:
            logger.warning("Cannot subscribe empty symbol in market data service")
            return False

        # 1. Subscribe to IB tick data
        success = await self.ib_adapter.subscribe_symbol(symbol)

        if success:
            ib_connected = bool(self.ib_adapter and self.ib_adapter.is_connected())
            active_subscriptions = set(self.ib_adapter.get_subscribed_symbols())
            deferred = (symbol not in active_subscriptions) and (not ib_connected)

            # 2. Add to aggregator's symbol list (optional, aggregator auto-handles)
            if hasattr(self.aggregator, 'symbols') and symbol not in self.aggregator.symbols:
                self.aggregator.symbols.append(symbol)

            # 3. Add to service's symbol list
            if symbol not in self.symbols:
                self.symbols.append(symbol)

            log_fn = logger.warning if deferred else logger.info
            log_fn(
                "Symbol accepted by market data service",
                symbol=symbol,
                total_symbols=len(self.symbols),
                ib_subscriptions=len(active_subscriptions),
                ib_connected=ib_connected,
                deferred=deferred,
            )
        else:
            logger.warning("Failed to subscribe symbol in market data service", symbol=symbol)

        return success

    async def unsubscribe_symbol(self, symbol: str) -> bool:
        """
        Unsubscribe from a symbol's tick data.

        Args:
            symbol: Stock symbol to unsubscribe

        Returns:
            bool: True if unsubscription successful, False otherwise
        """
        if not self.ib_adapter:
            logger.error("IB adapter not available", symbol=symbol)
            return False

        success = await self.ib_adapter.unsubscribe_symbol(symbol)

        if success:
            # Clean up from local lists
            if symbol in self.symbols:
                self.symbols.remove(symbol)

            if hasattr(self.aggregator, 'symbols') and symbol in self.aggregator.symbols:
                self.aggregator.symbols.remove(symbol)

            logger.info("✅ Symbol removed from market data service",
                       symbol=symbol,
                       remaining_symbols=len(self.symbols))

        return success

    def get_subscribed_symbols(self) -> List[str]:
        """
        Get list of currently subscribed symbols.

        Returns:
            List[str]: List of subscribed stock symbols
        """
        if self.ib_adapter:
            return self.ib_adapter.get_subscribed_symbols()
        return []

    async def monitor_loop(self, interval_sec: int = 10):
        """Monitor service health"""
        while self._running:
            try:
                await asyncio.sleep(interval_sec)

                status = self.get_status()

                logger.info("=" * 80)
                logger.info("MARKET DATA SERVICE STATUS")
                logger.info("=" * 80)
                logger.info(f"  Running: {status['running']}")
                logger.info(f"  IB Connected: {status['ib_connected']}")
                logger.info(f"  Symbols: {', '.join(status['symbols'])}")

                if status['backfill_enabled']:
                    logger.info(f"  Backfill Queue: {status['backfill_queue']}")

                # Show last prices
                logger.info("")
                logger.info("  Last Prices:")
                for symbol in self.symbols:
                    price = self.ib_adapter.get_last_price(symbol)
                    if price:
                        logger.info(f"    {symbol}: ${price:.2f}")
                    else:
                        logger.info(f"    {symbol}: N/A")

                logger.info("=" * 80)

            except Exception as e:
                logger.error("Monitor loop error", error=str(e))


async def main():
    """Main entry point"""
    # Symbols to track
    symbols = ['AAPL', 'MSFT', 'GOOGL']

    # Create service
    service = MarketDataService(
        symbols=symbols,
        enable_backfill=True
    )

    # Setup signal handlers
    def signal_handler(sig):
        logger.info(f"Received signal {sig}")
        asyncio.create_task(service.stop())

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))

    try:
        # Start service
        await service.start()

        # Start monitor
        monitor_task = asyncio.create_task(service.monitor_loop(interval_sec=30))

        # Wait for stop
        while service._running:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error("Service error", error=str(e), exc_info=True)
    finally:
        await service.stop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete")
