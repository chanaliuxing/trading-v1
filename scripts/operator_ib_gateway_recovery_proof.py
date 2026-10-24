#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.ibkr.ib_gateway_circuit import get_ib_gateway_connection_circuit
from services.ibkr.ib_gateway_recovery_coordinator import run_authorized_gateway_recovery_proof

DEFAULT_ACK = "I understand this will perform a disposable IB API proof"
DEFAULT_ARTIFACT = REPO_ROOT / "runtime/recovery/ib_gateway_operator_proof/latest.json"


def _now() -> datetime:
    return datetime.now().astimezone()


def _authorized_sources() -> set[str]:
    raw = os.getenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _proof_state(status: str, ok: bool) -> str:
    if status in {"DRY_RUN", "UNAUTHORIZED", "MISSING_ACK", "LIVE_OPT_IN_MISSING", "LEASE_DENIED"}:
        return "not_run"
    return "ok" if ok else "failed"


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an operator-approved IB Gateway manual recovery proof.")
    parser.add_argument("--live", action="store_true", help="Actually perform the disposable IB API proof.")
    parser.add_argument("--ack", default="", help=f"Required live ack text: {DEFAULT_ACK!r}.")
    parser.add_argument("--source", default="operator_cli", help="Recovery proof source; must be authorized for live mode.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=98)
    parser.add_argument("--account", default="")
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument(
        "--allow-when-not-manual-required",
        action="store_true",
        help="Allow one authorized verification proof even when manual_required is already false.",
    )
    parser.add_argument("--artifact-out", default=str(DEFAULT_ARTIFACT))
    return parser


async def run_operator_proof(args: argparse.Namespace, *, ib_factory: Any | None = None) -> tuple[int, dict[str, Any]]:
    generated_at = _now().isoformat()
    artifact_path = Path(str(args.artifact_out)).expanduser()
    circuit_before = get_ib_gateway_connection_circuit().snapshot().as_dict()
    source = str(args.source or "").strip()
    host = str(args.host)
    port = int(args.port)
    tcp_port = {"status": "not_run", "host": host, "port": port, "error": ""}
    authorized = source in _authorized_sources()
    ack_present = str(args.ack or "").strip() == DEFAULT_ACK
    live_env_ok = os.getenv("TRADING_TEST_ALLOW_LIVE_IBKR", "").strip() == "1"
    result = "DRY_RUN"
    errors: list[str] = []
    proof_status = "DRY_RUN"
    proof_obj = None
    exit_code = 0

    if not bool(args.live):
        result = "DRY_RUN"
    elif not live_env_ok:
        result = "LIVE_OPT_IN_MISSING"
        errors.append("TRADING_TEST_ALLOW_LIVE_IBKR=1 is required with --live")
        exit_code = 3
    elif not ack_present:
        result = "MISSING_ACK"
        errors.append("operator ack text missing or does not match required phrase")
        exit_code = 3
    elif not authorized:
        result = "UNAUTHORIZED"
        errors.append("source is not in IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES")
        exit_code = 3
    else:
        if ib_factory is None:
            tcp_port = {
                "status": "not_checked_by_design",
                "host": host,
                "port": port,
                "error": "authorized proof uses one disposable IB API session; passive port evidence belongs in check_paper_runtime",
            }
        else:
            tcp_port = {"status": "skipped_fake_ib_factory", "host": host, "port": port, "error": ""}
        proof_obj = await run_authorized_gateway_recovery_proof(
            source=source,
            ack_token=str(args.ack),
            now=_now(),
            host=host,
            port=port,
            ib_factory=ib_factory,
            timeout_sec=float(args.timeout_sec),
            client_id=int(args.client_id),
            allow_when_not_manual_required=bool(args.allow_when_not_manual_required),
        )
        proof_status = proof_obj.status
        if proof_obj.status == "PROOF_SUCCEEDED":
            result = "SUCCESS"
            exit_code = 0
        elif proof_obj.status == "LEASE_DENIED":
            result = "LEASE_DENIED"
            errors.append(proof_obj.reason)
            exit_code = 4
        elif proof_obj.status in {"UNAUTHORIZED", "ACK_REQUIRED"}:
            result = "UNAUTHORIZED" if proof_obj.status == "UNAUTHORIZED" else "MISSING_ACK"
            errors.append(proof_obj.reason)
            exit_code = 3
        else:
            result = "FAILED"
            errors.append(proof_obj.reason)
            exit_code = 2

    circuit_after = get_ib_gateway_connection_circuit().snapshot().as_dict()
    current_time_ok = bool(getattr(proof_obj, "current_time_ok", False))
    positions_ok = bool(getattr(proof_obj, "positions_ok", False))
    open_orders_ok = bool(getattr(proof_obj, "open_orders_ok", False))
    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "source": source,
        "host": host,
        "port": port,
        "client_id": int(args.client_id),
        "account": str(args.account or ""),
        "live_mode": bool(args.live),
        "authorized": bool(authorized),
        "ack_present": bool(ack_present),
        "lease_acquired": bool(proof_status in {"PROOF_SUCCEEDED", "PROOF_FAILED"}),
        "tcp_port": tcp_port,
        "circuit_before": circuit_before,
        "proof": {
            "api_handshake": _proof_state(result, current_time_ok),
            "current_time": _proof_state(result, current_time_ok),
            "positions": _proof_state(result, positions_ok),
            "open_orders": _proof_state(result, open_orders_ok),
        },
        "circuit_after": circuit_after,
        "manual_required_cleared": bool(circuit_before.get("manual_required")) and not bool(circuit_after.get("manual_required")),
        "result": result,
        "coordinator_status": proof_status,
        "errors": errors,
        "artifact_path": str(artifact_path),
    }
    _write_artifact(artifact_path, payload)
    return exit_code, payload


def main(argv: list[str] | None = None, *, ib_factory: Any | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    exit_code, payload = asyncio.run(run_operator_proof(args, ib_factory=ib_factory))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
