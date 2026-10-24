"""Lightweight simulated execution adapter for research backtests."""

from __future__ import annotations

import math
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

from obs.audit import REASONS, log_event

try:  # optional tracing
    from opentelemetry import trace
except Exception:  # pragma: no cover
    trace = None  # type: ignore

_TRACER = trace.get_tracer("trader.exec") if trace else None


class _NullOverlay:
    """Fallback overlay with no risk constraints."""

    def adjust_order(self, symbol: str, side: str, qty: int, ref_px: float) -> int:
        return qty

    def on_fill(self, symbol: str, side: str, qty: int, price: float) -> None:
        return

    @property
    def halted(self) -> bool:
        return False

    @property
    def state(self):
        class _S:
            peak_nav = 0.0
            halted = False

        return _S()


@dataclass
class _Position:
    qty: float = 0.0
    avg_cost: float = 0.0
    last_px: float = 0.0
    last_ts: int = 0
    realized_pnl: float = 0.0


class SimAdapter:
    """Cost-aware simulator used by research backtests and HPO."""

    def __init__(
        self,
        *,
        starting_cash: float = 1_000_000.0,
        commission_per_share: float = 0.005,
        commission_min: float = 1.0,
        slippage_bps: float = 1.0,
        dynamic_spread_bps: float = 0.0,
        borrow_rate: float = 0.0,
        participation_cap: float = 0.1,
        fill_ref: str = "open",
        overlay: Optional[Any] = None,
        slo: Optional[Any] = None,
    ) -> None:
        self.cash = float(starting_cash)
        self.positions: Dict[str, _Position] = {}
        self.ledger: list[dict[str, float]] = []
        self._last_features: Dict[str, Dict[str, Any]] = {}
        self.commission_per_share = float(commission_per_share)
        self.commission_min = float(commission_min)
        self.slippage_bps = float(slippage_bps)
        self.dynamic_spread_bps = float(dynamic_spread_bps)
        self.borrow_rate = float(borrow_rate)
        self.participation_cap = float(participation_cap)
        self.fill_ref = fill_ref
        self.overlay = overlay or _NullOverlay()
        self.slo = slo

    # ------------------------------------------------------------------
    async def on_market_data(self, features: Dict[str, Any]) -> None:
        symbol = str(features.get("symbol"))
        ts_raw = features.get("ts", int(time.time() * 1000))
        # Handle pandas Timestamp objects
        if hasattr(ts_raw, 'timestamp'):
            ts = int(ts_raw.timestamp() * 1000)
        else:
            ts = int(ts_raw)
        px = float(features.get("close", features.get("open", 0.0)))
        self._last_features[symbol] = features

        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = _Position(qty=0.0, avg_cost=0.0, last_px=px, last_ts=ts)
            return

        # Borrow fee for shorts
        if pos.qty < 0 and self.borrow_rate > 0:
            dt_sec = max(0.0, (ts - pos.last_ts) / 1000.0)
            if dt_sec > 0:
                notional = abs(pos.qty) * (pos.last_px if pos.last_px > 0 else px)
                borrow_fee = notional * self.borrow_rate * (dt_sec / (365.0 * 24 * 3600))
                self.cash -= borrow_fee
                pos.realized_pnl -= borrow_fee
                self.ledger.append({
                    "ts": float(ts),
                    "symbol": symbol,
                    "side": "BORROW",
                    "qty": 0.0,
                    "price": 0.0,
                    "fee": float(borrow_fee),
                    "realized_pnl": float(-borrow_fee),
                    "cash_delta": float(-borrow_fee)
                })

        pos.last_px = px
        pos.last_ts = ts

    # ------------------------------------------------------------------
    async def submit_order(self, order: Dict[str, Any]) -> None:
        if getattr(self.overlay, "halted", False):
            if self.slo:
                try:
                    self.slo.record_order_failure()
                except Exception:
                    pass
            return

        symbol = str(order.get("symbol"))
        side = str(order.get("side", "BUY")).upper()
        req_qty = int(order.get("qty", 0))
        typ = str(order.get("type", "MKT")).upper()
        limit_px = order.get("limit_price")
        ts = int(order.get("ts", int(time.time() * 1000)))

        span_cm = _TRACER.start_as_current_span(
            "order",
            attributes={
                "symbol": symbol,
                "side": side,
                "req.qty": req_qty,
                "type": typ,
            },
        ) if _TRACER else nullcontext()

        with span_cm as span:  # type: ignore[attr-defined]
            if getattr(self.overlay, "halted", False):
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute("blocked", True)
                    span.set_attribute("reason", "kill_switch")
                log_event(
                    "order_rejected",
                    component="SimAdapter",
                    symbol=symbol,
                    side=side,
                    qty=req_qty,
                    reason="kill_switch",
                    reason_detail=REASONS.get("kill_switch", "halted"),
                )
                if self.slo:
                    try:
                        self.slo.record_order_failure()
                    except Exception:
                        pass
                return

            if req_qty <= 0:
                if self.slo:
                    try:
                        self.slo.record_order_failure()
                    except Exception:
                        pass
                return

            features = self._last_features.get(symbol)
            if not features:
                return

            if self.slo:
                try:
                    self.slo.record_order_attempt()
                except Exception:
                    pass

            ref_px = float(features.get(self.fill_ref, features.get("close", features.get("open", 0.0))))
            if ref_px <= 0:
                if self.slo:
                    try:
                        self.slo.record_order_failure()
                    except Exception:
                        pass
                return

            adj_qty = req_qty
            if hasattr(self.overlay, "adjust_order"):
                try:
                    adj_qty = int(self.overlay.adjust_order(symbol, side, req_qty, ref_px))
                except Exception:
                    adj_qty = req_qty
            if adj_qty <= 0:
                log_event(
                    "overlay_block",
                    component="Overlay",
                    symbol=symbol,
                    side=side,
                    req_qty=req_qty,
                    adj_qty=adj_qty,
                    reason="overlay_limit",
                    reason_detail=REASONS["overlay_limit"],
                )
                if self.slo:
                    try:
                        self.slo.record_order_failure()
                    except Exception:
                        pass
                return

            # Participation cap
            bar_vol = float(features.get("volume", 0.0))
            cap_qty = adj_qty
            if bar_vol > 0 and self.participation_cap > 0:
                cap_qty = min(adj_qty, max(1, int(bar_vol * self.participation_cap)))
            fill_qty = max(0, cap_qty)
            if fill_qty <= 0:
                log_event(
                    "order_rejected",
                    component="SimAdapter",
                    symbol=symbol,
                    side=side,
                    req_qty=req_qty,
                    adj_qty=adj_qty,
                    bar_vol=bar_vol,
                    reason="participation_cap",
                    reason_detail=REASONS["participation_cap"],
                )
                if self.slo:
                    try:
                        self.slo.record_order_failure()
                    except Exception:
                        pass
                return
            if fill_qty < adj_qty:
                log_event(
                    "order_trimmed",
                    component="SimAdapter",
                    symbol=symbol,
                    side=side,
                    req_qty=req_qty,
                    adj_qty=adj_qty,
                    fill_qty=fill_qty,
                    bar_vol=bar_vol,
                    reason="participation_cap",
                    reason_detail=REASONS["participation_cap"],
                )

            # Limit check and fill price determination
            if typ == "LMT" and limit_px is not None:
                # For limit orders, check if they would fill based on bar OHLC
                bar_high = float(features.get("high", ref_px))
                bar_low = float(features.get("low", ref_px))

                if side == "BUY":
                    # BUY limit fills if bar low <= limit price
                    if bar_low > float(limit_px) + 1e-9:
                        log_event(
                            "order_rejected",
                            component="SimAdapter",
                            symbol=symbol,
                            side=side,
                            qty=req_qty,
                            limit_price=float(limit_px),
                            bar_low=bar_low,
                            reason="limit_reject",
                            reason_detail=REASONS["limit_reject"],
                        )
                        if self.slo:
                            try:
                                self.slo.record_order_failure()
                            except Exception:
                                pass
                        return
                    # Fill at limit price (best case for buyer)
                    fill_px = float(limit_px)
                else:
                    # SELL limit fills if bar high >= limit price
                    if bar_high < float(limit_px) - 1e-9:
                        log_event(
                            "order_rejected",
                            component="SimAdapter",
                            symbol=symbol,
                            side=side,
                            qty=req_qty,
                            limit_price=float(limit_px),
                            bar_high=bar_high,
                            reason="limit_reject",
                            reason_detail=REASONS["limit_reject"],
                        )
                        if self.slo:
                            try:
                                self.slo.record_order_failure()
                            except Exception:
                                pass
                        return
                    # Fill at limit price (best case for seller)
                    fill_px = float(limit_px)
            else:
                # Market order: apply slippage + dynamic spread
                slip = ref_px * (self.slippage_bps / 1e4)
                slip += ref_px * (self.dynamic_spread_bps / 1e4)
                fill_px = ref_px + (slip if side == "BUY" else -slip)

            fee = max(self.commission_min, self.commission_per_share * fill_qty)

            signed_qty = float(fill_qty if side == "BUY" else -fill_qty)
            pos = self.positions.setdefault(symbol, _Position())
            prev_qty = pos.qty

            realized = 0.0
            if prev_qty != 0 and (prev_qty > 0) != (signed_qty > 0):
                closing = min(abs(prev_qty), abs(signed_qty))
                pnl_per = (fill_px - pos.avg_cost) if prev_qty > 0 else (pos.avg_cost - fill_px)
                realized = closing * pnl_per

            new_qty = prev_qty + signed_qty
            if (prev_qty >= 0 and signed_qty >= 0) or (prev_qty <= 0 and signed_qty <= 0):
                total = abs(prev_qty) + abs(signed_qty)
                if total > 0:
                    pos.avg_cost = (abs(prev_qty) * pos.avg_cost + abs(signed_qty) * fill_px) / total
            elif new_qty == 0:
                pos.avg_cost = 0.0

            pos.qty = new_qty
            pos.last_px = fill_px
            pos.last_ts = ts

            cash_delta = -fill_px * signed_qty - fee
            self.cash += cash_delta

            pos.realized_pnl -= fee
            if realized != 0.0:
                pos.realized_pnl += realized

            net_realized = realized - fee

            self.ledger.append({
                "ts": float(ts),
                "symbol": symbol,
                "side": side,
                "qty": float(fill_qty),
                "price": float(fill_px),
                "fee": float(fee),
                "realized_pnl": float(net_realized),
                "cash_delta": float(cash_delta)
            })

            if span is not None and hasattr(span, "set_attribute"):
                span.set_attribute("fill.qty", float(fill_qty))
                span.set_attribute("fill.price", float(fill_px))
                span.set_attribute("fee", float(fee))

            log_event(
                "order_fill",
                component="SimAdapter",
                symbol=symbol,
                side=side,
                qty=fill_qty,
                price=fill_px,
                fee=fee,
                slippage_bps=self.slippage_bps + self.dynamic_spread_bps,
            )

            if self.slo:
                try:
                    self.slo.record_slippage_bps(ref_px, fill_px, side)
                except Exception:
                    pass

            if hasattr(self.overlay, "on_fill"):
                try:
                    self.overlay.on_fill(symbol, side, fill_qty, float(fill_px))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    def current_nav(self, px_map: Optional[Dict[str, float]] = None) -> float:
        nav = self.cash
        for sym, pos in self.positions.items():
            px = pos.last_px
            if px_map and sym in px_map:
                px = float(px_map[sym])
            nav += pos.qty * px
        return float(nav)

    # ------------------------------------------------------------------
    def get_trade_ledger(self) -> List[Dict[str, float]]:
        """Return a shallow copy of the trade ledger for analytics."""
        return [dict(entry) for entry in self.ledger]

    def snapshot(self, px_map: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Lightweight portfolio snapshot used by backtests/HPO."""
        nav = self.current_nav(px_map)
        positions = {
            sym: {
                "qty": float(pos.qty),
                "avg_cost": float(pos.avg_cost),
                "last_px": float(pos.last_px),
                "realized_pnl": float(pos.realized_pnl)
            }
            for sym, pos in self.positions.items()
        }
        return {
            "cash": float(self.cash),
            "equity": float(nav),
            "positions": positions
        }
