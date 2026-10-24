# broker/router.py
from __future__ import annotations
from typing import Dict, Any, Optional
from sim_adapter import SimAdapter
from broker.adapter import BrokerAdapter


class DualRouter:
    """
    Dual routing for shadow/canary/production modes.

    Modes:
      - "shadow": All orders to SimAdapter (no real broker)
      - "canary": Fraction to real broker, rest to SimAdapter
      - "prod": All orders to real broker
    """
    def __init__(self, sim: SimAdapter, real: BrokerAdapter, mode: str="shadow", canary_ratio: float=0.1, slo=None):
        self.sim = sim
        self.real = real
        self.mode = mode
        self.canary_ratio = float(canary_ratio)
        self.slo = slo

    async def route(self, order: Dict[str,Any], ref_px: float):
        """
        Route order based on mode.

        Args:
            order: Order dict
            ref_px: Reference price for SLO tracking

        Returns:
            Response dict with real and shadow execution results
        """
        sym = order["symbol"]
        side = order["side"]
        qty = int(order["qty"])

        if self.mode == "prod":
            # Production mode: all to real broker
            r = self.real.place_order(sym, side, qty, order.get("type","MKT"), order.get("limit_price"))
            if not r.get("ok"):
                if self.slo:
                    self.slo.record_order_failure()
            return {"real": r, "shadow": None}

        elif self.mode == "canary":
            # Canary mode: split between real and shadow
            q_real = max(0, int(round(qty * self.canary_ratio)))
            q_shadow = qty - q_real

            r_real = None
            if q_real > 0:
                r_real = self.real.place_order(sym, side, q_real, order.get("type","MKT"), order.get("limit_price"))
                if not r_real.get("ok") and self.slo:
                    self.slo.record_order_failure()

            if q_shadow > 0:
                o2 = dict(order)
                o2["qty"] = q_shadow
                await self.sim.submit_order(o2)

            return {"real": r_real or {"ok":True}, "shadow": {"ok": True}}

        else:  # shadow
            # Shadow mode: all to SimAdapter
            await self.sim.submit_order(order)
            return {"real": None, "shadow": {"ok": True}}
