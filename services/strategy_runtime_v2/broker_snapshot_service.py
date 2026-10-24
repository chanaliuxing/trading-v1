from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol


def _now_us() -> int:
    return int(time.time() * 1_000_000)


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    symbol: str
    qty: float
    avg_px: Optional[float] = None
    account: Optional[str] = None
    source: str = "broker"
    ts_us: int = field(default_factory=_now_us)


@dataclass(frozen=True)
class BrokerOpenOrderSnapshot:
    broker_order_id: str
    symbol: str
    side: str
    qty: float
    order_type: str
    state: str
    order_ref: str = ""
    strategy_id: Optional[str] = None
    account: Optional[str] = None
    source: str = "broker"
    ts_us: int = field(default_factory=_now_us)


class BrokerPositionSource(Protocol):
    async def positions(self) -> Iterable[Any]:
        ...


class BrokerOpenOrderSource(Protocol):
    async def open_orders(self) -> Iterable[Any]:
        ...


class BrokerSnapshotService:
    def __init__(
        self,
        *,
        position_source: Optional[BrokerPositionSource] = None,
        open_order_source: Optional[BrokerOpenOrderSource] = None,
    ) -> None:
        self.position_source = position_source
        self.open_order_source = open_order_source

    async def _call_source(self, fn: Any) -> list[Any]:
        if fn is None:
            return []
        result = fn()
        if inspect.isawaitable(result):
            result = await result
        return list(result or [])

    @staticmethod
    def _read_field(obj: Any, *names: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            for name in names:
                if name in obj and obj.get(name) is not None:
                    return obj.get(name)
            return default
        for name in names:
            if hasattr(obj, name):
                value = getattr(obj, name)
                if value is not None:
                    return value
        return default

    async def read_positions(self, symbols: Optional[Iterable[str]] = None) -> list[BrokerPositionSnapshot]:
        if self.position_source is None:
            return []
        allowed = {str(sym or "").strip().upper() for sym in (symbols or []) if str(sym or "").strip()}
        rows = await self._call_source(self.position_source.positions)
        snapshots: list[BrokerPositionSnapshot] = []
        for row in rows:
            symbol = str(
                self._read_field(row, "symbol", default=None)
                or self._read_field(self._read_field(row, "contract", default=None), "symbol", default="")
            ).strip().upper()
            if not symbol:
                continue
            if allowed and symbol not in allowed:
                continue
            qty = float(self._read_field(row, "qty", "pos_qty", "position", default=0.0) or 0.0)
            avg_px_raw = self._read_field(row, "avg_px", "avgCost", default=None)
            avg_px = float(avg_px_raw) if avg_px_raw not in {None, ""} else None
            account = self._read_field(row, "account", default=None)
            snapshots.append(
                BrokerPositionSnapshot(
                    symbol=symbol,
                    qty=qty,
                    avg_px=avg_px,
                    account=str(account) if account not in {None, ""} else None,
                )
            )
        return snapshots

    async def read_position(self, symbol: str) -> Optional[BrokerPositionSnapshot]:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return None
        rows = await self.read_positions(symbols=[symbol_u])
        if not rows:
            return None
        qty = sum(float(row.qty) for row in rows)
        if abs(qty) <= 1e-12:
            return BrokerPositionSnapshot(symbol=symbol_u, qty=0.0)
        weighted_notional = sum(abs(float(row.qty)) * float(row.avg_px or 0.0) for row in rows if row.avg_px is not None)
        avg_px = weighted_notional / max(sum(abs(float(row.qty)) for row in rows if row.avg_px is not None), 1e-12)
        account = next((row.account for row in rows if row.account), None)
        return BrokerPositionSnapshot(
            symbol=symbol_u,
            qty=float(qty),
            avg_px=float(avg_px) if weighted_notional > 0 else None,
            account=account,
        )

    async def read_open_orders(self, symbols: Optional[Iterable[str]] = None) -> list[BrokerOpenOrderSnapshot]:
        if self.open_order_source is None:
            return []
        allowed = {str(sym or "").strip().upper() for sym in (symbols or []) if str(sym or "").strip()}
        rows = await self._call_source(self.open_order_source.open_orders)
        snapshots: list[BrokerOpenOrderSnapshot] = []
        for row in rows:
            symbol = str(
                self._read_field(row, "symbol", default=None)
                or self._read_field(self._read_field(row, "contract", default=None), "symbol", default="")
            ).strip().upper()
            if not symbol:
                continue
            if allowed and symbol not in allowed:
                continue
            broker_order_id = str(self._read_field(row, "broker_order_id", "order_id", "id", default="") or "").strip()
            if not broker_order_id:
                continue
            side = str(self._read_field(row, "side", default="")).strip().upper() or "UNKNOWN"
            qty = float(self._read_field(row, "qty", "totalQuantity", default=0.0) or 0.0)
            order_type = str(
                self._read_field(row, "order_type", default=None)
                or self._read_field(self._read_field(row, "order", default=None), "orderType", default="")
            ).strip().upper() or "UNKNOWN"
            state = str(
                self._read_field(row, "state", default=None)
                or self._read_field(self._read_field(row, "orderStatus", default=None), "status", default="")
            ).strip().upper() or "UNKNOWN"
            snapshots.append(
                BrokerOpenOrderSnapshot(
                    broker_order_id=broker_order_id,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_type=order_type,
                    state=state,
                    order_ref=str(self._read_field(row, "order_ref", default="") or "").strip(),
                    strategy_id=str(self._read_field(row, "strategy_id", default="") or "").strip() or None,
                    account=str(self._read_field(row, "account", default="") or "").strip() or None,
                )
            )
        return snapshots
