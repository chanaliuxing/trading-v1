"""
Interactive Brokers adapter using ib_insync
Based on doc/36.md specification
"""
import asyncio
import os
import time
import contextlib
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, List, Dict, Tuple, Callable, Awaitable
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, Index, Option, Order, Contract, Trade, ComboLeg
from prometheus_client import Counter

from ..models import PlacedOrder, RebindResult, RebindStatus, rebind_broker_order_to_owner
from libs.logger import get_logger
from libs.config import config
from services.metrics.exporter import inc_ibkr_disconnect
from services.metrics.exporter import inc_ibkr_probe_failure
from services.metrics.exporter import mark_ibkr_probe_ok
from services.metrics.exporter import set_ibkr_connection_up
from services.ibkr.client_id_registry import get_client_id_registry
from services.ibkr.ib_gateway_circuit import get_ib_gateway_connection_circuit

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")
_EXPECTED_CANCEL_NOT_FOUND_UNTIL: Dict[int, float] = {}
_TERMINAL_ORDER_STATUSES = {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}
_LOCALLY_ACCEPTED_OPEN_STATUSES = {"PreSubmitted", "Submitted"}
_TRANSIENT_SUBMISSION_STATUSES = {"", "PendingSubmit", "ApiPending", "PreSubmitted", "Submitted"}
_LOCAL_PROTECTIVE_PENDING_STATUSES = {"PendingSubmit", "ApiPending"}
_LOCAL_PROTECTIVE_PENDING_GRACE_SEC = 8.0
# Benign IB farm/status chatter that should not be treated as order-path errors.
_IB_INFO_ERROR_CODES = {2104, 2106, 2107, 2108, 2119, 2151, 2158}
_IB_CONNECTIVITY_ERROR_CODES = {1100, 1101, 1102, 1300, 326}
_IB_ORDER_WARNING_CODES = {202, 399}
ib_adapter_epoch_validation_total = Counter(
    "ib_adapter_epoch_validation_total",
    "Runtime identity validation result at IB adapter egress",
    ["result", "operation"],
)


class IBHandleEpochMismatch(RuntimeError):
    """Raised when an IB adapter call is attempted with a stale exec epoch."""

    def __init__(self, *, expected: int, actual: int, operation: str):
        super().__init__(
            f"ib_handle_epoch_mismatch:{operation}:expected={int(expected)}:actual={int(actual)}"
        )
        self.expected = int(expected)
        self.actual = int(actual)
        self.operation = str(operation or "unknown")


def _prune_expected_cancel_not_found(now_ts: Optional[float] = None) -> None:
    now = float(now_ts if now_ts is not None else time.time())
    stale = [oid for oid, until in _EXPECTED_CANCEL_NOT_FOUND_UNTIL.items() if float(until or 0.0) <= now]
    for oid in stale:
        _EXPECTED_CANCEL_NOT_FOUND_UNTIL.pop(int(oid), None)


def mark_expected_cancel_not_found(order_id: int, ttl_sec: float = 60.0) -> None:
    try:
        oid = int(order_id)
    except Exception:
        return
    _prune_expected_cancel_not_found()
    _EXPECTED_CANCEL_NOT_FOUND_UNTIL[oid] = time.time() + max(1.0, float(ttl_sec))


def is_expected_cancel_not_found_message(msg: str) -> bool:
    text = str(msg or "")
    if "Error 10147" not in text or "that needs to be cancelled is not found" not in text:
        return False
    m = re.search(r"reqId\s+(-?\d+)", text)
    if not m:
        return False
    try:
        oid = int(m.group(1))
    except Exception:
        return False
    _prune_expected_cancel_not_found()
    return float(_EXPECTED_CANCEL_NOT_FOUND_UNTIL.get(oid, 0.0) or 0.0) > time.time()


def _now_us() -> int:
    """Get current timestamp in microseconds"""
    return int(time.time() * 1e6)


