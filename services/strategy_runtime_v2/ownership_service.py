from __future__ import annotations

import asyncio
import time
import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional, Protocol


def _now_us() -> int:
    return int(time.time() * 1_000_000)


class OwnershipState(str, Enum):
    UNCLAIMED = "unclaimed"
    CLAIMED = "claimed"
    QUARANTINED = "quarantined"


class OwnershipConflictError(RuntimeError):
    pass


class OwnershipQuarantinedError(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnershipRecord:
    symbol: str
    state: OwnershipState = OwnershipState.UNCLAIMED
    owner_strategy_id: Optional[str] = None
    claimed_qty: float = 0.0
    trigger_id: Optional[str] = None
    order_namespace: str = "iswv2"
    broker_qty: Optional[float] = None
    quarantine_reason: Optional[str] = None
    acquired_at_us: int = field(default_factory=_now_us)
    updated_at_us: int = field(default_factory=_now_us)

    def evolve(self, **changes: object) -> "OwnershipRecord":
        if "updated_at_us" not in changes:
            changes["updated_at_us"] = _now_us()
        return replace(self, **changes)


class PositionOwnershipService(Protocol):
    async def get(self, symbol: str) -> OwnershipRecord:
        ...

    async def claim(
        self,
        *,
        strategy_id: str,
        symbol: str,
        qty: float,
        trigger_id: Optional[str] = None,
        order_namespace: str = "iswv2",
    ) -> OwnershipRecord:
        ...

    async def release(self, *, strategy_id: str, symbol: str, force: bool = False) -> OwnershipRecord:
        ...

    async def quarantine(
        self,
        *,
        symbol: str,
        reason: str,
        broker_qty: Optional[float] = None,
        owner_strategy_id: Optional[str] = None,
    ) -> OwnershipRecord:
        ...

    async def is_owned_by(self, *, strategy_id: str, symbol: str) -> bool:
        ...


class InMemoryPositionOwnershipService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[str, OwnershipRecord] = {}

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return str(symbol or "").strip().upper()

    def _default_record(self, symbol: str) -> OwnershipRecord:
        return OwnershipRecord(symbol=self._normalize_symbol(symbol))

    async def get(self, symbol: str) -> OwnershipRecord:
        symbol_u = self._normalize_symbol(symbol)
        async with self._lock:
            return self._records.get(symbol_u, self._default_record(symbol_u))

    async def claim(
        self,
        *,
        strategy_id: str,
        symbol: str,
        qty: float,
        trigger_id: Optional[str] = None,
        order_namespace: str = "iswv2",
    ) -> OwnershipRecord:
        symbol_u = self._normalize_symbol(symbol)
        strategy_u = str(strategy_id or "").strip()
        async with self._lock:
            current = self._records.get(symbol_u, self._default_record(symbol_u))
            if current.state == OwnershipState.QUARANTINED:
                raise OwnershipQuarantinedError(
                    f"symbol={symbol_u} quarantine_reason={current.quarantine_reason or 'unknown'}"
                )
            if current.state == OwnershipState.CLAIMED and current.owner_strategy_id != strategy_u:
                raise OwnershipConflictError(
                    f"symbol={symbol_u} owner={current.owner_strategy_id} contender={strategy_u}"
                )
            next_record = current.evolve(
                state=OwnershipState.CLAIMED,
                owner_strategy_id=strategy_u,
                claimed_qty=float(qty),
                trigger_id=str(trigger_id or "").strip() or None,
                order_namespace=str(order_namespace or "iswv2").strip() or "iswv2",
                quarantine_reason=None,
                broker_qty=None,
            )
            self._records[symbol_u] = next_record
            return next_record

    async def release(self, *, strategy_id: str, symbol: str, force: bool = False) -> OwnershipRecord:
        symbol_u = self._normalize_symbol(symbol)
        strategy_u = str(strategy_id or "").strip()
        async with self._lock:
            current = self._records.get(symbol_u, self._default_record(symbol_u))
            if current.state == OwnershipState.CLAIMED and not force and current.owner_strategy_id not in {None, strategy_u}:
                raise OwnershipConflictError(
                    f"symbol={symbol_u} owner={current.owner_strategy_id} contender={strategy_u}"
                )
            released = self._default_record(symbol_u).evolve(
                acquired_at_us=current.acquired_at_us,
            )
            self._records[symbol_u] = released
            return released

    async def quarantine(
        self,
        *,
        symbol: str,
        reason: str,
        broker_qty: Optional[float] = None,
        owner_strategy_id: Optional[str] = None,
    ) -> OwnershipRecord:
        symbol_u = self._normalize_symbol(symbol)
        async with self._lock:
            current = self._records.get(symbol_u, self._default_record(symbol_u))
            quarantined = current.evolve(
                state=OwnershipState.QUARANTINED,
                owner_strategy_id=str(owner_strategy_id or "").strip() or current.owner_strategy_id,
                quarantine_reason=str(reason or "").strip() or "unknown",
                broker_qty=float(broker_qty) if broker_qty is not None else current.broker_qty,
            )
            self._records[symbol_u] = quarantined
            return quarantined

    async def is_owned_by(self, *, strategy_id: str, symbol: str) -> bool:
        record = await self.get(symbol)
        return record.state == OwnershipState.CLAIMED and record.owner_strategy_id == str(strategy_id or "").strip()


_SHARED_OWNERSHIP_SERVICES: dict[str, InMemoryPositionOwnershipService] = {}
_SHARED_OWNERSHIP_SERVICES_LOCK = threading.Lock()


def get_shared_ownership_service(scope: str = "index_super_win") -> InMemoryPositionOwnershipService:
    key = str(scope or "index_super_win").strip() or "index_super_win"
    with _SHARED_OWNERSHIP_SERVICES_LOCK:
        svc = _SHARED_OWNERSHIP_SERVICES.get(key)
        if svc is None:
            svc = InMemoryPositionOwnershipService()
            _SHARED_OWNERSHIP_SERVICES[key] = svc
        return svc
