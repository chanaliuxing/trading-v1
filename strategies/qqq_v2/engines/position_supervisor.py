"""
PositionSupervisor Engine (QQQ V2)
Coordinates trade lifecycle with Hard Reality Pulls and Blocking States.
"""
import asyncio
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional
from libs.strategy.components.execution_guard import ExecutionGuard
from libs.strategy.components.protective_shield import ProtectiveShield
from libs.strategy import DecisionEnvelope, make_decision_id, OrderStyle, Freshness

class PositionSupervisor:
    def __init__(self, ctx: Any, state: Dict[str, Any], guard: ExecutionGuard, shield: ProtectiveShield):
        self.ctx = ctx
        self.state = state
        self.guard = guard
        self.shield = shield
        self.logger = ctx.log
        self._is_dead = False 

    async def enter(self, symbol: str, side: str, qty: int, atr_pre: float, current_price: float) -> None:
        if self._is_dead: return
        initial_stop = self.shield.compute_initial_stop(current_price, atr_pre)
        decision_id = make_decision_id()
        envelope = DecisionEnvelope(
            decision_id=decision_id, strategy_id=self.ctx.strategy_id, symbol=symbol,
            ts_bar_end_us=int(datetime.now().timestamp() * 1_000_000), 
            target_qty=float(qty), freshness=Freshness(ttl_ms=2000, dlb_ms=500),
            order_style=OrderStyle(kind="MKT", tif="DAY", outside_rth=False), 
            extras={"reason": "qqq_v2_entry"}
        )
        self.guard.lock_decision(decision_id)
        self.state["exec_symbol"] = symbol; self.state["side"] = side; self.state["is_traded"] = True
        await self.ctx.submit_intent(envelope)

    async def manage(self, now: datetime, current_price: float, q_vwap: float, atr_live: Optional[float] = None) -> None:
        """Called every loop iteration. V1 Port: Uses centralized 'now' clock."""
        if not self.state.get("is_traded") or self._is_dead: return
        
        # V1 Port: Hard Reality Check (L2115). Sync qty before any action.
        real_qty = abs(await self._get_current_qty())
        if real_qty <= 0:
            if not self.state.get("is_flattening"):
                self.logger.warning("manage_halted_position_zeroed")
                return 

        # 1. Stop & Target Checks
        stop_px = self.state.get("stop_px")
        if stop_px and current_price <= stop_px:
            await self.flatten("stop_hit"); return

        target_dist = self.state.get("target", 1.2); entry_px = self.state.get("entry_px", 0.0); side = self.state.get("side")
        if entry_px > 0:
            if side == "LONG" and current_price >= entry_px + target_dist:
                await self.flatten("target_hit"); return
            elif side == "SHORT" and current_price <= entry_px - target_dist:
                await self.flatten("target_hit"); return

        # 2. Logic Retreat (Minute Heartbeat)
        if self.state.get("last_retreat_eval_min") != now.strftime("%H:%M"):
            self.state["last_retreat_eval_min"] = now.strftime("%H:%M")
            wrong_side = (side == "LONG" and current_price < q_vwap) or (side == "SHORT" and current_price > q_vwap)
            if wrong_side: await self.flatten("logic_retreat"); return

        # 3. Update Stop (5m Boundary Control)
        m5 = (now.minute // 5) * 5
        bar_5m_end = now.replace(minute=m5, second=0, microsecond=0) + timedelta(minutes=5)
        if self.state.get("last_stop_update_5m_end") != bar_5m_end.isoformat():
            new_stop, reason = await self.shield.manage_stop(current_price, atr_live)
            if new_stop:
                # V1 Port: Pass real_qty to modify intent
                await self._modify_stop_order(new_stop, reason, force_qty=real_qty)
                self.state["last_stop_update_5m_end"] = bar_5m_end.isoformat()

    async def flatten(self, reason: str) -> None:
        symbol = self.state.get("exec_symbol")
        if not symbol or self.state.get("is_flattening") or self._is_dead: return
        self.state["is_flattening"] = True; self._is_dead = True 
        try:
            await self.ctx.engine.broker_adapter.cancel_strategy_orders(self.ctx.strategy_id)
            await asyncio.sleep(0.5)
        except Exception: pass
        envelope = DecisionEnvelope(
            decision_id=make_decision_id(), strategy_id=self.ctx.strategy_id, symbol=symbol,
            ts_bar_end_us=int(datetime.now().timestamp() * 1_000_000), 
            target_qty=0.0, order_style=OrderStyle(kind="MKT", tif="DAY", outside_rth=False),
            extras={"reason": reason, "flatten_action": True, "order_ref": "FORCE_FLATTEN"}
        )
        await self.ctx.submit_intent(envelope)

    async def _modify_stop_order(self, new_stop: float, reason: str, force_qty: Optional[float] = None) -> None:
        if self._is_dead: return
        now_ts = datetime.now().timestamp()
        if (now_ts - self.state.get("last_stop_modify_ts", 0)) < 30.0 and reason != "qty_sync": return
        symbol = self.state.get("exec_symbol")
        # V1 Port: Use current broker reality for qty (L2115)
        qty = force_qty or abs(await self._get_current_qty())
        if qty <= 0: return
        envelope = DecisionEnvelope(
            decision_id=make_decision_id(), strategy_id=self.ctx.strategy_id, symbol=symbol,
            ts_bar_end_us=int(datetime.now().timestamp() * 1_000_000), 
            target_qty=0.0, order_style=OrderStyle(kind="STP", stop_px=new_stop, tif="DAY", outside_rth=False),
            extras={"reason": reason, "protective_stop": True, "qty": qty}
        )
        await self.ctx.submit_intent(envelope); self.state["last_stop_modify_ts"] = now_ts

    async def _get_current_qty(self) -> float:
        symbol = self.state.get("exec_symbol")
        pos = await self.ctx.position(symbol)
        return float(getattr(pos, "pos_qty", 0.0) or 0.0)
