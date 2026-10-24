from __future__ import annotations

import json
import os
import sys
import time
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_state_path() -> Path:
    raw = os.getenv("IB_GATEWAY_CIRCUIT_STATE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / "runtime" / "state" / "ib_gateway_connection_circuit.json"


def _utc_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(float(ts if ts is not None else time.time()), timezone.utc).isoformat()


@dataclass(frozen=True)
class IBGatewayCircuitState:
    enabled: bool
    state: str
    open: bool
    manual_required: bool
    reason: str
    source: str
    consecutive_failures: int
    opened_at: Optional[str]
    open_until: Optional[str]
    required_action: str
    last_proof_status: str
    last_proof_reason: str
    remaining_sec: float
    retry_after_sec: float
    path: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "state": self.state,
            "open": bool(self.open),
            "manual_required": bool(self.manual_required),
            "reason": self.reason,
            "source": self.source,
            "consecutive_failures": int(self.consecutive_failures),
            "opened_at": self.opened_at,
            "open_until": self.open_until,
            "required_action": self.required_action,
            "last_proof_status": self.last_proof_status,
            "last_proof_reason": self.last_proof_reason,
            "remaining_sec": round(float(self.remaining_sec), 3),
            "retry_after_sec": round(float(self.retry_after_sec), 3),
            "path": self.path,
        }


