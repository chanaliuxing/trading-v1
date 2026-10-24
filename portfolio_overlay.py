# portfolio_overlay.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List
import math
import time
from collections import deque

from obs.audit import REASONS, log_event


@dataclass
class OverlayConfig:
    vol_target_annual: float = 0.15       # Annual target volatility (15%)
    vol_window: int = 390                 # ~1 day window for 1m bars
    max_position_pct: float = 0.08        # Single position max 8% of NAV
    max_gross: float = 2.0                # Total gross exposure / NAV
    max_net: float = 0.5                  # Net exposure / NAV
    rebalance: str = "bar"                # "bar" | "daily"
    max_drawdown: float = 0.35            # Circuit breaker at 35% drawdown
    turnover_bps_budget: float = 30.0     # Soft turnover constraint
    min_lot: int = 1                      # Minimum lot size
    vol_floor: float = 0.05               # Min vol estimate (prevent over-leverage)
    vol_cap: float = 0.8                  # Max vol estimate (prevent de-risking)
    enable: bool = True


@dataclass
class PortfolioState:
    # Rolling portfolio returns for vol estimation
    rets: deque = field(default_factory=lambda: deque(maxlen=390*5))  # ~1 week
    peak_nav: float = 1.0
    halted: bool = False
    last_rebalance_day: Optional[int] = None
    leverage: float = 1.0


