from services.market.ib_market_data import IBMarketDataAdapter
import asyncio
import time


class _DummyEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers = [h for h in self.handlers if h != handler]
        return self


class _DummyTicker:
    def __init__(self):
        self.updateEvent = _DummyEvent()


class _DisconnectedIB:
    def isConnected(self):
        return False


class _ConnectedIB:
    def __init__(self, client_id=27):
        self.client = type("_Client", (), {"clientId": client_id})()
        self.market_data_types = []

    def isConnected(self):
        return True

    def reqMarketDataType(self, market_data_type):
        self.market_data_types.append(market_data_type)


def test_premarket_uses_sticky_port_before_threshold():
    adapter = IBMarketDataAdapter(symbols=["QQQ"], port=4002, client_id=123)
    adapter._connect_failures = 0
    ports = adapter._candidate_ports(session_phase="PREMARKET")
    assert ports == [4002]

    adapter._connect_failures = 2
    ports = adapter._candidate_ports(session_phase="PREMARKET")
    assert ports == [4002]


def test_port_fallback_scan_is_disabled_by_default():
    adapter = IBMarketDataAdapter(symbols=["QQQ"], port=4002, client_id=123)
    adapter._connect_failures = adapter._premarket_port_fallback_failures
    ports = adapter._candidate_ports(session_phase="PREMARKET")
    assert ports == [4002]


def test_port_fallback_scan_can_be_enabled_explicitly():
    adapter = IBMarketDataAdapter(symbols=["QQQ"], port=7497, client_id=123)
    adapter._enable_port_fallback_scan = True
    adapter._connect_failures = 0
    ports = adapter._candidate_ports(session_phase="RTH")
    assert ports[0] == 7497
    assert set(ports) == {4002, 7497, 7496}


def test_shared_adapter_refreshes_ib_via_connection_manager(monkeypatch):
    stale_ib = _DisconnectedIB()
    fresh_ib = _ConnectedIB(client_id=77)

    class _Manager:
        async def get_market_ib(self, *, host, port):
            assert host == "127.0.0.1"
            assert port == 4002
            return fresh_ib

    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: _Manager(),
    )

    adapter = IBMarketDataAdapter(symbols=["QQQ"], port=4002, client_id=123, ib=stale_ib)

    asyncio.run(adapter._connect())

    assert adapter.ib is fresh_ib
    assert adapter.ib_client_id == 77


def test_shared_manager_mode_uses_connection_manager_without_initial_ib(monkeypatch):
    fresh_ib = _ConnectedIB(client_id=88)

    class _Manager:
        async def get_market_ib(self, *, host, port):
            assert host == "127.0.0.1"
            assert port == 4002
            return fresh_ib

    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: _Manager(),
    )

    adapter = IBMarketDataAdapter(
        symbols=["QQQ"],
        port=4002,
        client_id=123,
        use_shared_manager=True,
    )

    assert adapter._owns_ib is False
    asyncio.run(adapter._connect())

    assert adapter.ib is fresh_ib
    assert adapter.ib_client_id == 88


def test_shared_manager_mode_rejects_empty_market_handle(monkeypatch):
    class _Handle:
        ib = None
        epoch = 1

    class _Manager:
        async def get_market_ib_handle(self, *, host, port):
            return _Handle()

    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: _Manager(),
    )

    adapter = IBMarketDataAdapter(
        symbols=["QQQ"],
        port=4002,
        client_id=123,
        use_shared_manager=True,
    )

    try:
        asyncio.run(adapter._connect())
    except ConnectionError as exc:
        assert "shared_market_ib_not_connected" in str(exc)
    else:
        raise AssertionError("expected shared market handle without connected IB to fail closed")


def test_stale_feed_triggers_symbol_resubscribe(monkeypatch):
    adapter = IBMarketDataAdapter(symbols=["SMH", "SOXL"], port=4002, client_id=123)
    adapter._stale_resubscribe_sec = 30.0
    adapter._stale_resubscribe_cooldown_sec = 15.0
    adapter._running = True
    adapter.contracts = {"SMH": object(), "SOXL": object()}
    now = time.time()
    adapter._symbol_last_update_ts = {"SMH": now - 120.0, "SOXL": now - 5.0}

    monkeypatch.setattr(adapter, "_is_market_open", lambda: True)
    monkeypatch.setattr(adapter, "_session_phase_et", lambda *_args, **_kwargs: "RTH")
    monkeypatch.setattr(adapter, "_is_index_like_symbol", lambda _s: False)

    called = []

    async def _fake_resubscribe(symbol: str) -> None:
        called.append(symbol)

    monkeypatch.setattr(adapter, "_resubscribe_symbol", _fake_resubscribe)

    asyncio.run(adapter._check_stale_subscriptions())
    assert called == ["SMH"]


def test_stale_feed_resubscribe_still_runs_during_market_revalidation(monkeypatch):
    adapter = IBMarketDataAdapter(symbols=["SMH"], port=4002, client_id=123)
    adapter._stale_resubscribe_sec = 10.0
    now = time.time()
    adapter._symbol_last_update_ts = {"SMH": now - 200.0}

    monkeypatch.setattr(adapter, "_is_market_open", lambda: True)
    monkeypatch.setattr(adapter, "_session_phase_et", lambda *_args, **_kwargs: "RTH")
    monkeypatch.setattr(adapter, "_is_index_like_symbol", lambda _s: False)

    called = []

    async def _fake_resubscribe(symbol: str) -> None:
        called.append(symbol)

    monkeypatch.setattr(adapter, "_resubscribe_symbol", _fake_resubscribe)

    asyncio.run(adapter._check_stale_subscriptions())
    # Current contract: the connection manager gates business requests during
    # revalidation; local stale-feed recovery still resubscribes.
    assert called == ["SMH"]


