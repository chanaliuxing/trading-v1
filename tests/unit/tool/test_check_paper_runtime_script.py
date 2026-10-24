from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_paper_runtime.sh"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run_check(
    repo_root: Path,
    *,
    ib_port: int = 9,
    pgrep_pattern: str = r"definitely-no-run-system-process-for-test",
) -> dict:
    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["TRADING_RUNTIME_PYTHON"] = sys.executable
    env["IB_HOST"] = "127.0.0.1"
    env["IB_PORT"] = str(ib_port)
    env["RUN_SYSTEM_PGREP_PATTERN"] = pgrep_pattern
    proc = subprocess.run(
        [str(SCRIPT)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_check_paper_runtime_reports_manual_gateway_circuit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_json(
        repo_root / "runtime/state/ib_gateway_connection_circuit.json",
        {
            "state": "proof_failed",
            "manual_required": True,
            "reason": "manual_gateway_api_recovery_required",
            "source": "market:pre_connect_api_probe",
            "last_proof_status": "PROOF_FAILED",
            "last_proof_reason": "TimeoutError: TimeoutError()",
        },
    )

    payload = _run_check(repo_root)

    assert payload["status"] == "fail"
    assert payload["reason"] == "ib_gateway_manual_required"
    assert payload["gateway_circuit"]["last_proof_status"] == "PROOF_FAILED"
    assert (
        payload["gateway_circuit"]["required_action"]
        == "operator_verify_gateway_login_without_restart_then_run_authorized_recovery_proof"
    )
    assert payload["gateway_circuit"]["do_not_restart_gateway"] is True
    assert payload["ib_api_ready"] is False
    assert payload["ib_port_check_mode"] == "passive_lsof"


def test_check_paper_runtime_distinguishes_tcp_open_from_api_handshake_failure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_json(
        repo_root / "runtime/state/ib_gateway_connection_circuit.json",
        {
            "state": "proof_failed",
            "manual_required": True,
            "reason": "manual_gateway_api_recovery_required",
            "source": "market:pre_connect_api_probe",
            "last_proof_status": "PROOF_FAILED",
            "last_proof_reason": "TimeoutError: TimeoutError()",
        },
    )
    _write_json(
        repo_root / "runtime/recovery/ib_gateway_operator_proof/latest.json",
        {
            "result": "FAILED",
            "coordinator_status": "PROOF_FAILED",
            "client_id": 97,
            "tcp_port": {"status": "ok"},
            "proof": {"api_handshake": "failed", "current_time": "failed"},
            "errors": ["TimeoutError: TimeoutError()"],
        },
    )
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = int(server.getsockname()[1])
        stop = threading.Event()

        def _accept_loop() -> None:
            server.settimeout(0.1)
            while not stop.is_set():
                try:
                    conn, _addr = server.accept()
                except OSError:
                    continue
                with conn:
                    pass

        thread = threading.Thread(target=_accept_loop, daemon=True)
        thread.start()

        payload = _run_check(repo_root, ib_port=port, pgrep_pattern="pytest")
        stop.set()
        thread.join(timeout=1.0)

    assert payload["status"] == "fail"
    assert payload["reason"] == "ib_api_handshake_failed_manual_required"
    assert payload["ib_port_ready"] is True
    assert payload["ib_port_check_mode"] == "passive_lsof"
    assert payload["ib_api_ready"] is False
    assert payload["operator_proof"]["tcp_port_status"] == "ok"
    assert payload["operator_proof"]["api_handshake"] == "failed"


def test_check_paper_runtime_reports_missing_process_without_manual_circuit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    payload = _run_check(repo_root)

    assert payload["status"] == "fail"
    assert payload["reason"] == "process_not_started"
    assert payload["gateway_circuit"]["manual_required"] is False
    assert payload["production_state"]["runtime_pid_alive"] is False


def test_check_paper_runtime_does_not_treat_stale_success_proof_as_ready(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_json(
        repo_root / "runtime/state/ib_gateway_connection_circuit.json",
        {
            "state": "recoverable_cooldown",
            "manual_required": False,
            "reason": "TimeoutError: TimeoutError()",
            "source": "market",
            "last_proof_status": "PROOF_SUCCEEDED",
            "last_proof_reason": "authorized_gateway_recovery_proof_succeeded",
        },
    )
    _write_json(
        repo_root / "runtime/recovery/ib_gateway_operator_proof/latest.json",
        {
            "result": "SUCCESS",
            "coordinator_status": "PROOF_SUCCEEDED",
            "manual_required_cleared": True,
            "client_id": 97,
            "proof": {
                "api_handshake": "ok",
                "current_time": "ok",
                "positions": "ok",
                "open_orders": "ok",
            },
        },
    )
    _write_json(
        repo_root / "runtime/recovery/ib_gateway_operator_proof/client10_verify.json",
        {
            "result": "FAILED",
            "coordinator_status": "PROOF_FAILED",
            "client_id": 10,
            "proof": {"api_handshake": "failed", "current_time": "failed"},
            "errors": ["TimeoutError: TimeoutError()"],
        },
    )

    payload = _run_check(repo_root)

    assert payload["status"] == "fail"
    assert payload["reason"] == "process_not_started"
    assert payload["ib_api_ready"] is False
    assert payload["ib_disposable_api_ready"] is False
    assert payload["ib_production_clients_ready"] is False
    assert payload["operator_proof"]["matches_circuit"] is False
    assert payload["production_client_proof"]["client_id"] == 10
    assert payload["production_client_proof"]["api_handshake"] == "failed"


def test_check_paper_runtime_reports_production_client_handshake_failure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_json(
        repo_root / "runtime/state/ib_gateway_connection_circuit.json",
        {
            "state": "recoverable_cooldown",
            "manual_required": False,
            "reason": "TimeoutError: TimeoutError()",
            "source": "market",
        },
    )
    _write_json(
        repo_root / "runtime/readiness/production_state/production_state_latest.json",
        {
            "generated_at": "2099-01-01T12:00:00-05:00",
            "go_no_go": "no_go",
            "runtime_pid": os.getpid(),
        },
    )
    _write_json(
        repo_root / "runtime/recovery/ib_gateway_operator_proof/client10_verify.json",
        {
            "result": "FAILED",
            "coordinator_status": "PROOF_FAILED",
            "client_id": 10,
            "proof": {"api_handshake": "failed"},
            "errors": ["TimeoutError: TimeoutError()"],
        },
    )
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = int(server.getsockname()[1])
        stop = threading.Event()

        def _accept_loop() -> None:
            server.settimeout(0.1)
            while not stop.is_set():
                try:
                    conn, _addr = server.accept()
                except OSError:
                    continue
                with conn:
                    pass

        thread = threading.Thread(target=_accept_loop, daemon=True)
        thread.start()
        payload = _run_check(repo_root, ib_port=port)
        stop.set()
        thread.join(timeout=1.0)

    assert payload["status"] == "fail"
    assert payload["reason"] == "production_client_api_handshake_failed"
    assert payload["ib_port_check_mode"] == "passive_lsof"
    assert payload["production_state"]["runtime_pid_alive"] is True
