#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tool.ops.build_opening_health_report import build_opening_health_report

LAUNCHD_LABEL = "com.trading.run_system.0925"


def _terminate_pids(pids: list[int]) -> dict[str, Any]:
    terminated: list[int] = []
    failed: list[str] = []
    for pid in sorted(set(int(pid) for pid in pids if int(pid) > 1)):
        try:
            os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
        except Exception as exc:
            failed.append(f"{pid}:{exc}")
    return {"terminated_pids": terminated, "terminate_failures": failed}


def _run_restart_command(cmd: str, *, cwd: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["/bin/bash", "-lc", cmd],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "returncode": int(proc.returncode),
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-20:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-20:]),
    }


def _default_restart_command() -> str:
    uid = os.getuid()
    service = f"gui/{uid}/{LAUNCHD_LABEL}"
    # launchd is the real supervisor on macOS. nohup is only a fallback for
    # developer shells where the LaunchAgent is not installed.
    return (
        f"launchctl print {service} >/dev/null 2>&1 "
        f"&& launchctl kickstart -k {service} "
        f"|| nohup scripts/run_system_autostart.sh >/dev/null 2>&1 &"
    )


def run_opening_supervisor(
    *,
    repo_root: str | Path | None = None,
    phase: str = "open",
    day: str | None = None,
    auto_remediate: bool = False,
    restart_cmd: str | None = None,
    stale_restart_sec: float = 180.0,
    recheck_delay_sec: float = 8.0,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    effective_restart_cmd = restart_cmd if restart_cmd is not None else _default_restart_command()
    initial = build_opening_health_report(repo_root=root, phase=phase, day=day)
    actions: list[dict[str, Any]] = []

    if auto_remediate:
        if "start_run_system" in list(initial.get("remediation_actions") or []):
            restart_result = _run_restart_command(effective_restart_cmd, cwd=root)
            actions.append({"action": "start_run_system", **restart_result})
        elif "restart_run_system" in list(initial.get("remediation_actions") or []):
            obs_age = float((initial.get("qqq_observability") or {}).get("age_sec") or 0.0)
            state_age = float((initial.get("qqq_state") or {}).get("age_sec") or 0.0)
            state_requires_heartbeat = bool((initial.get("qqq_state") or {}).get("requires_heartbeat"))
            state_is_stale = state_requires_heartbeat and state_age >= stale_restart_sec
            obs_is_stale = obs_age >= stale_restart_sec
            if obs_is_stale or state_is_stale:
                terminate_result = _terminate_pids(list((initial.get("run_system") or {}).get("process_pids") or []))
                actions.append({"action": "terminate_stale_run_system", **terminate_result})
                time.sleep(max(1.0, recheck_delay_sec))
                restart_result = _run_restart_command(effective_restart_cmd, cwd=root)
                actions.append({"action": "restart_run_system", **restart_result})

    if actions:
        time.sleep(max(1.0, recheck_delay_sec))
    final = build_opening_health_report(repo_root=root, phase=phase, day=day)
    restart_failures = [
        item for item in actions
        if str(item.get("action") or "").strip() in {"start_run_system", "restart_run_system"}
        and int(item.get("returncode") or 0) != 0
    ]
    if restart_failures:
        final = dict(final)
        blockers = list(final.get("blockers") or [])
        blockers.append("restart_command_failed")
        final["blockers"] = sorted(set(blockers))
        if str(final.get("recommendation") or "").strip() == "PASS":
            final["recommendation"] = "HOLD"
    payload = {
        "initial": initial,
        "actions": actions,
        "final": final,
        "auto_remediate": bool(auto_remediate),
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opening-time supervisor with bounded auto-remediation")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--phase", default="open", choices=("preopen", "open", "acceptance"))
    parser.add_argument("--day", default="")
    parser.add_argument("--auto-remediate", action="store_true")
    parser.add_argument("--restart-cmd", default="")
    parser.add_argument("--stale-restart-sec", type=float, default=180.0)
    parser.add_argument("--recheck-delay-sec", type=float, default=8.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    payload = run_opening_supervisor(
        repo_root=args.repo_root or None,
        phase=args.phase,
        day=args.day or None,
        auto_remediate=bool(args.auto_remediate),
        restart_cmd=args.restart_cmd or None,
        stale_restart_sec=args.stale_restart_sec,
        recheck_delay_sec=args.recheck_delay_sec,
    )
    output = Path(args.output) if str(args.output or "").strip() else REPO_ROOT / "runtime/state/opening_day" / f"{payload['final']['day']}_{payload['final']['phase']}_supervisor.json"
    if not output.is_absolute():
        output = REPO_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "phase": payload["final"]["phase"],
                "initial_recommendation": payload["initial"]["recommendation"],
                "final_recommendation": payload["final"]["recommendation"],
                "actions": [item["action"] for item in payload["actions"]],
                "final_blockers": payload["final"]["blockers"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if payload["final"]["recommendation"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
