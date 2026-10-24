from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _copy_start_script(repo_root: Path) -> Path:
    source = Path(__file__).resolve().parents[3] / "scripts" / "start_paper_runtime.sh"
    target = repo_root / "scripts" / "start_paper_runtime.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    return target


def _install_no_match_pgrep(repo_root: Path, env: dict[str, str]) -> None:
    bin_dir = repo_root / "test-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    pgrep = bin_dir / "pgrep"
    pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    pgrep.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"


def _install_fake_python(repo_root: Path, body: str | None = None) -> Path:
    fake_python = repo_root / "venv_ml" / "bin" / "python"
    fake_python.parent.mkdir(parents=True, exist_ok=True)
    fake_python.write_text(
        body
        or (
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "-" ]; then exit 0; fi\n'
            "exit 0\n"
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return fake_python


def test_start_paper_runtime_check_only_removes_dead_lock_with_evidence(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    script = _copy_start_script(repo_root)
    _install_fake_python(repo_root)
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")
    lock_path = repo_root / "runtime" / "locks" / "run_system.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("99999999", encoding="utf-8")

    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["START_PAPER_RUNTIME_SKIP_DEP_CHECK"] = "1"
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script), "--check-only"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] == "ok"
    assert not lock_path.exists()


def test_start_paper_runtime_rejects_live_lock_owner(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    script = _copy_start_script(repo_root)
    _install_fake_python(repo_root)
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")
    lock_path = repo_root / "runtime" / "locks" / "run_system.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["START_PAPER_RUNTIME_SKIP_DEP_CHECK"] = "1"
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script), "--check-only"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "run_system_lock_owned_by_live_process" in proc.stderr
    assert lock_path.exists()


def test_start_paper_runtime_defaults_to_venv_ml(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    script = _copy_start_script(repo_root)
    capture = repo_root / "python_exec.txt"
    fake_python = _install_fake_python(
        repo_root,
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-" ]; then exit 0; fi\n'
        f'printf "%s\\n" "$0" > "{capture}"\n',
    )
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")

    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["START_PAPER_RUNTIME_SKIP_DEP_CHECK"] = "1"
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert capture.read_text(encoding="utf-8").strip() == str(fake_python)


def test_start_paper_runtime_blocks_manual_required_gateway_without_restart(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    script = _copy_start_script(repo_root)
    _install_fake_python(repo_root)
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")
    circuit_path = repo_root / "runtime" / "state" / "ib_gateway_connection_circuit.json"
    circuit_path.parent.mkdir(parents=True, exist_ok=True)
    circuit_path.write_text(
        (
            '{"manual_required": true, "reason": "manual_gateway_api_recovery_required", '
            '"last_proof_reason": "TimeoutError: TimeoutError()"}\n'
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["START_PAPER_RUNTIME_SKIP_DEP_CHECK"] = "1"
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script), "--check-only"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["reason"] in {"ib_gateway_manual_required", "ib_api_handshake_failed_manual_required"}
    assert "do_not_restart_gateway=true" in payload["details"]
    assert "operator_verify_gateway_login_without_restart_then_run_authorized_recovery_proof" in payload["details"]
    assert "ib_port_check_mode=passive_lsof" in payload["details"]


def test_start_paper_runtime_reports_handshake_failed_when_manual_circuit_has_passive_listen(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    script = _copy_start_script(repo_root)
    _install_fake_python(repo_root)
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")
    circuit_path = repo_root / "runtime" / "state" / "ib_gateway_connection_circuit.json"
    circuit_path.parent.mkdir(parents=True, exist_ok=True)
    circuit_path.write_text(
        json.dumps(
            {
                "manual_required": True,
                "reason": "manual_gateway_api_recovery_required",
                "last_proof_reason": "TimeoutError: TimeoutError()",
            }
        ),
        encoding="utf-8",
    )
    bin_dir = repo_root / "test-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lsof = bin_dir / "lsof"
    lsof.write_text(
        "#!/usr/bin/env bash\n"
        'printf "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\\nJava 1 u 1 TCP *:4002 (LISTEN)\\n"\n',
        encoding="utf-8",
    )
    lsof.chmod(0o755)

    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["START_PAPER_RUNTIME_SKIP_DEP_CHECK"] = "1"
    env["TRADING_RUNTIME_PYTHON"] = sys.executable
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script), "--check-only"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["reason"] == "ib_api_handshake_failed_manual_required"
    assert "ib_port_ready=true" in payload["details"]


def test_start_paper_runtime_allows_stale_manual_pre_connect_probe_circuit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    script = _copy_start_script(repo_root)
    _install_fake_python(repo_root)
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")
    circuit_path = repo_root / "runtime" / "state" / "ib_gateway_connection_circuit.json"
    circuit_path.parent.mkdir(parents=True, exist_ok=True)
    circuit_path.write_text(
        json.dumps(
            {
                "state": "manual_required",
                "manual_required": True,
                "source": "market:pre_connect_api_probe",
                "reason": "manual_gateway_api_recovery_required",
                "last_proof_status": "PROOF_FAILED",
                "last_proof_reason": "TimeoutError: TimeoutError()",
                "consecutive_failures": 7,
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["START_PAPER_RUNTIME_SKIP_DEP_CHECK"] = "1"
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script), "--check-only"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stderr
    assert payload["reason"] == "preflight_passed"
    assert "stale pre-connect probe manual circuit" in proc.stderr


def test_start_paper_runtime_blocks_repeated_nonmanual_api_handshake_failure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    script = _copy_start_script(repo_root)
    _install_fake_python(repo_root)
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")
    circuit_path = repo_root / "runtime" / "state" / "ib_gateway_connection_circuit.json"
    circuit_path.parent.mkdir(parents=True, exist_ok=True)
    circuit_path.write_text(
        json.dumps(
            {
                "state": "recoverable_cooldown",
                "manual_required": False,
                "reason": "TimeoutError: TimeoutError()",
                "last_proof_status": "PROOF_FAILED",
                "last_proof_reason": "TimeoutError: TimeoutError()",
                "consecutive_failures": 3,
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["START_PAPER_RUNTIME_SKIP_DEP_CHECK"] = "1"
    env["TRADING_RUNTIME_PYTHON"] = sys.executable
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script), "--check-only"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["reason"] == "ib_api_handshake_failed_recovery_required"
    assert "do_not_start_runtime=true" in payload["details"]
    assert "do_not_restart_gateway=true" in payload["details"]


def test_start_paper_runtime_allows_stale_nonmanual_pre_connect_probe_circuit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    script = _copy_start_script(repo_root)
    _install_fake_python(repo_root)
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")
    circuit_path = repo_root / "runtime" / "state" / "ib_gateway_connection_circuit.json"
    circuit_path.parent.mkdir(parents=True, exist_ok=True)
    circuit_path.write_text(
        json.dumps(
            {
                "state": "closed",
                "manual_required": False,
                "source": "market:pre_connect_api_probe",
                "reason": "manual_gateway_api_recovery_required",
                "last_proof_status": "PROOF_FAILED",
                "last_proof_reason": "TimeoutError: TimeoutError()",
                "consecutive_failures": 0,
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["REPO_DIR"] = str(repo_root)
    env["START_PAPER_RUNTIME_SKIP_DEP_CHECK"] = "1"
    env["TRADING_RUNTIME_PYTHON"] = sys.executable
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script), "--check-only"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stderr
    assert payload["reason"] == "preflight_passed"
