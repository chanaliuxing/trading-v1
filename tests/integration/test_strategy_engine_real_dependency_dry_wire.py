from __future__ import annotations

import pytest

from libs.strategy.base import StrategyAlgorithm
from libs.strategy.registry import get_registry
from services.health import SystemHealthController
from services.strategy.engine import StrategyEngine


class _FakePostgresDB:
    async def fetchrow(self, *args, **kwargs):
        return None

    async def fetchall(self, *args, **kwargs):
        return []

    async def execute(self, *args, **kwargs):
        return None


class _FakeMarketDataFacade:
    async def vix_symbol_async(self) -> str:
        return "VIX"

    async def bars_async(self, *args, **kwargs):
        return []


class _FakeDecisionPipeline:
    shadow_mode = False

    async def process_decision(self, envelope):
        return {"status": "DRY_WIRE"}


class _RealWireStrategy(StrategyAlgorithm):
    async def initialize(self) -> None:
        assert self.ctx.engine.__class__.__name__ == "RealEngine"
        assert getattr(self.ctx.engine, "decision_pipeline", None) is not None
        await self.subscribe_system_health()
        await self.ctx.set_cadence(5)
        await self.ctx.subscribe_tick_data(["QQQ"])

    async def on_features(self, symbol: str, features: dict) -> None:
        return None


@pytest.mark.asyncio
async def test_strategy_engine_load_strategy_real_dependency_dry_wire(monkeypatch) -> None:
    monkeypatch.setattr("libs.db.postgres.PostgresDB", _FakePostgresDB)
    strategy_name = "_real_dependency_dry_wire_strategy"
    get_registry().register(strategy_name, _RealWireStrategy)

    engine = StrategyEngine(
        market_data_facade=_FakeMarketDataFacade(),
        decision_pipeline=_FakeDecisionPipeline(),
        market_data_service=None,
        system_health_controller=SystemHealthController(),
    )

    await engine.load_strategy(
        strategy_id="real_wire_strategy_001",
        name=strategy_name,
        params={"cadence_s": 5, "extras": {}},
    )

    assert "real_wire_strategy_001" in engine._strategies
    assert engine.engine.__class__.__name__ == "RealEngine"
    assert engine.engine.decision_pipeline is not None
    await engine.shutdown()
