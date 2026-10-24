"""
Fill Event Handler
Processes fill events from broker and updates order state + releases capital

Responsibilities:
- Subscribe to IB fill events
- Convert broker fills to internal Fill objects
- Call SafetyKernel.on_fill() for processing
- Handle partial and full fills
- Ensure idempotency (same fill not processed twice)

Based on doc/system/1.md specification.
"""
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Set, Optional, Dict, Any
from dataclasses import dataclass

from prometheus_client import Counter, Histogram

from libs.logger import get_logger
from libs.obs.audit_writer import LocalAuditWriter
from services.strategy_runtime_v2 import build_local_audit_transition_sink
from services.notifications import NotificationEvent, NotificationManager
from .order_event_writer import emit_order_event
from .flatten_controller import FlattenController
from .safety_kernel import SafetyKernel
from .models import Fill
from services.reconciliation.broker_truth_convergence import BrokerTruthConvergenceService

logger = get_logger(__name__)


# ============================================================================
# Metrics
# ============================================================================

fills_received_total = Counter(
    'fills_received_total',
    'Total fills received from broker'
)

fills_processed_total = Counter(
    'fills_processed_total',
    'Total fills processed successfully'
)

fills_duplicate_total = Counter(
    'fills_duplicate_total',
    'Duplicate fills ignored'
)

fills_error_total = Counter(
    'fills_error_total',
    'Fill processing errors'
)

fill_processing_latency = Histogram(
    'fill_processing_latency_seconds',
    'Fill processing latency',
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5]
)


# ============================================================================
# Fill Event Handler
# ============================================================================

