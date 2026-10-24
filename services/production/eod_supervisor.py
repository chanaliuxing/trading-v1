from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from services.production.recovery_incident_service import RecoveryIncidentService

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        parts = [int(p) for p in str(value or "").strip().split(":")]
        if len(parts) == 2:
            return time(parts[0], parts[1])
        if len(parts) >= 3:
            return time(parts[0], parts[1], parts[2])
    except Exception:
        pass
    return fallback


@dataclass(frozen=True)
class SessionClosePolicy:
    strategy_id: str
    strategy_instance_id: str = ""
    account_id: str = ""
    symbol: str = ""
    session: str = "RTH"
    policy_type: str = "RTH_ONLY_EQUITY"
    flatten_enabled: bool = True
    close_only_allowed: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Any) -> "SessionClosePolicy":
        data = dict(value or {}) if isinstance(value, dict) else {}
        return cls(
            strategy_id=str(data.get("strategy_id") or data.get("id") or "").strip(),
            strategy_instance_id=str(data.get("strategy_instance_id") or "").strip(),
            account_id=str(data.get("account_id") or data.get("account") or "").strip(),
            symbol=str(data.get("symbol") or "").strip().upper(),
            session=str(data.get("session") or "RTH").strip().upper() or "RTH",
            policy_type=str(data.get("policy_type") or data.get("session_policy") or "RTH_ONLY_EQUITY").strip().upper(),
            flatten_enabled=bool(data.get("flatten_enabled", data.get("eod_flatten_enabled", True))),
            close_only_allowed=bool(data.get("close_only_allowed", True)),
            metadata=dict(data.get("metadata") or {}),
        )

    def should_flatten(self) -> bool:
        if not self.strategy_id or not self.flatten_enabled or not self.close_only_allowed:
            return False
        return self.policy_type in {"RTH_ONLY_EQUITY", "OPTIONS_RTH_ONLY_DEFAULT", "TEST_EXTENDED_CLOSE"}

    def to_artifact(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_instance_id": self.strategy_instance_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "session": self.session,
            "policy_type": self.policy_type,
            "flatten_enabled": self.flatten_enabled,
            "close_only_allowed": self.close_only_allowed,
            "selected_for_flatten": self.should_flatten(),
            "metadata": dict(self.metadata or {}),
        }


def session_close_policies_from_config(config: dict[str, Any]) -> tuple[SessionClosePolicy, ...]:
    policies: list[SessionClosePolicy] = []
    strategies = config.get("strategies") if isinstance(config, dict) else []
    if not isinstance(strategies, list):
        return ()
    for spec in strategies:
        if not isinstance(spec, dict) or not bool(spec.get("enabled")):
            continue
        strategy_id = str(spec.get("id") or spec.get("strategy_id") or "").strip()
        if not strategy_id:
            continue
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
        manifest = extras.get("production_manifest") if isinstance(extras.get("production_manifest"), dict) else {}
        eod = manifest.get("eod_contract") if isinstance(manifest.get("eod_contract"), dict) else {}
        if not (bool(eod.get("platform_eod_required")) or bool(eod.get("no_overnight"))):
            continue
        policies.append(
            SessionClosePolicy(
                strategy_id=strategy_id,
                strategy_instance_id=str(extras.get("strategy_instance_id") or strategy_id).strip(),
                account_id=str(extras.get("account_id") or extras.get("account") or "").strip(),
                symbol=_first_strategy_symbol(extras),
                session="RTH",
                policy_type=_policy_type_for_strategy(spec=spec, extras=extras),
                flatten_enabled=bool(eod.get("platform_eod_required", eod.get("no_overnight", True))),
                close_only_allowed=True,
                metadata={
                    "source": "production_manifest.eod_contract",
                    "no_overnight": bool(eod.get("no_overnight")),
                    "platform_eod_required": bool(eod.get("platform_eod_required")),
                },
            )
        )
    return tuple(policies)


def _first_strategy_symbol(extras: dict[str, Any]) -> str:
    data_contract = extras.get("data_contract") if isinstance(extras.get("data_contract"), dict) else {}
    symbols = data_contract.get("symbols") if isinstance(data_contract.get("symbols"), list) else []
    for value in symbols:
        symbol = str(value or "").strip().upper()
        if symbol:
            return symbol
    universe = extras.get("universe") if isinstance(extras.get("universe"), list) else []
    for lane in universe:
        if not isinstance(lane, dict):
            continue
        for key in ("long_exec_symbol", "short_exec_symbol", "signal_symbol", "symbol"):
            symbol = str(lane.get(key) or "").strip().upper()
            if symbol:
                return symbol
    policy_params = extras.get("policy_params") if isinstance(extras.get("policy_params"), dict) else {}
    return str(policy_params.get("symbol") or extras.get("symbol") or "").strip().upper()


def _policy_type_for_strategy(*, spec: dict[str, Any], extras: dict[str, Any]) -> str:
    module = str(spec.get("module") or "").lower()
    cls = str(spec.get("class") or "").lower()
    if "option" in module or "option" in cls:
        return "OPTIONS_RTH_ONLY_DEFAULT"
    session_cfg = extras.get("gate_overrides") if isinstance(extras.get("gate_overrides"), dict) else {}
    session_window = session_cfg.get("session_window") if isinstance(session_cfg.get("session_window"), dict) else {}
    params = session_window.get("params") if isinstance(session_window.get("params"), dict) else {}
    allowed = {str(item or "").strip().upper() for item in list(params.get("allowed_sessions") or [])}
    if allowed & {"PRE", "POST", "OVERNIGHT", "OFFSESSION"}:
        return "EXTENDED_EQUITY"
    return "RTH_ONLY_EQUITY"


