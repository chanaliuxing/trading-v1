#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks_kernel.runtime.replay import ReplayFrame, run_strategy_replay
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy


class _Logger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class _ReplayEngine:
    async def subscribe_features(self, strategy_id, symbols, bar_sec):
        return None

    async def subscribe_tick_data(self, strategy_id, symbols):
        return {symbol: True for symbol in symbols}


async def _run(config: dict, frames: list[dict]) -> list[dict]:
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_ReplayEngine(),
        strategy_id=str(config.get("strategy_id") or "stocks_kernel_replay"),
        strategy_run_id="replay-run-1",
        subscribe_features=lambda symbols, bar_sec=5: _ReplayEngine().subscribe_features(config.get("strategy_id"), symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _ReplayEngine().subscribe_tick_data(config.get("strategy_id"), symbols),
    )
    params = SimpleNamespace(
        strategy_id=str(config.get("strategy_id") or "stocks_kernel_replay"),
        ttl_ms=int(config.get("ttl_ms") or 2000),
        extras=dict(config.get("extras") or {}),
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    return await run_strategy_replay(
        strategy=strategy,
        frames=[ReplayFrame(symbol=str(item["symbol"]), features=dict(item["features"])) for item in frames],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run stocks kernel replay from JSON input")
    parser.add_argument("--config", required=True)
    parser.add_argument("--frames", required=True)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    frames = json.loads(Path(args.frames).read_text(encoding="utf-8"))
    payload = asyncio.run(_run(config=config, frames=frames))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