class FillEventHandler:
    """
    Fill Event Handler

    Subscribes to broker fill events and processes them:
    1. Receive fill from IB adapter
    2. Convert to internal Fill object
    3. Check for duplicates (idempotency)
    4. Call SafetyKernel.on_fill() for:
       - Database persistence (core_fill table)
       - Order state update (PARTIAL/FILLED)
       - Capital reservation release (if FILLED)
       - Position tracking
       - OCO attachment (on first fill)
    """

    def __init__(
        self,
        ib_adapter,
        safety_kernel: SafetyKernel,
        notification_manager: Optional[NotificationManager] = None,
        order_event_enabled: bool = True,
        audit_writer: Optional[LocalAuditWriter] = None,
    ):
        """
        Initialize Fill Event Handler

        Args:
            ib_adapter: IB adapter (IBInsyncAdapter)
            safety_kernel: Safety kernel for fill processing
        """
        self.adapter = ib_adapter
        self.kernel = safety_kernel
        existing_flatten_controller = getattr(safety_kernel, "flatten_controller", None)
        self.flatten_controller = (
            existing_flatten_controller
            if isinstance(existing_flatten_controller, FlattenController)
            else FlattenController(
                pg=getattr(safety_kernel, "pg", None),
                manual_intervention_lock_store=getattr(safety_kernel, "manual_intervention_lock_store", None),
                transition_sink=build_local_audit_transition_sink(
                    audit_writer,
                    component="fill_event_handler_flatten",
                ),
            )
        )
        self.notification_manager = notification_manager
        self.order_event_enabled = bool(order_event_enabled)
        self.audit_writer = audit_writer
        self._cancel_notification_enabled = str(
            os.getenv("TELEGRAM_NOTIFY_CANCELLED_ENABLED", "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._context_retry_attempts: int = 4
        self._context_retry_sleep_sec: float = 0.02
        self._fill_persist_check_attempts: int = 3
        self._fill_persist_check_sleep_sec: float = 0.02
        self._post_fill_reconcile_attempts: int = 8
        self._post_fill_reconcile_sleep_sec: float = 0.05
        self._fill_persist_alert_cooldown_sec: float = 60.0
        self._last_fill_persist_alert_ts: float = 0.0
        self._broker_order_id_fallback_recent_sec: float = 300.0
        self._allow_broker_order_id_context_fallback: bool = False
        self.convergence_service = BrokerTruthConvergenceService(
            pg=getattr(safety_kernel, "pg", None),
            position_ledger=getattr(safety_kernel, "position_ledger", None),
        )

        # Track processed fills to avoid duplicates
        self.processed_fills: Set[str] = set()
        self.processed_terminal_statuses: Set[str] = set()
        self._fills_inflight: Dict[str, Dict[str, Any]] = {}
        self._fill_inflight_wait_timeout_sec: float = 2.0
        self._fill_inflight_takeover_sec: float = 10.0
        self._lock = asyncio.Lock()
        self._running = False

        logger.info("FillEventHandler initialized")

    async def start(self):
        """Start the fill event handler"""
        self._running = True

        # Subscribe to IB fill events
        self.adapter.ib.execDetailsEvent += self._on_ib_fill
        self.adapter.ib.orderStatusEvent += self._on_ib_order_status

        # Start background cleanup task
        asyncio.create_task(self._cleanup_processed_fills())

        logger.info("FillEventHandler started")

    async def stop(self):
        """Stop the fill event handler"""
        self._running = False

        # Unsubscribe from IB events
        self.adapter.ib.execDetailsEvent -= self._on_ib_fill
        self.adapter.ib.orderStatusEvent -= self._on_ib_order_status

        logger.info("FillEventHandler stopped")

    def _emit_local_audit(
        self,
        *,
        stage: str,
        severity: str = "info",
        status: Optional[str] = None,
        reason: Optional[str] = None,
        strategy_id: Optional[str] = None,
        strategy_run_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
        symbol: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.audit_writer is None:
            return
        try:
            self.audit_writer.emit(
                domain="exec",
                component="fill_event_handler",
                stage=stage,
                severity=severity,
                strategy_id=strategy_id,
                strategy_run_id=strategy_run_id,
                trigger_id=trigger_id,
                decision_id=decision_id,
                trace_id=trace_id,
                causation_id=causation_id,
                order_id=order_id,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
                symbol=symbol,
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            logger.warning("local_fill_audit_emit_failed", stage=stage, order_id=order_id, error=str(exc))

    def _on_ib_fill(self, trade, fill):
        """
        IB fill event callback (synchronous)

        Args:
            trade: ib_insync Trade object
            fill: ib_insync Fill object
        """
        # Convert to async task
        asyncio.create_task(self._process_fill(trade, fill))

    def _on_ib_order_status(self, trade):
        """IB order-status callback for non-fill terminal lifecycle."""
        asyncio.create_task(self._process_order_status(trade))

    async def _process_order_status(self, trade) -> None:
        if not self._running:
            return
        try:
            order = getattr(trade, "order", None)
            order_status = getattr(trade, "orderStatus", None)
            if order is None or order_status is None:
                return

            order_id = int(getattr(order, "orderId", 0) or 0)
            if order_id <= 0:
                return
            status = str(getattr(order_status, "status", "") or "").strip()
            status_norm = status.replace(" ", "").replace("_", "").upper()
            if status_norm not in {"CANCELLED", "CANCELED", "APICANCELLED", "INACTIVE", "REJECTED"}:
                return

            dedupe_key = f"{order_id}:{status_norm}"
            async with self._lock:
                if dedupe_key in self.processed_terminal_statuses:
                    return
                self.processed_terminal_statuses.add(dedupe_key)

            reason = str(getattr(order_status, "whyHeld", "") or "").strip()
            if not reason:
                reason = f"ib_order_status:{status_norm.lower()}"

            if status_norm == "REJECTED":
                await self.kernel.on_reject(order_id, reason=reason)
            else:
                await self.kernel.on_cancel(order_id, reason=reason)

            try:
                ctx = await self._resolve_order_context_from_status(
                    order_id=order_id,
                    perm_id=getattr(order, "permId", None),
                    order_ref=str(getattr(order, "orderRef", "") or "").strip(),
                )
                if not ctx:
                    await self._quarantine_unresolved_broker_event(
                        event_kind="order_status",
                        reason="unattributed_broker_order_status",
                        symbol=str(getattr(getattr(trade, "contract", None), "symbol", "") or ""),
                        broker_order_id=str(order_id),
                        perm_id=getattr(order, "permId", None),
                        order_ref=str(getattr(order, "orderRef", "") or "").strip(),
                        side=str(getattr(order, "action", "") or "").strip().upper() or None,
                        qty=float(getattr(order, "totalQuantity", 0.0) or 0.0) or None,
                        order_type=str(getattr(order, "orderType", "") or "").strip() or None,
                        payload={
                            "status": status,
                            "why_held": reason,
                            "filled": getattr(order_status, "filled", None),
                            "remaining": getattr(order_status, "remaining", None),
                        },
                    )
            except Exception as exc:
                logger.warning("terminal_status_quarantine_failed", order_id=order_id, error=str(exc))

            self._emit_local_audit(
                stage="terminal_status_processed",
                status=status_norm.lower(),
                broker_order_id=str(order_id),
                perm_id=getattr(order, "permId", None),
                symbol=str(getattr(getattr(trade, "contract", None), "symbol", "") or ""),
                reason=reason,
                payload={
                    "status": status,
                    "filled": getattr(order_status, "filled", None),
                    "remaining": getattr(order_status, "remaining", None),
                    "avg_fill_price": getattr(order_status, "avgFillPrice", None),
                    "order_ref": getattr(order, "orderRef", None),
                },
            )
            await self._publish_terminal_status_notification(
                trade=trade,
                status_norm=status_norm,
                reason=reason,
            )
        except Exception as exc:
            logger.warning("ib_order_status_process_failed", error=str(exc))

    async def _process_fill(self, trade, fill):
        """
        Process fill from IB

        Args:
            trade: ib_insync Trade object
            fill: ib_insync Fill object
        """
        start_time = time.time()
        fills_received_total.inc()
        fill_id = ""
        inflight_owner = False
        owner_token = ""

        try:
            # Extract fill details
            order_id = trade.order.orderId
            symbol = trade.contract.symbol
            try:
                from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract

                mapped = internal_opt_symbol_from_ib_contract(getattr(trade, "contract", None))
                if mapped:
                    symbol = mapped
            except Exception:
                pass
            exec_id = fill.execution.execId
            qty = float(fill.execution.shares)
            price = float(fill.execution.price)
            perm_id_raw = getattr(fill.execution, "permId", None)
            if perm_id_raw is None and hasattr(trade, "order"):
                perm_id_raw = getattr(trade.order, "permId", None)
            perm_id: Optional[int]
            try:
                perm_id = int(perm_id_raw) if perm_id_raw is not None else None
            except Exception:
                perm_id = None
            commission = float(fill.commissionReport.commission) if fill.commissionReport else 0.0
            ts_us = int(fill.time.timestamp() * 1e6) if hasattr(fill, 'time') else int(time.time() * 1e6)
            exchange = fill.execution.exchange if hasattr(fill.execution, 'exchange') else None

            # Generate fill ID
            fill_id = f"{exec_id}:{order_id}"

            while True:
                wait_evt: Optional[asyncio.Event] = None
                async with self._lock:
                    if fill_id in self.processed_fills:
                        fills_duplicate_total.inc()
                        logger.debug("Duplicate fill ignored", fill_id=fill_id)
                        return
                    inflight_slot = self._fills_inflight.get(fill_id)
                    if inflight_slot is None:
                        owner_token = str(uuid.uuid4())
                        owner_evt = asyncio.Event()
                        self._fills_inflight[fill_id] = {
                            "token": owner_token,
                            "evt": owner_evt,
                            "started_mono": asyncio.get_running_loop().time(),
                            "takeover_count": 0,
                        }
                        inflight_owner = True
                        break
                    wait_evt = inflight_slot.get("evt")
                if wait_evt is not None:
                    try:
                        await asyncio.wait_for(wait_evt.wait(), timeout=self._fill_inflight_wait_timeout_sec)
                    except asyncio.TimeoutError:
                        async with self._lock:
                            current = self._fills_inflight.get(fill_id)
                            current_evt = (current or {}).get("evt")
                            started_mono = float((current or {}).get("started_mono") or 0.0)
                            takeover_count = int((current or {}).get("takeover_count") or 0)
                            now_mono = asyncio.get_running_loop().time()
                            inflight_age = (now_mono - started_mono) if started_mono > 0 else 0.0
                            if (
                                current_evt is wait_evt
                                and not wait_evt.is_set()
                                and inflight_age >= self._fill_inflight_takeover_sec
                                and takeover_count == 0
                            ):
                                owner_token = str(uuid.uuid4())
                                owner_evt = asyncio.Event()
                                self._fills_inflight[fill_id] = {
                                    "token": owner_token,
                                    "evt": owner_evt,
                                    "started_mono": now_mono,
                                    "takeover_count": takeover_count + 1,
                                }
                                inflight_owner = True
                                logger.warning(
                                    "fill_inflight_wait_timeout_takeover",
                                    fill_id=fill_id,
                                    wait_timeout_sec=self._fill_inflight_wait_timeout_sec,
                                    inflight_age_sec=round(float(inflight_age), 3),
                                    takeover_sec=self._fill_inflight_takeover_sec,
                                )
                                break

            # Create Fill object
            fill_obj = Fill(
                fill_id=fill_id,
                order_id=order_id,
                order_ref=trade.order.orderRef or "",
                symbol=symbol,
                qty=qty,
                price=price,
                ts_us=ts_us,
                side=self._fill_side_from_trade(trade=trade, fill=fill),
                commission=commission,
                exchange=exchange,
                exec_id=exec_id,
                perm_id=perm_id,
            )

            # Process through kernel
            await self.kernel.on_fill(fill_obj)
            persisted = await self._recover_unpersisted_fill(fill_obj=fill_obj)
            if not persisted:
                convergence = await self.convergence_service.converge_unattributed_fill(
                    fill=fill_obj,
                    side=self._fill_side_from_trade(trade=trade, fill=fill),
                )
                if convergence.ok:
                    persisted = True
                    self._emit_local_audit(
                        stage="unattributed_fill_converged",
                        status="converged",
                        reason=convergence.reason,
                        broker_order_id=str(fill_obj.order_id),
                        perm_id=fill_obj.perm_id,
                        symbol=str(fill_obj.symbol or ""),
                        payload=dict(convergence.metadata or {}),
                    )
                else:
                    logger.warning(
                        "unattributed_fill_convergence_failed",
                        fill_id=fill_obj.fill_id,
                        order_id=fill_obj.order_id,
                        perm_id=fill_obj.perm_id,
                        symbol=fill_obj.symbol,
                        exec_id=fill_obj.exec_id,
                        reason=convergence.reason,
                        metadata=convergence.metadata,
                    )
            if not persisted:
                await self._quarantine_unresolved_broker_event(
                    event_kind="fill",
                    reason="unattributed_broker_fill",
                    symbol=str(fill_obj.symbol or ""),
                    broker_order_id=str(fill_obj.order_id),
                    perm_id=fill_obj.perm_id,
                    order_ref=str(fill_obj.order_ref or ""),
                    qty=float(fill_obj.qty) if fill_obj.qty is not None else None,
                    payload={
                        "exec_id": fill_obj.exec_id,
                        "fill_id": fill_obj.fill_id,
                        "price": fill_obj.price,
                        "exchange": fill_obj.exchange,
                    },
                )
                logger.warning(
                    "fill_left_unprocessed_pending_retry",
                    fill_id=fill_obj.fill_id,
                    order_id=fill_obj.order_id,
                    perm_id=fill_obj.perm_id,
                    symbol=fill_obj.symbol,
                    exec_id=fill_obj.exec_id,
                )
                return
            async with self._lock:
                self.processed_fills.add(fill_id)
            try:
                await self._emit_fill_event(fill_obj=fill_obj)
            except Exception as exc:
                logger.warning(
                    "fill_event_emit_failed_non_blocking",
                    fill_id=fill_obj.fill_id,
                    order_id=fill_obj.order_id,
                    symbol=fill_obj.symbol,
                    error=str(exc),
                )
            try:
                ctx = await self._resolve_order_context(fill_obj=fill_obj)
                await self._emit_close_only_completion_audit(ctx=ctx, fill_obj=fill_obj)
            except Exception as exc:
                logger.warning(
                    "close_only_completion_audit_failed_non_blocking",
                    fill_id=fill_obj.fill_id,
                    order_id=fill_obj.order_id,
                    symbol=fill_obj.symbol,
                    error=str(exc),
                )
            await self._publish_fill_notification(trade=trade, fill=fill, fill_obj=fill_obj)

            fills_processed_total.inc()

            latency = time.time() - start_time
            fill_processing_latency.observe(latency)

            logger.info("Fill processed",
                       fill_id=fill_id,
                       order_id=order_id,
                       symbol=symbol,
                       qty=qty,
                       price=price,
                       latency_ms=latency * 1000)

        except Exception as e:
            fills_error_total.inc()
            logger.error("Fill processing error",
                        order_id=trade.order.orderId if trade else None,
                        error=str(e),
                        exc_info=True)
        finally:
            if inflight_owner and fill_id:
                async with self._lock:
                    slot = self._fills_inflight.get(fill_id)
                    if slot and slot.get("token") == owner_token:
                        evt = slot.get("evt")
                        self._fills_inflight.pop(fill_id, None)
                        if evt is not None:
                            evt.set()

    @staticmethod
    def _fill_side_from_trade(*, trade: Any, fill: Any) -> str:
        for value in (
            getattr(getattr(fill, "execution", None), "side", None),
            getattr(getattr(trade, "order", None), "action", None),
        ):
            raw = str(value or "").strip().upper()
            if raw in {"BOT", "BUY", "BOUGHT"}:
                return "BUY"
            if raw in {"SLD", "SELL", "SOLD"}:
                return "SELL"
        return ""

    async def _publish_fill_notification(self, *, trade: Any, fill: Any, fill_obj: Fill) -> None:
        if self.notification_manager is None:
            return

        try:
            ctx = await self._resolve_order_context(fill_obj=fill_obj)
            side = str(ctx.get("side") or getattr(fill.execution, "side", "") or getattr(trade.order, "action", "") or "").upper()
            order_ref = str(ctx.get("order_ref") or getattr(trade.order, "orderRef", "") or "")
            strategy_id = str(ctx.get("strategy_id") or self._strategy_id_from_order_ref(order_ref) or "unknown")
            symbol = str(ctx.get("symbol") or getattr(trade.contract, "symbol", ""))
            order_type = str(ctx.get("order_type") or getattr(trade.order, "orderType", ""))
            status = str(ctx.get("state") or getattr(getattr(trade, "orderStatus", None), "status", "FILLED")).upper()
            reason = str(ctx.get("reason") or "")
            order_role = self._classify_order_role(reason=reason, order_ref=order_ref, order_type=order_type)
            if self._should_suppress_fill_notification(order_role=order_role, reason=reason):
                return
            persisted = await self._is_fill_persisted(fill_obj=fill_obj)
            if not persisted:
                self._emit_local_audit(
                    stage="fill_notification_skipped_not_persisted",
                    severity="warning",
                    status="skipped",
                    reason="fill_not_persisted_in_core_fill",
                    broker_order_id=str(fill_obj.order_id),
                    perm_id=fill_obj.perm_id,
                    symbol=str(fill_obj.symbol or ""),
                    payload={"exec_id": fill_obj.exec_id, "fill_id": fill_obj.fill_id},
                )
                logger.warning(
                    "fill_notification_skipped_not_persisted",
                    order_id=fill_obj.order_id,
                    perm_id=fill_obj.perm_id,
                    symbol=fill_obj.symbol,
                    exec_id=fill_obj.exec_id,
                )
                return

            qty = float(getattr(fill.execution, "shares", 0.0) or 0.0)
            price = float(getattr(fill.execution, "price", 0.0) or 0.0)
            amount_usd = qty * price if qty > 0 and price > 0 else None
            ts = fill_obj.ts_us / 1e6 if fill_obj.ts_us else time.time()

            event = NotificationEvent(
                event_type="order_fill",
                status=status,
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=order_type,
                decision_id=str(ctx.get("decision_id") or ""),
                ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                order_id=int(getattr(trade.order, "orderId", 0) or 0),
                broker_order_id=str(getattr(trade.order, "orderId", "") or ""),
                perm_id=fill_obj.perm_id,
                order_ref=order_ref,
                order_role=order_role,
                price=price if price > 0 else None,
                amount_usd=amount_usd,
                fill_qty=qty,
                reason=reason,
                metadata={
                    "exec_id": fill_obj.exec_id,
                    "exchange": getattr(fill.execution, "exchange", None),
                },
            )
            published = bool(self.notification_manager.publish(event))
            if not published:
                logger.warning(
                    "fill_notification_publish_rejected",
                    order_id=fill_obj.order_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    event_type="order_fill",
                    order_ref=order_ref,
                )
        except Exception as exc:
            logger.warning("fill_notification_publish_failed", error=str(exc), order_id=fill_obj.order_id)

    async def _recover_unpersisted_fill(self, *, fill_obj: Fill) -> bool:
        if await self._is_fill_persisted(fill_obj=fill_obj):
            return True

        attempts = max(0, int(getattr(self, "_post_fill_reconcile_attempts", 0) or 0))
        sleep_sec = max(0.0, float(getattr(self, "_post_fill_reconcile_sleep_sec", 0.0) or 0.0))
        if attempts <= 0 or sleep_sec <= 0.0:
            self._emit_local_audit(
                stage="fill_persist_missing_after_initial_kernel",
                severity="warning",
                status="missing",
                reason="fill_not_persisted_after_kernel",
                broker_order_id=str(fill_obj.order_id),
                perm_id=fill_obj.perm_id,
                symbol=str(fill_obj.symbol or ""),
                payload={"exec_id": fill_obj.exec_id, "fill_id": fill_obj.fill_id},
            )
            return False

        for attempt in range(1, attempts + 1):
            await asyncio.sleep(sleep_sec)
            if await self._is_fill_persisted(fill_obj=fill_obj):
                self._emit_local_audit(
                    stage="fill_persist_recovered_after_retry",
                    status="recovered",
                    reason="fill_row_observed_after_delay",
                    broker_order_id=str(fill_obj.order_id),
                    perm_id=fill_obj.perm_id,
                    symbol=str(fill_obj.symbol or ""),
                    payload={"exec_id": fill_obj.exec_id, "fill_id": fill_obj.fill_id, "attempt": attempt},
                )
                return True
            backfill_fill = getattr(self.kernel, "backfill_fill_after_order_visible", None)
            if callable(backfill_fill):
                try:
                    result = await backfill_fill(fill_obj)
                except Exception as exc:
                    logger.warning(
                        "fill_backfill_helper_failed",
                        order_id=fill_obj.order_id,
                        perm_id=fill_obj.perm_id,
                        exec_id=fill_obj.exec_id,
                        attempt=attempt,
                        error=str(exc),
                    )
                    result = {}
                if bool((result or {}).get("persisted")) and await self._is_fill_persisted(fill_obj=fill_obj):
                    self._emit_local_audit(
                        stage="fill_persist_recovered_after_retry",
                        status="recovered",
                        reason="fill_backfilled_after_order_visible",
                        broker_order_id=str(fill_obj.order_id),
                        perm_id=fill_obj.perm_id,
                        symbol=str(fill_obj.symbol or ""),
                        payload={
                            "exec_id": fill_obj.exec_id,
                            "fill_id": fill_obj.fill_id,
                            "attempt": attempt,
                            "backfill_result": dict(result or {}),
                        },
                    )
                    return True

        self._emit_local_audit(
            stage="fill_persist_missing_after_retry",
            severity="error",
            status="missing",
            reason="fill_not_persisted_after_retries",
            broker_order_id=str(fill_obj.order_id),
            perm_id=fill_obj.perm_id,
            symbol=str(fill_obj.symbol or ""),
            payload={
                "exec_id": fill_obj.exec_id,
                "fill_id": fill_obj.fill_id,
                "attempts": attempts,
            },
        )
        return False

    async def _quarantine_unresolved_broker_event(
        self,
        *,
        event_kind: str,
        reason: str,
        symbol: str,
        broker_order_id: Optional[str],
        perm_id: Optional[int],
        order_ref: str = "",
        side: Optional[str] = None,
        qty: Optional[float] = None,
        order_type: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        pg = getattr(self.kernel, "pg", None)
        if pg is None:
            return
        symbol_u = str(symbol or "").strip().upper() or "UNKNOWN"
        broker_order_id_v = str(broker_order_id or "").strip() or None
        try:
            perm_id_v = int(perm_id) if perm_id is not None else None
        except Exception:
            perm_id_v = None
        order_ref_v = str(order_ref or "").strip() or None
        side_v = str(side or "").strip().upper() or None
        qty_v = float(qty) if qty is not None else None
        order_type_v = str(order_type or "").strip() or None
        note_payload: Dict[str, Any] = dict(payload or {})
        note_payload.update(
            {
                "source": "fill_event_handler",
                "event_kind": str(event_kind or "").strip() or "unknown",
                "reason": str(reason or "").strip() or "unknown",
                "broker_order_id": broker_order_id_v,
                "perm_id": perm_id_v,
                "order_ref": order_ref_v,
                "side": side_v,
                "qty": qty_v,
                "order_type": order_type_v,
            }
        )
        await pg.execute(
            """
            WITH updated AS (
                UPDATE core.core_order_quarantine
                SET symbol = $1,
                    order_ref = COALESCE($4, order_ref),
                    reason = $7,
                    note = $8,
                    updated_at = now(),
                    observed_at = now()
                WHERE status = 'active'
                  AND (
                        ($2::text IS NOT NULL AND broker_order_id = $2::text)
                     OR ($3::bigint IS NOT NULL AND (ib_perm_id = $3::bigint OR perm_id = $3::bigint))
                  )
                RETURNING quarantine_id
            )
            INSERT INTO core.core_order_quarantine (
                symbol, broker_order_id, perm_id, ib_perm_id, order_ref, reason, status, note
            )
            SELECT $1, $2, $3, $3, $4, $7, 'active', $8
            WHERE NOT EXISTS (SELECT 1 FROM updated)
              AND (
                    $2::text IS NOT NULL
                 OR $3::bigint IS NOT NULL
              )
            """,
            symbol_u,
            broker_order_id_v,
            perm_id_v,
            order_ref_v,
            side_v,
            qty_v,
            str(reason or "").strip() or "unknown",
            json.dumps(note_payload, ensure_ascii=False),
        )
        self._emit_local_audit(
            stage="unresolved_broker_event_quarantined",
            severity="error",
            status="quarantined",
            reason=str(reason or "").strip() or "unknown",
            broker_order_id=broker_order_id_v,
            perm_id=perm_id_v,
            symbol=symbol_u,
            payload={"event_kind": event_kind, "order_ref": order_ref_v, "qty": qty_v, "order_type": order_type_v},
        )

    async def _is_fill_persisted(self, *, fill_obj: Fill) -> bool:
        pg = getattr(self.kernel, "pg", None)
        if pg is None:
            return False
        exec_id = str(fill_obj.exec_id or "").strip()
        if not exec_id:
            return False
        broker_order_id = str(fill_obj.order_id or "").strip()
        perm_id = int(fill_obj.perm_id) if fill_obj.perm_id is not None else None
        query = """
            SELECT 1
            FROM core.core_fill f
            JOIN core.core_order o ON o.order_id = f.order_id
            WHERE f.exec_id = $1
              AND (
                    ($2 <> '' AND o.broker_order_id = $2)
                 OR ($3::bigint IS NOT NULL AND o.ib_perm_id = $3::bigint)
              )
            LIMIT 1
        """
        attempts = max(1, int(self._fill_persist_check_attempts))
        last_error: Optional[str] = None
        for idx in range(attempts):
            try:
                row = await pg.fetchone(query, exec_id, broker_order_id, perm_id)
                if row:
                    return True
            except Exception as exc:
                last_error = str(exc)
            if idx < attempts - 1:
                await asyncio.sleep(max(0.0, float(self._fill_persist_check_sleep_sec)))
        if last_error:
            self._emit_local_audit(
                stage="fill_persist_check_error",
                severity="error",
                status="degraded",
                reason="fill_persist_check_db_error",
                broker_order_id=broker_order_id,
                perm_id=fill_obj.perm_id,
                symbol=str(fill_obj.symbol or ""),
                payload={"exec_id": exec_id, "error": last_error},
            )
            self._publish_fill_persist_alert(
                fill_obj=fill_obj,
                reason="fill_persist_check_db_error",
                error=last_error,
            )
        return False

    def _publish_fill_persist_alert(self, *, fill_obj: Fill, reason: str, error: str) -> None:
        if self.notification_manager is None:
            return
        now_ts = time.time()
        if (now_ts - float(self._last_fill_persist_alert_ts or 0.0)) < float(self._fill_persist_alert_cooldown_sec):
            return
        self._last_fill_persist_alert_ts = now_ts
        try:
            event = NotificationEvent(
                event_type="reconciliation_alert",
                status="DEGRADED",
                strategy_id="system_reconciler",
                symbol=str(fill_obj.symbol or "MULTI"),
                side="N/A",
                qty=0.0,
                order_type="N/A",
                decision_id="fill_persist_check",
                reason=reason,
                metadata={
                    "component": "fill_event_handler",
                    "severity": "warning",
                    "exec_id": str(fill_obj.exec_id or ""),
                    "broker_order_id": str(fill_obj.order_id or ""),
                    "perm_id": fill_obj.perm_id,
                    "error": str(error or ""),
                },
            )
            self.notification_manager.publish(event)
        except Exception as exc:
            logger.warning("fill_persist_alert_publish_failed", error=str(exc), order_id=fill_obj.order_id)

    async def _resolve_order_context_once(self, *, fill_obj: Fill) -> Dict[str, Any]:
        pg = getattr(self.kernel, "pg", None)
        if pg is None:
            return {}

        async def _query(sql: str, *args) -> Optional[Dict[str, Any]]:
            try:
                row = await pg.fetchone(sql, *args)
                return dict(row) if row else None
            except Exception:
                return None

        if fill_obj.perm_id:
            row = await _query(
                """
                SELECT order_id::text AS order_id, strategy_id, symbol, side, order_type, state, order_ref, note
                FROM core.core_order
                WHERE ib_perm_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                int(fill_obj.perm_id),
            )
            if row:
                return self._augment_order_context(
                    row,
                    attribution={"path": "perm_id", "matched": True, "fallback_used": False, "ambiguous": False},
                )

        if fill_obj.order_ref:
            row = await _query(
                """
                SELECT order_id::text AS order_id, strategy_id, symbol, side, order_type, state, order_ref, note
                FROM core.core_order
                WHERE order_ref = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(fill_obj.order_ref),
            )
            if row:
                return self._augment_order_context(
                    row,
                    attribution={"path": "order_ref", "matched": True, "fallback_used": False, "ambiguous": False},
                )

        if not bool(getattr(self, "_allow_broker_order_id_context_fallback", False)):
            return self._augment_order_context(
                {},
                attribution={
                    "path": "broker_order_id",
                    "matched": False,
                    "fallback_used": False,
                    "fallback_disabled": True,
                    "ambiguous": False,
                    "candidate_count": 0,
                },
            )

        row = await _query(
            """
            SELECT order_id::text AS order_id, strategy_id, symbol, side, order_type, state, order_ref, note,
                   created_at, updated_at,
                   COUNT(*) OVER() AS candidate_count
            FROM core.core_order
            WHERE broker_order_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            str(fill_obj.order_id),
        )
        candidate_count = 0
        if row:
            try:
                candidate_count = int(row.get("candidate_count") or 0)
            except Exception:
                candidate_count = 0
            if not self._is_recent_broker_order_fallback_candidate(row):
                return self._augment_order_context(
                    {},
                    attribution={
                        "path": "broker_order_id",
                        "matched": False,
                        "fallback_used": True,
                        "ambiguous": candidate_count > 1,
                        "candidate_count": candidate_count,
                        "stale_candidate_ignored": True,
                    },
                )
        return self._augment_order_context(
            row or {},
            attribution={
                "path": "broker_order_id",
                "matched": bool(row),
                "fallback_used": True,
                "ambiguous": candidate_count > 1,
                "candidate_count": candidate_count,
            },
        )

    async def _resolve_order_context(self, *, fill_obj: Fill) -> Dict[str, Any]:
        ctx = await self._resolve_order_context_once(fill_obj=fill_obj)
        attribution = dict(ctx.get("attribution") or {})
        matched = bool(attribution.get("matched"))
        if matched:
            attribution["retry_count"] = 0
            attribution["retry_used"] = False
            ctx["attribution"] = attribution
            return ctx

        retry_attempts = max(0, int(getattr(self, "_context_retry_attempts", 0) or 0))
        retry_sleep_sec = max(0.0, float(getattr(self, "_context_retry_sleep_sec", 0.0) or 0.0))
        has_lookup_key = bool(fill_obj.perm_id or fill_obj.order_ref or fill_obj.order_id)
        if not has_lookup_key or retry_attempts <= 0 or retry_sleep_sec <= 0.0:
            attribution["retry_count"] = 0
            attribution["retry_used"] = False
            ctx["attribution"] = attribution
            return ctx

        for attempt in range(1, retry_attempts + 1):
            await asyncio.sleep(retry_sleep_sec)
            retried_ctx = await self._resolve_order_context_once(fill_obj=fill_obj)
            retried_attr = dict(retried_ctx.get("attribution") or {})
            retried_attr["retry_count"] = attempt
            retried_attr["retry_used"] = True
            retried_ctx["attribution"] = retried_attr
            if bool(retried_attr.get("matched")):
                return retried_ctx
            ctx = retried_ctx

        return ctx

    async def _resolve_order_context_from_status(
        self,
        *,
        order_id: int,
        perm_id: Optional[int],
        order_ref: str,
    ) -> Dict[str, Any]:
        pg = getattr(self.kernel, "pg", None)
        if pg is None:
            return {}

        async def _query(sql: str, *args) -> Optional[Dict[str, Any]]:
            try:
                row = await pg.fetchone(sql, *args)
                return dict(row) if row else None
            except Exception:
                return None

        base_sql = """
            SELECT order_id::text AS order_id, strategy_id, symbol, side, order_type, state, order_ref, note,
                   created_at, updated_at
            FROM core.core_order
            WHERE {where_clause}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
        """

        if perm_id:
            row = await _query(base_sql.format(where_clause="ib_perm_id = $1"), int(perm_id))
            if row:
                return self._augment_order_context(
                    row,
                    attribution={"path": "perm_id", "matched": True, "fallback_used": False, "ambiguous": False},
                )

        if order_ref:
            row = await _query(base_sql.format(where_clause="order_ref = $1"), str(order_ref))
            if row:
                return self._augment_order_context(
                    row,
                    attribution={"path": "order_ref", "matched": True, "fallback_used": False, "ambiguous": False},
                )

        if not bool(getattr(self, "_allow_broker_order_id_context_fallback", False)):
            return {}

        if order_id > 0:
            row = await _query(base_sql.format(where_clause="broker_order_id = $1"), str(order_id))
            if row and self._is_recent_broker_order_fallback_candidate(row):
                return self._augment_order_context(
                    row,
                    attribution={"path": "broker_order_id", "matched": True, "fallback_used": True, "ambiguous": False},
                )
        return {}

    def _is_recent_broker_order_fallback_candidate(self, row: Dict[str, Any]) -> bool:
        recent_sec = max(1.0, float(getattr(self, "_broker_order_id_fallback_recent_sec", 300.0) or 300.0))
        candidate_dt = self._coerce_candidate_datetime(
            row.get("updated_at"),
            row.get("created_at"),
        )
        if candidate_dt is None:
            return True
        now_utc = datetime.now(timezone.utc)
        return candidate_dt >= (now_utc - timedelta(seconds=recent_sec))

    @staticmethod
    def _coerce_candidate_datetime(*values: Any) -> Optional[datetime]:
        for value in values:
            if value is None:
                continue
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    return value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
        return None

    async def _publish_terminal_status_notification(
        self,
        *,
        trade: Any,
        status_norm: str,
        reason: str,
    ) -> None:
        if not self._cancel_notification_enabled or self.notification_manager is None:
            return
        if status_norm not in {"CANCELLED", "CANCELED", "APICANCELLED", "INACTIVE"}:
            return

        try:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            order_status = getattr(trade, "orderStatus", None)
            if order is None:
                return
            order_id = int(getattr(order, "orderId", 0) or 0)
            raw_perm_id = getattr(order, "permId", None)
            perm_id = int(raw_perm_id) if raw_perm_id not in (None, "") else None
            order_ref = str(getattr(order, "orderRef", "") or "").strip()
            ctx = await self._resolve_order_context_from_status(
                order_id=order_id,
                perm_id=perm_id,
                order_ref=order_ref,
            )
            side = str(ctx.get("side") or getattr(order, "action", "") or "").upper()
            symbol = str(ctx.get("symbol") or getattr(contract, "symbol", "") or "")
            order_type = str(ctx.get("order_type") or getattr(order, "orderType", "") or "")
            strategy_id = str(ctx.get("strategy_id") or self._strategy_id_from_order_ref(order_ref) or "unknown")
            resolved_order_ref = str(ctx.get("order_ref") or order_ref or "")
            resolved_reason = str(ctx.get("reason") or reason or "")
            order_role = self._classify_order_role(
                reason=resolved_reason,
                order_ref=resolved_order_ref,
                order_type=order_type,
            )
            qty = float(getattr(order, "totalQuantity", 0.0) or 0.0)
            if qty <= 0 and order_status is not None:
                qty = float(getattr(order_status, "filled", 0.0) or 0.0)
            event = NotificationEvent(
                event_type="order_cancelled",
                status=status_norm,
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=order_type,
                decision_id=str(ctx.get("decision_id") or ""),
                order_id=order_id if order_id > 0 else None,
                broker_order_id=str(order_id) if order_id > 0 else None,
                perm_id=perm_id,
                order_ref=resolved_order_ref,
                order_role=order_role,
                reason=resolved_reason,
                metadata={
                    "filled": float(getattr(order_status, "filled", 0.0) or 0.0) if order_status is not None else 0.0,
                    "remaining": float(getattr(order_status, "remaining", 0.0) or 0.0) if order_status is not None else 0.0,
                    "why_held": str(getattr(order_status, "whyHeld", "") or "") if order_status is not None else "",
                },
            )
            self.notification_manager.publish(event)
        except Exception as exc:
            logger.warning(
                "terminal_status_notification_publish_failed",
                error=str(exc),
                order_id=getattr(getattr(trade, "order", None), "orderId", None),
            )

    @staticmethod
    def _augment_order_context(row: Dict[str, Any], attribution: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        note = row.get("note")
        reason = ""
        decision_id = ""
        trace: Dict[str, Any] = {}
        extras: Dict[str, Any] = {}
        if isinstance(note, str) and note.strip():
            try:
                data = json.loads(note)
                if isinstance(data, dict):
                    decision_id = str(data.get("decision_id") or "")
                    t = data.get("trace") or {}
                    if isinstance(t, dict):
                        trace = t
                    extras = data.get("extras") or {}
                    if isinstance(extras, dict):
                        reason = str(extras.get("reason") or "")
                    if not reason:
                        reason = str(data.get("reason") or "")
            except Exception:
                pass
        if not reason:
            reason = FillEventHandler._infer_reason_from_context(
                extras=extras if isinstance(extras, dict) else {},
                order_ref=str(row.get("order_ref") or ""),
                order_type=str(row.get("order_type") or ""),
            )
        row["reason"] = reason
        row["decision_id"] = decision_id
        row["trace"] = trace
        row["extras"] = dict(extras or {})
        row["attribution"] = dict(attribution or {})
        return row

    async def _emit_close_only_completion_audit(self, *, ctx: Dict[str, Any], fill_obj: Fill) -> None:
        extras = dict(ctx.get("extras") or {})
        if not bool(extras.get("flatten_action") or extras.get("close_only") or extras.get("protective_stop")):
            return
        pg = getattr(self.kernel, "pg", None)
        if pg is None:
            return
        strategy_id = str(ctx.get("strategy_id") or "").strip()
        symbol = str(ctx.get("symbol") or "").strip()
        if not strategy_id or not symbol:
            return
        try:
            completion = await self.flatten_controller.probe_completion(
                strategy_id=strategy_id,
                symbol=symbol,
            )
            pos_qty = float(completion.get("pos_qty") or 0.0)
            open_count = int(completion.get("open_order_count") or 0)
            is_flat = bool(completion.get("is_flat"))
        except Exception:
            return
        is_flatten = bool(extras.get("flatten_action"))
        stage = "flatten_completion_confirmed" if (is_flat and is_flatten) else (
            "flatten_completion_pending" if is_flatten else (
                "close_only_completion_confirmed" if is_flat else "close_only_completion_pending"
            )
        )
        status = "confirmed" if is_flat else "pending"
        reason = "flat_and_no_open_orders" if is_flat else "still_open_position_or_orders"
        trace = ctx.get("trace") if isinstance(ctx.get("trace"), dict) else {}
        self._emit_local_audit(
            stage=stage,
            severity="info" if is_flat else "warning",
            status=status,
            reason=reason,
            strategy_id=strategy_id,
            strategy_run_id=str(trace.get("strategy_run_id") or "") or None,
            trigger_id=str(trace.get("trigger_id") or "") or None,
            decision_id=str(ctx.get("decision_id") or "") or None,
            trace_id=str(trace.get("trace_id") or "") or None,
            causation_id=str(trace.get("causation_id") or "") or None,
            order_id=str(ctx.get("order_id") or "") or None,
            broker_order_id=str(fill_obj.order_id),
            perm_id=fill_obj.perm_id,
            symbol=symbol,
            payload={
                "pos_qty": float(pos_qty),
                "open_order_count": int(open_count),
                "flatten_action": bool(extras.get("flatten_action")),
                "protective_stop": bool(extras.get("protective_stop")),
                "close_only": bool(extras.get("close_only")),
                "position_source": completion.get("position_source"),
                "open_orders_source": completion.get("open_orders_source"),
            },
        )

    @staticmethod
    def _infer_reason_from_context(*, extras: Dict[str, Any], order_ref: str, order_type: str) -> str:
        ex = dict(extras or {})
        raw_action = str(ex.get("intent_action") or "").strip().lower()
        if raw_action:
            return raw_action
        if bool(ex.get("flatten_action")):
            return "flatten"
        if bool(ex.get("protective_stop")):
            return "protective_stop"
        if bool(ex.get("close_only")):
            return "exit"

        ref = str(order_ref or "").strip().upper()
        otype = str(order_type or "").strip().upper()
        if ":FLATTEN" in ref:
            return "flatten"
        if ":TP" in ref:
            return "take_profit"
        if ":STOP" in ref or otype in {"STP", "STP_LMT", "STOP", "STOP_LIMIT"}:
            return "stop_loss"
        if ":ENTRY" in ref:
            return "entry"
        return ""

    async def _emit_fill_event(self, *, fill_obj: Fill) -> None:
        pg = getattr(self.kernel, "pg", None)
        if pg is None:
            return
        ctx = await self._resolve_order_context(fill_obj=fill_obj)
        trace = ctx.get("trace") if isinstance(ctx.get("trace"), dict) else {}

        strategy_id = str(ctx.get("strategy_id") or "unknown")
        decision_id = str(ctx.get("decision_id") or "")
        trace_id = str(trace.get("trace_id") or "")
        strategy_run_id = str(trace.get("strategy_run_id") or "")
        trigger_id = str(trace.get("trigger_id") or "")
        causation_id = str(trace.get("causation_id") or "") or None
        attribution = dict(ctx.get("attribution") or {})
        if not trace_id:
            trace_id = f"fallback:{fill_obj.symbol}:{fill_obj.order_id}:{fill_obj.exec_id}"

        attr_path = str(attribution.get("path") or "unknown")
        fallback_used = bool(attribution.get("fallback_used", False))
        ambiguous = bool(attribution.get("ambiguous", False))
        candidate_count = int(attribution.get("candidate_count") or 0)
        reason = str(ctx.get("reason") or "")
        if not decision_id or not reason:
            self._emit_local_audit(
                stage="fill_context_missing_fields",
                severity="warning",
                status="partial",
                reason=f"fill_context_missing:{attr_path}",
                strategy_id=strategy_id,
                strategy_run_id=strategy_run_id or None,
                trigger_id=trigger_id or None,
                decision_id=decision_id or None,
                trace_id=trace_id,
                causation_id=causation_id,
                order_id=str(ctx.get("order_id") or "") or None,
                broker_order_id=str(fill_obj.order_id),
                perm_id=fill_obj.perm_id,
                symbol=str(fill_obj.symbol or ""),
                payload={
                    "missing_reason": not bool(reason),
                    "missing_decision_id": not bool(decision_id),
                    "order_ref": fill_obj.order_ref or None,
                    "candidate_count": candidate_count,
                    "fallback_used": fallback_used,
                    "ambiguous": ambiguous,
                },
            )
        if ambiguous:
            self._emit_local_audit(
                stage="fill_attribution_ambiguous",
                severity="warning",
                status="ambiguous",
                reason=f"fill_attribution:{attr_path}",
                strategy_id=strategy_id,
                strategy_run_id=strategy_run_id or None,
                trigger_id=trigger_id or None,
                decision_id=decision_id or None,
                trace_id=trace_id,
                causation_id=causation_id,
                order_id=str(ctx.get("order_id") or "") or None,
                broker_order_id=str(fill_obj.order_id),
                perm_id=fill_obj.perm_id,
                symbol=str(fill_obj.symbol or ""),
                payload={"candidate_count": candidate_count, "order_ref": fill_obj.order_ref or None},
            )
        elif fallback_used:
            self._emit_local_audit(
                stage="fill_attribution_fallback_broker_order_id",
                severity="warning",
                status="resolved",
                reason="fill_attribution:broker_order_id",
                strategy_id=strategy_id,
                strategy_run_id=strategy_run_id or None,
                trigger_id=trigger_id or None,
                decision_id=decision_id or None,
                trace_id=trace_id,
                causation_id=causation_id,
                order_id=str(ctx.get("order_id") or "") or None,
                broker_order_id=str(fill_obj.order_id),
                perm_id=fill_obj.perm_id,
                symbol=str(fill_obj.symbol or ""),
                payload={"candidate_count": candidate_count, "order_ref": fill_obj.order_ref or None},
            )
        else:
            self._emit_local_audit(
                stage="fill_attribution_resolved",
                status="resolved",
                reason=f"fill_attribution:{attr_path}",
                strategy_id=strategy_id,
                strategy_run_id=strategy_run_id or None,
                trigger_id=trigger_id or None,
                decision_id=decision_id or None,
                trace_id=trace_id,
                causation_id=causation_id,
                order_id=str(ctx.get("order_id") or "") or None,
                broker_order_id=str(fill_obj.order_id),
                perm_id=fill_obj.perm_id,
                symbol=str(fill_obj.symbol or ""),
                payload={"order_ref": fill_obj.order_ref or None},
            )

        timeout_sec = 0.2
        await asyncio.wait_for(
            emit_order_event(
                pg=pg,
                enabled=self.order_event_enabled,
                event_type="fill_received",
                trace_id=trace_id,
                causation_id=causation_id,
                strategy_id=strategy_id,
                strategy_run_id=strategy_run_id or "unknown",
                trigger_id=trigger_id or None,
                decision_id=decision_id or "unknown",
                intent_id=None,
                order_id=str(ctx.get("order_id") or "") or None,
                broker_order_id=str(fill_obj.order_id),
                symbol=str(fill_obj.symbol or ""),
                payload={
                    "fill_id": fill_obj.fill_id,
                    "qty": fill_obj.qty,
                    "price": fill_obj.price,
                    "commission": fill_obj.commission,
                    "exec_id": fill_obj.exec_id,
                    "perm_id": fill_obj.perm_id,
                    "trace_fallback": "trace" not in ctx or not isinstance(ctx.get("trace"), dict) or not bool(ctx.get("trace", {}).get("trace_id")),
                },
            ),
            timeout=timeout_sec,
        )
        if self.audit_writer is not None:
            try:
                self.audit_writer.emit(
                    domain="exec",
                    component="fill_event_handler",
                    stage="fill_received",
                    strategy_id=strategy_id,
                    strategy_run_id=strategy_run_id or "unknown",
                    trigger_id=trigger_id or None,
                    decision_id=decision_id or "unknown",
                    trace_id=trace_id,
                    causation_id=causation_id,
                    order_id=str(ctx.get("order_id") or "") or None,
                    broker_order_id=str(fill_obj.order_id),
                    perm_id=fill_obj.perm_id,
                    symbol=str(fill_obj.symbol or ""),
                    status="received",
                    payload={
                        "fill_id": fill_obj.fill_id,
                        "qty": fill_obj.qty,
                        "price": fill_obj.price,
                        "commission": fill_obj.commission,
                        "exec_id": fill_obj.exec_id,
                        "trace_fallback": "trace" not in ctx or not isinstance(ctx.get("trace"), dict) or not bool(ctx.get("trace", {}).get("trace_id")),
                    },
                )
            except Exception as exc:
                logger.warning("local_fill_audit_emit_failed", order_id=fill_obj.order_id, error=str(exc))

    @staticmethod
    def _classify_order_role(reason: str, order_ref: str, order_type: str) -> str:
        rs = str(reason or "").lower()
        ref = str(order_ref or "").upper()
        otype = str(order_type or "").upper()
        if "flatten" in rs or ":FLATTEN" in ref:
            return "FLATTEN"
        if "take_profit" in rs or ":TP" in ref or ref.startswith("TP:"):
            return "TAKE_PROFIT"
        if "protective_stop" in rs or ":STOP" in ref or ref.startswith("SL:") or otype in {"STP", "STP_LMT", "STOP", "STOP_LIMIT"}:
            return "STOP_LOSS"
        if "entry" in rs or ":ENTRY" in ref:
            return "ENTRY"
        return "ORDER"

    @staticmethod
    def _strategy_id_from_order_ref(order_ref: str) -> str:
        ref = str(order_ref or "").strip()
        if not ref:
            return ""
        parts = ref.split(":")
        # Common refs: qqqv1:<strategy_id>:..., stkv1:<strategy_id>:..., tp:<strategy_id>:...
        if len(parts) >= 2:
            candidate = str(parts[1] or "").strip()
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _should_suppress_fill_notification(*, order_role: str, reason: str) -> bool:
        rs = str(reason or "").strip().lower()
        role = str(order_role or "").strip().upper()
        # Align fill notifications with submit notifications so internal protective
        # stop self-healing does not page mobile clients when the rebuilt stop later fills.
        if rs == "audit_self_heal" and role in {"PROTECTIVE_STOP", "STOP_LOSS"}:
            return True
        return False

    async def _cleanup_processed_fills(self):
        """
        Background task to clean up old processed fill IDs
        Prevents memory leak from infinite accumulation
        """
        while self._running:
            await asyncio.sleep(3600)  # Run every hour

            async with self._lock:
                # Keep only recent fills (last hour is plenty)
                # In production, this should be more sophisticated
                if len(self.processed_fills) > 10000:
                    # Clear oldest half
                    logger.info("Cleaning up processed fills",
                               count=len(self.processed_fills))
                    self.processed_fills.clear()

    def get_stats(self):
        """Get handler statistics"""
        return {
            "processed_fills_count": len(self.processed_fills),
            "running": self._running
        }
