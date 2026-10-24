"""
Runtime Reconciliation Service
Continuously monitors for ghost orders during system operation

Responsibilities:
- Periodic reconciliation (every 30-60 seconds)
- Compare database orders vs broker orders
- Mark ghost orders (in DB but not in broker)
- Alert on discrepancies
- Emit metrics for monitoring

Based on doc/system/1.md specification - runtime component complementing cold startup reconciliation.
"""
import asyncio
import time
import json
import inspect
import hashlib
from typing import Set, Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from prometheus_client import Counter, Gauge, Histogram

from libs.logger import get_logger
from libs.db.postgres import PostgresDB
from libs.obs.audit_writer import LocalAuditWriter
from services.notifications import NotificationEvent, NotificationManager
from services.exec.order_event_writer import emit_order_event
from services.exec.manual_intervention_lock import ManualInterventionLockStore
from services.exec.flatten_controller import FlattenController
from services.ibkr.ib_order_truth_schema import assert_ib_order_truth_schema
from services.production.recovery_incident_service import RecoveryIncidentService
from services.reconciliation.flatten_lifecycle import FlattenLifecycleHandler
from services.reconciliation.order_quarantine import OrderQuarantineHandler
from services.reconciliation.position_sync import PositionSyncHandler
from services.reconciliation.broker_truth_convergence import BrokerTruthConvergenceService
from services.reconciliation.strategy_recovery_policy import (
    RecoveryAction,
    RecoveryPolicyContext,
    StrategyRecoveryPolicyRegistry,
    default_strategy_recovery_policy_registry,
)
from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract, is_internal_opt_symbol

logger = get_logger(__name__)

RECONCILIATION_MODE_REPORT_ONLY = "report_only"
RECONCILIATION_MODE_DISABLED = "disabled"


def _build_adopted_idempotency_key(*, broker_order_id: str, perm_id: Any, order_ref: str) -> str:
    perm = str(perm_id or "").strip()
    if perm and perm not in {"0", "0.0"}:
        return f"adopted:perm:{perm}"
    ref = str(order_ref or "").strip()
    if ref:
        digest = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:16]
        return f"adopted:ref:{digest}"
    return f"adopted:oid:{str(broker_order_id or '').strip()}"


# ============================================================================
# Metrics
# ============================================================================

recon_runs_total = Counter(
    'recon_runtime_runs_total',
    'Runtime reconciliation runs'
)

recon_ghosts_found_total = Counter(
    'recon_ghosts_found_total',
    'Ghost orders found'
)

recon_orphans_found_total = Counter(
    'recon_orphans_found_total',
    'Orphan orders found (IB has, DB missing)'
)

recon_mismatch_total = Counter(
    'recon_mismatches_total',
    'Order state mismatches',
    ['type']
)

recon_startup_blocks_total = Counter(
    'recon_startup_blocks_total',
    'Startup reconciliation hard-gate blocks',
    ['reason']
)

recon_latency_seconds = Histogram(
    'recon_runtime_latency_seconds',
    'Runtime reconciliation latency'
)

recon_db_orders_gauge = Gauge(
    'recon_db_open_orders',
    'Open orders in database'
)

recon_ib_orders_gauge = Gauge(
    'recon_ib_open_orders',
    'Open orders in IB'
)

recon_ib_presubmitted_orders_gauge = Gauge(
    'recon_ib_presubmitted_orders',
    'PreSubmitted open orders in IB'
)


# ============================================================================
# Runtime Reconciler
# ============================================================================

@dataclass
class ReconciliationReport:
    """Reconciliation run report"""
    timestamp: datetime
    db_open_count: int
    ib_open_count: int
    ghosts_found: int
    orphans_found: int
    mismatches: int
    duration_ms: float
    position_mismatches: int = 0


