from __future__ import annotations

import asyncio
import time
import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Optional, Protocol


def _now_us() -> int:
    return int(time.time() * 1_000_000)


class RuntimeLifecycle(str, Enum):
    FLAT = "flat"
    PREVIEW = "preview"
    INTENT_SUBMITTED = "intent_submitted"
    PENDING_ENTRY = "pending_entry"
    LIVE = "live"
    PENDING_EXIT = "pending_exit"
    QUARANTINE = "quarantine"
    BROKER_UNKNOWN = "broker_unknown"


@dataclass(frozen=True)
class RuntimeState:
    strategy_id: str
    symbol: str
    lifecycle: RuntimeLifecycle = RuntimeLifecycle.FLAT
    target_qty: float = 0.0
    live_qty: float = 0.0
    broker_qty: Optional[float] = None
    side: Optional[str] = None
    active_trigger_id: Optional[str] = None
    ownership_token: Optional[str] = None
    entry_order_ref: Optional[str] = None
    protective_order_refs: tuple[str, ...] = field(default_factory=tuple)
    quarantine_reason: Optional[str] = None
    last_error: Optional[str] = None
    last_synced_at_us: int = 0
    updated_at_us: int = field(default_factory=_now_us)

    def evolve(self, **changes: object) -> "RuntimeState":
        if "updated_at_us" not in changes:
            changes["updated_at_us"] = _now_us()
        return replace(self, **changes)


class RuntimeStore(Protocol):
    async def get_state(self, strategy_id: str, symbol: str) -> Optional[RuntimeState]:
        ...

    async def put_state(self, state: RuntimeState) -> RuntimeState:
        ...

    async def mutate_state(
        self,
        strategy_id: str,
        symbol: str,
        mutator: Callable[[Optional[RuntimeState]], RuntimeState],
    ) -> RuntimeState:
        ...

    async def delete_state(self, strategy_id: str, symbol: str) -> None:
        ...

    async def list_states(self, strategy_id: Optional[str] = None) -> list[RuntimeState]:
        ...


class InMemoryRuntimeStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: dict[tuple[str, str], RuntimeState] = {}

    @staticmethod
    def _key(strategy_id: str, symbol: str) -> tuple[str, str]:
        return str(strategy_id or "").strip(), str(symbol or "").strip().upper()

    async def get_state(self, strategy_id: str, symbol: str) -> Optional[RuntimeState]:
        async with self._lock:
            return self._states.get(self._key(strategy_id, symbol))

    async def put_state(self, state: RuntimeState) -> RuntimeState:
        key = self._key(state.strategy_id, state.symbol)
        async with self._lock:
            normalized = state.evolve(
                strategy_id=key[0],
                symbol=key[1],
            )
            self._states[key] = normalized
            return normalized

    async def mutate_state(
        self,
        strategy_id: str,
        symbol: str,
        mutator: Callable[[Optional[RuntimeState]], RuntimeState],
    ) -> RuntimeState:
        key = self._key(strategy_id, symbol)
        async with self._lock:
            current = self._states.get(key)
            updated = mutator(current)
            normalized = updated.evolve(
                strategy_id=key[0],
                symbol=key[1],
            )
            self._states[key] = normalized
            return normalized

    async def delete_state(self, strategy_id: str, symbol: str) -> None:
        async with self._lock:
            self._states.pop(self._key(strategy_id, symbol), None)

    async def list_states(self, strategy_id: Optional[str] = None) -> list[RuntimeState]:
        strategy_filter = str(strategy_id or "").strip() if strategy_id else None
        async with self._lock:
            states = list(self._states.values())
        if strategy_filter is None:
            return sorted(states, key=lambda s: (s.strategy_id, s.symbol))
        return sorted(
            [state for state in states if state.strategy_id == strategy_filter],
            key=lambda s: (s.strategy_id, s.symbol),
        )


_SHARED_RUNTIME_STORES: dict[str, InMemoryRuntimeStore] = {}
_SHARED_RUNTIME_STORES_LOCK = threading.Lock()


def get_shared_runtime_store(scope: str = "index_super_win") -> InMemoryRuntimeStore:
    key = str(scope or "index_super_win").strip() or "index_super_win"
    with _SHARED_RUNTIME_STORES_LOCK:
        store = _SHARED_RUNTIME_STORES.get(key)
        if store is None:
            store = InMemoryRuntimeStore()
            _SHARED_RUNTIME_STORES[key] = store
        return store
