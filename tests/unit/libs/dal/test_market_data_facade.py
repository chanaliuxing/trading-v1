import pytest

from libs.dal.market_data_facade import MarketDataFacade


@pytest.mark.asyncio
async def test_bars_async_falls_back_to_base_table_when_current_view_fails(monkeypatch):
    calls = []

    def _fake_threadsafe_query(client, sql, parameters=None, **kwargs):
        calls.append(sql)
        if "bars_1m_current" in sql:
            raise RuntimeError("current timeout")
        return [
            {"ts": "2026-04-02T02:40:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
            {"ts": "2026-04-02T02:41:00", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 200},
        ]

    monkeypatch.setattr("libs.dal.market_data_facade.threadsafe_query", _fake_threadsafe_query)

    rows = await MarketDataFacade(ch=object()).bars_async("SMH", "1m", 2)

    assert len(rows) == 2
    assert "bars_1m_current" in calls[0]
    assert "FROM trading.bars_1m" in calls[1]
    assert rows[0]["close"] == 2.0
    assert rows[1]["close"] == 1.5


@pytest.mark.asyncio
async def test_bars_async_keeps_current_view_when_available(monkeypatch):
    calls = []

    def _fake_threadsafe_query(client, sql, parameters=None, **kwargs):
        calls.append(sql)
        return [
            {"ts": "2026-04-02T02:40:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
        ]

    monkeypatch.setattr("libs.dal.market_data_facade.threadsafe_query", _fake_threadsafe_query)

    rows = await MarketDataFacade(ch=object()).bars_async("SMH", "1m", 1)

    assert len(rows) == 1
    assert len(calls) == 1
    assert "bars_1m_current" in calls[0]


@pytest.mark.asyncio
async def test_bars_async_skips_5s_current_view(monkeypatch):
    calls = []

    def _fake_threadsafe_query(client, sql, parameters=None, **kwargs):
        calls.append(sql)
        return [
            {"ts": "2026-04-02T02:40:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
        ]

    monkeypatch.setattr("libs.dal.market_data_facade.threadsafe_query", _fake_threadsafe_query)

    rows = await MarketDataFacade(ch=object()).bars_async("QQQ", "5s", 1)

    assert len(rows) == 1
    assert len(calls) == 1
    assert "bars_5s_current" not in calls[0]
    assert "FROM trading.bars_5s" in calls[0]
    assert "ts_end >= now() - INTERVAL 3 DAY" in calls[0]


@pytest.mark.asyncio
async def test_bars_async_rolls_up_1m_from_5s_when_1m_tables_are_empty(monkeypatch):
    calls = []

    def _fake_threadsafe_query(client, sql, parameters=None, **kwargs):
        calls.append(sql)
        if "FROM trading.bars_5s" in sql:
            return [
                {"ts": "2026-05-05T10:41:00", "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 100},
                {"ts": "2026-05-05T10:42:00", "open": 11.0, "high": 13.0, "low": 10.0, "close": 12.0, "volume": 120},
            ]
        return []

    cleared = []
    marked = []
    monkeypatch.setattr("libs.dal.market_data_facade.threadsafe_query", _fake_threadsafe_query)
    monkeypatch.setattr(MarketDataFacade, "_clear_data_gate", staticmethod(lambda **kwargs: cleared.append(kwargs)))
    monkeypatch.setattr(MarketDataFacade, "_mark_data_gate", staticmethod(lambda **kwargs: marked.append(kwargs)))

    rows = await MarketDataFacade(ch=object()).bars_async("SMH", "1m", 2)

    assert len(rows) == 2
    assert "bars_1m_current" in calls[0]
    assert "FROM trading.bars_1m" in calls[1]
    assert "FROM trading.bars_5s" in calls[2]
    assert "bars_5s_current" not in calls[2]
    assert cleared == [{"symbol": "SMH", "res": "1m"}]
    assert marked == []
