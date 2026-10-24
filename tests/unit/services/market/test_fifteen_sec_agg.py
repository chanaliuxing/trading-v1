import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.market.aggregator.fifteen_sec_agg import BarBucket, FifteenSecondAggregator
import services.market.aggregator.fifteen_sec_agg as agg_mod


class _FakePipe:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def set(self, key, value):
        self._ops.append((key, value))

    async def execute(self):
        for key, value in self._ops:
            self._store[key] = str(value)


class _FakeRedis:
    def __init__(self):
        self._store = {}

    async def get(self, key):
        return self._store.get(key)

    def pipeline(self):
        return _FakePipe(self._store)

    async def close(self):
        return None


class _FakeProducer:
    def __init__(self, *args, **kwargs):
        self.started = False
        self.stopped = False
        self.sent = []

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, topic, key=None, value=None):
        self.sent.append((topic, key, value))


class _FlakyProducerOnce:
    def __init__(self):
        self.fail_once = True
        self.sent = []
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, topic, key=None, value=None):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("ProducerClosed")
        self.sent.append((topic, key, value))


def _patch_deps(monkeypatch):
    monkeypatch.setattr(agg_mod.aioredis, "from_url", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(agg_mod, "AIOKafkaProducer", _FakeProducer)


def test_emit_bar_returns_true_on_success(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FifteenSecondAggregator(symbols=["QQQ"])
    agg._running = True
    bucket = BarBucket(symbol="QQQ", ts_start_ms=0, ts_end_ms=15_000, bar_seq=1)
    bucket.on_trade(100.0, 1.0)

    ok = asyncio.run(agg._emit_bar(bucket))

    assert ok is True
    assert len(agg.producer.sent) == 1
    topic, key, msg = agg.producer.sent[0]
    assert topic == agg_mod.BAR_TOPIC
    assert key == "QQQ"
    assert msg["data"]["symbol"] == "QQQ"


def test_emit_bar_recovers_after_producer_closed(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FifteenSecondAggregator(symbols=["QQQ"])
    agg._running = True
    agg.producer = _FlakyProducerOnce()
    rebuilt = _FakeProducer()
    monkeypatch.setattr(agg, "_build_producer", lambda: rebuilt)

    bucket = BarBucket(symbol="QQQ", ts_start_ms=0, ts_end_ms=15_000, bar_seq=1)
    bucket.on_trade(100.0, 1.0)

    ok = asyncio.run(agg._emit_bar(bucket))

    assert ok is True
    assert rebuilt.started is True
    assert len(rebuilt.sent) == 1
    assert rebuilt.sent[0][0] == agg_mod.BAR_TOPIC


def test_flusher_requeues_bucket_when_emit_fails(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FifteenSecondAggregator(symbols=["QQQ"])
    agg._running = True
    win_start = agg_mod.floor_15s(int(agg_mod.time.time() * 1000))
    key = ("QQQ", win_start)
    bucket = BarBucket(symbol="QQQ", ts_start_ms=win_start, ts_end_ms=win_start + 15_000, bar_seq=1)
    bucket.on_trade(100.0, 1.0)
    agg.buckets[key] = bucket
    agg.emit_schedule[key] = int(agg_mod.time.time() * 1000) - 1

    async def _fail_emit(_bucket):
        agg._running = False
        return False

    monkeypatch.setattr(agg, "_emit_bar", _fail_emit)

    asyncio.run(agg._flusher_loop())

    assert key in agg.buckets
    assert key in agg.emit_schedule
