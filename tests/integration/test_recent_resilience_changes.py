import os
import socket
import subprocess
import sys
import importlib.util
from pathlib import Path

import pytest


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def test_recent_resilience_changes_script():
    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "tools" / "test_recent_resilience_changes.py"
    if not script.exists():
        pytest.fail(f"missing script: {script}")

    if not _port_open("127.0.0.1", 9092):
        pytest.skip("Kafka localhost:9092 not available")
    if not _port_open("127.0.0.1", 6379):
        pytest.skip("Redis 127.0.0.1:6379 not available")
    if importlib.util.find_spec("lz4") is None:
        pytest.skip("lz4 not installed; Kafka topic uses lz4 compression")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)

    cmd = [
        sys.executable,
        str(script),
        "--kafka-bootstrap",
        "localhost:9092",
        "--redis-url",
        "redis://127.0.0.1:6379/0",
        "--tick-count",
        "120",
        "--timeout-sec",
        "25",
    ]
    completed = subprocess.run(
        cmd,
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    output = completed.stdout + "\n" + completed.stderr
    if completed.returncode != 0 and "UnsupportedCodecError" in output:
        pytest.skip("Kafka cluster/topic requires unsupported compression codec in local env")
    assert completed.returncode == 0, output
    assert "Summary: passed=2, failed=0, total=2" in completed.stdout
