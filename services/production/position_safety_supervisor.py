from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract
from services.production.order_safety_sanity import (
    is_safety_like_order,
    order_limit_px,
    order_stop_px,
    positive_float,
    validate_market_price_band,
)
from services.production.broker_truth_snapshot_service import (
    build_live_broker_truth_snapshot_from_manager,
    write_live_broker_truth_snapshot_artifact,
)
from services.production.recovery_incident_service import RecoveryIncidentService
from services.exec.protection_lifecycle import (
    ProtectionLifecycleState,
    registry_row_to_lifecycle_record,
)
from services.reconciliation.position_reconciliation import (
    BrokerAccountPosition,
    StrategyVirtualPosition,
    detect_overnight_carry_after_restart,
)

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class BrokerPositionSafetyRecord:
    symbol: str
    qty: float
    classified_as: str
    protected_or_flattening: bool
    active_order_count: int = 0
    active_quarantine: bool = False
    active_manual_lock_count: int = 0
    scoped_manual_lock_count: int = 0
    recovery_submitted: bool = False
    recovery_incident_id: str = ""
    contract_strategy_ids: tuple[str, ...] = ()
    manual_lock_ids: tuple[str, ...] = ()
    scoped_manual_lock_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class FlatAccountLiveOrderSafetyRecord:
    symbol: str
    side: str
    qty: float
    broker_order_id: str = ""
    order_ref: str = ""
    status: str = ""
    reason: str = "flat_account_live_close_order"


@dataclass(frozen=True)
class UnsafeLiveOrderSafetyRecord:
    symbol: str
    side: str
    qty: float
    expected_side: str
    broker_order_id: str = ""
    order_ref: str = ""
    status: str = ""
    reason: str = "live_safety_order_opens_or_adds_position"


@dataclass(frozen=True)
class PositionSafetyDependencyResult:
    state: str
    ok: bool
    go_no_go: str
    lookup_failed: bool
    blocker: str
    reason: str
    cleanup_allowed: bool
    dependency_state: dict[str, Any]


def validate_position_safety_broker_truth_dependency(
    broker_truth: dict[str, Any] | None,
    *,
    now: datetime,
    ttl_sec: float = 90.0,
    required: bool = True,
) -> PositionSafetyDependencyResult:
    snapshot = dict(broker_truth or {})
    generated_at_s = str(snapshot.get("generated_at") or "").strip()
    generated_at = _parse_iso_datetime(generated_at_s)
    age_sec = None
    if generated_at is not None:
        age_sec = max(0.0, (now.astimezone(ET) - generated_at.astimezone(ET)).total_seconds())
    dependency_state = {
        "broker_truth_required": bool(required),
        "broker_truth_available": bool(snapshot),
        "broker_truth_backed": bool(snapshot.get("broker_truth_backed")),
        "broker_truth_source": str(snapshot.get("source") or snapshot.get("truth_source") or ""),
        "broker_truth_snapshot_id": _broker_truth_snapshot_id(snapshot),
        "broker_truth_generated_at": generated_at_s,
        "broker_truth_age_sec": age_sec,
        "broker_truth_lookup_failed": bool(snapshot.get("lookup_failed")),
        "broker_truth_stale": bool(snapshot.get("stale")) or bool(age_sec is None or age_sec > float(ttl_sec)),
    }
    if not required and not snapshot:
        return PositionSafetyDependencyResult(
            state="NOT_REQUIRED",
            ok=True,
            go_no_go="go",
            lookup_failed=False,
            blocker="",
            reason="broker_truth_dependency_not_required",
            cleanup_allowed=True,
            dependency_state=dependency_state,
        )
    if not snapshot:
        return _dependency_result("MISSING", "broker_truth_missing", "broker_truth_missing", dependency_state)
    if str(snapshot.get("artifact_type") or "") != "live_broker_truth_snapshot":
        return _dependency_result("INVALID_SCHEMA", "broker_truth_invalid_schema", "broker_truth_invalid_schema", dependency_state)
    if bool(snapshot.get("lookup_failed")):
        return _dependency_result(
            "LOOKUP_FAILED",
            "broker_truth_lookup_failed",
            str(snapshot.get("error") or "broker_truth_lookup_failed"),
            dependency_state,
        )
    if bool(dependency_state["broker_truth_stale"]):
        return _dependency_result("STALE", "broker_truth_stale", "broker_truth_stale", dependency_state)
    if not bool(snapshot.get("broker_truth_backed")) or str(snapshot.get("source") or "") != "live_broker":
        return _dependency_result(
            "NOT_BROKER_BACKED",
            "broker_truth_not_broker_backed",
            "broker_truth_not_broker_backed",
            dependency_state,
        )
    account = str(snapshot.get("account") or "").strip()
    if not account or account.upper() == "UNKNOWN_ACCOUNT" or bool(snapshot.get("account_invalid")):
        return _dependency_result("INVALID_ACCOUNT", "broker_truth_invalid_account", "broker_truth_invalid_account", dependency_state)
    if not isinstance(snapshot.get("positions"), list) or not isinstance(snapshot.get("open_orders"), list):
        return _dependency_result("INVALID_SCHEMA", "broker_truth_invalid_schema", "broker_truth_invalid_schema", dependency_state)
    dependency_state["broker_truth_backed"] = True
    return PositionSafetyDependencyResult(
        state="OK",
        ok=True,
        go_no_go="go",
        lookup_failed=False,
        blocker="",
        reason="broker_truth_dependency_ok",
        cleanup_allowed=True,
        dependency_state=dependency_state,
    )


def _dependency_result(
    state: str,
    blocker: str,
    reason: str,
    dependency_state: dict[str, Any],
) -> PositionSafetyDependencyResult:
    return PositionSafetyDependencyResult(
        state=state,
        ok=False,
        go_no_go="no_go",
        lookup_failed=True,
        blocker=blocker,
        reason=reason,
        cleanup_allowed=False,
        dependency_state=dependency_state,
    )


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed
    except Exception:
        return None


def _broker_truth_snapshot_id(snapshot: dict[str, Any]) -> str:
    for key in ("snapshot_id", "source_event_id", "artifact_path"):
        value = str(snapshot.get(key) or "").strip()
        if value:
            return value
    generated_at = str(snapshot.get("generated_at") or "").strip()
    pid = str(snapshot.get("source_process_pid") or "").strip()
    if generated_at or pid:
        return f"broker_truth:{generated_at}:{pid}"
    return ""


def _repo_root_from_artifact_dir(path: Path) -> Path:
    artifact_dir = path if path.is_absolute() else Path.cwd() / path
    if len(artifact_dir.parts) >= 3 and artifact_dir.parts[-3:] == ("runtime", "readiness", "position_safety"):
        return artifact_dir.parents[2]
    return artifact_dir


