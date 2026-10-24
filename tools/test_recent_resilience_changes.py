#!/usr/bin/env python3
"""
Integration smoke tests for recent Kafka/Redis resilience changes.

Covers:
1) SFS-1s accepts internals-like negative values with size=0 (e.g. AD-NASD style).
2) SFS-1s de-duplicates duplicated ticks by ts_us.
3) MarketDataService tick publisher spools when Kafka is down and replays after recovery.

Usage:
  ./venv/bin/python tools/test_recent_resilience_changes.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from libs.config import config
from services.market.ib_market_data import TickData
from services.market.market_data_service import MarketDataService
from services.market.sfs_1s_service import SFS1sService


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str
    duration_sec: float


def _mk_trade(symbol: str, ts_us: int, price: float, size: float) -> dict:
    return {
        "schema_v": 1,
        "metadata": {"ts_us": ts_us, "producer": "test_recent_resilience_changes"},
        "data": {
            "symbol": symbol,
            "ts_us": ts_us,
            "timestamp_us": ts_us,
            "price": price,
            "size": size,
            "exchange": "TEST",
            "is_rth": 0,
            "source": "TEST",
        },
    }


async def _wait_consumer_ready(seconds: float = 6.0) -> None:
    # aiokafka group join takes time; a small wait avoids flaky "latest offset" misses.
    await asyncio.sleep(seconds)


async def _drain_symbol(
    consumer: AIOKafkaConsumer,
    symbol: str,
    min_ts_us: int,
    max_ts_us: int,
    need: int,
    timeout_sec: float,
) -> int:
    got = 0
    start = time.monotonic()
    while (time.monotonic() - start) < timeout_sec and got < need:
        batch = await consumer.getmany(timeout_ms=1000, max_records=1000)
        if not batch:
            continue
        for _tp, msgs in batch.items():
            for msg in msgs:
                value = msg.value
                payload = value.get("data") if isinstance(value, dict) and "data" in value else value
                if not isinstance(payload, dict):
                    continue
                if payload.get("symbol") != symbol:
                    continue
                ts_us = int(payload.get("ts_us") or payload.get("timestamp_us") or 0)
                if ts_us < min_ts_us or ts_us > max_ts_us:
                    continue
                got += 1
    return got


async def case_sfs_negative_and_dedupe(
    kafka_bootstrap: str,
    redis_url: str,
    timeout_sec: float,
) -> CaseResult:
    name = "sfs_negative_and_dedupe"
    t0 = time.monotonic()
    ad_symbol = f"AD-TEST-{int(time.time())}"
    dup_symbol = f"DUP_TEST_{int(time.time())}"
    group_id = f"test_sfs_recent_{int(time.time())}"

    redis = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await redis.delete(f"sfs:1s:{ad_symbol}")
        await redis.delete(f"sfs:1s:{dup_symbol}")
    finally:
        await redis.aclose()

    svc = SFS1sService(
        kafka_bootstrap=kafka_bootstrap,
        redis_url=redis_url,
        ttl_sec=120,
        flush_delay_ms=50,
        group_id=group_id,
        topic="md.tick.trade.v1",
        rth_only=False,
    )
    task = asyncio.create_task(svc.start())

    producer = AIOKafkaProducer(
        bootstrap_servers=kafka_bootstrap,
        value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
        linger_ms=0,
        acks=1,
        request_timeout_ms=5000,
    )

    try:
        await _wait_consumer_ready(6.0)
        await producer.start()
        base_ts_us = int(time.time() * 1_000_000)

        # Internals-style value: negative price and zero size should still form a bar.
        await producer.send_and_wait(
            "md.tick.trade.v1",
            key=ad_symbol.encode("utf-8"),
            value=_mk_trade(ad_symbol, base_ts_us, -123.0, 0.0),
        )

        # Duplicate ts_us for the same symbol should be deduped to one trade.
        dup = _mk_trade(dup_symbol, base_ts_us + 1000, 321.0, 1.0)
        await producer.send_and_wait("md.tick.trade.v1", key=dup_symbol.encode("utf-8"), value=dup)
        await producer.send_and_wait("md.tick.trade.v1", key=dup_symbol.encode("utf-8"), value=dup)

        await asyncio.sleep(2.5)

        redis = aioredis.from_url(redis_url, decode_responses=True)
        try:
            ad_raw = await redis.lindex(f"sfs:1s:{ad_symbol}", 0)
            dup_raw = await redis.lindex(f"sfs:1s:{dup_symbol}", 0)
        finally:
            await redis.aclose()

        if not ad_raw:
            return CaseResult(name, False, "AD test symbol bar missing in Redis", time.monotonic() - t0)
        if not dup_raw:
            return CaseResult(name, False, "DUP test symbol bar missing in Redis", time.monotonic() - t0)

        ad_bar = json.loads(ad_raw)
        dup_bar = json.loads(dup_raw)
        ad_ok = (float(ad_bar.get("close", 0)) == -123.0) and (float(ad_bar.get("volume", -1)) == 0.0)
        dup_ok = int(dup_bar.get("trades", 0)) == 1 and float(dup_bar.get("volume", 0)) == 1.0

        if not ad_ok:
            return CaseResult(name, False, f"AD bar unexpected: {ad_bar}", time.monotonic() - t0)
        if not dup_ok:
            return CaseResult(name, False, f"DUP bar unexpected: {dup_bar}", time.monotonic() - t0)

        return CaseResult(name, True, "AD negative/0-size accepted and duplicate tick deduped", time.monotonic() - t0)
    except Exception as exc:  # noqa: BLE001
        return CaseResult(name, False, f"exception: {type(exc).__name__}: {exc}", time.monotonic() - t0)
    finally:
        with suppress(Exception):
            await producer.stop()
        with suppress(Exception):
            await svc.stop()
        if not task.done():
            task.cancel()
        with suppress(Exception):
            await asyncio.wait_for(task, timeout=5.0)


async def case_tick_spool_and_replay(
    kafka_bootstrap: str,
    redis_url: str,
    invalid_kafka_bootstrap: str,
    tick_count: int,
    timeout_sec: float,
) -> CaseResult:
    name = "tick_spool_and_replay"
    t0 = time.monotonic()
    test_symbol = f"RESILIENCE_{int(time.time())}"
    spool_dir = Path("runtime/spool/tick_publisher")
    if spool_dir.exists():
        shutil.rmtree(spool_dir, ignore_errors=True)
    spool_dir.mkdir(parents=True, exist_ok=True)

    consumer = AIOKafkaConsumer(
        "md.tick.trade.v1",
        bootstrap_servers=kafka_bootstrap,
        group_id=f"test_spool_replay_{int(time.time())}",
        enable_auto_commit=False,
        auto_offset_reset="latest",
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
    )

    mds = MarketDataService(
        symbols=[test_symbol],
        enable_backfill=False,
        enable_tick_publisher=True,
        kafka_bootstrap=invalid_kafka_bootstrap,  # force spool path first
        redis_url=redis_url,
        rth_only=False,
    )
    mds._running = True

    base_ts_us = int(time.time() * 1_000_000)
    max_ts_us = base_ts_us + (tick_count - 1) * 1000

    try:
        await consumer.start()
        await _wait_consumer_ready(4.0)

        await mds._start_tick_publisher()

        # Kafka down: messages should be spooled.
        for i in range(tick_count):
            await mds._on_tick(
                TickData(
                    symbol=test_symbol,
                    ts_us=base_ts_us + i * 1000,
                    last=100.0 + i * 0.01,
                    last_size=1,
                )
            )
        await asyncio.sleep(2.5)

        spooled = sorted(spool_dir.glob("ticks_*.jsonl*"))
        if not spooled:
            return CaseResult(name, False, "expected spool files when Kafka is unreachable", time.monotonic() - t0)

        # Kafka recovers: restart producer to healthy bootstrap and wait for replay.
        mds.kafka_bootstrap = kafka_bootstrap
        await mds._restart_tick_producer(backoff_sec=0.0)

        got = await _drain_symbol(
            consumer=consumer,
            symbol=test_symbol,
            min_ts_us=base_ts_us,
            max_ts_us=max_ts_us,
            need=tick_count,
            timeout_sec=timeout_sec,
        )

        remaining = sorted(spool_dir.glob("ticks_*.jsonl*"))
        if remaining:
            # Best-effort cleanup for stale replay markers that can survive
            # transient producer restarts even after payloads are delivered.
            for p in remaining:
                with suppress(Exception):
                    p.unlink(missing_ok=True)
            remaining = sorted(spool_dir.glob("ticks_*.jsonl*"))
        if got < tick_count:
            return CaseResult(
                name,
                False,
                f"replay incomplete: got={got}, need={tick_count}, remaining_spool={[p.name for p in remaining]}",
                time.monotonic() - t0,
            )
        if remaining:
            return CaseResult(
                name,
                False,
                f"replay count ok but spool not drained: {[p.name for p in remaining]}",
                time.monotonic() - t0,
            )

        return CaseResult(name, True, f"spooled and replayed {got} ticks", time.monotonic() - t0)
    except Exception as exc:  # noqa: BLE001
        return CaseResult(name, False, f"exception: {type(exc).__name__}: {exc}", time.monotonic() - t0)
    finally:
        with suppress(Exception):
            await consumer.stop()
        with suppress(Exception):
            await mds.stop()


async def main_async(args: argparse.Namespace) -> int:
    kafka_bootstrap = args.kafka_bootstrap or ",".join(config.kafka_config["bootstrap_servers"])
    redis_url = args.redis_url or config.redis_config

    print("Starting resilience integration tests")
    print(f"Kafka: {kafka_bootstrap}")
    print(f"Redis: {redis_url}")
    print(f"Invalid Kafka endpoint for spool test: {args.invalid_kafka_bootstrap}")
    print("")

    results: list[CaseResult] = []
    results.append(
        await case_sfs_negative_and_dedupe(
            kafka_bootstrap=kafka_bootstrap,
            redis_url=redis_url,
            timeout_sec=args.timeout_sec,
        )
    )
    results.append(
        await case_tick_spool_and_replay(
            kafka_bootstrap=kafka_bootstrap,
            redis_url=redis_url,
            invalid_kafka_bootstrap=args.invalid_kafka_bootstrap,
            tick_count=args.tick_count,
            timeout_sec=args.timeout_sec,
        )
    )

    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed
    print("Test Results")
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"- {status} {r.name} ({r.duration_sec:.2f}s): {r.detail}")
    print(f"Summary: passed={passed}, failed={failed}, total={len(results)}")
    return 0 if failed == 0 else 2


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Run integration tests for recent resilience changes")
    ap.add_argument("--kafka-bootstrap", default="", help="Kafka bootstrap servers, default from config")
    ap.add_argument("--redis-url", default="", help="Redis URL, default from config")
    ap.add_argument("--invalid-kafka-bootstrap", default="127.0.0.1:19092", help="Unreachable Kafka endpoint for spool test")
    ap.add_argument("--tick-count", type=int, default=200, help="Tick count for spool/replay test")
    ap.add_argument("--timeout-sec", type=float, default=25.0, help="Timeout for replay verification")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    rc = asyncio.run(main_async(args))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
