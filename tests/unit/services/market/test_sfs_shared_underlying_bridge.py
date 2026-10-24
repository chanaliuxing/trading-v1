import asyncio
from unittest.mock import AsyncMock, MagicMock

from libs.dal.sfs_facade import SFS1sReader
from services.market.sfs_1s_service import SFS1sService, SecBucket
from strategies.shared_data import UnderlyingSharedDataPlane


def test_sfs_reader_falls_back_to_shared_underlying_plane(monkeypatch):
    plane = UnderlyingSharedDataPlane()
    plane.publish_underlying(
        symbol="QQQ",
        namespace="global",
        ts_epoch_s=1710000000.0,
        last_price=500.0,
        bar={
            "ts_epoch_s": 1710000000.0,
            "open": 499.0,
            "high": 501.0,
            "low": 498.0,
            "close": 500.0,
            "volume": 123.0,
            "vwap": 500.0,
        },
    )

    import libs.dal.sfs_facade as sfs_mod

    monkeypatch.setattr(sfs_mod, "shared_underlying_data_plane", lambda: plane)
    fake_redis = MagicMock()
    fake_redis.lrange = AsyncMock(return_value=[])
    fake_redis.lindex = AsyncMock(return_value=None)
    monkeypatch.setattr(sfs_mod.aioredis, "from_url", lambda *a, **k: fake_redis)

    reader = SFS1sReader(redis_url="redis://unused")
    rows = asyncio.run(reader.last_n("QQQ", 3))

    assert len(rows) == 1
    assert rows[0]["ts"] == 1710000000000
    assert float(rows[0]["close"] or 0.0) == 500.0


def test_sfs_service_flush_publishes_to_shared_underlying_plane(monkeypatch):
    plane = UnderlyingSharedDataPlane()
    observed = []

    import services.market.sfs_1s_service as sfs_mod

    class _FakeRedis:
        def register_script(self, _lua):
            async def _script(*, keys, args):
                return 2
            return _script

        async def close(self):
            return None

    class _FakeHistogram:
        def labels(self, **labels):
            observed.append(("labels", labels))
            return self

        def observe(self, value):
            observed.append(("observe", value))

    monkeypatch.setattr(sfs_mod, "shared_underlying_data_plane", lambda: plane)
    monkeypatch.setattr(sfs_mod, "SHARED_PLANE_PUBLISH_LATENCY_MS", _FakeHistogram())
    monkeypatch.setattr(sfs_mod.aioredis, "from_url", lambda *a, **k: _FakeRedis())
    svc = SFS1sService(redis_url="redis://unused")
    bucket = SecBucket(symbol="QQQ", ts_start_ms=1710000000000)
    bucket.on_trade(500.0, 10.0, 1710000000000)

    ok = asyncio.run(svc._flush_bucket_async(bucket))

    assert ok is True
    snap = plane.latest_snapshot(symbol="QQQ", namespace="global")
    assert snap is not None
    assert float(snap.last_price or 0.0) == 500.0
    assert ("labels", {"namespace": "global", "symbol": "QQQ"}) in observed
    assert any(kind == "observe" and value >= 0.0 for kind, value in observed)