class PositionSafetySupervisor:
    """Broker-position safety invariant.

    Invariant:
    every nonzero broker position must be protected, flattening, quarantined, or
    manually locked.  This supervisor is intentionally conservative; when it
    cannot prove safety it creates entry-blocking manual locks for the strategy
    ids that declare the symbol in their production data contract.
    """

    def __init__(
        self,
        *,
        pg: Any,
        ib_adapter: Any,
        manual_intervention_lock_store: Any,
        strategy_data_contracts: Any = (),
        recovery_order_manager: Any = None,
        recovery_incident_service: Any = None,
        live_broker_snapshot_provider: Any = None,
        require_broker_truth_dependency: bool | None = None,
        broker_truth_dependency_ttl_sec: float = 90.0,
        artifact_dir: str | Path = "runtime/readiness/position_safety",
        live_broker_truth_repo_root: str | Path | None = None,
        enabled: bool = True,
        poll_interval_sec: float = 30.0,
        fallback_strategy_id: str = "platform_position_safety",
        flat_live_safety_order_auto_cancel_enabled: bool = False,
        protection_convergence_window_sec: float = 45.0,
    ) -> None:
        self.pg = pg
        self.ib_adapter = ib_adapter
        self.manual_intervention_lock_store = manual_intervention_lock_store
        self.strategy_data_contracts = list(strategy_data_contracts or [])
        self.recovery_order_manager = recovery_order_manager
        self.live_broker_snapshot_provider = live_broker_snapshot_provider
        self.require_broker_truth_dependency = require_broker_truth_dependency
        self.broker_truth_dependency_ttl_sec = max(1.0, float(broker_truth_dependency_ttl_sec or 90.0))
        self.artifact_dir = Path(artifact_dir)
        self.live_broker_truth_repo_root = (
            Path(live_broker_truth_repo_root)
            if live_broker_truth_repo_root is not None
            else _repo_root_from_artifact_dir(self.artifact_dir)
        )
        self.recovery_incident_service = recovery_incident_service or RecoveryIncidentService(
            artifact_dir=self.artifact_dir.parent / "recovery_incidents"
        )
        self.enabled = bool(enabled)
        self.poll_interval_sec = max(5.0, float(poll_interval_sec or 30.0))
        self.fallback_strategy_id = str(fallback_strategy_id or "platform_position_safety").strip()
        self.flat_live_safety_order_auto_cancel_enabled = bool(flat_live_safety_order_auto_cancel_enabled)
        self.protection_convergence_window_sec = max(0.0, float(protection_convergence_window_sec or 0.0))
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._current_live_broker_snapshot: dict[str, Any] | None = None
        self._current_broker_positions: list[dict[str, Any]] | None = None
        self._current_broker_open_orders: list[dict[str, Any]] | None = None
        self._current_dependency_result: PositionSafetyDependencyResult | None = None
        self._run_lock = asyncio.Lock()

    async def start(self) -> None:
        if not self.enabled:
            logger.info("position_safety_supervisor_disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self.run_forever(), name="position_safety_supervisor")
        logger.info("position_safety_supervisor_started", poll_interval_sec=self.poll_interval_sec)

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self._running and self._task is not None and not self._task.done())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_forever(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("position_safety_supervisor_loop_error", error=str(exc), exc_info=True)
            await asyncio.sleep(self.poll_interval_sec)

    async def run_once(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        async with self._run_lock:
            now_et = (now or datetime.now(ET)).astimezone(ET)
            try:
                return await self._run_once_locked(now=now_et)
            except Exception as exc:
                self._write_failure_artifact(now_et=now_et, error=str(exc))
                logger.error(
                    "position_safety_supervisor_run_once_failed",
                    error=str(exc),
                    artifact_path=str(self.artifact_dir / "position_safety_latest.json"),
                    exc_info=True,
                )
                raise

    async def _run_once_locked(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        now_et = (now or datetime.now(ET)).astimezone(ET)
        self._current_live_broker_snapshot = None
        self._current_broker_positions = None
        self._current_broker_open_orders = None
        self._current_dependency_result = None
        records: list[BrokerPositionSafetyRecord] = []
        lookup_failed = False
        error = ""
        try:
            await self._prepare_live_broker_snapshot(now_et=now_et)
            broker_positions = await self._load_broker_positions()
        except Exception as exc:
            broker_positions = []
            lookup_failed = True
            error = str(exc)
            logger.error("position_safety_broker_truth_unavailable", error=error, exc_info=True)
        broker_open_orders_snapshot: list[dict[str, Any]] = []
        protection_registry_sync = await self._reconcile_protection_registry(
            broker_positions=broker_positions,
            broker_truth_available=not lookup_failed,
            now_et=now_et,
        )
        flat_live_orders: list[FlatAccountLiveOrderSafetyRecord] = []
        unsafe_live_orders: list[UnsafeLiveOrderSafetyRecord] = []
        if not lookup_failed:
            try:
                broker_open_orders_snapshot = await self._load_broker_open_orders(symbol="")
            except Exception as exc:
                broker_open_orders_snapshot = []
                lookup_failed = True
                error = error or str(exc)
                logger.error("position_safety_broker_open_orders_unavailable", error=str(exc), exc_info=True)
        if not lookup_failed:
            flat_live_order_cleanup = await self._cancel_flat_account_live_safety_orders(
                broker_positions=broker_positions,
                now_et=now_et,
            )
            unsafe_live_order_cleanup = await self._cancel_unsafe_live_safety_orders(
                broker_positions=broker_positions,
                now_et=now_et,
            )
            if int(flat_live_order_cleanup.get("cancelled_count") or 0) > 0 or int(
                unsafe_live_order_cleanup.get("cancelled_count") or 0
            ) > 0:
                broker_open_orders_snapshot = await self._load_broker_open_orders(symbol="")
            flat_live_orders = await self._flat_account_live_safety_orders(broker_positions=broker_positions)
            unsafe_live_orders = await self._unsafe_live_safety_orders(broker_positions=broker_positions)
            flat_manual_lock_cleanup = await self._clear_flat_position_manual_locks(broker_positions=broker_positions)
            core_position_residual_cleanup = await self._clear_flat_core_position_residuals(
                broker_positions=broker_positions,
                now_et=now_et,
            )
            flat_quarantine_cleanup = await self._resolve_flat_quarantines_after_broker_flat(
                broker_positions=broker_positions,
                broker_open_orders=broker_open_orders_snapshot,
                now_et=now_et,
            )
            protected_quarantine_cleanup = await self._resolve_protected_live_quarantines_after_owner_lane_verified(
                broker_positions=broker_positions,
                broker_open_orders=broker_open_orders_snapshot,
                now_et=now_et,
            )
        else:
            block_reason = str(
                (self._current_dependency_result.reason if self._current_dependency_result else "")
                or "broker_truth_unavailable"
            )
            flat_manual_lock_cleanup = {
                "status": "blocked",
                "cleared_count": 0,
                "skipped_count": 0,
                "error": "broker_truth_unavailable",
                "reason": block_reason,
            }
            flat_live_order_cleanup = {
                "status": "blocked",
                "cancelled_count": 0,
                "skipped_count": 0,
                "error": "broker_truth_unavailable",
                "reason": block_reason,
            }
            unsafe_live_order_cleanup = {
                "status": "blocked",
                "cancelled_count": 0,
                "skipped_count": 0,
                "error": "broker_truth_unavailable",
                "reason": block_reason,
            }
            core_position_residual_cleanup = {
                "status": "blocked",
                "cleared_count": 0,
                "skipped_count": 0,
                "lookup_failed": False,
                "error": "broker_truth_unavailable",
                "reason": block_reason,
            }
            flat_quarantine_cleanup = {
                "status": "blocked",
                "resolved_count": 0,
                "skipped_count": 0,
                "lookup_failed": False,
                "error": "broker_truth_unavailable",
                "reason": block_reason,
                "items": [],
            }
            protected_quarantine_cleanup = {
                "status": "blocked",
                "resolved_count": 0,
                "skipped_count": 0,
                "lookup_failed": False,
                "error": "broker_truth_unavailable",
                "reason": block_reason,
                "items": [],
            }

        for position in broker_positions:
            record = await self._classify_position(position=position, now_et=now_et)
            records.append(record)

        protected_symbols = {record.symbol for record in records if record.protected_or_flattening}
        overnight_carry = await self._detect_overnight_carry(
            broker_positions=broker_positions,
            broker_truth_available=not lookup_failed,
            now_et=now_et,
            protected_symbols=protected_symbols,
        )

        active_quarantine = await self._active_quarantine_summary()
        active_quarantine_count = int(active_quarantine.get("active_quarantine_count") or 0)
        active_quarantine_lookup_failed = bool(active_quarantine.get("lookup_failed"))
        recovery_incidents = await self._update_recovery_incidents(
            records=records,
            active_quarantine=active_quarantine,
            now_et=now_et,
        )
        core_position_residual_lookup_failed = bool(core_position_residual_cleanup.get("lookup_failed"))
        unsafe = [
            record
            for record in records
            if (
                not record.protected_or_flattening
                and record.classified_as != "protection_converging"
                and not record.active_quarantine
                and record.active_manual_lock_count <= 0
                and not record.recovery_submitted
            )
        ]
        recovery_submitted = [record for record in records if record.recovery_submitted]
        quarantined_manual_required = [
            record for record in records if record.active_quarantine and not record.protected_or_flattening
        ]
        active_manual_lock_count = sum(int(record.active_manual_lock_count or 0) for record in records)
        scoped_manual_lock_count = sum(int(record.scoped_manual_lock_count or 0) for record in records)
        broker_account = str(
            (self._current_live_broker_snapshot or {}).get("account")
            or getattr(self.ib_adapter, "account", "")
            or ""
        ).strip()
        broker_account_invalid = not broker_account or broker_account.upper() == "UNKNOWN_ACCOUNT"
        live_snapshot = dict(self._current_live_broker_snapshot or {})
        if not live_snapshot:
            live_snapshot = {
                "artifact_type": "live_broker_truth_snapshot",
                "artifact_present": True,
                "generated_at": now_et.isoformat(),
                "source_process_pid": os.getpid(),
                "account": broker_account,
                "account_invalid": broker_account_invalid,
                "lookup_failed": bool(lookup_failed),
                "stale": False,
                "go_no_go": "no_go" if (lookup_failed or broker_account_invalid) else "go",
                "error": error,
                "broker_position_count": len(broker_positions),
                "broker_open_order_count": len(broker_open_orders_snapshot),
                "positions": broker_positions,
                "open_orders": broker_open_orders_snapshot,
            }
        else:
            live_snapshot.setdefault("artifact_type", "live_broker_truth_snapshot")
            live_snapshot.setdefault("artifact_present", True)
            live_snapshot.setdefault("generated_at", now_et.isoformat())
            live_snapshot.setdefault("source_process_pid", os.getpid())
            live_snapshot["account"] = broker_account
            live_snapshot["account_invalid"] = broker_account_invalid
            live_snapshot["lookup_failed"] = bool(lookup_failed or live_snapshot.get("lookup_failed"))
            live_snapshot["stale"] = bool(live_snapshot.get("stale", False))
            live_snapshot["go_no_go"] = "no_go" if (lookup_failed or broker_account_invalid) else str(
                live_snapshot.get("go_no_go") or "go"
            )
            live_snapshot["error"] = error or str(live_snapshot.get("error") or "")
            live_snapshot["broker_position_count"] = len(broker_positions)
            live_snapshot["broker_open_order_count"] = len(broker_open_orders_snapshot)
            live_snapshot["positions"] = broker_positions
            live_snapshot["open_orders"] = broker_open_orders_snapshot
        dependency_result = self._current_dependency_result or validate_position_safety_broker_truth_dependency(
            live_snapshot,
            now=now_et,
            ttl_sec=self.broker_truth_dependency_ttl_sec,
            required=False,
        )
        blockers: list[str] = []
        warnings: list[str] = []
        if dependency_result.blocker:
            blockers.append(dependency_result.blocker)
        if broker_account_invalid:
            blockers.append("broker_account_invalid")
        if active_quarantine_lookup_failed:
            blockers.append("position_safety_active_quarantine_lookup_failed")
        if core_position_residual_lookup_failed:
            blockers.append("position_safety_core_position_residual_lookup_failed")
        if unsafe:
            blockers.append("unsafe_broker_position")
        if recovery_submitted:
            blockers.append("position_recovery_submitted")
        if flat_live_orders:
            blockers.append("flat_account_live_close_order")
        if unsafe_live_orders:
            blockers.append("live_safety_order_opens_or_adds_position")
        if active_quarantine_count > 0:
            blockers.append("active_order_or_position_quarantine")
        if active_manual_lock_count > 0:
            blockers.append("active_manual_intervention_lock")
        if int(overnight_carry.get("incident_count") or 0) > 0:
            blockers.append("overnight_carry")
        core_position_residual_cleanup.setdefault(
            "status",
            "failed" if bool(core_position_residual_cleanup.get("lookup_failed")) else "clean",
        )
        artifact = {
            "artifact_type": "position_safety",
            "generated_at": now_et.isoformat(),
            "live_broker_snapshot": live_snapshot,
            "dependency_state": dependency_result.dependency_state,
            "blockers": blockers,
            "warnings": warnings,
            "go_no_go": (
                "no_go"
                if (
                    lookup_failed
                    or broker_account_invalid
                    or active_quarantine_lookup_failed
                    or core_position_residual_lookup_failed
                    or unsafe
                    or recovery_submitted
                    or flat_live_orders
                    or unsafe_live_orders
                    or active_quarantine_count > 0
                    or active_manual_lock_count > 0
                    or int(overnight_carry.get("incident_count") or 0) > 0
                )
                else "go"
            ),
            "lookup_failed": lookup_failed,
            "error": error,
            "broker_account": broker_account,
            "broker_account_invalid": broker_account_invalid,
            "broker_position_count": len(broker_positions),
            "unsafe_position_count": len(unsafe),
            "recovery_submitted_count": len(recovery_submitted),
            "quarantined_manual_required_count": len(quarantined_manual_required),
            "flat_live_order_count": len(flat_live_orders),
            "unsafe_live_order_count": len(unsafe_live_orders),
            "active_manual_lock_count": active_manual_lock_count,
            "scoped_manual_lock_count": scoped_manual_lock_count,
            "active_quarantine_count": active_quarantine_count,
            "active_quarantine": active_quarantine,
            "recovery_incidents": recovery_incidents,
            "records": [asdict(record) for record in records],
            "quarantined_manual_required": [asdict(record) for record in quarantined_manual_required],
            "recovery_submitted": [asdict(record) for record in recovery_submitted],
            "flat_live_orders": [asdict(record) for record in flat_live_orders],
            "flat_live_order_cleanup": flat_live_order_cleanup,
            "unsafe_live_orders": [asdict(record) for record in unsafe_live_orders],
            "unsafe_live_order_cleanup": unsafe_live_order_cleanup,
            "overnight_carry": overnight_carry,
            "protection_registry_sync": protection_registry_sync,
            "flat_manual_lock_cleanup": flat_manual_lock_cleanup,
            "core_position_residual_cleanup": core_position_residual_cleanup,
            "flat_quarantine_cleanup": flat_quarantine_cleanup,
            "protected_quarantine_cleanup": protected_quarantine_cleanup,
        }
        self._write_artifact(artifact)
        if artifact["go_no_go"] != "go":
            logger.error(
                "position_safety_no_go",
                lookup_failed=lookup_failed,
                unsafe_position_count=len(unsafe),
                flat_live_order_count=len(flat_live_orders),
                unsafe_live_order_count=len(unsafe_live_orders),
                active_manual_lock_count=active_manual_lock_count,
                active_quarantine_count=active_quarantine_count,
                active_quarantine_lookup_failed=active_quarantine_lookup_failed,
                core_position_residual_lookup_failed=core_position_residual_lookup_failed,
                broker_position_count=len(broker_positions),
            )
        return artifact

    def _write_failure_artifact(self, *, now_et: datetime, error: str) -> dict[str, Any]:
        dependency_state = (
            dict(self._current_dependency_result.dependency_state)
            if self._current_dependency_result is not None
            else {}
        )
        artifact = {
            "artifact_type": "position_safety",
            "generated_at": now_et.isoformat(),
            "live_broker_snapshot": dict(self._current_live_broker_snapshot or {}),
            "dependency_state": dependency_state,
            "blockers": ["position_safety_supervisor_exception"],
            "warnings": [],
            "go_no_go": "no_go",
            "lookup_failed": True,
            "error": str(error or "position_safety_supervisor_exception"),
            "broker_account": str(
                (self._current_live_broker_snapshot or {}).get("account")
                or getattr(self.ib_adapter, "account", "")
                or ""
            ).strip(),
            "broker_account_invalid": False,
            "broker_position_count": 0,
            "unsafe_position_count": 0,
            "recovery_submitted_count": 0,
            "quarantined_manual_required_count": 0,
            "flat_live_order_count": 0,
            "unsafe_live_order_count": 0,
            "active_manual_lock_count": 0,
            "scoped_manual_lock_count": 0,
            "active_quarantine_count": 0,
            "records": [],
        }
        self._write_artifact(artifact)
        return artifact

    async def _unsafe_live_safety_orders(
        self,
        *,
        broker_positions: list[dict[str, Any]],
    ) -> list[UnsafeLiveOrderSafetyRecord]:
        """Flag live safety-like orders that would add to or flip an existing broker position."""
        try:
            broker_open_orders = await self._load_broker_open_orders(symbol="")
        except Exception as exc:
            logger.warning("position_safety_unsafe_order_broker_open_orders_unavailable", error=str(exc))
            return []
        position_by_symbol = {
            str(item.get("symbol") or "").strip().upper(): float(item.get("qty") or 0.0)
            for item in broker_positions or []
            if str(item.get("symbol") or "").strip() and abs(float(item.get("qty") or 0.0)) > 1e-6
        }
        records: list[UnsafeLiveOrderSafetyRecord] = []
        for row in broker_open_orders:
            symbol = str(row.get("symbol") or "").strip().upper()
            qty_live = float(position_by_symbol.get(symbol, 0.0) or 0.0)
            if not symbol or abs(qty_live) <= 1e-6:
                continue
            if not self._is_broker_safety_like_order(row):
                durable = await self._durable_order_row_for_broker_order(
                    broker_order_id=str(row.get("broker_order_id") or row.get("order_id") or "").strip(),
                    order_ref=str(row.get("order_ref") or "").strip(),
                    symbol=symbol,
                )
                if not durable or not self._is_safety_like_durable_order(durable):
                    continue
            side = str(row.get("side") or "").strip().upper()
            expected_side = "SELL" if qty_live > 0 else "BUY"
            try:
                order_qty = abs(float(row.get("qty") or 0.0))
            except Exception:
                order_qty = 0.0
            qty_limit = await self._owner_lane_qty_limit_for_live_order(row)
            if qty_limit is None:
                qty_limit = abs(qty_live)
            if side == expected_side and order_qty <= abs(float(qty_limit or 0.0)) * 1.05 + 1e-6:
                continue
            records.append(
                UnsafeLiveOrderSafetyRecord(
                    symbol=symbol,
                    side=side,
                    qty=order_qty,
                    expected_side=expected_side,
                    broker_order_id=str(row.get("broker_order_id") or row.get("order_id") or "").strip(),
                    order_ref=str(row.get("order_ref") or "").strip(),
                    status=str(row.get("status") or "").strip(),
                )
            )
        return records

    async def _owner_lane_qty_limit_for_live_order(self, order: dict[str, Any]) -> Optional[float]:
        broker_order_id = str(order.get("broker_order_id") or order.get("order_id") or "").strip()
        durable: dict[str, Any] = {}
        if broker_order_id:
            durable = await self._durable_order_row_for_broker_order(
                broker_order_id=broker_order_id,
                order_ref=str(order.get("order_ref") or ""),
                symbol=str(order.get("symbol") or ""),
            ) or {}
        note = durable.get("note")
        if isinstance(note, str):
            try:
                note = json.loads(note)
            except Exception:
                note = {}
        if not isinstance(note, dict):
            note = {}
        protection_key = str(note.get("protection_key") or "").strip()
        key_parts = protection_key.split("|")
        account_id = str(note.get("account_id") or (key_parts[0] if len(key_parts) >= 1 else "") or "").strip()
        strategy_instance_id = str(
            note.get("strategy_instance_id")
            or durable.get("strategy_instance_id")
            or durable.get("strategy_id")
            or (key_parts[1] if len(key_parts) >= 2 else "")
            or ""
        ).strip()
        symbol = str(note.get("symbol") or durable.get("symbol") or order.get("symbol") or "").strip().upper()
        lot_id = str(note.get("position_lot_id") or (key_parts[3] if len(key_parts) >= 4 else "") or "").strip()
        if not strategy_instance_id or not symbol or not lot_id:
            return None
        try:
            row = await self.pg.fetchone(
                """
                SELECT ABS(COALESCE(qty, 0))::float8 AS owner_lane_qty
                FROM core.position_ledger_current
                WHERE COALESCE(NULLIF(strategy_instance_id, ''), strategy_id) = $1
                  AND symbol = $2
                  AND lot_id = $3
                  AND ($4::text = '' OR account_id = $4::text)
                LIMIT 1
                """,
                strategy_instance_id,
                symbol,
                lot_id,
                account_id,
            )
        except Exception as exc:
            logger.warning(
                "position_safety_owner_lane_qty_lookup_failed",
                broker_order_id=broker_order_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                lot_id=lot_id,
                error=str(exc),
            )
            return None
        try:
            lane_qty = abs(float((row or {}).get("owner_lane_qty") or 0.0))
        except Exception:
            return None
        return lane_qty if lane_qty > 0.0 else None

    async def _flat_account_live_safety_orders(
        self,
        *,
        broker_positions: list[dict[str, Any]],
    ) -> list[FlatAccountLiveOrderSafetyRecord]:
        """Flag live close/protection/flatten orders that could open a reverse position from flat."""
        try:
            broker_open_orders = await self._load_broker_open_orders(symbol="")
        except Exception as exc:
            logger.warning("position_safety_flat_order_broker_open_orders_unavailable", error=str(exc))
            return []
        position_by_symbol = {
            str(item.get("symbol") or "").strip().upper(): float(item.get("qty") or 0.0)
            for item in broker_positions or []
            if str(item.get("symbol") or "").strip()
        }
        records: list[FlatAccountLiveOrderSafetyRecord] = []
        for row in broker_open_orders:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if abs(float(position_by_symbol.get(symbol, 0.0) or 0.0)) > 1e-6:
                continue
            if not self._is_broker_safety_like_order(row):
                durable = await self._durable_order_row_for_broker_order(
                    broker_order_id=str(row.get("broker_order_id") or row.get("order_id") or "").strip(),
                    order_ref=str(row.get("order_ref") or "").strip(),
                    symbol=symbol,
                )
                if not durable or not self._is_safety_like_durable_order(durable):
                    continue
            try:
                qty = abs(float(row.get("qty") or 0.0))
            except Exception:
                qty = 0.0
            records.append(
                FlatAccountLiveOrderSafetyRecord(
                    symbol=symbol,
                    side=str(row.get("side") or "").strip().upper(),
                    qty=qty,
                    broker_order_id=str(row.get("broker_order_id") or row.get("order_id") or "").strip(),
                    order_ref=str(row.get("order_ref") or "").strip(),
                    status=str(row.get("status") or "").strip(),
                )
            )
        return records

    async def _cancel_flat_account_live_safety_orders(
        self,
        *,
        broker_positions: list[dict[str, Any]],
        now_et: datetime,
    ) -> dict[str, Any]:
        if not self.flat_live_safety_order_auto_cancel_enabled:
            return {"enabled": False, "cancelled_count": 0, "skipped_count": 0, "items": []}
        cancel_fn = getattr(self.ib_adapter, "cancel", None)
        if not callable(cancel_fn):
            return {
                "enabled": True,
                "cancelled_count": 0,
                "skipped_count": 0,
                "items": [],
                "reason": "adapter_cancel_unavailable",
            }
        candidates = await self._flat_account_live_safety_orders(broker_positions=broker_positions)
        cancelled = 0
        skipped = 0
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            item = asdict(candidate)
            broker_order_id = str(candidate.broker_order_id or "").strip()
            if not broker_order_id:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "missing_broker_order_id"
                items.append(item)
                continue
            durable = await self._durable_order_row_for_broker_order(
                broker_order_id=broker_order_id,
                order_ref=candidate.order_ref,
                symbol=candidate.symbol,
            )
            if not durable:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "durable_order_row_missing"
                items.append(item)
                continue
            durable_symbol = str(durable.get("symbol") or "").strip().upper()
            if durable_symbol != str(candidate.symbol or "").strip().upper():
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "durable_order_symbol_mismatch"
                items.append(item)
                continue
            if not self._is_safety_like_durable_order(durable):
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "durable_order_not_safety_like"
                items.append(item)
                continue
            broker_id_int = self._safe_int(broker_order_id)
            if broker_id_int is None:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "broker_order_id_not_numeric"
                items.append(item)
                continue
            try:
                cancel_ok = bool(await cancel_fn(broker_id_int))
            except Exception as exc:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "cancel_failed"
                item["error"] = str(exc)
                logger.warning(
                    "position_safety_flat_live_safety_order_cancel_failed",
                    broker_order_id=broker_order_id,
                    symbol=candidate.symbol,
                    error=str(exc),
                )
                items.append(item)
                continue
            self._current_broker_open_orders = None
            confirmed_absent = await self._broker_order_absent(broker_order_id=broker_order_id)
            if not confirmed_absent:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "cancel_not_confirmed"
                item["cancel_ok"] = cancel_ok
                items.append(item)
                continue
            await self._mark_durable_order_cancelled_if_working(
                broker_order_id=broker_order_id,
                now_et=now_et,
                reason="position_safety_flat_live_safety_order_auto_cancelled",
            )
            cancelled += 1
            item["status"] = "cancelled"
            item["cancel_ok"] = cancel_ok
            item["confirmed_absent"] = True
            items.append(item)
            logger.warning(
                "position_safety_flat_live_safety_order_auto_cancelled",
                broker_order_id=broker_order_id,
                symbol=candidate.symbol,
                order_ref=candidate.order_ref,
            )
        return {
            "enabled": True,
            "cancelled_count": cancelled,
            "skipped_count": skipped,
            "items": items,
        }

    async def _cancel_unsafe_live_safety_orders(
        self,
        *,
        broker_positions: list[dict[str, Any]],
        now_et: datetime,
    ) -> dict[str, Any]:
        if not self.flat_live_safety_order_auto_cancel_enabled:
            return {"enabled": False, "cancelled_count": 0, "skipped_count": 0, "items": []}
        cancel_fn = getattr(self.ib_adapter, "cancel", None)
        if not callable(cancel_fn):
            return {
                "enabled": True,
                "cancelled_count": 0,
                "skipped_count": 0,
                "items": [],
                "reason": "adapter_cancel_unavailable",
            }
        candidates = await self._unsafe_live_safety_orders(broker_positions=broker_positions)
        cancelled = 0
        skipped = 0
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            item = asdict(candidate)
            broker_order_id = str(candidate.broker_order_id or "").strip()
            if not broker_order_id:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "missing_broker_order_id"
                items.append(item)
                continue
            durable = await self._durable_order_row_for_broker_order(
                broker_order_id=broker_order_id,
                order_ref=candidate.order_ref,
                symbol=candidate.symbol,
            )
            if not durable:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "durable_order_row_missing"
                items.append(item)
                continue
            durable_symbol = str(durable.get("symbol") or "").strip().upper()
            if durable_symbol != str(candidate.symbol or "").strip().upper():
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "durable_order_symbol_mismatch"
                items.append(item)
                continue
            durable_side = str(durable.get("side") or "").strip().upper()
            if durable_side and durable_side != str(candidate.side or "").strip().upper():
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "durable_order_side_mismatch"
                items.append(item)
                continue
            if not self._is_safety_like_durable_order(durable):
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "durable_order_not_safety_like"
                items.append(item)
                continue
            broker_id_int = self._safe_int(broker_order_id)
            if broker_id_int is None:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "broker_order_id_not_numeric"
                items.append(item)
                continue
            try:
                cancel_ok = bool(await cancel_fn(broker_id_int))
            except Exception as exc:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "cancel_failed"
                item["error"] = str(exc)
                logger.warning(
                    "position_safety_unsafe_live_safety_order_cancel_failed",
                    broker_order_id=broker_order_id,
                    symbol=candidate.symbol,
                    error=str(exc),
                )
                items.append(item)
                continue
            self._current_broker_open_orders = None
            confirmed_absent = await self._broker_order_absent(broker_order_id=broker_order_id)
            if not confirmed_absent:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "cancel_not_confirmed"
                item["cancel_ok"] = cancel_ok
                items.append(item)
                continue
            await self._mark_durable_order_cancelled_if_working(
                broker_order_id=broker_order_id,
                now_et=now_et,
                reason="position_safety_unsafe_live_safety_order_auto_cancelled",
            )
            cancelled += 1
            item["status"] = "cancelled"
            item["cancel_ok"] = cancel_ok
            item["confirmed_absent"] = True
            items.append(item)
            logger.warning(
                "position_safety_unsafe_live_safety_order_auto_cancelled",
                broker_order_id=broker_order_id,
                symbol=candidate.symbol,
                order_ref=candidate.order_ref,
                expected_side=candidate.expected_side,
                side=candidate.side,
            )
        return {
            "enabled": True,
            "cancelled_count": cancelled,
            "skipped_count": skipped,
            "items": items,
        }

    async def _reconcile_protection_registry(
        self,
        *,
        broker_positions: list[dict[str, Any]],
        broker_truth_available: bool,
        now_et: datetime,
    ) -> dict[str, Any]:
        if self.pg is None:
            return {"enabled": False, "reason": "pg_unavailable"}
        position_by_symbol = {
            str(item.get("symbol") or "").strip().upper(): float(item.get("qty") or 0.0)
            for item in broker_positions or []
            if str(item.get("symbol") or "").strip()
        }
        try:
            rows = await self.pg.fetchall(
                """
                SELECT protection_key, strategy_id, symbol, lot_id, protection_kind, side,
                       desired_qty, broker_order_id, ib_perm_id, order_ref, state
                FROM core.core_protection_order_registry
                WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL', 'ACTIVE')
                LIMIT 500
                """
            )
        except Exception as exc:
            logger.warning("position_safety_protection_registry_lookup_failed", error=str(exc))
            return {"enabled": True, "lookup_failed": True, "error": str(exc), "updated_count": 0}

        if not rows:
            return {"enabled": True, "lookup_failed": False, "checked_count": 0, "updated_count": 0}

        broker_open_orders: list[dict[str, Any]] = []
        broker_open_orders_available = False
        try:
            broker_open_orders = await self._load_broker_open_orders(symbol="")
            broker_open_orders_available = True
        except Exception as exc:
            logger.warning("position_safety_registry_broker_open_orders_unavailable", error=str(exc))

        checked = 0
        updated = 0
        skipped = 0
        for raw in rows or []:
            row = dict(raw)
            checked += 1
            symbol = str(row.get("symbol") or "").strip().upper()
            broker_qty = float(position_by_symbol.get(symbol, 0.0) or 0.0)
            flat_symbol = bool(broker_truth_available and abs(broker_qty) <= 1e-6)
            broker_match = (
                broker_open_orders_available
                and self._registry_row_matches_broker_open_order(row, broker_open_orders=broker_open_orders)
            )
            if not flat_symbol and not broker_open_orders_available:
                skipped += 1
                continue
            if not flat_symbol and broker_match:
                continue
            if (
                not flat_symbol
                and self._protection_registry_row_recently_converging(row=row, now_et=now_et)
            ):
                skipped += 1
                continue
            reason = "flat_broker_position" if flat_symbol else "broker_open_order_missing"
            try:
                await self.pg.execute(
                    """
                    UPDATE core.core_protection_order_registry
                    SET state = 'CANCELLED',
                        degraded_reason = $2,
                        last_broker_sync_at = $3,
                        updated_at = now()
                    WHERE protection_key = $1
                      AND state IN ('NEW', 'SUBMITTED', 'PARTIAL', 'ACTIVE')
                    """,
                    str(row.get("protection_key") or ""),
                    reason,
                    now_et,
                )
                updated += 1
            except Exception as exc:
                logger.warning(
                    "position_safety_protection_registry_terminalize_failed",
                    protection_key=row.get("protection_key"),
                    symbol=symbol,
                    reason=reason,
                    error=str(exc),
                )
        return {
            "enabled": True,
            "lookup_failed": False,
            "broker_open_orders_available": broker_open_orders_available,
            "checked_count": checked,
            "updated_count": updated,
            "skipped_count": skipped,
        }

    async def _classify_position(self, *, position: dict[str, Any], now_et: datetime) -> BrokerPositionSafetyRecord:
        symbol = str(position.get("symbol") or "").strip().upper()
        qty = float(position.get("qty") or 0.0)
        owner_lanes = await self._ledger_owner_lanes_for_symbol(symbol=symbol)
        owner_lanes = self._owner_lanes_relevant_for_broker_position(owner_lanes=owner_lanes, broker_qty=qty)
        contract_strategy_ids = tuple(self._owner_lane_strategy_ids(owner_lanes)) or tuple(self._contract_strategy_ids(symbol))
        active_orders = await self._active_orders(
            symbol=symbol,
            broker_qty=qty,
            market_ref_px=positive_float(position.get("market_ref_px")),
            contract_strategy_ids=contract_strategy_ids,
        )
        active_quarantine = await self._has_active_quarantine(symbol=symbol)
        protection_converging = await self._protection_converging_for_symbol(
            symbol=symbol,
            qty=qty,
            contract_strategy_ids=contract_strategy_ids,
            now_et=now_et,
        )
        active_lock_count = 0
        scoped_lock_count = 0
        manual_lock_ids: list[str] = []
        scoped_manual_lock_ids: list[str] = []
        stale_auto_lock_ids: list[str] = []
        stale_auto_lock_ids.extend(
            await self._clear_non_owner_position_safety_manual_locks(
                symbol=symbol,
                owner_strategy_ids=set(contract_strategy_ids) if owner_lanes else set(),
                now_et=now_et,
            )
        )
        owner_lane_state = self._owner_lane_protection_state(owner_lanes=owner_lanes, active_orders=active_orders)
        if owner_lane_state:
            protected_or_flattening = bool(owner_lane_state["all_protected"])
            unprotected_lanes = list(owner_lane_state["unprotected_lanes"])
            recovery_strategy_ids = tuple(self._owner_lane_strategy_ids(unprotected_lanes))
            recovery_qty_by_strategy = {
                str(lane.get("strategy_instance_id") or lane.get("strategy_id") or "").strip(): abs(
                    float(lane.get("qty") or 0.0)
                )
                for lane in unprotected_lanes
            }
            recovery_owner_lanes = tuple(dict(lane) for lane in unprotected_lanes)
        else:
            active_order_qty = sum(abs(float(order.get("qty") or 0.0)) for order in active_orders)
            protected_or_flattening = bool(active_orders) and active_order_qty + 1e-6 >= abs(qty)
            recovery_strategy_ids = contract_strategy_ids
            recovery_qty_by_strategy = {}
            recovery_owner_lanes = ()
        incident: dict[str, Any] = {}
        for strategy_id in contract_strategy_ids:
            lock = await self._get_active_manual_lock(strategy_id=strategy_id, symbol=symbol)
            if not lock:
                continue
            lane_protected = bool(
                owner_lane_state
                and strategy_id in set(owner_lane_state.get("protected_strategy_ids") or ())
            )
            if (protected_or_flattening or lane_protected) and self._is_position_safety_auto_lock(lock):
                if await self._unlock_manual_lock(strategy_id=strategy_id, symbol=symbol):
                    stale_auto_lock_ids.append(
                        f"position_safety_manual_lock:{now_et.date().isoformat()}:{strategy_id}:{symbol}"
                    )
                continue
            if (protected_or_flattening or lane_protected) and self._is_resolved_synthetic_recovery_lock(lock):
                if await self._unlock_manual_lock(strategy_id=strategy_id, symbol=symbol):
                    stale_auto_lock_ids.append(
                        f"synthetic_recovery_manual_lock:{now_et.date().isoformat()}:{strategy_id}:{symbol}"
                    )
                continue
            if self._is_stale_aggregate_position_safety_lock(
                lock=lock,
                lane_qty=float(recovery_qty_by_strategy.get(strategy_id, 0.0) or 0.0),
            ):
                if await self._unlock_manual_lock(strategy_id=strategy_id, symbol=symbol):
                    stale_auto_lock_ids.append(
                        f"position_safety_manual_lock:{now_et.date().isoformat()}:{strategy_id}:{symbol}"
                    )
                continue
            if (protected_or_flattening or lane_protected) and self._is_owner_scoped_close_only_lock(lock):
                scoped_lock_count += 1
                scoped_manual_lock_ids.append(self._manual_lock_identity(lock, strategy_id=strategy_id, symbol=symbol))
                continue
            await self._refresh_awaiting_market_reopen_manual_lock_context(
                strategy_id=strategy_id,
                symbol=symbol,
                lock=lock,
                now_et=now_et,
            )
            active_lock_count += 1

        classified_as = "protected_or_flattening" if protected_or_flattening else "unclassified"
        reason = ""
        recovery_submitted = False
        recovery_incident_id = ""
        if active_quarantine:
            classified_as = "quarantined"
            if protected_or_flattening:
                reason = "active_quarantine_nonzero_position_pending_verified_flatten"
                incident = self._upsert_recovery_incident(
                    strategy_id=self._recovery_strategy_id(contract_strategy_ids),
                    symbol=symbol,
                    qty=qty,
                    reason=reason,
                    now_et=now_et,
                    active_quarantine=active_quarantine,
                    protected_or_flattening=protected_or_flattening,
                )
                recovery_incident_id = str(incident.get("incident_id") or "")
            else:
                reason = "active_quarantine_nonzero_position_requires_flatten_or_policy_repair"
                incident = self._upsert_recovery_incident(
                    strategy_id=self._recovery_strategy_id(contract_strategy_ids),
                    symbol=symbol,
                    qty=qty,
                    reason=reason,
                    now_et=now_et,
                    active_quarantine=active_quarantine,
                    protected_or_flattening=protected_or_flattening,
                )
                recovery_incident_id = str(incident.get("incident_id") or "")
                recovery_submitted = await self._submit_position_recovery_flatten(
                    symbol=symbol,
                    qty=self._recovery_qty_for_strategy(
                        aggregate_qty=qty,
                        strategy_ids=recovery_strategy_ids,
                        recovery_qty_by_strategy=recovery_qty_by_strategy,
                    ),
                    contract_strategy_ids=recovery_strategy_ids or contract_strategy_ids,
                    owner_lanes=recovery_owner_lanes,
                    now_et=now_et,
                    reason=reason,
                    recovery_incident_id=recovery_incident_id,
                )
                if recovery_submitted:
                    classified_as = "recovery_submitted"
                else:
                    manual_lock_ids = await self._create_manual_locks(
                        symbol=symbol,
                        qty=self._recovery_qty_for_strategy(
                            aggregate_qty=qty,
                            strategy_ids=recovery_strategy_ids,
                            recovery_qty_by_strategy=recovery_qty_by_strategy,
                        ),
                        contract_strategy_ids=recovery_strategy_ids or contract_strategy_ids,
                        now_et=now_et,
                        reason=reason,
                    )
                    active_lock_count = len(manual_lock_ids)
                    self._mark_recovery_manual_required(
                        strategy_id=self._recovery_strategy_id(contract_strategy_ids),
                        symbol=symbol,
                        now_et=now_et,
                        manual_lock_ids=manual_lock_ids,
                        reason=reason,
                    )
        elif active_lock_count > 0:
            classified_as = "manual_locked"
        elif protection_converging:
            classified_as = "protection_converging"
            reason = "protection_lifecycle_converging"
        elif not protected_or_flattening:
            reason = "broker_position_without_protection_flatten_quarantine_or_manual_lock"
            incident = self._upsert_recovery_incident(
                strategy_id=self._recovery_strategy_id(contract_strategy_ids),
                symbol=symbol,
                qty=qty,
                reason=reason,
                now_et=now_et,
                active_quarantine=active_quarantine,
                protected_or_flattening=protected_or_flattening,
            )
            recovery_incident_id = str(incident.get("incident_id") or "")
            recovery_submitted = await self._submit_position_recovery_flatten(
                symbol=symbol,
                qty=self._recovery_qty_for_strategy(
                    aggregate_qty=qty,
                    strategy_ids=recovery_strategy_ids,
                    recovery_qty_by_strategy=recovery_qty_by_strategy,
                ),
                contract_strategy_ids=recovery_strategy_ids or contract_strategy_ids,
                owner_lanes=recovery_owner_lanes,
                now_et=now_et,
                reason=reason,
                recovery_incident_id=recovery_incident_id,
            )
            if recovery_submitted:
                classified_as = "recovery_submitted"
            else:
                manual_lock_ids = await self._create_manual_locks(
                    symbol=symbol,
                    qty=self._recovery_qty_for_strategy(
                        aggregate_qty=qty,
                        strategy_ids=recovery_strategy_ids,
                        recovery_qty_by_strategy=recovery_qty_by_strategy,
                    ),
                    contract_strategy_ids=recovery_strategy_ids or contract_strategy_ids,
                    now_et=now_et,
                    reason=reason,
                )
                active_lock_count = len(manual_lock_ids)
                classified_as = "manual_locked"
                self._mark_recovery_manual_required(
                    strategy_id=self._recovery_strategy_id(contract_strategy_ids),
                    symbol=symbol,
                    now_et=now_et,
                    manual_lock_ids=manual_lock_ids,
                    reason=reason,
                )

        return BrokerPositionSafetyRecord(
            symbol=symbol,
            qty=qty,
            classified_as=classified_as,
            protected_or_flattening=protected_or_flattening,
            active_order_count=len(active_orders),
            active_quarantine=active_quarantine,
            active_manual_lock_count=active_lock_count,
            scoped_manual_lock_count=scoped_lock_count,
            recovery_submitted=recovery_submitted,
            recovery_incident_id=recovery_incident_id,
            contract_strategy_ids=contract_strategy_ids,
            manual_lock_ids=tuple(manual_lock_ids or stale_auto_lock_ids),
            scoped_manual_lock_ids=tuple(scoped_manual_lock_ids),
            reason=reason,
        )

    def _protection_registry_row_recently_converging(self, *, row: dict[str, Any], now_et: datetime) -> bool:
        if self.protection_convergence_window_sec <= 0.0:
            return False
        lifecycle = registry_row_to_lifecycle_record(row)
        if lifecycle.state not in {
            ProtectionLifecycleState.PROTECTION_REQUIRED.value,
            ProtectionLifecycleState.PROTECTION_SUBMITTING.value,
        }:
            return False
        updated_at = _parse_iso_datetime(str(row.get("updated_at") or row.get("first_seen_at") or ""))
        if updated_at is None:
            raw_dt = row.get("updated_at") or row.get("first_seen_at")
            if isinstance(raw_dt, datetime):
                updated_at = raw_dt
        if updated_at is None:
            return False
        age_sec = max(0.0, (now_et.astimezone(ET) - updated_at.astimezone(ET)).total_seconds())
        return age_sec <= self.protection_convergence_window_sec

    async def _protection_converging_for_symbol(
        self,
        *,
        symbol: str,
        qty: float,
        contract_strategy_ids: tuple[str, ...],
        now_et: datetime,
    ) -> bool:
        if self.pg is None or self.protection_convergence_window_sec <= 0.0:
            return False
        expected_side = "SELL" if float(qty or 0.0) > 0 else "BUY"
        try:
            rows = await self.pg.fetchall(
                """
                SELECT protection_key, strategy_id, strategy_instance_id, symbol, side,
                       desired_qty, state, first_seen_at, updated_at
                FROM core.core_protection_order_registry
                WHERE symbol = $1
                  AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                LIMIT 100
                """,
                str(symbol or "").strip().upper(),
            )
        except Exception as exc:
            logger.warning("position_safety_protection_convergence_lookup_failed", symbol=symbol, error=str(exc))
            return False
        strategy_set = {str(item or "").strip() for item in contract_strategy_ids if str(item or "").strip()}
        converging_qty = 0.0
        for raw in rows or []:
            row = dict(raw)
            row_strategy = str(row.get("strategy_instance_id") or row.get("strategy_id") or "").strip()
            if strategy_set and row_strategy not in strategy_set:
                continue
            if str(row.get("side") or "").strip().upper() != expected_side:
                continue
            if not self._protection_registry_row_recently_converging(row=row, now_et=now_et):
                continue
            try:
                converging_qty += abs(float(row.get("desired_qty") or 0.0))
            except Exception:
                continue
        return converging_qty + 1e-6 >= abs(float(qty or 0.0))

    async def _load_broker_positions(self) -> list[dict[str, Any]]:
        if self._current_broker_positions is not None:
            raw_positions = list(self._current_broker_positions)
        elif hasattr(self.ib_adapter, "positions"):
            raw_positions = self.ib_adapter.positions()
            if asyncio.iscoroutine(raw_positions):
                raw_positions = await raw_positions
        elif hasattr(self.ib_adapter, "ib"):
            raw_positions = self.ib_adapter.ib.positions()
            if asyncio.iscoroutine(raw_positions):
                raw_positions = await raw_positions
        else:
            raise RuntimeError("broker_position_adapter_unavailable")
        out: list[dict[str, Any]] = []
        account = str(getattr(self.ib_adapter, "account", "") or "").strip()
        for item in raw_positions or []:
            try:
                if isinstance(item, dict):
                    item_account = str(item.get("account") or "").strip()
                    if account and item_account and item_account != account:
                        continue
                    symbol = str(item.get("symbol") or "").strip().upper()
                    qty = float(item.get("qty", item.get("position", 0.0)) or 0.0)
                    market_ref = (
                        positive_float(item.get("market_ref_px"))
                        or positive_float(item.get("marketPrice"))
                        or positive_float(item.get("avgCost"))
                    )
                else:
                    if account and getattr(item, "account", "") and str(getattr(item, "account", "")) != account:
                        continue
                    symbol = self._position_symbol(item)
                    qty = float(getattr(item, "position", 0.0) or 0.0)
                    item_account = str(getattr(item, "account", "") or "").strip()
                    market_ref = (
                        positive_float(getattr(item, "marketPrice", None))
                        or positive_float(getattr(item, "market_price", None))
                        or positive_float(getattr(item, "last", None))
                        or positive_float(getattr(item, "avgCost", None))
                        or positive_float(getattr(item, "averageCost", None))
                    )
            except Exception:
                continue
            if symbol and abs(qty) > 1e-6:
                row = {"account": item_account, "symbol": symbol, "qty": qty}
                if market_ref is not None:
                    row["market_ref_px"] = market_ref
                out.append(row)
        return out

    async def _detect_overnight_carry(
        self,
        *,
        broker_positions: list[dict[str, Any]],
        broker_truth_available: bool,
        now_et: datetime,
        protected_symbols: set[str] | None = None,
    ) -> dict[str, Any]:
        broker_account = str(
            (self._current_live_broker_snapshot or {}).get("account")
            or getattr(self.ib_adapter, "account", "")
            or ""
        ).strip()
        virtual_positions = await self._load_strategy_virtual_positions()
        broker_account_positions = [
            BrokerAccountPosition(
                account_id=str(item.get("account") or broker_account or "").strip(),
                symbol=str(item.get("symbol") or "").strip().upper(),
                quantity=Decimal(str(item.get("qty") or 0)),
                avg_cost=(
                    Decimal(str(item.get("avgCost") or item.get("avg_cost")))
                    if item.get("avgCost") is not None or item.get("avg_cost") is not None
                    else None
                ),
                generated_at=now_et,
            )
            for item in broker_positions or []
            if str(item.get("symbol") or "").strip()
        ]
        incidents = detect_overnight_carry_after_restart(
            account_id=broker_account,
            broker_positions=broker_account_positions,
            virtual_positions=virtual_positions,
            strategy_policies=self._strategy_policy_map(),
            now=now_et,
            broker_truth_available=broker_truth_available,
        )
        protected_symbol_set = {str(item or "").strip().upper() for item in protected_symbols or set() if item}
        if protected_symbol_set:
            incidents = [
                item
                for item in incidents
                if not (
                    getattr(item.status, "value", item.status) == "UNKNOWN_OWNER"
                    and str(item.symbol or "").strip().upper() in protected_symbol_set
                )
            ]
        payloads = [self._overnight_incident_payload(item) for item in incidents]
        return {
            "detected": bool(payloads),
            "incident_count": len(payloads),
            "owner_resolved_count": sum(1 for item in payloads if item.get("status") == "OWNER_RESOLVED"),
            "unknown_owner_count": sum(1 for item in payloads if item.get("status") == "UNKNOWN_OWNER"),
            "ambiguous_owner_count": sum(1 for item in payloads if item.get("status") == "AMBIGUOUS_OWNER"),
            "manual_review_required_count": sum(1 for item in payloads if item.get("requires_manual_review")),
            "blocks_new_entry": bool(payloads),
            "close_only_allowed": True,
            "incidents": payloads,
        }

    async def _load_strategy_virtual_positions(self) -> list[StrategyVirtualPosition]:
        if self.pg is None:
            return []
        try:
            rows = await self.pg.fetchall(
                """
                SELECT account_id, strategy_id, strategy_instance_id, symbol, lot_id, qty
                FROM core.position_ledger_current
                WHERE abs(qty) > 0.000001
                  AND COALESCE(strategy_instance_id, '') <> ''
                LIMIT 1000
                """
            )
        except Exception as exc:
            logger.warning("position_safety_overnight_virtual_position_lookup_failed", error=str(exc))
            return []
        out: list[StrategyVirtualPosition] = []
        for raw in rows or []:
            row = dict(raw)
            account_id = str(row.get("account_id") or "").strip()
            strategy_id = str(row.get("strategy_id") or "").strip()
            strategy_instance_id = str(row.get("strategy_instance_id") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            lot_id = str(row.get("lot_id") or "").strip()
            if not account_id or not strategy_id or not strategy_instance_id or not symbol or not lot_id:
                continue
            out.append(
                StrategyVirtualPosition(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    strategy_instance_id=strategy_instance_id,
                    symbol=symbol,
                    lot_id=lot_id,
                    quantity=Decimal(str(row.get("qty") or 0)),
                )
            )
        return out

    def _strategy_policy_map(self) -> dict[str, Any]:
        policies: dict[str, Any] = {}
        for contract in self.strategy_data_contracts or []:
            if isinstance(contract, dict):
                strategy_id = str(contract.get("strategy_id") or contract.get("id") or "").strip()
                strategy_instance_id = str(contract.get("strategy_instance_id") or "").strip()
                policy = dict(contract.get("policy") or contract.get("eod_contract") or contract.get("params") or {})
                if "eod_contract" not in policy and isinstance(contract.get("eod_contract"), dict):
                    policy["eod_contract"] = contract.get("eod_contract")
            else:
                strategy_id = str(getattr(contract, "strategy_id", "") or getattr(contract, "id", "") or "").strip()
                strategy_instance_id = str(getattr(contract, "strategy_instance_id", "") or "").strip()
                policy = {
                    "hard_no_overnight": bool(getattr(contract, "hard_no_overnight", False)),
                    "intraday_flat_required": bool(getattr(contract, "intraday_flat_required", False)),
                    "eod_contract": getattr(contract, "eod_contract", None),
                }
            if strategy_id:
                policies[strategy_id] = policy
            if strategy_instance_id:
                policies[strategy_instance_id] = policy
        return policies

    @staticmethod
    def _overnight_incident_payload(incident: Any) -> dict[str, Any]:
        payload = asdict(incident)
        status = payload.get("status")
        payload["status"] = getattr(status, "value", status)
        detected_at = payload.get("detected_at")
        if isinstance(detected_at, datetime):
            payload["detected_at"] = detected_at.isoformat()
        for key in ("broker_quantity", "quantity"):
            if isinstance(payload.get(key), Decimal):
                payload[key] = str(payload[key])
        return payload

    async def _prepare_live_broker_snapshot(self, *, now_et: datetime) -> None:
        provider = self.live_broker_snapshot_provider
        snapshot: dict[str, Any] | None = None
        require_dependency = self._broker_truth_dependency_required()
        if provider is not None:
            value = provider(now_et=now_et) if callable(provider) else provider
            if asyncio.iscoroutine(value):
                value = await value
            snapshot = dict(value or {})
        elif bool(getattr(self.ib_adapter, "_managed_by_connection_manager", False)):
            snapshot = await build_live_broker_truth_snapshot_from_manager(
                generated_at=now_et,
                account=str(getattr(self.ib_adapter, "account", "") or "").strip() or None,
            )
        if snapshot is None:
            if require_dependency:
                result = validate_position_safety_broker_truth_dependency(
                    None,
                    now=now_et,
                    ttl_sec=self.broker_truth_dependency_ttl_sec,
                    required=True,
                )
                self._current_dependency_result = result
                raise RuntimeError(result.reason)
            return
        try:
            write_live_broker_truth_snapshot_artifact(repo_root=self.live_broker_truth_repo_root, status=snapshot)
        except Exception as exc:
            logger.warning("position_safety_live_broker_truth_artifact_write_failed", error=str(exc))
        self._current_live_broker_snapshot = snapshot
        result = validate_position_safety_broker_truth_dependency(
            snapshot,
            now=now_et,
            ttl_sec=self.broker_truth_dependency_ttl_sec,
            required=require_dependency,
        )
        self._current_dependency_result = result
        if not result.ok:
            raise RuntimeError(result.reason)
        positions = snapshot.get("positions")
        open_orders = snapshot.get("open_orders")
        if not isinstance(positions, list) or not isinstance(open_orders, list):
            raise RuntimeError("broker_truth_live_snapshot_invalid_shape")
        self._current_broker_positions = [dict(item) if isinstance(item, dict) else item for item in positions]
        self._current_broker_open_orders = [dict(item) if isinstance(item, dict) else item for item in open_orders]

    def _broker_truth_dependency_required(self) -> bool:
        if self.require_broker_truth_dependency is not None:
            return bool(self.require_broker_truth_dependency)
        return bool(
            self.live_broker_snapshot_provider is not None
            or getattr(self.ib_adapter, "_managed_by_connection_manager", False)
        )

    @staticmethod
    def _position_symbol(position: Any) -> str:
        contract = getattr(position, "contract", None)
        if contract is None:
            return ""
        sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
        if sec_type in {"OPT", "FOP"}:
            mapped = internal_opt_symbol_from_ib_contract(contract)
            return str(mapped or "").strip().upper()
        return str(getattr(contract, "symbol", "") or "").strip().upper()

    async def _active_orders(
        self,
        *,
        symbol: str,
        broker_qty: float,
        market_ref_px: Optional[float],
        contract_strategy_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        try:
            broker_open_orders = await self._load_broker_open_orders(symbol=symbol)
        except Exception as exc:
            logger.warning(
                "position_safety_broker_open_orders_unavailable",
                symbol=str(symbol or "").strip().upper(),
                error=str(exc),
            )
            broker_open_orders = []
        rows = await self.pg.fetchall(
            """
            SELECT strategy_id, symbol, state, side, qty, broker_order_id, order_ref, note
            FROM core.core_order
            WHERE symbol = $1
              AND state NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED')
            LIMIT 100
            """,
            symbol,
        )
        return [
            row
            for row in (dict(item) for item in rows or [])
            if self._is_safety_order(
                row,
                broker_qty=broker_qty,
                market_ref_px=market_ref_px,
                contract_strategy_ids=contract_strategy_ids,
                broker_open_orders=broker_open_orders,
            )
        ]

    async def _load_broker_open_orders(self, *, symbol: str) -> list[dict[str, Any]]:
        if self._current_broker_open_orders is not None:
            raw_orders = list(self._current_broker_open_orders)
        else:
            open_orders_fn = getattr(self.ib_adapter, "open_orders", None)
            if not callable(open_orders_fn):
                raise RuntimeError("broker_open_order_adapter_unavailable")
            try:
                raw_orders = open_orders_fn(force_refresh=True)
            except TypeError:
                raw_orders = open_orders_fn()
            if asyncio.iscoroutine(raw_orders):
                raw_orders = await raw_orders
        out: list[dict[str, Any]] = []
        symbol_u = str(symbol or "").strip().upper()
        for item in raw_orders or []:
            if isinstance(item, dict):
                row = dict(item)
            else:
                row = {
                    "broker_order_id": getattr(item, "broker_order_id", None),
                    "order_id": getattr(item, "order_id", None),
                    "order_ref": getattr(item, "order_ref", None),
                    "symbol": getattr(item, "symbol", None),
                    "side": getattr(item, "side", None),
                    "qty": getattr(item, "qty", None),
                    "status": getattr(item, "status", None),
                    "order_type": getattr(item, "order_type", None),
                    "limit_px": getattr(item, "limit_px", None),
                    "stop_px": getattr(item, "stop_px", None),
                    "account": getattr(item, "account", None),
                }
            row_symbol = str(row.get("symbol") or "").strip().upper()
            if symbol_u and row_symbol and row_symbol != symbol_u:
                continue
            status = str(row.get("status") or "").strip().upper()
            if status in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "INACTIVE"}:
                continue
            out.append(row)
        return out

    @staticmethod
    def _is_safety_order(
        order: dict[str, Any],
        *,
        broker_qty: float,
        market_ref_px: Optional[float],
        contract_strategy_ids: tuple[str, ...],
        broker_open_orders: list[dict[str, Any]],
    ) -> bool:
        broker_match = PositionSafetySupervisor._matching_broker_open_order(
            order,
            broker_qty=broker_qty,
            broker_open_orders=broker_open_orders,
        )
        if broker_match is None:
            return False
        if PositionSafetySupervisor._has_unknown_account_identity(order) or PositionSafetySupervisor._has_unknown_account_identity(broker_match):
            logger.warning(
                "position_safety_rejected_unknown_account_safety_order",
                symbol=str(order.get("symbol") or "").strip().upper(),
                broker_order_id=str(order.get("broker_order_id") or ""),
                order_ref=str(order.get("order_ref") or ""),
            )
            return False
        merged_order = dict(order)
        for key in ("limit_px", "stop_px", "order_type"):
            if merged_order.get(key) in (None, "", 0):
                merged_order[key] = broker_match.get(key)
        ok, reason, details = validate_market_price_band(
            order=merged_order,
            market_ref_px=market_ref_px,
            require_ref_for_priced_order=True,
        )
        if not ok:
            logger.warning(
                "position_safety_rejected_bad_price_safety_order",
                symbol=str(order.get("symbol") or "").strip().upper(),
                broker_order_id=str(order.get("broker_order_id") or ""),
                order_ref=str(order.get("order_ref") or ""),
                reason=reason,
                details=details,
            )
            return False

        strategy_id = str(order.get("strategy_id") or "").strip()
        if strategy_id and strategy_id not in set(contract_strategy_ids):
            return False

        side = str(order.get("side") or "").strip().upper()
        expected_side = "SELL" if float(broker_qty or 0.0) > 0 else "BUY"
        if side and side != expected_side:
            return False

        try:
            order_qty = abs(float(order.get("qty") or 0.0))
        except Exception:
            order_qty = 0.0
        if order_qty <= 1e-6:
            return False
        if order_qty > abs(float(broker_qty or 0.0)) * 1.05 + 1e-6:
            return False

        return is_safety_like_order(order)

    @staticmethod
    def _is_broker_safety_like_order(order: dict[str, Any]) -> bool:
        return is_safety_like_order(order)

    @staticmethod
    def _has_unknown_account_identity(order: dict[str, Any]) -> bool:
        order_ref = str(order.get("order_ref") or "").strip().upper()
        note = str(order.get("note") or "").strip().upper()
        protection_key = str(order.get("protection_key") or "").strip().upper()
        return (
            "UNKNOWN_ACCOUNT" in order_ref
            or "UNKNOWN_ACCOUNT" in note
            or protection_key.startswith("UNKNOWN_ACCOUNT|")
        )

    @staticmethod
    def _matching_broker_open_order(
        order: dict[str, Any],
        *,
        broker_qty: float,
        broker_open_orders: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        broker_order_id = str(order.get("broker_order_id") or "").strip()
        order_ref = str(order.get("order_ref") or "").strip()
        symbol = str(order.get("symbol") or "").strip().upper()
        side = str(order.get("side") or "").strip().upper()
        try:
            qty = abs(float(order.get("qty") or 0.0))
        except Exception:
            qty = 0.0
        expected_side = "SELL" if float(broker_qty or 0.0) > 0 else "BUY"
        if side and side != expected_side:
            return None
        if qty <= 1e-6:
            return None
        for broker_order in broker_open_orders:
            broker_id = str(broker_order.get("broker_order_id") or broker_order.get("order_id") or "").strip()
            broker_ref = str(broker_order.get("order_ref") or "").strip()
            identity_match = False
            if broker_order_id:
                identity_match = broker_id == broker_order_id
            if order_ref and broker_ref == order_ref:
                identity_match = True
            if not identity_match:
                continue
            broker_symbol = str(broker_order.get("symbol") or "").strip().upper()
            if broker_symbol and symbol and broker_symbol != symbol:
                continue
            broker_side = str(broker_order.get("side") or "").strip().upper()
            if broker_side and broker_side != expected_side:
                continue
            try:
                broker_qty_open = abs(float(broker_order.get("qty") or 0.0))
            except Exception:
                broker_qty_open = 0.0
            if broker_qty_open + 1e-6 < qty:
                continue
            return dict(broker_order)
        return None

    @staticmethod
    def _has_matching_broker_open_order(
        order: dict[str, Any],
        *,
        broker_qty: float,
        broker_open_orders: list[dict[str, Any]],
    ) -> bool:
        return (
            PositionSafetySupervisor._matching_broker_open_order(
                order,
                broker_qty=broker_qty,
                broker_open_orders=broker_open_orders,
            )
            is not None
        )

    @staticmethod
    def _registry_row_matches_broker_open_order(
        row: dict[str, Any],
        *,
        broker_open_orders: list[dict[str, Any]],
    ) -> bool:
        broker_order_id = str(row.get("broker_order_id") or "").strip()
        ib_perm_id = str(row.get("ib_perm_id") or "").strip()
        order_ref = str(row.get("order_ref") or "").strip()
        symbol = str(row.get("symbol") or "").strip().upper()
        side = str(row.get("side") or "").strip().upper()
        try:
            desired_qty = abs(float(row.get("desired_qty") or 0.0))
        except Exception:
            desired_qty = 0.0
        for broker_order in broker_open_orders:
            broker_id = str(broker_order.get("broker_order_id") or broker_order.get("order_id") or "").strip()
            broker_perm_id = str(broker_order.get("ib_perm_id") or broker_order.get("perm_id") or "").strip()
            broker_ref = str(broker_order.get("order_ref") or "").strip()
            identity_match = False
            if broker_order_id and broker_id == broker_order_id:
                identity_match = True
            if ib_perm_id and broker_perm_id == ib_perm_id:
                identity_match = True
            if order_ref and broker_ref == order_ref:
                identity_match = True
            if not identity_match:
                continue
            broker_symbol = str(broker_order.get("symbol") or "").strip().upper()
            if broker_symbol and symbol and broker_symbol != symbol:
                continue
            broker_side = str(broker_order.get("side") or "").strip().upper()
            if broker_side and side and broker_side != side:
                continue
            try:
                broker_qty = abs(float(broker_order.get("qty") or 0.0))
            except Exception:
                broker_qty = 0.0
            if desired_qty > 1e-6 and broker_qty + 1e-6 < desired_qty:
                continue
            return True
        return False

    async def _has_active_quarantine(self, *, symbol: str) -> bool:
        row = await self.pg.fetchone(
            """
            SELECT 1 AS active
            FROM core.core_order_quarantine
            WHERE symbol = $1
              AND status = 'active'
            LIMIT 1
            """,
            symbol,
        )
        return bool(row)

    async def _active_quarantine_summary(self) -> dict[str, Any]:
        try:
            rows = await self.pg.fetchall(
                """
                SELECT symbol, reason, COUNT(*) AS count
                FROM core.core_order_quarantine
                WHERE status = 'active'
                GROUP BY symbol, reason
                ORDER BY count DESC, symbol
                LIMIT 100
                """
            )
        except Exception as exc:
            logger.warning("position_safety_active_quarantine_lookup_failed", error=str(exc))
            return {"lookup_failed": True, "active_quarantine_count": 0, "items": [], "error": str(exc)}
        items = [dict(row) for row in rows or []]
        total = 0
        for item in items:
            try:
                total += int(item.get("count") or 0)
            except Exception:
                continue
        return {"lookup_failed": False, "active_quarantine_count": total, "items": items}

    async def _resolve_flat_quarantines_after_broker_flat(
        self,
        *,
        broker_positions: list[dict[str, Any]],
        broker_open_orders: list[dict[str, Any]],
        now_et: datetime,
    ) -> dict[str, Any]:
        active_broker_symbols = {
            str(item.get("symbol") or "").strip().upper()
            for item in broker_positions or []
            if str(item.get("symbol") or "").strip()
        }
        active_broker_open_symbols = {
            str(item.get("symbol") or "").strip().upper()
            for item in broker_open_orders or []
            if str(item.get("symbol") or "").strip()
        }
        try:
            rows = await self.pg.fetchall(
                """
                SELECT symbol, reason, COUNT(*) AS count
                FROM core.core_order_quarantine
                WHERE status = 'active'
                GROUP BY symbol, reason
                ORDER BY symbol, reason
                LIMIT 100
                """
            )
        except Exception as exc:
            logger.warning("position_safety_flat_quarantine_lookup_failed", error=str(exc))
            return {"resolved_count": 0, "skipped_count": 0, "lookup_failed": True, "error": str(exc), "items": []}

        resolved = 0
        skipped = 0
        items: list[dict[str, Any]] = []
        resolved_symbols: set[str] = set()
        for raw in rows or []:
            row = dict(raw)
            symbol = str(row.get("symbol") or "").strip().upper()
            item = {
                "symbol": symbol,
                "reason": str(row.get("reason") or ""),
                "count": int(row.get("count") or 0),
            }
            if not symbol:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "missing_symbol"
                items.append(item)
                continue
            if symbol in active_broker_symbols:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "broker_position_nonflat"
                items.append(item)
                continue
            if symbol in active_broker_open_symbols:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "broker_order_still_open"
                items.append(item)
                continue
            if await self._symbol_core_position_nonflat(symbol=symbol):
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "core_position_nonflat"
                items.append(item)
                continue
            ledger_nonflat = await self._symbol_ledger_nonflat(symbol=symbol)
            if ledger_nonflat is None:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "ledger_lookup_failed"
                items.append(item)
                continue
            if ledger_nonflat:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "ledger_nonflat"
                items.append(item)
                continue
            stale_order_cleanup = await self._terminalize_absent_symbol_orders_after_flat(symbol=symbol)
            item["stale_order_cleanup"] = stale_order_cleanup
            if bool(stale_order_cleanup.get("lookup_failed")):
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "stale_order_lookup_failed"
                items.append(item)
                continue
            if await self._symbol_has_nonterminal_order(symbol=symbol):
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "nonterminal_order_present"
                items.append(item)
                continue
            try:
                await self.pg.execute(
                    """
                    UPDATE core.core_order_quarantine
                    SET status = 'resolved',
                        resolved_at = now(),
                        updated_at = now(),
                        note = $2
                    WHERE symbol = $1
                      AND status = 'active'
                    """,
                    symbol,
                    json.dumps(
                        {
                            "auto_resolved_by": "position_safety_supervisor",
                            "auto_resolved_at": now_et.isoformat(),
                            "reason": "broker_open_orders_positions_core_and_ledger_flat",
                        },
                        ensure_ascii=False,
                    ),
                )
                resolved += int(item["count"] or 0)
                resolved_symbols.add(symbol)
                item["status"] = "resolved"
                item["resolved_at"] = now_et.isoformat()
                logger.warning(
                    "position_safety_flat_quarantine_resolved",
                    symbol=symbol,
                    reason=item["reason"],
                    count=item["count"],
                )
            except Exception as exc:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "resolve_failed"
                item["error"] = str(exc)
                logger.warning("position_safety_flat_quarantine_resolve_failed", symbol=symbol, error=str(exc))
            items.append(item)
        if resolved_symbols and self.recovery_incident_service is not None:
            try:
                resolved_incidents: list[dict[str, Any]] = []
                for symbol in sorted(resolved_symbols):
                    resolved_incidents.extend(
                        self.recovery_incident_service.resolve_symbol_verified_flat(
                            symbol=symbol,
                            now_et=now_et,
                        )
                    )
                self.recovery_incident_service.write_index(
                    now_et=now_et,
                    resolved_verified_flat_count=len(resolved_incidents),
                )
            except Exception as exc:
                logger.warning("position_safety_flat_quarantine_incident_resolve_failed", error=str(exc))
        return {"resolved_count": resolved, "skipped_count": skipped, "lookup_failed": False, "items": items}

    async def _resolve_protected_live_quarantines_after_owner_lane_verified(
        self,
        *,
        broker_positions: list[dict[str, Any]],
        broker_open_orders: list[dict[str, Any]],
        now_et: datetime,
    ) -> dict[str, Any]:
        """Resolve stale synthetic quarantine when a live owner lane is fully protected.

        This is intentionally narrower than flat quarantine cleanup. It only
        handles synthetic recovery rows without broker/order identity and
        requires broker, core_position, ledger, DB order, and live broker order
        evidence for one owner lane. It must not resolve unknown-order or
        orphan-order quarantines.
        """
        broker_qty_by_symbol: dict[str, float] = {}
        broker_ref_by_symbol: dict[str, float] = {}
        for item in broker_positions or []:
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            broker_qty_by_symbol[symbol] = broker_qty_by_symbol.get(symbol, 0.0) + float(item.get("qty") or 0.0)
            market_ref = positive_float(item.get("market_ref_px"))
            if market_ref is not None:
                broker_ref_by_symbol[symbol] = market_ref
        if not broker_qty_by_symbol:
            return {"resolved_count": 0, "skipped_count": 0, "lookup_failed": False, "items": []}
        try:
            rows = await self.pg.fetchall(
                """
                SELECT quarantine_id, symbol, reason, broker_order_id, perm_id, ib_perm_id, order_ref, note, COUNT(*) OVER () AS total_count
                FROM core.core_order_quarantine
                WHERE status = 'active'
                  AND symbol = ANY($1::text[])
                ORDER BY symbol, reason, created_at
                LIMIT 100
                """,
                list(sorted(broker_qty_by_symbol)),
            )
        except Exception as exc:
            logger.warning("position_safety_protected_quarantine_lookup_failed", error=str(exc))
            return {"resolved_count": 0, "skipped_count": 0, "lookup_failed": True, "error": str(exc), "items": []}

        resolved = 0
        skipped = 0
        items: list[dict[str, Any]] = []
        resolved_symbols: set[str] = set()
        for raw in rows or []:
            row = dict(raw)
            quarantine_id = row.get("quarantine_id")
            symbol = str(row.get("symbol") or "").strip().upper()
            reason = str(row.get("reason") or "").strip()
            item: dict[str, Any] = {
                "quarantine_id": str(quarantine_id or ""),
                "symbol": symbol,
                "reason": reason,
                "count": 1,
                "total_count": int(row.get("total_count") or 1),
            }
            protected_resolvable_reasons = {
                "recovery_action_not_executable",
                "db_persist_submitted_failed",
                "scoped_tradability_denied:owner_identity_incomplete",
            }
            if reason not in protected_resolvable_reasons:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "not_synthetic_recovery_quarantine"
                items.append(item)
                continue
            if any(str(row.get(key) or "").strip() for key in ("broker_order_id", "perm_id", "ib_perm_id", "order_ref")):
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "quarantine_has_order_identity"
                items.append(item)
                continue
            broker_qty = float(broker_qty_by_symbol.get(symbol, 0.0) or 0.0)
            if abs(broker_qty) <= 1e-6:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "broker_position_flat"
                items.append(item)
                continue
            owner = await self._verified_protected_owner_lane_for_symbol(
                symbol=symbol,
                broker_qty=broker_qty,
                market_ref_px=broker_ref_by_symbol.get(symbol),
                broker_open_orders=broker_open_orders,
            )
            if not owner:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "owner_lane_not_verified"
                items.append(item)
                continue
            if not self._quarantine_note_matches_owner_or_legacy_empty(row.get("note"), owner):
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "quarantine_owner_scope_mismatch"
                item["owner_lane"] = owner
                items.append(item)
                continue
            try:
                await self.pg.execute(
                    """
                    UPDATE core.core_order_quarantine
                    SET status = 'resolved',
                        resolved_at = now(),
                        updated_at = now(),
                        note = $2
                    WHERE quarantine_id = $1
                      AND status = 'active'
                      AND reason = ANY($3::text[])
                      AND broker_order_id IS NULL
                      AND ib_perm_id IS NULL
                      AND COALESCE(order_ref, '') = ''
                    """,
                    quarantine_id,
                    json.dumps(
                        {
                            "auto_resolved_by": "position_safety_supervisor",
                            "auto_resolved_at": now_et.isoformat(),
                            "reason": "owner_lane_live_position_protected",
                            "owner_lane": owner,
                        },
                        ensure_ascii=False,
                    ),
                    sorted(protected_resolvable_reasons),
                )
                await self.pg.execute(
                    """
                    DELETE FROM core.manual_intervention_lock
                    WHERE symbol = $1
                      AND reason = 'strategy_recovery_action_quarantine'
                      AND (
                        context->>'reason' = ANY($2::text[])
                        OR context->>'reason' IS NULL
                      )
                      AND ($3::text = '' OR COALESCE(context->>'account_id', '') = $3::text)
                      AND ($4::text = '' OR COALESCE(context->>'strategy_instance_id', '') = $4::text)
                      AND ($5::text = '' OR COALESCE(context->>'position_owner_key', '') = $5::text)
                      AND ($6::text = '' OR COALESCE(context->>'position_lot_id', context->>'lot_id', '') = $6::text)
                    """,
                    symbol,
                    sorted(protected_resolvable_reasons),
                    str(owner.get("account_id") or ""),
                    str(owner.get("strategy_instance_id") or ""),
                    str(owner.get("position_owner_key") or ""),
                    str(owner.get("lot_id") or owner.get("position_lot_id") or ""),
                )
                resolved += int(item["count"] or 1)
                resolved_symbols.add(symbol)
                item.update({"status": "resolved", "resolved_at": now_et.isoformat(), "owner_lane": owner})
                logger.warning(
                    "position_safety_protected_quarantine_resolved",
                    symbol=symbol,
                    strategy_id=owner.get("strategy_id"),
                    broker_qty=broker_qty,
                    protected_order_count=owner.get("protected_order_count"),
                )
            except Exception as exc:
                skipped += 1
                item["status"] = "skipped"
                item["skip_reason"] = "resolve_failed"
                item["error"] = str(exc)
                logger.warning("position_safety_protected_quarantine_resolve_failed", symbol=symbol, error=str(exc))
            items.append(item)
        if resolved_symbols and self.recovery_incident_service is not None:
            try:
                resolved_incidents: list[dict[str, Any]] = []
                for symbol in sorted(resolved_symbols):
                    resolved_incidents.extend(
                        self.recovery_incident_service.resolve_symbol_verified_flat(
                            symbol=symbol,
                            now_et=now_et,
                        )
                    )
                self.recovery_incident_service.write_index(
                    now_et=now_et,
                    resolved_verified_flat_count=len(resolved_incidents),
                )
            except Exception as exc:
                logger.warning("position_safety_protected_quarantine_incident_resolve_failed", error=str(exc))
        return {"resolved_count": resolved, "skipped_count": skipped, "lookup_failed": False, "items": items}

    async def _verified_protected_owner_lane_for_symbol(
        self,
        *,
        symbol: str,
        broker_qty: float,
        market_ref_px: Optional[float],
        broker_open_orders: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        lanes = await self._nonflat_core_position_lanes(symbol=symbol)
        if not lanes:
            return None
        core_qty_by_strategy: dict[str, float] = {}
        for lane in lanes:
            strategy_id = str(lane.get("strategy_id") or "").strip()
            if not strategy_id:
                return None
            core_qty_by_strategy[strategy_id] = core_qty_by_strategy.get(strategy_id, 0.0) + self._safe_float(lane.get("pos_qty"))
        core_qty = sum(core_qty_by_strategy.values())
        if abs(core_qty - float(broker_qty or 0.0)) > 1e-6:
            return None
        owner_lanes = await self._ledger_owner_lanes_for_symbol(symbol=symbol)
        owner_lanes = self._owner_lanes_relevant_for_broker_position(owner_lanes=owner_lanes, broker_qty=broker_qty)
        matching_owner_lanes = [
            dict(lane)
            for lane in owner_lanes
            if self._owner_lane_identity_complete(lane)
            and str(lane.get("strategy_id") or "").strip() in core_qty_by_strategy
        ]
        if not matching_owner_lanes:
            return None
        owner_qty_by_strategy: dict[str, float] = {}
        for lane in matching_owner_lanes:
            lane_strategy_id = str(lane.get("strategy_id") or "").strip()
            owner_qty_by_strategy[lane_strategy_id] = owner_qty_by_strategy.get(lane_strategy_id, 0.0) + float(
                lane.get("qty") or 0.0
            )
        if set(owner_qty_by_strategy) != set(core_qty_by_strategy):
            return None
        for strategy_id, strategy_qty in core_qty_by_strategy.items():
            if abs(float(owner_qty_by_strategy.get(strategy_id, 0.0)) - float(strategy_qty or 0.0)) > 1e-6:
                return None
        owner_qty = sum(owner_qty_by_strategy.values())
        if abs(owner_qty - float(broker_qty or 0.0)) > 1e-6:
            return None
        strategy_ids = tuple(sorted(core_qty_by_strategy))
        active_orders = await self._active_orders(
            symbol=symbol,
            broker_qty=broker_qty,
            market_ref_px=market_ref_px,
            contract_strategy_ids=strategy_ids,
        )
        active_orders = [
            dict(order) for order in active_orders if str(order.get("strategy_id") or "").strip() in set(strategy_ids)
        ]
        protected_qty_by_strategy: dict[str, float] = {}
        for order in active_orders:
            order_strategy_id = str(order.get("strategy_id") or "").strip()
            protected_qty_by_strategy[order_strategy_id] = protected_qty_by_strategy.get(order_strategy_id, 0.0) + abs(
                float(order.get("qty") or 0.0)
            )
        for strategy_id, owner_qty_for_strategy in owner_qty_by_strategy.items():
            if protected_qty_by_strategy.get(strategy_id, 0.0) + 1e-6 < abs(float(owner_qty_for_strategy or 0.0)):
                return None
        protected_qty = sum(protected_qty_by_strategy.values())
        if protected_qty + 1e-6 < abs(float(broker_qty or 0.0)):
            return None
        if not active_orders:
            return None
        for order in active_orders:
            if not self._matching_broker_open_order(order, broker_qty=broker_qty, broker_open_orders=broker_open_orders):
                return None
        if len(matching_owner_lanes) == 1:
            owner_lane = matching_owner_lanes[0]
            strategy_id = str(owner_lane.get("strategy_id") or "").strip()
            return {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "broker_qty": broker_qty,
                "core_qty": core_qty,
                "ledger_qty": float(owner_qty or 0.0),
                "protected_order_count": len(active_orders),
                "protected_qty": protected_qty,
                "account_id": str(owner_lane.get("account_id") or ""),
                "strategy_instance_id": str(owner_lane.get("strategy_instance_id") or ""),
                "lot_id": str(owner_lane.get("lot_id") or ""),
                "position_lot_id": str(owner_lane.get("lot_id") or ""),
                "position_owner_key": str(owner_lane.get("position_owner_key") or ""),
            }
        return {
            "strategy_id": "aggregate_owner_lanes",
            "symbol": symbol,
            "broker_qty": broker_qty,
            "core_qty": core_qty,
            "ledger_qty": float(owner_qty or 0.0),
            "protected_order_count": len(active_orders),
            "protected_qty": protected_qty,
            "aggregate_owner_lanes_verified": True,
            "owner_lanes": tuple(
                {
                    "account_id": str(lane.get("account_id") or ""),
                    "strategy_id": str(lane.get("strategy_id") or ""),
                    "strategy_instance_id": str(lane.get("strategy_instance_id") or ""),
                    "symbol": str(lane.get("symbol") or "").strip().upper(),
                    "lot_id": str(lane.get("lot_id") or ""),
                    "position_owner_key": str(lane.get("position_owner_key") or ""),
                    "qty": float(lane.get("qty") or 0.0),
                }
                for lane in matching_owner_lanes
            ),
        }

    async def _nonflat_core_position_lanes(self, *, symbol: str) -> list[dict[str, Any]]:
        rows = await self.pg.fetchall(
            """
            SELECT strategy_id, symbol, COALESCE(pos_qty, 0)::float8 AS pos_qty
            FROM core.core_position
            WHERE symbol = $1
              AND ABS(COALESCE(pos_qty, 0)) > 1e-9
            ORDER BY strategy_id
            LIMIT 20
            """,
            symbol,
        )
        out: list[dict[str, Any]] = []
        symbol_u = str(symbol or "").strip().upper()
        for raw in rows or []:
            row = dict(raw)
            if str(row.get("symbol") or "").strip().upper() != symbol_u:
                continue
            if abs(self._safe_float(row.get("pos_qty"))) <= 1e-9:
                continue
            out.append(row)
        return out

    async def _symbol_has_nonterminal_order(self, *, symbol: str) -> bool:
        row = await self.pg.fetchone(
            """
            SELECT 1 AS active
            FROM core.core_order
            WHERE symbol = $1
              AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'FAILED')
            LIMIT 1
            """,
            symbol,
        )
        return bool(row)

    async def _terminalize_absent_symbol_orders_after_flat(self, *, symbol: str) -> dict[str, Any]:
        """Terminalize stale local orders after broker, core, and ledger flat evidence.

        This is used by flat quarantine cleanup before the remaining local order
        check. It does not cancel broker orders; it only reconciles DB rows that
        are nonterminal locally but absent from the current live broker open
        order snapshot for a symbol already proven flat.
        """

        try:
            broker_open_orders = await self._load_broker_open_orders(symbol=symbol)
            rows = await self.pg.fetchall(
                """
                SELECT order_id, strategy_id, symbol, state, side, qty, broker_order_id, order_ref, note
                FROM core.core_order
                WHERE symbol = $1
                  AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'FAILED')
                ORDER BY created_at DESC
                LIMIT 100
                """,
                symbol,
            )
        except Exception as exc:
            logger.warning("position_safety_absent_symbol_order_lookup_failed", symbol=symbol, error=str(exc))
            return {"terminalized_count": 0, "skipped_count": 0, "items": [], "lookup_failed": True, "error": str(exc)}

        symbol_u = str(symbol or "").strip().upper()
        terminalized = 0
        skipped = 0
        items: list[dict[str, Any]] = []
        for raw in rows or []:
            row = dict(raw)
            row_symbol = str(row.get("symbol") or "").strip().upper()
            if row_symbol != symbol_u:
                continue
            item = {
                "order_id": str(row.get("order_id") or ""),
                "strategy_id": str(row.get("strategy_id") or ""),
                "broker_order_id": str(row.get("broker_order_id") or ""),
                "order_ref": str(row.get("order_ref") or ""),
                "state": str(row.get("state") or ""),
                "side": str(row.get("side") or ""),
            }
            if self._db_order_has_broker_open_match(row, broker_open_orders=broker_open_orders):
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "broker_order_still_open"
                items.append(item)
                continue
            try:
                await self.pg.execute(
                    """
                    UPDATE core.core_order
                    SET state = 'CANCELLED',
                        updated_at = now()
                    WHERE order_id = $1
                      AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'FAILED')
                    """,
                    row.get("order_id"),
                )
                terminalized += 1
                item["status"] = "terminalized"
                item["terminal_state"] = "CANCELLED"
                item["reason"] = "nonterminal_order_absent_after_broker_core_ledger_flat"
                logger.warning(
                    "position_safety_absent_symbol_order_terminalized",
                    strategy_id=str(row.get("strategy_id") or ""),
                    symbol=symbol_u,
                    order_id=str(row.get("order_id") or ""),
                    broker_order_id=str(row.get("broker_order_id") or ""),
                    order_ref=str(row.get("order_ref") or ""),
                )
            except Exception as exc:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "terminalize_failed"
                item["error"] = str(exc)
                logger.warning(
                    "position_safety_absent_symbol_order_terminalize_failed",
                    symbol=symbol_u,
                    order_id=str(row.get("order_id") or ""),
                    error=str(exc),
                )
            items.append(item)
        return {
            "terminalized_count": terminalized,
            "skipped_count": skipped,
            "items": items,
            "lookup_failed": False,
        }

    async def _symbol_core_position_nonflat(self, *, symbol: str) -> bool:
        row = await self.pg.fetchone(
            """
            SELECT 1 AS active
            FROM core.core_position
            WHERE symbol = $1
              AND ABS(COALESCE(pos_qty, 0)) > 1e-9
            LIMIT 1
            """,
            symbol,
        )
        return bool(row)

    async def _symbol_ledger_nonflat(self, *, symbol: str) -> Optional[bool]:
        try:
            row = await self.pg.fetchone(
                """
                SELECT COALESCE(SUM(qty), 0)::float8 AS ledger_qty
                FROM core.position_ledger_current
                WHERE symbol = $1
                """,
                symbol,
            )
        except Exception as exc:
            logger.warning("position_safety_symbol_ledger_lookup_failed", symbol=symbol, error=str(exc))
            return None
        try:
            return abs(float((row or {}).get("ledger_qty") or 0.0)) > 1e-6
        except Exception:
            return None

    async def _clear_flat_position_manual_locks(self, *, broker_positions: list[BrokerPosition]) -> dict[str, Any]:
        active_broker_symbols = {
            str((item.get("symbol") if isinstance(item, dict) else getattr(item, "symbol", "")) or "").strip().upper()
            for item in broker_positions
        }
        active_broker_symbols.discard("")
        try:
            rows = await self.pg.fetchall(
                """
                SELECT strategy_id, symbol, reason, context
                FROM core.manual_intervention_lock
                WHERE expire_at > now()
                  AND reason IN (
                    'manual_flatten',
                    'flatten_action',
                    'protection_failed',
                    'position_safety_manual_required',
                    'position_mismatch_quarantine',
                    'strategy_recovery_action_quarantine',
                    'orphan_order_quarantine'
                  )
                ORDER BY strategy_id, symbol
                """
            )
        except Exception as exc:
            logger.warning("position_safety_flat_manual_lock_lookup_failed", error=str(exc))
            return {"cleared_count": 0, "skipped_count": 0, "lookup_failed": True, "error": str(exc)}
        cleared = 0
        skipped = 0
        cleared_items: list[dict[str, str]] = []
        for row in rows or []:
            item = dict(row)
            strategy_id = str(item.get("strategy_id") or "").strip()
            symbol = str(item.get("symbol") or "").strip().upper()
            reason = str(item.get("reason") or "").strip()
            context = item.get("context")
            if isinstance(context, str):
                try:
                    context = json.loads(context)
                except Exception:
                    context = {}
            if not isinstance(context, dict):
                context = {}
            owner_scoped_close_only = self._is_owner_scoped_close_only_lock(
                {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "reason": reason,
                    "context": context,
                }
            )
            if not strategy_id or not symbol:
                skipped += 1
                continue
            if symbol in active_broker_symbols and not owner_scoped_close_only:
                skipped += 1
                continue
            if not await self._core_position_flat(strategy_id=strategy_id, symbol=symbol):
                skipped += 1
                continue
            if owner_scoped_close_only:
                ledger_qty = await self._ledger_current_qty(strategy_id=strategy_id, symbol=symbol)
                if ledger_qty is None or abs(float(ledger_qty or 0.0)) > 1e-6:
                    skipped += 1
                    continue
            if await self._has_nonterminal_order(strategy_id=strategy_id, symbol=symbol):
                skipped += 1
                continue
            if await self._unlock_manual_lock(strategy_id=strategy_id, symbol=symbol):
                cleared += 1
                cleared_items.append(
                    {
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "reason": reason,
                        "scope": "owner_lane" if owner_scoped_close_only else "symbol_flat",
                    }
                )
            else:
                skipped += 1
        return {"cleared_count": cleared, "skipped_count": skipped, "lookup_failed": False, "items": cleared_items}

    async def _clear_flat_core_position_residuals(
        self,
        *,
        broker_positions: list[dict[str, Any]],
        now_et: datetime,
    ) -> dict[str, Any]:
        """Clear stale core_position rows only when broker and owner ledger are both flat.

        `core.core_position` is a derived operational cache.  It must not keep a
        strategy startup-blocked after broker truth and the append-only ledger
        have already converged to flat. Broker positions are account+symbol
        aggregates, however, so broker-flat evidence is only a prerequisite.
        The mutation requires exact strategy_id+symbol ledger-flat evidence and
        absence of live/nonterminal orders for the same owner lane.
        """
        active_broker_symbols = {
            str(item.get("symbol") or "").strip().upper()
            for item in broker_positions or []
            if str(item.get("symbol") or "").strip()
        }
        contract_pairs = self._production_contract_pairs()
        production_strategy_ids = self._production_strategy_ids()
        if not contract_pairs and not production_strategy_ids:
            return {
                "checked_count": 0,
                "cleared_count": 0,
                "skipped_count": 0,
                "lookup_failed": False,
                "items": [],
                "reason": "no_production_contract_pairs",
            }
        try:
            rows = await self.pg.fetchall(
                """
                SELECT strategy_id, symbol, COALESCE(pos_qty, 0)::float8 AS pos_qty
                FROM core.core_position
                WHERE ABS(COALESCE(pos_qty, 0)) > 1e-9
                LIMIT 500
                """
            )
        except Exception as exc:
            logger.warning("position_safety_core_position_residual_lookup_failed", error=str(exc))
            return {"checked_count": 0, "cleared_count": 0, "skipped_count": 0, "lookup_failed": True, "error": str(exc), "items": []}

        checked = 0
        cleared = 0
        skipped = 0
        items: list[dict[str, Any]] = []
        for raw in rows or []:
            row = dict(raw)
            strategy_id = str(row.get("strategy_id") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            owned_contract_symbol = (strategy_id, symbol) in contract_pairs
            owned_option_symbol = symbol.startswith("OPT:") and strategy_id in production_strategy_ids
            if not strategy_id or not symbol or not (owned_contract_symbol or owned_option_symbol):
                continue
            checked += 1
            pos_qty = self._safe_float(row.get("pos_qty"))
            item: dict[str, Any] = {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "core_pos_qty": pos_qty,
            }
            if symbol in active_broker_symbols:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "broker_position_nonflat"
                items.append(item)
                continue
            ledger_qty = await self._ledger_current_qty(strategy_id=strategy_id, symbol=symbol)
            item["ledger_qty"] = ledger_qty
            if ledger_qty is None:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "ledger_lookup_failed"
                items.append(item)
                continue
            if abs(float(ledger_qty or 0.0)) > 1e-6:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "ledger_nonflat"
                items.append(item)
                continue
            stale_close_cleanup = await self._terminalize_absent_flat_close_orders(
                strategy_id=strategy_id,
                symbol=symbol,
                core_pos_qty=pos_qty,
            )
            item["stale_close_order_cleanup"] = stale_close_cleanup
            if bool(stale_close_cleanup.get("lookup_failed")):
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "stale_close_order_lookup_failed"
                items.append(item)
                continue
            if await self._has_nonterminal_order(strategy_id=strategy_id, symbol=symbol):
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "nonterminal_order_present"
                items.append(item)
                continue
            try:
                await self.pg.execute(
                    """
                    UPDATE core.core_position
                    SET pos_qty = 0,
                        avg_px = NULL,
                        updated_at = now()
                    WHERE strategy_id = $1
                      AND symbol = $2
                      AND ABS(COALESCE(pos_qty, 0)) > 1e-9
                    """,
                    strategy_id,
                    symbol,
                )
                cleared += 1
                item["status"] = "cleared"
                item["reason"] = "owner_lane_ledger_flat_broker_flat_no_open_orders"
                item["cleared_at"] = now_et.isoformat()
                logger.warning(
                    "position_safety_core_position_residual_cleared",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    core_pos_qty=pos_qty,
                    ledger_qty=ledger_qty,
                    reason="owner_lane_ledger_flat_broker_flat_no_open_orders",
                )
            except Exception as exc:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "update_failed"
                item["error"] = str(exc)
                logger.warning(
                    "position_safety_core_position_residual_clear_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
            items.append(item)
        return {
            "checked_count": checked,
            "cleared_count": cleared,
            "skipped_count": skipped,
            "lookup_failed": False,
            "items": items,
        }

    async def _terminalize_absent_flat_close_orders(
        self,
        *,
        strategy_id: str,
        symbol: str,
        core_pos_qty: float,
    ) -> dict[str, Any]:
        """Terminalize stale DB close orders after broker open-order absence is proven."""

        try:
            broker_open_orders = await self._load_broker_open_orders(symbol=symbol)
            rows = await self.pg.fetchall(
                """
                SELECT order_id, strategy_id, symbol, state, side, qty, broker_order_id, order_ref, note
                FROM core.core_order
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'FAILED')
                ORDER BY created_at DESC
                LIMIT 100
                """,
                strategy_id,
                symbol,
            )
        except Exception as exc:
            logger.warning(
                "position_safety_absent_close_order_lookup_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return {"terminalized_count": 0, "skipped_count": 0, "items": [], "lookup_failed": True, "error": str(exc)}

        expected_side = "SELL" if float(core_pos_qty or 0.0) > 0 else "BUY"
        terminalized = 0
        skipped = 0
        items: list[dict[str, Any]] = []
        for raw in rows or []:
            row = dict(raw)
            item = {
                "order_id": str(row.get("order_id") or ""),
                "broker_order_id": str(row.get("broker_order_id") or ""),
                "order_ref": str(row.get("order_ref") or ""),
                "state": str(row.get("state") or ""),
                "side": str(row.get("side") or ""),
            }
            if str(row.get("side") or "").strip().upper() != expected_side:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "not_reducing_side"
                items.append(item)
                continue
            if self._db_order_has_broker_open_match(row, broker_open_orders=broker_open_orders):
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "broker_order_still_open"
                items.append(item)
                continue
            try:
                await self.pg.execute(
                    """
                    UPDATE core.core_order
                    SET state = 'CANCELLED',
                        updated_at = now()
                    WHERE order_id = $1
                      AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'FAILED')
                    """,
                    row.get("order_id"),
                )
                terminalized += 1
                item["status"] = "terminalized"
                item["terminal_state"] = "CANCELLED"
                item["reason"] = "reduce_only_close_order_absent_after_broker_and_ledger_flat"
                logger.warning(
                    "position_safety_absent_flat_close_order_terminalized",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_id=str(row.get("order_id") or ""),
                    broker_order_id=str(row.get("broker_order_id") or ""),
                    order_ref=str(row.get("order_ref") or ""),
                )
            except Exception as exc:
                skipped += 1
                item["status"] = "skipped"
                item["reason"] = "terminalize_failed"
                item["error"] = str(exc)
                logger.warning(
                    "position_safety_absent_flat_close_order_terminalize_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_id=str(row.get("order_id") or ""),
                    error=str(exc),
                )
            items.append(item)
        return {
            "terminalized_count": terminalized,
            "skipped_count": skipped,
            "items": items,
            "lookup_failed": False,
        }

    @staticmethod
    def _db_order_has_broker_open_match(order: dict[str, Any], *, broker_open_orders: list[dict[str, Any]]) -> bool:
        broker_order_id = str(order.get("broker_order_id") or "").strip()
        order_ref = str(order.get("order_ref") or "").strip()
        for broker_order in broker_open_orders or []:
            broker_id = str(broker_order.get("broker_order_id") or broker_order.get("order_id") or "").strip()
            broker_ref = str(broker_order.get("order_ref") or "").strip()
            if broker_order_id and broker_id == broker_order_id:
                return True
            if order_ref and broker_ref == order_ref:
                return True
        return False

    async def _durable_order_row_for_broker_order_id(self, *, broker_order_id: str) -> dict[str, Any] | None:
        if self.pg is None:
            return None
        try:
            row = await self.pg.fetchone(
                """
                SELECT order_id, broker_order_id, strategy_id, symbol, state, side,
                       qty, order_type, order_ref, note
                FROM core.core_order
                WHERE broker_order_id = $1
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
                """,
                str(broker_order_id or "").strip(),
            )
        except Exception as exc:
            logger.warning(
                "position_safety_durable_order_lookup_failed",
                broker_order_id=str(broker_order_id or ""),
                error=str(exc),
            )
            return None
        return dict(row) if row else None

    async def _durable_order_row_for_broker_order(
        self,
        *,
        broker_order_id: str,
        order_ref: str,
        symbol: str,
    ) -> dict[str, Any] | None:
        row = await self._durable_order_row_for_broker_order_id(broker_order_id=broker_order_id)
        if row:
            return row
        if self.pg is None:
            return None
        order_ref_v = str(order_ref or "").strip()
        symbol_u = str(symbol or "").strip().upper()
        if not order_ref_v or not symbol_u:
            return None
        try:
            row = await self.pg.fetchone(
                """
                SELECT order_id, broker_order_id, strategy_id, symbol, state, side,
                       qty, order_type, order_ref, note
                FROM core.core_order
                WHERE order_ref = $1
                  AND symbol = $2
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
                """,
                order_ref_v,
                symbol_u,
            )
        except Exception as exc:
            logger.warning(
                "position_safety_durable_order_ref_lookup_failed",
                broker_order_id=str(broker_order_id or ""),
                order_ref=order_ref_v,
                symbol=symbol_u,
                error=str(exc),
            )
            return None
        return dict(row) if row else None

    @staticmethod
    def _is_safety_like_durable_order(row: dict[str, Any]) -> bool:
        if is_safety_like_order(row):
            return True
        note_raw = row.get("note")
        try:
            note = json.loads(note_raw) if isinstance(note_raw, str) and note_raw.strip() else {}
        except Exception:
            note = {}
        extras = note.get("extras") if isinstance(note, dict) and isinstance(note.get("extras"), dict) else {}
        return bool(
            isinstance(note, dict)
            and (
                note.get("close_only")
                or note.get("flatten_action")
                or note.get("protective_stop")
                or extras.get("close_only")
                or extras.get("flatten_action")
                or extras.get("protective_stop")
            )
        )

    async def _broker_order_absent(self, *, broker_order_id: str) -> bool:
        try:
            open_orders = await self._load_broker_open_orders(symbol="")
        except Exception as exc:
            logger.warning(
                "position_safety_flat_live_safety_order_cancel_refresh_failed",
                broker_order_id=str(broker_order_id or ""),
                error=str(exc),
            )
            return False
        target = str(broker_order_id or "").strip()
        for order in open_orders:
            current = str(order.get("broker_order_id") or order.get("order_id") or "").strip()
            if current and current == target:
                return False
        return True

    async def _mark_durable_order_cancelled_if_working(
        self,
        *,
        broker_order_id: str,
        now_et: datetime,
        reason: str,
    ) -> None:
        if self.pg is None:
            return
        try:
            await self.pg.execute(
                """
                UPDATE core.core_order
                SET state = 'CANCELLED',
                    note = (COALESCE(NULLIF(note, ''), '{}')::jsonb || $2::jsonb)::text,
                    updated_at = now()
                WHERE broker_order_id = $1
                  AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'FAILED')
                """,
                str(broker_order_id or "").strip(),
                json.dumps(
                    {
                        "position_safety_flat_live_order_cleanup": {
                            "reason": reason,
                            "at": now_et.isoformat(),
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as exc:
            logger.warning(
                "position_safety_flat_live_safety_order_mark_cancelled_failed",
                broker_order_id=str(broker_order_id or ""),
                error=str(exc),
            )

    def _production_contract_pairs(self) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for contract in self.strategy_data_contracts:
            strategy_id = str(getattr(contract, "strategy_id", "") or "").strip()
            if not strategy_id or not bool(getattr(contract, "production_blocking", True)):
                continue
            for symbol in getattr(contract, "symbols", ()) or ():
                symbol_u = str(symbol or "").strip().upper()
                if symbol_u:
                    pairs.add((strategy_id, symbol_u))
        return pairs

    def _production_strategy_ids(self) -> set[str]:
        ids: set[str] = set()
        for contract in self.strategy_data_contracts:
            strategy_id = str(getattr(contract, "strategy_id", "") or "").strip()
            if strategy_id and bool(getattr(contract, "production_blocking", True)):
                ids.add(strategy_id)
        return ids

    async def _ledger_owner_lanes_for_symbol(self, *, symbol: str) -> list[dict[str, Any]]:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return []
        try:
            rows = await self.pg.fetchall(
                """
                SELECT
                    account_id,
                    strategy_id,
                    strategy_instance_id,
                    symbol,
                    lot_id,
                    COALESCE(position_owner_key, '') AS position_owner_key,
                    COALESCE(SUM(qty), 0)::float8 AS qty
                FROM core.position_ledger_current
                WHERE symbol = $1
                GROUP BY account_id, strategy_id, strategy_instance_id, symbol, lot_id, position_owner_key
                HAVING ABS(COALESCE(SUM(qty), 0)) > 1e-9
                ORDER BY strategy_instance_id, strategy_id, lot_id
                LIMIT 100
                """,
                symbol_u,
            )
        except Exception as exc:
            logger.warning("position_safety_owner_lane_lookup_failed", symbol=symbol_u, error=str(exc))
            return []
        lanes: list[dict[str, Any]] = []
        for raw in rows or []:
            row = dict(raw)
            row_symbol = str(row.get("symbol") or "").strip().upper()
            strategy_id = str(row.get("strategy_id") or "").strip()
            strategy_instance_id = str(row.get("strategy_instance_id") or strategy_id).strip()
            qty = self._safe_float(row.get("qty"))
            if row_symbol != symbol_u or not strategy_instance_id or abs(qty) <= 1e-9:
                continue
            row["symbol"] = row_symbol
            row["strategy_id"] = strategy_id or strategy_instance_id
            row["strategy_instance_id"] = strategy_instance_id
            row["qty"] = qty
            lanes.append(row)
        return lanes

    @staticmethod
    def _owner_lanes_relevant_for_broker_position(
        *,
        owner_lanes: list[dict[str, Any]],
        broker_qty: float,
    ) -> list[dict[str, Any]]:
        if not owner_lanes:
            return []
        complete_lanes = [
            dict(lane)
            for lane in owner_lanes
            if PositionSafetySupervisor._owner_lane_identity_complete(lane)
            and abs(float(lane.get("qty") or 0.0)) > 1e-9
            and float(lane.get("qty") or 0.0) * float(broker_qty or 0.0) > 0
        ]
        if not complete_lanes:
            return list(owner_lanes)
        complete_qty = sum(float(lane.get("qty") or 0.0) for lane in complete_lanes)
        if abs(complete_qty - float(broker_qty or 0.0)) <= 1e-6:
            return complete_lanes
        return list(owner_lanes)

    @staticmethod
    def _owner_lane_identity_complete(lane: dict[str, Any]) -> bool:
        strategy_id = str(lane.get("strategy_id") or "").strip()
        return bool(
            strategy_id
            and strategy_id != "adopted_reconciler"
            and str(lane.get("account_id") or "").strip()
            and str(lane.get("strategy_instance_id") or "").strip()
            and str(lane.get("lot_id") or "").strip()
            and str(lane.get("position_owner_key") or "").strip()
        )

    @staticmethod
    def _owner_lane_strategy_ids(owner_lanes: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[str]:
        result: list[str] = []
        for lane in owner_lanes or []:
            strategy_id = str(lane.get("strategy_instance_id") or lane.get("strategy_id") or "").strip()
            if strategy_id and strategy_id not in result:
                result.append(strategy_id)
        return result

    @staticmethod
    def _recovery_owner_lane(
        *,
        owner_lanes: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        strategy_id: str,
        symbol: str,
    ) -> dict[str, Any]:
        strategy_u = str(strategy_id or "").strip()
        symbol_u = str(symbol or "").strip().upper()
        for lane in owner_lanes or ():
            lane_strategy = str(lane.get("strategy_instance_id") or lane.get("strategy_id") or "").strip()
            lane_symbol = str(lane.get("symbol") or "").strip().upper()
            if lane_strategy == strategy_u and lane_symbol == symbol_u:
                return dict(lane)
        return {}

    @staticmethod
    def _owner_lane_protection_state(
        *,
        owner_lanes: list[dict[str, Any]],
        active_orders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not owner_lanes:
            return {}
        protected_strategy_ids: set[str] = set()
        unprotected_lanes: list[dict[str, Any]] = []
        protected_lanes: list[dict[str, Any]] = []
        active_qty_by_strategy: dict[str, float] = {}
        for order in active_orders or []:
            strategy_id = str(order.get("strategy_id") or "").strip()
            if not strategy_id:
                continue
            try:
                qty = abs(float(order.get("qty") or 0.0))
            except Exception:
                qty = 0.0
            active_qty_by_strategy[strategy_id] = active_qty_by_strategy.get(strategy_id, 0.0) + qty
        for lane in owner_lanes:
            strategy_id = str(lane.get("strategy_instance_id") or lane.get("strategy_id") or "").strip()
            lane_qty = abs(float(lane.get("qty") or 0.0))
            if strategy_id and active_qty_by_strategy.get(strategy_id, 0.0) + 1e-6 >= lane_qty:
                protected_strategy_ids.add(strategy_id)
                protected_lanes.append(dict(lane))
            else:
                unprotected_lanes.append(dict(lane))
        return {
            "all_protected": not unprotected_lanes,
            "protected_strategy_ids": tuple(sorted(protected_strategy_ids)),
            "protected_lanes": tuple(protected_lanes),
            "unprotected_lanes": tuple(unprotected_lanes),
        }

    async def _clear_non_owner_position_safety_manual_locks(
        self,
        *,
        symbol: str,
        owner_strategy_ids: set[str],
        now_et: datetime,
    ) -> list[str]:
        if not owner_strategy_ids:
            return []
        try:
            rows = await self.pg.fetchall(
                """
                SELECT strategy_id, symbol, reason, context
                FROM core.manual_intervention_lock
                WHERE symbol = $1
                  AND expire_at > now()
                  AND reason = 'position_safety_manual_required'
                ORDER BY strategy_id
                """,
                str(symbol or "").strip().upper(),
            )
        except Exception as exc:
            logger.warning("position_safety_non_owner_manual_lock_lookup_failed", symbol=symbol, error=str(exc))
            return []
        cleared: list[str] = []
        for raw in rows or []:
            row = dict(raw)
            strategy_id = str(row.get("strategy_id") or "").strip()
            if not strategy_id or strategy_id in owner_strategy_ids:
                continue
            if not self._is_position_safety_auto_lock(row):
                continue
            if await self._unlock_manual_lock(strategy_id=strategy_id, symbol=symbol):
                cleared.append(f"position_safety_manual_lock:{now_et.date().isoformat()}:{strategy_id}:{symbol}")
        return cleared

    @staticmethod
    def _is_stale_aggregate_position_safety_lock(*, lock: dict[str, Any], lane_qty: float) -> bool:
        if lane_qty <= 1e-9 or not PositionSafetySupervisor._is_position_safety_auto_lock(lock):
            return False
        context = lock.get("context")
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except Exception:
                context = {}
        if not isinstance(context, dict):
            context = {}
        reason = str(context.get("reason") or "").strip()
        if not reason.startswith("broker_position_without_protection"):
            return False
        try:
            lock_qty = abs(float(context.get("qty") or 0.0))
        except Exception:
            lock_qty = 0.0
        return lock_qty > abs(float(lane_qty or 0.0)) + 1e-6

    @staticmethod
    def _recovery_qty_for_strategy(
        *,
        aggregate_qty: float,
        strategy_ids: tuple[str, ...],
        recovery_qty_by_strategy: dict[str, float],
    ) -> float:
        sign = 1.0 if float(aggregate_qty or 0.0) >= 0 else -1.0
        if not recovery_qty_by_strategy or not strategy_ids:
            return float(aggregate_qty or 0.0)
        qty = sum(abs(float(recovery_qty_by_strategy.get(strategy_id, 0.0) or 0.0)) for strategy_id in strategy_ids)
        return sign * qty if qty > 1e-9 else float(aggregate_qty or 0.0)

    async def _ledger_current_qty(self, *, strategy_id: str, symbol: str) -> Optional[float]:
        try:
            row = await self.pg.fetchone(
                """
                SELECT COALESCE(SUM(qty), 0)::float8 AS ledger_qty
                FROM core.position_ledger_current
                WHERE strategy_id = $1
                  AND symbol = $2
                """,
                strategy_id,
                symbol,
            )
        except Exception as exc:
            logger.warning(
                "position_safety_ledger_current_qty_lookup_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return None
        if not row:
            return 0.0
        try:
            value = row.get("ledger_qty") if isinstance(row, dict) else row["ledger_qty"]
            return float(value or 0.0)
        except Exception:
            return None

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(str(value).strip())
        except Exception:
            return None

    async def _core_position_flat(self, *, strategy_id: str, symbol: str) -> bool:
        try:
            row = await self.pg.fetchone(
                """
                SELECT COALESCE(pos_qty, 0)::float8 AS pos_qty
                FROM core.core_position
                WHERE strategy_id = $1 AND symbol = $2
                """,
                strategy_id,
                symbol,
            )
        except Exception as exc:
            logger.warning(
                "position_safety_core_position_flat_lookup_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return False
        if not row:
            return True
        try:
            value = row.get("pos_qty") if isinstance(row, dict) else row["pos_qty"]
            return abs(float(value or 0.0)) <= 1e-9
        except Exception:
            return False

    async def _has_nonterminal_order(self, *, strategy_id: str, symbol: str) -> bool:
        try:
            row = await self.pg.fetchone(
                """
                SELECT 1 AS active
                FROM core.core_order
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
                LIMIT 1
                """,
                strategy_id,
                symbol,
            )
        except Exception as exc:
            logger.warning(
                "position_safety_nonterminal_order_lookup_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return True
        return bool(row)

    async def _get_active_manual_lock(self, *, strategy_id: str, symbol: str) -> dict[str, Any] | None:
        store = self.manual_intervention_lock_store
        if store is None:
            return None
        lock = await store.get_active(strategy_id=strategy_id, symbol=symbol)
        if not isinstance(lock, dict):
            return None
        normalized = dict(lock)
        normalized.setdefault("strategy_id", strategy_id)
        normalized.setdefault("symbol", symbol)
        return normalized

    @staticmethod
    def _is_position_safety_auto_lock(lock: dict[str, Any]) -> bool:
        reason = str(lock.get("reason") or "").strip()
        if reason != "position_safety_manual_required":
            return False
        context = lock.get("context")
        if not isinstance(context, dict):
            return True
        lock_id = str(context.get("lock_id") or "")
        ctx_reason = str(context.get("reason") or "")
        return lock_id.startswith("position_safety_manual_lock:") or ctx_reason.startswith(
            "broker_position_without_protection"
        )

    @staticmethod
    def _is_resolved_synthetic_recovery_lock(lock: dict[str, Any]) -> bool:
        reason = str(lock.get("reason") or "").strip()
        if reason != "strategy_recovery_action_quarantine":
            return False
        context = lock.get("context")
        if not isinstance(context, dict):
            return False
        ctx_reason = str(context.get("reason") or "").strip()
        return ctx_reason in {
            "db_persist_submitted_failed",
            "recovery_action_not_executable",
            "scoped_tradability_denied:owner_identity_incomplete",
        }

    @staticmethod
    def _quarantine_note_matches_owner_or_legacy_empty(note: Any, owner: dict[str, Any]) -> bool:
        if isinstance(note, str):
            try:
                note = json.loads(note) if note.strip() else {}
            except Exception:
                note = {}
        if not isinstance(note, dict):
            return False
        account_id = str(note.get("account_id") or "").strip()
        strategy_instance_id = str(note.get("strategy_instance_id") or "").strip()
        position_owner_key = str(note.get("position_owner_key") or "").strip()
        lot_id = str(note.get("position_lot_id") or note.get("lot_id") or "").strip()
        if not any((account_id, strategy_instance_id, position_owner_key, lot_id)):
            return bool(note.get("legacy_unscoped_synthetic") is True or owner.get("aggregate_owner_lanes_verified") is True)
        return (
            (not account_id or account_id == str(owner.get("account_id") or "").strip())
            and (
                not strategy_instance_id
                or strategy_instance_id == str(owner.get("strategy_instance_id") or owner.get("strategy_id") or "").strip()
            )
            and (not position_owner_key or position_owner_key == str(owner.get("position_owner_key") or "").strip())
            and (not lot_id or lot_id == str(owner.get("lot_id") or owner.get("position_lot_id") or "").strip())
        )

    @staticmethod
    def _is_owner_scoped_close_only_lock(lock: dict[str, Any]) -> bool:
        reason = str(lock.get("reason") or "").strip()
        context = lock.get("context") if isinstance(lock.get("context"), dict) else {}
        if reason not in {"flatten_action", "platform_eod_manual_required"}:
            return False
        if not bool(context.get("flatten_action") or context.get("close_only")):
            return False
        account_id = str(lock.get("account_id") or context.get("account_id") or "").strip()
        strategy_instance_id = str(
            lock.get("strategy_instance_id") or context.get("strategy_instance_id") or lock.get("strategy_id") or ""
        ).strip()
        symbol = str(lock.get("symbol") or context.get("symbol") or "").strip()
        owner_key = str(context.get("position_owner_key") or "").strip()
        lot_id = str(context.get("position_lot_id") or context.get("lot_id") or "").strip()
        return bool(account_id and strategy_instance_id and symbol and (owner_key or lot_id))

    @staticmethod
    def _manual_lock_identity(lock: dict[str, Any], *, strategy_id: str, symbol: str) -> str:
        context = lock.get("context") if isinstance(lock.get("context"), dict) else {}
        lock_id = str(context.get("lock_id") or "").strip()
        if lock_id:
            return lock_id
        account_id = str(lock.get("account_id") or context.get("account_id") or "").strip()
        strategy_instance_id = str(
            lock.get("strategy_instance_id") or context.get("strategy_instance_id") or strategy_id
        ).strip()
        lot_id = str(context.get("position_lot_id") or context.get("lot_id") or "").strip()
        reason = str(lock.get("reason") or "").strip() or "manual_lock"
        return ":".join(
            part
            for part in (
                reason,
                account_id,
                strategy_instance_id,
                str(symbol or "").strip().upper(),
                lot_id,
            )
            if part
        )

    async def _unlock_manual_lock(self, *, strategy_id: str, symbol: str) -> bool:
        store = self.manual_intervention_lock_store
        if store is None:
            return False
        try:
            unlocked = await store.unlock(strategy_id=strategy_id, symbol=symbol)
            if unlocked:
                logger.info(
                    "position_safety_auto_manual_lock_cleared",
                    strategy_id=strategy_id,
                    symbol=symbol,
                )
            return bool(unlocked)
        except Exception as exc:
            logger.warning(
                "position_safety_auto_manual_lock_clear_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return False

    async def _refresh_awaiting_market_reopen_manual_lock_context(
        self,
        *,
        strategy_id: str,
        symbol: str,
        lock: dict[str, Any],
        now_et: datetime,
    ) -> None:
        store = self.manual_intervention_lock_store
        if store is None:
            return
        try:
            service = self.recovery_incident_service
            if service is None or not hasattr(service, "awaiting_market_reopen_context"):
                return
            awaiting_context = service.awaiting_market_reopen_context(symbol=symbol)
            if not awaiting_context:
                return
            context = dict(lock.get("context") or {}) if isinstance(lock.get("context"), dict) else {}
            if context.get("awaiting_market_reopen_not_before") == awaiting_context.get("awaiting_market_reopen_not_before"):
                return
            ttl_sec = 24 * 60 * 60
            try:
                expire_ts = float(lock.get("expire_ts") or 0.0)
                now_ts = now_et.timestamp()
                if expire_ts > now_ts:
                    ttl_sec = max(1, int(expire_ts - now_ts))
            except Exception:
                ttl_sec = 24 * 60 * 60
            await store.lock(
                strategy_id=strategy_id,
                symbol=symbol,
                ttl_sec=ttl_sec,
                reason=str(lock.get("reason") or "position_safety_manual_required"),
                context={
                    **context,
                    **dict(awaiting_context or {}),
                    "awaiting_market_reopen_context_refreshed_at": now_et.isoformat(),
                },
            )
        except Exception as exc:
            logger.warning(
                "position_safety_awaiting_market_reopen_manual_lock_refresh_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )

    async def _has_active_manual_lock(self, *, strategy_id: str, symbol: str) -> bool:
        return bool(await self._get_active_manual_lock(strategy_id=strategy_id, symbol=symbol))

    def _contract_strategy_ids(self, symbol: str) -> list[str]:
        symbol_u = str(symbol or "").strip().upper()
        result: list[str] = []
        for contract in self.strategy_data_contracts:
            strategy_id = str(getattr(contract, "strategy_id", "") or "").strip()
            if not strategy_id or not bool(getattr(contract, "production_blocking", True)):
                continue
            symbols = {str(item or "").strip().upper() for item in getattr(contract, "symbols", ()) or ()}
            if symbol_u in symbols and strategy_id not in result:
                result.append(strategy_id)
        return result or [self.fallback_strategy_id]

    def _recovery_strategy_id(self, contract_strategy_ids: tuple[str, ...]) -> str:
        return str((contract_strategy_ids or (self.fallback_strategy_id,))[0] or self.fallback_strategy_id).strip()

    def _upsert_recovery_incident(
        self,
        *,
        strategy_id: str,
        symbol: str,
        qty: float,
        reason: str,
        now_et: datetime,
        active_quarantine: bool,
        protected_or_flattening: bool,
    ) -> dict[str, Any]:
        service = self.recovery_incident_service
        if service is None:
            return {}
        try:
            return service.upsert_active(
                strategy_id=strategy_id,
                symbol=symbol,
                qty=qty,
                reason=reason,
                now_et=now_et,
                source="position_safety_supervisor",
                active_quarantine=active_quarantine,
                protected_or_flattening=protected_or_flattening,
            )
        except Exception as exc:
            logger.warning("position_safety_recovery_incident_upsert_failed", symbol=symbol, error=str(exc))
            return {}

    def _mark_recovery_manual_required(
        self,
        *,
        strategy_id: str,
        symbol: str,
        now_et: datetime,
        manual_lock_ids: list[str],
        reason: str,
    ) -> None:
        service = self.recovery_incident_service
        if service is None:
            return
        try:
            service.mark_manual_required(
                strategy_id=strategy_id,
                symbol=symbol,
                now_et=now_et,
                manual_lock_ids=manual_lock_ids,
                reason=reason,
            )
        except Exception as exc:
            logger.warning("position_safety_recovery_incident_manual_required_failed", symbol=symbol, error=str(exc))

    async def _update_recovery_incidents(
        self,
        *,
        records: list[BrokerPositionSafetyRecord],
        active_quarantine: dict[str, Any],
        now_et: datetime,
    ) -> dict[str, Any]:
        service = self.recovery_incident_service
        if service is None:
            return {}
        try:
            nonflat_symbols = {str(record.symbol or "").strip().upper() for record in records}
            quarantine_symbols = {
                str(item.get("symbol") or "").strip().upper()
                for item in active_quarantine.get("items", []) or []
                if str(item.get("symbol") or "").strip()
            }
            resolved = service.resolve_verified_flat(
                nonflat_symbols=nonflat_symbols,
                active_quarantine_symbols=quarantine_symbols,
                protected_symbols={str(record.symbol or "").strip().upper() for record in records if record.protected_or_flattening},
                now_et=now_et,
            )
            index = service.write_index(now_et=now_et, resolved_verified_flat_count=len(resolved))
            return index
        except Exception as exc:
            logger.warning("position_safety_recovery_incident_index_failed", error=str(exc))
            return {"lookup_failed": True, "error": str(exc)}

    async def _submit_position_recovery_flatten(
        self,
        *,
        symbol: str,
        qty: float,
        contract_strategy_ids: tuple[str, ...],
        owner_lanes: tuple[dict[str, Any], ...],
        now_et: datetime,
        reason: str,
        recovery_incident_id: str = "",
    ) -> bool:
        manager = self.recovery_order_manager
        if manager is None or abs(float(qty or 0.0)) <= 1e-9:
            return False
        strategy_id = self._recovery_strategy_id(contract_strategy_ids)
        target_qty = -float(qty)
        side = "SELL" if qty > 0 else "BUY"
        owner_lane = self._recovery_owner_lane(owner_lanes=owner_lanes, strategy_id=strategy_id, symbol=symbol)
        account_id = str(owner_lane.get("account_id") or "").strip()
        strategy_instance_id = str(owner_lane.get("strategy_instance_id") or strategy_id or "").strip()
        lot_id = str(owner_lane.get("lot_id") or "").strip()
        decision_id = (
            f"position_safety_recovery:{now_et.date().isoformat()}:"
            f"{strategy_id}:{str(symbol or '').strip().upper()}:{'long' if qty > 0 else 'short'}"
        )
        order_ref = ""
        if account_id and strategy_instance_id and lot_id:
            token = "".join(ch for ch in decision_id if ch.isalnum())[-12:] or "recovery"
            order_ref = f"rflat:{account_id}|{strategy_instance_id}|{str(symbol or '').strip().upper()}|{lot_id}|{token}"
        try:
            from services.decision.pipeline import PipelineResult
            from services.decision.translator import ExecPlan

            result = PipelineResult(
                status="APPROVED",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=str(symbol or "").strip().upper(),
                stage="position_safety_supervisor",
                plan=ExecPlan(
                    order_type="MKT",
                    side=side,
                    qty=abs(float(qty)),
                    limit_px=None,
                    stop_px=None,
                    tif="DAY",
                    price_collar_pct=0.0,
                    oco={},
                    child={"type": "NONE"},
                    notes={"source": "position_safety_supervisor"},
                ),
                reasons=[reason],
                metadata={
                    "envelope_extras": {
                        "position_safety_recovery": True,
                        "runtime_recovery_action": True,
                        "flatten_action": True,
                        "close_only": True,
                        "broker_truth_recovery": True,
                        "recovery_incident_id": recovery_incident_id,
                        "recovery_requires_uom_safety_lane": True,
                        "broker_position_qty_hint": float(qty),
                        "position_qty_hint": float(qty),
                        "position_qty_hint_source": "position_safety_supervisor",
                        "target_qty": target_qty,
                        "suppress_manual_intervention_lock": True,
                        "account_id": account_id,
                        "strategy_instance_id": strategy_instance_id,
                        "position_lot_id": lot_id,
                        "lot_id": lot_id,
                        "owner_identity_source": "position_ledger_current",
                        "order_ref": order_ref[:120] if order_ref else "",
                    }
                },
            )
            submit = getattr(manager, "submit_flatten", None) or getattr(manager, "execute", None)
            if not callable(submit):
                return False
            outcome = submit(result)
            if hasattr(outcome, "__await__"):
                outcome = await outcome
            success = bool(getattr(outcome, "success", False))
            logger.warning(
                "position_safety_recovery_flatten_submitted" if success else "position_safety_recovery_flatten_rejected",
                strategy_id=strategy_id,
                symbol=str(symbol or "").strip().upper(),
                qty=float(qty),
                side=side,
                reason=reason,
                outcome_reason=str(getattr(outcome, "reason", "") or ""),
            )
            if success and self.recovery_incident_service is not None:
                try:
                    self.recovery_incident_service.mark_recovery_submitted(
                        strategy_id=strategy_id,
                        symbol=str(symbol or "").strip().upper(),
                        now_et=now_et,
                        decision_id=decision_id,
                        side=side,
                        qty=abs(float(qty)),
                    )
                except Exception as exc:
                    logger.warning(
                        "position_safety_recovery_incident_submit_mark_failed",
                        strategy_id=strategy_id,
                        symbol=str(symbol or "").strip().upper(),
                        error=str(exc),
                    )
            return success
        except Exception as exc:
            logger.warning(
                "position_safety_recovery_flatten_submit_failed",
                strategy_id=strategy_id,
                symbol=str(symbol or "").strip().upper(),
                qty=float(qty),
                reason=reason,
                error=str(exc),
            )
            return False

    async def _create_manual_locks(
        self,
        *,
        symbol: str,
        qty: float,
        contract_strategy_ids: tuple[str, ...],
        now_et: datetime,
        reason: str,
    ) -> list[str]:
        store = self.manual_intervention_lock_store
        if store is None:
            return []
        lock_ids: list[str] = []
        for strategy_id in contract_strategy_ids:
            lock_id = f"position_safety_manual_lock:{now_et.date().isoformat()}:{strategy_id}:{symbol}"
            awaiting_context = {}
            try:
                service = self.recovery_incident_service
                if service is not None and hasattr(service, "awaiting_market_reopen_context"):
                    awaiting_context = service.awaiting_market_reopen_context(symbol=symbol)
            except Exception:
                awaiting_context = {}
            await store.lock(
                strategy_id=strategy_id,
                symbol=symbol,
                ttl_sec=24 * 60 * 60,
                reason="position_safety_manual_required",
                context={
                    "lock_id": lock_id,
                    "symbol": symbol,
                    "qty": float(qty),
                    "reason": reason,
                    "created_at": now_et.isoformat(),
                    **dict(awaiting_context or {}),
                },
            )
            lock_ids.append(lock_id)
        return lock_ids

    def _write_artifact(self, artifact: dict[str, Any]) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / "position_safety_latest.json"
        path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
