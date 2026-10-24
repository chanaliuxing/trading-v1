"""
Ad-hoc test verifying daily backfill emission and writer ingestion path.

This does not require Kafka or ClickHouse. It tests that:
1) BackfillService._emit_bars handles naive NY datetimes with ZoneInfo and emits a payload.
2) ClickHouseBarWriter._record_to_row accepts the emitted payload for a 1d bar.
"""
import asyncio
import os
import sys
import types
from datetime import datetime
from types import SimpleNamespace

# Ensure repo root on path for direct module imports
sys.path.insert(0, os.getcwd())

# ---- Lightweight stubs to avoid importing heavy C extensions (aiokafka/clickhouse) ----
class _DummyLogger:
    def debug(self, *args, **kwargs):
        pass
    def info(self, *args, **kwargs):
        pass
    def warning(self, *args, **kwargs):
        pass
    def error(self, *args, **kwargs):
        pass

_logger_mod = types.ModuleType('libs.logger')
_logger_mod.get_logger = lambda name=None: _DummyLogger()
sys.modules['libs.logger'] = _logger_mod

_config_mod = types.ModuleType('libs.config')
_config_mod.config = types.SimpleNamespace(
    kafka_config={"bootstrap_servers": "dummy:9092"},
    ib_config={"host": "127.0.0.1", "port": 4002, "client_id": 7},
)
sys.modules['libs.config'] = _config_mod

_db_mod = types.ModuleType('libs.db')
_db_mod.ch_pool = types.SimpleNamespace(get_async_client=lambda: None)
sys.modules['libs.db'] = _db_mod

# aiokafka stubs
_aiokafka_mod = types.ModuleType('aiokafka')
class _DummyKafkaConsumer:
    def __init__(self, *args, **kwargs):
        pass
    async def start(self):
        pass
    async def stop(self):
        pass
    async def commit(self):
        pass
    def __aiter__(self):
        async def _gen():
            if False:
                yield None
        return _gen()

class _DummyKafkaProducer:
    def __init__(self, *args, **kwargs):
        self.client = None
    async def start(self):
        pass
    async def stop(self):
        pass
    async def send(self, topic, key=None, value=None):
        return types.SimpleNamespace(topic=topic, key=key, value=value)

_aiokafka_mod.AIOKafkaConsumer = _DummyKafkaConsumer
_aiokafka_mod.AIOKafkaProducer = _DummyKafkaProducer
sys.modules['aiokafka'] = _aiokafka_mod
_aiokafka_structs = types.ModuleType('aiokafka.structs')
_aiokafka_structs.ConsumerRecord = type('ConsumerRecord', (), {})
sys.modules['aiokafka.structs'] = _aiokafka_structs

# ib_insync stub to avoid external dependency
_ib_mod = types.ModuleType('ib_insync')
class _DummyIB:
    def __init__(self):
        pass
    def isConnected(self):
        return False
class _DummyBarData:
    pass
class _DummyStock:
    def __init__(self, *args, **kwargs):
        pass
class _DummyIndex:
    def __init__(self, *args, **kwargs):
        pass
_ib_mod.IB = _DummyIB
_ib_mod.BarData = _DummyBarData
_ib_mod.Stock = _DummyStock
_ib_mod.Index = _DummyIndex
sys.modules['ib_insync'] = _ib_mod

from services.market.backfill_service import BackfillService, NY_TZ  # type: ignore
from services.market.writer.ch_writer import ClickHouseBarWriter  # type: ignore


class DummyProducer:
    def __init__(self):
        self.sent = []

    async def send(self, topic, key=None, value=None):
        # Record send and return a dummy future/result
        self.sent.append((topic, key, value))
        return SimpleNamespace(topic=topic, key=key, value=value)


class DummyBar:
    def __init__(self, date: datetime, open=10.0, high=11.0, low=9.5, close=10.5, volume=12345):
        # Mimic ib_insync BarData minimal fields
        self.date = date
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


async def main():
    # Arrange: create BackfillService with dummy producer and 1d topic
    svc = BackfillService(kafka_bootstrap="dummy", bar_topic="md.bar.1d.ibkr")
    svc.producer = DummyProducer()

    # Create a naive NY-local datetime simulating IBKR daily bar timestamp
    # Example: 2025-10-30 00:00:00 (naive, NY local)
    naive_dt = datetime(2025, 10, 30, 0, 0, 0)
    bar = DummyBar(date=naive_dt)

    # Act: emit daily bar and capture payload
    await svc._emit_bars("AAPL", [bar], "1 day")

    assert svc.producer.sent, "No payloads were emitted"
    topic, key, payload = svc.producer.sent[0]

    print("Emit OK:", {
        "topic": topic,
        "key": key,
        "ts_start_us": payload["data"]["ts_start_us"],
        "ts_end_us": payload["data"]["ts_end_us"],
    })

    # Arrange: create writer and feed the emitted payload to _record_to_row
    writer = ClickHouseBarWriter(topic="md.bar.1d.ibkr", table_name="bars_1d_ibkr", group_id="test")
    # Construct a minimal message-like object
    msg = SimpleNamespace(value=payload)
    row = writer._record_to_row(msg)

    assert row is not None, "Writer dropped the 1d row"
    symbol, ts_start, ts_end = row[0], row[1], row[2]
    dur = (ts_end - ts_start).total_seconds()
    print("Writer OK:", {
        "symbol": symbol,
        "ts_start": ts_start.isoformat(),
        "ts_end": ts_end.isoformat(),
        "duration_sec": dur,
    })


if __name__ == "__main__":
    asyncio.run(main())
