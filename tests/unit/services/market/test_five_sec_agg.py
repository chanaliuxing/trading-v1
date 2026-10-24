import asyncio
from dataclasses import dataclass
import sys
from pathlib import Path
import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.market.aggregator.five_sec_agg import BarBucket, FiveSecondAggregator
import services.market.aggregator.five_sec_agg as agg_mod


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
        self.closed = False

    async def get(self, key):
        return self._store.get(key)

    def pipeline(self):
        return _FakePipe(self._store)

    async def close(self):
        self.closed = True


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
        self.started = False
        self.stopped = False
        self.sent = []
        self.fail_once = True

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, topic, key=None, value=None):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("ProducerClosed")
        self.sent.append((topic, key, value))


class _SlowStopProducer(_FakeProducer):
    async def stop(self):
        await asyncio.sleep(10)


@dataclass
class _Contract:
    symbol: str


@dataclass
class _Ticker:
    contract: _Contract
    last: float
    lastSize: float
    bid: float | None = None
    ask: float | None = None


def _patch_deps(monkeypatch):
    monkeypatch.setattr(agg_mod.aioredis, "from_url", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(agg_mod, "AIOKafkaProducer", _FakeProducer)


def test_barbucket_vwap_calculation():
    bucket = BarBucket(symbol="QQQ", ts_start_ms=0, ts_end_ms=5000, bar_seq=1)
    bucket.on_trade(100.0, 2.0)
    bucket.on_trade(101.0, 1.0)

    msg = bucket.to_bar_message()
    data = msg["data"]
    assert data["open"] == 100.0
    assert data["high"] == 101.0
    assert data["low"] == 100.0
    assert data["close"] == 101.0
    assert data["volume"] == 3.0
    assert data["trades"] == 2
    assert data["vwap"] == 100.3333


def test_aggregator_ingest_creates_bucket(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)

    tickers = [
        _Ticker(contract=_Contract("QQQ"), last=100.0, lastSize=2.0),
        _Ticker(contract=_Contract("QQQ"), last=101.0, lastSize=1.0),
    ]
    asyncio.run(agg._ingest_tickers(tickers))

    assert len(agg.buckets) == 1
    bucket = next(iter(agg.buckets.values()))
    assert bucket.bar_seq == 1
    assert bucket.open == 100.0
    assert bucket.high == 101.0
    assert bucket.low == 100.0
    assert bucket.close == 101.0
    assert bucket.volume == 3.0
    assert bucket.trades == 2


def test_aggregator_gap_detection_emits_seq_and_time_gap(monkeypatch):
    _patch_deps(monkeypatch)
    monkeypatch.setattr(agg_mod, "is_rth", lambda _epoch_sec: True)
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)
    agg.last_emitted_seq["QQQ"] = 1
    agg.last_emitted_ts["QQQ"] = 1_000

    events = []

    async def _capture_emit_gap(symbol, reason, **kwargs):
        events.append((symbol, reason, kwargs))

    monkeypatch.setattr(agg, "_emit_gap", _capture_emit_gap)
    bucket = BarBucket(symbol="QQQ", ts_start_ms=20_000, ts_end_ms=25_000, bar_seq=3)
    asyncio.run(agg._check_and_emit_gap(bucket))

    reasons = [event[1] for event in events]
    assert "SEQ_GAP" in reasons
    assert "TIME_GAP" in reasons


def test_aggregator_emit_bar_sends_to_kafka(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)
    bucket = BarBucket(symbol="QQQ", ts_start_ms=0, ts_end_ms=5000, bar_seq=1)
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
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)
    agg._running = True
    agg.producer = _FlakyProducerOnce()

    rebuilt = _FakeProducer()
    monkeypatch.setattr(agg, "_build_producer", lambda: rebuilt)

    bucket = BarBucket(symbol="QQQ", ts_start_ms=0, ts_end_ms=5000, bar_seq=1)
    bucket.on_trade(100.0, 1.0)

    ok = asyncio.run(agg._emit_bar(bucket))

    assert ok is True
    assert rebuilt.started is True
    assert len(rebuilt.sent) == 1
    assert rebuilt.sent[0][0] == agg_mod.BAR_TOPIC


@pytest.mark.asyncio
async def test_stop_defers_producer_close_after_timeout(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)
    slow = _SlowStopProducer()
    agg.producer = slow

    await agg.stop()

    assert slow in agg._retired_producers
    assert len(agg._producer_stop_tasks) == 1
    task = next(iter(agg._producer_stop_tasks))
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_flusher_requeues_bucket_on_emit_failure(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)
    agg._running = True

    win_start = agg_mod.floor_5s(int(agg_mod.time.time() * 1000))
    key = ("QQQ", win_start)
    bucket = BarBucket(symbol="QQQ", ts_start_ms=win_start, ts_end_ms=win_start + 5000, bar_seq=1)
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


