import json
import subprocess
import sys
from pathlib import Path


def test_run_stocks_kernel_launch_go_no_go_gate_writes_go_report(tmp_path: Path) -> None:
    readiness_path = tmp_path / "readiness.json"
    matrix_path = tmp_path / "matrix.json"
    audit_path = tmp_path / "audit.json"
    output_path = tmp_path / "gate.json"

    readiness_path.write_text(json.dumps({"ready": True, "reason": "ready", "blocking_checks": []}), encoding="utf-8")
    matrix_path.write_text(json.dumps({"passed": True, "reason": "ready", "scenario_count": 2}), encoding="utf-8")
    audit_path.write_text(json.dumps({"rows": 10, "blocked_events": 0, "kill_switch_events": 0, "protection_events": 0}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(Path("./tool/replay/run_stocks_kernel_launch_go_no_go_gate.py")),
            "--readiness-report",
            str(readiness_path),
            "--matrix-report",
            str(matrix_path),
            "--audit-summary",
            str(audit_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "GO"
