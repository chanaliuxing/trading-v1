# services/metrics/exporter.py
"""
Prometheus metrics exporter for trading system observability.
Exposes /metrics endpoint with key SLA and health metrics.
"""
from __future__ import annotations
import time
import asyncio
from typing import Callable, Awaitable, Optional, Dict

from prometheus_client import (
    Counter, Gauge, Histogram,
    start_http_server, CollectorRegistry, REGISTRY, make_asgi_app
)


# ============ Histogram Buckets (milliseconds) ============
MS_BUCKETS_FAST = (5, 10, 25, 50, 100, 150, 250, 500, 1000)                      # 0–1s
MS_BUCKETS_SLOW = (10, 25, 50, 100, 150, 250, 500, 1000, 2000, 5000, 10000)      # 0–10s
MS_BUCKETS_DB = (1, 2, 5, 10, 20, 50, 100, 200, 500)                            # DB writes


# ============ Metrics Definitions ============

# --- Service Health ---
service_up = Gauge(
    "service_up",
    "Service heartbeat: 1=up, 0=down",
    labelnames=("service",)
)

kafka_lag = Gauge(
    "kafka_topic_lag",
    "Kafka consumer lag (records behind the head)",
    labelnames=("group", "topic", "partition")
)

# --- Critical Latencies (milliseconds) ---
latency_tick_to_signal_ms = Histogram(
    "latency_tick_to_signal_ms",
    "End-to-end latency from market tick ingestion to signal (ms)",
    buckets=MS_BUCKETS_FAST
)

latency_signal_to_order_ms = Histogram(
    "latency_signal_to_order_ms",
    "End-to-end latency from signal emitted to order submitted (ms)",
    buckets=MS_BUCKETS_FAST
)

latency_order_to_fill_ms = Histogram(
    "latency_order_to_fill_ms",
    "End-to-end latency from order submitted to fill (ms)",
    buckets=MS_BUCKETS_SLOW
)

# --- Data Pipeline ---
clickhouse_write_latency_ms = Histogram(
    "clickhouse_write_latency_ms",
    "Batch write latency to ClickHouse (ms)",
    buckets=MS_BUCKETS_DB,
    labelnames=("table",)
)

# --- Business Metrics ---
orders_rejected_total = Counter(
    "orders_rejected_total",
    "Total rejected orders",
    labelnames=("code", "venue", "strategy")
)

errors_total = Counter(
    "errors_total",
    "Unhandled errors",
    labelnames=("service", "etype")
)

ibkr_connection_up = Gauge(
    "ibkr_connection_up",
    "IBKR connection state: 1=up, 0=down",
    labelnames=("channel",)
)

ibkr_disconnects_total = Counter(
    "ibkr_disconnects_total",
    "Total IBKR disconnect events by channel and classified reason",
    labelnames=("channel", "reason", "cause")
)

ibkr_probe_ok_timestamp = Gauge(
    "ibkr_probe_ok_timestamp",
    "Unix timestamp of last successful IBKR API probe",
    labelnames=("channel",)
)

ibkr_probe_latency_ms = Histogram(
    "ibkr_probe_latency_ms",
    "Latency of IBKR API probe requests in milliseconds",
    labelnames=("channel",),
    buckets=MS_BUCKETS_SLOW
)

ibkr_probe_failures_total = Counter(
    "ibkr_probe_failures_total",
    "Total failed IBKR API probes",
    labelnames=("channel", "reason")
)


# ============ Exposure Methods ============

def start_metrics_http_server(port: int = 9108, addr: str = "0.0.0.0") -> None:
    """
    Start /metrics HTTP server on independent thread.
    This is the simplest way to expose Prometheus metrics.

    Usage:
        from services.metrics.exporter import start_metrics_http_server
        start_metrics_http_server(port=9108)
    """
    start_http_server(port, addr=addr)


def get_asgi_metrics_app():
    """
    Return ASGI app for mounting to existing web framework.

    Usage with FastAPI:
        from fastapi import FastAPI
        from services.metrics.exporter import get_asgi_metrics_app

        app = FastAPI()
        app.mount("/metrics", get_asgi_metrics_app())
    """
    return make_asgi_app()


# ============ Timing Utilities ============

class MsTimer:
    """Context manager for measuring duration in milliseconds."""
    def __init__(self, hist: Histogram, labels: Optional[Dict[str, str]] = None):
        self.hist = hist
        self.labels = labels or {}
        self._start_ns = 0

    def __enter__(self):
        self._start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, exc_type, exc, tb):
        dur_ms = (time.perf_counter_ns() - self._start_ns) / 1_000_000
        if self.labels:
            self.hist.labels(**self.labels).observe(dur_ms)
        else:
            self.hist.observe(dur_ms)


def time_async(hist: Histogram, labels: Optional[Dict[str, str]] = None):
    """
    Decorator for measuring async function duration.

    Usage:
        @time_async(latency_signal_to_order_ms)
        async def route_signal(...):
            ...

        # With labels:
        @time_async(clickhouse_write_latency_ms, {"table": "positions"})
        async def write_positions(...):
            ...
    """
    labels = labels or {}

    def deco(fn: Callable[..., Awaitable]):
        async def wrapper(*args, **kwargs):
            start = time.perf_counter_ns()
            try:
                return await fn(*args, **kwargs)
            finally:
                dur_ms = (time.perf_counter_ns() - start) / 1_000_000
                if labels:
                    hist.labels(**labels).observe(dur_ms)
                else:
                    hist.observe(dur_ms)
        return wrapper
    return deco


# ============ Convenience APIs ============

def mark_service_up(service_name: str, up: bool = True) -> None:
    """Mark service as up (1) or down (0)."""
    service_up.labels(service_name).set(1 if up else 0)


def inc_rejected(code: str, venue: str, strategy: str) -> None:
    """Increment rejected order counter."""
    orders_rejected_total.labels(code, venue, strategy).inc()


def inc_error(service: str, etype: str) -> None:
    """Increment error counter."""
    errors_total.labels(service, etype).inc()


def observe_kafka_lag(group: str, topic: str, partition: int, lag_value: int) -> None:
    """Set Kafka lag gauge."""
    kafka_lag.labels(group, topic, str(partition)).set(lag_value)


def set_ibkr_connection_up(channel: str, up: bool) -> None:
    """Set IBKR channel connection state."""
    ibkr_connection_up.labels(channel).set(1 if up else 0)


def inc_ibkr_disconnect(channel: str, reason: str, cause: str) -> None:
    """Increment IBKR disconnect counter."""
    ibkr_disconnects_total.labels(channel, reason, cause).inc()


def mark_ibkr_probe_ok(channel: str, latency_ms: float) -> None:
    """Record successful IBKR probe timestamp and latency."""
    ibkr_probe_ok_timestamp.labels(channel).set(time.time())
    ibkr_probe_latency_ms.labels(channel).observe(float(latency_ms))


def inc_ibkr_probe_failure(channel: str, reason: str) -> None:
    """Increment failed IBKR probe counter."""
    ibkr_probe_failures_total.labels(channel, reason).inc()


async def observe_ch_batch(table: str, coro: Awaitable):
    """
    Wrap ClickHouse batch write with timing.

    Usage:
        await observe_ch_batch('positions_snapshots',
                               ch_writer.write_batch('positions_snapshots', rows))
    """
    start = time.perf_counter_ns()
    try:
        return await coro
    finally:
        dur_ms = (time.perf_counter_ns() - start) / 1_000_000
        clickhouse_write_latency_ms.labels(table).observe(dur_ms)
