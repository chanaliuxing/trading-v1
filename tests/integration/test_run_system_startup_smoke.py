import json
import os
import subprocess
import sys
from pathlib import Path


def test_run_system_smoke_entrypoint_bootstraps_without_external_services() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "RUN_SYSTEM_SMOKE_CHECK": "1",
        "TRADING_MODE": "paper",
        "DISABLE_TICK_PUBLISHER": "1",
        "DISABLE_SFS_1S": "1",
    }

    proc = subprocess.run(
        [sys.executable, "run_system.py"],
        cwd=repo_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )

    marker = "RUN_SYSTEM_SMOKE_CHECK_RESULT "
    result_lines = [line for line in proc.stdout.splitlines() if line.startswith(marker)]
    assert proc.returncode == 0, proc.stdout
    assert result_lines, proc.stdout
    payload = json.loads(result_lines[-1][len(marker):])
    assert payload["ok"] is True
    check_names = {row["name"] for row in payload["checks"]}
    assert {
        "real_trading_system_instantiates",
        "enabled_strategy_modules_import",
        "phase0_freeze_gate",
        "strategy_engine_dry_load",
        "uom_caller_gate",
        "system_health_controller_instantiates",
        "unified_order_manager_instantiates",
        "runtime_reconciler_instantiates",
        "ib_startup_status_snapshot",
    }.issubset(check_names)

    checks_by_name = {row["name"]: row for row in payload["checks"]}
    dry_load = checks_by_name["strategy_engine_dry_load"]
    assert dry_load["enabled_count"] > 0
    assert dry_load["loaded_count"] == dry_load["enabled_count"]
    assert dry_load["failed_count"] == 0
    assert set(dry_load["live_strategy_ids"]).issubset(set(dry_load["loaded_strategy_ids"]))
