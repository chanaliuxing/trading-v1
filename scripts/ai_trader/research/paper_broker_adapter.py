from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from scripts.ai_trader.research.broker_models import (
    BrokerAccount,
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    iso_now,
)


class PaperBrokerAdapter:
    def __init__(self, *, session_id: str, broker_profile: str = "paper") -> None:
        self.session_id = session_id
        self.broker_profile = broker_profile
        self._orders: Dict[str, BrokerOrder] = {}
        self._client_to_broker: Dict[str, str] = {}
        self._fills: List[BrokerFill] = []
        self._positions: Dict[str, BrokerPosition] = {}
        self._seq = 0
        self._account = BrokerAccount(
            cash=100_000.0,
            buying_power=100_000.0,
            equity=100_000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            day_pnl=0.0,
        )

    def _next_broker_order_id(self) -> str:
        self._seq += 1
        return f"{self.session_id}-brk-{self._seq:06d}"

    def submit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        candidate_id: str,
        experiment_id: str,
        behavior: str = "filled",
    ) -> Dict[str, Any]:
        broker_order_id = self._next_broker_order_id()
        order = BrokerOrder(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            symbol=symbol,
            side=side,
            qty=int(max(qty, 0)),
            price=float(max(price, 0.0)),
            status="accepted",
            candidate_id=candidate_id,
            experiment_id=experiment_id,
        )
        self._orders[broker_order_id] = order
        self._client_to_broker[client_order_id] = broker_order_id

        if behavior == "rejected":
            order.status = "rejected"
            order.updated_at = iso_now()
            return order.to_dict()

        if behavior == "partial":
            fill_qty = max(1, order.qty // 2)
            order.status = "partially_filled"
        else:
            fill_qty = order.qty
            order.status = "filled"

        fill_price = round(order.price * 1.0005, 4)
        order.filled_qty = fill_qty
        order.avg_fill_price = fill_price
        order.updated_at = iso_now()

        self._fills.append(
            BrokerFill(
                broker_order_id=order.broker_order_id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                qty=fill_qty,
                fill_price=fill_price,
            )
        )
        signed_qty = fill_qty if order.side.upper() == "BUY" else -fill_qty
        prev = self._positions.get(order.symbol)
        if prev is None:
            self._positions[order.symbol] = BrokerPosition(symbol=order.symbol, qty=signed_qty, avg_price=fill_price)
        else:
            new_qty = prev.qty + signed_qty
            if new_qty == 0:
                self._positions.pop(order.symbol, None)
            else:
                prev.avg_price = round((prev.avg_price * abs(prev.qty) + fill_price * abs(signed_qty)) / max(abs(new_qty), 1), 4)
                prev.qty = new_qty

        notional = fill_qty * fill_price
        if order.side.upper() == "BUY":
            self._account.cash -= notional
        else:
            self._account.cash += notional
        self._account.buying_power = self._account.cash
        self._account.equity = self._account.cash
        return order.to_dict()

    def cancel_order(self, *, broker_order_id: str) -> Dict[str, Any]:
        order = self._orders.get(broker_order_id)
        if order is None:
            return {"ok": False, "reason": "order_not_found", "broker_order_id": broker_order_id}
        if order.status in {"filled", "canceled", "rejected", "expired"}:
            return {"ok": False, "reason": "not_cancelable", "broker_order_id": broker_order_id}
        order.status = "canceled"
        order.updated_at = iso_now()
        return {"ok": True, "order": order.to_dict()}

    def get_order(self, *, broker_order_id: str) -> Dict[str, Any]:
        order = self._orders.get(broker_order_id)
        return order.to_dict() if order else {}

    def list_open_orders(self) -> List[Dict[str, Any]]:
        return [
            x.to_dict()
            for x in self._orders.values()
            if x.status in {"accepted", "partially_filled", "submitted", "created"}
        ]

    def get_positions(self) -> List[Dict[str, Any]]:
        return [x.to_dict() for x in self._positions.values()]

    def get_account(self) -> Dict[str, Any]:
        return self._account.to_dict()

    def flatten_all(self) -> Dict[str, Any]:
        flattened: List[Dict[str, Any]] = []
        current = list(self._positions.values())
        for pos in current:
            if pos.qty == 0:
                continue
            side = "SELL" if pos.qty > 0 else "BUY"
            qty = abs(pos.qty)
            oid = f"flatten-{pos.symbol}-{self._next_broker_order_id()}"
            result = self.submit_order(
                client_order_id=oid,
                symbol=pos.symbol,
                side=side,
                qty=qty,
                price=pos.avg_price or 100.0,
                candidate_id="*",
                experiment_id="flatten_all",
                behavior="filled",
            )
            flattened.append(result)
        return {"flattened_orders": flattened}

    def export_state(self) -> Dict[str, Any]:
        fills_by_order = defaultdict(int)
        for fill in self._fills:
            fills_by_order[fill.broker_order_id] += fill.qty
        return {
            "session_id": self.session_id,
            "broker_profile": self.broker_profile,
            "orders": [x.to_dict() for x in self._orders.values()],
            "fills": [x.to_dict() for x in self._fills],
            "positions": [x.to_dict() for x in self._positions.values()],
            "account": self._account.to_dict(),
            "fills_by_order": dict(fills_by_order),
            "client_to_broker": dict(self._client_to_broker),
        }

