# -*- coding: utf-8 -*-
"""
Simulated Broker Adapter (vectorized-friendly)
----------------------------------------------
- Multi-symbol strict mark-to-market (per-symbol last price cache)
- Order types: MARKET / MKT_LIMITABLE / LIMIT / STOP
- Intrabar trigger logic with gap handling
- Slippage (bps), commission (per share + min)
- Positions, cash, realized PnL
- Trade ledger for closed trades (for accurate win-rate / profit factor)
"""

from __future__ import annotations
import math, uuid
from contextlib import nullcontext
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

# Optional portfolio overlay for risk management
try:
    from portfolio_overlay import PortfolioOverlay
    HAS_OVERLAY = True
except ImportError:
    HAS_OVERLAY = False
    PortfolioOverlay = None

from obs.audit import log_event, REASONS

try:
    from opentelemetry import trace
except Exception:  # pragma: no cover
    trace = None  # type: ignore

_TRACER = trace.get_tracer("trader.exec") if trace else None

# -------- Data structures --------

@dataclass
class Order:
    order_id: str
    decision_id: str
    ts: int                     # event time (ms)
    symbol: str
    side: str                   # "BUY" or "SELL"
    qty: float
    type: str                   # "MARKET" | "LIMIT" | "STOP" | "MKT_LIMITABLE"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    tif: str = "DAY"
    tag: Optional[str] = None
    status: str = "NEW"         # NEW, WORKING, FILLED, CANCELLED, REJECTED
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0

@dataclass
class Fill:
    fill_id: str
    order_id: str
    ts: int
    symbol: str
    side: str
    qty: float
    price: float
    commission: float = 0.0
    slippage_cost: float = 0.0

@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

@dataclass
class Portfolio:
    cash: float = 100_000.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    equity: float = 100_000.0
    last_mark_ts: Optional[int] = None

def _now_ms() -> int:
    return int(datetime.now(ZoneInfo("America/New_York")).timestamp() * 1000)

def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"

