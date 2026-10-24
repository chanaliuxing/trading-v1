import libs.db.ch_pool as ch_pool
from libs.db.ch_pool import ClickHouseClientWrapper


class _QueryResult:
    result_rows = [(1, "QQQ")]

    def named_results(self):
        return [{"n": 1, "symbol": "QQQ"}]


class _FakeClient:
    def __init__(self) -> None:
        self.command_calls = []

    def query(self, sql, parameters=None, **kwargs):
        return _QueryResult()

    def command(self, sql, parameters=None, **kwargs):
        self.command_calls.append((sql, parameters, kwargs))
        return "OK"


def test_clickhouse_wrapper_execute_returns_tuple_rows_for_select() -> None:
    wrapper = ClickHouseClientWrapper(_FakeClient())

    rows = wrapper.execute("SELECT 1 AS n, 'QQQ' AS symbol")

    assert rows == [(1, "QQQ")]


def test_clickhouse_wrapper_execute_routes_non_select_to_command() -> None:
    client = _FakeClient()
    wrapper = ClickHouseClientWrapper(client)

    result = wrapper.execute("OPTIMIZE TABLE core.lake_trades")

    assert result == "OK"
    assert client.command_calls[0][0] == "OPTIMIZE TABLE core.lake_trades"


def test_clickhouse_wrapper_merges_default_settings() -> None:
    client = _FakeClient()
    wrapper = ClickHouseClientWrapper(client, default_settings={"session_timezone": "America/New_York"})

    wrapper.query("SELECT 1")

    # _FakeClient does not keep query calls; the assertion is on the public
    # merge helper so we do not couple the test to clickhouse_connect internals.
    assert wrapper._merge_settings({})["settings"]["session_timezone"] == "America/New_York"


def test_get_client_disables_auto_sessions(monkeypatch) -> None:
    captured = {}

    class _FakeConfig:
        clickhouse_config = {
            "host": "localhost",
            "port": 8123,
            "database": "trading",
            "user": "default",
            "password": "",
            "timezone": "America/New_York",
        }

    def _fake_get_client(**kwargs):
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(ch_pool, "config", _FakeConfig())
    monkeypatch.setattr(ch_pool.clickhouse_connect, "get_client", _fake_get_client)

    client = ch_pool.get_client()

    assert isinstance(client, ClickHouseClientWrapper)
    assert captured["autogenerate_session_id"] is False
    assert captured["query_retries"] == 0
    assert client._merge_settings({})["settings"]["session_timezone"] == "America/New_York"


def test_threadlocal_client_reuses_client_on_same_thread(monkeypatch) -> None:
    calls = {"count": 0}

    def _fake_get_client(**kwargs):
        calls["count"] += 1
        return _FakeClient()

    monkeypatch.delattr(ch_pool._THREAD_LOCAL, "client", raising=False)
    monkeypatch.setattr(ch_pool, "get_client", _fake_get_client)

    first = ch_pool.get_threadlocal_client()
    second = ch_pool.get_threadlocal_client()

    assert first is second
    assert calls["count"] == 1
