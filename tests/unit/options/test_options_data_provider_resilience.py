import asyncio
from types import SimpleNamespace

import pytest

from services.market.options_data import IBKROptionsDataProvider


class _FakeConnManager:
    def __init__(self, rejected=False, revalidate_required=False, connect_exc=None):
        self.rejected = rejected
        self.revalidate_required_flag = revalidate_required
        self.revalidated = []
        self.connect_exc = connect_exc

    def was_market_req_rejected(self, req_id, *, error_codes=(354,)):
        return self.rejected

    def market_revalidation_required(self):
        return self.revalidate_required_flag

    def mark_market_revalidated(self, *, source, ok, detail=None):
        self.revalidated.append({"source": source, "ok": ok, "detail": detail})
        if ok:
            self.revalidate_required_flag = False

    async def get_market_ib(self, *, host=None, port=None):
        if self.connect_exc is not None:
            raise self.connect_exc
        return _FakeIBConnected()


class _FakeIB:
    def __init__(self):
        self.cancelled = []
        self.qualified = []

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False):
        return SimpleNamespace(tickerId=getattr(contract, "tickerId", 1), bid=1.0, ask=1.1, last=1.05)

    def cancelMktData(self, contract):
        self.cancelled.append(contract)

    async def qualifyContractsAsync(self, *contracts):
        self.qualified.extend(contracts)
        return list(contracts)


class _FakeIBConnected(_FakeIB):
    def isConnected(self):
        return True


@pytest.mark.asyncio
async def test_req_mktdata_batch_skips_cancel_for_rejected_req():
    provider = IBKROptionsDataProvider()
    provider.ib = _FakeIB()
    provider._conn_manager = _FakeConnManager(rejected=True)
    provider._req_mktdata_spacing_sec = 0.0

    contract = SimpleNamespace(symbol="QQQ", tickerId=57)
    tickers = await provider._req_mktdata_batch([contract], wait_sec=0.0)

    assert len(tickers) == 1
    assert provider.ib.cancelled == []


@pytest.mark.asyncio
async def test_req_mktdata_batch_cancels_active_req():
    provider = IBKROptionsDataProvider()
    provider.ib = _FakeIB()
    provider._conn_manager = _FakeConnManager(rejected=False)
    provider._req_mktdata_spacing_sec = 0.0

    contract = SimpleNamespace(symbol="QQQ", tickerId=58)
    await provider._req_mktdata_batch([contract], wait_sec=0.0)

    assert provider.ib.cancelled == [contract]


@pytest.mark.asyncio
async def test_req_mktdata_batch_skips_contracts_in_reject_cooldown(monkeypatch):
    provider = IBKROptionsDataProvider()
    provider.ib = _FakeIB()
    provider._conn_manager = _FakeConnManager(rejected=False)
    provider._req_mktdata_spacing_sec = 0.0
    provider._rejected_contract_cooldown_sec = 300.0

    contract = SimpleNamespace(
        symbol="QQQ",
        secType="OPT",
        localSymbol="QQQ   260422C00650000",
        lastTradeDateOrContractMonth="20260422",
        strike=650.0,
        right="C",
        exchange="SMART",
        tradingClass="QQQ",
        tickerId=59,
    )
    provider._mark_contract_rejected(contract, now_ts=1_000.0)
    monkeypatch.setattr("services.market.options_data.time.time", lambda: 1_010.0)

    tickers = await provider._req_mktdata_batch([contract], wait_sec=0.0)

    assert tickers == [None]
    assert provider.ib.cancelled == []


def test_chain_watchdog_clears_caches_after_repeated_empty_results(monkeypatch):
    provider = IBKROptionsDataProvider()
    key = ("QQQ", "0", "C")
    provider._chain_cache[key] = (9999999999.0, [], [])
    provider._chain_price_cache[key] = (9999999999.0, [])

    t = {"now": 1_000_000.0}
    monkeypatch.setattr("services.market.options_data.time.time", lambda: t["now"])

    provider._update_chain_watchdog(key, result_count=0)
    t["now"] += 60.0
    provider._update_chain_watchdog(key, result_count=0)
    t["now"] += 60.0
    provider._update_chain_watchdog(key, result_count=0)

    assert key not in provider._chain_price_cache
    assert key not in provider._chain_cache


@pytest.mark.asyncio
async def test_validate_market_session_marks_revalidated_when_price_available():
    provider = IBKROptionsDataProvider()
    provider.ib = _FakeIB()
    provider._conn_manager = _FakeConnManager(revalidate_required=True)
    provider._req_mktdata_spacing_sec = 0.0

    await provider._validate_market_session_if_needed("QQQ")

    assert provider._conn_manager.revalidated
    assert provider._conn_manager.revalidated[-1]["ok"] is True


@pytest.mark.asyncio
async def test_ensure_connected_requires_shared_market_session(monkeypatch):
    provider = IBKROptionsDataProvider(allow_direct_fallback=False)
    fake_manager = _FakeConnManager(connect_exc=RuntimeError("shared unavailable"))
    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: fake_manager,
    )

    with pytest.raises(RuntimeError, match="Failed to connect to IBKR"):
        await provider._ensure_connected()


@pytest.mark.asyncio
async def test_ensure_connected_does_not_bypass_manager_even_when_direct_fallback_enabled(monkeypatch):
    provider = IBKROptionsDataProvider(client_id=77, allow_direct_fallback=True)
    fake_manager = _FakeConnManager(connect_exc=RuntimeError("shared unavailable"))
    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: fake_manager,
    )

    with pytest.raises(RuntimeError, match="Failed to connect to IBKR"):
        await provider._ensure_connected()

    assert provider.ib is None


@pytest.mark.asyncio
async def test_disconnect_detaches_without_closing_shared_market_session():
    provider = IBKROptionsDataProvider()
    shared_ib = _FakeIBConnected()
    shared_ib.disconnect = lambda: (_ for _ in ()).throw(AssertionError("shared session must not be disconnected"))
    provider.ib = shared_ib
    provider._connected = True
    provider._conn_manager = object()

    await provider.disconnect()

    assert provider.ib is None
    assert provider._connected is False
    assert provider._conn_manager is None
