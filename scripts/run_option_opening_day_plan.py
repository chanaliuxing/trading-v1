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
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False, text=True, capture_output=True)
    out_tail = "\n".join((proc.stdout or "").strip().splitlines()[-8:]).strip()
    err_tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:]).strip()
    tail = out_tail if out_tail else ""
    if err_tail:
        tail = (tail + "\n" + err_tail).strip()
    return int(proc.returncode), tail


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _step_commands(*, phase: str, date: str, state_start: str, max_days: int) -> list[tuple[str, list[str]]]:
    py = str(ROOT / "venv" / "bin" / "python")
    steps: list[tuple[str, list[str]]] = []
    if phase == "preopen":
        steps.extend(
            [
                ("spy_readiness_check", [py, str(ROOT / "scripts" / "check_spy_option_ma_readiness.py")]),
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
        return steps

    if phase == "intraday":
        steps.append(
            (
                "launch_readiness_cycle",
                [
                    py,
                    str(ROOT / "scripts" / "run_option_launch_readiness_cycle.py"),
                    "--date",
                    str(date),
                    "--state-instrumentation-start-date",
                    str(state_start),
                    "--max-days",
                    str(int(max_days)),
                ],
            )
        )
        return steps

    if phase == "eod":
        steps.extend(
            [
                (
                    "qqq_state_shadow_batch",
                    [
                        py,
                        str(ROOT / "tool" / "replay" / "run_qqq_state_shadow_acceptance_batch.py"),
                        "--source",
                        "auto",
                        "--auto-days",
                        "--state-instrumentation-start-date",
                        str(state_start),
                        "--max-days",
                        str(int(max_days)),
                    ],
                ),
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
        return steps

    raise ValueError(f"unsupported phase: {phase}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run opening-day option launch plan by phase")
    parser.add_argument("--phase", choices=["preopen", "intraday", "eod"], required=True)
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"))
    parser.add_argument("--state-instrumentation-start-date", default="2026-04-03")
    parser.add_argument("--max-days", type=int, default=10)
    args = parser.parse_args()

    phase = str(args.phase)
    date = str(args.date)
    state_start = str(args.state_instrumentation_start_date)
    max_days = int(args.max_days)

    steps = _step_commands(phase=phase, date=date, state_start=state_start, max_days=max_days)
    status: dict[str, int] = {}
    output_tail: dict[str, str] = {}
    for name, cmd in steps:
        code, tail = _run(cmd)
        status[name] = code
        if tail:
            output_tail[name] = tail

    gate = _load_json(ROOT / "runtime" / "state" / "option_launch_go_no_go_gate.json")
    evidence = _load_json(ROOT / "runtime" / "state" / "option_active_strategy_evidence_status.json")

    summary = {
        "phase": phase,
        "date": date,
        "state_instrumentation_start_date": state_start,
        "step_status": status,
        "step_output_tail": output_tail,
        "active_evidence_recommendation": str(evidence.get("recommendation") or ""),
        "launch_gate_recommendation": str(gate.get("recommendation") or ""),
        "launch_gate_blockers": list(gate.get("blockers") or []),
    }

    out_dir = ROOT / "runtime" / "state" / "opening_day" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{phase}_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["report_path"] = str(out_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))

    if phase == "preopen":
        return 0 if status.get("spy_readiness_check", 1) == 0 else 1
    return 0 if str(gate.get("recommendation") or "") == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
