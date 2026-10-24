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
        await asyncio.sleep(0.02)


class _FakeStrategy:
    async def on_shutdown(self, reason: str) -> None:
        return None


@pytest.mark.asyncio
async def test_shared_underlying_stream_fanout_delivers_to_all_consumers() -> None:
    md = _FakeMD()
    engine = StrategyEngine(market_data_facade=md, dry_run=True)
    dispatch_calls: list[tuple[str, str]] = []

    async def _capture(strategy_id: str, symbol: str, features: dict):
        dispatch_calls.append((str(strategy_id), str(symbol)))

    engine.execute_strategy = _capture  # type: ignore[method-assign]
    for sid in ("s1", "s2"):
        engine._strategies[sid] = _FakeStrategy()
        engine._cadences[sid] = 1
        engine._last_exec[sid] = 0

    try:
        await engine.subscribe_features_unified("s1", ["QQQ"], 60)
        await engine.subscribe_features_unified("s2", ["QQQ"], 60)
        await asyncio.sleep(0.03)

        assert len(engine._poll_stream_tasks) == 1
        assert len(md.poll_calls) == 1
        assert ("s1", "QQQ") in dispatch_calls
        assert ("s2", "QQQ") in dispatch_calls
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_shared_underlying_stream_unload_keeps_task_until_last_consumer() -> None:
    md = _FakeMD()
    engine = StrategyEngine(market_data_facade=md, dry_run=True)
    async def _noop(*_args, **_kwargs):
        return None

    engine.execute_strategy = _noop  # type: ignore[method-assign]
    for sid in ("s1", "s2"):
        engine._strategies[sid] = _FakeStrategy()
        engine._cadences[sid] = 1
        engine._last_exec[sid] = 0

    try:
        await engine.subscribe_features_unified("s1", ["QQQ"], 60)
        await engine.subscribe_features_unified("s2", ["QQQ"], 60)
        await asyncio.sleep(0.01)
        key = ("QQQ", "1m")
        assert key in engine._poll_stream_tasks
        assert engine._poll_stream_consumers[key] == {"s1", "s2"}

        await engine.unload_strategy("s1", reason="test")
        assert key in engine._poll_stream_tasks
        assert engine._poll_stream_consumers[key] == {"s2"}

        await engine.unload_strategy("s2", reason="test")
        assert key not in engine._poll_stream_tasks
        assert key not in engine._poll_stream_consumers
    finally:
        await engine.shutdown()
