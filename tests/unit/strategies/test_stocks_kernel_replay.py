from types import SimpleNamespace

import pytest

from strategies.stocks_kernel.runtime.replay import ReplayFrame, run_strategy_replay
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import HistoryBar


class _Logger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class _Engine:
    async def subscribe_features(self, strategy_id, symbols, bar_sec):
        return None

    async def subscribe_tick_data(self, strategy_id, symbols):
        return {symbol: True for symbol in symbols}


@pytest.mark.asyncio
async def test_run_strategy_replay_returns_signal_and_plan():
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_replay_001",
        strategy_run_id="run-1",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_replay_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_replay_001", symbols),
    )
    params = SimpleNamespace(
        strategy_id="kernel_replay_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    frames = [
        ReplayFrame(
            symbol="QQQ",
            features={
                "ts_epoch_s": 100.0,
                "now_epoch_s": 101.0,
                "last_price": 500.0,
                "session": "RTH",
                "vwap": 499.0,
                "planned_qty": 2,
                "trigger_id": "t1",
                "bars_1m": [
                    HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                    for i in range(1, 16)
                ],
            },
        )
    ]

    rows = await run_strategy_replay(strategy=strategy, frames=frames)

    assert rows[0]["allowed"] is True
    assert rows[0]["signal_action"] == "enter"
    assert rows[0]["entry_qty"] == 2.0
