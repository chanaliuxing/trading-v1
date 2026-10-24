from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from services.market.backfill_service import BackfillService


class _FakeSharedIB:
    def __init__(self, connected: bool = True):
        self._connected = connected

    def isConnected(self):
        return self._connected


@pytest.mark.asyncio
async def test_connect_ib_uses_shared_market_session(monkeypatch):
    shared_ib = _FakeSharedIB(connected=True)

    class _FakeManager:
        def __init__(self):
            self.calls = []

        async def get_market_ib_handle(self, *, host=None, port=None):
            self.calls.append((host, port))
            return SimpleNamespace(ib=shared_ib, epoch=1)

    fake_manager = _FakeManager()
    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: fake_manager,
    )

    svc = BackfillService(ib=None, kafka_bootstrap="localhost:9092")
    await svc._connect_ib()

    assert svc.ib is shared_ib
    assert fake_manager.calls == [(svc._ib_host, svc._ib_port)]


@pytest.mark.asyncio
async def test_attempt_reconnect_reattaches_shared_market_session(monkeypatch):
    first_ib = _FakeSharedIB(connected=False)
    shared_ib = _FakeSharedIB(connected=True)

    class _FakeManager:
        def __init__(self):
            self.calls = 0

        async def get_market_ib_handle(self, *, host=None, port=None):
            self.calls += 1
            return SimpleNamespace(ib=shared_ib, epoch=1)

    fake_manager = _FakeManager()
    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: fake_manager,
    )

    svc = BackfillService(ib=None, kafka_bootstrap="localhost:9092")
    svc.ib = first_ib

    ok = await svc._attempt_reconnect(worker_idx=1, max_retries=1)

    assert ok is True
    assert svc.ib is shared_ib
    assert fake_manager.calls == 1
