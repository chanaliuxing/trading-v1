import asyncio

import pytest

from services.strategy.engine import StrategyEngine


class _FakeMD:
    def __init__(self) -> None:
        self.poll_calls: list[tuple[str, str, int]] = []

    async def vix_symbol_async(self) -> str:
        return "VIX"

    async def poll_new_bar(self, symbol: str, res: str, poll_sec: int, cb):
        self.poll_calls.append((symbol, res, int(poll_sec)))
        await cb({"ts": "2026-04-09 10:00:00", "close": 500.0})
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_single_underlying_stream_reused_across_strategies() -> None:
    md = _FakeMD()
    engine = StrategyEngine(market_data_facade=md, dry_run=True)
    dispatch_calls: list[tuple[str, str]] = []

    async def _capture(strategy_id: str, symbol: str, features: dict):
        dispatch_calls.append((str(strategy_id), str(symbol)))

    engine.execute_strategy = _capture  # type: ignore[method-assign]

    class _FakeStrategy:
        async def on_shutdown(self, reason: str) -> None:
            return None

    # satisfy callback and shutdown lifecycle contracts
    engine._strategies["s1"] = _FakeStrategy()
    engine._strategies["s2"] = _FakeStrategy()
    engine._cadences["s1"] = 1
    engine._cadences["s2"] = 1
    engine._last_exec["s1"] = 0
    engine._last_exec["s2"] = 0
    try:
        await engine.subscribe_features_unified("s1", ["QQQ"], 60)
        await engine.subscribe_features_unified("s2", ["QQQ"], 60)
        await asyncio.sleep(0.02)

        assert len(engine._poll_stream_tasks) == 1
        assert len(md.poll_calls) >= 1
        assert md.poll_calls[0][0] == "QQQ"
        assert md.poll_calls[0][1] == "1m"

        key = ("QQQ", "1m")
        assert key in engine._poll_stream_consumers
        assert engine._poll_stream_consumers[key] == {"s1", "s2"}
        assert ("s1", "QQQ") in dispatch_calls
        assert ("s2", "QQQ") in dispatch_calls
    finally:
        await engine.shutdown()
