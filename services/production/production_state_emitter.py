from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from libs.logger import get_logger

logger = get_logger(__name__)


class ProductionStateEmitter:
    """Periodic in-process production go/no-go heartbeat.

    The watchdog still owns alert transitions.  This emitter only writes the
    machine-checkable production-state artifact from inside `run_system`, so
    stale external scheduling cannot make the latest production truth disappear.
    """

    def __init__(
        self,
        *,
        repo_root: str | Path,
        enabled: bool = True,
        interval_sec: float = 30.0,
        system_health_controller: Any = None,
        runtime_lifecycle_controller: Any = None,
        readiness_refreshers: list[tuple[str, Callable[[], Awaitable[Any]]]] | None = None,
        readiness_refresh_timeout_sec: float = 20.0,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.enabled = bool(enabled)
        self.interval_sec = max(5.0, float(interval_sec or 30.0))
        self.system_health_controller = system_health_controller
        self.runtime_lifecycle_controller = runtime_lifecycle_controller
        self.readiness_refreshers = list(readiness_refreshers or [])
        self.readiness_refresh_timeout_sec = max(1.0, float(readiness_refresh_timeout_sec or 20.0))
        self.exit_on_core_stopped = str(os.getenv("PRODUCTION_STATE_EXIT_ON_CORE_STOPPED", "0")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        self.exit_after_consecutive = max(1, int(os.getenv("PRODUCTION_STATE_EXIT_AFTER_CORE_STOPPED_COUNT", "3")))
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._core_stopped_streak = 0

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self._running and self._task is not None and not self._task.done())

    async def start(self) -> None:
        if not self.enabled:
            logger.info("production_state_emitter_disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self.run_forever(), name="production_state_emitter")
        logger.info("production_state_emitter_started", interval_sec=self.interval_sec)

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
                logger.error("production_state_emitter_error", error=str(exc), exc_info=True)
            await asyncio.sleep(self.interval_sec)

    async def run_once(self) -> dict[str, Any]:
        await self._refresh_readiness_dependencies()
        state = await asyncio.to_thread(self._run_once_sync)
        await self._sync_system_health(state)
        return state

    async def _refresh_readiness_dependencies(self) -> None:
        for name, refresher in self.readiness_refreshers:
            try:
                await asyncio.wait_for(refresher(), timeout=self.readiness_refresh_timeout_sec)
            except asyncio.TimeoutError:
                logger.error(
                    "production_state_readiness_refresh_timeout",
                    dependency=name,
                    timeout_sec=self.readiness_refresh_timeout_sec,
                )
            except Exception as exc:
                logger.error(
                    "production_state_readiness_refresh_failed",
                    dependency=name,
                    error=str(exc),
                    exc_info=True,
                )

    async def _sync_system_health(self, state: dict[str, Any]) -> None:
        lifecycle_snapshot = self._sync_runtime_lifecycle(state)
        controller = self.system_health_controller
        if controller is None:
            return
        if lifecycle_snapshot is not None:
            await self._publish_lifecycle_health_projection(state=state, lifecycle_snapshot=lifecycle_snapshot)
            return
        go_no_go = str(state.get("go_no_go") or "").strip().lower()
        failures = list(state.get("failures") or [])
        publish = getattr(controller, "publish", None)
        if go_no_go != "go":
            metadata = {
                "production_state_go_no_go": go_no_go or "unknown",
                "production_state_failure_count": len(failures),
                "production_state_failures": failures[:20],
            }
            hard_failures = self._hard_production_state_failures(failures)
            if not hard_failures:
                metadata["production_state_soft_no_go"] = True
                if self._only_pause_derived_failures(failures):
                    current_state = str(getattr(controller, "state", "") or "").strip().lower()
                    if current_state in {"degraded", "recovering"} and callable(publish):
                        metadata["production_state_pause_derived_only"] = True
                        try:
                            await publish(
                                event_type="system_resumed",
                                reason="production_state_pause_derived_only",
                                source="production_state_emitter",
                                detail="soft_no_go_only_contains_system_paused",
                                metadata=metadata,
                            )
                        except Exception as exc:
                            logger.warning("production_state_soft_resume_failed", error=str(exc))
                        return
                heartbeat = getattr(controller, "emit_heartbeat", None)
                if callable(heartbeat):
                    try:
                        heartbeat(
                            reason="production_state_soft_no_go",
                            source="production_state_emitter",
                            metadata=metadata,
                        )
                    except Exception as exc:
                        logger.warning("production_state_soft_heartbeat_failed", error=str(exc))
                return
            if callable(publish):
                try:
                    await publish(
                        event_type="system_degraded",
                        reason="production_state_no_go",
                        source="production_state_emitter",
                        detail=f"failure_count={len(failures)}",
                        metadata=metadata,
                    )
                except Exception as exc:
                    logger.warning("production_state_health_degrade_failed", error=str(exc))
            return
        current_state = str(getattr(controller, "state", "") or "").strip().lower()
        if current_state in {"degraded", "recovering"} and callable(publish):
            last_event = dict(getattr(controller, "last_event", {}) or {})
            metadata = dict(last_event.get("metadata") or {})
            metadata["production_state_go_no_go"] = "go"
            metadata["production_state_failure_count"] = 0
            try:
                await publish(
                    event_type="system_resumed",
                    reason="production_state_go",
                    source="production_state_emitter",
                    detail="production_state_go",
                    metadata=metadata,
                )
            except Exception as exc:
                logger.warning("production_state_health_resume_failed", error=str(exc))
            return
        heartbeat = getattr(controller, "emit_heartbeat", None)
        if callable(heartbeat):
            try:
                heartbeat(reason="production_state_emitter_heartbeat", source="production_state_emitter")
            except Exception as exc:
                logger.warning("production_state_health_heartbeat_failed", error=str(exc))

    def _sync_runtime_lifecycle(self, state: dict[str, Any]) -> Any:
        controller = self.runtime_lifecycle_controller
        if controller is None:
            return None
        try:
            from services.runtime.lifecycle_controller import RuntimeLifecycleState

            go_no_go = str(state.get("go_no_go") or "").strip().lower()
            entry_go_no_go = str(state.get("entry_go_no_go") or "").strip().lower()
            failures = list(state.get("failures") or [])
            hard_failures = self._hard_production_state_failures(failures)
            metadata = {
                "production_state_go_no_go": go_no_go or "unknown",
                "production_state_entry_go_no_go": entry_go_no_go or "unknown",
                "production_state_failure_count": len(failures),
                "production_state_failures": failures[:20],
            }
            if go_no_go == "go":
                return controller.mark_running(
                    partial_entry=entry_go_no_go == "partial",
                    reason="production_state_go",
                    source="production_state_emitter",
                    metadata=metadata,
                )
            if hard_failures:
                return controller.transition(
                    RuntimeLifecycleState.DEGRADED_CLOSE_ONLY,
                    reason="production_state_hard_no_go",
                    source="production_state_emitter",
                    metadata=metadata,
                    clear_conditions=("production_state_go",),
                )
            return controller.transition(
                RuntimeLifecycleState.RUNNING_PARTIAL_ENTRY,
                reason="production_state_soft_no_go",
                source="production_state_emitter",
                metadata=metadata,
                clear_conditions=("scoped_entry_ready",),
            )
        except Exception as exc:
            logger.warning("production_state_lifecycle_sync_failed", error=str(exc), exc_info=True)
            return None

    async def _publish_lifecycle_health_projection(self, *, state: dict[str, Any], lifecycle_snapshot: Any) -> None:
        controller = self.system_health_controller
        if controller is None:
            return
        publish = getattr(controller, "publish", None)
        heartbeat = getattr(controller, "emit_heartbeat", None)
        lifecycle_state = str(getattr(lifecycle_snapshot, "state", "") or "").strip().upper()
        metadata = dict(getattr(lifecycle_snapshot, "metadata", {}) or {})
        metadata["runtime_lifecycle_state"] = lifecycle_state
        metadata["runtime_lifecycle_generation"] = getattr(lifecycle_snapshot, "generation", None)
        if lifecycle_state in {"DEGRADED_CLOSE_ONLY", "MANUAL_REQUIRED", "HALTED"} and callable(publish):
            try:
                await publish(
                    event_type="system_degraded" if lifecycle_state == "DEGRADED_CLOSE_ONLY" else "system_halted",
                    reason="runtime_lifecycle_projection",
                    source="production_state_emitter",
                    detail=f"runtime_lifecycle_state={lifecycle_state}",
                    metadata=metadata,
                )
            except Exception as exc:
                logger.warning("production_state_lifecycle_degrade_publish_failed", error=str(exc))
            return
        if lifecycle_state in {"RUNNING", "RUNNING_PARTIAL_ENTRY"}:
            current_state = str(getattr(controller, "state", "") or "").strip().lower()
            if current_state in {"degraded", "recovering", "halted"} and callable(publish):
                try:
                    await publish(
                        event_type="system_resumed",
                        reason="runtime_lifecycle_projection",
                        source="production_state_emitter",
                        detail=f"runtime_lifecycle_state={lifecycle_state}",
                        metadata=metadata,
                    )
                except Exception as exc:
                    logger.warning("production_state_lifecycle_resume_publish_failed", error=str(exc))
                return
        if callable(heartbeat):
            try:
                heartbeat(reason="runtime_lifecycle_projection", source="production_state_emitter", metadata=metadata)
            except Exception as exc:
                logger.warning("production_state_lifecycle_heartbeat_failed", error=str(exc))

    @staticmethod
    def _hard_production_state_failures(failures: list[Any]) -> list[dict[str, Any]]:
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
            "session_end",
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
        hard: list[dict[str, Any]] = []
        for item in failures:
            if not isinstance(item, dict):
                hard.append({"issue_key": "unknown_failure"})
                continue
            key = str(item.get("issue_key") or "").strip()
            if not key:
                hard.append(item)
                continue
            if key in soft_exact or key.startswith(soft_prefixes):
                continue
            if key.startswith("data_availability:") and ":strategy_tradability:" in key:
                continue
            if key.startswith(hard_prefixes):
                hard.append(item)
                continue
            hard.append(item)
        return hard

    @staticmethod
    def _only_pause_derived_failures(failures: list[Any]) -> bool:
        if not failures:
            return False
        for item in failures:
            if not isinstance(item, dict):
                return False
            key = str(item.get("issue_key") or "").strip()
            detail = str(item.get("detail") or "").strip()
            if key.startswith("strategy_tradability:") and (
                key.split(":")[-1] == "system_paused" or detail == "system_paused"
            ):
                continue
            if key.startswith("system_health:") and (
                key.split(":")[-1] in {"system_paused", "degraded", "recovering"}
            ):
                continue
            return False
        return True

    def _run_once_sync(self) -> dict[str, Any]:
        from tool.ops.run_runtime_operations_watchdog import (
            _write_production_state,
            build_runtime_operations_watchdog_report,
        )

        report = build_runtime_operations_watchdog_report(repo_root=self.repo_root)
        if self._maybe_build_current_process_ib_restart_drill(report):
            report = build_runtime_operations_watchdog_report(repo_root=self.repo_root)
        state = _write_production_state(self.repo_root, report)
        self._apply_lifecycle_policy(state)
        logger.info(
            "production_state_emitted",
            go_no_go=state.get("go_no_go"),
            failure_count=state.get("failure_count"),
            artifact_path=state.get("artifact_path"),
        )
        return state

    def _maybe_build_current_process_ib_restart_drill(self, report: dict[str, Any]) -> bool:
        ib_status = dict(report.get("ib_restart_drill") or {})
        needs_rebuild = (
            (not bool(ib_status.get("artifact_present")))
            or bool(ib_status.get("artifact_before_process_start"))
            or bool(ib_status.get("generated_at_missing"))
            or str(ib_status.get("go_no_go") or "").strip().lower() != "go"
        )
        if not bool(ib_status.get("required")) and not (bool(ib_status.get("artifact_present")) and needs_rebuild):
            return False
        if not needs_rebuild:
            return False
        runtime_version = dict(report.get("runtime_version") or {})
        process_start_time = str(
            ib_status.get("process_start_time")
            or runtime_version.get("process_start_time")
            or ""
        ).strip()
        try:
            from services.production.ib_restart_drill_artifact import build_ib_restart_drill_artifact

            artifact = build_ib_restart_drill_artifact(
                repo_root=self.repo_root,
                start_ts=process_start_time or None,
                drill_id="paper_ib_restart_latest",
            )
            logger.warning(
                "production_state_ib_restart_drill_built",
                go_no_go=artifact.get("go_no_go"),
                blocker_count=len(list(artifact.get("blockers") or [])),
                process_start_time=process_start_time,
            )
            return True
        except Exception as exc:
            logger.error(
                "production_state_ib_restart_drill_build_failed",
                error=str(exc),
                process_start_time=process_start_time,
                exc_info=True,
            )
            return False

    def _apply_lifecycle_policy(self, state: dict[str, Any]) -> None:
        failures = list(state.get("failures") or [])
        critical = [
            failure
            for failure in failures
            if self._is_core_service_lifecycle_failure(failure)
        ]
        if critical:
            self._core_stopped_streak += 1
        else:
            self._core_stopped_streak = 0
        if not critical:
            return
        logger.error(
            "production_state_core_lifecycle_failure",
            streak=self._core_stopped_streak,
            exit_after=self.exit_after_consecutive,
            exit_on_core_stopped=self.exit_on_core_stopped,
            failures=critical,
        )
        if not self.exit_on_core_stopped:
            return
        if self._core_stopped_streak >= self.exit_after_consecutive:
            logger.error(
                "production_state_core_lifecycle_hard_exit",
                streak=self._core_stopped_streak,
                exit_code=78,
            )
            os._exit(78)

    @staticmethod
    def _is_core_service_lifecycle_failure(failure: dict[str, Any]) -> bool:
        key = str(failure.get("issue_key") or failure.get("error_type") or "").strip().lower()
        service = str(failure.get("service") or "").strip().upper()
        detail = str(failure.get("detail") or "").strip().lower()
        if key in {
            "position_safety_artifact_missing",
            "position_safety_artifact_stale",
            "system_health_transition_stale",
        }:
            return True
        if key.startswith("recovery_readiness:") and (
            "not_ready" in key or "stale" in key or "shutdown" in key
        ):
            return True
        if service in {"SYSTEM_HEALTH", "POSITION_SAFETY", "RECOVERY_READINESS"} and (
            "stale" in detail or "shutdown" in detail or "not_ready" in detail
        ):
            return True
        return False
