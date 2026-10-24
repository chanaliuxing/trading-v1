#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        check=False,
        text=True,
        capture_output=True,
    )
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-5:]).strip()
    if proc.returncode != 0 and proc.stderr:
        err_tail = "\n".join((proc.stderr or "").strip().splitlines()[-5:]).strip()
        if err_tail:
            tail = f"{tail}\n{err_tail}".strip()
    return int(proc.returncode), tail


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one full option launch readiness cycle")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"))
    parser.add_argument("--state-instrumentation-start-date", default="2026-04-03")
    parser.add_argument("--max-days", type=int, default=10)
    parser.add_argument("--skip-spy-shadow", action="store_true")
    args = parser.parse_args()

    py = str(ROOT / "venv" / "bin" / "python")

    steps: list[tuple[str, list[str]]] = [
        (
            "qqq_state_shadow_batch",
            [
                py,
                str(ROOT / "tool" / "replay" / "run_qqq_state_shadow_acceptance_batch.py"),
                "--source",
                "auto",
                "--auto-days",
                "--state-instrumentation-start-date",
                str(args.state_instrumentation_start_date),
                "--max-days",
                str(int(args.max_days)),
            ],
        ),
    ]

    if not bool(args.skip_spy_shadow):
        steps.append(
            (
                "spy_shadow_parity_day",
                [
                    py,
                    str(ROOT / "scripts" / "run_spy_option_ma_shadow_parity_day.py"),
                    "--date",
                    str(args.date),
                    "--out",
                    str(ROOT / "runtime" / "state" / f"spy_option_ma_shadow_parity_{args.date}.json"),
                ],
            )
        )

    steps.extend(
        [
            (
                "active_evidence",
                [py, str(ROOT / "tool" / "replay" / "run_option_active_strategy_evidence_status.py")],
            ),
            (
                "launch_gate",
                [py, str(ROOT / "tool" / "replay" / "run_option_launch_go_no_go_gate.py")],
            ),
        ]
    )

    status: dict[str, int] = {}
    outputs: dict[str, str] = {}
    for name, cmd in steps:
        code, tail = _run(cmd)
        status[name] = code
        if tail:
            outputs[name] = tail

    gate = _load_json(ROOT / "runtime" / "state" / "option_launch_go_no_go_gate.json")
    evidence = _load_json(ROOT / "runtime" / "state" / "option_active_strategy_evidence_status.json")

    summary = {
        "date": str(args.date),
        "state_instrumentation_start_date": str(args.state_instrumentation_start_date),
        "step_status": status,
        "step_output_tail": outputs,
        "active_evidence_recommendation": str(evidence.get("recommendation") or ""),
        "launch_gate_recommendation": str(gate.get("recommendation") or ""),
        "launch_gate_blockers": list(gate.get("blockers") or []),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if str(gate.get("recommendation") or "") == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
