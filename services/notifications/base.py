from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class NotificationEvent:
    """Normalized notification payload for order lifecycle events."""

    event_type: str
    status: str
    strategy_id: str
    symbol: str
    side: str
    qty: float
    order_type: str
    decision_id: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id: Optional[int] = None
    broker_order_id: Optional[str] = None
    perm_id: Optional[int] = None
    order_ref: str = ""
    order_role: str = ""
    price: Optional[float] = None
    amount_usd: Optional[float] = None
    fill_qty: Optional[float] = None
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class Notifier(ABC):
    """Abstract notification sink."""

    @abstractmethod
    async def send(self, event: NotificationEvent) -> bool:
        """Send one event and return success status."""

    async def close(self) -> None:
        """Optional cleanup hook."""
        return None
