import pytest

from services.market.options_data import IBKROptionsDataProvider


class _FakeManager:
    def __init__(self, *, revalidation_required: bool = False, allow: bool = True):
        self._revalidation_required = revalidation_required
        self._allow = allow
        self.revalidated = []
        self.recorded = []
        self.allow_calls = []

    def market_revalidation_required(self):
        return self._revalidation_required

    def market_connection_epoch(self):
        return 1

    def mark_market_revalidated(self, **kwargs):
        self.revalidated.append(kwargs)

    def was_market_req_rejected(self, *_args, **_kwargs):
        return False

    def allow_market_request(self, **kwargs):
        self.allow_calls.append(kwargs)
        return type("_Decision", (), {"allowed": self._allow, "reason": "blocked_for_test"})()

    def record_market_request_result(self, **kwargs):
        self.recorded.append(kwargs)


class _FakeIB:
    def __init__(self, *, con_id: int = 0):
        self.con_id = con_id
        self.qualify_calls = 0
        self.secdef_calls = 0

    def isConnected(self):
        return True

    async def qualifyContractsAsync(self, *contracts):
        self.qualify_calls += 1
        if not contracts:
            return []
        contract = contracts[0]
        setattr(contract, "conId", self.con_id)
        return [contract]

    async def reqSecDefOptParamsAsync(self, **_kwargs):
        self.secdef_calls += 1
        return []


@pytest.mark.asyncio
async def test_get_chain_skips_secdef_while_market_revalidation_is_cooling_down():
    provider = IBKROptionsDataProvider()
    provider.ib = _FakeIB(con_id=123)
    provider._connected = True
    provider._conn_manager = _FakeManager(revalidation_required=True)
    provider._market_handle_epoch = 1
    provider._last_session_revalidate_ts = 1_000_000.0
    provider._session_revalidate_cooldown_sec = 999_999.0

    contracts = await provider.get_chain("QQQ", [0], "C")

    assert contracts == []
    assert provider.ib.secdef_calls == 0
    assert ("QQQ", "0", "C") in provider._chain_failure_backoff_until


@pytest.mark.asyncio
async def test_get_chain_allows_governor_request_during_market_revalidation():
    provider = IBKROptionsDataProvider()
    provider.ib = _FakeIB(con_id=123)
    provider._connected = True
    manager = _FakeManager(revalidation_required=True)
    provider._conn_manager = manager
    provider._market_handle_epoch = 1
    provider._last_session_revalidate_ts = 1_000_000.0
    provider._session_revalidate_cooldown_sec = 999_999.0

    contracts = await provider.get_chain("QQQ", [0], "C")

    assert contracts == []
    assert manager.allow_calls
    assert manager.allow_calls[0]["allow_during_revalidation"] is True
    assert provider.ib.secdef_calls == 0


@pytest.mark.asyncio
async def test_get_chain_requires_qualified_underlying_conid_before_secdef():
    provider = IBKROptionsDataProvider()
    provider.ib = _FakeIB(con_id=0)
    provider._connected = True
    provider._conn_manager = _FakeManager(revalidation_required=False)
    provider._market_handle_epoch = 1

    contracts = await provider.get_chain("QQQ", [0], "C")

    assert contracts == []
    assert provider.ib.qualify_calls == 1
    assert provider.ib.secdef_calls == 0
    assert ("QQQ", "0", "C") in provider._chain_failure_backoff_until


@pytest.mark.asyncio
async def test_get_chain_obeys_request_governor_before_secdef():
    provider = IBKROptionsDataProvider()
    provider.ib = _FakeIB(con_id=123)
    provider._connected = True
    provider._conn_manager = _FakeManager(revalidation_required=False, allow=False)
    provider._market_handle_epoch = 1

    contracts = await provider.get_chain("QQQ", [0], "C")

    assert contracts == []
    assert provider.ib.qualify_calls == 0
    assert provider.ib.secdef_calls == 0
    assert ("QQQ", "0", "C") in provider._chain_failure_backoff_until