class PlatformEODSupervisor:
    """
    Platform-level EOD flatten fallback.

    This is independent of strategy loops and data availability.  It uses broker
    and ledger truth through ManualFlattenService and submits close-only flatten
    actions through the provided order manager/execution surface.
    """

    def __init__(
        self,
        *,
        manual_flatten_service: Any,
        manual_intervention_lock_store: Any = None,
        flatten_time_et: str = "15:59:00",
        artifact_dir: str | Path = "runtime/readiness/eod",
        manual_lock_dir: str | Path = "runtime/readiness/manual_locks",
        recovery_incident_service: Any = None,
        enabled: bool = True,
        poll_interval_sec: float = 5.0,
        max_attempts_per_day: int = 12,
        verified_flat_recheck_interval_sec: float = 60.0,
        session_close_policies: Any = None,
        session_close_policy_provider: Any = None,
    ) -> None:
        self.manual_flatten_service = manual_flatten_service
        self.manual_intervention_lock_store = manual_intervention_lock_store
        self.flatten_time = _parse_hhmm(flatten_time_et, time(15, 59))
        self.artifact_dir = Path(artifact_dir)
        self.manual_lock_dir = Path(manual_lock_dir)
        self.recovery_incident_service = recovery_incident_service or RecoveryIncidentService(
            artifact_dir=self.artifact_dir.parent / "recovery_incidents"
        )
        self.enabled = bool(enabled)
        self.poll_interval_sec = max(1.0, float(poll_interval_sec))
        self.max_attempts_per_day = max(1, int(max_attempts_per_day or 12))
        self.verified_flat_recheck_interval_sec = max(1.0, float(verified_flat_recheck_interval_sec or 60.0))
        self.session_close_policies = tuple(
            item if isinstance(item, SessionClosePolicy) else SessionClosePolicy.from_mapping(item)
            for item in list(session_close_policies or ())
            if isinstance(item, (dict, SessionClosePolicy))
        )
        self.session_close_policy_provider = session_close_policy_provider
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_run_day: Optional[str] = None
        self._last_run_at_by_day: dict[str, datetime] = {}
        self._state_by_day: dict[str, str] = {}
        self._attempt_count_by_day: dict[str, int] = {}
        self._awaiting_market_reopen_not_before_by_day: dict[str, datetime] = {}

    async def start(self) -> None:
        if not self.enabled:
            logger.info("platform_eod_supervisor_disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self.run_forever(), name="platform_eod_supervisor")
        logger.info("platform_eod_supervisor_started", flatten_time_et=str(self.flatten_time))

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
                now = datetime.now(ET)
                if self._should_run(now):
                    await self.run_once(now=now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("platform_eod_supervisor_loop_error", error=str(exc), exc_info=True)
            await asyncio.sleep(self.poll_interval_sec)

    def _should_run(self, now: datetime) -> bool:
        day = now.date().isoformat()
        if now.weekday() >= 5:
            return False
        self._hydrate_day_state_from_artifact(day=day)
        state = self._state_by_day.get(day)
        if state in {"MANUAL_REQUIRED", "QUARANTINED"} or self._last_run_day == day:
            return False
        not_before = self._awaiting_market_reopen_not_before_by_day.get(day)
        if not_before is not None and now.astimezone(ET) < not_before.astimezone(ET):
            return False
        if state == "VERIFIED_FLAT":
            last_run_at = self._last_run_at_by_day.get(day)
            if last_run_at is not None:
                elapsed = (now.astimezone(ET) - last_run_at.astimezone(ET)).total_seconds()
                if elapsed < self.verified_flat_recheck_interval_sec:
                    return False
        if state == "AWAITING_MARKET_REOPEN" and not self._is_flatten_execution_window(now):
            return False
        return now.time() >= self.flatten_time

    async def run_once(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        now_et = (now or datetime.now(ET)).astimezone(ET)
        day = now_et.date().isoformat()
        trigger_id = f"platform_eod_flatten:{day}"
        started = datetime.now(ET)
        previous_attempt_count = int(self._attempt_count_by_day.get(day, 0))
        attempt_count = previous_attempt_count
        self._last_run_at_by_day[day] = now_et
        previous_state = str(self._state_by_day.get(day) or "")
        self._state_by_day[day] = "VERIFYING_FLAT" if previous_state == "VERIFIED_FLAT" else (
            "SUBMITTING" if previous_attempt_count <= 0 else "RETRYING"
        )
        result = None
        error = ""
        policy_plan = await self._session_close_policy_plan(day=day, trigger_id=trigger_id, now_et=now_et)
        try:
            result = await self._execute_policy_flatten(trigger_id=trigger_id, policy_plan=policy_plan)
        except Exception as exc:
            error = str(exc)
            logger.error(
                "platform_eod_flatten_attempt_failed",
                day=day,
                attempt_count=attempt_count,
                max_attempts_per_day=self.max_attempts_per_day,
                error=error,
                exc_info=True,
            )
        completed = datetime.now(ET)
        order_results = []
        for item in list(getattr(result, "order_results", []) or []):
            try:
                order_results.append(asdict(item))
            except Exception:
                order_results.append(dict(getattr(item, "__dict__", {}) or {}))
        verified_flat = bool(getattr(result, "verified_flat", False)) if result is not None else False
        remaining_position_targets = int(getattr(result, "remaining_position_targets", 0) or 0) if result is not None else 0
        remaining_open_orders = int(getattr(result, "remaining_open_orders", 0) or 0) if result is not None else 0
        awaiting_market_reopen = self._is_awaiting_market_reopen(order_results=order_results)
        if awaiting_market_reopen:
            not_before = self._awaiting_market_reopen_not_before(now_et=now_et, order_results=order_results)
            if not_before is not None:
                self._awaiting_market_reopen_not_before_by_day[day] = not_before
        else:
            self._awaiting_market_reopen_not_before_by_day.pop(day, None)
        spends_recovery_attempt = (not verified_flat) and (not awaiting_market_reopen)
        if spends_recovery_attempt:
            attempt_count = previous_attempt_count + 1
            self._attempt_count_by_day[day] = attempt_count
        else:
            self._attempt_count_by_day[day] = previous_attempt_count
        if verified_flat:
            state = "VERIFIED_FLAT"
        elif awaiting_market_reopen:
            state = "AWAITING_MARKET_REOPEN"
        elif attempt_count >= self.max_attempts_per_day:
            state = "MANUAL_REQUIRED"
            self._last_run_day = day
        elif result is None:
            state = "RETRYING"
        else:
            state = "SUBMITTED_NOT_VERIFIED"
        self._state_by_day[day] = state
        manual_lock_ids: list[str] = []
        manual_locks_cleared: list[dict[str, Any]] = []
        if state == "MANUAL_REQUIRED":
            manual_lock_ids = await self._create_manual_locks(
                day=day,
                trigger_id=trigger_id,
                attempt_count=attempt_count,
                reason="platform_eod_flatten_not_verified",
                remaining_position_targets=remaining_position_targets,
                remaining_open_orders=remaining_open_orders,
                error=error,
                order_results=order_results,
                policy_plan=policy_plan,
            )
            recovery_incident_ids = self._mark_manual_required_recovery_incidents(
                day=day,
                trigger_id=trigger_id,
                now_et=now_et,
                manual_lock_ids=manual_lock_ids,
                reason="platform_eod_flatten_not_verified",
                remaining_position_targets=remaining_position_targets,
                remaining_open_orders=remaining_open_orders,
                order_results=order_results,
            )
        elif verified_flat:
            manual_locks_cleared = await self._clear_verified_flat_manual_locks(day=day, trigger_id=trigger_id)
            recovery_incident_ids = self._resolve_eod_recovery_incidents(now_et=now_et)
        elif awaiting_market_reopen:
            recovery_incident_ids = self._mark_awaiting_market_reopen_recovery_incidents(
                day=day,
                trigger_id=trigger_id,
                now_et=now_et,
                not_before=self._awaiting_market_reopen_not_before_by_day.get(day),
                order_results=order_results,
            )
        else:
            recovery_incident_ids = []
        artifact = {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "go" if verified_flat else "no_go",
            "state": state,
            "attempt_count": attempt_count,
            "max_attempts_per_day": self.max_attempts_per_day,
            "trigger_id": trigger_id,
            "session_close_policy_mode": policy_plan["mode"],
            "session_close_policies": policy_plan["policies"],
            "session_close_selected_strategy_ids": policy_plan["selected_strategy_ids"],
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "flatten_time_et": str(self.flatten_time),
            "scope_mode": str(getattr(result, "scope_mode", "")) if result is not None else "",
            "verification_state": str(getattr(result, "verification_state", "")) if result is not None else "SUBMIT_EXCEPTION",
            "verified_flat": verified_flat,
            "total_orders": int(getattr(result, "total_orders", 0) or 0) if result is not None else 0,
            "success_orders": int(getattr(result, "success_orders", 0) or 0) if result is not None else 0,
            "failed_orders": int(getattr(result, "failed_orders", 0) or 0) if result is not None else 0,
            "db_targets": int(getattr(result, "db_targets", 0) or 0) if result is not None else 0,
            "broker_fallback_targets": int(getattr(result, "broker_fallback_targets", 0) or 0) if result is not None else 0,
            "remaining_position_targets": remaining_position_targets,
            "remaining_open_orders": remaining_open_orders,
            "awaiting_market_reopen": awaiting_market_reopen,
            "awaiting_market_reopen_not_before": (
                self._awaiting_market_reopen_not_before_by_day.get(day).isoformat()
                if self._awaiting_market_reopen_not_before_by_day.get(day) is not None
                else ""
            ),
            "db_position_query_ok": bool(getattr(result, "db_position_query_ok", False)) if result is not None else False,
            "broker_position_query_ok": bool(getattr(result, "broker_position_query_ok", False)) if result is not None else False,
            "truth_query_errors": list(getattr(result, "truth_query_errors", []) or []) if result is not None else [],
            "quarantine_ids": [],
            "manual_lock_ids": manual_lock_ids,
            "manual_locks_cleared": manual_locks_cleared,
            "recovery_incident_ids": recovery_incident_ids,
            "order_ids": [
                item.get("order_id")
                for item in order_results
                if isinstance(item, dict) and item.get("order_id") is not None
            ],
            "broker_order_ids": [
                item.get("order_id")
                for item in order_results
                if isinstance(item, dict) and item.get("order_id") is not None
            ],
            "error": error,
            "order_results": order_results,
        }
        self._write_artifact(day=day, artifact=artifact)
        logger.warning(
            "platform_eod_flatten_completed",
            go_no_go=artifact["go_no_go"],
            state=state,
            attempt_count=attempt_count,
            verification_state=artifact["verification_state"],
            total_orders=artifact["total_orders"],
            remaining_position_targets=artifact["remaining_position_targets"],
            remaining_open_orders=artifact["remaining_open_orders"],
        )
        return artifact

    async def _session_close_policy_plan(self, *, day: str, trigger_id: str, now_et: datetime) -> dict[str, Any]:
        policies = list(self.session_close_policies)
        provider = self.session_close_policy_provider
        if callable(provider):
            supplied = provider({"day": day, "trigger_id": trigger_id, "now_et": now_et})
            if asyncio.iscoroutine(supplied):
                supplied = await supplied
            policies = [
                item if isinstance(item, SessionClosePolicy) else SessionClosePolicy.from_mapping(item)
                for item in list(supplied or ())
            ]
        selected = [policy for policy in policies if policy.should_flatten()]
        return {
            "mode": "POLICY_SCOPED" if policies else "LEGACY_ACCOUNT_ALL",
            "policies": [policy.to_artifact() for policy in policies],
            "selected_strategy_ids": [policy.strategy_id for policy in selected],
            "selected": selected,
        }

    async def _execute_policy_flatten(self, *, trigger_id: str, policy_plan: dict[str, Any]) -> Any:
        selected = [item for item in list(policy_plan.get("selected") or ()) if isinstance(item, SessionClosePolicy)]
        if not selected:
            if str(policy_plan.get("mode") or "") == "POLICY_SCOPED":
                return self._empty_policy_flatten_result(trigger_id=trigger_id, policy_plan=policy_plan)
            return await self.manual_flatten_service.execute_flatten_all(trigger_id=trigger_id)
        if not hasattr(self.manual_flatten_service, "execute_flatten_for_strategy"):
            return await self.manual_flatten_service.execute_flatten_all(trigger_id=trigger_id)
        results = []
        for policy in selected:
            results.append(
                await self.manual_flatten_service.execute_flatten_for_strategy(
                    trigger_id=f"{trigger_id}:{policy.strategy_id}",
                    strategy_id=policy.strategy_id,
                )
            )
        return self._combine_policy_flatten_results(trigger_id=trigger_id, results=results)

    @staticmethod
    def _empty_policy_flatten_result(*, trigger_id: str, policy_plan: dict[str, Any]) -> Any:
        return type(
            "PolicyScopedEmptyFlattenResult",
            (),
            {
                "intervention_id": trigger_id,
                "trigger_id": trigger_id,
                "scope_mode": "POLICY_SCOPED",
                "verification_state": "NO_POLICY_TARGETS",
                "verified_flat": True,
                "total_orders": 0,
                "success_orders": 0,
                "failed_orders": 0,
                "db_targets": 0,
                "broker_fallback_targets": 0,
                "remaining_position_targets": 0,
                "remaining_open_orders": 0,
                "db_position_query_ok": True,
                "broker_position_query_ok": True,
                "truth_query_errors": [],
                "order_results": [],
            },
        )()

    @staticmethod
    def _combine_policy_flatten_results(*, trigger_id: str, results: list[Any]) -> Any:
        order_results: list[Any] = []
        for result in results:
            order_results.extend(list(getattr(result, "order_results", []) or []))
        remaining_position_targets = sum(int(getattr(result, "remaining_position_targets", 0) or 0) for result in results)
        remaining_open_orders = sum(int(getattr(result, "remaining_open_orders", 0) or 0) for result in results)
        failed_orders = sum(int(getattr(result, "failed_orders", 0) or 0) for result in results)
        return type(
            "PolicyScopedFlattenResult",
            (),
            {
                "intervention_id": trigger_id,
                "trigger_id": trigger_id,
                "scope_mode": "POLICY_SCOPED",
                "verification_state": "VERIFIED_FLAT" if remaining_position_targets == 0 and remaining_open_orders == 0 else "SUBMITTED_NOT_VERIFIED",
                "verified_flat": remaining_position_targets == 0 and remaining_open_orders == 0,
                "total_orders": sum(int(getattr(result, "total_orders", 0) or 0) for result in results),
                "success_orders": sum(int(getattr(result, "success_orders", 0) or 0) for result in results),
                "failed_orders": failed_orders,
                "db_targets": sum(int(getattr(result, "db_targets", 0) or 0) for result in results),
                "broker_fallback_targets": sum(int(getattr(result, "broker_fallback_targets", 0) or 0) for result in results),
                "remaining_position_targets": remaining_position_targets,
                "remaining_open_orders": remaining_open_orders,
                "db_position_query_ok": all(bool(getattr(result, "db_position_query_ok", True)) for result in results),
                "broker_position_query_ok": all(bool(getattr(result, "broker_position_query_ok", True)) for result in results),
                "truth_query_errors": [
                    item
                    for result in results
                    for item in list(getattr(result, "truth_query_errors", []) or [])
                ],
                "order_results": order_results,
            },
        )()

    def _hydrate_day_state_from_artifact(self, *, day: str) -> None:
        if day in self._state_by_day and day in self._attempt_count_by_day:
            return
        path = self.artifact_dir / f"platform_eod_flatten_{day}.json"
        if not path.exists():
            return
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        state = str((artifact or {}).get("state") or "").strip()
        if state:
            self._state_by_day.setdefault(day, state)
        try:
            self._attempt_count_by_day.setdefault(day, int((artifact or {}).get("attempt_count") or 0))
        except Exception:
            self._attempt_count_by_day.setdefault(day, 0)
        not_before_s = str((artifact or {}).get("awaiting_market_reopen_not_before") or "").strip()
        needs_rewrite = False
        if not_before_s:
            try:
                self._awaiting_market_reopen_not_before_by_day.setdefault(
                    day,
                    datetime.fromisoformat(not_before_s).astimezone(ET),
                )
            except Exception:
                pass
        elif state == "AWAITING_MARKET_REOPEN":
            try:
                completed_at = datetime.fromisoformat(str((artifact or {}).get("completed_at") or "")).astimezone(ET)
            except Exception:
                completed_at = datetime.now(ET)
            not_before = self._awaiting_market_reopen_not_before(
                now_et=completed_at,
                order_results=list((artifact or {}).get("order_results") or []),
            )
            if not_before is not None:
                self._awaiting_market_reopen_not_before_by_day[day] = not_before
                artifact["awaiting_market_reopen_not_before"] = not_before.isoformat()
                needs_rewrite = True
        if state == "AWAITING_MARKET_REOPEN" and self._is_awaiting_market_reopen(
            order_results=list((artifact or {}).get("order_results") or [])
        ):
            if int(self._attempt_count_by_day.get(day, 0) or 0) != 0:
                self._attempt_count_by_day[day] = 0
                artifact["attempt_count"] = 0
                needs_rewrite = True
            try:
                completed_at = datetime.fromisoformat(str((artifact or {}).get("completed_at") or "")).astimezone(ET)
            except Exception:
                completed_at = datetime.now(ET)
            recovery_incident_ids = self._mark_awaiting_market_reopen_recovery_incidents(
                day=day,
                trigger_id=str((artifact or {}).get("trigger_id") or f"platform_eod_flatten:{day}"),
                now_et=completed_at,
                not_before=self._awaiting_market_reopen_not_before_by_day.get(day),
                order_results=list((artifact or {}).get("order_results") or []),
            )
            existing_ids = [str(item or "") for item in list((artifact or {}).get("recovery_incident_ids") or []) if str(item or "")]
            if recovery_incident_ids and existing_ids != recovery_incident_ids:
                artifact["recovery_incident_ids"] = recovery_incident_ids
                needs_rewrite = True
        if needs_rewrite:
            try:
                path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            except Exception:
                logger.warning("platform_eod_awaiting_market_reopen_artifact_migration_failed", day=day)

    def _mark_awaiting_market_reopen_recovery_incidents(
        self,
        *,
        day: str,
        trigger_id: str,
        now_et: datetime,
        not_before: datetime | None,
        order_results: list[dict[str, Any]],
    ) -> list[str]:
        service = self.recovery_incident_service
        if service is None:
            return []
        incident_ids: list[str] = []
        seen_lanes: set[tuple[str, str]] = set()
        seen_symbols: set[str] = set()
        for item in order_results or []:
            if not isinstance(item, dict):
                continue
            strategy_id = str(item.get("strategy_id") or "").strip()
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if not strategy_id:
                strategy_id = "platform_eod_flatten"
            try:
                qty = abs(float(item.get("qty") or 0.0))
            except Exception:
                qty = 0.0
            reason = str(item.get("reason") or "manual_flatten_awaiting_market_reopen").strip()
            side = str(item.get("side") or "").strip().upper()
            lane_key = (strategy_id, symbol)
            try:
                if lane_key not in seen_lanes:
                    seen_lanes.add(lane_key)
                    incident = service.mark_awaiting_market_reopen(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        now_et=now_et,
                        qty=qty,
                        reason=f"{reason}:trigger_id={trigger_id}:day={day}",
                        not_before=not_before,
                        trigger_id=trigger_id,
                        side=side,
                    )
                    incident_id = str(incident.get("incident_id") or "")
                    if incident_id:
                        incident_ids.append(incident_id)
                if symbol not in seen_symbols:
                    seen_symbols.add(symbol)
                    for incident in service.mark_symbol_awaiting_market_reopen(
                        symbol=symbol,
                        now_et=now_et,
                        qty=qty,
                        reason=f"{reason}:trigger_id={trigger_id}:day={day}",
                        not_before=not_before,
                        trigger_id=trigger_id,
                        side=side,
                    ):
                        incident_id = str(incident.get("incident_id") or "")
                        if incident_id and incident_id not in incident_ids:
                            incident_ids.append(incident_id)
            except Exception as exc:
                logger.warning(
                    "platform_eod_recovery_incident_awaiting_market_reopen_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
        try:
            service.write_index(now_et=now_et)
        except Exception as exc:
            logger.warning("platform_eod_recovery_incident_index_failed", error=str(exc))
        return incident_ids

    def _mark_manual_required_recovery_incidents(
        self,
        *,
        day: str,
        trigger_id: str,
        now_et: datetime,
        manual_lock_ids: list[str],
        reason: str,
        remaining_position_targets: int,
        remaining_open_orders: int,
        order_results: list[dict[str, Any]],
    ) -> list[str]:
        service = self.recovery_incident_service
        if service is None:
            return []
        lanes: list[tuple[str, str, float]] = []
        seen: set[tuple[str, str]] = set()
        for item in order_results or []:
            if not isinstance(item, dict):
                continue
            strategy_id = str(item.get("strategy_id") or "").strip()
            symbol = str(item.get("symbol") or "").strip().upper()
            if not strategy_id or not symbol:
                continue
            key = (strategy_id, symbol)
            if key in seen:
                continue
            seen.add(key)
            try:
                qty = abs(float(item.get("qty") or 0.0))
            except Exception:
                qty = 0.0
            lanes.append((strategy_id, symbol, qty))
        if not lanes:
            lanes.append(("platform_eod_flatten", "ACCOUNT_ALL", 0.0))
        incident_ids: list[str] = []
        for strategy_id, symbol, qty in lanes:
            try:
                incident = service.upsert_active(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    qty=qty,
                    reason=reason,
                    now_et=now_et,
                    source="platform_eod_supervisor",
                    active_quarantine=False,
                    protected_or_flattening=False,
                )
                incident = service.mark_manual_required(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    now_et=now_et,
                    manual_lock_ids=manual_lock_ids,
                    reason=(
                        f"{reason}:trigger_id={trigger_id}:day={day}:"
                        f"remaining_position_targets={int(remaining_position_targets)}:"
                        f"remaining_open_orders={int(remaining_open_orders)}"
                    ),
                )
                incident_id = str(incident.get("incident_id") or "")
                if incident_id:
                    incident_ids.append(incident_id)
            except Exception as exc:
                logger.warning(
                    "platform_eod_recovery_incident_manual_required_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
        try:
            service.write_index(now_et=now_et)
        except Exception as exc:
            logger.warning("platform_eod_recovery_incident_index_failed", error=str(exc))
        return incident_ids

    def _resolve_eod_recovery_incidents(self, *, now_et: datetime) -> list[str]:
        service = self.recovery_incident_service
        if service is None:
            return []
        try:
            incidents = service.resolve_source_verified_flat(source="platform_eod_supervisor", now_et=now_et)
            service.write_index(now_et=now_et, resolved_verified_flat_count=len(incidents))
            return [str(item.get("incident_id") or "") for item in incidents if str(item.get("incident_id") or "")]
        except Exception as exc:
            logger.warning("platform_eod_recovery_incident_verified_flat_failed", error=str(exc))
            return []

    @staticmethod
    def _is_flatten_execution_window(now: datetime) -> bool:
        now_et = now.astimezone(ET)
        t = now_et.time()
        if now_et.weekday() >= 5:
            return False
        return time(4, 0) <= t <= time(20, 0)

    @staticmethod
    def _is_awaiting_market_reopen(*, order_results: list[dict[str, Any]]) -> bool:
        if not order_results:
            return False
        reasons = {
            str(item.get("reason") or "").strip()
            for item in order_results
            if isinstance(item, dict)
        }
        if not reasons:
            return False
        return reasons.issubset({
            "manual_flatten_awaiting_market_reopen",
            "manual_flatten_awaiting_option_market_reopen",
            "manual_flatten_market_closed",
        })

    @staticmethod
    def _awaiting_market_reopen_not_before(
        *,
        now_et: datetime,
        order_results: list[dict[str, Any]],
    ) -> Optional[datetime]:
        reasons = {
            str(item.get("reason") or "").strip()
            for item in order_results or []
            if isinstance(item, dict)
        }
        if "manual_flatten_awaiting_option_market_reopen" in reasons:
            target = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            if now_et >= target:
                target = target + timedelta(days=1)
            return target
        return None

    async def _clear_verified_flat_manual_locks(self, *, day: str, trigger_id: str) -> list[dict[str, Any]]:
        cleared: list[dict[str, Any]] = []
        for path in sorted(self.manual_lock_dir.glob(f"platform_eod_manual_lock_{day}*.json")):
            payload: dict[str, Any] = {}
            try:
                raw = path.read_text(encoding="utf-8").strip()
                payload = json.loads(raw) if raw else {}
            except Exception as exc:
                logger.warning("platform_eod_manual_lock_file_read_failed", path=str(path), error=str(exc))
                continue
            if str(payload.get("trigger_id") or "") and str(payload.get("trigger_id")) != trigger_id:
                continue
            try:
                path.unlink(missing_ok=True)
                cleared.append(
                    {
                        "source": "file",
                        "path": str(path),
                        "lock_id": str(payload.get("lock_id") or path.stem),
                        "reason": str(payload.get("reason") or ""),
                    }
                )
            except Exception as exc:
                logger.error("platform_eod_manual_lock_file_clear_failed", path=str(path), error=str(exc), exc_info=True)

        store = self.manual_intervention_lock_store
        if store is None:
            return cleared
        for lock in await self._load_platform_eod_manual_locks_from_store(day=day, trigger_id=trigger_id):
            strategy_id = str(lock.get("strategy_id") or "").strip()
            symbol = str(lock.get("symbol") or "").strip().upper()
            if not strategy_id or not symbol:
                continue
            try:
                unlocked = await store.unlock(strategy_id=strategy_id, symbol=symbol)
                if unlocked:
                    cleared.append(
                        {
                            "source": "db",
                            "strategy_id": strategy_id,
                            "symbol": symbol,
                            "reason": str(lock.get("reason") or ""),
                            "lock_id": str((lock.get("context") or {}).get("lock_id") or ""),
                        }
                    )
            except Exception as exc:
                logger.error(
                    "platform_eod_manual_lock_db_clear_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                    exc_info=True,
                )
        if cleared:
            logger.warning(
                "platform_eod_manual_locks_cleared_after_verified_flat",
                day=day,
                trigger_id=trigger_id,
                cleared_count=len(cleared),
            )
        return cleared

    async def _load_platform_eod_manual_locks_from_store(self, *, day: str, trigger_id: str) -> list[dict[str, Any]]:
        store = self.manual_intervention_lock_store
        if store is None:
            return []
        list_active = getattr(store, "list_active", None)
        if callable(list_active):
            try:
                rows = list_active()
                if asyncio.iscoroutine(rows):
                    rows = await rows
                return [
                    dict(row)
                    for row in rows or []
                    if self._is_platform_eod_lock(row, day=day, trigger_id=trigger_id)
                ]
            except Exception as exc:
                logger.warning("platform_eod_manual_lock_list_active_failed", error=str(exc), exc_info=True)
        pg = getattr(store, "pg", None)
        table_name = str(getattr(store, "table_name", "") or "")
        if pg is None or not table_name:
            return []
        try:
            rows = await pg.fetchall(
                f"""
                SELECT strategy_id, symbol, reason, context
                FROM {table_name}
                WHERE reason = 'platform_eod_manual_required'
                  AND expire_at > now()
                """
            )
        except Exception as exc:
            logger.warning("platform_eod_manual_lock_pg_query_failed", error=str(exc), exc_info=True)
            return []
        locks: list[dict[str, Any]] = []
        for row in rows or []:
            rec = dict(row)
            context = rec.get("context")
            if isinstance(context, str):
                try:
                    context = json.loads(context)
                except Exception:
                    context = {}
            rec["context"] = dict(context or {})
            if self._is_platform_eod_lock(rec, day=day, trigger_id=trigger_id):
                locks.append(rec)
        return locks

    @staticmethod
    def _is_platform_eod_lock(row: Any, *, day: str, trigger_id: str) -> bool:
        if not isinstance(row, dict):
            return False
        if str(row.get("reason") or "") != "platform_eod_manual_required":
            return False
        context = row.get("context")
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except Exception:
                context = {}
        context = dict(context or {})
        row_trigger = str(context.get("trigger_id") or "")
        if row_trigger:
            return row_trigger == trigger_id
        lock_id = str(context.get("lock_id") or "")
        return lock_id.startswith(f"platform_eod_manual_lock:{day}")

    def _write_artifact(self, *, day: str, artifact: dict[str, Any]) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"platform_eod_flatten_{day}.json"
        path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

    async def _create_manual_locks(
        self,
        *,
        day: str,
        trigger_id: str,
        attempt_count: int,
        reason: str,
        remaining_position_targets: int,
        remaining_open_orders: int,
        error: str,
        order_results: list[dict[str, Any]],
        policy_plan: dict[str, Any],
    ) -> list[str]:
        policy_scoped = str(policy_plan.get("mode") or "") == "POLICY_SCOPED"
        targets = self._manual_lock_targets(order_results=order_results, policy_plan=policy_plan)
        lock_ids: list[str] = []
        if not policy_scoped:
            lock_ids.append(
                self._write_manual_lock(
                    day=day,
                    trigger_id=trigger_id,
                    attempt_count=attempt_count,
                    reason=reason,
                    remaining_position_targets=remaining_position_targets,
                    remaining_open_orders=remaining_open_orders,
                    error=error,
                )
            )
        for target in targets:
            lock_ids.append(
                self._write_scoped_manual_lock(
                    day=day,
                    trigger_id=trigger_id,
                    attempt_count=attempt_count,
                    reason=reason,
                    remaining_position_targets=remaining_position_targets,
                    remaining_open_orders=remaining_open_orders,
                    error=error,
                    strategy_id=target["strategy_id"],
                    symbol=target["symbol"],
                    account_id=target.get("account_id", ""),
                    strategy_instance_id=target.get("strategy_instance_id", ""),
                )
            )
        store = self.manual_intervention_lock_store
        if store is None:
            return lock_ids
        seen: set[tuple[str, str]] = set()
        for target in targets:
            strategy_id = str(target.get("strategy_id") or "").strip()
            symbol = str(target.get("symbol") or "").strip().upper()
            if not strategy_id or not symbol:
                continue
            key = (strategy_id, symbol)
            if key in seen:
                continue
            seen.add(key)
            lock_id = f"platform_eod_manual_lock:{day}:{strategy_id}:{symbol}"
            try:
                await store.lock(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    ttl_sec=24 * 60 * 60,
                    reason="platform_eod_manual_required",
                    context={
                        "lock_id": lock_id,
                        "trigger_id": trigger_id,
                        "attempt_count": int(attempt_count),
                        "remaining_position_targets": int(remaining_position_targets),
                        "remaining_open_orders": int(remaining_open_orders),
                        "eod_reason": reason,
                        "error": str(error or ""),
                        "account_id": str(target.get("account_id") or ""),
                        "strategy_instance_id": str(target.get("strategy_instance_id") or strategy_id),
                        "session": str(target.get("session") or "RTH"),
                    },
                )
            except Exception as exc:
                logger.error(
                    "platform_eod_manual_lock_store_failed",
                    lock_id=lock_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                    exc_info=True,
                )
        return lock_ids

    @staticmethod
    def _manual_lock_targets(*, order_results: list[dict[str, Any]], policy_plan: dict[str, Any]) -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in order_results or []:
            if not isinstance(item, dict):
                continue
            strategy_id = str(item.get("strategy_id") or "").strip()
            symbol = str(item.get("symbol") or "").strip().upper()
            if not strategy_id or not symbol:
                continue
            key = (strategy_id, symbol)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_instance_id": str(item.get("strategy_instance_id") or strategy_id).strip(),
                    "account_id": str(item.get("account_id") or "").strip(),
                    "symbol": symbol,
                    "session": str(item.get("session") or "RTH").strip().upper() or "RTH",
                }
            )
        if targets:
            return targets
        for policy in list(policy_plan.get("selected") or ()):
            if not isinstance(policy, SessionClosePolicy):
                continue
            strategy_id = str(policy.strategy_id or "").strip()
            symbol = str(policy.symbol or "").strip().upper()
            if not strategy_id or not symbol:
                continue
            key = (strategy_id, symbol)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_instance_id": str(policy.strategy_instance_id or strategy_id).strip(),
                    "account_id": str(policy.account_id or "").strip(),
                    "symbol": symbol,
                    "session": str(policy.session or "RTH").strip().upper() or "RTH",
                }
            )
        return targets

    def _write_scoped_manual_lock(
        self,
        *,
        day: str,
        trigger_id: str,
        attempt_count: int,
        reason: str,
        remaining_position_targets: int,
        remaining_open_orders: int,
        error: str,
        strategy_id: str,
        symbol: str,
        account_id: str = "",
        strategy_instance_id: str = "",
    ) -> str:
        self.manual_lock_dir.mkdir(parents=True, exist_ok=True)
        strategy_id_s = str(strategy_id or "").strip()
        symbol_s = str(symbol or "").strip().upper()
        lock_id = f"platform_eod_manual_lock:{day}:{strategy_id_s}:{symbol_s}"
        payload = {
            "artifact_type": "platform_manual_lock",
            "lock_id": lock_id,
            "source": "platform_eod_supervisor",
            "day": day,
            "trigger_id": trigger_id,
            "state": "ACTIVE",
            "reason": reason,
            "account_id": str(account_id or ""),
            "strategy_id": strategy_id_s,
            "strategy_instance_id": str(strategy_instance_id or strategy_id_s),
            "symbol": symbol_s,
            "attempt_count": int(attempt_count),
            "remaining_position_targets": int(remaining_position_targets),
            "remaining_open_orders": int(remaining_open_orders),
            "error": str(error or ""),
            "created_at": datetime.now(ET).isoformat(),
        }
        path = self.manual_lock_dir / f"{lock_id.replace(':', '_')}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        logger.error(
            "platform_eod_scoped_manual_lock_created",
            lock_id=lock_id,
            strategy_id=strategy_id_s,
            symbol=symbol_s,
            reason=reason,
            attempt_count=attempt_count,
            remaining_position_targets=remaining_position_targets,
            remaining_open_orders=remaining_open_orders,
        )
        return lock_id

    def _write_manual_lock(
        self,
        *,
        day: str,
        trigger_id: str,
        attempt_count: int,
        reason: str,
        remaining_position_targets: int,
        remaining_open_orders: int,
        error: str,
    ) -> str:
        self.manual_lock_dir.mkdir(parents=True, exist_ok=True)
        lock_id = f"platform_eod_manual_lock:{day}"
        payload = {
            "artifact_type": "platform_manual_lock",
            "lock_id": lock_id,
            "source": "platform_eod_supervisor",
            "day": day,
            "trigger_id": trigger_id,
            "state": "ACTIVE",
            "reason": reason,
            "attempt_count": int(attempt_count),
            "remaining_position_targets": int(remaining_position_targets),
            "remaining_open_orders": int(remaining_open_orders),
            "error": str(error or ""),
            "created_at": datetime.now(ET).isoformat(),
        }
        path = self.manual_lock_dir / f"{lock_id.replace(':', '_')}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        logger.error(
            "platform_eod_manual_lock_created",
            lock_id=lock_id,
            reason=reason,
            attempt_count=attempt_count,
            remaining_position_targets=remaining_position_targets,
            remaining_open_orders=remaining_open_orders,
        )
        return lock_id
