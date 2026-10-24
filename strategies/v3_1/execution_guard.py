"""Execution guard implementing Fix-1/2/3 without core code changes."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GuardResult:
    ok: bool
    status: str
    order_id: Optional[int] = None
    reason: Optional[str] = None


class ExecutionGuard:
    def __init__(self, enable_lock: bool = True, lock_ttl_ms: int = 3000):
        self.enable_lock = enable_lock
        self.lock_ttl_ms = int(lock_ttl_ms)
        self.lock_owner = f"{os.getenv('HOSTNAME', 'host')}-{os.getpid()}"
        self._lock_client = None
        self._ib_adapter = None
        self._order_monitor = None
        self._order_index_lock = asyncio.Lock()
        self._open_by_conid_side: Dict[int, Dict[str, set]] = {}
        self._order_to_legs: Dict[int, List[Tuple[int, str]]] = {}
        self._bound = False

    async def init(self) -> None:
        from services.exec.adapters.ib_adapter import get_ib_adapter
        from services.portfolio.order_monitor import init_global_order_monitor, get_order_monitor
        try:
            adapter = get_ib_adapter()
            if not adapter.is_connected():
                await adapter.start()
            self._ib_adapter = adapter
            if not self._bound and getattr(adapter, "ib", None):
                adapter.ib.orderStatusEvent += self._on_order_event
                if hasattr(adapter.ib, "openOrderEvent"):
                    adapter.ib.openOrderEvent += self._on_order_event
                self._bound = True
            await self._sync_open_orders()
        except Exception as exc:
            logger.warning("v3_1_guard_ib_init_failed", error=str(exc))
        try:
            await init_global_order_monitor()
            self._order_monitor = get_order_monitor()
        except Exception as exc:
            logger.warning("v3_1_guard_order_monitor_failed", error=str(exc))
        if self.enable_lock and self._lock_client is None:
            try:
                import redis

                self._lock_client = redis.from_url(os.getenv("STATEBUS_REDIS", "redis://127.0.0.1:6379/0"), decode_responses=True)
            except Exception as exc:
                logger.warning("v3_1_guard_lock_unavailable", error=str(exc))
                self.enable_lock = False

    async def place_equity_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_type: str = "MKT",
        limit_px: Optional[float] = None,
        tif: str = "DAY",
        allow_cancel_replace: bool = True,
    ) -> GuardResult:
        logger.info("guard_place_order_start", symbol=symbol, side=side, qty=qty)
        if not self._ib_adapter:
            return GuardResult(False, "adapter_missing", reason="IB adapter not ready")

        logger.info("guard_resolving_conid", symbol=symbol)
        conid = await self._get_conid(symbol)
        if not conid:
            logger.warning("guard_conid_missing", symbol=symbol)
            return GuardResult(False, "conid_missing", reason="Failed to resolve conId")

        logger.info("guard_acquiring_lock", symbol=symbol, conid=conid)
        if not await self._acquire_lock([conid]):
            logger.warning("guard_lock_busy", symbol=symbol, conid=conid)
            return GuardResult(False, "lock_busy", reason="ConId lock busy")

        try:
            logger.info("guard_checking_conflicts", symbol=symbol, conid=conid)
            conflict = await self._check_conflicts(conid, side)
            if conflict and allow_cancel_replace:
                logger.info("guard_canceling_conflicts", symbol=symbol, count=len(conflict))
                await self._cancel_conflicts(conflict)
            elif conflict:
                logger.warning("guard_conflict_exists", symbol=symbol)
                return GuardResult(False, "conflict", reason="Open order conflict")

            logger.info("guard_submitting_to_adapter", symbol=symbol, side=side, qty=qty)
            order_ref = f"v3_1_{symbol}_{side}_{int(time.time())}"
            if order_type == "MKT":
                order = await self._ib_adapter.place_market(symbol, side, qty, tif, order_ref)
            else:
                order = await self._ib_adapter.place_limit(symbol, side, qty, limit_px, tif, order_ref)
            
            if not order:
                logger.warning("guard_place_failed_null", symbol=symbol)
                return GuardResult(False, "place_failed", reason="Order placement returned None")
            
            logger.info("guard_place_success", symbol=symbol, order_id=order.order_id)
            return GuardResult(True, "placed", order_id=order.order_id)
        except Exception as exc:
            logger.error("guard_place_exception", symbol=symbol, error=str(exc), exc_info=True)
            return GuardResult(False, "exception", reason=str(exc))
        finally:
            await self._release_lock([conid])

    async def _check_conflicts(self, conid: int, side: str) -> List[int]:
        async with self._order_index_lock:
            bucket = self._open_by_conid_side.get(conid, {})
            conflicts = bucket.get(side.upper(), set()) | bucket.get(_flip_side(side), set())
            return list(conflicts)

    async def _cancel_conflicts(self, order_ids: List[int]) -> None:
        for oid in order_ids:
            try:
                await self._ib_adapter.cancel(oid)
                await self._wait_cancelled(oid)
            except Exception as exc:
                logger.warning("v3_1_guard_cancel_failed", order_id=oid, error=str(exc))

    async def _wait_cancelled(self, order_id: int, timeout_sec: int = 15) -> None:
        if not self._order_monitor:
            await asyncio.sleep(0.5)
            return
        status = await self._order_monitor.wait_for_fill(order_id, timeout_sec=timeout_sec)
        if status.status not in ("Cancelled", "Cancelled", "ApiCancelled", "Inactive"):
            logger.warning("v3_1_guard_cancel_timeout", order_id=order_id, status=status.status)

    def _on_order_event(self, trade: Any) -> None:
        asyncio.create_task(self._handle_order_update(trade))

    async def _handle_order_update(self, trade: Any) -> None:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        if not order or not contract:
            return
        order_id = getattr(order, "orderId", None)
        if order_id is None:
            return
        status = getattr(getattr(trade, "orderStatus", None), "status", None) or "UNKNOWN"
        conid = getattr(contract, "conId", None)
        side = getattr(order, "action", "").upper()
        if not conid or side not in ("BUY", "SELL"):
            return
        await self._update_open_index(int(order_id), [(int(conid), side)], status)

    async def _sync_open_orders(self) -> None:
        if not self._ib_adapter:
            return
        for trade in self._ib_adapter.ib.openTrades():
            await self._handle_order_update(trade)

    async def _update_open_index(self, order_id: int, legs: List[Tuple[int, str]], status: str) -> None:
        is_open = _is_open_status(status)
        async with self._order_index_lock:
            prev = self._order_to_legs.get(order_id, [])
            for conid, side in prev:
                bucket = self._open_by_conid_side.get(conid, {})
                if side in bucket and order_id in bucket[side]:
                    bucket[side].discard(order_id)
            if is_open:
                self._order_to_legs[order_id] = legs
                for conid, side in legs:
                    bucket = self._open_by_conid_side.setdefault(conid, {})
                    bucket.setdefault(side, set()).add(order_id)
            else:
                self._order_to_legs.pop(order_id, None)

    async def _get_conid(self, symbol: str) -> Optional[int]:
        try:
            from ib_insync import Stock
            logger.debug("guard_qualifying_contract", symbol=symbol)
            contract = Stock(symbol, "SMART", "USD")
            conn_manager = None
            try:
                from services.ibkr.ib_connection_manager import get_ib_connection_manager

                conn_manager = get_ib_connection_manager()
            except Exception:
                conn_manager = None
            if conn_manager is not None and hasattr(conn_manager, "allow_market_request"):
                decision = conn_manager.allow_market_request(
                    kind="stock_contract_qualification",
                    key=str(symbol).strip().upper(),
                    requester="v3_1_execution_guard",
                )
                if not decision.allowed:
                    return None
            await asyncio.wait_for(self._ib_adapter.ib.qualifyContractsAsync(contract), timeout=10.0)
            con_id = int(getattr(contract, "conId", 0) or 0)
            if conn_manager is not None and hasattr(conn_manager, "record_market_request_result"):
                conn_manager.record_market_request_result(
                    kind="stock_contract_qualification",
                    key=str(symbol).strip().upper(),
                    ok=con_id > 0,
                    reason=None if con_id > 0 else "conid_missing",
                )
            return con_id
        except asyncio.TimeoutError:
            logger.warning("guard_conid_timeout", symbol=symbol)
            return None
        except Exception as exc:
            logger.warning("guard_conid_failed", symbol=symbol, error=str(exc))
            return None

    async def _acquire_lock(self, conids: List[int]) -> bool:
        if not self.enable_lock or not self._lock_client:
            return True
        acquired = []
        for conid in conids:
            key = f"lock:conid:{conid}"
            ok = self._lock_client.set(key, self.lock_owner, nx=True, px=self.lock_ttl_ms)
            if ok:
                acquired.append(conid)
            else:
                await self._release_lock(acquired)
                return False
        return True

    async def _release_lock(self, conids: List[int]) -> None:
        if not self.enable_lock or not self._lock_client:
            return
        lua = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
        for conid in conids:
            key = f"lock:conid:{conid}"
            try:
                self._lock_client.eval(lua, 1, key, self.lock_owner)
            except Exception:
                continue


def _flip_side(side: str) -> str:
    return "BUY" if side.upper() == "SELL" else "SELL"


def _is_open_status(status: str) -> bool:
    s = (status or "").strip().upper().replace(" ", "")
    return s not in {"FILLED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "APICANCELLED"}
