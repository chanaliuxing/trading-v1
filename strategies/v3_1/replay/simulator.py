"""Simple replay simulator for V3.1 strategy outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class SimFill:
    ts: str
    symbol: str
    side: str
    qty: int
    price: float


class SimBroker:
    def __init__(self):
        self.fills: List[SimFill] = []

    def execute_market(self, ts: str, symbol: str, side: str, qty: int, price: float) -> SimFill:
        fill = SimFill(ts=ts, symbol=symbol, side=side, qty=qty, price=price)
        self.fills.append(fill)
        return fill


class ReplayEngine:
    def __init__(self, strategy):
        self.strategy = strategy
        self.broker = SimBroker()

    async def feed(self, symbol: str, bar: Dict[str, Any]) -> None:
        await self.strategy.on_features(symbol, bar)
