from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _install_no_match_pgrep(repo_root: Path, env: dict[str, str]) -> None:
    bin_dir = repo_root / "test-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    pgrep = bin_dir / "pgrep"
    pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    pgrep.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"


def _install_fake_runtime(repo_root: Path, *, start_body: str) -> Path:
    source_script = Path(__file__).resolve().parents[3] / "scripts" / "run_system_autostart.sh"
    script_path = repo_root / "scripts" / "run_system_autostart.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(source_script.read_text(encoding="utf-8"), encoding="utf-8")
    script_path.chmod(0o755)

    fake_python = repo_root / "venv_ml" / "bin" / "python"
    fake_python.parent.mkdir(parents=True, exist_ok=True)
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-" ]; then exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    (repo_root / "run_system.py").write_text("print('ok')\n", encoding="utf-8")

    start_script = repo_root / "scripts" / "start_paper_runtime.sh"
    start_script.write_text(start_body, encoding="utf-8")
    start_script.chmod(0o755)
    return script_path


def test_run_system_autostart_delegates_to_unique_start_script(tmp_path: Path) -> None:
    repo_root = tmp_path
    capture_path = repo_root / "start_invocation.txt"
    script_path = _install_fake_runtime(
        repo_root,
        start_body=(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$PWD" "$TRADING_MODE" "$AUDIT_LOG_PATH" > "{capture_path}"\n'
        ),
    )

    env = os.environ.copy()
    env.pop("REPO_DIR", None)
    env.pop("LOG_DIR", None)
    env.pop("AUDIT_LOG_PATH", None)
    env.pop("TRADING_MODE", None)
    blocked_home = tmp_path / "blocked-home"
    blocked_home.write_text("not a directory", encoding="utf-8")
    env["HOME"] = str(blocked_home)
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script_path)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert capture_path.exists()
    lines = capture_path.read_text(encoding="utf-8").splitlines()
    assert lines == [str(repo_root), "shadow", str(repo_root / "logs/trader.log")]


def test_run_system_autostart_respects_env_file_trading_mode(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    capture_path = repo_root / "trading_mode.txt"
    script_path = _install_fake_runtime(
        repo_root,
        start_body=("#!/usr/bin/env bash\n" f'printf "%s\\n" "$TRADING_MODE" > "{capture_path}"\n'),
    )
    (repo_root / ".env").write_text("TRADING_MODE=paper\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop("TRADING_MODE", None)
    env.pop("REPO_DIR", None)
    env.pop("LOG_DIR", None)
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script_path)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert capture_path.read_text(encoding="utf-8").strip() == "paper"


def test_run_system_autostart_uses_venv_ml_for_audit_probe(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    capture_path = repo_root / "python_probe.txt"
    script_path = _install_fake_runtime(
        repo_root,
        start_body="#!/usr/bin/env bash\nexit 0\n",
    )
    fake_python = repo_root / "venv_ml" / "bin" / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-" ]; then\n'
        f'  printf "%s\\n" "$0" > "{capture_path}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("REPO_DIR", None)
    env.pop("LOG_DIR", None)
    _install_no_match_pgrep(repo_root, env)

    proc = subprocess.run(
        [str(script_path)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert capture_path.read_text(encoding="utf-8").strip() == str(fake_python)
