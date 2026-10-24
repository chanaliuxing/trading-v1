from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from libs.logger import get_logger
from services.ibkr.ib_gateway_circuit import get_ib_gateway_connection_circuit

logger = get_logger(__name__)


@dataclass(frozen=True)
class IBGatewayRecoveryDecision:
    allowed: bool
    reason: str
    circuit_open: bool
    manual_required: bool
    source: str
    retry_after_sec: float
    state: Dict[str, Any]


@dataclass(frozen=True)
class RecoveryProofResult:
    status: str
    source: str
    reason: str
    circuit_state: Dict[str, Any]
    current_time_ok: bool = False
    positions_ok: bool = False
    open_orders_ok: bool = False
    lease_owner: str = ""
    ib_constructed: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "PROOF_SUCCEEDED"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "reason": self.reason,
            "circuit_state": self.circuit_state,
            "current_time_ok": bool(self.current_time_ok),
            "positions_ok": bool(self.positions_ok),
            "open_orders_ok": bool(self.open_orders_ok),
            "lease_owner": self.lease_owner,
            "ib_constructed": bool(self.ib_constructed),
        }


class IBGatewayRecoveryCoordinator:
    """Single policy boundary for real IB Gateway API recovery probes.

    The coordinator is deliberately small: it does not own long-lived market,
    exec, or master sessions. Its job is to centralize the decision of whether
    a disposable API login/probe is allowed and to make all probe outcomes update
    the same durable Gateway circuit.
    """

    def __init__(self) -> None:
        raw_sources = os.getenv(
            "IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES",
            "market,exec,master,"
            "market:pre_connect_api_probe,exec:pre_connect_api_probe,master:pre_connect_api_probe,"
            "manager_runtime_recovery:circuit_recovery_probe,run_system.start:circuit_recovery_probe",
        )
        self._authorized_probe_sources = {
            item.strip()
            for item in str(raw_sources or "").split(",")
            if item.strip()
        }
        self._probe_while_open_interval_sec = float(
            os.getenv("IB_GATEWAY_RECOVERY_PROBE_WHILE_OPEN_SEC", "30")
        )
        self._probe_while_open_lease_sec = float(
            os.getenv("IB_GATEWAY_RECOVERY_PROBE_LEASE_SEC", "20")
        )
        self._allow_manual_refusal_recovery_probe = (
            os.getenv("IB_GATEWAY_RECOVERY_ALLOW_MANUAL_REFUSAL_PROOF", "true").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        self._proof_lease_sec = float(os.getenv("IB_GATEWAY_RECOVERY_PROOF_LEASE_SEC", "30"))

    def probe_decision(self, *, caller: str, host: str, port: int) -> IBGatewayRecoveryDecision:
        state = get_ib_gateway_connection_circuit().check_allowed()
        state_dict = state.as_dict()
        caller_v = str(caller or "").strip()
        if self._authorized_probe_sources and caller_v not in self._authorized_probe_sources:
            logger.warning(
                "ib_gateway_recovery_probe_blocked_unauthorized_caller",
                caller=caller_v,
                host=host,
                port=int(port),
                authorized_sources=sorted(self._authorized_probe_sources),
            )
            return IBGatewayRecoveryDecision(
                allowed=False,
                reason="unauthorized_recovery_probe_caller",
                circuit_open=bool(state.open),
                manual_required=bool(state.manual_required),
                source=str(state.source or ""),
                retry_after_sec=float(state.retry_after_sec),
                state=state_dict,
            )

        if state.open:
            if not state.manual_required:
                lease = get_ib_gateway_connection_circuit().try_acquire_recovery_probe_lease(
                    source=caller_v,
                    min_interval_sec=self._probe_while_open_interval_sec,
                    lease_sec=self._probe_while_open_lease_sec,
                )
                lease_allowed = bool(lease.get("allowed"))
                event = (
                    "ib_gateway_recovery_probe_allowed_circuit_open"
                    if lease_allowed
                    else "ib_gateway_recovery_probe_blocked_circuit_open"
                )
                logger.warning(
                    event,
                    caller=caller_v,
                    host=host,
                    port=int(port),
                    reason=str(lease.get("reason") or state.reason),
                    circuit_reason=state.reason,
                    source=state.source,
                    retry_after_sec=round(float(lease.get("retry_after_sec") or state.retry_after_sec), 2),
                    owner=str(lease.get("owner") or ""),
                )
                return IBGatewayRecoveryDecision(
                    allowed=lease_allowed,
                    reason=str(lease.get("reason") or "gateway_circuit_open"),
                    circuit_open=True,
                    manual_required=False,
                    source=str(state.source or ""),
                    retry_after_sec=float(lease.get("retry_after_sec") or 0.0),
                    state=state_dict,
                )

            lease = get_ib_gateway_connection_circuit().try_acquire_recovery_probe_lease(
                source=caller_v,
                min_interval_sec=self._probe_while_open_interval_sec,
                lease_sec=self._probe_while_open_lease_sec,
                allow_manual_required=self._allow_manual_refusal_recovery_probe,
            )
            lease_allowed = bool(lease.get("allowed"))
            event = (
                "ib_gateway_recovery_probe_allowed_manual_required"
                if lease_allowed
                else "ib_gateway_recovery_probe_blocked_circuit_open"
            )
            logger.warning(
                event,
                caller=caller_v,
                host=host,
                port=int(port),
                reason=str(lease.get("reason") or state.reason),
                circuit_reason=state.reason,
                source=state.source,
                manual_required=bool(state.manual_required),
                retry_after_sec=round(float(lease.get("retry_after_sec") or state.retry_after_sec), 2),
                owner=str(lease.get("owner") or ""),
            )
            return IBGatewayRecoveryDecision(
                allowed=lease_allowed,
                reason=str(lease.get("reason") or "gateway_circuit_open"),
                circuit_open=True,
                manual_required=bool(state.manual_required),
                source=str(state.source or ""),
                retry_after_sec=float(lease.get("retry_after_sec") or state.retry_after_sec),
                state=state_dict,
            )

        return IBGatewayRecoveryDecision(
            allowed=True,
            reason="allowed",
            circuit_open=False,
            manual_required=False,
            source=str(state.source or ""),
            retry_after_sec=0.0,
            state=state_dict,
        )

    def record_probe_success(self, *, source: str) -> Dict[str, Any]:
        return get_ib_gateway_connection_circuit().record_success(source=source).as_dict()

    def record_probe_failure(
        self,
        *,
        reason: str,
        source: str,
        host: str,
        port: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {"host": host, "port": int(port), **dict(metadata or {})}
        return get_ib_gateway_connection_circuit().record_failure(
            reason=reason,
            source=source,
            metadata=payload,
        ).as_dict()

    def mark_manual_required(
        self,
        *,
        reason: str,
        source: str,
        host: str,
        port: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {"host": host, "port": int(port), **dict(metadata or {})}
        return get_ib_gateway_connection_circuit().mark_manual_required(
            reason=reason,
            source=source,
            metadata=payload,
        ).as_dict()

    def _write_proof_artifact(self, result: RecoveryProofResult) -> None:
        try:
            circuit_path = Path(get_ib_gateway_connection_circuit().path)
            path = circuit_path.parent / "ib_gateway_recovery_proof_latest.json"
            payload = {
                **result.as_dict(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("ib_gateway_recovery_proof_artifact_write_failed", error=str(exc))

    def _proof_result(
        self,
        *,
        status: str,
        source: str,
        reason: str,
        current_time_ok: bool = False,
        positions_ok: bool = False,
        open_orders_ok: bool = False,
        lease_owner: str = "",
        ib_constructed: bool = False,
    ) -> RecoveryProofResult:
        result = RecoveryProofResult(
            status=status,
            source=source,
            reason=reason,
            circuit_state=get_ib_gateway_connection_circuit().snapshot().as_dict(),
            current_time_ok=current_time_ok,
            positions_ok=positions_ok,
            open_orders_ok=open_orders_ok,
            lease_owner=lease_owner,
            ib_constructed=ib_constructed,
        )
        self._write_proof_artifact(result)
        return result

    async def run_authorized_gateway_recovery_proof(
        self,
        *,
        source: str,
        ack_token: str | None,
        now: datetime,
        host: str,
        port: int,
        ib_factory: Any | None = None,
        gatekeeper: Any | None = None,
        timeout_sec: float = 10.0,
        client_id: int | None = None,
        allow_when_not_manual_required: bool = False,
    ) -> RecoveryProofResult:
        """Run the operator-acknowledged single-lease manual recovery proof.

        This path is intentionally separate from automatic recovery probes. It is
        the only path allowed to clear a true manual-required gateway latch.
        """
        source_s = str(source or "").strip()
        if self._authorized_probe_sources and source_s not in self._authorized_probe_sources:
            return self._proof_result(
                status="UNAUTHORIZED",
                source=source_s,
                reason="unauthorized_recovery_probe_caller",
            )
        state = get_ib_gateway_connection_circuit().snapshot()
        ack_ok = bool(str(ack_token or "").strip()) or (
            os.getenv("IB_GATEWAY_RECOVERY_OPERATOR_ACK", "").strip().lower() in {"1", "true", "yes", "on"}
        )
        if state.manual_required and not ack_ok:
            return self._proof_result(
                status="ACK_REQUIRED",
                source=source_s,
                reason="operator_ack_required",
            )
        lease = get_ib_gateway_connection_circuit().try_acquire_manual_recovery_proof_lease(
            source=source_s,
            lease_sec=self._proof_lease_sec,
            allow_when_not_manual_required=bool(allow_when_not_manual_required),
            now_ts=now.timestamp(),
        )
        if not bool(lease.get("allowed")):
            return self._proof_result(
                status="LEASE_DENIED",
                source=source_s,
                reason=str(lease.get("reason") or "lease_denied"),
                lease_owner=str(lease.get("owner") or ""),
            )

        ib_constructed = False
        current_time_ok = False
        positions_ok = False
        open_orders_ok = False
        ib = None
        try:
            if ib_factory is None:
                from ib_insync import IB

                ib_factory = IB
            ib = ib_factory()
            ib_constructed = True
            proof_client_id = int(client_id if client_id is not None else os.getenv("IB_GATEWAY_RECOVERY_PROOF_CLIENT_ID", "98"))
            await ib.connectAsync(host, int(port), clientId=proof_client_id, timeout=max(1.0, float(timeout_sec)))
            await asyncio.wait_for(ib.reqCurrentTimeAsync(), timeout=max(1.0, float(timeout_sec)))
            current_time_ok = True
            positions_value = ib.positions()
            if hasattr(positions_value, "__await__"):
                positions_value = await positions_value
            list(positions_value or [])
            positions_ok = True
            if hasattr(ib, "reqOpenOrdersAsync"):
                await asyncio.wait_for(ib.reqOpenOrdersAsync(), timeout=max(1.0, float(timeout_sec)))
            open_orders_fn = getattr(ib, "openTrades", None) or getattr(ib, "openOrders", None)
            list(open_orders_fn() if callable(open_orders_fn) else [])
            open_orders_ok = True
            get_ib_gateway_connection_circuit().record_recovery_proof_success(
                source=source_s,
                reason="authorized_gateway_recovery_proof_succeeded",
            )
            if gatekeeper is not None and hasattr(gatekeeper, "clear_failure_backoff"):
                gatekeeper.clear_failure_backoff(caller=source_s, reason="authorized_gateway_recovery_proof_succeeded")
            result = self._proof_result(
                status="PROOF_SUCCEEDED",
                source=source_s,
                reason="authorized_gateway_recovery_proof_succeeded",
                current_time_ok=current_time_ok,
                positions_ok=positions_ok,
                open_orders_ok=open_orders_ok,
                lease_owner=source_s,
                ib_constructed=ib_constructed,
            )
            logger.info("ib_gateway_recovery_proof_succeeded", **result.as_dict())
            return result
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc) or repr(exc)}"
            get_ib_gateway_connection_circuit().record_recovery_proof_failure(
                reason=reason,
                source=source_s,
                now_ts=now.timestamp(),
            )
            if gatekeeper is not None and hasattr(gatekeeper, "report_aborted"):
                gatekeeper.report_aborted(caller=source_s, reason=reason)
            result = self._proof_result(
                status="PROOF_FAILED",
                source=source_s,
                reason=reason,
                current_time_ok=current_time_ok,
                positions_ok=positions_ok,
                open_orders_ok=open_orders_ok,
                lease_owner=source_s,
                ib_constructed=ib_constructed,
            )
            logger.warning("ib_gateway_recovery_proof_failed", **result.as_dict())
            return result
        finally:
            if ib is not None:
                try:
                    ib.disconnect()
                except Exception:
                    pass


_COORDINATOR: Optional[IBGatewayRecoveryCoordinator] = None


def get_ib_gateway_recovery_coordinator() -> IBGatewayRecoveryCoordinator:
    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = IBGatewayRecoveryCoordinator()
    return _COORDINATOR


async def run_authorized_gateway_recovery_proof(
    *,
    source: str,
    ack_token: str | None,
    now: datetime,
    host: str = "127.0.0.1",
    port: int = 4002,
    ib_factory: Any | None = None,
    gatekeeper: Any | None = None,
    timeout_sec: float = 10.0,
    client_id: int | None = None,
    allow_when_not_manual_required: bool = False,
) -> RecoveryProofResult:
    return await get_ib_gateway_recovery_coordinator().run_authorized_gateway_recovery_proof(
        source=source,
        ack_token=ack_token,
        now=now,
        host=host,
        port=port,
        ib_factory=ib_factory,
        gatekeeper=gatekeeper,
        timeout_sec=timeout_sec,
        client_id=client_id,
        allow_when_not_manual_required=allow_when_not_manual_required,
    )
