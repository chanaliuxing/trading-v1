import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from libs.strategy.components.history_provider import HistoryProvider


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings = []

    def warning(self, event, **kwargs):
        self.warnings.append((event, kwargs))


class _FakeContext:
    def __init__(self, logger):
        self.log = logger
        self.data_unavailable = []

    def report_data_unavailable(self, payload):
        self.data_unavailable.append(payload)


class _FakeClient:
    def __init__(self, rows=None, exc=None) -> None:
        self.rows = rows or []
        self.exc = exc
        self.calls = []

    def execute(self, query, parameters=None):
        self.calls.append((query, parameters))
        if self.exc is not None:
            raise self.exc
        return self.rows


@pytest.mark.asyncio
async def test_history_provider_clickhouse_fallback_uses_sync_execute_in_thread(monkeypatch) -> None:
    client = _FakeClient(
        rows=[(datetime(2026, 5, 1, 9, 30), 1.0, 2.0, 0.5, 1.5, 100)]
    )
    monkeypatch.setattr("libs.db.ch_pool.get_client", lambda: client)
    logger = _FakeLogger()
    provider = HistoryProvider(ctx=SimpleNamespace(log=logger), state={}, state_lock=asyncio.Lock())

    bars, source, count = await provider.fetch_local_db_1m_bars(
        "qqq",
        datetime(2026, 5, 1, 9, 30),
        datetime(2026, 5, 1, 9, 31),
    )

    assert source == "ch_reconstructed"
    assert count == 1
    assert bars[0].open == 1.0
    assert client.calls[0][1]["symbol"] == "QQQ"


@pytest.mark.asyncio
async def test_history_provider_clickhouse_error_is_logged(monkeypatch) -> None:
    client = _FakeClient(exc=RuntimeError("driver down"))
    monkeypatch.setattr("libs.db.ch_pool.get_client", lambda: client)
    logger = _FakeLogger()
    state = {}
    ctx = _FakeContext(logger)
    provider = HistoryProvider(ctx=ctx, state=state, state_lock=asyncio.Lock())

    bars, source, count = await provider.fetch_local_db_5m_bars(
        "QQQ",
        datetime(2026, 5, 1, 9, 30),
        datetime(2026, 5, 1, 9, 35),
    )

    assert bars == []
    assert source == "ch_error"
    assert count == 0
    assert logger.warnings
    assert logger.warnings[0][0] == "clickhouse_history_fetch_failed"
    assert state["data_integrity_warning"] is True
    assert state["data_unavailable_gate"]["source"] == "clickhouse"
    assert ctx.data_unavailable[0]["reason"] == "clickhouse_history_fetch_failed"