class IBGatewayConnectionCircuit:
    """Persistent IB Gateway connection circuit breaker.

    This is deliberately process-shared. IB Gateway can enter a half-open state
    during daily restarts where the TCP port accepts or refuses connections but
    API login/handshake is not healthy. A per-process backoff is insufficient
    when launchd or multiple runtime components retry independently.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _default_state_path()
        explicit_state_path = bool(os.getenv("IB_GATEWAY_CIRCUIT_STATE_PATH", "").strip())
        running_under_pytest = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
        default_enabled = "false" if (running_under_pytest and not explicit_state_path) else "true"
        self.enabled = os.getenv("IB_GATEWAY_CIRCUIT_ENABLED", default_enabled).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.base_cooldown_sec = max(1.0, float(os.getenv("IB_GATEWAY_CIRCUIT_BASE_COOLDOWN_SEC", "30")))
        self.max_cooldown_sec = max(self.base_cooldown_sec, float(os.getenv("IB_GATEWAY_CIRCUIT_MAX_COOLDOWN_SEC", "300")))
        self.failure_window_sec = max(1.0, float(os.getenv("IB_GATEWAY_CIRCUIT_FAILURE_WINDOW_SEC", "1800")))

    @staticmethod
    def is_gateway_failure(reason: str) -> bool:
        msg = str(reason or "").lower()
        tokens = (
            "gateway_api_half_open",
            "ib_api_handshake_not_ready",
            "apistart",
            "timeouterror",
            "timed out",
            "timeout",
            "connectionrefusederror",
            "connection refused",
            "connect call failed",
            "errno 61",
            "peer closed connection",
            "connection reset by peer",
            "api connection failed",
            "client id is already in use",
            "already in use",
            "1100",
            "1300",
        )
        return any(tok in msg for tok in tokens)

    @staticmethod
    def is_port_refused_failure(reason: str) -> bool:
        msg = str(reason or "").lower()
        tokens = (
            "connectionrefusederror",
            "connection refused",
            "connect call failed",
            "errno 61",
        )
        return any(tok in msg for tok in tokens)

    @classmethod
    def _manual_required_allows_recovery_proof(cls, raw: Dict[str, Any]) -> bool:
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        haystack = " ".join(
            str(item or "")
            for item in (
                raw.get("reason"),
                raw.get("source"),
                metadata.get("error"),
                metadata.get("detail"),
                metadata.get("gateway_reason"),
            )
        )
        return cls.is_port_refused_failure(haystack)

    def _read_raw(self) -> Dict[str, Any]:
        try:
            if not self.path.exists():
                return {}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_raw(self, payload: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @contextmanager
    def _locked_raw(self) -> Iterator[Dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield self._read_raw()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def snapshot(self, *, now_ts: Optional[float] = None) -> IBGatewayCircuitState:
        now = float(now_ts if now_ts is not None else time.time())
        raw = self._read_raw()
        open_until_ts = float(raw.get("open_until_ts") or 0.0)
        manual_required = bool(raw.get("manual_required"))
        open_now = bool(self.enabled and (manual_required or open_until_ts > now))
        remaining = max(0.0, open_until_ts - now) if open_now and not manual_required else 0.0
        return IBGatewayCircuitState(
            enabled=bool(self.enabled),
            state=str(raw.get("state") or ("manual_required" if manual_required else ("open" if open_now else "closed"))),
            open=open_now,
            manual_required=manual_required,
            reason=str(raw.get("reason") or ""),
            source=str(raw.get("source") or ""),
            consecutive_failures=int(raw.get("consecutive_failures") or 0),
            opened_at=raw.get("opened_at"),
            open_until=raw.get("open_until"),
            required_action=(
                "operator_ack_then_single_lease_gateway_recovery_proof" if manual_required else ""
            ),
            last_proof_status=str(raw.get("last_proof_status") or ""),
            last_proof_reason=str(raw.get("last_proof_reason") or ""),
            remaining_sec=remaining,
            retry_after_sec=remaining,
            path=str(self.path),
        )

    def declare_planned_restart(
        self,
        *,
        source: str,
        grace_sec: float,
        now_ts: Optional[float] = None,
        reason: str = "planned_restart_window",
    ) -> IBGatewayCircuitState:
        now = float(now_ts if now_ts is not None else time.time())
        grace = max(1.0, float(grace_sec or 0.0))
        if not self.enabled:
            return self.snapshot(now_ts=now)
        raw = self._read_raw()
        until_ts = now + grace
        payload = {
            **raw,
            "state": "recoverable_restarting",
            "manual_required": False,
            "reason": str(reason or "planned_restart_window")[:240],
            "source": str(source or "unknown")[:160],
            "planned_restart_active": True,
            "planned_restart_until_ts": until_ts,
            "planned_restart_until": _utc_iso(until_ts),
            "planned_restart_source": str(source or "unknown")[:160],
            "open_until_ts": until_ts,
            "open_until": _utc_iso(until_ts),
            "opened_at_ts": float(raw.get("opened_at_ts") or now),
            "opened_at": raw.get("opened_at") or _utc_iso(now),
            "updated_at_ts": now,
            "updated_at": _utc_iso(now),
        }
        self._write_raw(payload)
        return self.snapshot(now_ts=now)

    def check_allowed(self, *, now_ts: Optional[float] = None) -> IBGatewayCircuitState:
        return self.snapshot(now_ts=now_ts)

    def record_success(self, *, source: str = "unknown") -> IBGatewayCircuitState:
        now = time.time()
        if not self.enabled:
            return self.snapshot(now_ts=now)
        raw = self._read_raw()
        payload = {
            **raw,
            "state": "closed",
            "manual_required": False,
            "open_until_ts": 0.0,
            "open_until": None,
            "closed_at_ts": now,
            "closed_at": _utc_iso(now),
            "closed_by": str(source or "unknown")[:160],
            "consecutive_failures": 0,
            "recovery_probe_lease_until_ts": 0.0,
            "recovery_probe_owner": "",
            "updated_at_ts": now,
            "updated_at": _utc_iso(now),
        }
        self._write_raw(payload)
        return self.snapshot(now_ts=now)

    def record_recovery_proof_success(
        self,
        *,
        source: str,
        reason: str = "authorized_gateway_recovery_proof_succeeded",
        now_ts: Optional[float] = None,
    ) -> IBGatewayCircuitState:
        now = float(now_ts if now_ts is not None else time.time())
        if not self.enabled:
            return self.snapshot(now_ts=now)
        raw = self._read_raw()
        payload = {
            **raw,
            "state": "closed",
            "manual_required": False,
            "reason": str(reason or "authorized_gateway_recovery_proof_succeeded")[:240],
            "source": str(source or "unknown")[:160],
            "open_until_ts": 0.0,
            "open_until": None,
            "closed_at_ts": now,
            "closed_at": _utc_iso(now),
            "closed_by": str(source or "unknown")[:160],
            "consecutive_failures": 0,
            "last_proof_status": "PROOF_SUCCEEDED",
            "last_proof_reason": str(reason or "authorized_gateway_recovery_proof_succeeded")[:240],
            "last_proof_source": str(source or "unknown")[:160],
            "recovery_probe_lease_until_ts": 0.0,
            "recovery_probe_owner": "",
            "updated_at_ts": now,
            "updated_at": _utc_iso(now),
        }
        self._write_raw(payload)
        return self.snapshot(now_ts=now)

    def recent_recovery_proof_succeeded(
        self,
        *,
        max_age_sec: float,
        now_ts: Optional[float] = None,
    ) -> bool:
        now = float(now_ts if now_ts is not None else time.time())
        raw = self._read_raw()
        if bool(raw.get("manual_required")):
            return False
        if str(raw.get("state") or "") != "closed":
            return False
        if str(raw.get("last_proof_status") or "") != "PROOF_SUCCEEDED":
            return False
        closed_at_ts = float(raw.get("closed_at_ts") or 0.0)
        if closed_at_ts <= 0.0:
            return False
        return (now - closed_at_ts) <= max(1.0, float(max_age_sec))

    def mark_manual_required(
        self,
        *,
        reason: str,
        source: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IBGatewayCircuitState:
        now = time.time()
        if not self.enabled:
            return self.snapshot(now_ts=now)
        raw = self._read_raw()
        payload = {
            **raw,
            "state": "manual_required",
            "manual_required": True,
            "reason": str(reason or "manual_gateway_api_recovery_required")[:240],
            "source": str(source or "unknown")[:160],
            "opened_at_ts": float(raw.get("opened_at_ts") or now),
            "opened_at": raw.get("opened_at") or _utc_iso(now),
            "open_until_ts": 0.0,
            "open_until": None,
            "recovery_probe_lease_until_ts": 0.0,
            "recovery_probe_owner": "",
            "updated_at_ts": now,
            "updated_at": _utc_iso(now),
            "metadata": metadata or {},
        }
        self._write_raw(payload)
        return self.snapshot(now_ts=now)

    def try_acquire_recovery_probe_lease(
        self,
        *,
        source: str,
        min_interval_sec: float,
        lease_sec: float,
        allow_manual_required: bool = False,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Allow one process-shared recovery probe while real connects stay blocked."""
        now = float(now_ts if now_ts is not None else time.time())
        min_interval = max(1.0, float(min_interval_sec))
        lease = max(1.0, float(lease_sec))
        if not self.enabled:
            return {"allowed": True, "reason": "circuit_disabled", "retry_after_sec": 0.0}
        with self._locked_raw() as raw:
            if bool(raw.get("manual_required")):
                if not allow_manual_required or not self._manual_required_allows_recovery_proof(raw):
                    return {
                        "allowed": False,
                        "reason": "manual_required",
                        "retry_after_sec": 0.0,
                        "manual_required": True,
                    }
            open_until_ts = float(raw.get("open_until_ts") or 0.0)
            manual_required = bool(raw.get("manual_required"))
            if open_until_ts <= now and not manual_required:
                return {"allowed": True, "reason": "circuit_closed_or_expired", "retry_after_sec": 0.0}
            lease_until = float(raw.get("recovery_probe_lease_until_ts") or 0.0)
            if lease_until > now:
                return {
                    "allowed": False,
                    "reason": "recovery_probe_lease_active",
                    "retry_after_sec": max(0.0, lease_until - now),
                    "owner": str(raw.get("recovery_probe_owner") or ""),
                }
            last_attempt = float(raw.get("last_recovery_probe_attempt_ts") or 0.0)
            elapsed = now - last_attempt if last_attempt > 0 else min_interval
            if elapsed < min_interval:
                return {
                    "allowed": False,
                    "reason": "recovery_probe_cooldown",
                    "retry_after_sec": max(0.0, min_interval - elapsed),
                }
            payload = {
                **raw,
                "last_recovery_probe_attempt_ts": now,
                "last_recovery_probe_attempt_at": _utc_iso(now),
                "recovery_probe_lease_until_ts": now + lease,
                "recovery_probe_owner": str(source or "unknown")[:160],
                "updated_at_ts": now,
                "updated_at": _utc_iso(now),
            }
            self._write_raw(payload)
            reason = (
                "manual_refusal_recovery_probe_lease_acquired"
                if manual_required
                else "recovery_probe_lease_acquired"
            )
            return {
                "allowed": True,
                "reason": reason,
                "retry_after_sec": 0.0,
                "manual_required": manual_required,
            }

    def try_acquire_manual_recovery_proof_lease(
        self,
        *,
        source: str,
        lease_sec: float,
        allow_when_not_manual_required: bool = False,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        lease = max(1.0, float(lease_sec))
        if not self.enabled:
            return {"allowed": True, "reason": "circuit_disabled", "retry_after_sec": 0.0}
        with self._locked_raw() as raw:
            if not bool(raw.get("manual_required")):
                if not allow_when_not_manual_required:
                    return {"allowed": False, "reason": "manual_required_not_active", "retry_after_sec": 0.0}
            owner = str(raw.get("recovery_probe_owner") or "")
            if (
                owner
                and owner != str(source or "unknown")[:160]
                and str(raw.get("last_proof_status") or "") == "PROOF_IN_PROGRESS"
            ):
                return {
                    "allowed": False,
                    "reason": "recovery_probe_lease_active",
                    "retry_after_sec": 0.0,
                    "owner": owner,
                    "manual_required": True,
                }
            lease_until = float(raw.get("recovery_probe_lease_until_ts") or 0.0)
            if lease_until > now:
                return {
                    "allowed": False,
                    "reason": "recovery_probe_lease_active",
                    "retry_after_sec": max(0.0, lease_until - now),
                    "owner": str(raw.get("recovery_probe_owner") or ""),
                    "manual_required": True,
                }
            payload = {
                **raw,
                "state": "proof_in_progress" if bool(raw.get("manual_required")) else str(raw.get("state") or "closed"),
                "last_recovery_probe_attempt_ts": now,
                "last_recovery_probe_attempt_at": _utc_iso(now),
                "recovery_probe_lease_until_ts": now + lease,
                "recovery_probe_owner": str(source or "unknown")[:160],
                "last_proof_status": "PROOF_IN_PROGRESS",
                "last_proof_source": str(source or "unknown")[:160],
                "updated_at_ts": now,
                "updated_at": _utc_iso(now),
            }
            self._write_raw(payload)
            return {
                "allowed": True,
                "reason": "manual_recovery_proof_lease_acquired",
                "retry_after_sec": 0.0,
                "manual_required": True,
            }

    def record_recovery_proof_failure(
        self,
        *,
        reason: str,
        source: str,
        now_ts: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IBGatewayCircuitState:
        now = float(now_ts if now_ts is not None else time.time())
        if not self.enabled:
            return self.snapshot(now_ts=now)
        raw = self._read_raw()
        payload = {
            **raw,
            "state": "proof_failed" if bool(raw.get("manual_required")) else str(raw.get("state") or "open"),
            "manual_required": bool(raw.get("manual_required")),
            "last_proof_status": "PROOF_FAILED",
            "last_proof_reason": str(reason or "proof_failed")[:240],
            "last_proof_source": str(source or "unknown")[:160],
            "recovery_probe_lease_until_ts": 0.0,
            "recovery_probe_owner": "",
            "updated_at_ts": now,
            "updated_at": _utc_iso(now),
            "metadata": {**dict(raw.get("metadata") or {}), **dict(metadata or {})},
        }
        self._write_raw(payload)
        return self.snapshot(now_ts=now)

    def record_failure(
        self,
        *,
        reason: str,
        source: str,
        now_ts: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IBGatewayCircuitState:
        now = float(now_ts if now_ts is not None else time.time())
        if not self.enabled:
            return self.snapshot(now_ts=now)
        raw = self._read_raw()
        preserve_manual_required = bool(raw.get("manual_required"))
        last_failure_ts = float(raw.get("last_failure_ts") or 0.0)
        previous_failures = int(raw.get("consecutive_failures") or 0)
        if last_failure_ts <= 0.0 or (now - last_failure_ts) > self.failure_window_sec:
            failures = 1
        else:
            failures = previous_failures + 1
        cooldown = min(self.max_cooldown_sec, self.base_cooldown_sec * (2 ** max(0, failures - 1)))
        open_until_ts = now + cooldown
        planned_until_ts = float(raw.get("planned_restart_until_ts") or 0.0)
        port_refused = self.is_port_refused_failure(reason)
        within_planned_restart = bool(port_refused and planned_until_ts > now)
        state = "manual_required" if preserve_manual_required else "open"
        if not preserve_manual_required and within_planned_restart:
            state = "recoverable_restarting"
        elif not preserve_manual_required and port_refused:
            state = "recoverable_refused"
        elif not preserve_manual_required and self.is_gateway_failure(reason):
            state = "recoverable_cooldown"
        payload = {
            **raw,
            "state": state,
            "manual_required": preserve_manual_required,
            "reason": ("gateway_restarting" if within_planned_restart else str(reason or "ib_gateway_connection_failure"))[:240],
            "source": str(source or "unknown")[:160],
            "consecutive_failures": int(failures),
            "last_failure_ts": now,
            "last_failure_at": _utc_iso(now),
            "opened_at_ts": float(raw.get("opened_at_ts") or now),
            "opened_at": raw.get("opened_at") or _utc_iso(now),
            "open_until_ts": 0.0 if preserve_manual_required else open_until_ts,
            "open_until": None if preserve_manual_required else _utc_iso(open_until_ts),
            "cooldown_sec": float(cooldown),
            "updated_at_ts": now,
            "updated_at": _utc_iso(now),
            "planned_restart_active": bool(raw.get("planned_restart_active") and planned_until_ts > now),
            "planned_restart_until_ts": planned_until_ts if planned_until_ts > now else float(raw.get("planned_restart_until_ts") or 0.0),
            "planned_restart_until": raw.get("planned_restart_until"),
            "planned_restart_source": raw.get("planned_restart_source"),
            "metadata": {
                **dict(raw.get("metadata") or {}),
                **dict(metadata or {}),
                "gateway_recovery_state": state,
                "restart_intent_source": raw.get("planned_restart_source") if within_planned_restart else None,
            },
        }
        self._write_raw(payload)
        return self.snapshot(now_ts=now)


_CIRCUIT: Optional[IBGatewayConnectionCircuit] = None


def get_ib_gateway_connection_circuit() -> IBGatewayConnectionCircuit:
    global _CIRCUIT
    if _CIRCUIT is None:
        _CIRCUIT = IBGatewayConnectionCircuit()
    return _CIRCUIT
