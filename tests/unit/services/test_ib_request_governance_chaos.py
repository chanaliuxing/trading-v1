import asyncio

import pytest

from services.ibkr.ib_connection_manager import IBConnectionManager
from services.market.ib_market_data import IBMarketDataAdapter
from services.market.options_data import IBKROptionsDataProvider


class _CountingIB:
    def __init__(self):
        self.qualify_calls = 0
        self.secdef_calls = 0

    def isConnected(self):
        return True

    def reqMarketDataType(self, *_args, **_kwargs):
        return None

    async def qualifyContractsAsync(self, *contracts):
        self.qualify_calls += 1
        return list(contracts)

    async def reqSecDefOptParamsAsync(self, **_kwargs):
        self.secdef_calls += 1
        return []


@pytest.mark.asyncio
async def test_market_revalidation_chaos_blocks_business_ib_request_storm(monkeypatch):
    manager = IBConnectionManager()
    manager._market_revalidate_required = True
    manager._market_degraded = True

    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: manager,
    )

    stale_adapter = IBMarketDataAdapter(
        symbols=["QQQ", "SPY", "TQQQ"],
        port=4002,
        client_id=123,
        ib=_CountingIB(),
    )
    stale_adapter._stale_resubscribe_sec = 1.0
    stale_adapter._stale_resubscribe_cooldown_sec = 0.0
    stale_adapter._symbol_last_update_ts = {"QQQ": 0.0, "SPY": 0.0, "TQQQ": 0.0}
    monkeypatch.setattr(stale_adapter, "_is_market_open", lambda: True)
    monkeypatch.setattr(stale_adapter, "_session_phase_et", lambda *_args, **_kwargs: "RTH")
    monkeypatch.setattr(stale_adapter, "_is_index_like_symbol", lambda _s: False)

    resubscribe_calls = []

    async def _fake_resubscribe(symbol: str) -> None:
        resubscribe_calls.append(symbol)

    monkeypatch.setattr(stale_adapter, "_resubscribe_symbol", _fake_resubscribe)

    ib = _CountingIB()
    provider = IBKROptionsDataProvider()
    provider.ib = ib
    provider._connected = True
    provider._conn_manager = manager
    provider._market_handle_epoch = manager.market_connection_epoch()

    async def _no_live_revalidation(_underlying: str) -> None:
        return None

    monkeypatch.setattr(provider, "_validate_market_session_if_needed", _no_live_revalidation)

    for _ in range(5):
        await stale_adapter._check_stale_subscriptions()
        assert await provider.get_chain("QQQ", [0], "C") == []
        await asyncio.sleep(0)

    # Current contract: market revalidation blocks business option-chain IB
    # requests, while stale L1 resubscribe recovery is still attempted by the
    # market-data adapter.
    assert resubscribe_calls == ["QQQ", "SPY", "TQQQ"] * 5
    assert ib.qualify_calls == 0
    assert ib.secdef_calls == 0
