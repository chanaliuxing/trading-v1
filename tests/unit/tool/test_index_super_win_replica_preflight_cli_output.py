from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml


def _write_manifest(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "runtime_scope": "index_super_win_replica",
                "order_namespace": "iswv2",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                        "volatility_symbol": "QQQ",
                        "enabled": True,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_preflight_cli_writes_output_report(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    manifest_path = tmp_path / "manifest.yml"
    baseline_path = tmp_path / "baseline.json"
    runtime_path = tmp_path / "runtime.json"
    broker_path = tmp_path / "broker.json"
    diag_path = tmp_path / "runtime" / "state" / "diag.json"
    audit_root = tmp_path / "runtime" / "audit"
    output_path = tmp_path / "preflight.json"

    _write_manifest(manifest_path)
    baseline_path.write_text(json.dumps({"baseline": True}), encoding="utf-8")
    runtime_path.write_text(
        json.dumps(
            {
                "QQQ": {
                    "is_traded": False,
                    "active_side": None,
                    "expected_entry_qty": 0,
                    "extras": {"live_qty": 0},
                }
            }
        ),
        encoding="utf-8",
    )
    broker_path.write_text(json.dumps({"TQQQ": 0.0, "SQQQ": 0.0}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(root / "tool" / "index_super_win_replica_preflight.py"),
            "--manifest",
            str(manifest_path),
            "--baseline",
            str(baseline_path),
            "--runtime-snapshot",
            str(runtime_path),
            "--broker-positions",
            str(broker_path),
            "--diag-snapshot",
            str(diag_path),
            "--audit-root",
            str(audit_root),
            "--output-path",
            str(output_path),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "ready_for_enablement" in payload
    assert result.returncode in {0, 2}