class IBInsyncAdapter:
    """
    IB adapter using ib_insync library

    Features:
    - Async/await API
    - Automatic reconnection
    - OCO order support via ocaGroup
    - Order reference tracking for reconciliation
    """

    def __init__(self,
                 host: Optional[str] = None,
                 port: Optional[int] = None,
                 client_id: Optional[int] = None,
                 account: Optional[str] = None,
                 default_exchange: str = "SMART",
                 default_currency: str = "USD",
                 primary_exchange_hint: Optional[str] = None,
                 outside_rth: bool = False):
        """
        Initialize IB adapter

        Args:
            host: IB Gateway/TWS host
            port: IB Gateway/TWS port
            client_id: Client ID for connection
            account: Account number (optional)
            default_exchange: Default exchange (SMART)
            default_currency: Default currency (USD)
            primary_exchange_hint: Primary exchange hint for stocks
            outside_rth: Allow trading outside regular hours
        """
        ib_config = config.ib_config

        self.host = host or ib_config['host']
        self.port = port or ib_config['port']
        self.client_id = client_id if client_id is not None else int(get_client_id_registry().resolve("exec"))
        self.account = account or os.getenv("IB_ACCOUNT", "")
        self.exch = default_exchange
        self.ccy = default_currency
        self.primary = primary_exchange_hint
        self.outside_rth = outside_rth or (os.getenv("IB_OUTSIDE_RTH", "false").lower() == "true")

        self.ib = IB()
        self._lock = asyncio.Lock()
        self._trades: Dict[int, Trade] = {}  # orderId -> Trade
        self._running = False
        self._account_summary: Dict[str, Any] = {}
        self._events_bound = False
        self._last_ib_error_code: Optional[int] = None
        self._last_ib_error_ts: float = 0.0
        self._last_ib_error_msg: str = ""
        self._last_ib_error_req_id: Optional[int] = None
        self._disconnect_count: int = 0
        self._last_disconnect_reason: Optional[str] = None
        self._last_disconnect_ts: float = 0.0
        self._last_connectivity_error_code: Optional[int] = None
        self._last_connectivity_error_msg: str = ""
        self._last_connectivity_error_ts: float = 0.0
        self._last_connectivity_error_req_id: Optional[int] = None
        self._last_probe_ok_ts: float = 0.0
        self._last_probe_latency_ms: Optional[float] = None
        self._probe_task: Optional[asyncio.Task] = None
        self._probe_interval_sec: float = float(os.getenv("IB_EXEC_PROBE_INTERVAL_SEC", "30"))
        self._probe_interval_recovery_sec: float = float(
            os.getenv("IB_EXEC_PROBE_INTERVAL_RECOVERY_SEC", "120")
        )
        self._probe_timeout_sec: float = float(os.getenv("IB_EXEC_PROBE_TIMEOUT_SEC", "5"))
        # Reconnect backoff state (used by background reconnector only).
        self._reconnect_failures: int = 0
        self._reconnect_sleep_sec: float = 2.0
        self._premarket_backoff_max_sec: float = float(os.getenv("IB_PREMARKET_RECONNECT_MAX_SEC", "30"))
        self._premarket_backoff_base_sec: float = float(os.getenv("IB_PREMARKET_RECONNECT_BASE_SEC", "5"))
        # Off-RTH hard-failure backoff. Gateway restarts daily at ~19:45 and ~09:33 ET;
        # without exponential backoff the old code hammered TCP connect every 7s for
        # 20+ minutes, which IBKR's pacing protection treats as abuse.
        self._offhours_backoff_base_sec: float = float(os.getenv("IB_OFFHOURS_RECONNECT_BASE_SEC", "5"))
        self._offhours_backoff_max_sec: float = float(os.getenv("IB_OFFHOURS_RECONNECT_MAX_SEC", "120"))
        # Dedup recent order_refs to avoid duplicate submissions within a short window.
        self._recent_order_refs: Dict[str, float] = {}
        self._dedup_window_sec: float = float(os.getenv("IB_ORDER_DEDUP_SEC", "5"))
        # Throttle expensive "reqAllOpenOrders" refreshes to keep reconciliation safe.
        self._open_orders_refresh_sec: float = float(os.getenv("IB_OPEN_ORDERS_REFRESH_SEC", "5"))
        self._open_orders_refresh_recovery_sec: float = float(
            os.getenv("IB_OPEN_ORDERS_REFRESH_RECOVERY_SEC", "30")
        )
        self._open_orders_req_timeout_sec: float = float(os.getenv("IB_OPEN_ORDERS_REQ_TIMEOUT_SEC", "1.2"))
        self._last_open_orders_req_ts: float = 0.0
        # Auxiliary probe sessions are disabled by default. They can be enabled
        # only for diagnostics and are never used as normal runtime behavior.
        self._aux_probe_sessions_enabled: bool = (
            os.getenv("IB_ENABLE_AUX_PROBE_SESSIONS", "false").strip().lower() in {"1", "true", "yes", "on"}
        )
        self._fresh_probe_client_id_offset: int = int(os.getenv("IB_FRESH_PROBE_CLIENT_ID_OFFSET", "10000"))
        self._fresh_probe_min_interval_sec: float = float(os.getenv("IB_FRESH_PROBE_MIN_INTERVAL_SEC", "10"))
        self._fresh_probe_min_interval_rth_sec: float = float(os.getenv("IB_FRESH_PROBE_MIN_INTERVAL_RTH_SEC", "1"))
        self._fresh_probe_last_ts: Dict[int, float] = {}
        self._fresh_probe_last_result: Dict[int, Dict[str, Any]] = {}
        self._fresh_probe_inflight: Dict[int, asyncio.Task] = {}
        self._owner_probe_min_interval_sec: float = float(os.getenv("IB_OWNER_PROBE_MIN_INTERVAL_SEC", "10"))
        self._owner_probe_min_interval_rth_sec: float = float(os.getenv("IB_OWNER_PROBE_MIN_INTERVAL_RTH_SEC", "2"))
        self._owner_probe_last_ts: Dict[Tuple[int, int], float] = {}
        self._owner_probe_last_result: Dict[Tuple[int, int], bool] = {}
        self._owner_probe_inflight: Dict[Tuple[int, int], asyncio.Task] = {}
        self._contract_min_tick_cache: Dict[str, float] = {}
        self._reconnect_gatekeeper: Any = None
        self._connect_gate_depth: int = 0
        self._connect_timeout_streak: int = 0
        self._reconnector_task: Optional[asyncio.Task] = None
        self._managed_by_connection_manager: bool = False
        self._on_connected_callback: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._on_disconnected_callback: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._connect_generation: int = 0

        logger.info("IBInsyncAdapter initialized",
                    host=self.host, port=self.port, client_id=self.client_id)

    @staticmethod
    def _current_epoch() -> Optional[int]:
        try:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            return int(get_ib_connection_manager().exec_connection_epoch())
        except Exception:
            return None

    def _require_epoch(self, expected_epoch: Optional[int], *, operation: str) -> None:
        if expected_epoch is None:
            return
        current_epoch = self._current_epoch()
        if current_epoch is None:
            return
        if int(current_epoch) != int(expected_epoch):
            ib_adapter_epoch_validation_total.labels(result="rejected", operation=operation).inc()
            raise IBHandleEpochMismatch(
                expected=int(expected_epoch),
                actual=int(current_epoch),
                operation=operation,
            )
        ib_adapter_epoch_validation_total.labels(result="ok", operation=operation).inc()

    def _ensure_ib(self) -> None:
        if self.ib is None:
            self.ib = IB()
            self._events_bound = False

    @staticmethod
    def rebind_open_order_owner(broker_order: Any, durable_orders: Any, *, current_epoch: Optional[int] = None) -> RebindResult:
        """Classify a broker open order using durable owner rows.

        UNKNOWN_OWNER and AMBIGUOUS_OWNER are non-mutating outcomes: callers must
        quarantine or require manual review rather than assign by symbol/order_ref
        heuristics.
        """
        result = rebind_broker_order_to_owner(
            broker_order,
            durable_orders,
            current_epoch=current_epoch,
        )
        if result.status in {RebindStatus.UNKNOWN_OWNER, RebindStatus.AMBIGUOUS_OWNER}:
            return result
        return result

    def configure_managed_mode(
        self,
        *,
        enabled: bool,
        on_connected_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_disconnected_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self._managed_by_connection_manager = bool(enabled)
        self._on_connected_callback = on_connected_callback
        self._on_disconnected_callback = on_disconnected_callback

    async def _notify_connected(self, *, source: str) -> None:
        cb = self._on_connected_callback
        if cb is None:
            return
        payload = {
            "client_id": int(self.client_id),
            "generation": int(self._connect_generation),
            "source": str(source or "unknown"),
            "ts": time.time(),
        }
        try:
            result = cb(payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning("ib_exec_on_connected_callback_failed", error=str(exc), source=source)

    def _notify_disconnected(self, *, reason: str, source: str) -> None:
        cb = self._on_disconnected_callback
        if cb is None:
            return
        payload = {
            "client_id": int(self.client_id),
            "generation": int(self._connect_generation),
            "reason": str(reason or "unknown"),
            "source": str(source or "unknown"),
            "ts": time.time(),
        }
        try:
            result = cb(payload)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as exc:
            logger.warning(
                "ib_exec_on_disconnected_callback_failed",
                error=str(exc),
                source=source,
                reason=reason,
            )

    def _bind_events(self) -> None:
        """Bind IB event callbacks exactly once per IB instance."""
        if not self.ib or self._events_bound:
            return
        # Track last IB error code for better connect retry decisions
        self.ib.errorEvent += self._on_error_event
        self.ib.orderStatusEvent += self._on_order_status_event
        self.ib.execDetailsEvent += self._on_exec_details_event
        if hasattr(self.ib, "disconnectedEvent"):
            self.ib.disconnectedEvent += self._on_disconnected_event
        self._events_bound = True

    def _unbind_events(self) -> None:
        """Best-effort unbind IB event callbacks (idempotent)."""
        if not self.ib or not self._events_bound:
            return
        with contextlib.suppress(Exception):
            self.ib.errorEvent -= self._on_error_event
        with contextlib.suppress(Exception):
            self.ib.orderStatusEvent -= self._on_order_status_event
        with contextlib.suppress(Exception):
            self.ib.execDetailsEvent -= self._on_exec_details_event
        with contextlib.suppress(Exception):
            if hasattr(self.ib, "disconnectedEvent"):
                self.ib.disconnectedEvent -= self._on_disconnected_event
        self._events_bound = False

    def _on_order_status_event(self, trade: Trade) -> None:
        """IB callback: order status updates (fills, cancels, etc.)."""
        try:
            order_id = getattr(getattr(trade, "order", None), "orderId", None)
            if order_id is not None:
                self._trades[int(order_id)] = trade

            contract = getattr(trade, "contract", None)
            order = getattr(trade, "order", None)
            status = getattr(getattr(trade, "orderStatus", None), "status", None)
            filled = getattr(getattr(trade, "orderStatus", None), "filled", None)
            remaining = getattr(getattr(trade, "orderStatus", None), "remaining", None)
            avg_fill_price = getattr(getattr(trade, "orderStatus", None), "avgFillPrice", None)
            why_held = getattr(getattr(trade, "orderStatus", None), "whyHeld", None)

            logger.info(
                "IB order status",
                order_id=order_id,
                broker_order_id=str(order_id) if order_id is not None else None,
                perm_id=getattr(order, "permId", None),
                client_id=getattr(order, "clientId", None),
                order_ref=getattr(order, "orderRef", None),
                symbol=getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None),
                side=getattr(order, "action", None),
                order_type=getattr(order, "orderType", None),
                status=status,
                filled=filled,
                remaining=remaining,
                avg_fill_price=avg_fill_price,
                why_held=why_held,
            )
        except Exception as exc:
            logger.warning("ib_order_status_event_error", error=str(exc))

    def _on_exec_details_event(self, trade: Trade, fill: Any) -> None:
        """IB callback: execution details events."""
        try:
            order_id = getattr(getattr(trade, "order", None), "orderId", None)
            symbol = getattr(getattr(trade, "contract", None), "symbol", None)

            execution = getattr(fill, "execution", None)
            shares = getattr(execution, "shares", None)
            price = getattr(execution, "price", None)
            side = getattr(execution, "side", None) or getattr(getattr(trade, "order", None), "action", None)
            perm_id = getattr(execution, "permId", None)
            exec_id = getattr(execution, "execId", None)

            logger.info(
                "IB exec details",
                order_id=order_id,
                broker_order_id=str(order_id) if order_id is not None else None,
                symbol=getattr(getattr(trade, "contract", None), "localSymbol", None) or symbol,
                side=side,
                shares=shares,
                price=price,
                perm_id=perm_id,
                exec_id=exec_id,
                client_id=getattr(getattr(trade, "order", None), "clientId", None),
                order_ref=getattr(getattr(trade, "order", None), "orderRef", None),
            )
        except Exception as exc:
            logger.warning("ib_exec_details_event_error", error=str(exc))

    async def _await_trade_identity(
        self,
        trade: Trade,
        *,
        requested_order_ref: str,
        timeout_sec: float = 1.0,
        poll_sec: float = 0.05,
    ) -> tuple[str, Optional[int], str]:
        """
        Wait briefly for IB to populate stable order identity after placeOrder().

        IB often returns orderId immediately while `permId` is still 0/None on the
        first synchronous snapshot. Treat 0 as "not ready" and give the event loop a
        short chance to receive the follow-up orderStatus payload before returning.
        """
        order_ref_fallback = str(requested_order_ref or "").strip()
        deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
        while True:
            order_obj = getattr(trade, "order", None)
            order_ref = str(getattr(order_obj, "orderRef", "") or "").strip() or order_ref_fallback
            order_status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            perm_raw = getattr(order_obj, "permId", None)
            try:
                perm_id = int(perm_raw) if perm_raw is not None else None
            except Exception:
                perm_id = None
            if perm_id is not None and perm_id > 0:
                return order_ref, perm_id, order_status or "Submitted"
            if order_status in _TERMINAL_ORDER_STATUSES:
                return order_ref, None, order_status
            if time.time() >= deadline:
                return order_ref, None, order_status
            await asyncio.sleep(max(0.01, float(poll_sec or 0.05)))

    async def _await_trade_modifiable(
        self,
        trade: Trade,
        *,
        timeout_sec: float = 2.0,
        poll_sec: float = 0.05,
    ) -> bool:
        """
        Wait until an IB trade leaves the initial submission phase.

        IB rejects same-orderId modifications while an order is still in
        PendingSubmit/ApiPending. Returning False lets callers retry later
        instead of cancelling protection via duplicate-order-id errors.
        """
        deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
        while True:
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if status in _TERMINAL_ORDER_STATUSES:
                return False
            if status not in {"PendingSubmit", "ApiPending"}:
                return True
            if time.time() >= deadline:
                return False
            await asyncio.sleep(max(0.01, float(poll_sec or 0.05)))

    @staticmethod
    def _trade_identity_snapshot(
        trade: Trade,
        *,
        requested_order_ref: str = "",
    ) -> tuple[str, Optional[int], str]:
        order_obj = getattr(trade, "order", None)
        order_ref = str(getattr(order_obj, "orderRef", "") or "").strip() or str(requested_order_ref or "").strip()
        order_status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "").strip()
        perm_raw = getattr(order_obj, "permId", None)
        try:
            perm_id = int(perm_raw) if perm_raw is not None else None
        except Exception:
            perm_id = None
        if perm_id is not None and perm_id <= 0:
            perm_id = None
        return order_ref, perm_id, order_status

    @staticmethod
    def _is_protective_order_ref(order_ref: str) -> bool:
        ref = str(order_ref or "").strip().lower()
        return ref.startswith("sl:") or ref.startswith("tp:")

    @classmethod
    def _is_locally_confirmable_protective_status(
        cls,
        *,
        order_ref: str,
        status: str,
        oca_group: Optional[str] = None,
    ) -> bool:
        status_norm = str(status or "").strip()
        if status_norm not in _LOCAL_PROTECTIVE_PENDING_STATUSES:
            return False
        if cls._is_protective_order_ref(order_ref):
            return True
        return bool(str(oca_group or "").strip())

    @classmethod
    def _is_locally_confirmed_open_trade(
        cls,
        trade: Trade,
        *,
        requested_order_ref: str = "",
    ) -> bool:
        local_ref, local_perm_id, local_status = cls._trade_identity_snapshot(
            trade,
            requested_order_ref=requested_order_ref,
        )
        local_status_norm = str(local_status or "").strip()
        if local_perm_id is not None or local_status_norm in _LOCALLY_ACCEPTED_OPEN_STATUSES:
            return True
        local_order = getattr(trade, "order", None)
        local_oca_group = getattr(local_order, "ocaGroup", None)
        return cls._is_locally_confirmable_protective_status(
            order_ref=local_ref,
            status=local_status_norm,
            oca_group=local_oca_group,
        )

    async def _refresh_open_orders_snapshot(self) -> None:
        try:
            timeout_sec = max(0.0, float(self._open_orders_req_timeout_sec or 0.0))
            if hasattr(self.ib, "reqAllOpenOrdersAsync"):
                coro = self.ib.reqAllOpenOrdersAsync()
            elif hasattr(self.ib, "reqOpenOrdersAsync"):
                coro = self.ib.reqOpenOrdersAsync()
            else:
                return
            if timeout_sec > 0.0:
                await asyncio.wait_for(coro, timeout=timeout_sec)
            else:
                await coro
        except asyncio.TimeoutError:
            logger.warning("ib_req_open_orders_timeout", timeout_sec=float(self._open_orders_req_timeout_sec))
        except Exception as exc:
            logger.warning("ib_req_open_orders_failed", error=str(exc))

    def _find_open_trade_by_id(self, order_id: int) -> Optional[Trade]:
        if not hasattr(self.ib, "openTrades"):
            return None
        for trade in self.ib.openTrades():
            try:
                ib_order = getattr(trade, "order", None)
                if ib_order is None or int(getattr(ib_order, "orderId", 0) or 0) != int(order_id):
                    continue
                if self.account and getattr(ib_order, "account", "") and str(getattr(ib_order, "account", "")) != str(self.account):
                    continue
                return trade
            except Exception:
                continue
        return None

    async def _confirm_open_submission(
        self,
        trade: Trade,
        *,
        requested_order_ref: str,
    ) -> tuple[str, Optional[int], str]:
        order_ref, perm_id, status = self._trade_identity_snapshot(
            trade,
            requested_order_ref=requested_order_ref,
        )
        status_norm = str(status or "").strip()
        if perm_id is not None or status_norm in _TERMINAL_ORDER_STATUSES:
            return order_ref, perm_id, status_norm

        order_id = int(getattr(getattr(trade, "order", None), "orderId", 0) or 0)
        if order_id <= 0:
            return order_ref, None, status_norm

        # IB child/OCO orders often sit in PendingSubmit briefly before a non-zero
        # permId is assigned or before a fresh probe session can observe them.
        # Give the owning session a short bounded window to converge first.
        if status_norm in _TRANSIENT_SUBMISSION_STATUSES:
            await self._refresh_open_orders_snapshot()
            await asyncio.sleep(0.2)
            order_ref, perm_id, status = await self._await_trade_identity(
                trade,
                requested_order_ref=requested_order_ref,
                timeout_sec=0.8,
                poll_sec=0.05,
            )
            status_norm = str(status or "").strip()
            if perm_id is not None or status_norm in _TERMINAL_ORDER_STATUSES:
                return order_ref, perm_id, status_norm

            local_trade = self._find_open_trade_by_id(order_id)
            if local_trade is not None:
                local_ref, local_perm_id, local_status = self._trade_identity_snapshot(
                    local_trade,
                    requested_order_ref=requested_order_ref,
                )
                local_status_norm = str(local_status or "").strip()
                if self._is_locally_confirmed_open_trade(
                    local_trade,
                    requested_order_ref=requested_order_ref,
                ):
                    return local_ref, local_perm_id, local_status_norm

        probe_result: Dict[str, Any] | None = None
        attempts = 3 if status_norm in _TRANSIENT_SUBMISSION_STATUSES else 1
        for attempt in range(attempts):
            probe_result = await self.probe_order_open_fresh(order_id)
            if bool(probe_result.get("found")):
                probe_status = str(probe_result.get("status") or status_norm or "Submitted").strip()
                probe_perm_raw = probe_result.get("perm_id")
                try:
                    probe_perm_id = int(probe_perm_raw) if probe_perm_raw is not None else None
                except Exception:
                    probe_perm_id = None
                if probe_perm_id is not None and probe_perm_id <= 0:
                    probe_perm_id = None
                return order_ref, probe_perm_id, probe_status

            if attempt + 1 < attempts:
                await asyncio.sleep(0.2 * (attempt + 1))
                await self._refresh_open_orders_snapshot()
                order_ref, perm_id, status = self._trade_identity_snapshot(
                    trade,
                    requested_order_ref=requested_order_ref,
                )
                status_norm = str(status or "").strip()
                if perm_id is not None or status_norm in _TERMINAL_ORDER_STATUSES:
                    return order_ref, perm_id, status_norm
                local_trade = self._find_open_trade_by_id(order_id)
                if local_trade is not None:
                    local_ref, local_perm_id, local_status = self._trade_identity_snapshot(
                        local_trade,
                        requested_order_ref=requested_order_ref,
                    )
                    local_status_norm = str(local_status or "").strip()
                    if self._is_locally_confirmed_open_trade(
                        local_trade,
                        requested_order_ref=requested_order_ref,
                    ):
                        return local_ref, local_perm_id, local_status_norm

        probe_error = None if probe_result is None else (probe_result.get("error") or "not_found")
        raise RuntimeError(
            "ib_place_unconfirmed_open_status:"
            f"{order_id}:{status_norm or 'unknown'}:{probe_error}"
        )

    @staticmethod
    def _normalize_quantity(contract: Contract, qty: float) -> float:
        q = float(qty)
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        if sec_type not in {"STK", "OPT", "FUT"}:
            return q
        rounded = round(q)
        if abs(q - float(rounded)) <= 1e-6:
            q = float(rounded)
        if abs(q - round(q)) > 1e-9:
            raise RuntimeError(f"non_integral_qty_not_allowed:{sec_type}:{qty}")
        if q <= 0:
            raise RuntimeError(f"non_positive_qty_not_allowed:{sec_type}:{qty}")
        return float(int(round(q)))

    def _on_error_event(self, req_id: int, error_code: int, error_string: str, contract: Any) -> None:
        """IB callback: captures recent error codes for smarter reconnects."""
        try:
            self._last_ib_error_code = int(error_code)
        except Exception:
            self._last_ib_error_code = None
        try:
            self._last_ib_error_req_id = int(req_id)
        except Exception:
            self._last_ib_error_req_id = None
        self._last_ib_error_ts = time.time()
        self._last_ib_error_msg = str(error_string or "")
        symbol = None
        try:
            symbol = (
                getattr(contract, "localSymbol", None)
                or getattr(contract, "symbol", None)
            )
        except Exception:
            symbol = None
        if self._last_ib_error_code in _IB_CONNECTIVITY_ERROR_CODES:
            logger.warning(
                "ib_exec_error_event",
                req_id=self._last_ib_error_req_id,
                error_code=self._last_ib_error_code,
                error_msg=(self._last_ib_error_msg or "")[:300],
            )
        elif self._last_ib_error_code in _IB_ORDER_WARNING_CODES:
            logger.warning(
                "ib_exec_order_warning_event",
                req_id=self._last_ib_error_req_id,
                error_code=self._last_ib_error_code,
                error_msg=(self._last_ib_error_msg or "")[:300],
                symbol=symbol,
            )
        elif self._last_ib_error_code not in _IB_INFO_ERROR_CODES:
            logger.error(
                "ib_exec_order_error_event",
                req_id=self._last_ib_error_req_id,
                error_code=self._last_ib_error_code,
                error_msg=(self._last_ib_error_msg or "")[:300],
                symbol=symbol,
            )
        self._remember_connectivity_error(
            req_id=self._last_ib_error_req_id,
            error_code=self._last_ib_error_code,
            error_msg=self._last_ib_error_msg,
        )

    def _remember_connectivity_error(
        self,
        *,
        req_id: Optional[int],
        error_code: Optional[int],
        error_msg: str,
    ) -> None:
        try:
            code = int(error_code) if error_code is not None else None
        except Exception:
            code = None
        if code not in {1100, 1300, 326}:
            return
        self._last_connectivity_error_code = code
        self._last_connectivity_error_msg = str(error_msg or "")
        self._last_connectivity_error_ts = time.time()
        self._last_connectivity_error_req_id = req_id

    @staticmethod
    def _classify_disconnect_reason(
        *,
        error_code: Optional[int],
        error_msg: str,
        error_age_sec: Optional[float],
    ) -> tuple[str, str]:
        code = int(error_code) if error_code is not None else None
        msg = str(error_msg or "").lower()
        age = float(error_age_sec) if error_age_sec is not None else None

        if code == 1100:
            return "ib_connectivity_lost_1100", "ib_gateway_or_network"
        if code == 1300:
            return "ib_socket_port_reset_1300", "ib_gateway_reset"
        if code == 326:
            return "ib_client_id_conflict_326", "session_conflict"
        if "connection reset by peer" in msg or "peer closed connection" in msg:
            return "tcp_peer_closed", "network"
        if "timed out" in msg or "timeout" in msg:
            return "tcp_timeout", "network"
        if age is not None and age > 60:
            return "stale_or_unknown_error_context", "unknown"
        return "unknown_disconnect", "unknown"

    def _resolve_disconnect_context(self) -> tuple[Optional[int], str, Optional[float], Optional[int], str]:
        now = time.time()
        if self._last_connectivity_error_ts:
            age = now - self._last_connectivity_error_ts
            if age <= 300:
                return (
                    self._last_connectivity_error_code,
                    self._last_connectivity_error_msg,
                    age,
                    self._last_connectivity_error_req_id,
                    "recent_connectivity_error",
                )
        error_age_sec = (now - self._last_ib_error_ts) if self._last_ib_error_ts else None
        return (
            self._last_ib_error_code,
            self._last_ib_error_msg,
            error_age_sec,
            self._last_ib_error_req_id,
            "last_error_event",
        )

    def _on_disconnected_event(self) -> None:
        error_code, error_msg, error_age_sec, error_req_id, disconnect_source = self._resolve_disconnect_context()
        reason_code, cause_type = self._classify_disconnect_reason(
            error_code=error_code,
            error_msg=error_msg,
            error_age_sec=error_age_sec,
        )
        self._disconnect_count += 1
        self._last_disconnect_reason = reason_code
        self._last_disconnect_ts = time.time()
        set_ibkr_connection_up("exec", False)
        inc_ibkr_disconnect("exec", reason_code, cause_type)
        logger.error(
            "ib_exec_disconnected",
            disconnect_reason=reason_code,
            disconnect_cause=cause_type,
            disconnect_source=disconnect_source,
            disconnect_count=int(self._disconnect_count),
            last_error_req_id=error_req_id,
            last_error_code=error_code,
            last_error_msg=(error_msg or "")[:300],
            last_error_age_sec=error_age_sec,
        )
        self._notify_disconnected(reason=reason_code, source=disconnect_source)

    async def _probe_connection(self, *, source: str) -> None:
        if not self.ib or not self.ib.isConnected():
            set_ibkr_connection_up("exec", False)
            raise ConnectionError("exec_ib_not_connected")
        start = time.perf_counter_ns()
        await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), timeout=self._probe_timeout_sec)
        latency_ms = (time.perf_counter_ns() - start) / 1_000_000
        self._last_probe_ok_ts = time.time()
        self._last_probe_latency_ms = float(latency_ms)
        set_ibkr_connection_up("exec", True)
        mark_ibkr_probe_ok("exec", latency_ms)
        logger.info(
            "ib_exec_probe_ok",
            source=source,
            latency_ms=round(latency_ms, 2),
            connected=True,
        )

    async def _probe_loop(self) -> None:
        try:
            while self._running:
                interval_sec = float(self._probe_interval_sec)
                if self._manager_runtime_recovery_active():
                    interval_sec = max(interval_sec, float(self._probe_interval_recovery_sec))
                await asyncio.sleep(interval_sec)
                if not self._running:
                    break
                try:
                    await self._probe_connection(source="background")
                except Exception as exc:
                    error_code, error_msg, error_age_sec, _req_id, _source = self._resolve_disconnect_context()
                    reason_code, _cause = self._classify_disconnect_reason(
                        error_code=error_code,
                        error_msg=error_msg or str(exc),
                        error_age_sec=error_age_sec,
                    )
                    set_ibkr_connection_up("exec", False)
                    inc_ibkr_probe_failure("exec", reason_code)
                    logger.warning(
                        "ib_exec_probe_failed",
                        error=str(exc),
                        reason=reason_code,
                        last_error_code=error_code,
                        last_probe_ok_age_sec=(
                            time.time() - self._last_probe_ok_ts
                            if self._last_probe_ok_ts
                            else None
                        ),
                    )
        except asyncio.CancelledError:
            return

    def _ensure_probe_task(self) -> None:
        if self._probe_task and not self._probe_task.done():
            return
        self._probe_task = asyncio.create_task(self._probe_loop(), name="ib_exec_probe")

    @staticmethod
    def _manager_runtime_recovery_active() -> bool:
        try:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            mgr = get_ib_connection_manager()
            active_fn = getattr(mgr, "runtime_recovery_active", None)
            if callable(active_fn):
                return bool(active_fn())
            return bool(getattr(mgr, "_runtime_recovery_active", False))
        except Exception:
            return False

    @staticmethod
    def _session_phase_et(now: Optional[datetime] = None) -> str:
        t = (now or datetime.now(ET)).astimezone(ET).time()
        if dt_time(7, 0) <= t < dt_time(9, 30):
            return "PREMARKET"
        if dt_time(9, 30) <= t < dt_time(16, 0):
            return "RTH"
        if dt_time(16, 0) <= t < dt_time(20, 0):
            return "AFTERHOURS"
        return "OFFHOURS"

    @staticmethod
    def _is_hard_reconnect_error(exc: Exception, recent_code: Optional[int], recent_msg: str) -> bool:
        msg = f"{exc}".lower()
        err = (recent_msg or "").lower()
        hard_tokens = (
            "connectionrefusederror",
            "connect call failed",
            "peer closed connection",
            "connection reset by peer",
            "timed out",
            "timeout",
        )
        if any(tok in msg for tok in hard_tokens):
            return True
        if any(tok in err for tok in hard_tokens):
            return True
        return recent_code in {1100, 1300}

    def _next_reconnect_sleep(self, *, hard_failure: bool) -> float:
        phase = self._session_phase_et()
        if phase == "PREMARKET" and hard_failure:
            self._reconnect_failures += 1
            delay = min(
                self._premarket_backoff_max_sec,
                self._premarket_backoff_base_sec * (2 ** max(0, self._reconnect_failures - 1)),
            )
            self._reconnect_sleep_sec = float(delay)
            return self._reconnect_sleep_sec

        # AFTERHOURS / OFFHOURS hard failures: Gateway is almost certainly in its
        # daily restart window. Keep exponential backoff so we don't hammer TCP.
        # RTH and soft failures keep the original 2s fast-recovery behavior.
        if hard_failure and phase in ("AFTERHOURS", "OFFHOURS"):
            self._reconnect_failures += 1
            delay = min(
                self._offhours_backoff_max_sec,
                self._offhours_backoff_base_sec * (2 ** max(0, self._reconnect_failures - 1)),
            )
            self._reconnect_sleep_sec = float(delay)
            return self._reconnect_sleep_sec

        self._reconnect_failures += 1
        self._reconnect_sleep_sec = 2.0
        return self._reconnect_sleep_sec

    def _current_fresh_probe_min_interval_sec(self) -> float:
        phase = self._session_phase_et()
        if phase == "RTH":
            return max(0.0, float(self._fresh_probe_min_interval_rth_sec))
        return max(0.0, float(self._fresh_probe_min_interval_sec))

    def _current_owner_probe_min_interval_sec(self) -> float:
        phase = self._session_phase_et()
        if phase == "RTH":
            return max(0.0, float(self._owner_probe_min_interval_rth_sec))
        return max(0.0, float(self._owner_probe_min_interval_sec))

    def _reset_reconnect_backoff(self) -> None:
        self._reconnect_failures = 0
        self._reconnect_sleep_sec = 2.0

    def _advance_client_id_after_failed_connect(self, *, exc: Exception) -> None:
        msg = str(exc or "").lower()
        is_timeout = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in msg
        is_in_use = (self._last_ib_error_code == 326 and (time.time() - self._last_ib_error_ts) < 5.0) or (
            "client id is already in use" in msg
        )
        if is_timeout:
            self._connect_timeout_streak += 1
            logger.warning(
                "ib_exec_connect_timeout_keep_client_id",
                host=self.host,
                port=self.port,
                client_id=self.client_id,
                timeout_streak=self._connect_timeout_streak,
                error=str(exc),
            )
        elif is_in_use:
            self._connect_timeout_streak = 0
            logger.warning(
                "ib_exec_connect_conflict_keep_client_id",
                host=self.host,
                port=self.port,
                client_id=self.client_id,
                error=str(exc),
            )
        else:
            self._connect_timeout_streak = 0

    async def start(self):
        """Start the adapter and begin reconnection loop"""
        if self._running:
            if self._managed_by_connection_manager and not bool(self.ib and self.ib.isConnected()):
                await self._connect(initiated_by_manager=True)
            logger.info(
                "IBInsyncAdapter already started",
                connected=bool(self.ib and self.ib.isConnected()),
            )
            return

        self._running = True
        self._ensure_ib()
        self._bind_events()
        try:
            await self._connect(initiated_by_manager=self._managed_by_connection_manager)
        except Exception as exc:
            # Do not fail system startup just because the execution session
            # needs extra time to complete the API handshake. Order placement
            # paths still call _connect() and the background reconnector will
            # continue trying until the session is healthy.
            logger.warning(
                "IBInsyncAdapter started in degraded mode",
                host=self.host,
                port=self.port,
                client_id=self.client_id,
                error=str(exc),
            )
        if not self._managed_by_connection_manager:
            self._reconnector_task = asyncio.create_task(self._reconnector())
        self._ensure_probe_task()
        logger.info(
            "IBInsyncAdapter started",
            connected=bool(self.ib and self.ib.isConnected()),
        )

    async def stop(self, *, dispose: bool = False):
        """Stop the adapter"""
        self._running = False
        if self._probe_task:
            self._probe_task.cancel()
            self._probe_task = None
        if self._reconnector_task:
            self._reconnector_task.cancel()
            self._reconnector_task = None
        with contextlib.suppress(Exception):
            self._unbind_events()
        with contextlib.suppress(Exception):
            if self.ib and self.ib.isConnected():
                self.ib.disconnect()
                # Give ib_insync/asyncio transports one loop turn to flush cleanup.
                await asyncio.sleep(0.1)
        set_ibkr_connection_up("exec", False)
        if dispose:
            self.ib = None
        logger.info("IBInsyncAdapter stopped")

    async def _connect(self, *, initiated_by_manager: bool = False):
        """Connect to IB Gateway/TWS"""
        self._ensure_ib()
        if self.ib.isConnected():
            return
        if self._managed_by_connection_manager and (not initiated_by_manager):
            raise ConnectionError("ib_exec_connect_managed_by_connection_manager")

        gk = self._reconnect_gatekeeper
        gate_acquired = False
        gate_depth_acquired = False
        if gk is not None and self._connect_gate_depth <= 0:
            allowed = await gk.acquire(
                caller="exec_adapter_connect",
                host=self.host,
                port=self.port,
            )
            if not allowed:
                raise ConnectionError("reconnect_gatekeeper_denied:exec_adapter_connect")
            gate_acquired = True
            self._connect_gate_depth += 1
            gate_depth_acquired = True

        # A client_id collision (IB error 326) can happen if:
        # - another process is already connected using the same clientId
        # - a previous instance crashed and IB hasn't released the slot yet
        # Keep gatekeeper lease short: one handshake per lease by default.
        max_attempts = int(os.getenv("IB_EXEC_CONNECT_MAX_ATTEMPTS", "1")) if gk is not None else 5
        attempt = 1
        try:
            while True:
                try:
                    await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=8.0)
                    break
                except asyncio.CancelledError:
                    if self.ib.isConnected():
                        try:
                            self.ib.disconnect()
                            logger.warning(
                                "ib_exec_connect_cancel_disconnect",
                                host=self.host,
                                port=self.port,
                                client_id=self.client_id,
                                ib_id=id(self.ib),
                            )
                        except Exception as disconnect_exc:
                            logger.warning(
                                "ib_exec_connect_cancel_disconnect_failed",
                                host=self.host,
                                port=self.port,
                                client_id=self.client_id,
                                ib_id=id(self.ib),
                                error=str(disconnect_exc),
                            )
                    logger.warning(
                        "ib_exec_connect_cancelled",
                        host=self.host,
                        port=self.port,
                        client_id=self.client_id,
                        ib_id=id(self.ib),
                        gate_acquired=gate_acquired,
                    )
                    raise
                except Exception as e:
                    recent_code = None
                    if (time.time() - self._last_ib_error_ts) < 5.0:
                        recent_code = self._last_ib_error_code
                    msg = f"{e}" or self._last_ib_error_msg or ""
                    is_client_id_in_use = (recent_code == 326) or ("client id is already in use" in msg.lower())
                    is_timeout = isinstance(e, (TimeoutError, asyncio.TimeoutError)) or ("timeout" in msg.lower())
                    if self.ib.isConnected():
                        try:
                            self.ib.disconnect()
                            logger.warning(
                                "ib_exec_partial_session_disconnected",
                                host=self.host,
                                port=self.port,
                                client_id=self.client_id,
                                reason="connect_failed",
                            )
                        except Exception as disconnect_exc:
                            logger.warning(
                                "ib_exec_partial_session_disconnect_failed",
                                host=self.host,
                                port=self.port,
                                client_id=self.client_id,
                                reason="connect_failed",
                                error=str(disconnect_exc),
                            )
                    # Timeout on connectAsync can leave ib_insync client in a bad state.
                    # Recreate client instance so next reconnect loop starts clean.
                    if is_timeout:
                        old_ib = self.ib
                        self._unbind_events()
                        self.ib = IB()
                        self._events_bound = False
                        self._bind_events()
                        logger.warning(
                            "ib_exec_connect_timeout_recreate_client",
                            old_ib_id=id(old_ib),
                            new_ib_id=id(self.ib),
                            client_id=self.client_id,
                            host=self.host,
                            port=self.port,
                        )
                    if is_client_id_in_use and attempt < max_attempts:
                        logger.warning(
                            "ib_client_id_in_use_retry_same_id",
                            client_id=self.client_id,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            last_error_code=recent_code,
                            last_error_msg=(self._last_ib_error_msg or "")[:200],
                        )
                        attempt += 1
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    self._advance_client_id_after_failed_connect(exc=e)
                    logger.error("Failed to connect to IB", error=str(e))
                    raise

            # Request account updates if account specified
            if not self.account:
                try:
                    managed_accounts = list(self.ib.managedAccounts() or [])
                except Exception:
                    managed_accounts = []
                if len(managed_accounts) == 1 and str(managed_accounts[0] or "").strip():
                    self.account = str(managed_accounts[0]).strip()
                    logger.info(
                        "ib_exec_account_resolved_from_managed_accounts",
                        account=self.account,
                    )
            if self.account:
                try:
                    timeout_sec = max(
                        0.5,
                        float(os.getenv("IB_EXEC_ACCOUNT_ENRICHMENT_TIMEOUT_SEC", "5")),
                    )
                    try:
                        await asyncio.wait_for(
                            self.ib.reqAccountUpdatesAsync(self.account),
                            timeout=timeout_sec,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "ib_exec_account_updates_timeout",
                            account=self.account,
                            timeout_sec=timeout_sec,
                        )
                    await self._refresh_account_summary(timeout_sec=timeout_sec)
                except Exception as e:
                    logger.warning("Failed to request account updates", error=str(e))

            logger.info(
                "Connected to IB",
                host=self.host,
                port=self.port,
                reconnect_failures=self._reconnect_failures,
                previous_disconnect_reason=self._last_disconnect_reason,
                previous_disconnect_age_sec=(
                    time.time() - self._last_disconnect_ts
                    if self._last_disconnect_ts
                    else None
                ),
            )
            set_ibkr_connection_up("exec", True)
            await self._probe_connection(source="connect")
            self._last_connectivity_error_code = None
            self._last_connectivity_error_msg = ""
            self._last_connectivity_error_ts = 0.0
            self._last_connectivity_error_req_id = None
            self._connect_timeout_streak = 0
            self._connect_generation += 1
            await self._notify_connected(source="connect")
            if gate_acquired and gk is not None and self.ib.isConnected():
                gk.report_success(caller="exec_adapter_connect")
                gate_acquired = False
        except Exception as exc:
            if gate_acquired and gk is not None:
                gk.report_failure(caller="exec_adapter_connect", error=str(exc), host=self.host, port=self.port)
                gate_acquired = False
            raise
        finally:
            if gate_acquired and gk is not None:
                gk.report_aborted(caller="exec_adapter_connect", reason="connect_exited_without_outcome")
            if gate_depth_acquired:
                self._connect_gate_depth = max(0, self._connect_gate_depth - 1)

    async def _reconnector(self):
        """Background task to maintain connection"""
        while self._running:
            sleep_sec = self._reconnect_sleep_sec
            try:
                self._ensure_ib()
                if not self.ib.isConnected():
                    logger.warning(
                        "IB disconnected, reconnecting...",
                        session_phase=self._session_phase_et(),
                    )
                    await self._connect(initiated_by_manager=False)
                    self._bind_events()
                    if self._reconnect_failures > 0:
                        logger.info(
                            "ib_exec_reconnect_reset",
                            session_phase=self._session_phase_et(),
                            failures=self._reconnect_failures,
                        )
                    self._reset_reconnect_backoff()
                    sleep_sec = self._reconnect_sleep_sec
            except Exception as e:
                if "reconnect_gatekeeper_denied:" in str(e):
                    # Gate denial is flow-control, not a transport/connect failure.
                    logger.warning(
                        "ib_exec_reconnect_gate_denied",
                        session_phase=self._session_phase_et(),
                        retry_in_sec=1.0,
                    )
                    await asyncio.sleep(1.0)
                    sleep_sec = self._reconnect_sleep_sec
                    continue
                recent_code = self._last_ib_error_code if (time.time() - self._last_ib_error_ts) < 30.0 else None
                hard_failure = self._is_hard_reconnect_error(e, recent_code, self._last_ib_error_msg)
                sleep_sec = self._next_reconnect_sleep(hard_failure=hard_failure)
                logger.error(
                    "Reconnection failed",
                    error=str(e),
                    session_phase=self._session_phase_et(),
                    hard_failure=hard_failure,
                    recent_error_code=recent_code,
                    retry_in_sec=sleep_sec,
                    consecutive_failures=self._reconnect_failures,
                )
            await asyncio.sleep(sleep_sec)

    async def _refresh_account_summary(self, *, timeout_sec: float | None = None) -> None:
        """Fetch account summary (available funds)."""
        try:
            coro = self.ib.accountSummaryAsync(self.account or "")
            if timeout_sec is not None:
                summary = await asyncio.wait_for(coro, timeout=max(0.5, float(timeout_sec)))
            else:
                summary = await coro
            self._account_summary = {item.tag: item.value for item in summary}
        except asyncio.TimeoutError:
            logger.warning(
                "ib_account_summary_timeout",
                account=self.account,
                timeout_sec=timeout_sec,
            )
        except Exception as e:
            logger.warning("ib_account_summary_failed", error=str(e))

    async def available_funds(self) -> Optional[float]:
        """Return AvailableFunds in USD if known."""
        if not self._account_summary:
            await self._refresh_account_summary()
        try:
            af = self._account_summary.get("AvailableFunds")
            return float(af) if af is not None else None
        except Exception:
            return None

    async def _contract(self, symbol: str, *, exchange_override: Optional[str] = None) -> Contract:
        """
        Build and qualify contract for symbol

        Args:
            symbol: Stock symbol

        Returns:
            Qualified contract
        """
        sym = str(symbol or "").strip()
        if sym.upper().startswith("OPT:"):
            from libs.options.internal_symbol import parse_internal_opt_symbol

            parts = parse_internal_opt_symbol(sym)
            c = Option(
                symbol=parts.underlying,
                lastTradeDateOrContractMonth=parts.expiry_ymd,
                strike=float(parts.strike),
                right=parts.right,
                exchange="SMART",
                currency=self.ccy,
                multiplier="100",
            )
        elif sym.upper() == "VIX":
            c = Index("VIX", "CBOE", self.ccy)
        else:
            exchange = str(exchange_override or self.exch or "SMART").strip() or "SMART"
            c = Stock(sym, exchange, self.ccy)
        if self.primary:
            c.primaryExchange = self.primary

        await self._connect()
        qualified = await self.ib.qualifyContractsAsync(c)
        return qualified[0] if qualified else c

    def _contract_cache_key(self, contract: Contract) -> str:
        con_id = getattr(contract, "conId", None)
        if con_id:
            return f"conid:{con_id}"
        parts = [
            str(getattr(contract, "secType", "") or "").strip().upper(),
            str(getattr(contract, "symbol", "") or "").strip().upper(),
            str(getattr(contract, "localSymbol", "") or "").strip().upper(),
            str(getattr(contract, "exchange", "") or "").strip().upper(),
            str(getattr(contract, "primaryExchange", "") or "").strip().upper(),
            str(getattr(contract, "currency", "") or "").strip().upper(),
        ]
        return "|".join(parts)

    async def _contract_min_tick(self, contract: Contract) -> float:
        key = self._contract_cache_key(contract)
        cached = self._contract_min_tick_cache.get(key)
        if cached and cached > 0:
            return float(cached)

        tick = 0.01
        try:
            details = await self.ib.reqContractDetailsAsync(contract)
            for detail in details or []:
                try:
                    min_tick = float(getattr(detail, "minTick", 0.0) or 0.0)
                except Exception:
                    min_tick = 0.0
                if min_tick > 0.0:
                    tick = min_tick
                    break
        except Exception as exc:
            logger.warning(
                "ib_contract_min_tick_lookup_failed",
                symbol=getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None),
                error=str(exc),
            )

        self._contract_min_tick_cache[key] = float(tick)
        return float(tick)

    def _round_to_tick(self, px: float, tick: float) -> float:
        tick_f = float(tick or 0.0)
        if tick_f <= 0.0:
            return float(px)
        px_dec = Decimal(str(float(px)))
        tick_dec = Decimal(str(tick_f))
        steps = (px_dec / tick_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return float(steps * tick_dec)

    async def _normalize_price_fields(
        self,
        contract: Contract,
        *,
        limit_px: Optional[float] = None,
        stop_px: Optional[float] = None,
    ) -> tuple[Optional[float], Optional[float]]:
        tick = await self._contract_min_tick(contract)
        norm_limit = None if limit_px is None else self._round_to_tick(limit_px, tick)
        norm_stop = None if stop_px is None else self._round_to_tick(stop_px, tick)
        if limit_px is not None and abs(float(limit_px) - float(norm_limit)) > 1e-9:
            logger.info(
                "ib_limit_price_normalized_to_tick",
                symbol=getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None),
                requested_px=float(limit_px),
                normalized_px=float(norm_limit),
                min_tick=float(tick),
            )
        if stop_px is not None and abs(float(stop_px) - float(norm_stop)) > 1e-9:
            logger.info(
                "ib_stop_price_normalized_to_tick",
                symbol=getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None),
                requested_px=float(stop_px),
                normalized_px=float(norm_stop),
                min_tick=float(tick),
            )
        return norm_limit, norm_stop

    async def _place(
        self,
        c: Contract,
        o: Order,
        order_ref: str,
        oca_group: Optional[str],
        *,
        outside_rth: Optional[bool] = None,
        symbol_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """
        Internal method to place order

        Args:
            c: Contract
            o: Order
            order_ref: Order reference for tracking
            oca_group: OCA group for OCO orders

        Returns:
            PlacedOrder object
        """
        await self._connect()

        o.account = self.account or o.account
        if outside_rth is None:
            o.outsideRth = bool(self.outside_rth)
        else:
            o.outsideRth = bool(outside_rth)
        requested_order_ref = str(order_ref or "").strip()
        o.orderRef = requested_order_ref[:45]  # IB has length limit on orderRef

        if oca_group:
            o.ocaGroup = oca_group[:30]
            o.ocaType = 1  # Cancel remaining orders (typical OCO)

        # Deduplicate rapid re-submissions with the same orderRef to prevent accidental floods.
        now = time.time()
        last_ts = self._recent_order_refs.get(o.orderRef)
        if last_ts and (now - last_ts) < self._dedup_window_sec:
            logger.warning(
                "skip_duplicate_order_ref",
                order_ref=o.orderRef,
                window_sec=self._dedup_window_sec,
                since=now - last_ts,
            )
            raise RuntimeError(f"Duplicate orderRef within {self._dedup_window_sec}s: {o.orderRef}")
        self._recent_order_refs[o.orderRef] = now
        self._require_epoch(expected_epoch, operation="place_order")

        trade = self.ib.placeOrder(c, o)

        async with self._lock:
            self._trades[trade.order.orderId] = trade

        stable_order_ref, stable_perm_id, stable_status = await self._await_trade_identity(
            trade,
            requested_order_ref=requested_order_ref,
        )
        status_norm = str(stable_status or "").strip()
        if stable_perm_id is None and status_norm not in _TERMINAL_ORDER_STATUSES:
            stable_order_ref, stable_perm_id, stable_status = await self._confirm_open_submission(
                trade,
                requested_order_ref=requested_order_ref,
            )
            status_norm = str(stable_status or "").strip()
        if status_norm in _TERMINAL_ORDER_STATUSES:
            raise RuntimeError(
                f"ib_place_terminal_status:{status_norm}:{getattr(trade.order, 'orderId', None)}"
            )

        order_type_norm = o.orderType.replace(" ", "_")
        aux_price = float(getattr(o, "auxPrice", 0) or 0) if hasattr(o, "auxPrice") else None
        po = PlacedOrder(
            order_id=trade.order.orderId,
            broker_order_id=str(trade.order.orderId),
            order_ref=stable_order_ref or requested_order_ref,
            symbol=str(symbol_override or getattr(c, "symbol", "") or ""),
            side=o.action.upper(),
            order_type=order_type_norm,
            qty=float(o.totalQuantity),
            limit_px=float(getattr(o, "lmtPrice", 0) or 0) if hasattr(o, "lmtPrice") else None,
            stop_px=aux_price if order_type_norm not in {"TRAIL", "TRAIL_LIMIT"} else None,
            trail_amount=aux_price if order_type_norm in {"TRAIL", "TRAIL_LIMIT"} else None,
            trail_percent=(
                float(getattr(o, "trailingPercent", 0) or 0)
                if getattr(o, "trailingPercent", None) not in (None, 0, 0.0)
                else None
            ),
            trail_stop_px=(
                float(getattr(o, "trailStopPrice", 0) or 0)
                if getattr(o, "trailStopPrice", None) not in (None, 0, 0.0)
                else None
            ),
            lmt_price_offset=(
                float(getattr(o, "lmtPriceOffset", 0) or 0)
                if getattr(o, "lmtPriceOffset", None) not in (None, 0, 0.0)
                else None
            ),
            tif=o.tif,
            oca_group=o.ocaGroup or None,
            status=status_norm or trade.orderStatus.status or "SUBMITTED",
            perm_id=stable_perm_id,
            ts_us=_now_us()
        )

        logger.info("Order placed", order_id=po.order_id, symbol=po.symbol,
                    side=po.side, type=po.order_type, qty=po.qty)

        return po

    async def place_limit(
        self,
        symbol: str,
        side: str,
        qty: float,
        px: float,
        tif: str,
        order_ref: str,
        oca_group: Optional[str] = None,
        *,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """Place limit order"""
        c = await self._contract(symbol, exchange_override=exchange_override)
        qty = self._normalize_quantity(c, qty)
        limit_px, _ = await self._normalize_price_fields(c, limit_px=px)
        o = Order()
        o.orderType = "LMT"
        o.lmtPrice = float(limit_px)
        o.action = side.upper()
        o.totalQuantity = qty
        o.tif = tif
        return await self._place(
            c,
            o,
            order_ref,
            oca_group,
            outside_rth=outside_rth,
            symbol_override=symbol,
            expected_epoch=expected_epoch,
        )

    async def place_stop(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_px: float,
        tif: str,
        order_ref: str,
        oca_group: Optional[str] = None,
        *,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """Place stop market order"""
        c = await self._contract(symbol, exchange_override=exchange_override)
        qty = self._normalize_quantity(c, qty)
        _, normalized_stop = await self._normalize_price_fields(c, stop_px=stop_px)
        o = Order()
        o.orderType = "STP"
        o.auxPrice = float(normalized_stop)
        o.action = side.upper()
        o.totalQuantity = qty
        o.tif = tif
        return await self._place(
            c,
            o,
            order_ref,
            oca_group,
            outside_rth=outside_rth,
            symbol_override=symbol,
            expected_epoch=expected_epoch,
        )

    async def place_stop_limit(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_px: float,
        limit_px: float,
        tif: str,
        order_ref: str,
        oca_group: Optional[str] = None,
        *,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """Place stop limit order"""
        c = await self._contract(symbol, exchange_override=exchange_override)
        qty = self._normalize_quantity(c, qty)
        normalized_limit, normalized_stop = await self._normalize_price_fields(
            c,
            limit_px=limit_px,
            stop_px=stop_px,
        )
        o = Order()
        o.orderType = "STP LMT"
        o.auxPrice = float(normalized_stop)
        o.lmtPrice = float(normalized_limit)
        o.action = side.upper()
        o.totalQuantity = qty
        o.tif = tif
        return await self._place(
            c,
            o,
            order_ref,
            oca_group,
            outside_rth=outside_rth,
            symbol_override=symbol,
            expected_epoch=expected_epoch,
        )

    async def place_trailing_stop(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str,
        order_ref: str,
        oca_group: Optional[str] = None,
        *,
        trail_amount: Optional[float] = None,
        trail_percent: Optional[float] = None,
        trail_stop_px: Optional[float] = None,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """Place native IBKR trailing stop order."""
        if (trail_amount is None) == (trail_percent is None):
            raise ValueError("TRAIL requires exactly one of trail_amount or trail_percent")
        c = await self._contract(symbol, exchange_override=exchange_override)
        qty = self._normalize_quantity(c, qty)
        _, normalized_trail_stop = await self._normalize_price_fields(c, stop_px=trail_stop_px)
        o = Order()
        o.orderType = "TRAIL"
        o.action = side.upper()
        o.totalQuantity = qty
        o.tif = tif
        if trail_percent is not None:
            if float(trail_percent) <= 0:
                raise ValueError("TRAIL trailingPercent must be positive")
            o.trailingPercent = float(trail_percent)
        else:
            if float(trail_amount or 0) <= 0:
                raise ValueError("TRAIL auxPrice trail amount must be positive")
            o.auxPrice = float(trail_amount)
        if normalized_trail_stop is not None:
            o.trailStopPrice = float(normalized_trail_stop)
        return await self._place(
            c,
            o,
            order_ref,
            oca_group,
            outside_rth=outside_rth,
            symbol_override=symbol,
            expected_epoch=expected_epoch,
        )

    async def place_trailing_stop_limit(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str,
        order_ref: str,
        oca_group: Optional[str] = None,
        *,
        trail_amount: Optional[float] = None,
        trail_percent: Optional[float] = None,
        trail_stop_px: Optional[float] = None,
        lmt_price_offset: Optional[float] = None,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """Place native IBKR trailing stop limit order."""
        if (trail_amount is None) == (trail_percent is None):
            raise ValueError("TRAIL_LIMIT requires exactly one of trail_amount or trail_percent")
        if lmt_price_offset is None or float(lmt_price_offset) <= 0:
            raise ValueError("TRAIL_LIMIT requires positive lmt_price_offset")
        c = await self._contract(symbol, exchange_override=exchange_override)
        qty = self._normalize_quantity(c, qty)
        normalized_offset, normalized_trail_stop = await self._normalize_price_fields(
            c,
            limit_px=lmt_price_offset,
            stop_px=trail_stop_px,
        )
        o = Order()
        o.orderType = "TRAIL LIMIT"
        o.action = side.upper()
        o.totalQuantity = qty
        o.tif = tif
        if trail_percent is not None:
            if float(trail_percent) <= 0:
                raise ValueError("TRAIL_LIMIT trailingPercent must be positive")
            o.trailingPercent = float(trail_percent)
        else:
            if float(trail_amount or 0) <= 0:
                raise ValueError("TRAIL_LIMIT auxPrice trail amount must be positive")
            o.auxPrice = float(trail_amount)
        if normalized_trail_stop is not None:
            o.trailStopPrice = float(normalized_trail_stop)
        o.lmtPriceOffset = float(normalized_offset)
        return await self._place(
            c,
            o,
            order_ref,
            oca_group,
            outside_rth=outside_rth,
            symbol_override=symbol,
            expected_epoch=expected_epoch,
        )

    async def place_market(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str,
        order_ref: str,
        oca_group: Optional[str] = None,
        *,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """Place market order"""
        c = await self._contract(symbol, exchange_override=exchange_override)
        qty = self._normalize_quantity(c, qty)
        o = Order()
        o.orderType = "MKT"
        o.action = side.upper()
        o.totalQuantity = qty
        o.tif = tif
        return await self._place(
            c,
            o,
            order_ref,
            oca_group,
            outside_rth=outside_rth,
            symbol_override=symbol,
            expected_epoch=expected_epoch,
        )

    async def emergency_exit(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str,
        order_ref: str,
        *,
        limit_px: Optional[float] = None,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """Place the fallback close order used when protective orders fail."""
        if limit_px is not None:
            return await self.place_limit(
                symbol,
                side,
                qty,
                limit_px,
                tif,
                order_ref,
                outside_rth=outside_rth,
                exchange_override=exchange_override,
                expected_epoch=expected_epoch,
            )
        return await self.place_market(
            symbol,
            side,
            qty,
            tif,
            order_ref,
            outside_rth=outside_rth,
            exchange_override=exchange_override,
            expected_epoch=expected_epoch,
        )

    async def place_combo_order(
        self,
        underlying: str,
        combo_legs: List[ComboLeg],
        order_type: str,
        limit_price: Optional[float] = None,
        qty: int = 1,
        side: str = "SELL",
        tif: str = "DAY",
        order_ref: str = "",
        oca_group: Optional[str] = None,
        transmit: bool = True,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        """
        Place combo order (option spread)

        Args:
            underlying: Underlying symbol (e.g., "QQQ")
            combo_legs: List of ComboLeg objects defining the spread
            order_type: Order type ("LMT" or "MKT")
            limit_price: Limit price for the spread (credit/debit)
            qty: Number of spreads
            side: "SELL" (credit spread) or "BUY" (debit spread)
            tif: Time in force
            order_ref: Order reference for tracking
            oca_group: OCA group for TP/SL coordination
            transmit: Whether to transmit order immediately

        Returns:
            PlacedOrder with order details
        """
        await self._connect()

        from ib_insync import Contract

        # Build BAG contract
        combo = Contract()
        combo.symbol = underlying
        combo.secType = "BAG"
        combo.currency = self.ccy
        combo.exchange = self.exch
        combo.comboLegs = combo_legs
        # Do not qualify the BAG itself (IB rejects contractData requests for BAG);
        # legs are already qualified upstream.

        # Build order
        o = Order()
        o.action = side.upper()
        o.orderType = order_type.upper()
        o.totalQuantity = abs(qty)
        o.tif = tif
        o.transmit = transmit

        if order_type.upper() == "LMT" and limit_price is not None:
            o.lmtPrice = float(limit_price)

        if self.account:
            o.account = self.account

        # Use _place for consistency
        return await self._place(
            combo,
            o,
            order_ref,
            oca_group,
            symbol_override=underlying,
            expected_epoch=expected_epoch,
        )

    async def build_combo_legs_from_specs(
        self,
        underlying: str,
        legs: List[Dict[str, Any]],
        order_side: str = "BUY",
    ) -> List[ComboLeg]:
        """
        Build and qualify ComboLegs from option leg specs.

        legs items must include: expiry (YYYY-MM-DD), strike, right, side, qty.
        """
        await self._connect()

        options: List[Option] = []
        for leg in legs:
            expiry = str(leg["expiry"]).replace("-", "")
            opt = Option(underlying, expiry, float(leg["strike"]), leg["right"], self.exch)
            options.append(opt)

        await self.ib.qualifyContractsAsync(*options)

        combo_legs: List[ComboLeg] = []
        invert = str(order_side).upper() == "SELL"
        for leg, opt in zip(legs, options):
            if not getattr(opt, "conId", None):
                raise ValueError(f"Failed to qualify option leg: {leg}")
            combo_leg = ComboLeg()
            combo_leg.conId = opt.conId
            combo_leg.ratio = abs(int(leg.get("qty", 1)))
            action = str(leg["side"]).upper()
            if invert:
                action = "SELL" if action == "BUY" else "BUY"
            combo_leg.action = action
            combo_leg.exchange = self.exch
            combo_legs.append(combo_leg)

        return combo_legs

    async def place_vertical_spread(
        self,
        *,
        underlying: str,
        expiry: str,
        short_strike: float,
        long_strike: float,
        qty: int,
        side: str = "SELL",
        right: str = "P",
        limit_price: Optional[float] = None,
        tif: str = "DAY",
        order_ref: str = "",
        oca_group: Optional[str] = None,
        transmit: bool = True,
    ) -> PlacedOrder:
        """Place an option vertical spread as an IBKR BAG (combo) order.

        Contract semantics follow IB's BAG convention:

        - ComboLeg.action describes the leg directions when BUYING the combo.
        - SELLing the combo reverses all legs.

        To represent a bull put CREDIT spread (short higher-strike put, long lower-strike put):
          * We define the BAG legs as a DEBIT bear put (BUY higher strike, SELL lower strike).
          * Entry uses side='SELL' with a positive limit_price (credit received).
          * Exit uses side='BUY' to buy back the same combo.

        Expiry is converted from `YYYY-MM-DD` to IB's `YYYYMMDD` automatically.
        """
        await self._connect()

        side_u = side.upper()
        if side_u not in {"BUY", "SELL"}:
            raise ValueError(f"Unsupported side: {side!r}")

        expiry_ib = expiry.replace("-", "")
        if len(expiry_ib) != 8:
            raise ValueError(f"Invalid expiry format: {expiry!r}")

        short_opt = Option(underlying, expiry_ib, float(short_strike), right, self.exch)
        long_opt = Option(underlying, expiry_ib, float(long_strike), right, self.exch)
        await self.ib.qualifyContractsAsync(short_opt, long_opt)

        if not getattr(short_opt, "conId", None) or not getattr(long_opt, "conId", None):
            raise ValueError(
                f"Failed to qualify option legs: short_conId={getattr(short_opt,'conId',None)} "
                f"long_conId={getattr(long_opt,'conId',None)}"
            )

        # Define combo legs as a *debit* bear put when BUYING the combo:
        #   BUY higher strike (short_strike), SELL lower strike (long_strike).
        # SELLing this combo corresponds to the intended bull put CREDIT spread.
        short_leg = ComboLeg()
        short_leg.conId = short_opt.conId
        short_leg.ratio = abs(int(qty))
        short_leg.action = "BUY"
        short_leg.exchange = self.exch

        long_leg = ComboLeg()
        long_leg.conId = long_opt.conId
        long_leg.ratio = abs(int(qty))
        long_leg.action = "SELL"
        long_leg.exchange = self.exch

        if not order_ref:
            order_ref = f"vertical_{underlying}_{expiry}_{short_strike}_{long_strike}"

        return await self.place_combo_order(
            underlying=underlying,
            combo_legs=[short_leg, long_leg],
            order_type="LMT" if limit_price is not None else "MKT",
            limit_price=limit_price,
            qty=abs(int(qty)),
            side=side_u,
            tif=tif,
            order_ref=order_ref,
            oca_group=oca_group,
            transmit=transmit,
        )

    async def modify(self, order_id: int, **fields) -> bool:
        """
        Modify existing order

        Args:
            order_id: Order ID to modify
            **fields: Fields to update (qty, limit_px, stop_px, tif)

        Returns:
            True if successful
        """
        await self._connect()
        expected_epoch = fields.pop("expected_epoch", None)

        async with self._lock:
            t = self._trades.get(order_id)

        if not t:
            # Try to find in open trades
            for tr in self.ib.openTrades():
                if tr.order.orderId == order_id:
                    t = tr
                    break

        if not t:
            logger.warning("Order not found for modification", order_id=order_id)
            return False

        modifiable = await self._await_trade_modifiable(t)
        if not modifiable:
            logger.warning(
                "order_not_modifiable_yet",
                order_id=order_id,
                status=str(getattr(getattr(t, "orderStatus", None), "status", "") or ""),
            )
            return False

        o = t.order

        # Update supported fields
        if "qty" in fields:
            o.totalQuantity = float(fields["qty"])
        if "limit_px" in fields:
            o.lmtPrice = float(fields["limit_px"])
        if "stop_px" in fields:
            o.auxPrice = float(fields["stop_px"])
        if "tif" in fields:
            o.tif = fields["tif"]

        # Re-place order (IB treats this as modification)
        self._require_epoch(expected_epoch, operation="modify_order")
        self.ib.placeOrder(t.contract, o)
        logger.info("Order modified", order_id=order_id, fields=fields)
        return True

    async def modify_stop_order(
        self,
        order_id: int,
        *,
        stop_px: Optional[float] = None,
        qty: Optional[float] = None,
        expected_epoch: Optional[int] = None,
    ) -> bool:
        fields: Dict[str, Any] = {}
        if stop_px is not None:
            fields["stop_px"] = float(stop_px)
        if qty is not None:
            fields["qty"] = float(qty)
        if expected_epoch is not None:
            fields["expected_epoch"] = int(expected_epoch)
        if not fields:
            return True
        return await self.modify(order_id, **fields)

    async def replace_protective_order_atomicish(
        self,
        *,
        old_order_id: Optional[int],
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        tif: str,
        order_ref: str,
        limit_px: Optional[float] = None,
        stop_px: Optional[float] = None,
        trail_amount: Optional[float] = None,
        trail_percent: Optional[float] = None,
        trail_stop_px: Optional[float] = None,
        lmt_price_offset: Optional[float] = None,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> PlacedOrder:
        order_type_u = str(order_type or "").strip().upper().replace(" ", "_")
        if order_type_u == "STP":
            new_order = await self.place_stop(
                symbol,
                side,
                qty,
                float(stop_px),
                tif,
                order_ref,
                outside_rth=outside_rth,
                exchange_override=exchange_override,
                expected_epoch=expected_epoch,
            )
        elif order_type_u == "STP_LMT":
            new_order = await self.place_stop_limit(
                symbol,
                side,
                qty,
                float(stop_px),
                float(limit_px),
                tif,
                order_ref,
                outside_rth=outside_rth,
                exchange_override=exchange_override,
                expected_epoch=expected_epoch,
            )
        elif order_type_u == "TRAIL":
            new_order = await self.place_trailing_stop(
                symbol,
                side,
                qty,
                tif,
                order_ref,
                trail_amount=trail_amount,
                trail_percent=trail_percent,
                trail_stop_px=trail_stop_px,
                outside_rth=outside_rth,
                exchange_override=exchange_override,
                expected_epoch=expected_epoch,
            )
        elif order_type_u == "TRAIL_LIMIT":
            new_order = await self.place_trailing_stop_limit(
                symbol,
                side,
                qty,
                tif,
                order_ref,
                trail_amount=trail_amount,
                trail_percent=trail_percent,
                trail_stop_px=trail_stop_px,
                lmt_price_offset=lmt_price_offset,
                outside_rth=outside_rth,
                exchange_override=exchange_override,
                expected_epoch=expected_epoch,
            )
        else:
            raise ValueError(f"unsupported protective replacement order type: {order_type}")
        status = str(new_order.status or "").strip()
        if status not in {"PreSubmitted", "Submitted", "SUBMITTED"}:
            raise RuntimeError(f"new protective order not active: {new_order.order_id} {status}")
        if old_order_id is not None:
            await self.cancel(int(old_order_id), expected_epoch=expected_epoch)
        return new_order

    async def cancel(self, order_id: int, *, expected_epoch: Optional[int] = None) -> bool:
        """
        Cancel order

        Args:
            order_id: Order ID to cancel

        Returns:
            True if successful
        """
        return await self._cancel_impl(order_id, expected_epoch=expected_epoch)

    async def _cancel_impl(
        self,
        order_id: int,
        *,
        owner_client_id: Optional[int] = None,
        perm_id: Any = None,
        order_ref: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> bool:
        await self._connect()

        async with self._lock:
            t = self._trades.get(order_id)

        if not t:
            # Refresh broker open-order cache once before concluding the order is gone.
            try:
                if hasattr(self.ib, "reqAllOpenOrdersAsync"):
                    await self.ib.reqAllOpenOrdersAsync()
                elif hasattr(self.ib, "reqOpenOrdersAsync"):
                    await self.ib.reqOpenOrdersAsync()
            except Exception as exc:
                logger.warning("ib_req_open_orders_before_cancel_failed", order_id=order_id, error=str(exc))
            for tr in self.ib.openTrades():
                if tr.order.orderId == order_id:
                    t = tr
                    break

        if not t:
            logger.warning("Order not found for cancellation", order_id=order_id)
            if owner_client_id is not None and int(owner_client_id) != int(self.client_id):
                return await self._cancel_via_owner_client(
                    order_id=order_id,
                    owner_client_id=int(owner_client_id),
                    perm_id=perm_id,
                    order_ref=order_ref,
                )
            return False

        if owner_client_id is not None and int(owner_client_id) != int(self.client_id):
            owner_cancelled = await self._cancel_via_owner_client(
                order_id=order_id,
                owner_client_id=int(owner_client_id),
                perm_id=perm_id,
                order_ref=order_ref or getattr(getattr(t, "order", None), "orderRef", None),
            )
            if owner_cancelled:
                logger.info(
                    "ib_cancel_requested_via_owner_client",
                    order_id=order_id,
                    owner_client_id=int(owner_client_id),
                    live_client_id=self.client_id,
                    perm_id=perm_id,
                    order_ref=order_ref or getattr(getattr(t, "order", None), "orderRef", None),
                )
                return True

        logger.warning(
            "ib_cancel_requested",
            order_id=order_id,
            broker_order_id=str(order_id),
            client_id=getattr(getattr(t, "order", None), "clientId", None),
            perm_id=getattr(getattr(t, "order", None), "permId", None),
            order_ref=getattr(getattr(t, "order", None), "orderRef", None),
            symbol=getattr(getattr(t, "contract", None), "localSymbol", None) or getattr(getattr(t, "contract", None), "symbol", None),
            side=getattr(getattr(t, "order", None), "action", None),
            order_type=getattr(getattr(t, "order", None), "orderType", None),
            status=getattr(getattr(t, "orderStatus", None), "status", None),
            why_held=getattr(getattr(t, "orderStatus", None), "whyHeld", None),
        )
        mark_expected_cancel_not_found(order_id)
        self._require_epoch(expected_epoch, operation="cancel_order")
        self.ib.cancelOrder(t.order)
        logger.info("Order cancelled", order_id=order_id)
        return True

    async def cancel_with_owner(
        self,
        order_id: int,
        *,
        owner_client_id: Optional[int] = None,
        perm_id: Any = None,
        order_ref: Optional[str] = None,
    ) -> bool:
        return await self._cancel_impl(
            order_id,
            owner_client_id=owner_client_id,
            perm_id=perm_id,
            order_ref=order_ref,
        )

    async def _cancel_via_owner_client(
        self,
        *,
        order_id: int,
        owner_client_id: int,
        perm_id: Any = None,
        order_ref: Optional[str] = None,
    ) -> bool:
        if self._managed_by_connection_manager:
            logger.info(
                "ib_owner_client_cancel_skipped_managed_mode",
                order_id=order_id,
                owner_client_id=int(owner_client_id),
            )
            return False
        if not self._aux_probe_sessions_enabled:
            logger.info(
                "ib_owner_client_cancel_skipped_aux_probe_disabled",
                order_id=order_id,
                owner_client_id=int(owner_client_id),
            )
            return False
        circuit_state = get_ib_gateway_connection_circuit().check_allowed()
        if circuit_state.open:
            logger.warning(
                "ib_owner_client_cancel_skipped_gateway_circuit_open",
                order_id=order_id,
                owner_client_id=int(owner_client_id),
                reason=circuit_state.reason,
                source=circuit_state.source,
                manual_required=bool(getattr(circuit_state, "manual_required", False)),
                retry_after_sec=round(circuit_state.retry_after_sec, 2),
            )
            return False
        if int(owner_client_id) == int(self.client_id):
            return False
        probe_key = (int(order_id), int(owner_client_id))
        inflight = self._owner_probe_inflight.get(probe_key)
        if inflight is not None and not inflight.done():
            return await asyncio.shield(inflight)
        now = time.time()
        min_interval_sec = self._current_owner_probe_min_interval_sec()
        last_ts = float(self._owner_probe_last_ts.get(probe_key, 0.0) or 0.0)
        if min_interval_sec > 0 and last_ts > 0 and (now - last_ts) < min_interval_sec:
            cached = bool(self._owner_probe_last_result.get(probe_key, False))
            logger.debug(
                "ib_owner_client_cancel_probe_throttled",
                order_id=order_id,
                owner_client_id=int(owner_client_id),
                cached_result=cached,
                min_interval_sec=min_interval_sec,
                elapsed_sec=round(now - last_ts, 3),
            )
            return cached
        async def _run_probe() -> bool:
            probe = IB()
            found_trade = None
            gk = self._reconnect_gatekeeper
            try:
                if gk is not None:
                    allowed = await gk.acquire(
                        caller="exec_owner_cancel_probe",
                        host=self.host,
                        port=self.port,
                    )
                    if not allowed:
                        self._owner_probe_last_ts[probe_key] = time.time()
                        self._owner_probe_last_result[probe_key] = False
                        return False
                await probe.connectAsync(self.host, self.port, clientId=int(owner_client_id), timeout=5.0)
                if gk is not None:
                    gk.report_success(caller="exec_owner_cancel_probe")
                if hasattr(probe, "reqAllOpenOrdersAsync"):
                    await probe.reqAllOpenOrdersAsync()
                elif hasattr(probe, "reqOpenOrdersAsync"):
                    await probe.reqOpenOrdersAsync()
                else:
                    probe.reqAllOpenOrders()
                    await asyncio.sleep(0.2)
                await asyncio.sleep(0.2)
                for trade in probe.openTrades():
                    try:
                        ib_order = getattr(trade, "order", None)
                        if ib_order is None:
                            continue
                        if int(getattr(ib_order, "orderId", 0) or 0) != int(order_id):
                            continue
                        if self.account and getattr(ib_order, "account", "") and str(getattr(ib_order, "account", "")) != str(self.account):
                            continue
                        found_trade = trade
                        break
                    except Exception:
                        continue
                if found_trade is None:
                    logger.warning(
                        "ib_owner_client_cancel_order_missing",
                        order_id=order_id,
                        owner_client_id=int(owner_client_id),
                        perm_id=perm_id,
                        order_ref=order_ref,
                    )
                    self._owner_probe_last_ts[probe_key] = time.time()
                    self._owner_probe_last_result[probe_key] = False
                    return False
                mark_expected_cancel_not_found(order_id)
                probe.cancelOrder(found_trade.order)
                await asyncio.sleep(0.2)
                logger.warning(
                    "ib_owner_client_cancel_requested",
                    order_id=order_id,
                    owner_client_id=int(owner_client_id),
                    perm_id=perm_id,
                    order_ref=order_ref,
                    status=getattr(getattr(found_trade, "orderStatus", None), "status", None),
                )
                self._owner_probe_last_ts[probe_key] = time.time()
                self._owner_probe_last_result[probe_key] = True
                return True
            except Exception as exc:
                if gk is not None:
                    gk.report_failure(caller="exec_owner_cancel_probe", error=str(exc), host=self.host, port=self.port)
                self._owner_probe_last_ts[probe_key] = time.time()
                self._owner_probe_last_result[probe_key] = False
                logger.warning(
                    "ib_owner_client_cancel_failed",
                    order_id=order_id,
                    owner_client_id=int(owner_client_id),
                    perm_id=perm_id,
                    order_ref=order_ref,
                    error=str(exc),
                )
                return False
            finally:
                with contextlib.suppress(Exception):
                    probe.disconnect()

        task = asyncio.create_task(_run_probe())
        self._owner_probe_inflight[probe_key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._owner_probe_inflight.get(probe_key) is task:
                self._owner_probe_inflight.pop(probe_key, None)

    async def position_qty(self, symbol: str) -> float:
        """
        Get current live position quantity for a symbol (broker-side).

        Returns:
            Net position qty (positive long, negative short), 0 if flat/unknown.
        """
        await self._connect()

        sym = str(symbol or "").strip().upper()
        if not sym:
            return 0.0

        qty = 0.0
        try:
            for p in self.ib.positions():
                try:
                    if self.account and getattr(p, "account", "") and getattr(p, "account", "") != self.account:
                        continue
                    c = getattr(p, "contract", None)
                    if not c:
                        continue
                    if str(getattr(c, "symbol", "") or "").strip().upper() != sym:
                        continue
                    qty += float(getattr(p, "position", 0.0) or 0.0)
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("ib_position_qty_failed", symbol=sym, error=str(exc))
            return 0.0

        return float(qty)

    async def open_orders(self, *, force_refresh: bool = False) -> List[PlacedOrder]:
        """
        Get all open orders

        Returns:
            List of PlacedOrder objects
        """
        await self._connect()

        # Important: after reconnect/restart, ib_insync may not have a complete openTrades cache
        # until we explicitly request open orders from IB. Without this, reconciler can misclassify
        # live broker orders as "ghost" and incorrectly terminalize DB rows.
        now = time.time()
        recovery_active = self._manager_runtime_recovery_active()
        refresh_sec = float(self._open_orders_refresh_sec or 0.0)
        if recovery_active:
            refresh_sec = max(refresh_sec, float(self._open_orders_refresh_recovery_sec or refresh_sec))
        should_refresh = bool(force_refresh) or (
            (now - float(self._last_open_orders_req_ts or 0.0)) >= refresh_sec
        )
        if should_refresh:
            self._last_open_orders_req_ts = now
            try:
                timeout_sec = max(0.0, float(self._open_orders_req_timeout_sec or 0.0))
                if hasattr(self.ib, "reqAllOpenOrdersAsync"):
                    coro = self.ib.reqAllOpenOrdersAsync()
                elif hasattr(self.ib, "reqOpenOrdersAsync"):
                    coro = self.ib.reqOpenOrdersAsync()
                else:
                    coro = None
                if coro is not None:
                    if timeout_sec > 0.0:
                        await asyncio.wait_for(coro, timeout=timeout_sec)
                    else:
                        await coro
            except asyncio.TimeoutError:
                logger.warning("ib_req_open_orders_timeout", timeout_sec=float(self._open_orders_req_timeout_sec))
            except Exception as exc:
                logger.warning("ib_req_open_orders_failed", error=str(exc))

        orders = []
        for trade in self.ib.openTrades():
            o = trade.order
            try:
                if self.account and getattr(o, "account", "") and str(getattr(o, "account", "")) != str(self.account):
                    continue
            except Exception:
                pass
            sym = str(getattr(trade.contract, "symbol", "") or "")
            try:
                from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract

                mapped = internal_opt_symbol_from_ib_contract(trade.contract)
                if mapped:
                    sym = mapped
            except Exception:
                pass
            po = PlacedOrder(
                order_id=o.orderId,
                broker_order_id=str(o.orderId),
                order_ref=o.orderRef or "",
                symbol=sym,
                side=o.action.upper(),
                order_type=o.orderType.replace(" ", "_"),
                qty=float(o.totalQuantity),
                limit_px=float(getattr(o, "lmtPrice", 0) or 0) if hasattr(o, "lmtPrice") else None,
                stop_px=float(getattr(o, "auxPrice", 0) or 0) if hasattr(o, "auxPrice") else None,
                tif=o.tif,
                oca_group=o.ocaGroup or None,
                status=trade.orderStatus.status,
                perm_id=o.permId if hasattr(o, 'permId') else None,
                client_id=int(getattr(o, "clientId", 0) or 0) or None,
                ts_us=_now_us()
            )
            orders.append(po)

        if force_refresh and orders:
            filtered_orders: List[PlacedOrder] = []
            filtered_unconfirmed_ids: List[int] = []
            for po in orders:
                status_norm = str(getattr(po, "status", "") or "").strip()
                probe_found: Optional[bool] = None
                probe_error: Optional[str] = None
                probe_status: Optional[str] = None
                age_sec: Optional[float] = None
                try:
                    ts_us = float(getattr(po, "ts_us", 0) or 0)
                    if ts_us > 0:
                        age_sec = max(0.0, (time.time() * 1_000_000 - ts_us) / 1_000_000.0)
                except Exception:
                    age_sec = None
                try:
                    probe = await self.probe_order_open_fresh(int(po.order_id))
                    probe_found = probe.get("found")
                    probe_error = probe.get("error")
                    probe_status = str(probe.get("status") or "").strip() or None
                except Exception as exc:
                    probe_error = str(exc)

                if probe_found is False:
                    if self._is_locally_confirmable_protective_status(
                        order_ref=str(po.order_ref or ""),
                        status=status_norm,
                        oca_group=po.oca_group,
                    ):
                        if age_sec is not None and age_sec > _LOCAL_PROTECTIVE_PENDING_GRACE_SEC:
                            filtered_unconfirmed_ids.append(int(po.order_id))
                            logger.warning(
                                "ib_open_orders_force_refresh_drop_stale_local_protective_pending",
                                order_id=int(po.order_id),
                                order_ref=str(po.order_ref or ""),
                                symbol=str(po.symbol or ""),
                                status=status_norm,
                                age_sec=round(float(age_sec), 2),
                                probe_status=probe_status,
                                probe_error=probe_error,
                            )
                            continue
                        filtered_orders.append(po)
                        logger.info(
                            "ib_open_orders_force_refresh_keep_local_protective_pending",
                            order_id=int(po.order_id),
                            order_ref=str(po.order_ref or ""),
                            symbol=str(po.symbol or ""),
                            status=status_norm,
                            age_sec=round(float(age_sec), 2) if age_sec is not None else None,
                            probe_status=probe_status,
                            probe_error=probe_error,
                        )
                        continue
                    filtered_unconfirmed_ids.append(int(po.order_id))
                    logger.info(
                        "ib_open_orders_force_refresh_unconfirmed_filtered",
                        order_id=int(po.order_id),
                        order_ref=str(po.order_ref or ""),
                        symbol=str(po.symbol or ""),
                        status=status_norm,
                        probe_status=probe_status,
                        probe_error=probe_error,
                    )
                    continue

                filtered_orders.append(po)

            orders = filtered_orders

        if force_refresh or should_refresh:
            logger.info(
                "ib_open_orders_snapshot",
                force_refresh=bool(force_refresh),
                recovery_active=bool(recovery_active),
                refresh_sec=round(float(refresh_sec), 2),
                open_count=len(orders),
                client_id=self.client_id,
                filtered_unconfirmed_ids=filtered_unconfirmed_ids if force_refresh and 'filtered_unconfirmed_ids' in locals() else [],
                sample=[
                    {
                        "order_id": int(o.order_id),
                        "symbol": str(o.symbol),
                        "status": str(o.status),
                        "side": str(o.side),
                        "order_type": str(o.order_type),
                        "perm_id": o.perm_id,
                        "order_ref": str(o.order_ref),
                    }
                    for o in orders[:10]
                ],
            )
        return orders

    async def probe_order_open_fresh(self, order_id: int) -> Dict[str, Any]:
        """
        Query broker open orders from a fresh IB session for diagnostics.

        This is intentionally used only on anomaly paths so we can compare the
        live adapter's long-lived session view with a clean broker snapshot.
        """
        try:
            oid = int(order_id)
        except Exception:
            return {"found": None, "error": f"invalid_order_id:{order_id}"}
        if self._managed_by_connection_manager:
            return {
                "found": None,
                "probe_client_id": None,
                "owner_client_id": None,
                "status": None,
                "perm_id": None,
                "why_held": None,
                "error": "managed_mode_probe_disabled",
                "throttled": False,
                "checked": False,
            }
        if not self._aux_probe_sessions_enabled:
            return {
                "found": None,
                "probe_client_id": None,
                "owner_client_id": None,
                "status": None,
                "perm_id": None,
                "why_held": None,
                "error": "aux_probe_sessions_disabled",
                "throttled": False,
                "checked": False,
            }
        circuit_state = get_ib_gateway_connection_circuit().check_allowed()
        if circuit_state.open:
            return {
                "found": None,
                "probe_client_id": None,
                "owner_client_id": None,
                "status": None,
                "perm_id": None,
                "why_held": None,
                "error": "gateway_circuit_open:aux_probe_blocked",
                "throttled": False,
                "checked": False,
                "gateway_circuit": circuit_state.as_dict(),
            }

        now = time.time()
        min_interval_sec = self._current_fresh_probe_min_interval_sec()
        last_ts = float(self._fresh_probe_last_ts.get(oid, 0.0) or 0.0)
        inflight = self._fresh_probe_inflight.get(oid)
        if inflight is not None and not inflight.done():
            return await asyncio.shield(inflight)
        if min_interval_sec > 0 and last_ts > 0 and (now - last_ts) < min_interval_sec:
            cached = self._fresh_probe_last_result.get(oid)
            if cached is not None:
                result_cached = dict(cached)
                result_cached["error"] = result_cached.get("error") or "fresh_probe_throttled_cached"
                result_cached["throttled"] = True
                logger.debug(
                    "ib_probe_order_open_fresh_throttled",
                    order_id=oid,
                    min_interval_sec=min_interval_sec,
                    elapsed_sec=round(now - last_ts, 3),
                )
                return result_cached

        probe_client_id = int(self.client_id) + int(self._fresh_probe_client_id_offset)
        async def _run_probe() -> Dict[str, Any]:
            probe = IB()
            gk = self._reconnect_gatekeeper
            result: Dict[str, Any] = {
                "found": None,
                "probe_client_id": probe_client_id,
                "owner_client_id": None,
                "status": None,
                "perm_id": None,
                "why_held": None,
                "error": None,
                "throttled": False,
            }

            try:
                if gk is not None:
                    allowed = await gk.acquire(
                        caller="exec_fresh_order_probe",
                        host=self.host,
                        port=self.port,
                    )
                    if not allowed:
                        result["error"] = "reconnect_gatekeeper_denied:exec_fresh_order_probe"
                        return result
                await probe.connectAsync(self.host, self.port, clientId=probe_client_id, timeout=5.0)
                if gk is not None:
                    gk.report_success(caller="exec_fresh_order_probe")
                if hasattr(probe, "reqAllOpenOrdersAsync"):
                    await probe.reqAllOpenOrdersAsync()
                elif hasattr(probe, "reqOpenOrdersAsync"):
                    await probe.reqOpenOrdersAsync()
                else:
                    probe.reqAllOpenOrders()
                    await asyncio.sleep(0.2)
                await asyncio.sleep(0.2)

                result["found"] = False
                for trade in probe.openTrades():
                    try:
                        o = getattr(trade, "order", None)
                        if o is None or int(getattr(o, "orderId", 0) or 0) != oid:
                            continue
                        if self.account and getattr(o, "account", "") and str(getattr(o, "account", "")) != str(self.account):
                            continue
                        result["found"] = True
                        result["owner_client_id"] = getattr(o, "clientId", None)
                        result["perm_id"] = getattr(o, "permId", None)
                        result["status"] = getattr(getattr(trade, "orderStatus", None), "status", None)
                        result["why_held"] = getattr(getattr(trade, "orderStatus", None), "whyHeld", None)
                        break
                    except Exception:
                        continue
            except Exception as exc:
                if gk is not None:
                    gk.report_failure(caller="exec_fresh_order_probe", error=str(exc), host=self.host, port=self.port)
                result["error"] = str(exc)
            finally:
                with contextlib.suppress(Exception):
                    probe.disconnect()

            self._fresh_probe_last_ts[oid] = time.time()
            self._fresh_probe_last_result[oid] = dict(result)
            if len(self._fresh_probe_last_ts) > 2048:
                # keep memory bounded on long-running sessions
                oldest = sorted(self._fresh_probe_last_ts.items(), key=lambda kv: kv[1])[:256]
                for stale_oid, _ in oldest:
                    self._fresh_probe_last_ts.pop(stale_oid, None)
                    self._fresh_probe_last_result.pop(stale_oid, None)

            logger.info("ib_probe_order_open_fresh_result", order_id=oid, **result)
            return result

        task = asyncio.create_task(_run_probe())
        self._fresh_probe_inflight[oid] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._fresh_probe_inflight.get(oid) is task:
                self._fresh_probe_inflight.pop(oid, None)

    def is_connected(self) -> bool:
        """Check if connected to IB"""
        return self.ib.isConnected()


# Global adapter singleton
_global_adapter: Optional[IBInsyncAdapter] = None


def get_ib_adapter() -> IBInsyncAdapter:
    """
    Get global IBInsyncAdapter singleton.

    This ensures all parts of the system use the same IB connection,
    avoiding multiple connection conflicts and enabling centralized
    order tracking.

    Returns:
        Global IBInsyncAdapter instance
    """
    global _global_adapter

    # Legacy access is read-only compatibility: only return an already-registered
    # shared adapter or an explicitly seeded test adapter. Do not self-create.
    try:
        from services.ibkr.ib_connection_manager import get_ib_connection_manager

        mgr = get_ib_connection_manager()
        shared_adapter = getattr(mgr, "_exec_adapter", None)
        if shared_adapter is not None:
            _global_adapter = shared_adapter
            return shared_adapter
    except Exception:
        pass

    if _global_adapter is not None:
        return _global_adapter

    raise RuntimeError("ib_global_adapter_unavailable_use_connection_manager")


def peek_global_ib_adapter() -> Optional[IBInsyncAdapter]:
    """Return global adapter singleton if already created, without side effects."""
    return _global_adapter


async def init_global_ib_adapter() -> IBInsyncAdapter:
    """
    Initialize and start the global IB adapter.

    Should be called once during system startup.

    Returns:
        Started global IBInsyncAdapter instance
    """
    from services.ibkr.ib_connection_manager import get_ib_connection_manager

    ib_cfg = config.ib_config
    adapter = await get_ib_connection_manager().get_exec_adapter(
        host=ib_cfg.get("host", "127.0.0.1"),
        port=int(ib_cfg.get("port", 4002)),
        account=os.getenv("IB_ACCOUNT", ""),
    )
    global _global_adapter
    _global_adapter = adapter
    logger.info("Global IBInsyncAdapter started")
    return adapter