class PortfolioOverlay:
    """
    Portfolio-level risk control and leverage management.
    Intercepts and adjusts all orders before execution.

    External interface:
      - on_bar(symbol, px, nav, positions, ts)
      - limit_order(order, nav, positions) -> adjusted_qty
    """
    def __init__(self, cfg: OverlayConfig):
        self.cfg = cfg
        self.state = PortfolioState()
        self._px: Dict[str, float] = {}

    def _estimate_annual_vol(self) -> float:
        """Estimate annualized volatility from rolling returns."""
        if len(self.state.rets) < 10:
            return self.cfg.vol_target_annual  # Cold start
        import numpy as np
        s = float(np.std(self.state.rets))
        return max(1e-9, s * math.sqrt(252.0))

    def _recalc_leverage(self):
        """Recalculate leverage based on current vs target volatility."""
        cur_vol = self._estimate_annual_vol()
        # Target leverage ≈ vol_target / max(cur_vol, vol_floor)
        lev = self.cfg.vol_target_annual / max(self.cfg.vol_floor, cur_vol)
        # Clamp to reasonable bounds
        lev = min(max(lev, 1.0/self.cfg.vol_cap), 1.0/self.cfg.vol_floor)
        self.state.leverage = float(lev)

    def on_bar(self, symbol: str, px: float, nav: float, positions: Dict[str, Dict[str, float]], ts: int):
        """Called every bar to update state and check circuit breakers."""
        if not self.cfg.enable:
            return

        self._px[symbol] = float(px)

        # Drawdown circuit breaker
        self.state.peak_nav = max(self.state.peak_nav, nav)
        dd = 0.0 if self.state.peak_nav <= 0 else (self.state.peak_nav - nav)/self.state.peak_nav
        if dd >= self.cfg.max_drawdown:
            if not self.state.halted:
                self.state.halted = True
                log_event(
                    "overlay_block",
                    component="Overlay",
                    symbol=symbol,
                    reason="kill_switch",
                    reason_detail=REASONS.get("kill_switch", "halted"),
                    nav=float(nav),
                    peak_nav=float(self.state.peak_nav),
                    drawdown=float(dd),
                )

        # Rebalance rhythm
        if self.cfg.rebalance == "daily":
            day = int(ts // (1000*60*60*24))
            if self.state.last_rebalance_day != day:
                self.state.last_rebalance_day = day
                self._recalc_leverage()
        else:
            self._recalc_leverage()

    def _projected_exposures(self, order: Dict[str, Any], nav: float, positions: Dict[str, Dict[str, float]]) -> Tuple[float,float,float]:
        """
        Calculate projected exposures after order execution.

        Returns:
            (gross_after, net_after, pos_pct_after_sym)
            gross = sum(|qty_i*px_i|)/NAV
            net = sum(qty_i*px_i)/NAV
        """
        sym = order["symbol"]
        side = order["side"].upper()
        qty  = int(order["qty"])
        px   = float(order.get("price_hint") or order.get("bar", {}).get("close") or
                     order.get("bar", {}).get("open") or self._px.get(sym, 0.0))

        if px <= 0 or nav <= 0:
            return 0.0, 0.0, 0.0

        # Current positions
        cur = {s: p.get("qty",0) for s, p in positions.items()}
        cur_val = {s: (p.get("qty",0) * self._px.get(s, px)) for s,p in positions.items()}
        cur_gross = sum(abs(v) for v in cur_val.values()) / nav
        cur_net   = sum(v for v in cur_val.values()) / nav

        # After order execution
        sym_new_qty = cur.get(sym, 0) + (qty if side=="BUY" else -qty)
        sym_after_val = sym_new_qty * px
        gross_after = ( (cur_gross*nav) - abs(cur.get(sym,0)*px) + abs(sym_after_val) ) / nav
        net_after   = ( (cur_net*nav)   - (cur.get(sym,0)*px) + sym_after_val ) / nav
        pos_pct_after = abs(sym_after_val)/nav

        return float(gross_after), float(net_after), float(pos_pct_after)

    def limit_order(self, order: Dict[str, Any], nav: float, positions: Dict[str, Dict[str, float]]) -> int:
        """
        Apply risk limits to order and return adjusted quantity.
        May reduce qty to zero if constraints violated.

        Args:
            order: Order dict with symbol, side, qty
            nav: Current NAV
            positions: Current positions dict

        Returns:
            Adjusted quantity (0 if order rejected)
        """
        if not self.cfg.enable or self.state.halted:
            log_event(
                "overlay_block",
                component="Overlay",
                symbol=order.get("symbol"),
                side=order.get("side"),
                req_qty=int(order.get("qty", 0)),
                reason="kill_switch" if self.state.halted else "overlay_limit",
                reason_detail=REASONS.get("kill_switch", "halted") if self.state.halted else REASONS.get("overlay_limit"),
            )
            return 0 if self.state.halted else int(order["qty"])

        qty = int(order["qty"])
        if qty == 0 or nav <= 0:
            return 0

        # Leverage scaling (soft constraint)
        lev = self.state.leverage
        qty = int(max(self.cfg.min_lot, round(qty * lev)))

        # Hard constraints: position limit, gross/net limits
        # Binary search to find max qty satisfying all constraints
        lo, hi = 0, qty
        best = 0
        tries = 0

        while lo <= hi and tries < 20:
            mid = (lo + hi)//2
            tmp = dict(order)
            tmp["qty"] = mid
            g_after, n_after, pos_pct = self._projected_exposures(tmp, nav, positions)

            ok_pos  = pos_pct <= self.cfg.max_position_pct + 1e-9
            ok_gross= g_after <= self.cfg.max_gross + 1e-9
            ok_net  = abs(n_after) <= self.cfg.max_net + 1e-9

            if ok_pos and ok_gross and ok_net:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
            tries += 1

        # Turnover budget (soft constraint) - hook for future implementation
        adjusted = int(max(0, best))
        if adjusted == 0:
            log_event(
                "overlay_block",
                component="Overlay",
                symbol=order.get("symbol"),
                side=order.get("side"),
                req_qty=int(order.get("qty", 0)),
                reason="overlay_limit",
                reason_detail=REASONS["overlay_limit"],
            )
        elif adjusted < int(order.get("qty", 0)):
            log_event(
                "overlay_block",
                component="Overlay",
                symbol=order.get("symbol"),
                side=order.get("side"),
                req_qty=int(order.get("qty", 0)),
                adj_qty=adjusted,
                reason="overlay_limit",
                reason_detail=REASONS["overlay_limit"],
            )
        return adjusted
