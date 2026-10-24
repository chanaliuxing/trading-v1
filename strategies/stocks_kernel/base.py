from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from libs.strategy import OrderUpdate, PositionSnapshot, StrategyAlgorithm, StrategyParams


class EquityKernelStrategy(StrategyAlgorithm, ABC):
    """
    Base class for the new stock strategy kernel.

    This stays compatible with the existing StrategyEngine/StrategyContext
    contract and intentionally avoids introducing a parallel runtime model.
    """

    def __init__(self, ctx, params: StrategyParams):
        super().__init__(ctx, params)
        self.kernel_state: Dict[str, Any] = {}

    @abstractmethod
    async def initialize(self) -> None:
        pass

    @abstractmethod
    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        pass

    async def on_order_update(self, update: OrderUpdate) -> None:
        return None

    async def on_position_update(self, position: PositionSnapshot) -> None:
        return None

    async def on_shutdown(self, reason: str) -> None:
        return None
