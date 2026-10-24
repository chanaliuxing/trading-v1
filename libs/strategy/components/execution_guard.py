"""
ExecutionGuard Component
Handles trade quotas with Reality-Based Synchronization (V1 Port).
"""
import asyncio
from typing import Any, Dict, Optional, Set
from datetime import datetime
from .base import StrategyComponent

class ExecutionGuard(StrategyComponent):
    def __init__(self, ctx: Any, state: Dict[str, Any], state_lock: asyncio.Lock, max_trades: int = 1):
        super().__init__(ctx, state, state_lock)
        self.max_trades = max_trades
        self._processed_fills: Set[str] = set()
        
        if "success_count" not in self.state: self.state["success_count"] = 0
        if "is_traded" not in self.state: self.state["is_traded"] = False
        if "pending_decision_id" not in self.state: self.state["pending_decision_id"] = None

    async def initialize(self) -> None:
        # V1 Port: Cold Start Reality Check
        await self.sync_with_broker()
        self.logger.info("execution_guard_initialized", max_trades=self.max_trades)

    def can_enter(self) -> bool:
        if self.state.get("is_traded", False): return False
        if self.state["success_count"] >= self.max_trades: return False
        
        pending_id = self.state.get("pending_decision_id")
        if pending_id:
            lock_ts = self.state.get("pending_decision_at", 0)
            if (datetime.now().timestamp() - lock_ts) < 5.0: return False
            else: self.clear_lock()
        return True

    def lock_decision(self, decision_id: str) -> None:
        self.state["pending_decision_id"] = decision_id
        self.state["pending_decision_at"] = datetime.now().timestamp()

    def clear_lock(self) -> None:
        self.state["pending_decision_id"] = None

    async def sync_with_broker(self) -> None:
        """V1 Port: Hard Reality Check (L1800)."""
        try:
            # Check 1: Real Positions & Side Integrity (Issue 6)
            for sym in ("TQQQ", "SQQQ"):
                pos = await self.ctx.position(sym)
                qty = float(getattr(pos, "pos_qty", 0.0) or 0.0)
                if abs(qty) > 0:
                    async with self.state_lock:
                        if not self.state.get("is_traded"):
                            self.logger.warning("reality_sync_locked_is_traded", symbol=sym, qty=qty)
                            self._lock_from_reality(sym)
                        
                        # Side Sign Mismatch Guard (L1810)
                        strategy_side = self.state.get("side")
                        if strategy_side:
                            # Both TQQQ and SQQQ are 'LONG' strategies in the instrument
                            if qty < 0:
                                self.logger.error("POSITION_SIDE_MISMATCH", symbol=sym, actual=qty, expected="LONG")
                                raise RuntimeError("CRITICAL_SIDE_MISMATCH")
                    return

            # Check 2: Open Orders (Ghost/Orphan Detection)
            open_orders = await self.ctx.open_orders()
            for o in open_orders:
                if o.strategy_id == self.ctx.strategy_id and o.symbol in ("TQQQ", "SQQQ"):
                    async with self.state_lock:
                        if not self.state.get("is_traded"):
                            self.logger.warning("reality_sync_locked_via_orphan_order", order_id=o.order_id)
                            self._lock_from_reality(o.symbol)
                    return
        except Exception as e:
            self.logger.error("reality_sync_failed", error=str(e))

    def _lock_from_reality(self, symbol: str) -> None:
        self.state["is_traded"] = True
        self.state["success_count"] = 1
        self.state["exec_symbol"] = symbol
        self.state["pending_decision_id"] = None

    async def on_order_update(self, update: Any) -> None:
        if getattr(update, "state", "") == "FILLED":
            order_id = str(getattr(update, "broker_order_id", ""))
            if not order_id: return
            async with self.state_lock:
                if order_id not in self._processed_fills:
                    self._processed_fills.add(order_id)
                    if self.state["success_count"] < self.max_trades:
                        self.state["success_count"] += 1
                        self.state["is_traded"] = True
                        self.state["pending_decision_id"] = None
