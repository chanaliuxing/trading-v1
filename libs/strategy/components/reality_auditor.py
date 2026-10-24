"""
RealityAuditor Component
Ensures the resting Stop Loss order exists in the broker's system at all times.
- 1.2s high-frequency audit rhythm (safe via local cache)
- 8-consecutive-miss threshold (approx 10s)
- Automatic Kill Switch (Market Flatten) on persistent audit failure
"""
import asyncio
import asyncio
from typing import Any, Dict, Optional, Callable, Awaitable
from datetime import datetime
from .base import StrategyComponent

class RealityAuditor(StrategyComponent):
    def __init__(self, ctx: Any, state: Dict[str, Any], state_lock: asyncio.Lock,
                 audit_interval: float = 1.2,
                 max_miss_count: int = 8):
        super().__init__(ctx, state, state_lock)
        self.audit_interval = audit_interval
        self.max_miss_count = max_miss_count
        self._audit_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Initialize state keys
        if "audit_miss_count" not in self.state:
            self.state["audit_miss_count"] = 0

    async def initialize(self) -> None:
        self.logger.info("reality_auditor_initialized", 
                         interval=self.audit_interval,
                         max_misses=self.max_miss_count)

    async def start(self, flatten_callback: Callable[[str], Awaitable[None]]) -> None:
        """Starts the high-frequency audit background loop."""
        if self._is_running:
            return
        
        self._is_running = True
        self._audit_task = asyncio.create_task(
            self._audit_loop(flatten_callback),
            name=f"audit_loop:{self.ctx.strategy_id}"
        )
        self._audit_task.add_done_callback(self._handle_task_done)

    def _handle_task_done(self, task: asyncio.Task) -> None:
        """V1 Port: Prevent silent background task death."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error("audit_loop_crashed_critically", error=str(e), exc_info=True)
            import signal, os
            os.kill(os.getpid(), signal.SIGTERM)

    async def stop(self) -> None:
        """Stops the audit background loop."""
        self._is_running = False
        if self._audit_task:
            self._audit_task.cancel()
            try:
                await self._audit_task
            except asyncio.CancelledError:
                pass
            self._audit_task = None

    async def _audit_loop(self, flatten_callback: Callable[[str], Awaitable[None]]) -> None:
        """The main 1.2s audit rhythm."""
        self.logger.info("audit_loop_started", strategy_id=self.ctx.strategy_id)
        
        while self._is_running:
            try:
                # Only audit if the strategy thinks it's traded and has an active symbol
                is_traded = self.state.get("is_traded", False)
                exec_symbol = self.state.get("exec_symbol")
                
                if is_traded and exec_symbol:
                    # Ask Context for open orders (Reads from ib_insync memory cache)
                    orders = await self.ctx.open_orders()
                    
                    # Logic match: Does a valid protective order exist?
                    found = False
                    for o in orders:
                        if (o.strategy_id == self.ctx.strategy_id and 
                            o.symbol == exec_symbol and 
                            o.state in ("NEW", "SUBMITTED", "PARTIAL", "PRESUBMITTED")):
                            # We found an active order for this strategy
                            found = True
                            break
                    
                    if found:
                        if self.state["audit_miss_count"] > 0:
                            self.logger.info("audit_recovered", symbol=exec_symbol)
                        self.state["audit_miss_count"] = 0
                    else:
                        self.state["audit_miss_count"] += 1
                        self.logger.warning("audit_miss_detected", 
                                            symbol=exec_symbol, 
                                            count=self.state["audit_miss_count"])

                    # KILL SWITCH: 8 consecutive misses (~10 seconds)
                    if self.state["audit_miss_count"] >= self.max_miss_count:
                        self.logger.error("KILL_SWITCH_AUDIT_FAILURE", 
                                          reason=f"Stop loss missing for {self.max_miss_count} checks")
                        await flatten_callback("STOP_LOSS_MISSING_KILL_SWITCH")
                        # After kill switch, we stop auditing until next trade
                        self.state["audit_miss_count"] = 0 

            except Exception as exc:
                self.logger.warning("audit_loop_exception", error=str(exc))
            
            await asyncio.sleep(self.audit_interval)

    def get_state_snapshot(self) -> Dict[str, Any]:
        return {"audit_miss_count": self.state.get("audit_miss_count", 0)}
