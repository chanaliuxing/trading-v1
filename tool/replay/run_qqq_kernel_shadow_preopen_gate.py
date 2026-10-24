#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.replay.check_qqq_kernel_shadow_readiness import build_readiness_report


def _run(cmd: list[str]) -> dict[str, Any]:
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
    return {
        "cmd": cmd,
        "returncode": int(result.returncode),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "passed": int(result.returncode) == 0,
    }


def run_preopen_gate(
    *,
    config_path: Path,
    output: Path,
    run_tests: bool = True,
    allow_enabled: bool = False,
) -> dict[str, Any]:
    readiness = build_readiness_report(config_path=config_path, require_disabled=not bool(allow_enabled))
    commands: list[dict[str, Any]] = []
    if run_tests:
        commands.append(
            _run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/unit/tool/test_check_qqq_kernel_shadow_readiness.py",
                    "tests/unit/tool/test_run_qqq_kernel_shadow_parity.py",
                    "tests/unit/tool/test_switch_qqq_kernel_shadow.py",
                    "tests/unit/strategies/test_qqq_kernel_shadow_config.py",
                ]
            )
        )
    ok = bool(readiness.get("ok")) and all(bool(item.get("passed")) for item in commands)
    payload = {
        "ok": ok,
        "recommendation": "GO_FOR_READINESS_ONLY" if ok else "HOLD",
        "readiness": readiness,
        "commands": commands,
        "next_allowed_action": "run_dry_run_shadow" if ok and allow_enabled else "keep_shadow_disabled_or_apply_migration" if ok else "fix_failed_checks",
        "not_allowed": [
            "do_not_disable_qqq_intraday_001",
            "do_not_set_shadow_dry_run_false",
            "do_not_remove_shadow_only",
            "do_not_treat_readiness_go_as_cutover_go",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run QQQ kernel shadow pre-open readiness gate.")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--output", default=str(ROOT / "reports" / "qqq_kernel_shadow_parity" / "20260421_preopen_gate.json"))
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--allow-enabled", action="store_true")
    args = parser.parse_args(argv)
    payload = run_preopen_gate(
        config_path=Path(args.config),
        output=Path(args.output),
        run_tests=not bool(args.skip_tests),
        allow_enabled=bool(args.allow_enabled),
    )
    print(json.dumps({"ok": payload["ok"], "recommendation": payload["recommendation"], "output": str(args.output)}, ensure_ascii=False, sort_keys=True))
    return 0 if bool(payload["ok"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