class SimAdapter:
    """
    Deterministic simulator with multi-symbol MTM and trade ledger.
    Price book is provided by bar features: keys 'open','high','low','close'.
    """

    def __init__(self,
                 initial_cash: float = 100_000.0,
                 commission_per_share: float = 0.005,
                 commission_min: float = 1.0,
                 slippage_bps: float = 1.0,
                 dynamic_spread_bps: float = 0.0,
                 borrow_rate: float = 0.0,
                 participation_cap: float = 0.1,
                 fill_ref: str = "open",  # 'open' or 'close'
                 overlay: Optional[Any] = None,
                 slo: Optional[Any] = None
                 ) -> None:
        self.portfolio = Portfolio(cash=initial_cash, equity=initial_cash)
        self.commission_per_share = commission_per_share
        self.commission_min = commission_min
        self.slippage_bps = slippage_bps
        self.dynamic_spread_bps = dynamic_spread_bps
        self.borrow_rate = borrow_rate
        self.participation_cap = participation_cap
        self.fill_ref = fill_ref
        self.overlay = overlay  # Optional PortfolioOverlay for risk management
        self.slo = slo
        self._orders: Dict[str, Order] = {}
        self._working: Dict[str, Order] = {}
        self._on_fill_callbacks: List[Callable[[Fill], None]] = []
        self._on_status_callbacks: List[Callable[[Order], None]] = []

        # Per-symbol last price cache for strict MTM
        self._last_prices: Dict[str, float] = {}
        self._last_borrow_ts: Dict[str, int] = {}

        # Trade ledger for closed fragments
        # Each entry: {ts, symbol, side, qty, entry_price, exit_price, pnl, commission}
        self._closed_trades: List[Dict[str, float]] = []

    # --------- Callback wiring ---------

    def on_fill(self, fn: Callable[[Fill], None]) -> None:
        self._on_fill_callbacks.append(fn)

    def on_status(self, fn: Callable[[Order], None]) -> None:
        self._on_status_callbacks.append(fn)

    def _emit_status(self, order: Order) -> None:
        for fn in self._on_status_callbacks:
            try:
                fn(order)
            except Exception:
                pass

    def _emit_fill(self, fill: Fill) -> None:
        for fn in self._on_fill_callbacks:
            try:
                fn(fill)
            except Exception:
                pass

    # --------- Public API ---------

    def submit(self, order: Order) -> str:
        span_cm = _TRACER.start_as_current_span(
            "order_submit",
            attributes={
                "order_id": order.order_id,
                "decision_id": order.decision_id,
                "symbol": order.symbol,
                "side": order.side,
                "req.qty": float(order.qty),
                "type": order.type,
            },
        ) if _TRACER else nullcontext()

        with span_cm as span:  # type: ignore[attr-defined]
            if self.slo:
                try:
                    self.slo.record_order_attempt()
                except Exception:
                    pass

            if order.qty <= 0:
                log_event(
                    "order_rejected",
                    component="SimAdapter",
                    symbol=order.symbol,
                    side=order.side,
                    reason="zero_signal",
                    reason_detail=REASONS["zero_signal"],
                )
                order.status = "REJECTED"
                self._emit_status(order)
                return order.order_id

        # Apply overlay order limiting (position sizing, exposure limits, circuit breaker)
        original_qty = order.qty
        if self.overlay is not None:
            nav = self.portfolio.equity
            positions_dict = {sym: {"qty": p.qty, "avg_price": p.avg_price}
                             for sym, p in self.portfolio.positions.items()}
            order_dict = {
                "symbol": order.symbol,
                "side": order.side,
                "qty": order.qty
            }
            if hasattr(self.overlay, "adjust_order"):
                try:
                    limited_qty = int(self.overlay.adjust_order(order.symbol, order.side, order.qty, self._last_prices.get(order.symbol, 0.0)))
                except Exception:
                    limited_qty = order.qty
            elif hasattr(self.overlay, "limit_order"):
                limited_qty = self.overlay.limit_order(order_dict, nav, positions_dict)
            else:
                limited_qty = order.qty
            order.qty = limited_qty

            # Reject if overlay reduced qty to 0 or we're halted
            if limited_qty <= 0 or getattr(self.overlay, "halted", False):
                reason = "kill_switch" if getattr(self.overlay, "halted", False) else "overlay_limit"
                log_event(
                    "overlay_block",
                    component="Overlay",
                    symbol=order.symbol,
                    side=order.side,
                    req_qty=float(original_qty),
                    adj_qty=0,
                    reason=reason,
                    reason_detail=REASONS.get(reason, reason),
                )
                order.status = "REJECTED"
                if self.slo:
                    try:
                        self.slo.record_order_failure()
                    except Exception:
                        pass
                self._emit_status(order)
                return order.order_id
            elif limited_qty < original_qty:
                log_event(
                    "overlay_block",
                    component="Overlay",
                    symbol=order.symbol,
                    side=order.side,
                    req_qty=float(original_qty),
                    adj_qty=float(limited_qty),
                    reason="overlay_limit",
                    reason_detail=REASONS["overlay_limit"],
                )

        log_event(
            "order_submitted",
            component="SimAdapter",
            symbol=order.symbol,
            side=order.side,
            qty=float(order.qty),
            type=order.type,
        )

        if span is not None and hasattr(span, "set_attribute"):
            span.set_attribute("accepted.qty", float(order.qty))

        order.status = "WORKING"
        self._orders[order.order_id] = order
        self._working[order.order_id] = order
        self._emit_status(order)
        return order.order_id

    def cancel(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        if not o or o.status in ("CANCELLED","FILLED"):
            return
        o.status = "CANCELLED"
        self._working.pop(order_id, None)
        self._emit_status(o)

    def cancel_all(self, symbol: Optional[str] = None) -> None:
        for oid, o in list(self._working.items()):
            if symbol and o.symbol != symbol:
                continue
            self.cancel(oid)

    # --------- Core mechanics ---------

    def on_bar(self, ts_ms: int, symbol: str, bar: Dict[str, float]) -> None:
        """
        Called once per bar per symbol. bar must include open/high/low/close.
        1) Try to execute orders
        2) Update last price for symbol
        3) Mark-to-market equity across ALL symbols
        4) Update overlay state if present
        """
        span_cm = _TRACER.start_as_current_span(
            "bar",
            attributes={
                "symbol": symbol,
                "ts_ms": ts_ms,
            },
        ) if _TRACER else nullcontext()

        with span_cm:
            mark_price = bar.get("close", bar.get("open"))
            prev_ts = self._last_borrow_ts.get(symbol)
            if prev_ts is not None and self.borrow_rate > 0:
                dt_sec = max(0.0, (ts_ms - prev_ts) / 1000.0)
                if dt_sec > 0:
                    pos = self.portfolio.positions.get(symbol)
                    if pos and pos.qty < 0:
                        last_px = self._last_prices.get(symbol, pos.avg_price or mark_price or 0.0)
                        notional = abs(pos.qty) * last_px
                        borrow_fee = notional * self.borrow_rate * (dt_sec / (365.0 * 24 * 3600.0))
                        if borrow_fee > 0:
                            self.portfolio.cash -= borrow_fee
                            self.portfolio.realized_pnl -= borrow_fee
                            self._closed_trades.append({
                                "ts": ts_ms,
                                "symbol": symbol,
                                "qty": 0.0,
                                "entry_price": 0.0,
                                "exit_price": 0.0,
                                "side": "BORROW",
                                "pnl": -borrow_fee,
                                "commission": 0.0
                            })
            self._last_borrow_ts[symbol] = ts_ms

            bar_vol = float(bar.get("volume", 0.0) or 0.0)

            # 1) Try to execute market & triggered orders
            for oid, o in list(self._working.items()):
                if o.symbol != symbol:
                    continue
                filled = False
                fill_price = None

                oref = bar.get(self.fill_ref, bar.get("open"))
                high = bar.get("high"); low = bar.get("low"); open_ = bar.get("open")

                if o.type == "MARKET":
                    base = oref
                    slip = base * (self.slippage_bps / 10_000.0)
                    slip += base * (self.dynamic_spread_bps / 10_000.0)
                    fill_price = base + (slip if o.side == "BUY" else -slip)
                    filled = True

                elif o.type == "MKT_LIMITABLE":
                    base = oref
                    slip = base * (self.slippage_bps / 10_000.0)
                    slip += base * (self.dynamic_spread_bps / 10_000.0)
                    px = base + (slip if o.side == "BUY" else -slip)
                    if o.limit_price is not None:
                        if o.side == "BUY":
                            px = min(px, o.limit_price)
                        else:
                            px = max(px, o.limit_price)
                    fill_price = px
                    filled = True

                elif o.type == "LIMIT":
                    if o.side == "BUY":
                        if low is not None and o.limit_price is not None and low <= o.limit_price:
                            exec_price = min(open_, o.limit_price)
                            fill_price = exec_price
                            filled = True
                    else:  # SELL
                        if high is not None and o.limit_price is not None and high >= o.limit_price:
                            exec_price = max(open_, o.limit_price)
                            fill_price = exec_price
                            filled = True

                elif o.type == "STOP":
                    if o.side == "BUY":
                        if high is not None and o.stop_price is not None and high >= o.stop_price:
                            exec_price = max(open_, o.stop_price)
                            slip = exec_price * (self.dynamic_spread_bps / 10_000.0)
                            fill_price = exec_price + slip
                            filled = True
                    else:  # SELL
                        if low is not None and o.stop_price is not None and low <= o.stop_price:
                            exec_price = min(open_, o.stop_price)
                            slip = exec_price * (self.dynamic_spread_bps / 10_000.0)
                            fill_price = exec_price - slip
                            filled = True

                if filled and fill_price is not None:
                    remaining = float(abs(o.qty))
                    if remaining <= 0:
                        continue
                    cap_qty = remaining
                    if bar_vol > 0 and self.participation_cap > 0:
                        cap_qty = min(remaining, max(1.0, bar_vol * self.participation_cap))
                    fill_qty = max(0.0, min(remaining, cap_qty))
                    if fill_qty <= 0:
                        continue
                    if fill_qty < remaining:
                        log_event(
                            "order_trimmed",
                            component="SimAdapter",
                            symbol=symbol,
                            side=o.side,
                            req_qty=float(remaining),
                            fill_qty=float(fill_qty),
                            bar_vol=bar_vol,
                            reason="participation_cap",
                            reason_detail=REASONS.get("participation_cap"),
                        )
                    self._fill_order(o, ts_ms, fill_price, fill_qty)
                    bar_vol = max(0.0, bar_vol - fill_qty)

            # 2) Update last price for strict MTM
            if mark_price is not None:
                self._last_prices[symbol] = float(mark_price)

            # 3) Mark-to-market equity across ALL symbols (strict)
            self._mark_equity(ts_ms)

            # 4) Update overlay state (volatility, circuit breaker, peak tracking)
            if self.overlay is not None:
                nav = self.portfolio.equity
                positions_dict = {sym: {"qty": p.qty, "avg_price": p.avg_price}
                                 for sym, p in self.portfolio.positions.items()}
                self.overlay.on_bar(symbol, mark_price or 0.0, nav, positions_dict, ts_ms)

    # --------- Internals ---------

    def _commission(self, qty: float) -> float:
        fee = max(self.commission_min, abs(qty) * self.commission_per_share)
        return round(fee, 4)

    def _record_closed_fragment(self, ts_ms: int, symbol: str, closing_qty: float, entry_price: float, exit_price: float, side_long: bool, commission: float) -> float:
        """
        Record a closed fragment for ledger and return realized pnl of this fragment.
        """
        # side_long indicates the direction of the position being closed
        pnl = (exit_price - entry_price) * closing_qty if side_long else (entry_price - exit_price) * closing_qty
        self._closed_trades.append({
            "ts": ts_ms,
            "symbol": symbol,
            "qty": float(closing_qty),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "side": "LONG" if side_long else "SHORT",
            "pnl": float(pnl - commission),
            "commission": float(commission)
        })
        return pnl

    def _fill_order(self, o: Order, ts_ms: int, price: float, fill_qty: float) -> None:
        qty_signed = float(fill_qty if o.side == "BUY" else -fill_qty)
        pos = self.portfolio.positions.get(o.symbol) or Position(symbol=o.symbol)

        # If reducing/closing existing position, realize pnl fragment(s)
        realized = 0.0
        commission_alloc = self._commission(abs(qty_signed))
        if pos.qty != 0 and (pos.qty > 0) != (qty_signed > 0):
            # Closing direction
            closing_qty = min(abs(pos.qty), abs(qty_signed))
            # Attribute a proportional commission to the closing fragment
            close_comm = commission_alloc * (closing_qty / abs(qty_signed)) if abs(qty_signed) > 0 else 0.0
            realized_fragment = self._record_closed_fragment(
                ts_ms=ts_ms, symbol=o.symbol, closing_qty=closing_qty,
                entry_price=pos.avg_price, exit_price=price, side_long=(pos.qty > 0),
                commission=close_comm
            )
            realized += realized_fragment

        # Position arithmetic
        new_qty = pos.qty + qty_signed
        if new_qty == 0:
            pos.avg_price = 0.0
        else:
            # Same-side add -> average
            if (pos.qty >= 0 and qty_signed >= 0) or (pos.qty <= 0 and qty_signed <= 0):
                total_cost = pos.avg_price * abs(pos.qty) + price * abs(qty_signed)
                pos.avg_price = total_cost / (abs(pos.qty) + abs(qty_signed))
            # Flip handled by realized fragment above; avg_price becomes the residual side's price when non-zero

        pos.qty = new_qty
        pos.realized_pnl += realized
        self.portfolio.realized_pnl += realized

        # Cash and commission
        cash_delta = -price * qty_signed
        self.portfolio.cash += cash_delta - commission_alloc

        # Finalize order state
        o.filled_qty += abs(qty_signed)
        remaining = max(0.0, abs(o.qty) - abs(qty_signed))
        if remaining <= 0:
            o.status = "FILLED"
            o.qty = 0.0
            self._working.pop(o.order_id, None)
        else:
            o.status = "PARTIAL"
            o.qty = remaining
            self._working[o.order_id] = o
        o.avg_fill_price = price

        fill = Fill(
            fill_id=_uid("fill"),
            order_id=o.order_id,
            ts=ts_ms,
            symbol=o.symbol,
            side=o.side,
            qty=abs(qty_signed),
            price=price,
            commission=commission_alloc,
            slippage_cost=0.0
        )
        self._emit_fill(fill)

        if trace is not None:
            cur_span = trace.get_current_span()
            if cur_span and cur_span.get_span_context().is_valid:
                cur_span.set_attribute("fill.qty", float(abs(qty_signed)))
                cur_span.set_attribute("fill.price", float(price))
                cur_span.set_attribute("fee", float(commission_alloc))

        log_event(
            "order_fill",
            component="SimAdapter",
            symbol=o.symbol,
            side=o.side,
            qty=float(abs(qty_signed)),
            price=float(price),
            fee=float(commission_alloc),
        )

        # Update positions dict
        if pos.qty != 0:
            self.portfolio.positions[o.symbol] = pos
        else:
            self.portfolio.positions.pop(o.symbol, None)

    def _mark_equity(self, ts_ms: int) -> None:
        eq = self.portfolio.cash
        for sym, pos in self.portfolio.positions.items():
            last_px = self._last_prices.get(sym, pos.avg_price or 0.0)
            eq += pos.qty * last_px
        self.portfolio.equity = eq
        self.portfolio.last_mark_ts = ts_ms

    # --------- Helpers for engine ---------

    def build_order(self,
                    decision_id: str,
                    symbol: str,
                    side: str,
                    qty: float,
                    type_: str = "MARKET",
                    limit_price: float | None = None,
                    stop_price: float | None = None,
                    tag: str | None = None,
                    ts_ms: int | None = None
                    ) -> Order:
        return Order(
            order_id=_uid("ord"),
            decision_id=decision_id,
            ts=ts_ms or _now_ms(),
            symbol=symbol, side=side, qty=qty,
            type=type_, limit_price=limit_price, stop_price=stop_price, tag=tag
        )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "cash": self.portfolio.cash,
            "equity": self.portfolio.equity,
            "realized_pnl": self.portfolio.realized_pnl,
            "positions": {k: asdict(v) for k, v in self.portfolio.positions.items()},
            "open_orders": {k: asdict(v) for k, v in self._working.items()},
            "last_prices": dict(self._last_prices),
            "closed_trades": len(self._closed_trades)
        }

    def get_trade_ledger(self) -> List[Dict[str, Any]]:
        return list(self._closed_trades)
