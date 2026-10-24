from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List


ORDER_STATUSES = {
    "created",
    "submitted",
    "accepted",
    "partially_filled",
    "filled",
    "canceled",
    "rejected",
    "expired",
}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BrokerOrder:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    status: str
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    candidate_id: str = ""
    experiment_id: str = ""
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "candidate_id": self.candidate_id,
            "experiment_id": self.experiment_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class BrokerFill:
    broker_order_id: str
    client_order_id: str
    symbol: str
    qty: int
    fill_price: float
    ts: str = field(default_factory=iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "broker_order_id": self.broker_order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": self.qty,
            "fill_price": self.fill_price,
            "ts": self.ts,
        }


@dataclass
class BrokerPosition:
    symbol: str
    qty: int
    avg_price: float

    def to_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "qty": self.qty, "avg_price": self.avg_price}


@dataclass
class BrokerAccount:
    cash: float
    buying_power: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    day_pnl: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cash": self.cash,
            "buying_power": self.buying_power,
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "day_pnl": self.day_pnl,
        }


@dataclass
class BrokerSession:
    session_id: str
    broker_profile: str
    capabilities: List[str]
    created_at: str = field(default_factory=iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "broker_profile": self.broker_profile,
            "capabilities": self.capabilities,
            "created_at": self.created_at,
        }

