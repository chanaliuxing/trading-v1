#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR_DEFAULT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="${REPO_DIR:-$REPO_DIR_DEFAULT}"
PY="${TRADING_RUNTIME_PYTHON:-$REPO_DIR/venv_ml/bin/python}"

cd "$REPO_DIR"

if [ ! -x "$PY" ]; then
  printf '{"status":"fail","reason":"runtime_python_missing","python":"%s"}\n' "$PY"
  exit 1
fi

exec "$PY" - "$REPO_DIR" <<'PY'
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

root = Path(sys.argv[1]).resolve()
et = ZoneInfo("America/New_York")


def read_json(*rel_paths: str) -> dict:
    for rel in rel_paths:
        path = root / rel
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def git(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=str(root), text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def pids() -> list[str]:
    try:
        pattern = os.getenv("RUN_SYSTEM_PGREP_PATTERN", r"[p]ython.*run_system.py")
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True, stderr=subprocess.DEVNULL)
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def pid_alive(pid: object) -> bool:
    try:
        pid_i = int(pid or 0)
        if pid_i <= 0:
            return False
        os.kill(pid_i, 0)
        return True
    except Exception:
        return False


def ib_port_ready() -> bool:
    host = os.getenv("IB_HOST", "127.0.0.1")
    port = int(os.getenv("IB_PORT", "4002"))
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except Exception:
        return False


def ib_port_listening_passive() -> bool:
    port = int(os.getenv("IB_PORT", "4002"))
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        return f":{port} " in out or f":{port}->" in out or f"*:{port}" in out
    except Exception:
        return False


prod = read_json(
    "runtime/readiness/production_state/production_state_latest.json",
    "runtime/readiness/production_state_latest.json",
)
pos = read_json("runtime/readiness/position_safety/position_safety_latest.json")
circuit = read_json("runtime/state/ib_gateway_connection_circuit.json")
proof = read_json("runtime/recovery/ib_gateway_operator_proof/latest.json")
production_client_proof = read_json(
    "runtime/recovery/ib_gateway_operator_proof/production_client_ids_latest.json",
    "runtime/recovery/ib_gateway_operator_proof/client10_verify.json",
)
pids_found = pids()
manual_required = bool(circuit.get("manual_required"))
gateway_reason_text = " ".join(
    str(item or "")
    for item in (
        circuit.get("state"),
        circuit.get("reason"),
        circuit.get("last_proof_reason"),
    )
)
gateway_api_handshake_failed = "TimeoutError" in gateway_reason_text
if manual_required or gateway_api_handshake_failed:
    ib_port_is_ready = ib_port_listening_passive()
    ib_port_check_mode = "passive_lsof"
else:
    ib_port_is_ready = ib_port_ready()
    ib_port_check_mode = "active_tcp_connect"
proof_result = str(proof.get("result") or "")
proof_status = str(proof.get("coordinator_status") or "")
proof_block = proof.get("proof") if isinstance(proof.get("proof"), dict) else {}
latest_proof_matches_circuit = (
    proof_result.upper() == "SUCCESS"
    and str(circuit.get("state") or "") == "closed"
    and str(circuit.get("last_proof_status") or "") == "PROOF_SUCCEEDED"
    and not bool(circuit.get("manual_required"))
)
runtime_pid = prod.get("runtime_pid") or (prod.get("runtime_version") or {}).get("process_pid")
runtime_pid_alive = pid_alive(runtime_pid)
process_running = bool(pids_found) or bool(runtime_pid_alive)
if proof_result.upper() == "DRY_RUN" and str(circuit.get("last_proof_status") or ""):
    proof_result = "FAILED" if str(circuit.get("last_proof_status")) == "PROOF_FAILED" else proof_result
    proof_status = str(circuit.get("last_proof_status") or proof_status)
    if "TimeoutError" in str(circuit.get("last_proof_reason") or ""):
        proof_block = {
            **proof_block,
            "api_handshake": "failed",
            "current_time": "failed",
            "positions": "failed",
            "open_orders": "failed",
        }
generated_at = str(prod.get("generated_at") or "")
stale = True
try:
    ts = datetime.fromisoformat(generated_at)
    stale = (datetime.now(et) - ts.astimezone(et)).total_seconds() > 120
except Exception:
    stale = True

status = "ok"
reason = "runtime_ready"
if manual_required:
    last_proof_reason = str(circuit.get("last_proof_reason") or "")
    if ib_port_is_ready and "TimeoutError" in last_proof_reason:
        status, reason = "fail", "ib_api_handshake_failed_manual_required"
    else:
        status, reason = "fail", "ib_gateway_manual_required"
