"""
SFS Fusion Demo Strategy

Demonstrates how to read Redis-only 1s microbars (SFS-1s) alongside 1m features
and submit a simple decision when micro-structure aligns with a zscore signal.

Usage: enable via config.yml strategies: with module/class and desired symbols.
"""
from __future__ import annotations

from typing import Dict, Any, List

from libs.strategy import (
    StrategyAlgorithm,
    DecisionEnvelope,
    OrderStyle,
    Freshness,
    make_decision_id,
)
from libs.dal.sfs_facade import SFS1sReader
from libs.logger import get_logger

logger = get_logger(__name__)


class SfsFusionDemo(StrategyAlgorithm):
    async def initialize(self) -> None:
        # symbols configured via params.extras.symbols (fallback to universe[0])
        symbols: List[str] = self.params.extras.get("symbols") or ["AAPL"]
        await self.ctx.subscribe_features(symbols, bar_sec=60)
        self.sfs = SFS1sReader()
        logger.info("SfsFusionDemo initialized", symbols=symbols)

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        # 1m signal
        z = float(features.get("zscore_20", 0) or 0)
        px = float(features.get("close", 0) or 0)
        if px <= 0:
            return

        # micro structure: last 20 seconds trend confirmation (very simple)
        sub = await self.sfs.last_n(symbol, 20)
        if len(sub) < 5:
            return
        closes = [float(x.get("close") or 0) for x in sub if x]
        if not closes or closes[0] <= 0:
            return

        # newest first in Redis → check short-term momentum consistency
        up_count = sum(1 for i in range(min(10, len(closes) - 1)) if closes[i] > closes[i + 1])
        down_count = 10 - up_count

        go_long = (z < -2.0) and (up_count >= 6)
        go_flat = (z > 2.0) and (down_count >= 6)

        if not (go_long or go_flat):
            return

        target_qty = 100.0 if go_long else 0.0
        env = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=self.ctx.strategy_id,
            symbol=symbol,
            ts_bar_end_us=self.ctx.now_us(),
            target_qty=target_qty,
            order_style=OrderStyle(kind="MKT"),
            freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=500),
            extras={
                "ref_px": px,
                "z": z,
                "sfs_up10": up_count,
                "sfs_len": len(sub),
            },
        )
        await self.ctx.submit_intent(env)