def test_aggregator_rejects_trade_far_from_quote_when_filter_enabled(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FiveSecondAggregator(
        symbols=["QQQ"],
        include_ah=True,
        enable_trade_sanity_filter=True,
        trade_max_deviation_from_quote_pct=0.02,
    )

    tickers = [_Ticker(contract=_Contract("QQQ"), last=5.29, lastSize=10.0, bid=587.9, ask=588.0)]
    asyncio.run(agg._ingest_tickers(tickers))

    assert len(agg.buckets) == 0
    assert agg.rejected_trade_counts[("QQQ", "TOO_FAR_FROM_QUOTE")] == 1


def test_aggregator_accepts_ah_last_without_size_for_ohlc(monkeypatch):
    _patch_deps(monkeypatch)
    monkeypatch.setattr(agg_mod, "is_rth", lambda _epoch_sec: False)
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)

    tickers = [_Ticker(contract=_Contract("QQQ"), last=587.63, lastSize=0.0)]
    asyncio.run(agg._ingest_tickers(tickers))

    assert len(agg.buckets) == 1
    bucket = next(iter(agg.buckets.values()))
    assert bucket.open == 587.63
    assert bucket.close == 587.63
    assert bucket.volume == 0.0
    assert bucket.trades == 1


def test_aggregator_keeps_rth_last_without_size_blocked(monkeypatch):
    _patch_deps(monkeypatch)
    monkeypatch.setattr(agg_mod, "is_rth", lambda _epoch_sec: True)
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)

    tickers = [_Ticker(contract=_Contract("QQQ"), last=587.63, lastSize=0.0)]
    asyncio.run(agg._ingest_tickers(tickers))

    assert len(agg.buckets) == 0


@pytest.mark.asyncio
async def test_aggregator_on_tick_accepts_ah_last_without_size(monkeypatch):
    _patch_deps(monkeypatch)
    monkeypatch.setattr(agg_mod, "is_rth", lambda _epoch_sec: False)
    agg = FiveSecondAggregator(symbols=["QQQ"], include_ah=True)

    agg.on_tick(symbol="QQQ", px=587.63, size=None, bid=587.62, ask=587.64, ts_ms=1_700_000_000_000)
    await asyncio.sleep(0)

    assert len(agg.buckets) == 1
    bucket = next(iter(agg.buckets.values()))
    assert bucket.close == 587.63
    assert bucket.volume == 0.0
    assert bucket.trades == 1


def test_aggregator_rejects_trade_far_from_last_good_when_filter_enabled(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FiveSecondAggregator(
        symbols=["QQQ"],
        include_ah=True,
        enable_trade_sanity_filter=True,
        trade_max_jump_from_last_pct=0.05,
    )
    agg.last_good_trade_px["QQQ"] = 588.0

    tickers = [_Ticker(contract=_Contract("QQQ"), last=620.0, lastSize=10.0, bid=None, ask=None)]
    asyncio.run(agg._ingest_tickers(tickers))

    assert len(agg.buckets) == 0
    assert agg.rejected_trade_counts[("QQQ", "TOO_FAR_FROM_LAST_GOOD_TRADE")] == 1


def test_emit_bar_quarantines_anomalous_bar_when_filter_enabled(monkeypatch):
    _patch_deps(monkeypatch)
    agg = FiveSecondAggregator(
        symbols=["QQQ"],
        include_ah=True,
        enable_bar_sanity_filter=True,
        bar_max_deviation_pct=0.35,
        bar_max_range_pct=0.20,
    )
    agg.last_good_trade_px["QQQ"] = 588.0
    bucket = BarBucket(symbol="QQQ", ts_start_ms=0, ts_end_ms=5000, bar_seq=1)
    bucket.open = 588.03
    bucket.high = 588.04
    bucket.low = 0.06
    bucket.close = 5.29
    bucket.volume = 100.0
    bucket.trades = 4

    ok = asyncio.run(agg._emit_bar(bucket))

    assert ok is True
    assert len(agg.producer.sent) == 0
    assert len(agg.quarantined_bars) == 1
    assert agg.quarantined_bars[0]["reason"] in {"BAR_TOO_FAR_FROM_REF", "BAR_RANGE_TOO_WIDE"}
