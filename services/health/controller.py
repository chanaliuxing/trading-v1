from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


class SystemHealthController:
    """
    System-wide health coordinator.

    Phase 1 scope:
    - explicit strategy subscription/admission
    - pause/resume event fanout
    - IB disconnect/connect event bridge
    - optional alert sink callback
    """

    def __init__(
        self,
        *,
        alert_sink: Optional[Callable[..., Awaitable[bool]]] = None,
        audit_writer: Optional[Any] = None,
    ) -> None:
        self._alert_sink = alert_sink
        self._audit_writer = audit_writer
        self._lock = asyncio.Lock()
        self._strategies: Dict[str, Any] = {}
        self._subscriptions: Dict[str, datetime] = {}
        self._state: str = "starting"
        self._production_state_go_no_go: str = "unknown"
        self._production_state_failure_count: int = 0
        self._production_state_failures: list[Dict[str, Any]] = []
        self._last_event: Dict[str, Any] = {
            "event_type": "system_starting",
            "reason": "bootstrap",
            "source": "system_health_controller",
            "ts_et": datetime.now(ET).isoformat(),
        }

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_event(self) -> Dict[str, Any]:
        return dict(self._last_event)

    def attach_context(self, ctx: Any) -> None:
        setattr(ctx, "system_health", self)

    def set_audit_writer(self, audit_writer: Optional[Any]) -> None:
        self._audit_writer = audit_writer

    async def register_strategy(self, strategy_id: str, strategy: Any) -> None:
        async with self._lock:
            self._strategies[str(strategy_id)] = strategy

    async def subscribe(self, strategy_id: str, strategy: Any) -> None:
        strategy_key = str(strategy_id)
        async with self._lock:
            self._strategies[strategy_key] = strategy
            self._subscriptions[strategy_key] = datetime.now(ET)
            last_event = dict(self._last_event)
        logger.info("system_health_strategy_subscribed", strategy_id=strategy_key)
        if str(last_event.get("event_type") or "").strip().lower() in {
            "ib_disconnected",
            "system_paused",
            "pause",
            "system_recovering",
            "system_halted",
            "halt",
        }:
            await self._notify_strategy(strategy_key, strategy, last_event)

    async def assert_strategy_admitted(self, strategy_id: str) -> None:
        strategy_key = str(strategy_id)
        async with self._lock:
            subscribed = strategy_key in self._subscriptions
        if subscribed:
            return
        logger.error("system_health_strategy_rejected_missing_subscription", strategy_id=strategy_key)
        raise RuntimeError(f"system_health_subscription_required:{strategy_key}")

    async def publish(
        self,
        *,
        event_type: str,
        reason: str,
        source: str,
        detail: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = {
            "event_type": str(event_type or "system_event"),
            "reason": str(reason or "unknown"),
            "source": str(source or "system_health_controller"),
            "detail": str(detail or ""),
            "metadata": dict(metadata or {}),
            "ts_et": datetime.now(ET).isoformat(),
        }
        event_type_lower = payload["event_type"].lower()
        self._record_production_state_metadata(payload)
        if event_type_lower in {"ib_disconnected", "system_paused", "pause"}:
            self._state = "paused"
        elif event_type_lower == "ib_connected":
            self._state = "recovering"
            payload["event_type"] = "system_recovering"
            payload["reason"] = payload["reason"] or "ib_connected_waiting_for_broker_truth"
        elif event_type_lower == "system_recovering":
            self._state = "recovering"
        elif event_type_lower in {"system_resumed", "resume"}:
            if not self._resume_conditions_met(payload):
                self._state = "recovering"
                payload["event_type"] = "system_recovering"
                payload["reason"] = "resume_conditions_not_met"
            else:
                self._state = "running"
        elif event_type_lower in {"system_halted", "halt"}:
            self._state = "halted"
        elif event_type_lower in {"system_degraded"}:
            self._state = "degraded"
        self._last_event = payload
        self._emit_transition_audit(payload)

        await self._maybe_send_alert(payload)
        await self._fanout(payload)

    def _emit_transition_audit(self, payload: Dict[str, Any]) -> None:
        event_type = str((payload or {}).get("event_type") or "").strip().lower()
        if event_type not in {
            "ib_disconnected",
            "system_paused",
            "system_recovering",
            "system_resumed",
            "system_degraded",
            "system_halted",
        }:
            return
        logger.info(
            "system_health_transition",
            state=self._state,
            event_type=payload.get("event_type"),
            reason=payload.get("reason"),
            source=payload.get("source"),
            detail=payload.get("detail"),
            metadata=dict(payload.get("metadata") or {}),
            ts_et=payload.get("ts_et"),
        )
        writer = getattr(self, "_audit_writer", None)
        if writer is None:
            return
        try:
            writer.emit(
                domain="system",
                component="system_health_controller",
                stage="health_transition",
                status=self._state,
                reason=str(payload.get("reason") or ""),
                payload={
                    "state": self._state,
                    "event_type": payload.get("event_type"),
                    "source": payload.get("source"),
                    "detail": payload.get("detail"),
                    "metadata": dict(payload.get("metadata") or {}),
                    "ts_et": payload.get("ts_et"),
                },
            )
        except Exception as exc:
            logger.warning("system_health_audit_emit_failed", error=str(exc), event_type=event_type)

    def emit_heartbeat(
        self,
        *,
        reason: str = "periodic_health_heartbeat",
        source: str = "system_health_controller",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Refresh machine-checkable health evidence without changing state or fanning out."""
        last_event = dict(self._last_event or {})
        merged_metadata = dict(last_event.get("metadata") or {})
        merged_metadata.update(dict(metadata or {}))
        payload = {
            "event_type": "system_resumed" if self._state == "running" else f"system_{self._state}",
            "reason": str(reason or "periodic_health_heartbeat"),
            "source": str(source or "system_health_controller"),
            "detail": str(last_event.get("detail") or ""),
            "metadata": merged_metadata,
            "ts_et": datetime.now(ET).isoformat(),
        }
        self._record_production_state_metadata(payload)
        self._emit_transition_audit(payload)

    async def publish_lifecycle_snapshot(self, snapshot: Any, *, source: str = "runtime_lifecycle_controller") -> None:
        state = str(getattr(snapshot, "state", "") or "").strip().upper()
        metadata = dict(getattr(snapshot, "metadata", {}) or {})
        metadata["runtime_lifecycle_state"] = state
        metadata["runtime_lifecycle_generation"] = getattr(snapshot, "generation", None)
        metadata["runtime_lifecycle_allowed_flows"] = dict(getattr(snapshot, "allowed_flows", {}) or {})
        if state in {"RUNNING", "RUNNING_PARTIAL_ENTRY"}:
            await self.publish(
                event_type="system_resumed",
                reason="runtime_lifecycle_projection",
                source=source,
                detail=f"runtime_lifecycle_state={state}",
                metadata=metadata,
            )
            return
        if state in {"RECOVERING", "BROKER_TRUTH_SYNCING", "RUNTIME_RECONCILING", "PROTECTIVE_VERIFYING"}:
            await self.publish(
                event_type="system_recovering",
                reason="runtime_lifecycle_projection",
                source=source,
                detail=f"runtime_lifecycle_state={state}",
                metadata=metadata,
            )
            return
        if state in {"DEGRADED_CLOSE_ONLY"}:
            await self.publish(
                event_type="system_degraded",
                reason="runtime_lifecycle_projection",
                source=source,
                detail=f"runtime_lifecycle_state={state}",
                metadata=metadata,
            )
            return
        if state in {"MANUAL_REQUIRED", "HALTED", "STARTUP_PREFLIGHT_FAILED"}:
            await self.publish(
                event_type="system_halted",
                reason="runtime_lifecycle_projection",
                source=source,
                detail=f"runtime_lifecycle_state={state}",
                metadata=metadata,
            )
            return
        self.emit_heartbeat(reason="runtime_lifecycle_projection", source=source, metadata=metadata)

    def _record_production_state_metadata(self, payload: Dict[str, Any]) -> None:
        metadata = dict((payload or {}).get("metadata") or {})
        value = str(metadata.get("production_state_go_no_go") or "").strip().lower()
        if not value:
            return
        self._production_state_go_no_go = value
        try:
            self._production_state_failure_count = int(metadata.get("production_state_failure_count") or 0)
        except Exception:
            self._production_state_failure_count = 0
        raw_failures = metadata.get("production_state_failures")
        if isinstance(raw_failures, list):
            self._production_state_failures = [item for item in raw_failures if isinstance(item, dict)]
        else:
            self._production_state_failures = []

    async def on_ib_connection_event(self, event: Dict[str, Any]) -> None:
        raw_event = str((event or {}).get("event_type") or "").strip().lower()
        source = str((event or {}).get("source") or "ib_connection_manager").strip() or "ib_connection_manager"
        detail = str((event or {}).get("detail") or "").strip()
        if raw_event == "ib_disconnected":
            await self.publish(
                event_type="ib_disconnected",
                reason=str((event or {}).get("reason") or "ib_exec_disconnected"),
                source=source,
                detail=detail,
                metadata=dict(event or {}),
            )
            return
        if raw_event == "ib_connected":
            await self.publish(
                event_type="ib_connected",
                reason=str((event or {}).get("reason") or "ib_exec_connected"),
                source=source,
                detail=detail,
                metadata=dict(event or {}),
            )
            return
        if raw_event == "broker_api_ready":
            await self.publish(
                event_type="ib_connected",
                reason=str((event or {}).get("reason") or "broker_api_ready_waiting_for_reconciler"),
                source=source,
                detail=detail,
                metadata=dict(event or {}),
            )
            return
        if raw_event == "system_resumed":
            await self.publish(
                event_type="system_resumed",
                reason=str((event or {}).get("reason") or "ib_runtime_recovery_ready"),
                source=source,
                detail=detail,
                metadata=dict(event or {}),
            )
            return
        if raw_event == "ib_gateway_api_unhealthy":
            reason = str((event or {}).get("reason") or "gateway_api_unhealthy")
            action = str((event or {}).get("action") or "manual_gateway_api_recovery_required")
            await self.publish(
                event_type="system_halted",
                reason=reason,
                source=source,
                detail=detail or action,
                metadata=dict(event or {}),
            )

    async def _fanout(self, payload: Dict[str, Any]) -> None:
        async with self._lock:
            strategies = list(self._strategies.items())
        for strategy_id, strategy in strategies:
            await self._notify_strategy(strategy_id, strategy, payload)

    async def _notify_strategy(self, strategy_id: str, strategy: Any, payload: Dict[str, Any]) -> None:
        try:
            if hasattr(strategy, "on_system_health_event"):
                await strategy.on_system_health_event(dict(payload))
            event_type_lower = str(payload.get("event_type") or "").strip().lower()
            if event_type_lower in {"ib_disconnected", "system_paused", "pause"} and hasattr(strategy, "on_system_paused"):
                await strategy.on_system_paused(dict(payload))
            elif event_type_lower in {"system_resumed", "resume"} and hasattr(strategy, "on_system_resumed"):
                await strategy.on_system_resumed(dict(payload))
            elif event_type_lower in {"system_recovering"} and hasattr(strategy, "on_system_paused"):
                await strategy.on_system_paused(dict(payload))
            elif event_type_lower in {"system_halted", "halt"} and hasattr(strategy, "on_system_halted"):
                await strategy.on_system_halted(dict(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "system_health_strategy_callback_failed",
                strategy_id=strategy_id,
                event_type=payload.get("event_type"),
                error=str(exc),
            )

    async def _maybe_send_alert(self, payload: Dict[str, Any]) -> None:
        if self._alert_sink is None:
            return
        event_type = str(payload.get("event_type") or "").strip().lower()
        if event_type == "system_recovering" and str(payload.get("reason") or "") == "resume_conditions_not_met":
            return
        if event_type not in {"ib_disconnected", "ib_connected", "system_recovering", "system_resumed", "system_halted"}:
            return
        status = "ERROR" if event_type in {"ib_disconnected", "system_halted"} else "INFO"
        try:
            await self._alert_sink(
                status=status,
                reason=str(payload.get("reason") or event_type),
                service="IB_CONNECTION",
                error=str(payload.get("detail") or payload.get("reason") or event_type),
                error_type=event_type,
                detail=str(payload.get("source") or "system_health_controller"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("system_health_alert_sink_failed", error=str(exc), event_type=event_type)

    def _resume_conditions_met(self, payload: Dict[str, Any]) -> bool:
        if self._production_state_blocks_resume():
            return False
        metadata = dict((payload or {}).get("metadata") or {})
        lifecycle_state = str(metadata.get("runtime_lifecycle_state") or "").strip().upper()
        if (
            str((payload or {}).get("reason") or "").strip() == "runtime_lifecycle_projection"
            and lifecycle_state in {"RUNNING", "RUNNING_PARTIAL_ENTRY"}
        ):
            return True
        if (
            str((payload or {}).get("source") or "").strip() == "production_state_emitter"
            and bool(metadata.get("production_state_soft_no_go"))
        ):
            return True
        if bool(metadata.get("platform_recovery_ready")) is not True:
            return False
        recovery_readiness = metadata.get("recovery_readiness")
        if not isinstance(recovery_readiness, dict) or bool(recovery_readiness.get("ready")) is not True:
            return False
        required = (
            "broker_connected",
            "broker_truth_complete",
            "order_truth_clean",
            "position_ledger_clean",
            "strategies_paused_ack",
        )
        return all(bool(metadata.get(key)) for key in required)

    def _production_state_blocks_resume(self) -> bool:
        """Return True only for production-state blockers that must prevent resume.

        ProductionState includes both hard safety blockers and post-resume trading
        blockers.  During startup/recovery, strategy tradability can legitimately
        report `system_paused`; using that as a resume precondition creates a
        circular latch: health cannot resume until strategies are tradable, and
        strategies cannot become tradable until health resumes.  Keep the hard
        safety blockers fail-closed, but let recovery proceed past resume-derived
        blockers so the next production-state heartbeat can evaluate strategies
        under a running system.
        """
        if self._production_state_go_no_go != "no_go":
            return False
        failures = list(self._production_state_failures or [])
        if not failures:
            return bool(self._production_state_failure_count)

        hard_prefixes = (
            "active_quarantine",
            "broker",
            "data_availability",
            "decision_pipeline",
            "execution",
            "manual_intervention",
            "platform_eod",
            "position_ledger",
            "position_safety",
            "recovery_readiness",
            "runtime_reconciliation",
        )
        soft_exact = {
            "ib_restart_drill_artifact_missing",
            "ib_restart_drill_artifact_stale",
            "ib_restart_drill_no_go",
            "system_health_transition_stale",
        }
        soft_prefixes = (
            "strategy_tradability:",
            "system_health:",
        )
        for item in failures:
            key = str(item.get("issue_key") or "").strip()
            if not key:
                return True
            if key in soft_exact or key.startswith(soft_prefixes):
                continue
            if key.startswith("data_availability:") and ":strategy_tradability:" in key:
                continue
            if key.startswith(hard_prefixes):
                return True
            return True
        return False
