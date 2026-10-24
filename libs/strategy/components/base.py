"""
Strategy Component Base Class
Unified interface with built-in concurrency protection.
"""
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from datetime import datetime

class StrategyComponent(ABC):
    def __init__(self, ctx: Any, state: Dict[str, Any], state_lock: asyncio.Lock):
        """
        Args:
            ctx: StrategyContext
            state: Shared persistent state dictionary
            state_lock: Concurrency lock for atomic state access
        """
        self.ctx = ctx
        self.state = state
        self.state_lock = state_lock
        self.logger = ctx.log

    @abstractmethod
    async def initialize(self) -> None: pass

    async def on_tick(self, symbol: str, price: float, ts: datetime) -> None: pass
    async def on_order_update(self, update: Any) -> None: pass
    async def on_position_update(self, position: Any) -> None: pass

    def get_state_snapshot(self) -> Dict[str, Any]:
        return {}
