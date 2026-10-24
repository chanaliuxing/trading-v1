import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.market.sfs_1s_service import SFS1sService, SecBucket
import services.market.sfs_1s_service as sfs_mod


class _FakeRedis:
    def __init__(self, fail_first_n: int = 0):
        self.closed = False
        self.calls = 0
        self.fail_first_n = fail_first_n

    def register_script(self, _lua: str):
        async def _script(*, keys, args):
            self.calls += 1
            if self.calls <= self.fail_first_n:
                raise asyncio.TimeoutError("redis timeout")
            return 2

        return _script

    async def close(self):
        self.closed = True


def _patch_fake_redis(monkeypatch, redis_obj: _FakeRedis):
    monkeypatch.setattr(sfs_mod.aioredis, "from_url", lambda *a, **k: redis_obj)


def test_sfs_internal_negative_and_dedupe(monkeypatch):
    fake_redis = _FakeRedis()
    _patch_fake_redis(monkeypatch, fake_redis)
    svc = SFS1sService(redis_url="redis://unused", rth_only=False)

    ts_us = int(time.time() * 1_000_000)
    internal = "AD-NASD"

    svc._on_trade_msg({"symbol": internal, "price": -50.0, "size": 0.0, "ts_us": ts_us})
    svc._on_trade_msg({"symbol": internal, "price": -60.0, "size": 1.0, "ts_us": ts_us})  # duplicate
    svc._on_trade_msg({"symbol": internal, "price": -40.0, "size": 0.0, "ts_us": ts_us + 1000})
    svc._on_trade_msg({"symbol": "QQQ", "price": -1.0, "size": 1.0, "ts_us": ts_us + 2000})

    bucket = svc._buckets[internal]
    assert bucket.open == -50.0
    assert bucket.high == -40.0
    assert bucket.low == -50.0
    assert bucket.close == -40.0
    assert bucket.trades == 2
    assert bucket.volume == 0.0
    assert "QQQ" not in svc._buckets


def test_sfs_flush_timeout_trips_breaker(monkeypatch):
    fake_redis = _FakeRedis(fail_first_n=1)
    _patch_fake_redis(monkeypatch, fake_redis)
    svc = SFS1sService(redis_url="redis://unused")
    svc._redis_breaker_threshold = 1
    svc._redis_breaker_base_sec = 1.0
    svc._redis_breaker_max_sec = 1.0

    bucket = SecBucket(symbol="QQQ", ts_start_ms=1_700_000_000_000)
    bucket.on_trade(100.0, 1.0, bucket.ts_start_ms)

    ok = asyncio.run(svc._flush_bucket_async(bucket))
    assert ok is False
    assert svc._redis_disabled is True
    assert svc._redis_retry_at_ms > 0


def test_sfs_flush_recovery_resets_streak(monkeypatch):
    fake_redis = _FakeRedis(fail_first_n=1)
    _patch_fake_redis(monkeypatch, fake_redis)
    svc = SFS1sService(redis_url="redis://unused")

    bucket = SecBucket(symbol="QQQ", ts_start_ms=1_700_000_005_000)
    bucket.on_trade(101.0, 2.0, bucket.ts_start_ms)

    first = asyncio.run(svc._flush_bucket_async(bucket))
    second = asyncio.run(svc._flush_bucket_async(bucket))

    assert first is False
    assert second is True
    assert svc._redis_error_streak == 0
