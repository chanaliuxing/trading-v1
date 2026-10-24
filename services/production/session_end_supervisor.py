from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract
from services.production.eod_supervisor import session_close_policies_from_config
from services.production.recovery_incident_service import RecoveryIncidentService

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


def _parse_hhmm(value: Any, fallback: time) -> time:
    try:
        parts = [int(part) for part in str(value or "").strip().split(":")]
        if len(parts) == 2:
            return time(parts[0], parts[1])
        if len(parts) >= 3:
            return time(parts[0], parts[1], parts[2])
    except Exception:
        pass
    return fallback


@dataclass(frozen=True)
class SessionEndViolation:
    strategy_id: str
    symbol: str
    qty: float
    deadline_et: str
    reason: str = "session_end_no_overnight_position_not_flat"
    recovery_incident_id: str = ""
    manual_required: bool = False
    manual_lock_ids: tuple[str, ...] = ()


class SessionEndSupervisor:
    """No-overnight/session-end verification.

    This supervisor does not submit orders. Strategy/EOD/position-safety lanes
    own flatten execution. Its job is to make missed session-end flatness a
    durable, production-state-visible recovery incident instead of a stale EOD
    artifact or an unowned broker position.
    """

    def __init__(
        self,
        *,
        ib_adapter: Any,
        config: dict[str, Any],
        strategy_data_contracts: Any = (),
        manual_intervention_lock_store: Any = None,
        recovery_incident_service: Any = None,
        artifact_dir: str | Path = "runtime/readiness/session_end",
        manual_lock_dir: str | Path = "runtime/readiness/manual_locks",
        enabled: bool = True,
        poll_interval_sec: float = 30.0,
        default_deadline_et: str = "15:59:00",
        manual_required_after_sec: float = 300.0,
    ) -> None:
        self.ib_adapter = ib_adapter
        self.config = dict(config or {})
        self.strategy_data_contracts = list(strategy_data_contracts or [])
        self.manual_intervention_lock_store = manual_intervention_lock_store
        self.artifact_dir = Path(artifact_dir)
        self.manual_lock_dir = Path(manual_lock_dir)
        self.recovery_incident_service = recovery_incident_service or RecoveryIncidentService(
            artifact_dir=self.artifact_dir.parent / "recovery_incidents"
        )
        self.enabled = bool(enabled)
        self.poll_interval_sec = max(5.0, float(poll_interval_sec or 30.0))
        self.default_deadline = _parse_hhmm(default_deadline_et, time(15, 59))
        self.manual_required_after_sec = max(0.0, float(manual_required_after_sec or 0.0))
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if not self.enabled:
            logger.info("session_end_supervisor_disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self.run_forever(), name="session_end_supervisor")
        logger.info("session_end_supervisor_started", poll_interval_sec=self.poll_interval_sec)

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
                logger.error("session_end_supervisor_loop_error", error=str(exc), exc_info=True)
            await asyncio.sleep(self.poll_interval_sec)

    async def run_once(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        now_et = (now or datetime.now(ET)).astimezone(ET)
        policies = self._no_overnight_policies()
        lookup_failed = False
        error = ""
        try:
            positions = await self._load_broker_positions()
        except Exception as exc:
            positions = {}
            lookup_failed = True
            error = str(exc)
            logger.error("session_end_broker_truth_unavailable", error=error, exc_info=True)

        violations: list[SessionEndViolation] = []
        checked_count = 0
        for policy in policies:
            if not self._deadline_due(now_et=now_et, deadline=policy["deadline"]):
                continue
            checked_count += 1
            for symbol in policy["symbols"]:
                qty = float(positions.get(symbol, 0.0) or 0.0)
                if abs(qty) <= 1e-6:
                    continue
                incident_result = await self._upsert_or_escalate_incident(
                    strategy_id=policy["strategy_id"],
                    symbol=symbol,
                    qty=qty,
                    now_et=now_et,
                )
                violations.append(
                    SessionEndViolation(
                        strategy_id=policy["strategy_id"],
                        symbol=symbol,
                        qty=qty,
                        deadline_et=policy["deadline"].isoformat(),
                        recovery_incident_id=str(incident_result.get("incident_id") or ""),
                        manual_required=bool(incident_result.get("manual_required")),
                        manual_lock_ids=tuple(incident_result.get("manual_lock_ids") or ()),
                    )
                )

        if not lookup_failed and not violations:
            self._resolve_incidents(now_et=now_et)

        artifact = {
            "artifact_type": "session_end_no_overnight",
            "generated_at": now_et.isoformat(),
            "go_no_go": "no_go" if lookup_failed or violations else "go",
            "lookup_failed": lookup_failed,
            "error": error,
            "policy_count": len(policies),
            "checked_count": checked_count,
            "violation_count": len(violations),
            "manual_required_count": sum(1 for item in violations if item.manual_required),
            "violations": [asdict(item) for item in violations],
            "source": "session_end_supervisor",
        }
        self._write_artifact(artifact)
        return artifact

    def _no_overnight_policies(self) -> list[dict[str, Any]]:
        symbols_by_strategy: dict[str, list[str]] = {}
        for contract in self.strategy_data_contracts:
            strategy_id = str(getattr(contract, "strategy_id", "") or "").strip()
            if not strategy_id or not bool(getattr(contract, "production_blocking", True)):
                continue
            symbols_by_strategy.setdefault(strategy_id, [])
            for symbol in getattr(contract, "symbols", ()) or ():
                symbol_u = str(symbol or "").strip().upper()
                if symbol_u and symbol_u not in symbols_by_strategy[strategy_id]:
                    symbols_by_strategy[strategy_id].append(symbol_u)

        policies: list[dict[str, Any]] = []
        session_close_policies = {
            policy.strategy_id: policy
            for policy in session_close_policies_from_config(self.config)
            if policy.strategy_id
        }
        strategies = self.config.get("strategies") if isinstance(self.config, dict) else []
        if not isinstance(strategies, list):
            return policies
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
            if not bool(eod.get("no_overnight")):
                continue
            session_policy = session_close_policies.get(strategy_id)
            if session_policy is not None and not session_policy.should_flatten():
                continue
            policy_symbol = str(getattr(session_policy, "symbol", "") or "").strip().upper()
            symbols = [policy_symbol] if policy_symbol else symbols_by_strategy.get(strategy_id, [])
            if not symbols:
                continue
            policies.append(
                {
                    "strategy_id": strategy_id,
                    "symbols": symbols,
                    "deadline": self._strategy_deadline(params=params, extras=extras),
                    "policy_type": str(getattr(session_policy, "policy_type", "") or "RTH_ONLY_EQUITY"),
                }
            )
        return policies

    def _strategy_deadline(self, *, params: dict[str, Any], extras: dict[str, Any]) -> time:
        for key in (
            "force_flatten_time",
            "final_flatten",
            "eod_flatten_time",
            "eod_close_time",
            "session_end_flatten_time",
        ):
            if key in extras:
                return _parse_hhmm(extras.get(key), self.default_deadline)
            if key in params:
                return _parse_hhmm(params.get(key), self.default_deadline)
        return self.default_deadline

    @staticmethod
    def _deadline_due(*, now_et: datetime, deadline: time) -> bool:
        return now_et.timetz().replace(tzinfo=None) >= deadline

    async def _load_broker_positions(self) -> dict[str, float]:
        positions_fn = getattr(self.ib_adapter, "positions", None)
        raw_positions = None
        if callable(positions_fn):
            raw_positions = positions_fn()
            if hasattr(raw_positions, "__await__"):
                raw_positions = await raw_positions
        elif getattr(self.ib_adapter, "ib", None) is not None and hasattr(self.ib_adapter.ib, "positions"):
            raw_positions = self.ib_adapter.ib.positions()
        else:
            raise RuntimeError("broker_position_adapter_unavailable")
        account = str(getattr(self.ib_adapter, "account", "") or "").strip()
        out: dict[str, float] = {}
        for item in raw_positions or []:
            try:
                if account and getattr(item, "account", "") and str(getattr(item, "account", "")) != account:
                    continue
                symbol = self._position_symbol(item)
                qty = float(getattr(item, "position", 0.0) or 0.0)
            except Exception:
                continue
            if symbol and abs(qty) > 1e-6:
                out[symbol] = out.get(symbol, 0.0) + qty
        return out

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

    async def _upsert_or_escalate_incident(
        self,
        *,
        strategy_id: str,
        symbol: str,
        qty: float,
        now_et: datetime,
    ) -> dict[str, Any]:
        service = self.recovery_incident_service
        if service is None:
            return {"incident_id": "", "manual_required": False, "manual_lock_ids": []}
        existing = service.get_incident(strategy_id=strategy_id, symbol=symbol)
        if str(existing.get("state") or "") == "manual_required_acknowledged":
            return {
                "incident_id": str(existing.get("incident_id") or ""),
                "manual_required": True,
                "manual_lock_ids": list(existing.get("manual_lock_ids") or []),
            }

        incident = service.upsert_active(
            strategy_id=strategy_id,
            symbol=symbol,
            qty=abs(float(qty or 0.0)),
            reason="session_end_no_overnight_position_not_flat",
            now_et=now_et,
            source="session_end_supervisor",
            active_quarantine=False,
            protected_or_flattening=False,
        )
        first_seen = _parse_iso_dt(incident.get("first_seen"))
        age_sec = (now_et - first_seen.astimezone(ET)).total_seconds() if first_seen is not None else 0.0
        if age_sec < self.manual_required_after_sec:
            service.write_index(now_et=now_et)
            return {
                "incident_id": str(incident.get("incident_id") or ""),
                "manual_required": False,
                "manual_lock_ids": [],
            }

        lock_ids = await self._create_manual_required_locks(
            strategy_id=strategy_id,
            symbol=symbol,
            qty=qty,
            now_et=now_et,
        )
        terminal = service.mark_manual_required(
            strategy_id=strategy_id,
            symbol=symbol,
            now_et=now_et,
            manual_lock_ids=lock_ids,
            reason="session_end_no_overnight_position_still_not_flat",
        )
        service.write_index(now_et=now_et)
        return {
            "incident_id": str(terminal.get("incident_id") or incident.get("incident_id") or ""),
            "manual_required": True,
            "manual_lock_ids": lock_ids,
        }

    async def _create_manual_required_locks(
        self,
        *,
        strategy_id: str,
        symbol: str,
        qty: float,
        now_et: datetime,
    ) -> list[str]:
        lock_id = f"session_end_manual_lock:{now_et.date().isoformat()}:{strategy_id}:{symbol}"
        payload = {
            "lock_id": lock_id,
            "source": "session_end_supervisor",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "qty": float(qty or 0.0),
            "reason": "session_end_manual_required",
            "created_at": now_et.isoformat(),
            "state": "active",
        }
        self._write_manual_lock(payload)
        store = self.manual_intervention_lock_store
        if store is not None:
            try:
                await store.lock(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    ttl_sec=24 * 60 * 60,
                    reason="session_end_manual_required",
                    context=payload,
                )
            except Exception as exc:
                logger.warning(
                    "session_end_manual_lock_store_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
        return [lock_id]

    def _resolve_incidents(self, *, now_et: datetime) -> None:
        service = self.recovery_incident_service
        if service is None:
            return
        resolved = service.resolve_source_verified_flat(source="session_end_supervisor", now_et=now_et)
        service.write_index(now_et=now_et, resolved_verified_flat_count=len(resolved))

    def _write_artifact(self, artifact: dict[str, Any]) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / "session_end_latest.json"
        path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

    def _write_manual_lock(self, payload: dict[str, Any]) -> None:
        self.manual_lock_dir.mkdir(parents=True, exist_ok=True)
        lock_id = str(payload.get("lock_id") or "session_end_manual_lock").replace(":", "_")
        path = self.manual_lock_dir / f"{lock_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def _parse_iso_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed
