from datetime import datetime
from types import SimpleNamespace

import pytest

from libs.strategy import StrategyContext, StrategyParams
from strategies.qqq_intraday_strategy import ET, QQQIntradayStrategy


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class _FakeIB:
    def __init__(self):
        self.qualified = 0
        self.historical = 0

    def isConnected(self):
        return True

    async def qualifyContractsAsync(self, *_args):
        self.qualified += 1
        return []

    async def reqHistoricalDataAsync(self, **_kwargs):
        self.historical += 1
        return []


class _FakeManager:
    def allow_market_request(self, **_kwargs):
        return SimpleNamespace(allowed=False, reason="market_revalidation_required")

    def record_market_request_result(self, **_kwargs):
        raise AssertionError("blocked QQQ fallback must not record a real request result")


@pytest.mark.asyncio
async def test_qqq_price_base_ibkr_fallback_obeys_request_governor(monkeypatch):
    ib = _FakeIB()
    engine = SimpleNamespace(market_data_service=SimpleNamespace(ib_adapter=SimpleNamespace(ib=ib)))
    ctx = StrategyContext(
        engine=engine,
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    strategy = QQQIntradayStrategy(ctx, StrategyParams())

    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: _FakeManager(),
    )

    price, reason, samples = await strategy._recover_price_base_from_ibkr(
        datetime(2026, 4, 28, 10, 0, tzinfo=ET)
    )

    assert price is None
    assert reason == "ibkr_governor_blocked:market_revalidation_required"
    assert samples == 0
    assert ib.qualified == 0
    assert ib.historical == 0
