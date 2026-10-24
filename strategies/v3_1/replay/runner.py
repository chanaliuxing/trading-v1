"""Replay runner for V3.1 strategy."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from libs.strategy import StrategyParams
from strategies.v3_1.strategy_v3_1 import V31Strategy
from strategies.v3_1.replay.simulator import ReplayEngine


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", required=True, help="Path to JSONL bars (symbol, ts, open/high/low/close/volume)")
    args = parser.parse_args()

    # Build a dummy StrategyContext-like stub for replay
    class DummyCtx:
        def __init__(self):
            self.strategy_id = "v3_1_replay"
            self.log = None
            self.metrics = None
            self.engine = None

        async def set_cadence(self, seconds: int) -> None:
            return None

        async def subscribe_features(self, symbols, bar_sec: int = 60) -> None:
            return None

        async def portfolio(self) -> dict:
            return {"equity": 100000}

        async def position(self, symbol: str):
            class P:
                pos_qty = 0
            return P()

    ctx = DummyCtx()
    params = StrategyParams(cadence_s=1)
    strat = V31Strategy(ctx, params)
    await strat.initialize()
    replay = ReplayEngine(strat)

    with Path(args.bars).open("r", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            sym = row.pop("symbol")
            await replay.feed(sym, row)


if __name__ == "__main__":
    asyncio.run(main())