def test_stale_feed_resubscribe_honors_cooldown(monkeypatch):
    adapter = IBMarketDataAdapter(symbols=["SMH"], port=4002, client_id=123)
    adapter._stale_resubscribe_sec = 10.0
    adapter._stale_resubscribe_cooldown_sec = 120.0
    adapter._running = True
    adapter.contracts = {"SMH": object()}
    now = time.time()
    adapter._symbol_last_update_ts = {"SMH": now - 200.0}
    adapter._stale_resubscribe_last_try_ts = {"SMH": now - 30.0}

    monkeypatch.setattr(adapter, "_is_market_open", lambda: True)
    monkeypatch.setattr(adapter, "_session_phase_et", lambda *_args, **_kwargs: "RTH")
    monkeypatch.setattr(adapter, "_is_index_like_symbol", lambda _s: False)

    called = []

    async def _fake_resubscribe(symbol: str) -> None:
        called.append(symbol)

    monkeypatch.setattr(adapter, "_resubscribe_symbol", _fake_resubscribe)

    asyncio.run(adapter._check_stale_subscriptions())
    assert called == []


def test_stale_feed_without_first_tick_triggers_resubscribe(monkeypatch):
    adapter = IBMarketDataAdapter(symbols=["SMH"], port=4002, client_id=123)
    adapter._stale_resubscribe_sec = 20.0
    adapter._stale_resubscribe_cooldown_sec = 10.0
    now = time.time()
    adapter._symbol_subscription_ts = {"SMH": now - 120.0}

    monkeypatch.setattr(adapter, "_is_market_open", lambda: True)
    monkeypatch.setattr(adapter, "_session_phase_et", lambda *_args, **_kwargs: "RTH")
    monkeypatch.setattr(adapter, "_is_index_like_symbol", lambda _s: False)

    called = []

    async def _fake_resubscribe(symbol: str) -> None:
        called.append(symbol)

    monkeypatch.setattr(adapter, "_resubscribe_symbol", _fake_resubscribe)

    asyncio.run(adapter._check_stale_subscriptions())
    assert called == ["SMH"]


def test_stale_feed_skips_resubscribe_outside_rth(monkeypatch):
    adapter = IBMarketDataAdapter(symbols=["SMH"], port=4002, client_id=123)
    adapter._stale_resubscribe_sec = 20.0
    adapter._stale_resubscribe_cooldown_sec = 10.0
    now = time.time()
    adapter._symbol_last_update_ts = {"SMH": now - 120.0}

    monkeypatch.setattr(adapter, "_is_market_open", lambda: True)
    monkeypatch.setattr(adapter, "_session_phase_et", lambda *_args, **_kwargs: "AFTERHOURS")
    monkeypatch.setattr(adapter, "_is_index_like_symbol", lambda _s: False)

    called = []

    async def _fake_resubscribe(symbol: str) -> None:
        called.append(symbol)

    monkeypatch.setattr(adapter, "_resubscribe_symbol", _fake_resubscribe)

    asyncio.run(adapter._check_stale_subscriptions())
    assert called == []


def test_resubscribe_failure_restores_previous_subscription(monkeypatch):
    adapter = IBMarketDataAdapter(symbols=["SMH"], port=4002, client_id=123)
    old_contract = object()
    old_ticker = _DummyTicker()
    old_ticker.updateEvent += adapter._on_ticker_update
    adapter.contracts = {"SMH": old_contract}
    adapter.tickers = {"SMH": old_ticker}
    adapter._ticker_id_to_symbol = {id(old_ticker): "SMH"}

    async def _boom(_symbol: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(adapter, "_subscribe_symbol", _boom)

    asyncio.run(adapter._resubscribe_symbol("SMH"))
    assert adapter.contracts.get("SMH") is old_contract
    assert adapter.tickers.get("SMH") is old_ticker
    assert adapter._ticker_id_to_symbol.get(id(old_ticker)) == "SMH"


def test_dynamic_subscribe_is_deferred_when_disconnected():
    adapter = IBMarketDataAdapter(symbols=["QQQ"], port=4002, client_id=123, ib=_DisconnectedIB())

    ok = asyncio.run(adapter.subscribe_symbol("AAPL"))

    assert ok is True
    assert "AAPL" in adapter.symbols
    assert "AAPL" not in adapter.contracts


def test_dynamic_subscribe_adds_index_symbol_to_deferred_set_when_disconnected(monkeypatch):
    adapter = IBMarketDataAdapter(symbols=["QQQ"], port=4002, client_id=123, ib=_DisconnectedIB())
    monkeypatch.setattr(adapter, "_is_rth", lambda: False)
    monkeypatch.setattr(adapter, "_is_index_like_symbol", lambda _s: True)

    ok = asyncio.run(adapter.subscribe_symbol("VIX"))

    assert ok is True
    assert "VIX" in adapter.symbols
    assert "VIX" in adapter._deferred_symbols


def test_passive_recovery_connect_error_classification():
    assert IBMarketDataAdapter._is_passive_recovery_connect_error("reconnect_gatekeeper_denied:market_data_adapter")
    assert IBMarketDataAdapter._is_passive_recovery_connect_error(
        "ib_recovery_short_circuit_active:ib_connectivity_lost_1100"
    )
    assert IBMarketDataAdapter._is_passive_recovery_connect_error("TimeoutError: ib_api_handshake_not_ready")
    assert not IBMarketDataAdapter._is_passive_recovery_connect_error("RuntimeError: bad_contract")