elif not process_running:
    status, reason = "fail", "process_not_started"
elif not ib_port_is_ready:
    status, reason = "fail", "ib_port_not_ready"
elif (
    str(production_client_proof.get("result") or "").upper() == "FAILED"
    and "TimeoutError" in " ".join(str(item) for item in (production_client_proof.get("errors") or []))
):
    status, reason = "fail", "production_client_api_handshake_failed"
elif "TimeoutError" in str(circuit.get("reason") or ""):
    status, reason = "warn", "ib_api_handshake_failed"
elif stale:
    status, reason = "warn", "runtime_artifact_stale"
elif str(prod.get("go_no_go") or "").lower() != "go":
    status, reason = "warn", "runtime_not_go"
elif str(prod.get("entry_go_no_go") or "").lower() == "partial":
    status, reason = "ok", "entry_partial_expected"

payload = {
    "status": status,
    "reason": reason,
    "repo_root": str(root),
    "runtime_pids": pids_found,
    "python": sys.executable,
    "git_head": git(["rev-parse", "HEAD"]),
    "git_dirty": bool(git(["status", "--porcelain"])),
    "ib_port_ready": ib_port_is_ready,
    "ib_api_ready": bool(latest_proof_matches_circuit),
    "ib_disposable_api_ready": bool(latest_proof_matches_circuit),
    "ib_production_clients_ready": (
        str(production_client_proof.get("result") or "").upper() == "SUCCESS"
        and str(production_client_proof.get("coordinator_status") or "") == "PROOF_SUCCEEDED"
    ),
    "gateway_circuit": {
        "state": circuit.get("state"),
        "manual_required": bool(circuit.get("manual_required")),
        "reason": circuit.get("reason"),
        "source": circuit.get("source"),
        "last_proof_status": circuit.get("last_proof_status"),
        "last_proof_reason": circuit.get("last_proof_reason"),
        "required_action": (
            "operator_verify_gateway_login_without_restart_then_run_authorized_recovery_proof"
            if bool(circuit.get("manual_required"))
            else circuit.get("required_action")
        ),
        "do_not_restart_gateway": bool(circuit.get("manual_required")),
    },
    "ib_port_check_mode": ib_port_check_mode,
    "operator_proof": {
        "result": proof_result or None,
        "coordinator_status": proof_status or None,
        "client_id": proof.get("client_id"),
        "generated_at": proof.get("generated_at"),
        "tcp_port_status": (proof.get("tcp_port") or {}).get("status"),
        "api_handshake": proof_block.get("api_handshake"),
        "current_time": proof_block.get("current_time"),
        "positions": proof_block.get("positions"),
        "open_orders": proof_block.get("open_orders"),
        "errors": proof.get("errors") or ([circuit.get("last_proof_reason")] if circuit.get("last_proof_reason") else []),
        "matches_circuit": bool(latest_proof_matches_circuit),
    },
    "production_client_proof": {
        "result": production_client_proof.get("result") or None,
        "coordinator_status": production_client_proof.get("coordinator_status") or None,
        "client_id": production_client_proof.get("client_id"),
        "generated_at": production_client_proof.get("generated_at"),
        "api_handshake": (
            production_client_proof.get("proof", {}).get("api_handshake")
            if isinstance(production_client_proof.get("proof"), dict)
            else None
        ),
        "current_time": (
            production_client_proof.get("proof", {}).get("current_time")
            if isinstance(production_client_proof.get("proof"), dict)
            else None
        ),
        "errors": production_client_proof.get("errors") or [],
    },
    "production_state": {
        "go_no_go": prod.get("go_no_go"),
        "entry_go_no_go": prod.get("entry_go_no_go"),
        "safety_go_no_go": prod.get("safety_go_no_go"),
        "generated_at": generated_at,
        "runtime_pid": runtime_pid,
        "runtime_pid_alive": runtime_pid_alive,
        "runtime_git_sha": prod.get("runtime_git_sha") or (prod.get("runtime_version") or {}).get("git_head"),
    },
    "position_safety": {
        "go_no_go": pos.get("go_no_go"),
        "active_manual_lock_count": pos.get("active_manual_lock_count"),
        "active_quarantine_count": pos.get("active_quarantine_count"),
        "blockers": pos.get("blockers") or [],
    },
}
print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
PY