class RuntimeReconciler:
    """
    Runtime Reconciliation Service

    Runs continuously to detect and report broker/DB divergence:
    1. Fetch open orders from database (state not in FILLED/CANCELLED/REJECTED)
    2. Fetch open orders from broker (IB)
    3. Compare by order_id (broker_order_id in DB = orderId in IB)
    4. Ghost detection: order in DB but not in IB → report + audit + alert
    5. Orphan detection: order in IB but not in DB → quarantine + alert
    6. State mismatch detection: order state differs between DB and IB

    Runtime mode:
    - Runs every 30-60 seconds
    - Never auto-cancel/terminalize broker or DB orders
    - Emits audit/metrics and quarantines unresolved broker orphans
    - Alerts on any discrepancies for manual review
    """

    def __init__(
        self,
        pg: PostgresDB,
        ib_adapter,
        manual_intervention_lock_store: Optional[ManualInterventionLockStore] = None,
        flatten_controller: Optional[FlattenController] = None,
        reservation_service: Optional[Any] = None,
        decision_finalizer: Optional[Any] = None,
        interval_seconds: int = 60,
        auto_cleanup_ghosts: bool = True,
        ghost_grace_seconds: int = 90,
        reconciliation_mode: Optional[str] = None,
        position_sync_enabled: bool = False,
        trading_interval_seconds: float = 1.0,
        offhours_interval_seconds: float = 60.0,
        trading_window_start_et: str = "09:30",
        trading_window_end_et: str = "16:00",
        dynamic_interval_enabled: bool = True,
        query_offhours: bool = False,
        alert_cooldown_seconds: int = 300,
        notification_manager: Optional[NotificationManager] = None,
        system_health_controller: Optional[Any] = None,
        order_event_enabled: bool = True,
        audit_writer: Optional[LocalAuditWriter] = None,
        order_truth_service: Optional[Any] = None,
        startup_block_on_ghosts: bool = True,
        startup_block_on_orphans: bool = True,
        startup_block_on_mismatches: bool = True,
        broker_flat_position_convergence_enabled: bool = True,
        flat_protective_auto_cancel_enabled: bool = False,
        strategy_recovery_policy_registry: Optional[StrategyRecoveryPolicyRegistry] = None,
        recovery_order_manager: Optional[Any] = None,
        recovery_incident_service: Optional[RecoveryIncidentService] = None,
        recovery_action_cooldown_seconds: float = 60.0,
        recovery_required_components: Optional[List[str]] = None,
        account_id: Optional[str] = None,
    ):
        """
        Initialize runtime reconciler

        Args:
            pg: PostgreSQL database
            ib_adapter: IB adapter for fetching open orders
            interval_seconds: Reconciliation interval (default 60s)
            auto_cleanup_ghosts: Auto-mark ghosts as CANCELLED (default True)
        """
        self.pg = pg
        self.adapter = ib_adapter
        self.account_id = str(
            account_id
            or getattr(ib_adapter, "account", "")
            or getattr(getattr(ib_adapter, "ib", None), "account", "")
            or ""
        ).strip()
        self.manual_intervention_lock_store = manual_intervention_lock_store
        self.flatten_controller = flatten_controller or FlattenController(
            pg=self.pg,
            manual_intervention_lock_store=self.manual_intervention_lock_store,
        )
        self.reservation = reservation_service
        self.decision_finalizer = decision_finalizer
        self.interval = interval_seconds
        self.trading_interval_seconds = max(0.2, float(trading_interval_seconds))
        self.offhours_interval_seconds = max(1.0, float(offhours_interval_seconds))
        self.dynamic_interval_enabled = bool(dynamic_interval_enabled)
        self.query_offhours = bool(query_offhours)
        self.alert_cooldown_seconds = max(30, int(alert_cooldown_seconds))
        self.notification_manager = notification_manager
        self.system_health_controller = system_health_controller
        self.order_event_enabled = bool(order_event_enabled)
        self.audit_writer = audit_writer
        self.order_truth_service = order_truth_service
        self.broker_flat_position_convergence_enabled = bool(broker_flat_position_convergence_enabled)
        self.flat_protective_auto_cancel_enabled = bool(flat_protective_auto_cancel_enabled)
        self.strategy_recovery_policy_registry = (
            strategy_recovery_policy_registry
            or default_strategy_recovery_policy_registry()
        )
        self.recovery_order_manager = recovery_order_manager
        self.recovery_incident_service = recovery_incident_service or RecoveryIncidentService()
        self.recovery_action_cooldown_seconds = max(0.0, float(recovery_action_cooldown_seconds or 0.0))
        self.recovery_required_components = [
            str(item).strip() for item in (recovery_required_components or []) if str(item).strip()
        ]
        self._recovery_action_suppress_until: Dict[str, float] = {}
        self.startup_block_on_ghosts = bool(startup_block_on_ghosts)
        self.startup_block_on_orphans = bool(startup_block_on_orphans)
        self.startup_block_on_mismatches = bool(startup_block_on_mismatches)
        self._et_tz = ZoneInfo("America/New_York")
        mode_raw = str(reconciliation_mode or "").strip().lower()
        if mode_raw in {"legacy_cleanup", "cleanup", "auto_cleanup"}:
            logger.warning(
                "runtime_reconciler_mode_coerced_report_only",
                requested_mode=mode_raw,
                reason="destructive_runtime_cleanup_removed",
            )
            mode_raw = RECONCILIATION_MODE_REPORT_ONLY
        if mode_raw not in {RECONCILIATION_MODE_REPORT_ONLY, RECONCILIATION_MODE_DISABLED}:
            mode_raw = RECONCILIATION_MODE_REPORT_ONLY
        self.reconciliation_mode = mode_raw
        self.auto_cleanup = False
        self.ghost_grace_seconds = max(0, int(ghost_grace_seconds))
        # IMPORTANT: IBKR positions are account-level. Writing them into strategy-scoped
        # `core.core_position` causes cross-strategy contamination. Keep disabled by default.
        if bool(position_sync_enabled):
            logger.warning(
                "runtime_reconciler_position_sync_forced_disabled",
                reason="account_level_ib_positions_cannot_be_assigned_to_strategy_positions",
            )
        self.position_sync_enabled = False
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._event_reconcile_task: Optional[asyncio.Task] = None
        self._platform_readiness_retry_task: Optional[asyncio.Task] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._recovery_readiness_listener_registered = False
        self._run_lock = asyncio.Lock()
        self._trading_window_start = self._parse_hhmm(trading_window_start_et, fallback=dt_time(9, 30))
        self._trading_window_end = self._parse_hhmm(trading_window_end_et, fallback=dt_time(16, 0))
        self._next_alert_push_ts = 0.0
        # Suppress repeated orphan alerts for broker order IDs right after a cancel attempt.
        self._orphan_suppress_until: Dict[Any, float] = {}
        # Suppress repeated ghost probe warnings for broker order IDs when fresh broker truth
        # already showed the order is still open or the probe was temporarily inconclusive.
        self._ghost_suppress_until: Dict[Any, float] = {}
        self.flatten_incomplete_grace_seconds = 30.0
        self.order_quarantine_handler = OrderQuarantineHandler(self)
        self.flatten_lifecycle_handler = FlattenLifecycleHandler(self)
        self.position_sync_handler = PositionSyncHandler(self)
        self.broker_truth_convergence = BrokerTruthConvergenceService(pg=self.pg, account_id=self.account_id or None)
        self.last_startup_result = None

        # Stats
        self.stats = {
            "runs": 0,
            "ghosts_found": 0,
            "orphans_found": 0,
            "last_run": None,
            "last_duration_ms": 0.0
        }
        from services.reconciliation.contracts import (
            AckReconciler,
            OrderTruthReconciler,
            PositionReconciler,
            StartupReconciler,
        )

        self.startup_reconciler = StartupReconciler(self)
        self.order_truth_reconciler = OrderTruthReconciler(self)
        self.position_reconciler = PositionReconciler(self, account_id=self.account_id or None)
        self.ack_reconciler = AckReconciler(self)

        logger.info("RuntimeReconciler initialized",
                   interval_seconds=interval_seconds,
                   reconciliation_mode=self.reconciliation_mode,
                   ghost_grace_seconds=self.ghost_grace_seconds,
                   decision_finalizer_enabled=bool(self.decision_finalizer is not None),
                   position_sync_enabled=self.position_sync_enabled,
                   trading_interval_seconds=self.trading_interval_seconds,
                   offhours_interval_seconds=self.offhours_interval_seconds,
                   trading_window_start_et=str(self._trading_window_start),
                   trading_window_end_et=str(self._trading_window_end),
                   dynamic_interval_enabled=self.dynamic_interval_enabled,
                   query_offhours=self.query_offhours,
                   alert_cooldown_seconds=self.alert_cooldown_seconds,
                   startup_block_on_ghosts=self.startup_block_on_ghosts,
                   startup_block_on_orphans=self.startup_block_on_orphans,
                   startup_block_on_mismatches=self.startup_block_on_mismatches,
                   flat_protective_auto_cancel_enabled=self.flat_protective_auto_cancel_enabled)

    @staticmethod
    def _parse_hhmm(value: str, fallback: dt_time) -> dt_time:
        raw = str(value or "").strip()
        if not raw:
            return fallback
        try:
            hh, mm = raw.split(":", 1)
            h = int(hh)
            m = int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                return fallback
            return dt_time(h, m)
        except Exception:
            return fallback

    def _in_trading_window(self) -> bool:
        now_et = datetime.now(self._et_tz)
        if now_et.weekday() >= 5:  # Sat/Sun
            return False
        t = now_et.time()
        return self._trading_window_start <= t <= self._trading_window_end

    def _loop_sleep_seconds(self) -> float:
        if not self.dynamic_interval_enabled:
            return max(1.0, float(self.interval))
        return self.trading_interval_seconds if self._in_trading_window() else self.offhours_interval_seconds

    @staticmethod
    def _ib_status_counts(ib_orders: List[Any]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for o in ib_orders or []:
            status = str(getattr(o, "status", "") or "UNKNOWN")
            counts[status] = counts.get(status, 0) + 1
        return counts

    @staticmethod
    def _normalize_broker_order_id(value: Any) -> Optional[Any]:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except Exception:
            return raw

    @staticmethod
    def _normalize_perm_id(value: Any) -> Optional[str]:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        # IB permId <= 0 means "missing/unknown" and must not be used for identity matching.
        try:
            if int(raw) <= 0:
                return None
        except Exception:
            pass
        return raw

    @staticmethod
    def _suppress_key_from_broker_order_id(value: Any) -> Optional[Any]:
        try:
            return RuntimeReconciler._normalize_broker_order_id(value)
        except Exception:
            return None

    @staticmethod
    def _numeric_broker_order_id(value: Any) -> Optional[int]:
        norm = RuntimeReconciler._normalize_broker_order_id(value)
        if isinstance(norm, int):
            return norm
        return None

    @staticmethod
    def _broker_order_id_matches(left: Any, right: Any) -> bool:
        left_norm = RuntimeReconciler._normalize_broker_order_id(left)
        right_norm = RuntimeReconciler._normalize_broker_order_id(right)
        return left_norm is not None and left_norm == right_norm

    @staticmethod
    def _is_order_stale(order: Dict[str, Any], grace_seconds: int) -> bool:
        if grace_seconds <= 0:
            return True
        updated_at = order.get("updated_at")
        if not isinstance(updated_at, datetime):
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_sec = (datetime.now(updated_at.tzinfo) - updated_at).total_seconds()
        return age_sec >= grace_seconds

    async def _probe_ghost_open_fresh(self, ghost: Dict[str, Any]) -> Dict[str, Any]:
        # Runtime reconciler is report-only and must not start extra probe sessions.
        return {
            "checked": False,
            "broker_order_id": self._normalize_broker_order_id(ghost.get("broker_order_id")),
            "found": None,
            "probe_client_id": None,
            "owner_client_id": None,
            "status": None,
            "perm_id": None,
            "why_held": None,
            "error": "runtime_reconciler_probe_disabled",
        }

    async def _confirm_ghost_candidates(self, ghosts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Keep this hook to preserve call-site compatibility while remaining read-only.
        return list(ghosts or [])

    async def start(self):
        """Start the runtime reconciliation loop"""
        if self._running:
            return
        self._running = True
        self._event_loop = asyncio.get_running_loop()
        self._register_recovery_readiness_listener()
        self._loop_task = asyncio.create_task(self._reconciliation_loop(), name="runtime_reconciler_loop")
        logger.info("RuntimeReconciler started")

    async def stop(self):
        """Stop the runtime reconciliation loop"""
        self._running = False
        self._unregister_recovery_readiness_listener()
        for task in (self._event_reconcile_task, self._platform_readiness_retry_task, self._loop_task):
            if task is not None and not task.done():
                task.cancel()
        logger.info("RuntimeReconciler stopped")

    def _register_recovery_readiness_listener(self) -> None:
        if self._recovery_readiness_listener_registered or not self.recovery_required_components:
            return
        try:
            from services.production.recovery_readiness import get_recovery_readiness_service

            get_recovery_readiness_service().add_listener(self._on_recovery_readiness_event)
            self._recovery_readiness_listener_registered = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("runtime_reconciler_recovery_readiness_listener_failed", error=str(exc))

    def _unregister_recovery_readiness_listener(self) -> None:
        if not self._recovery_readiness_listener_registered:
            return
        try:
            from services.production.recovery_readiness import get_recovery_readiness_service

            get_recovery_readiness_service().remove_listener(self._on_recovery_readiness_event)
        except Exception:
            pass
        self._recovery_readiness_listener_registered = False

    def _on_recovery_readiness_event(self, event: Dict[str, Any]) -> None:
        if not self._running:
            return
        component = str((event or {}).get("component") or "").strip()
        if component != "data_availability" and component not in set(self.recovery_required_components):
            return
        ready = bool((event or {}).get("ready"))
        previous_ready = bool((event or {}).get("previous_ready"))
        if ready and previous_ready:
            return
        loop = self._event_loop
        if loop is None or loop.is_closed():
            return
        reason = f"recovery_readiness_{'ready' if ready else 'not_ready'}:{component}"
        try:
            loop.call_soon_threadsafe(self._schedule_event_reconcile, reason)
        except RuntimeError:
            return

    def _schedule_event_reconcile(self, reason: str) -> None:
        if not self._running:
            return
        existing = self._event_reconcile_task
        if existing is not None and not existing.done():
            logger.info("runtime_reconciler_event_reconcile_already_pending", reason=reason)
            return
        self._event_reconcile_task = asyncio.create_task(
            self._run_once_from_health_event(reason=reason),
            name="runtime_reconciler_event_run_once",
        )

    async def on_system_health_event(self, payload: Dict[str, Any]) -> None:
        """Run an immediate broker-truth check after IB API recovery."""
        event_type = str((payload or {}).get("event_type") or "").strip().lower()
        reason = str((payload or {}).get("reason") or "").strip().lower()
        metadata = dict((payload or {}).get("metadata") or {})
        recovery_ready = (
            event_type == "system_recovering"
            and (
                reason in {"ib_runtime_recovery_api_ready", "broker_api_ready_waiting_for_reconciler"}
                or bool(metadata.get("requires_runtime_reconciler"))
            )
        )
        if not recovery_ready or not self._running:
            return
        self._schedule_event_reconcile(reason)

    async def _run_once_from_health_event(self, *, reason: str) -> None:
        try:
            logger.info("runtime_reconciler_event_reconcile_begin", reason=reason)
            async with self._run_lock:
                await self.run_once()
            logger.info("runtime_reconciler_event_reconcile_done", reason=reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("runtime_reconciler_event_reconcile_failed", reason=reason, error=str(exc), exc_info=True)

    def _schedule_platform_readiness_retry(self, *, delay_sec: float = 5.0) -> None:
        if not self._running:
            return
        existing = self._platform_readiness_retry_task
        if existing is not None and not existing.done():
            return
        self._platform_readiness_retry_task = asyncio.create_task(
            self._platform_readiness_retry_after_delay(delay_sec=max(0.5, float(delay_sec))),
            name="runtime_reconciler_platform_readiness_retry",
        )

    async def _platform_readiness_retry_after_delay(self, *, delay_sec: float) -> None:
        try:
            await asyncio.sleep(delay_sec)
            if not self._running:
                return
            controller = getattr(self, "system_health_controller", None)
            state = str(getattr(controller, "state", "") or "").strip().lower()
            if state not in {"recovering", "degraded", "broker_degraded"}:
                return
            await self._run_once_from_health_event(reason="platform_recovery_readiness_retry")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("runtime_reconciler_platform_readiness_retry_failed", error=str(exc), exc_info=True)

    def _raise_startup_gate_failure(self, *, report: "ReconciliationReport") -> None:
        reasons: list[str] = []
        if self.startup_block_on_ghosts and int(report.ghosts_found or 0) > 0:
            reasons.append("ghosts_found")
        if self.startup_block_on_orphans and int(report.orphans_found or 0) > 0:
            reasons.append("orphans_found")
        if self.startup_block_on_mismatches and int(report.mismatches or 0) > 0:
            reasons.append("mismatches_found")
        if self.startup_block_on_mismatches and int(report.position_mismatches or 0) > 0:
            reasons.append("position_mismatches_found")
        if not reasons:
            return
        reason = ",".join(reasons)
        for item in reasons:
            recon_startup_blocks_total.labels(reason=item).inc()
        self._emit_audit(
            stage="startup_reconcile_blocked",
            severity="critical",
            status="blocked",
            reason=reason,
            payload={
                "ghosts_found": int(report.ghosts_found or 0),
                "orphans_found": int(report.orphans_found or 0),
                "mismatches": int(report.mismatches or 0),
                "position_mismatches": int(report.position_mismatches or 0),
                "db_open_count": int(report.db_open_count or 0),
                "ib_open_count": int(report.ib_open_count or 0),
            },
        )
        raise RuntimeError(
            f"runtime_reconciler_startup_blocked:{reason}:"
            f"ghosts={int(report.ghosts_found or 0)}:"
            f"orphans={int(report.orphans_found or 0)}:"
            f"mismatches={int(report.mismatches or 0)}:"
            f"position_mismatches={int(report.position_mismatches or 0)}"
        )

    async def acknowledge_order_quarantine(
        self,
        *,
        actor: str,
        symbol: str,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
        note: Optional[str] = None,
        resolve: bool = True,
    ) -> int:
        return await self.order_quarantine_handler.acknowledge_order_quarantine(
            actor=actor,
            symbol=symbol,
            broker_order_id=broker_order_id,
            perm_id=perm_id,
            note=note,
            resolve=resolve,
        )

    async def ensure_startup_ready(self):
        await assert_ib_order_truth_schema(self.pg, source="runtime_reconciler")
        if self.reconciliation_mode == RECONCILIATION_MODE_DISABLED:
            return None
        result = await self.startup_reconciler.ensure_ready(raise_gate=False)
        result = await self._converge_startup_broker_flat_positions_if_needed(result)
        self._raise_startup_gate_failure(report=self._startup_report_from_result(result))
        self.last_startup_result = result
        return result

    async def _converge_startup_broker_flat_positions_if_needed(self, result: Any) -> Any:
        metadata = getattr(result, "metadata", None) or {}
        position_meta = metadata.get("position") or {}
        try:
            position_mismatches = int(metadata.get("position_mismatches") or position_meta.get("mismatch_count") or 0)
        except Exception:
            position_mismatches = 0
        if position_mismatches <= 0:
            return result

        class _PositionResult:
            def __init__(self, metadata: Dict[str, Any]) -> None:
                self.metadata = metadata

        before = list(position_meta.get("mismatches") or [])
        await self._converge_broker_flat_position_mismatches(position_result=_PositionResult(position_meta))
        refreshed = await self.startup_reconciler.ensure_ready(raise_gate=False)
        refreshed_meta = getattr(refreshed, "metadata", None) or {}
        refreshed_position_meta = refreshed_meta.get("position") or {}
        try:
            refreshed_position_mismatches = int(
                refreshed_meta.get("position_mismatches") or refreshed_position_meta.get("mismatch_count") or 0
            )
        except Exception:
            refreshed_position_mismatches = 0
        logger.info(
            "startup_broker_flat_position_convergence_checked",
            before_position_mismatches=position_mismatches,
            after_position_mismatches=refreshed_position_mismatches,
            symbols=[str((item or {}).get("symbol") or "") for item in before[:20]],
        )
        return refreshed

    def _startup_report_from_result(self, result: Any) -> ReconciliationReport:
        metadata = getattr(result, "metadata", None) or {}
        return ReconciliationReport(
            timestamp=None,
            db_open_count=int((metadata.get("order_truth") or {}).get("db_open_count") or 0),
            ib_open_count=int((metadata.get("order_truth") or {}).get("ib_open_count") or 0),
            ghosts_found=int(metadata.get("ghosts_found") or 0),
            orphans_found=int(metadata.get("orphans_found") or 0),
            mismatches=int(metadata.get("mismatches") or 0),
            duration_ms=0.0,
            position_mismatches=int(metadata.get("position_mismatches") or 0),
        )

    async def _reconciliation_loop(self):
        """Background reconciliation loop"""
        while self._running:
            try:
                if (not self.query_offhours) and (not self._in_trading_window()) and (not await self._has_db_working_orders()):
                    await asyncio.sleep(self.offhours_interval_seconds)
                    continue
                async with self._run_lock:
                    await self.run_once()
            except Exception as e:
                logger.error("Reconciliation loop error", error=str(e), exc_info=True)

            await asyncio.sleep(self._loop_sleep_seconds())

    async def run_once(self) -> ReconciliationReport:
        """
        Run reconciliation once

        Returns:
            ReconciliationReport
        """
        if self.reconciliation_mode == RECONCILIATION_MODE_DISABLED:
            logger.info("runtime_reconciler_run_skipped", reason="mode_disabled")
            return ReconciliationReport(
                timestamp=datetime.now(),
                db_open_count=0,
                ib_open_count=0,
                ghosts_found=0,
                orphans_found=0,
                mismatches=0,
                duration_ms=0.0,
            )
        start_time = time.time()
        recon_runs_total.inc()
        self.stats["runs"] += 1

        logger.debug("Starting runtime reconciliation")

        try:
            order_snapshot = await self.order_truth_reconciler.collect_snapshot(apply_runtime_filters=True)
            db_orders = order_snapshot.db_orders
            ib_orders = order_snapshot.ib_orders
            self._publish_runtime_order_truth(order_snapshot=order_snapshot)
            active_quarantine = await self.order_quarantine_handler.active_quarantine_summary()
            ib_status_counts = self._ib_status_counts(ib_orders)
            pre_submitted_cnt = ib_status_counts.get("PreSubmitted", 0)
            logger.info(
                "runtime_reconciliation_snapshot",
                db_open_count=len(db_orders),
                ib_open_count=len(ib_orders),
                ib_status_counts=ib_status_counts,
                ib_presubmitted_count=pre_submitted_cnt,
            )

            await self.order_quarantine_handler.resolve_cleared_order_quarantines(
                db_by_broker_id=order_snapshot.db_by_broker_id,
                db_by_perm_id=order_snapshot.db_by_perm_id,
                ib_by_id=order_snapshot.ib_by_id,
                ib_by_perm_id=order_snapshot.ib_by_perm_id,
            )

            ghosts = list(order_snapshot.ghosts)
            orphans = list(order_snapshot.orphans)
            mismatches = list(order_snapshot.mismatches)

            # 7. Process ghosts
            if ghosts:
                ghosts = await self._confirm_ghost_candidates(ghosts)
            if ghosts:
                logger.warning("Ghost orders detected",
                              count=len(ghosts),
                              order_ids=[g['order_id'] for g in ghosts])
                self._emit_audit(
                    stage="ghost_detected_report_only",
                    severity="warning",
                    status="detected",
                    reason="report_only_mode",
                    payload={
                        "count": len(ghosts),
                        "order_ids": [str(g.get("order_id") or "") for g in ghosts[:20]],
                    },
                )

                recon_ghosts_found_total.inc(len(ghosts))
                self.stats["ghosts_found"] += len(ghosts)

            # 8. Alert and quarantine orphans (IB has, DB missing)
            alert_orphans = list(orphans)
            if orphans:
                logger.error("Orphan orders detected (IB has, DB missing)",
                            count=len(orphans),
                            order_ids=[o.order_id for o in orphans])
                self._emit_audit(
                    stage="orphan_detected",
                    severity="warning",
                    status="detected",
                    payload={
                        "count": len(orphans),
                        "order_ids": [getattr(o, "order_id", None) for o in orphans[:20]],
                    },
                )

                alert_orphans = await self._quarantine_detected_orphans(list(orphans))
                if alert_orphans is None:
                    alert_orphans = list(orphans)
                self_healed_orphans = max(0, len(orphans) - len(alert_orphans))
                orphan_log = logger.warning if alert_orphans else logger.info
                orphan_log(
                    "runtime_orphan_reconciliation_result",
                    detected_orphans=len(orphans),
                    self_healed_orphans=self_healed_orphans,
                    unresolved_orphans=len(alert_orphans),
                    unresolved_order_ids=[getattr(o, "order_id", None) for o in alert_orphans[:10]],
                )

                recon_orphans_found_total.inc(len(orphans))
                self.stats["orphans_found"] += len(orphans)

            # 9. Alert on mismatches
            if mismatches:
                logger.warning("Order state mismatches detected",
                              count=len(mismatches))
                for db_order, ib_order in mismatches:
                    logger.warning("State mismatch",
                                  order_id=db_order['order_id'],
                                  db_state=db_order['state'],
                                  ib_status=ib_order.status)
                    recon_mismatch_total.labels(type='state').inc()
            await self.order_quarantine_handler.resolve_converged_synthetic_quarantines(
                db_orders=list(db_orders),
                ib_orders=list(ib_orders),
                ghosts=list(ghosts),
                orphans=list(alert_orphans),
                mismatches=list(mismatches),
            )

            if self.flat_protective_auto_cancel_enabled:
                await self._cancel_flat_protective_orders(
                    db_orders=list(db_orders),
                    ib_by_id=dict(order_snapshot.ib_by_id or {}),
                )

            # 9b. Position Audit (Truth Check)
            if self.position_sync_enabled:
                await self._sync_positions_from_ib()
            await self._audit_positions(db_open_orders=db_orders, ib_open_orders=ib_orders)
            position_result = None
            try:
                position_result = await self.position_reconciler.reconcile_once()
                self._publish_runtime_position_truth(position_result=position_result)
                position_mismatch_count = int(position_result.metadata.get("mismatch_count") or 0)
                if position_mismatch_count > 0:
                    logger.warning(
                        "position_ledger_mismatch_detected",
                        mismatch_count=position_mismatch_count,
                        mismatches=position_result.metadata.get("mismatches", [])[:10],
                    )
                    await self._converge_broker_flat_position_mismatches(position_result=position_result)
                    actions = self._evaluate_strategy_recovery_policy(position_result=position_result)
                    await self._execute_strategy_recovery_actions(actions)
                    recon_mismatch_total.labels(type="position").inc(position_mismatch_count)
                cleared_position_quarantines = await self.order_quarantine_handler.resolve_cleared_position_quarantines(
                    position_metadata=dict(position_result.metadata or {}),
                    db_orders=list(db_orders),
                    ib_orders=list(ib_orders),
                )
                if cleared_position_quarantines:
                    active_quarantine = await self.order_quarantine_handler.active_quarantine_summary()
            except Exception as exc:
                position_mismatch_count = 0
                logger.warning("position_ledger_reconcile_failed", error=str(exc), exc_info=True)
            if ghosts and position_result is not None:
                remaining_ghosts = await self._terminalize_flat_position_protection_ghosts(
                    ghosts=ghosts,
                    position_metadata=dict(position_result.metadata or {}),
                )
                if len(remaining_ghosts) != len(ghosts):
                    ghosts = remaining_ghosts
                    order_snapshot = await self.order_truth_reconciler.collect_snapshot(apply_runtime_filters=True)
                    db_orders = order_snapshot.db_orders
                    ib_orders = order_snapshot.ib_orders
            await self._audit_flatten_lifecycle(db_orders=db_orders)
            self._emit_audit(
                stage="position_audit_snapshot",
                status="ok",
                payload={
                    "db_open_count": len(db_orders),
                    "ib_open_count": len(ib_orders),
                    "ib_pre_submitted": pre_submitted_cnt,
                    "ghosts": len(ghosts),
                    "orphans_detected": len(orphans),
                    "orphans_unresolved": len(alert_orphans),
                    "mismatches": len(mismatches),
                    "position_mismatches": position_mismatch_count,
                    "active_quarantine_count": int(active_quarantine.get("active_count") or 0),
                    "position_reconciler": position_result.metadata if position_result is not None else None,
                },
            )

            # 10. Update metrics
            recon_db_orders_gauge.set(len(db_orders))
            recon_ib_orders_gauge.set(len(ib_orders))
            recon_ib_presubmitted_orders_gauge.set(pre_submitted_cnt)

            duration_ms = (time.time() - start_time) * 1000
            recon_latency_seconds.observe(duration_ms / 1000)

            self.stats["last_run"] = datetime.now()
            self.stats["last_duration_ms"] = duration_ms

            # Normal case is no discrepancies; log at debug to avoid noisy info logs.
            logger.debug(
                "Runtime reconciliation complete",
                db_orders=len(db_orders),
                ib_orders=len(ib_orders),
                ib_pre_submitted=pre_submitted_cnt,
                ib_status_counts=ib_status_counts,
                ghosts=len(ghosts),
                orphans=len(orphans),
                mismatches=len(mismatches),
                position_mismatches=position_mismatch_count,
                duration_ms=round(duration_ms, 1),
            )
            await self._maybe_push_alert(
                db_open_count=len(db_orders),
                ib_open_count=len(ib_orders),
                ib_pre_submitted=pre_submitted_cnt,
                ghosts=len(ghosts),
                orphans=len(alert_orphans),
                orphans_detected=len(orphans),
                orphans_self_healed=max(0, len(orphans) - len(alert_orphans)),
                mismatches=len(mismatches),
                active_quarantine_count=int(active_quarantine.get("active_count") or 0),
                evidence=self._build_alert_evidence(
                    ghosts=ghosts,
                    orphans_detected=orphans,
                    orphans_unresolved=alert_orphans,
                    mismatches=mismatches,
                    ib_status_counts=ib_status_counts,
                    active_quarantine=active_quarantine,
                ),
            )
            await self._publish_runtime_health_if_needed(
                ghosts=len(ghosts),
                unresolved_orphans=len(alert_orphans),
                mismatches=len(mismatches),
                position_mismatches=position_mismatch_count,
                active_quarantine_count=int(active_quarantine.get("active_count") or 0),
            )

            return ReconciliationReport(
                timestamp=datetime.now(),
                db_open_count=len(db_orders),
                ib_open_count=len(ib_orders),
                ghosts_found=len(ghosts),
                orphans_found=len(orphans),
                mismatches=len(mismatches),
                duration_ms=duration_ms,
                position_mismatches=position_mismatch_count,
            )

        except Exception as e:
            logger.error("Reconciliation failed", error=str(e), exc_info=True)
            raise

    def _publish_runtime_order_truth(self, *, order_snapshot: Any) -> None:
        truth = getattr(self, "order_truth_service", None)
        if truth is None:
            return
        publish = getattr(truth, "publish_reconciled_open_orders", None)
        if not callable(publish):
            return
        try:
            publish(
                db_orders=list(getattr(order_snapshot, "db_orders", []) or []),
                ib_orders=list(getattr(order_snapshot, "ib_orders", []) or []),
                published_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.warning("runtime_reconciler_publish_order_truth_failed", error=str(exc))

    def _publish_runtime_position_truth(self, *, position_result: Any) -> None:
        truth = getattr(self, "order_truth_service", None)
        if truth is None:
            return
        publish = getattr(truth, "publish_reconciled_positions", None)
        if not callable(publish):
            return
        metadata = getattr(position_result, "metadata", None) or {}
        try:
            publish(
                ib_positions=dict(metadata.get("ib_positions") or {}),
                ledger_positions=dict(metadata.get("ledger_positions") or {}),
                mismatches=list(metadata.get("mismatches") or []),
                published_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.warning("runtime_reconciler_publish_position_truth_failed", error=str(exc))

    async def _publish_runtime_health_if_needed(
        self,
        *,
        ghosts: int,
        unresolved_orphans: int,
        mismatches: int,
        position_mismatches: int,
        active_quarantine_count: int,
    ) -> None:
        controller = getattr(self, "system_health_controller", None)
        if controller is None:
            return
        clean = (
            ghosts <= 0
            and unresolved_orphans <= 0
            and mismatches <= 0
            and position_mismatches <= 0
            and active_quarantine_count <= 0
        )
        try:
            from services.production.recovery_readiness import get_recovery_readiness_service

            recovery_readiness = get_recovery_readiness_service().snapshot(
                required_components=self.recovery_required_components
            )
        except Exception as exc:  # noqa: BLE001
            recovery_readiness = {
                "ready": False,
                "blockers": ["recovery_readiness_snapshot_failed"],
                "error": str(exc),
            }
        platform_ready = bool(recovery_readiness.get("ready"))
        metadata = {
            "ghosts": int(ghosts),
            "unresolved_orphans": int(unresolved_orphans),
            "mismatches": int(mismatches),
            "position_mismatches": int(position_mismatches),
            "active_quarantine_count": int(active_quarantine_count),
            "broker_connected": True,
            "broker_truth_complete": clean,
            "order_truth_clean": ghosts <= 0 and unresolved_orphans <= 0 and mismatches <= 0,
            "position_ledger_clean": position_mismatches <= 0,
            "strategies_paused_ack": True,
            "runtime_reconciler_clean": clean,
            "recovery_readiness": recovery_readiness,
            "platform_recovery_ready": platform_ready,
        }
        if ghosts <= 0 and unresolved_orphans <= 0 and mismatches <= 0 and position_mismatches <= 0 and active_quarantine_count <= 0:
            state = str(getattr(controller, "state", "") or "").strip().lower()
            publish = getattr(controller, "publish", None)
            if not callable(publish):
                return
            if not platform_ready:
                if state == "running":
                    await publish(
                        event_type="system_degraded",
                        reason="platform_recovery_readiness_stale",
                        source="runtime_reconciler",
                        metadata=metadata,
                    )
                    self._schedule_platform_readiness_retry()
                    return
                if state not in {"recovering", "degraded", "broker_degraded"}:
                    return
                await publish(
                    event_type="system_recovering",
                    reason="platform_recovery_readiness_not_met",
                    source="runtime_reconciler",
                    metadata=metadata,
                )
                self._schedule_platform_readiness_retry()
                return
            if state not in {"recovering", "degraded", "broker_degraded"}:
                return
            await publish(
                event_type="system_resumed",
                reason="runtime_reconciliation_clean",
                source="runtime_reconciler",
                metadata=metadata,
            )
            return
        publish = getattr(controller, "publish", None)
        if not callable(publish):
            return
        await publish(
            event_type="system_degraded",
            reason="runtime_reconciliation_anomaly_detected",
            source="runtime_reconciler",
            metadata=metadata,
        )

    async def _converge_broker_flat_position_mismatches(self, *, position_result: Any) -> None:
        if not self.broker_flat_position_convergence_enabled:
            return
        metadata = getattr(position_result, "metadata", None) or {}
        for item in list(metadata.get("mismatches") or []):
            try:
                symbol = str((item or {}).get("symbol") or "").strip().upper()
                ib_qty = float((item or {}).get("ib_qty") or 0.0)
                ledger_qty = float((item or {}).get("ledger_qty") or 0.0)
                strategy_id = str((item or {}).get("strategy_id") or (item or {}).get("owner_strategy_id") or "").strip()
            except Exception:
                continue
            if not symbol or abs(ib_qty) > 0.001 or abs(ledger_qty) <= 0.001:
                continue
            result = await self.broker_truth_convergence.converge_position_mismatch(
                symbol=symbol,
                ib_qty=ib_qty,
                ledger_qty=ledger_qty,
                strategy_id=strategy_id or None,
                reason="runtime_reconciler_broker_flat_position_convergence",
            )
            self._emit_audit(
                stage="broker_flat_position_convergence",
                severity="warning" if result.ok else "error",
                symbol=symbol,
                status="converged" if result.ok else "failed",
                reason=result.reason,
                payload=dict(result.metadata or {}),
            )

    def _evaluate_strategy_recovery_policy(self, *, position_result: Any) -> List[Any]:
        actions: List[Any] = []
        registry = self.strategy_recovery_policy_registry
        if registry is None:
            return actions
        metadata = getattr(position_result, "metadata", None) or {}
        for item in list(metadata.get("mismatches") or []):
            try:
                symbol = str((item or {}).get("symbol") or "").strip().upper()
                broker_qty = float((item or {}).get("ib_qty") or 0.0)
                ledger_qty = float((item or {}).get("ledger_qty") or 0.0)
                owner_qty = float((item or {}).get("owner_lane_qty") or 0.0)
                strategy_id = str(
                    (item or {}).get("strategy_id")
                    or (item or {}).get("owner_strategy_id")
                    or ""
                ).strip()
                account_id = str((item or {}).get("account_id") or "").strip()
                strategy_instance_id = str((item or {}).get("strategy_instance_id") or "").strip()
                lot_id = str((item or {}).get("position_lot_id") or (item or {}).get("lot_id") or "").strip()
                position_owner_key = str((item or {}).get("position_owner_key") or "").strip()
                owner_strategy_source = str((item or {}).get("owner_strategy_source") or "").strip()
            except Exception:
                continue
            if not symbol:
                continue
            owner_identity_complete = bool(account_id and strategy_id and strategy_instance_id and lot_id and position_owner_key)
            ledger_owner_source = owner_strategy_source == "ledger_owner_lane"
            exact_owner_quantity = owner_identity_complete and abs(abs(float(broker_qty or 0.0)) - abs(float(owner_qty or 0.0))) <= 1e-6
            owner_direction_consistent = (
                exact_owner_quantity
                and abs(float(broker_qty or 0.0)) > 1e-9
                and abs(float(owner_qty or 0.0)) > 1e-9
                and (float(broker_qty or 0.0) * float(owner_qty or 0.0)) > 0
            )
            if not strategy_id or not owner_identity_complete or not ledger_owner_source or not exact_owner_quantity or not owner_direction_consistent:
                if not strategy_id:
                    reason = "position_mismatch_without_strategy_owner"
                elif not owner_identity_complete:
                    reason = "position_mismatch_without_complete_owner_identity"
                elif not ledger_owner_source:
                    reason = "position_mismatch_without_ledger_owner_source"
                elif not exact_owner_quantity:
                    reason = "position_mismatch_without_exact_owner_quantity"
                else:
                    reason = "position_mismatch_owner_direction_conflict"
                actions.append(
                    RecoveryAction(
                        action_type="quarantine",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        target_qty=0.0,
                        reason=reason,
                        extras={
                            "broker_position_qty_hint": broker_qty,
                            "position_qty_hint": broker_qty,
                            "ledger_qty": ledger_qty,
                            "owner_lane_qty": owner_qty,
                            "account_id": account_id or None,
                            "strategy_instance_id": strategy_instance_id or None,
                            "position_lot_id": lot_id or None,
                            "lot_id": lot_id or None,
                            "position_owner_key": position_owner_key or None,
                            "owner_strategy_source": owner_strategy_source or None,
                        },
                    )
                )
                self._emit_audit(
                    stage="strategy_recovery_policy_unresolved",
                    severity="error",
                    symbol=symbol,
                    status="blocked",
                    reason=reason,
                    payload={
                        "broker_qty": broker_qty,
                        "ledger_qty": ledger_qty,
                        "owner_lane_qty": owner_qty,
                        "mismatch": dict(item or {}),
                    },
                )
                continue
            context = RecoveryPolicyContext(
                strategy_id=strategy_id,
                symbol=symbol,
                broker_qty=broker_qty,
                ledger_qty=ledger_qty,
                reason="position_ledger_diverged",
                metadata=dict(item or {}),
                account_id=account_id,
                strategy_instance_id=strategy_instance_id,
                lot_id=lot_id,
                position_owner_key=position_owner_key,
                owner_qty=owner_qty,
            )
            try:
                policy_actions = list(registry.build_actions(context))
            except Exception as exc:
                logger.warning(
                    "strategy_recovery_policy_evaluation_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                    exc_info=True,
                )
                continue
            actions.extend(policy_actions)
            self._emit_audit(
                stage="strategy_recovery_policy_evaluated",
                severity="warning" if policy_actions else "info",
                strategy_id=strategy_id,
                symbol=symbol,
                status="actions_ready" if policy_actions else "no_action",
                reason="position_mismatch_strategy_policy",
                payload={
                    "broker_qty": broker_qty,
                    "ledger_qty": ledger_qty,
                    "policy_actions": [
                        {
                            "action_type": str(getattr(action, "action_type", "")),
                            "target_qty": float(getattr(action, "target_qty", 0.0) or 0.0),
                            "reason": str(getattr(action, "reason", "")),
                            "extras": dict(getattr(action, "extras", {}) or {}),
                        }
                        for action in policy_actions
                    ],
                },
                )
        return actions

    async def _execute_strategy_recovery_actions(self, actions: List[Any]) -> None:
        if not actions:
            return
        order_manager = getattr(self, "recovery_order_manager", None)
        for action in list(actions or []):
            if self._recovery_action_suppressed(action):
                continue
            self._mark_recovery_action_attempted(action)
            action_type = str(getattr(action, "action_type", "") or "").strip().lower()
            if action_type == "flatten" and order_manager is not None:
                await self._submit_recovery_flatten_action(action=action, order_manager=order_manager)
                continue
            await self._quarantine_recovery_action(action=action, reason="recovery_action_not_executable")

    def _recovery_action_key(self, action: Any) -> str:
        extras = dict(getattr(action, "extras", {}) or {})
        return "|".join(
            [
                str(getattr(action, "action_type", "") or "").strip().lower(),
                str(getattr(action, "strategy_id", "") or "").strip(),
                str(getattr(action, "symbol", "") or "").strip().upper(),
                str(round(float(getattr(action, "target_qty", 0.0) or 0.0), 6)),
                str(round(float(extras.get("broker_position_qty_hint") or extras.get("position_qty_hint") or 0.0), 6)),
            ]
        )

    def _recovery_action_suppressed(self, action: Any) -> bool:
        key = self._recovery_action_key(action)
        until = float(self._recovery_action_suppress_until.get(key, 0.0) or 0.0)
        now = time.time()
        if until <= now:
            if key in self._recovery_action_suppress_until:
                self._recovery_action_suppress_until.pop(key, None)
            return False
        self._emit_audit(
            stage="strategy_recovery_action_suppressed",
            severity="info",
            strategy_id=str(getattr(action, "strategy_id", "") or "").strip() or None,
            symbol=str(getattr(action, "symbol", "") or "").strip().upper() or None,
            status="suppressed",
            reason="recovery_action_cooldown",
            payload={
                "action_key": key,
                "cooldown_until": until,
                "cooldown_remaining_sec": max(0.0, until - now),
            },
        )
        return True

    def _mark_recovery_action_attempted(self, action: Any) -> None:
        if self.recovery_action_cooldown_seconds <= 0.0:
            return
        key = self._recovery_action_key(action)
        self._recovery_action_suppress_until[key] = time.time() + self.recovery_action_cooldown_seconds

    async def _submit_recovery_flatten_action(self, *, action: Any, order_manager: Any) -> None:
        try:
            from services.decision.pipeline import PipelineResult
            from services.decision.translator import ExecPlan

            envelope = action.to_decision_envelope()
            extras = dict(getattr(envelope, "extras", {}) or {})
            target_qty = float(getattr(action, "target_qty", 0.0) or 0.0)
            qty = abs(target_qty)
            if qty <= 1e-9:
                await self._quarantine_recovery_action(action=action, reason="recovery_flatten_zero_qty")
                return
            side = "BUY" if target_qty > 0 else "SELL"
            result = PipelineResult(
                status="APPROVED",
                decision_id=str(envelope.decision_id),
                strategy_id=str(envelope.strategy_id),
                symbol=str(envelope.symbol).strip().upper(),
                stage="runtime_recovery_policy",
                plan=ExecPlan(
                    order_type="MKT",
                    side=side,
                    qty=qty,
                    limit_px=None,
                    stop_px=None,
                    tif="DAY",
                    price_collar_pct=0.0,
                    oco={},
                    child={"type": "NONE"},
                    notes={"source": "runtime_reconciler_strategy_recovery_policy"},
                ),
                reasons=[str(getattr(action, "reason", "") or "runtime_recovery_flatten")],
                metadata={"envelope_extras": extras},
            )
            submit = getattr(order_manager, "submit_flatten", None) or getattr(order_manager, "execute", None)
            if not callable(submit):
                await self._quarantine_recovery_action(action=action, reason="recovery_order_manager_unavailable")
                return
            outcome = submit(result)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            success = bool(getattr(outcome, "success", False))
            self._emit_audit(
                stage="strategy_recovery_action_submitted",
                severity="warning" if success else "error",
                strategy_id=str(envelope.strategy_id),
                symbol=str(envelope.symbol).strip().upper(),
                decision_id=str(envelope.decision_id),
                status="submitted" if success else "rejected",
                reason=str(getattr(outcome, "reason", "") or "recovery_action_submitted"),
                payload={
                    "action_type": str(getattr(action, "action_type", "")),
                    "target_qty": target_qty,
                    "side": side,
                    "qty": qty,
                    "stage": str(getattr(outcome, "stage", "") or ""),
                    "metadata": dict(getattr(outcome, "metadata", {}) or {}),
                },
            )
            if not success:
                await self._quarantine_recovery_action(
                    action=action,
                    reason=str(getattr(outcome, "reason", "") or "recovery_flatten_rejected"),
                )
        except Exception as exc:
            logger.warning("strategy_recovery_action_submit_failed", error=str(exc), exc_info=True)
            await self._quarantine_recovery_action(action=action, reason="recovery_action_submit_exception")

    async def _quarantine_recovery_action(self, *, action: Any, reason: str) -> None:
        strategy_id = str(getattr(action, "strategy_id", "") or "").strip()
        symbol = str(getattr(action, "symbol", "") or "").strip().upper()
        extras = dict(getattr(action, "extras", {}) or {})
        qty = float(extras.get("broker_position_qty_hint") or extras.get("position_qty_hint") or 0.0)
        if strategy_id and symbol and self.manual_intervention_lock_store is not None:
            try:
                await self.manual_intervention_lock_store.lock(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    ttl_sec=6 * 60 * 60,
                    reason="strategy_recovery_action_quarantine",
                    context={
                        "reason": reason,
                        "action_type": str(getattr(action, "action_type", "")),
                        "target_qty": float(getattr(action, "target_qty", 0.0) or 0.0),
                        "broker_position_qty_hint": qty,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "strategy_recovery_action_lock_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
        if symbol:
            try:
                await self._upsert_order_quarantine(
                    symbol=symbol,
                    broker_order_id="",
                    perm_id=None,
                    order_ref="",
                    side=None,
                    qty=qty,
                    order_type=None,
                    source="runtime_reconciler_strategy_recovery",
                    reason=reason,
                    payload={
                        "strategy_id": strategy_id,
                        "action_type": str(getattr(action, "action_type", "")),
                        "target_qty": float(getattr(action, "target_qty", 0.0) or 0.0),
                        "extras": extras,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "strategy_recovery_action_quarantine_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
        self._emit_audit(
            stage="strategy_recovery_action_quarantined",
            severity="error",
            strategy_id=strategy_id or None,
            symbol=symbol or None,
            status="quarantined",
            reason=reason,
            payload={
                "action_type": str(getattr(action, "action_type", "")),
                "target_qty": float(getattr(action, "target_qty", 0.0) or 0.0),
                "broker_position_qty_hint": qty,
            },
        )

    async def _quarantine_detected_orphans(self, orphans: List[Any]) -> List[Any]:
        return await self.order_quarantine_handler.quarantine_detected_orphans(orphans)

    async def _resolve_cleared_order_quarantines(
        self,
        *,
        db_by_broker_id: Dict[Any, Dict[str, Any]],
        db_by_perm_id: Dict[str, Dict[str, Any]],
        ib_by_id: Dict[Any, Any],
        ib_by_perm_id: Dict[str, Any],
    ) -> int:
        return await self.order_quarantine_handler.resolve_cleared_order_quarantines(
            db_by_broker_id=db_by_broker_id,
            db_by_perm_id=db_by_perm_id,
            ib_by_id=ib_by_id,
            ib_by_perm_id=ib_by_perm_id,
        )

    async def _has_active_order_quarantine(
        self,
        *,
        broker_order_id: str,
        perm_id: Optional[int],
    ) -> bool:
        return await self.order_quarantine_handler.has_active_order_quarantine(
            broker_order_id=broker_order_id,
            perm_id=perm_id,
        )

    @staticmethod
    def _merge_note_json(raw_note: Any, updates: Dict[str, Any]) -> str:
        base: Dict[str, Any] = {}
        if isinstance(raw_note, str) and raw_note.strip():
            try:
                parsed = json.loads(raw_note)
                if isinstance(parsed, dict):
                    base = parsed
            except Exception:
                base = {}
        for key, value in (updates or {}).items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merged = dict(base.get(key) or {})
                merged.update(value)
                base[key] = merged
            else:
                base[key] = value
        return json.dumps(base, ensure_ascii=False)

    async def _reconcile_flatten_lifecycle(
        self,
        *,
        db_orders: List[Dict[str, Any]],
        ib_open_orders: List[Any],
    ) -> None:
        return await self.flatten_lifecycle_handler.reconcile(
            db_orders=db_orders,
            ib_open_orders=ib_open_orders,
        )

    async def _maybe_unlock_flatten_action_lock(self, *, strategy_id: str, symbol: str) -> None:
        return await self.flatten_lifecycle_handler.maybe_unlock_flatten_action_lock(
            strategy_id=strategy_id,
            symbol=symbol,
        )

    async def _audit_flatten_lifecycle(self, *, db_orders: List[Dict[str, Any]]) -> None:
        return await self.flatten_lifecycle_handler.audit(db_orders=db_orders)

    async def _has_db_working_orders(self) -> bool:
        """
        Off-hours safeguard: if DB still tracks working orders, keep reconciler alive
        even outside the configured trading window so stale SUBMITTED rows do not
        survive overnight waiting for the next RTH session.
        """
        try:
            row = await self.pg.fetchrow(
                """
                SELECT COUNT(*) AS working_count
                FROM core.core_order
                WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                """
            )
            payload = dict(row) if row is not None else {}
            return int(payload.get("working_count") or 0) > 0
        except Exception as exc:
            logger.warning("runtime_reconciler_db_working_probe_failed", error=str(exc))
            return False

    async def _maybe_push_alert(
        self,
        *,
        db_open_count: int,
        ib_open_count: int,
        ib_pre_submitted: int,
        ghosts: int,
        orphans: int,
        orphans_detected: Optional[int] = None,
        orphans_self_healed: Optional[int] = None,
        mismatches: int,
        active_quarantine_count: int = 0,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.notification_manager is None:
            return
        if not self._in_trading_window():
            return
        detected_orphans = int(orphans if orphans_detected is None else orphans_detected)
        self_healed_orphans = int(
            max(0, detected_orphans - orphans)
            if orphans_self_healed is None
            else orphans_self_healed
        )
        if not self._should_alert_reconciliation_anomaly(
            ghosts=ghosts,
            unresolved_orphans=orphans,
            detected_orphans=detected_orphans,
            self_healed_orphans=self_healed_orphans,
            mismatches=mismatches,
            active_quarantine_count=int(active_quarantine_count or 0),
            evidence=dict(evidence or {}),
        ):
            return

        now = time.time()
        if now < self._next_alert_push_ts:
            return
        self._next_alert_push_ts = now + self.alert_cooldown_seconds

        try:
            event = NotificationEvent(
                event_type="reconciliation_alert",
                status="WARNING",
                strategy_id="system_reconciler",
                symbol="MULTI",
                side="N/A",
                qty=0.0,
                order_type="N/A",
                decision_id="",
                reason="runtime_reconciliation_anomaly_detected",
                metadata={
                    "db_open_orders": db_open_count,
                    "ib_open_orders": ib_open_count,
                    "ib_pre_submitted_orders": ib_pre_submitted,
                    "ghosts_found": ghosts,
                    "orphans_found": detected_orphans,
                    "orphans_unresolved": orphans,
                    "orphans_detected": detected_orphans,
                    "orphans_self_healed": self_healed_orphans,
                    "mismatches": mismatches,
                    "active_quarantine_count": int(active_quarantine_count or 0),
                    "evidence": dict(evidence or {}),
                },
            )
            # Reuse the unified channel; details are in metadata.
            self.notification_manager.publish(event)
            self._emit_audit(
                stage="reconciliation_alert_pushed",
                severity="warning",
                status="warning",
                reason="runtime_reconciliation_anomaly_detected",
                payload={
                    "db_open_orders": db_open_count,
                    "ib_open_orders": ib_open_count,
                    "ib_pre_submitted_orders": ib_pre_submitted,
                    "ghosts_found": ghosts,
                    "orphans_found": detected_orphans,
                    "orphans_unresolved": orphans,
                    "orphans_detected": detected_orphans,
                    "orphans_self_healed": self_healed_orphans,
                    "mismatches": mismatches,
                    "active_quarantine_count": int(active_quarantine_count or 0),
                    "evidence": dict(evidence or {}),
                },
            )
        except Exception as exc:
            logger.warning("reconciliation_alert_push_failed", error=str(exc))

    @staticmethod
    def _should_alert_reconciliation_anomaly(
        *,
        ghosts: int,
        unresolved_orphans: int,
        detected_orphans: int,
        self_healed_orphans: int,
        mismatches: int,
        active_quarantine_count: int,
        evidence: Dict[str, Any],
    ) -> bool:
        if ghosts > 0 or unresolved_orphans > 0 or mismatches > 0:
            return True
        if detected_orphans > 0 and self_healed_orphans <= 0:
            return True
        if active_quarantine_count <= 0:
            return False

        quarantine = dict(evidence.get("active_quarantine") or {})
        if bool(quarantine.get("lookup_failed")):
            return True
        by_reason = list(quarantine.get("by_reason") or [])
        normalized_reasons = {
            str(dict(item).get("reason") or "").strip()
            for item in by_reason
            if str(dict(item).get("reason") or "").strip()
        }
        if not normalized_reasons:
            return True
        # Resolved unknown broker orders may remain quarantined for auditability.
        # They should not keep paging Telegram once the runtime has already
        # self-healed and no unresolved discrepancy remains.
        if normalized_reasons <= {"unknown_broker_order_detected"} and unresolved_orphans <= 0 and ghosts <= 0 and mismatches <= 0:
            return False
        return True

    @staticmethod
    def _sample_db_order(order: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "order_id": str(order.get("order_id") or ""),
            "strategy_id": str(order.get("strategy_id") or ""),
            "symbol": str(order.get("symbol") or ""),
            "state": str(order.get("state") or ""),
            "broker_order_id": str(order.get("broker_order_id") or ""),
            "ib_perm_id": order.get("ib_perm_id"),
            "order_ref": str(order.get("order_ref") or ""),
        }

    @staticmethod
    def _sample_ib_order(order: Any) -> Dict[str, Any]:
        return {
            "order_id": str(getattr(order, "order_id", "") or ""),
            "symbol": str(getattr(order, "symbol", "") or ""),
            "status": str(getattr(order, "status", "") or ""),
            "side": str(getattr(order, "side", "") or ""),
            "qty": float(getattr(order, "qty", 0.0) or 0.0),
            "order_type": str(getattr(order, "order_type", "") or ""),
            "perm_id": getattr(order, "perm_id", None),
            "order_ref": str(getattr(order, "order_ref", "") or ""),
        }

    @staticmethod
    def _is_unconfirmed_pending_ib_order(order: Any) -> bool:
        status = str(getattr(order, "status", "") or "").strip()
        try:
            perm_id = int(getattr(order, "perm_id", 0) or 0)
        except Exception:
            perm_id = 0
        return status in {"PendingSubmit", "ApiPending"} and perm_id <= 0

    def _build_alert_evidence(
        self,
        *,
        ghosts: List[Dict[str, Any]],
        orphans_detected: List[Any],
        orphans_unresolved: List[Any],
        mismatches: List[Any],
        ib_status_counts: Dict[str, int],
        active_quarantine: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        mismatch_samples: List[Dict[str, Any]] = []
        for db_order, ib_order in mismatches[:5]:
            mismatch_samples.append(
                {
                    "order_id": str(db_order.get("order_id") or ""),
                    "strategy_id": str(db_order.get("strategy_id") or ""),
                    "symbol": str(db_order.get("symbol") or getattr(ib_order, "symbol", "") or ""),
                    "db_state": str(db_order.get("state") or ""),
                    "ib_status": str(getattr(ib_order, "status", "") or ""),
                    "broker_order_id": str(db_order.get("broker_order_id") or getattr(ib_order, "order_id", "") or ""),
                    "order_ref": str(db_order.get("order_ref") or getattr(ib_order, "order_ref", "") or ""),
                }
            )
        return {
            "ghost_samples": [self._sample_db_order(order) for order in ghosts[:5]],
            "orphan_detected_samples": [self._sample_ib_order(order) for order in orphans_detected[:5]],
            "orphan_unresolved_samples": [self._sample_ib_order(order) for order in orphans_unresolved[:5]],
            "mismatch_samples": mismatch_samples,
            "ib_status_counts": dict(ib_status_counts or {}),
            "active_quarantine": dict(active_quarantine or {}),
        }

    def _emit_audit(
        self,
        *,
        stage: str,
        severity: str = "info",
        strategy_id: Optional[str] = None,
        strategy_run_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[Any] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.audit_writer is None:
            return
        try:
            self.audit_writer.emit(
                domain="recon",
                component="runtime_reconciler",
                stage=stage,
                severity=severity,
                strategy_id=strategy_id,
                strategy_run_id=strategy_run_id,
                trigger_id=trigger_id,
                decision_id=decision_id,
                trace_id=trace_id,
                causation_id=causation_id,
                intent_id=intent_id,
                order_id=order_id,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
                symbol=symbol,
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            logger.warning("local_recon_audit_emit_failed", stage=stage, error=str(exc))

    async def _audit_positions(
        self,
        *,
        db_open_orders: Optional[List[Dict[str, Any]]] = None,
        ib_open_orders: Optional[List[Any]] = None,
    ) -> None:
        return await self.position_sync_handler.audit_positions(
            db_open_orders=db_open_orders,
            ib_open_orders=ib_open_orders,
        )

    async def _auto_heal_stale_db_position(
        self,
        *,
        symbol: str,
        ib_qty: float,
        db_qty: float,
        db_rows: List[Dict[str, Any]],
        db_open_symbols: Set[str],
        ib_open_symbols: Set[str],
        broker_truth_connected: Optional[bool] = None,
    ) -> bool:
        """
        Runtime reconciler no longer mutates DB positions.
        Kept as an explicit disabled hook to guard against accidental reintroduction.
        """
        logger.warning(
            "runtime_reconciler_auto_heal_disabled",
            symbol=str(symbol or "").strip().upper() or None,
            reason="detector_only_runtime_path",
        )
        return False

    async def _quarantine_broker_only_position(
        self,
        *,
        symbol: str,
        ib_qty: float,
        db_qty: float,
        db_rows: List[Dict[str, Any]],
        broker_truth_connected: Optional[bool] = None,
    ) -> bool:
        return await self.position_sync_handler.quarantine_broker_only_position(
            symbol=symbol,
            ib_qty=ib_qty,
            db_qty=db_qty,
            db_rows=db_rows,
            broker_truth_connected=broker_truth_connected,
        )

    async def _quarantine_terminal_orphan(
        self,
        *,
        strategy_id: str,
        symbol: str,
        broker_order_id: str,
        order_ref: str,
        db_state: str,
        ib_state: str,
        perm_id: Optional[int] = None,
        error: Optional[str] = None,
    ) -> bool:
        sid = str(strategy_id or "").strip()
        sym = str(symbol or "").strip().upper()
        if not sid or not sym:
            return False
        if self.manual_intervention_lock_store is None:
            self._emit_audit(
                stage="orphan_quarantine_skipped",
                severity="error",
                strategy_id=sid,
                broker_order_id=broker_order_id or None,
                perm_id=perm_id,
                symbol=sym,
                status="lock_store_missing",
                reason="terminal_orphan_without_quarantine_lock_store",
                payload={
                    "order_ref": order_ref,
                    "db_state": db_state,
                    "ib_state": ib_state,
                    "error": str(error or ""),
                },
            )
            return False
        try:
            await self.manual_intervention_lock_store.lock(
                strategy_id=sid,
                symbol=sym,
                ttl_sec=6 * 60 * 60,
                reason="orphan_order_quarantine",
                context={
                    "broker_order_id": str(broker_order_id or ""),
                    "order_ref": str(order_ref or ""),
                    "db_state": str(db_state or ""),
                    "ib_state": str(ib_state or ""),
                    "error": str(error or ""),
                },
            )
        except Exception as exc:
            logger.warning(
                "terminal_orphan_quarantine_lock_failed",
                strategy_id=sid,
                symbol=sym,
                broker_order_id=broker_order_id,
                error=str(exc),
            )
            return False
        self._emit_audit(
            stage="orphan_quarantined",
            severity="error",
            strategy_id=sid,
            broker_order_id=broker_order_id or None,
            perm_id=perm_id,
            symbol=sym,
            status="quarantined",
            reason="terminal_orphan_cancel_not_confirmed",
            payload={
                "order_ref": order_ref,
                "db_state": db_state,
                "ib_state": ib_state,
                "ttl_sec": 6 * 60 * 60,
                "error": str(error or ""),
            },
        )
        await self._upsert_order_quarantine(
            symbol=sym,
            broker_order_id=str(broker_order_id or ""),
            perm_id=perm_id,
            order_ref=str(order_ref or ""),
            side=None,
            qty=None,
            order_type=None,
            source="runtime_reconciler_terminal_orphan",
            reason="terminal_orphan_cancel_not_confirmed",
            payload={
                "strategy_id": sid,
                "db_state": str(db_state or ""),
                "ib_state": str(ib_state or ""),
                "error": str(error or ""),
            },
        )
        return True

    async def _upsert_order_quarantine(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        perm_id: Optional[int],
        ib_client_id: Optional[int] = None,
        order_ref: str,
        side: Optional[str],
        qty: Optional[float],
        order_type: Optional[str],
        source: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        return await self.order_quarantine_handler.upsert_order_quarantine(
            symbol=symbol,
            broker_order_id=broker_order_id,
            perm_id=perm_id,
            ib_client_id=ib_client_id,
            order_ref=order_ref,
            side=side,
            qty=qty,
            order_type=order_type,
            source=source,
            reason=reason,
            payload=payload,
        )

    def _broker_truth_connected(self) -> Optional[bool]:
        try:
            if hasattr(self.adapter, "is_connected") and callable(getattr(self.adapter, "is_connected")):
                return bool(self.adapter.is_connected())
            ib = getattr(self.adapter, "ib", None)
            if ib is not None and hasattr(ib, "isConnected"):
                return bool(ib.isConnected())
        except Exception:
            return None
        return None

    async def _sync_positions_from_ib(self) -> None:
        return await self.position_sync_handler.sync_positions_from_ib()

    @staticmethod
    def _ib_position_symbol(position: Any) -> str:
        contract = getattr(position, "contract", None)
        if not contract:
            return ""
        sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
        # For options, never fallback to bare underlying symbol when mapping fails.
        # That can incorrectly treat an option position as flat for internal option symbols.
        if sec_type in {"OPT", "FOP"}:
            try:
                mapped = internal_opt_symbol_from_ib_contract(contract)
                if mapped:
                    return str(mapped).strip().upper()
            except Exception:
                pass
            return ""
        try:
            mapped = internal_opt_symbol_from_ib_contract(contract)
            if mapped:
                return str(mapped).strip().upper()
        except Exception:
            pass
        return str(getattr(contract, "symbol", "") or "").strip().upper()

    async def _ensure_reconciler_strategy(self) -> None:
        try:
            await self.pg.execute(
                """
                INSERT INTO core.core_strategy (strategy_id, name, capital_limit_usd, reserve_buffer_bps, is_enabled, created_at)
                VALUES ('adopted_reconciler', 'Adopted by Runtime Reconciler', 1000000, 10, true, now())
                ON CONFLICT (strategy_id) DO NOTHING
                """
            )
        except Exception:
            pass

    async def _fetch_db_open_orders(self) -> List[Dict[str, Any]]:
        """
        Fetch open orders from database

        Returns:
            List of order dicts
        """
        try:
            rows = await self.pg.fetchall(
                """
                SELECT order_id, broker_order_id, strategy_id, symbol,
                       side, order_type, ib_perm_id, qty, state, idempotency_key,
                       order_ref,
                       created_at, updated_at, note
                FROM core.core_order
                WHERE state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')
                  -- NOTE:
                  -- We must not hide rows based on note content. A previous cleanup attempt may
                  -- have written a "ghost" marker into note but failed to transition state to a
                  -- terminal value (or an operator may have annotated the note). If we filter
                  -- these out, entry-isolation will keep seeing "SUBMITTED" while the reconciler
                  -- can no longer repair the row.
                ORDER BY created_at DESC
                """
            )

            return [dict(r) for r in rows]

        except Exception as e:
            logger.error("Failed to fetch DB open orders", error=str(e))
            return []

    @staticmethod
    def _is_protective_order_ref(order_ref: Any) -> bool:
        ref = str(order_ref or "").strip().lower()
        return ref.startswith("sl:") or ref.startswith("tp:")

    async def _load_db_position_qty(self, *, strategy_id: str, symbol: str) -> float:
        try:
            row = await self.pg.fetchone(
                """
                SELECT pos_qty
                FROM core.core_position
                WHERE strategy_id = $1 AND symbol = $2
                """,
                str(strategy_id or "").strip(),
                str(symbol or "").strip().upper(),
            )
            return float((row or {}).get("pos_qty") or 0.0)
        except Exception:
            return 0.0

    async def _cancel_flat_protective_orders(
        self,
        *,
        db_orders: List[Dict[str, Any]],
        ib_by_id: Dict[Any, Any],
    ) -> None:
        cancel_fn = getattr(self.adapter, "cancel", None)
        if not callable(cancel_fn):
            return

        pos_cache: Dict[Tuple[str, str], float] = {}
        broker_symbol_pos_cache: Dict[str, Optional[float]] = {}

        for row in db_orders or []:
            state = str(row.get("state") or "").strip().upper()
            if state not in {"NEW", "SUBMITTED", "PARTIAL"}:
                continue
            order_ref = str(row.get("order_ref") or "").strip()
            if not self._is_protective_order_ref(order_ref):
                continue
            strategy_id = str(row.get("strategy_id") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            if not strategy_id or not symbol:
                continue

            key = (strategy_id, symbol)
            if key not in pos_cache:
                pos_cache[key] = float(await self._load_db_position_qty(strategy_id=strategy_id, symbol=symbol))
            if abs(float(pos_cache.get(key) or 0.0)) > 1e-6:
                continue
            if symbol not in broker_symbol_pos_cache:
                broker_symbol_pos_cache[symbol] = await self._load_broker_symbol_position_qty(symbol=symbol)
            broker_qty = broker_symbol_pos_cache[symbol]
            if broker_qty is None:
                logger.warning(
                    "flat_protective_cancel_skipped_broker_position_unknown",
                    order_id=row.get("order_id"),
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_ref=order_ref,
                )
                self._emit_audit(
                    stage="flat_protective_cancel_skipped_broker_position_unknown",
                    severity="warning",
                    strategy_id=strategy_id or None,
                    order_id=str(row.get("order_id") or "") or None,
                    broker_order_id=str(row.get("broker_order_id") or "") or None,
                    symbol=symbol or None,
                    status="skip",
                    reason="broker_position_unknown",
                    payload={
                        "order_ref": order_ref,
                    },
                )
                continue
            if abs(float(broker_qty or 0.0)) > 1e-6:
                logger.warning(
                    "flat_protective_cancel_skipped_broker_position_nonzero",
                    order_id=row.get("order_id"),
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_ref=order_ref,
                    broker_qty=float(broker_qty),
                )
                continue

            broker_order_id_int = self._numeric_broker_order_id(row.get("broker_order_id"))
            if broker_order_id_int is None:
                continue
            norm_id = self._normalize_broker_order_id(broker_order_id_int)
            if norm_id not in ib_by_id:
                continue

            cancel_ok = False
            try:
                cancel_ok = bool(await cancel_fn(broker_order_id_int))  # type: ignore[misc]
            except Exception as exc:
                logger.warning(
                    "flat_protective_cancel_failed",
                    order_id=row.get("order_id"),
                    broker_order_id=broker_order_id_int,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_ref=order_ref,
                    error=str(exc),
                )
                continue

            still_open = None
            if hasattr(self.adapter, "open_orders"):
                try:
                    refreshed = await self.adapter.open_orders(force_refresh=True)  # type: ignore[call-arg]
                    still_open = any(
                        self._broker_order_id_matches(getattr(o, "order_id", None), broker_order_id_int)
                        for o in (refreshed or [])
                    )
                except Exception:
                    still_open = None

            if still_open is True:
                logger.warning(
                    "flat_protective_cancel_not_confirmed",
                    order_id=row.get("order_id"),
                    broker_order_id=broker_order_id_int,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_ref=order_ref,
                    cancel_ok=cancel_ok,
                )
                continue
            probe_found = None
            if still_open is None and hasattr(self.adapter, "probe_order_open_fresh"):
                try:
                    probe = await self.adapter.probe_order_open_fresh(broker_order_id_int)  # type: ignore[attr-defined]
                    probe_found = probe.get("found") if isinstance(probe, dict) else None
                except Exception:
                    probe_found = None
            confirmed_absent = (still_open is False) or (probe_found is False)
            if not confirmed_absent:
                logger.warning(
                    "flat_protective_cancel_inconclusive_skip_db_terminal",
                    order_id=row.get("order_id"),
                    broker_order_id=broker_order_id_int,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_ref=order_ref,
                    still_open=still_open,
                    probe_found=probe_found,
                )
                continue

            note_data = self._merge_note_json(
                row.get("note"),
                {
                    "flat_protective_auto_cancel": {
                        "enabled": True,
                        "cancel_ok": bool(cancel_ok),
                        "confirmed_absent": bool(confirmed_absent),
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
            try:
                await self.pg.execute(
                    """
                    UPDATE core.core_order
                    SET state = 'CANCELLED',
                        note = $1,
                        updated_at = now()
                    WHERE order_id = $2
                    """,
                    note_data,
                    row.get("order_id"),
                )
            except Exception as exc:
                logger.warning(
                    "flat_protective_mark_cancelled_failed",
                    order_id=row.get("order_id"),
                    broker_order_id=broker_order_id_int,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_ref=order_ref,
                    error=str(exc),
                )
                continue

            idem = str(row.get("idempotency_key") or "").strip()
            if idem and self.reservation is not None:
                try:
                    await self.reservation.release_on_terminal(
                        idempotency_key=idem,
                        order_id=str(row.get("order_id") or ""),
                        terminal_state="CANCELLED",
                        reason="runtime_reconciler_flat_protective_auto_cancelled",
                    )
                except Exception as exc:
                    logger.warning(
                        "flat_protective_reservation_release_failed",
                        order_id=row.get("order_id"),
                        idempotency_key=idem,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        error=str(exc),
                    )

            logger.warning(
                "flat_protective_order_auto_cancelled",
                order_id=row.get("order_id"),
                broker_order_id=broker_order_id_int,
                strategy_id=strategy_id,
                symbol=symbol,
                order_ref=order_ref,
                cancel_ok=cancel_ok,
                confirmed_absent=bool(confirmed_absent),
            )
            self._emit_audit(
                stage="flat_protective_order_auto_cancelled",
                severity="warning",
                strategy_id=strategy_id or None,
                order_id=str(row.get("order_id") or "") or None,
                broker_order_id=str(broker_order_id_int),
                symbol=symbol or None,
                status="auto_healed",
                reason="flat_position_protective_order_residual",
                payload={
                    "order_ref": order_ref,
                    "cancel_ok": bool(cancel_ok),
                    "confirmed_absent": bool(confirmed_absent),
                },
            )

    async def _load_broker_symbol_position_qty(self, *, symbol: str) -> Optional[float]:
        try:
            if hasattr(self.adapter, "positions"):
                ib_positions = await self.adapter.positions()
            elif hasattr(self.adapter, "ib"):
                ib_positions = self.adapter.ib.positions()
            else:
                return None
        except Exception:
            return None
        sym_u = str(symbol or "").strip().upper()
        target_underlying = ""
        if sym_u.startswith("OPT:"):
            parts = sym_u.split(":")
            if len(parts) >= 2:
                target_underlying = str(parts[1] or "").strip().upper()
        qty = 0.0
        for p in ib_positions or []:
            try:
                if getattr(self.adapter, "account", "") and getattr(p, "account", "") and getattr(p, "account", "") != getattr(self.adapter, "account", ""):
                    continue
                contract = getattr(p, "contract", None)
                sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
                psym = self._ib_position_symbol(p)
                if not psym and sec_type in {"OPT", "FOP"} and target_underlying:
                    base_underlying = str(getattr(contract, "symbol", "") or "").strip().upper()
                    if base_underlying == target_underlying:
                        return None
                if str(psym or "").strip().upper() != sym_u:
                    continue
                qty += float(getattr(p, "position", 0.0) or 0.0)
            except Exception:
                continue
        return float(qty)

    async def _fetch_ib_open_orders(self, *, force_refresh: bool = False) -> List:
        """
        Fetch open orders from IB

        Returns:
            List of PlacedOrder objects
        """
        try:
            return await self.adapter.open_orders(force_refresh=bool(force_refresh))

        except Exception as e:
            logger.error("Failed to fetch IB open orders", error=str(e))
            return []

    def _state_mismatch(self, db_state: str, ib_status: str) -> bool:
        """
        Check if DB state and IB status represent a mismatch

        Args:
            db_state: Database state
            ib_status: IB status

        Returns:
            True if mismatch
        """
        expected_db_state = self._map_ib_status_to_db_state(ib_status)

        # Partial fills are okay
        if db_state == 'PARTIAL' and ib_status in ('Submitted', 'PreSubmitted', 'PendingSubmit', 'PendingCancel'):
            return False

        return db_state != expected_db_state

    @staticmethod
    def _map_ib_status_to_db_state(ib_status: str) -> str:
        """
        Canonical IB -> DB state mapping used by both mismatch detection and orphan adoption.
        PendingSubmit/PendingCancel are both "working" states, therefore map to SUBMITTED.
        """
        ib_to_db = {
            "Submitted": "SUBMITTED",
            "PreSubmitted": "SUBMITTED",
            "PendingSubmit": "SUBMITTED",
            "PendingCancel": "SUBMITTED",
            "Filled": "FILLED",
            "Cancelled": "CANCELLED",
            "ApiCancelled": "CANCELLED",
            "Inactive": "CANCELLED",
        }
        return ib_to_db.get(str(ib_status or "").strip(), "SUBMITTED")

    async def _cleanup_ghosts(self, ghosts: List[Dict[str, Any]]) -> None:
        logger.warning(
            "runtime_reconciler_cleanup_ghosts_removed",
            reason="destructive_runtime_cleanup_disabled",
            requested_count=len(ghosts or []),
        )
        return
        """
        Clean up ghost orders and move them to a terminal state.

        Safety rule:
        - Ghost detection only means "not currently present in broker open-order snapshot".
          It is insufficient evidence for execution.
        - Therefore, ghosts must never be auto-marked as FILLED by reconciler.
        - All ghosts are terminalized as CANCELLED and true fills must come from broker
          fill/order-status events or live position reconciliation.
        """
        for ghost in ghosts:
            try:
                probe = await self._probe_ghost_open_fresh(ghost)
                suppress_key = self._suppress_key_from_broker_order_id(probe.get("broker_order_id"))
                if probe.get("found") is True:
                    if suppress_key is not None:
                        self._ghost_suppress_until[suppress_key] = time.time() + 300.0
                    logger.warning(
                        "ghost_cleanup_skipped_still_open_fresh_probe",
                        order_id=ghost.get("order_id"),
                        broker_order_id=probe.get("broker_order_id"),
                        symbol=ghost.get("symbol"),
                        strategy_id=ghost.get("strategy_id"),
                        live_adapter_client_id=getattr(self.adapter, "client_id", None),
                        fresh_probe_client_id=probe.get("probe_client_id"),
                        fresh_probe_owner_client_id=probe.get("owner_client_id"),
                        fresh_probe_status=probe.get("status"),
                        fresh_probe_perm_id=probe.get("perm_id"),
                        fresh_probe_why_held=probe.get("why_held"),
                    )
                    self._emit_audit(
                        stage="ghost_cleanup_skipped",
                        severity="warning",
                        strategy_id=str(ghost.get("strategy_id") or "") or None,
                        order_id=str(ghost.get("order_id") or "") or None,
                        broker_order_id=str(probe.get("broker_order_id") or "") or None,
                        perm_id=probe.get("perm_id"),
                        symbol=str(ghost.get("symbol") or "") or None,
                        status="skip_terminalize",
                        reason="ghost_candidate_still_open",
                        payload={
                            "fresh_probe_found": True,
                            "fresh_probe_client_id": probe.get("probe_client_id"),
                            "fresh_probe_owner_client_id": probe.get("owner_client_id"),
                            "fresh_probe_status": probe.get("status"),
                            "fresh_probe_perm_id": probe.get("perm_id"),
                            "fresh_probe_why_held": probe.get("why_held"),
                        },
                    )
                    continue
                if probe.get("checked") and probe.get("found") is None:
                    if suppress_key is not None:
                        self._ghost_suppress_until[suppress_key] = time.time() + 60.0
                    logger.warning(
                        "ghost_cleanup_skipped_probe_inconclusive",
                        order_id=ghost.get("order_id"),
                        broker_order_id=probe.get("broker_order_id"),
                        symbol=ghost.get("symbol"),
                        strategy_id=ghost.get("strategy_id"),
                        live_adapter_client_id=getattr(self.adapter, "client_id", None),
                        fresh_probe_client_id=probe.get("probe_client_id"),
                        probe_error=probe.get("error"),
                    )
                    self._emit_audit(
                        stage="ghost_cleanup_skipped",
                        severity="warning",
                        strategy_id=str(ghost.get("strategy_id") or "") or None,
                        order_id=str(ghost.get("order_id") or "") or None,
                        broker_order_id=str(probe.get("broker_order_id") or "") or None,
                        symbol=str(ghost.get("symbol") or "") or None,
                        status="skip_terminalize",
                        reason="ghost_probe_inconclusive",
                        payload={
                            "fresh_probe_found": None,
                            "fresh_probe_client_id": probe.get("probe_client_id"),
                            "probe_error": probe.get("error"),
                        },
                    )
                    continue
                target_state = "CANCELLED"

                trace_obj: Dict[str, Any] = {}
                decision_id = ""
                try:
                    old_note = ghost.get("note")
                    if isinstance(old_note, str) and old_note.strip():
                        old_data = json.loads(old_note)
                        if isinstance(old_data, dict):
                            decision_id = self._extract_decision_id_from_note_obj(old_data)
                            t = old_data.get("trace") or {}
                            if isinstance(t, dict):
                                trace_obj = t
                except Exception:
                    trace_obj = {}
                    decision_id = ""
                if not decision_id:
                    decision_id = await self._resolve_decision_id_from_events(
                        order_id=ghost.get("order_id"),
                        broker_order_id=ghost.get("broker_order_id"),
                    )

                note_data = json.dumps({
                    "ghost_detected": True,
                    "detected_at": str(ghost.get('updated_at')),
                    "original_state": ghost.get('state'),
                    "auto_terminal_state": target_state,
                    "trace": trace_obj,
                })

                await self.pg.execute(
                    """
                    UPDATE core.core_order
                    SET state = $1,
                        note = $2,
                        updated_at = now()
                    WHERE order_id = $3
                    """,
                    target_state, note_data, ghost['order_id']
                )
                idem = str(ghost.get("idempotency_key") or "").strip()
                if idem and self.reservation is not None and target_state in {"FILLED", "CANCELLED", "REJECTED"}:
                    try:
                        await self.reservation.release_on_terminal(
                            idempotency_key=idem,
                            order_id=str(ghost.get("order_id") or ""),
                            terminal_state=target_state,
                            reason="runtime_reconciler_ghost_terminalized",
                        )
                    except Exception as exc:
                        logger.warning(
                            "ghost_reservation_release_failed",
                            order_id=ghost.get("order_id"),
                            idempotency_key=idem,
                            strategy_id=ghost.get("strategy_id"),
                            symbol=ghost.get("symbol"),
                            terminal_state=target_state,
                            error=str(exc),
                        )

                logger.warning("Ghost order marked terminal",
                              order_id=ghost['order_id'],
                              symbol=ghost['symbol'],
                              strategy_id=ghost['strategy_id'],
                              original_state=ghost['state'],
                              terminal_state=target_state)
                await self._finalize_decision_from_trace(
                    decision_id=decision_id or str(trace_obj.get("decision_id") or ""),
                    terminal_state=target_state,
                )
                if trace_obj and str(trace_obj.get("trace_id") or ""):
                    await emit_order_event(
                        pg=self.pg,
                        enabled=self.order_event_enabled,
                        event_type="reconciler_corrected",
                        trace_id=str(trace_obj.get("trace_id") or ""),
                        causation_id=str(trace_obj.get("causation_id") or "") or None,
                        strategy_id=str(ghost.get("strategy_id") or ""),
                        strategy_run_id=str(trace_obj.get("strategy_run_id") or "unknown"),
                        trigger_id=str(trace_obj.get("trigger_id") or "") or None,
                        decision_id=decision_id or str(trace_obj.get("decision_id") or "") or "unknown",
                        intent_id=str(ghost.get("idempotency_key") or "") or None,
                        order_id=str(ghost.get("order_id") or "") or None,
                        broker_order_id=str(ghost.get("broker_order_id") or "") or None,
                        symbol=str(ghost.get("symbol") or ""),
                        payload={
                            "reason": "ghost_order_terminalized",
                            "original_state": str(ghost.get("state") or ""),
                            "terminal_state": target_state,
                        },
                    )
                if str(ghost.get("strategy_id") or "") == "__netting__":
                    await self._propagate_parent_terminal(
                        parent_order_id=str(ghost["order_id"]),
                        terminal_state=target_state,
                        reason="runtime_reconciler_parent_terminal",
                    )

            except Exception as e:
                logger.error("Failed to cleanup ghost order",
                            order_id=ghost['order_id'],
                            error=str(e))

    async def _terminalize_flat_position_protection_ghosts(
        self,
        *,
        ghosts: List[Dict[str, Any]],
        position_metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Repair stale DB protection legs only when broker position truth is flat.

        Generic ghost cleanup stays disabled in runtime. This handles the OCO
        sibling case where a flatten/TP fill closes the broker position, IB
        removes the sibling stop/take-profit order, and DB still shows that
        protection leg as SUBMITTED.
        """
        ib_positions_raw = position_metadata.get("ib_positions") or {}
        ib_positions: Dict[str, float] = {}
        if isinstance(ib_positions_raw, dict):
            for symbol, qty in ib_positions_raw.items():
                try:
                    ib_positions[str(symbol or "").strip().upper()] = float(qty or 0.0)
                except Exception:
                    continue
        remaining: List[Dict[str, Any]] = []
        terminalized: List[Dict[str, Any]] = []
        for ghost in ghosts or []:
            symbol = str(ghost.get("symbol") or "").strip().upper()
            broker_qty = float(ib_positions.get(symbol, 0.0) or 0.0)
            if abs(broker_qty) > 1e-12 or not self._is_protection_ghost(ghost):
                remaining.append(ghost)
                continue
            await self._terminalize_protection_ghost(ghost)
            terminalized.append(ghost)
        if terminalized:
            self._emit_audit(
                stage="flat_position_protection_ghost_terminalized",
                severity="warning",
                status="repaired",
                reason="broker_position_flat_and_protection_missing_from_broker",
                payload={
                    "count": len(terminalized),
                    "order_ids": [str(item.get("order_id") or "") for item in terminalized],
                    "symbols": sorted({str(item.get("symbol") or "").strip().upper() for item in terminalized}),
                },
            )
        return remaining

    @staticmethod
    def _is_protection_ghost(ghost: Dict[str, Any]) -> bool:
        note = ghost.get("note")
        if isinstance(note, str):
            try:
                note = json.loads(note)
            except Exception:
                note = {}
        note_dict = note if isinstance(note, dict) else {}
        tokens = " ".join(
            str(value or "").strip().lower()
            for value in (
                ghost.get("order_ref"),
                ghost.get("idempotency_key"),
                note_dict.get("type"),
                note_dict.get("protection_key"),
            )
        )
        return any(token in tokens for token in ("stop_loss", "take_profit", "protect", "sl:", "tp:"))

    async def _terminalize_protection_ghost(self, ghost: Dict[str, Any]) -> None:
        old_note = ghost.get("note")
        if isinstance(old_note, str):
            try:
                note_data = json.loads(old_note)
            except Exception:
                note_data = {"previous_note": old_note}
        elif isinstance(old_note, dict):
            note_data = dict(old_note)
        else:
            note_data = {}
        note_data.update(
            {
                "ghost_detected": True,
                "ghost_terminalized": True,
                "ghost_terminalized_reason": "broker_position_flat_protection_missing_from_broker",
                "ghost_terminalized_at": datetime.now(timezone.utc).isoformat(),
                "original_state": str(ghost.get("state") or ""),
            }
        )
        await self.pg.execute(
            """
            UPDATE core.core_order
            SET state = 'CANCELLED',
                note = $2::jsonb,
                updated_at = now()
            WHERE order_id = $1
              AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
            """,
            ghost["order_id"],
            json.dumps(note_data, ensure_ascii=False),
        )
        idem = str(ghost.get("idempotency_key") or "").strip()
        if idem and self.reservation is not None:
            try:
                await self.reservation.release_on_terminal(
                    idempotency_key=idem,
                    order_id=str(ghost.get("order_id") or ""),
                    terminal_state="CANCELLED",
                    reason="runtime_reconciler_flat_position_protection_ghost_terminalized",
                )
            except Exception as exc:
                logger.warning(
                    "flat_position_protection_ghost_reservation_release_failed",
                    order_id=ghost.get("order_id"),
                    idempotency_key=idem,
                    strategy_id=ghost.get("strategy_id"),
                    symbol=ghost.get("symbol"),
                    error=str(exc),
                )

    async def _resolve_decision_id_from_events(self, *, order_id: Any, broker_order_id: Any) -> str:
        if self.pg is None:
            return ""
        try:
            order_uuid = str(order_id or "").strip()
            broker_raw = str(broker_order_id or "").strip()
            broker_id = None
            if broker_raw:
                try:
                    broker_id = int(broker_raw)
                except Exception:
                    broker_id = None
            row = None
            if order_uuid:
                try:
                    row = await self.pg.fetchone(
                        """
                        SELECT decision_id
                        FROM core.core_order_event
                        WHERE order_id = $1::uuid
                          AND COALESCE(decision_id, '') <> ''
                        ORDER BY event_ts DESC
                        LIMIT 1
                        """,
                        order_uuid,
                    )
                except Exception:
                    row = None
            if (not row) and (broker_id is not None):
                row = await self.pg.fetchone(
                    """
                    SELECT decision_id
                    FROM core.core_order_event
                    WHERE broker_order_id = $1
                      AND COALESCE(decision_id, '') <> ''
                    ORDER BY event_ts DESC
                    LIMIT 1
                    """,
                    str(broker_id),
                )
            return str((row or {}).get("decision_id") or "").strip()
        except Exception as exc:
            logger.debug(
                "runtime_reconciler_resolve_decision_id_from_events_failed",
                order_id=str(order_id or ""),
                broker_order_id=str(broker_order_id or ""),
                error=str(exc),
            )
            return ""

    @staticmethod
    def _extract_decision_id_from_note_obj(note_obj: Dict[str, Any]) -> str:
        try:
            if not isinstance(note_obj, dict):
                return ""
            direct = str(note_obj.get("decision_id") or "").strip()
            if direct:
                return direct
            trace = note_obj.get("trace")
            if isinstance(trace, dict):
                direct = str(trace.get("decision_id") or "").strip()
                if direct:
                    return direct
            extras = note_obj.get("extras")
            if isinstance(extras, dict):
                direct = str(extras.get("decision_id") or "").strip()
                if direct:
                    return direct
        except Exception:
            return ""
        return ""

    async def _finalize_decision_from_trace(self, *, decision_id: str, terminal_state: str) -> None:
        if self.decision_finalizer is None:
            return
        did = str(decision_id or "").strip()
        if not did:
            return
        try:
            ret = self.decision_finalizer(did, str(terminal_state or "").upper())
            if inspect.isawaitable(ret):
                await ret
        except Exception as exc:
            logger.warning(
                "runtime_reconciler_decision_finalizer_failed",
                decision_id=did,
                terminal_state=terminal_state,
                error=str(exc),
            )

    async def _propagate_parent_terminal(self, *, parent_order_id: str, terminal_state: str, reason: str) -> None:
        try:
            children = await self.pg.fetchall(
                """
                SELECT order_id, idempotency_key, state
                FROM core.core_order
                WHERE parent_order_id = $1::uuid
                  AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                """,
                parent_order_id,
            )
        except Exception as exc:
            logger.warning("reconciler_child_query_failed", parent_order_id=parent_order_id, error=str(exc))
            return

        for child in children or []:
            child_id = str(child.get("order_id"))
            idem = str(child.get("idempotency_key") or "")
            try:
                await self.pg.execute(
                    """
                    UPDATE core.core_order
                    SET state = $1,
                        updated_at = now()
                    WHERE order_id = $2::uuid
                    """,
                    terminal_state,
                    child_id,
                )
                if idem and self.reservation is not None and terminal_state in {"FILLED", "CANCELLED", "REJECTED"}:
                    await self.reservation.release_on_terminal(
                        idempotency_key=idem,
                        order_id=child_id,
                        terminal_state=terminal_state,
                        reason=reason,
                    )
            except Exception as exc:
                logger.warning(
                    "reconciler_child_terminal_propagation_failed",
                    parent_order_id=parent_order_id,
                    child_order_id=child_id,
                    terminal_state=terminal_state,
                    error=str(exc),
                )

    async def _adopt_orphans(self, orphans: List[Any]) -> List[Any]:
        logger.warning(
            "runtime_reconciler_adopt_orphans_removed",
            reason="destructive_runtime_adoption_disabled",
            requested_count=len(orphans or []),
        )
        return list(orphans or [])
        """
        Adopt orphan orders by creating database records for them.
        """
        import json
        unresolved_orphans: List[Any] = []

        # Ensure the virtual reconciler strategy exists to satisfy FK constraints
        try:
            await self.pg.execute("""
                INSERT INTO core.core_strategy (strategy_id, name, capital_limit_usd, reserve_buffer_bps, is_enabled, created_at)
                VALUES ('adopted_reconciler', 'Adopted by Runtime Reconciler', 1000000, 10, true, now())
                ON CONFLICT (strategy_id) DO NOTHING
            """)
        except Exception as e:
            logger.warning("Failed to register reconciler strategy", error=str(e))
            return list(orphans or [])

        # Safety: manual_flatten should never stack multiple working orders for the same symbol+side.
        # If we see duplicates (usually after restart/partial DB failure), cancel older ones first.
        manual_groups: Dict[tuple[str, str, str], List[Any]] = {}
        for orphan in orphans or []:
            try:
                ref = str(getattr(orphan, "order_ref", "") or "")
                if not ref.startswith("manual_flatten:"):
                    continue
                ref_parts = ref.split(":")
                strategy_id = str(ref_parts[2]).strip() if len(ref_parts) >= 3 else ""
                symbol = str(getattr(orphan, "symbol", "") or "").strip().upper()
                side = str(getattr(orphan, "side", "") or "").strip().upper()
                if symbol and side:
                    manual_groups.setdefault((strategy_id, symbol, side), []).append(orphan)
            except Exception:
                continue

        if manual_groups and hasattr(self.adapter, "cancel"):
            for (strategy_id, symbol, side), items in manual_groups.items():
                if len(items) <= 1:
                    continue
                try:
                    items_sorted = sorted(
                        items,
                        key=lambda x: str(self._normalize_broker_order_id(getattr(x, "order_id", None)) or ""),
                    )
                except Exception:
                    items_sorted = list(items)
                keep = items_sorted[-1]
                for old in items_sorted[:-1]:
                    try:
                        old_order_id = self._numeric_broker_order_id(getattr(old, "order_id", None))
                        if old_order_id is None:
                            continue
                        keep_order_id = self._normalize_broker_order_id(getattr(keep, "order_id", None))
                        await self.adapter.cancel(old_order_id)  # type: ignore[call-arg]
                        logger.error(
                            "manual_flatten_duplicate_orphan_cancelled",
                            strategy_id=strategy_id,
                            symbol=symbol,
                            side=side,
                            broker_order_id=old_order_id,
                            kept_broker_order_id=keep_order_id,
                            order_ref=str(getattr(old, "order_ref", "") or ""),
                        )
                    except Exception as exc:
                        logger.warning(
                            "manual_flatten_duplicate_orphan_cancel_failed",
                            strategy_id=strategy_id,
                            symbol=symbol,
                            side=side,
                            broker_order_id=getattr(old, "order_id", None),
                            error=str(exc),
                        )
                # Keep only the newest one in the adoption list; cancelled ones should disappear next cycle.
                orphans = [o for o in orphans if o is keep or (o not in items_sorted[:-1])]

        for orphan in orphans:
            try:
                # Extract basic info from PlacedOrder model
                broker_order_id = str(orphan.order_id)
                perm_id = getattr(orphan, 'perm_id', None)
                perm_id_norm = self._normalize_perm_id(perm_id)
                try:
                    perm_id_db = int(perm_id_norm) if perm_id_norm is not None else None
                except Exception:
                    perm_id_db = None
                symbol = getattr(orphan, 'symbol', 'UNKNOWN')
                side = getattr(orphan, 'side', 'BUY')
                qty = float(getattr(orphan, 'qty', 0.0))
                order_type = getattr(orphan, 'order_type', 'MKT')
                
                status_str = getattr(orphan, 'status', 'Submitted')
                state = self._map_ib_status_to_db_state(status_str)

                order_ref = str(getattr(orphan, "order_ref", "") or "").strip()

                # Try to match an existing DB row by order_ref (or note.extras.order_ref prefix).
                db_match = None
                if order_ref:
                    try:
                        row = await self.pg.fetchone(
                            """
                            SELECT order_id, strategy_id, state, idempotency_key
                            FROM core.core_order
                            WHERE order_ref = $1
                               OR COALESCE(
                                      substring(COALESCE(note, '') FROM '"order_ref"\\s*:\\s*"([^"]+)"'),
                                      ''
                                  ) LIKE ($1 || '%')
                               OR COALESCE(note, '') LIKE ('%' || $1 || '%')
                            ORDER BY updated_at DESC
                            LIMIT 1
                            """,
                            order_ref,
                        )
                        db_match = dict(row) if row else None
                    except Exception as exc:
                        logger.warning("orphan_db_match_failed", broker_order_id=broker_order_id, error=str(exc))
                        db_match = None

                # If DB already has a terminal verdict for this order_ref, prefer cancelling the broker order.
                if db_match is not None:
                    db_state = str(db_match.get("state") or "").upper()
                    if db_state in {"FILLED", "CANCELLED", "REJECTED"} and state in {"SUBMITTED", "NEW"}:
                        terminal_orphan_resolved = False
                        if hasattr(self.adapter, "cancel"):
                            try:
                                logger.warning(
                                    "orphan_db_terminal_cancel_attempt",
                                    broker_order_id=broker_order_id,
                                    perm_id=perm_id_db,
                                    order_ref=order_ref,
                                    strategy_id=str(db_match.get("strategy_id") or ""),
                                    db_order_id=str(db_match.get("order_id") or ""),
                                    db_state=db_state,
                                    ib_state=state,
                                    symbol=symbol,
                                    side=side,
                                    qty=float(qty),
                                    order_type=str(order_type),
                                    live_adapter_client_id=getattr(self.adapter, "client_id", None),
                                )
                                self._emit_audit(
                                    stage="orphan_cancel_attempted",
                                    severity="warning",
                                    broker_order_id=broker_order_id,
                                    perm_id=perm_id_db,
                                    symbol=symbol,
                                    status="attempted",
                                    reason="db_terminal_orphan",
                                    payload={
                                        "order_ref": order_ref,
                                        "db_state": db_state,
                                        "ib_state": state,
                                        "side": side,
                                        "qty": float(qty),
                                        "order_type": str(order_type),
                                    },
                                )
                                orphan_order_id = self._numeric_broker_order_id(getattr(orphan, "order_id", None))
                                cancel_ok = await self.adapter.cancel(orphan_order_id) if orphan_order_id is not None else False  # type: ignore[call-arg]

                                # Always verify against a forced fresh broker open-order snapshot.
                                # cancel(True) only means cancel request was submitted, not confirmed terminal.
                                still_open: Optional[bool] = None
                                refreshed_sample: List[Dict[str, Any]] = []
                                if hasattr(self.adapter, "open_orders"):
                                    try:
                                        refreshed = await self.adapter.open_orders(force_refresh=True)  # type: ignore[call-arg]
                                        still_open = any(
                                            self._broker_order_id_matches(getattr(o, "order_id", None), getattr(orphan, "order_id", None))
                                            for o in (refreshed or [])
                                        )
                                        refreshed_sample = [
                                            {
                                                "order_id": str(getattr(o, "order_id", "") or ""),
                                                "symbol": str(getattr(o, "symbol", "") or ""),
                                                "status": str(getattr(o, "status", "") or ""),
                                                "side": str(getattr(o, "side", "") or ""),
                                                "order_type": str(getattr(o, "order_type", "") or ""),
                                                "perm_id": getattr(o, "perm_id", None),
                                                "order_ref": str(getattr(o, "order_ref", "") or ""),
                                            }
                                            for o in (refreshed or [])[:10]
                                        ]
                                        logger.info(
                                            "orphan_post_cancel_refresh_snapshot",
                                            broker_order_id=broker_order_id,
                                            order_ref=order_ref,
                                            refresh_count=len(refreshed or []),
                                            still_open=still_open,
                                            sample=refreshed_sample,
                                        )
                                    except Exception as refresh_exc:
                                        logger.warning(
                                            "orphan_post_cancel_refresh_failed",
                                            broker_order_id=broker_order_id,
                                            order_ref=order_ref,
                                            error=str(refresh_exc),
                                        )

                                if still_open is False:
                                    suppress_key = self._suppress_key_from_broker_order_id(getattr(orphan, "order_id", None))
                                    if suppress_key is not None:
                                        self._orphan_suppress_until[suppress_key] = time.time() + 3600.0
                                    logger.info(
                                        "orphan_absent_after_cancel_attempt_db_terminal",
                                        broker_order_id=broker_order_id,
                                        order_ref=order_ref,
                                        db_state=db_state,
                                        ib_state=state,
                                        symbol=symbol,
                                        cancel_ok=bool(cancel_ok),
                                    )
                                    self._emit_audit(
                                        stage="orphan_cancel_confirmed",
                                        broker_order_id=broker_order_id,
                                        perm_id=perm_id_db,
                                        symbol=symbol,
                                        status="confirmed",
                                        reason="db_terminal_orphan",
                                        payload={
                                            "order_ref": order_ref,
                                            "db_state": db_state,
                                            "ib_state": state,
                                            "cancel_ok": bool(cancel_ok),
                                        },
                                    )
                                    terminal_orphan_resolved = True
                                else:
                                    suppress_key = self._suppress_key_from_broker_order_id(getattr(orphan, "order_id", None))
                                    if suppress_key is not None:
                                        self._orphan_suppress_until[suppress_key] = time.time() + 30.0
                                    probe_found = None
                                    probe_owner_client_id = None
                                    probe_status = None
                                    probe_perm_id = None
                                    probe_why_held = None
                                    probe_client_id = None
                                    probe_error = None
                                    if hasattr(self.adapter, "probe_order_open_fresh"):
                                        try:
                                            orphan_order_id = self._numeric_broker_order_id(getattr(orphan, "order_id", None))
                                            probe = await self.adapter.probe_order_open_fresh(orphan_order_id) if orphan_order_id is not None else {}  # type: ignore[attr-defined]
                                            probe_found = probe.get("found")
                                            probe_owner_client_id = probe.get("owner_client_id")
                                            probe_status = probe.get("status")
                                            probe_perm_id = probe.get("perm_id")
                                            probe_why_held = probe.get("why_held")
                                            probe_client_id = probe.get("probe_client_id")
                                            probe_error = probe.get("error")
                                        except Exception as probe_exc:
                                            probe_error = str(probe_exc)
                                    if (
                                        probe_found is True
                                        and probe_owner_client_id is not None
                                        and int(probe_owner_client_id) != int(getattr(self.adapter, "client_id", -1) or -1)
                                    ):
                                        try:
                                            cancel_with_owner = getattr(self.adapter, "cancel_with_owner", None)
                                            if callable(cancel_with_owner):
                                                targeted_cancel_ok = await cancel_with_owner(
                                                    orphan_order_id,
                                                    owner_client_id=int(probe_owner_client_id),
                                                    perm_id=probe_perm_id,
                                                    order_ref=order_ref,
                                                )
                                            else:
                                                targeted_cancel_ok = False
                                        except Exception as targeted_cancel_exc:
                                            targeted_cancel_ok = False
                                            logger.warning(
                                                "orphan_owner_client_cancel_failed",
                                                broker_order_id=broker_order_id,
                                                order_ref=order_ref,
                                                owner_client_id=probe_owner_client_id,
                                                error=str(targeted_cancel_exc),
                                            )
                                        if targeted_cancel_ok and hasattr(self.adapter, "open_orders"):
                                            try:
                                                refreshed = await self.adapter.open_orders(force_refresh=True)  # type: ignore[call-arg]
                                                still_open = any(
                                                    self._broker_order_id_matches(getattr(o, "order_id", None), getattr(orphan, "order_id", None))
                                                    for o in (refreshed or [])
                                                )
                                                refreshed_sample = [
                                                    {
                                                        "order_id": str(getattr(o, "order_id", "") or ""),
                                                        "symbol": str(getattr(o, "symbol", "") or ""),
                                                        "status": str(getattr(o, "status", "") or ""),
                                                        "side": str(getattr(o, "side", "") or ""),
                                                        "order_type": str(getattr(o, "order_type", "") or ""),
                                                        "perm_id": getattr(o, "perm_id", None),
                                                        "order_ref": str(getattr(o, "order_ref", "") or ""),
                                                    }
                                                    for o in (refreshed or [])[:10]
                                                ]
                                            except Exception as refresh_exc:
                                                logger.warning(
                                                    "orphan_owner_client_cancel_refresh_failed",
                                                    broker_order_id=broker_order_id,
                                                    order_ref=order_ref,
                                                    owner_client_id=probe_owner_client_id,
                                                    error=str(refresh_exc),
                                                )
                                            try:
                                                orphan_order_id = self._numeric_broker_order_id(getattr(orphan, "order_id", None))
                                                probe = await self.adapter.probe_order_open_fresh(orphan_order_id) if orphan_order_id is not None else {}  # type: ignore[attr-defined]
                                                probe_found = probe.get("found")
                                                probe_owner_client_id = probe.get("owner_client_id")
                                                probe_status = probe.get("status")
                                                probe_perm_id = probe.get("perm_id")
                                                probe_why_held = probe.get("why_held")
                                                probe_client_id = probe.get("probe_client_id")
                                                probe_error = probe.get("error")
                                            except Exception as probe_exc:
                                                probe_error = str(probe_exc)
                                    if probe_found is False:
                                        # Live adapter may transiently keep a local PendingCancel/openTrades shadow
                                        # even after broker has removed the order. Fresh probe is the source of truth.
                                        suppress_key = self._suppress_key_from_broker_order_id(getattr(orphan, "order_id", None))
                                        if suppress_key is not None:
                                            self._orphan_suppress_until[suppress_key] = time.time() + 3600.0
                                        logger.info(
                                            "orphan_absent_after_cancel_attempt_fresh_probe_db_terminal",
                                            broker_order_id=broker_order_id,
                                            order_ref=order_ref,
                                            db_state=db_state,
                                            ib_state=state,
                                            symbol=symbol,
                                            cancel_ok=bool(cancel_ok),
                                            still_open=still_open,
                                            fresh_probe_client_id=probe_client_id,
                                        )
                                        self._emit_audit(
                                            stage="orphan_cancel_confirmed",
                                            broker_order_id=broker_order_id,
                                            perm_id=perm_id_db,
                                            symbol=symbol,
                                            status="confirmed_via_fresh_probe",
                                            reason="db_terminal_orphan",
                                            payload={
                                                "order_ref": order_ref,
                                                "db_state": db_state,
                                                "ib_state": state,
                                                "cancel_ok": bool(cancel_ok),
                                                "still_open": still_open,
                                                "fresh_probe_found": False,
                                                "fresh_probe_client_id": probe_client_id,
                                            },
                                        )
                                        terminal_orphan_resolved = True
                                        continue
                                    logger.warning(
                                        "orphan_cancel_not_confirmed_db_terminal",
                                        broker_order_id=broker_order_id,
                                        order_ref=order_ref,
                                        db_state=db_state,
                                        ib_state=state,
                                        symbol=symbol,
                                        cancel_ok=bool(cancel_ok),
                                        still_open=still_open,
                                        live_adapter_client_id=getattr(self.adapter, "client_id", None),
                                        fresh_probe_found=probe_found,
                                        fresh_probe_owner_client_id=probe_owner_client_id,
                                        fresh_probe_status=probe_status,
                                        fresh_probe_perm_id=probe_perm_id,
                                        fresh_probe_why_held=probe_why_held,
                                        fresh_probe_client_id=probe_client_id,
                                        fresh_probe_error=probe_error,
                                        post_cancel_refresh_sample=refreshed_sample,
                                    )
                                    self._emit_audit(
                                        stage="orphan_cancel_not_confirmed",
                                        severity="warning",
                                        broker_order_id=broker_order_id,
                                        perm_id=perm_id_db,
                                        symbol=symbol,
                                        status="unresolved",
                                        reason="db_terminal_orphan",
                                        payload={
                                            "order_ref": order_ref,
                                            "db_state": db_state,
                                            "ib_state": state,
                                            "cancel_ok": bool(cancel_ok),
                                            "still_open": still_open,
                                            "fresh_probe_found": probe_found,
                                            "fresh_probe_owner_client_id": probe_owner_client_id,
                                            "fresh_probe_status": probe_status,
                                            "fresh_probe_perm_id": probe_perm_id,
                                            "fresh_probe_why_held": probe_why_held,
                                            "fresh_probe_client_id": probe_client_id,
                                            "fresh_probe_error": probe_error,
                                            "post_cancel_refresh_sample": refreshed_sample,
                                        },
                                    )
                                    await self._quarantine_terminal_orphan(
                                        strategy_id=str(db_match.get("strategy_id") or ""),
                                        symbol=symbol,
                                        broker_order_id=broker_order_id,
                                        order_ref=order_ref,
                                        db_state=db_state,
                                        ib_state=state,
                                        perm_id=perm_id_db,
                                    )
                                    unresolved_orphans.append(orphan)
                            except Exception as exc:
                                logger.warning(
                                    "orphan_cancel_failed_db_terminal",
                                    broker_order_id=broker_order_id,
                                    order_ref=order_ref,
                                    error=str(exc),
                                )
                                self._emit_audit(
                                    stage="orphan_cancel_not_confirmed",
                                    severity="warning",
                                    broker_order_id=broker_order_id,
                                    perm_id=perm_id_db,
                                    symbol=symbol,
                                    status="cancel_failed",
                                    reason="db_terminal_orphan",
                                    payload={"order_ref": order_ref, "error": str(exc)},
                                )
                                await self._quarantine_terminal_orphan(
                                    strategy_id=str(db_match.get("strategy_id") or ""),
                                    symbol=symbol,
                                    broker_order_id=broker_order_id,
                                    order_ref=order_ref,
                                    db_state=db_state,
                                    ib_state=state,
                                    perm_id=perm_id_db,
                                    error=str(exc),
                                )
                                unresolved_orphans.append(orphan)
                        else:
                            logger.warning(
                                "orphan_db_terminal_without_cancel_adapter",
                                broker_order_id=broker_order_id,
                                order_ref=order_ref,
                                db_state=db_state,
                                ib_state=state,
                                symbol=symbol,
                            )
                            await self._quarantine_terminal_orphan(
                                strategy_id=str(db_match.get("strategy_id") or ""),
                                symbol=symbol,
                                broker_order_id=broker_order_id,
                                order_ref=order_ref,
                                db_state=db_state,
                                ib_state=state,
                                perm_id=perm_id_db,
                                error="cancel_adapter_missing",
                            )
                            unresolved_orphans.append(orphan)

                        if terminal_orphan_resolved:
                            continue
                        # Terminal DB verdict exists. Never adopt these rows into adopted_reconciler;
                        # keep them unresolved for operator attention if cancellation was not confirmed.
                        continue

                    # Non-terminal DB row exists: sync identity onto it instead of inserting a new adopted row.
                    try:
                        await self.pg.execute(
                            """
                            UPDATE core.core_order
                            SET broker_order_id = $1,
                                ib_perm_id = $2,
                                order_ref = COALESCE(order_ref, $3),
                                state = CASE WHEN state IN ('NEW', 'SUBMITTED', 'PARTIAL') THEN $4 ELSE state END,
                                updated_at = now()
                            WHERE order_id = $5
                            """,
                            broker_order_id,
                            perm_id_db,
                            order_ref,
                            state,
                            str(db_match.get("order_id")),
                        )
                        logger.info(
                            "Orphan order matched existing DB row and synced identity",
                            broker_order_id=broker_order_id,
                            order_ref=order_ref,
                            symbol=symbol,
                            side=side,
                            qty=qty,
                            state=state,
                            db_order_id=str(db_match.get("order_id")),
                            strategy_id=str(db_match.get("strategy_id") or ""),
                        )
                        continue
                    except Exception as exc:
                        logger.warning(
                            "orphan_db_sync_failed_fallback_insert",
                            broker_order_id=broker_order_id,
                            order_ref=order_ref,
                            error=str(exc),
                        )

                # Heuristic to find strategy (fallback)
                strategy_id = 'adopted_reconciler'
                
                note_data = json.dumps({
                    "adopted_by_reconciler": True,
                    "ib_perm_id": perm_id_norm,
                    "ib_status": str(status_str),
                    "order_ref": order_ref,
                    "timestamp": datetime.now().isoformat()
                })
                adopted_idem = _build_adopted_idempotency_key(
                    broker_order_id=str(broker_order_id),
                    perm_id=perm_id_db,
                    order_ref=str(order_ref or ""),
                )

                # Insert into DB
                # Idempotent orphan adoption: repeated reconciler scans for the same broker orphan
                # must not fail with duplicate idempotency keys.
                await self.pg.execute(
                    """
                    INSERT INTO core.core_order (
                        order_id, strategy_id, symbol, idempotency_key,
                        broker_order_id, ib_perm_id, side, order_type, qty,
                        state, order_ref, note, created_at, updated_at
                    ) VALUES (
                        gen_random_uuid(), $1, $2, $3,
                        $4, $5, $6, $7, $8,
                        $9, $10, $11, now(), now()
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE
                    SET
                        broker_order_id = COALESCE(core.core_order.broker_order_id, EXCLUDED.broker_order_id),
                        ib_perm_id = COALESCE(core.core_order.ib_perm_id, EXCLUDED.ib_perm_id),
                        order_ref = COALESCE(core.core_order.order_ref, EXCLUDED.order_ref),
                        state = CASE
                            WHEN core.core_order.state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                                THEN EXCLUDED.state
                            ELSE core.core_order.state
                        END,
                        updated_at = now()
                    """,
                    strategy_id, symbol, adopted_idem,
                    broker_order_id, perm_id_db, side, order_type, qty,
                    state, order_ref or None, note_data
                )

                logger.info("Orphan order adopted and synced to DB",
                           broker_order_id=broker_order_id,
                           symbol=symbol,
                           side=side,
                           qty=qty,
                           state=state)

            except Exception as e:
                unresolved_orphans.append(orphan)
                logger.error("Failed to adopt orphan order",
                            broker_order_id=getattr(orphan, 'order_id', 'unknown'),
                            error=str(e))

        return unresolved_orphans

    def get_stats(self) -> Dict[str, Any]:
        """Get reconciliation statistics"""
        return self.stats.copy()
