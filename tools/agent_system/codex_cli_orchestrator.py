#!/usr/bin/env python3
"""
Codex CLI multi-agent orchestrator.

Uses `codex exec --json` and `codex exec --json resume <session_id>`
to run a staged agent workflow while preserving per-agent memory in
stable sessions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Stage:
    key: str
    role: str
    goal: str
    input_hint: str


DEFAULT_STAGES: List[Stage] = [
    Stage(
        key="requirements",
        role="RequirementsAgent",
        goal="Produce PRD, IO schema spec, and acceptance draft.",
        input_hint="Use the user's objective and existing architecture docs.",
    ),
    Stage(
        key="review",
        role="ReviewAgent",
        goal="Review requirements artifacts and list ambiguity, inconsistency, and missing constraints.",
        input_hint="Cite exact file paths and section names.",
    ),
    Stage(
        key="critical",
        role="CriticalAgent",
        goal="Attack the design with adversarial/failure analysis and challenge weak assumptions.",
        input_hint="Focus on exploit paths, race conditions, operational risks, and rollback risks.",
    ),
    Stage(
        key="review_revision",
        role="ReviewAgent",
        goal="Revise docs to resolve critical findings and produce final requirement pack.",
        input_hint="Resolve or explicitly accept each high-severity issue.",
    ),
    Stage(
        key="validate",
        role="ValidateAgent",
        goal="Validate consistency and produce gate verdict (PASS/FAIL/BLOCKED).",
        input_hint="Check schema consistency and requirement traceability.",
    ),
    Stage(
        key="dev",
        role="DevAgent",
        goal="Implement code changes + tests according to validated specs.",
        input_hint="Map each change to requirement IDs and provide test evidence.",
    ),
    Stage(
        key="acceptance",
        role="AcceptanceAgent",
        goal="Run acceptance + smoke verification and issue release verdict.",
        input_hint="Reject if any P0/P1 issue or failed gate remains.",
    ),
]


def _now_slug() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", name).strip("_") or "run"


def _load_text(path: Optional[str], fallback: str) -> str:
    if not path:
        return fallback
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Objective file not found: {p}")
    return p.read_text(encoding="utf-8").strip() or fallback


def _run_cmd(cmd: List[str], cwd: Path) -> Tuple[int, str, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _parse_codex_jsonl(output: str) -> Tuple[Optional[str], Optional[str], List[dict]]:
    thread_id: Optional[str] = None
    last_message: Optional[str] = None
    events: List[dict] = []

    for raw in output.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        events.append(obj)
        if obj.get("type") == "thread.started":
            thread_id = obj.get("thread_id")
        if obj.get("type") == "item.completed":
            item = obj.get("item", {})
            if item.get("type") == "agent_message":
                last_message = item.get("text")
    return thread_id, last_message, events


def _extract_status(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(?im)^\s*status\s*:\s*(PASS|FAIL|BLOCKED)\b", text)
    return m.group(1).upper() if m else None


def _load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _build_prompt(
    stage: Stage,
    objective: str,
    project_root: Path,
    artifact_dir: Path,
    previous_stage_note: str,
    run_name: str,
) -> str:
    return (
        f"You are {stage.role}.\\n"
        f"Run name: {run_name}\\n"
        f"Project root: {project_root}\\n"
        f"Artifact directory: {artifact_dir}\\n"
        f"Objective:\\n{objective}\\n\\n"
        f"Stage goal: {stage.goal}\\n"
        f"Stage input hint: {stage.input_hint}\\n"
        f"Previous stage summary:\\n{previous_stage_note}\\n\\n"
        "Output requirements:\\n"
        "1) Start with `Status: PASS|FAIL|BLOCKED` on its own line.\\n"
        "2) Then provide concise findings/changes/evidence.\\n"
        "3) Reference exact file paths you changed/reviewed.\\n"
        "4) Keep output actionable for the next agent."
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _print(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def run_stage(
    *,
    stage: Stage,
    session_id: Optional[str],
    prompt: str,
    project_root: Path,
) -> Tuple[Optional[str], str, str, Optional[str], List[dict]]:
    if session_id:
        cmd = ["codex", "exec", "--json", "resume", session_id, prompt]
    else:
        cmd = ["codex", "exec", "--json", prompt]

    rc, out, err = _run_cmd(cmd, cwd=project_root)
    if rc != 0:
        raise RuntimeError(
            f"Stage {stage.key} failed with rc={rc}.\\n"
            f"Command: {' '.join(shlex.quote(x) for x in cmd)}\\n"
            f"stderr:\\n{err.strip()}"
        )

    thread_id, last_message, events = _parse_codex_jsonl(out)
    if not thread_id and session_id:
        thread_id = session_id
    if not last_message:
        last_message = ""
    status = _extract_status(last_message)
    return thread_id, out, err, status, events


def main() -> int:
    parser = argparse.ArgumentParser(description="Run staged multi-agent workflow via Codex CLI sessions")
    parser.add_argument("--project-root", default=".", help="Project root where codex commands run")
    parser.add_argument("--objective", default="", help="Objective text for the workflow")
    parser.add_argument("--objective-file", default="", help="Path to objective markdown/text file")
    parser.add_argument("--run-name", default="", help="Optional run name")
    parser.add_argument(
        "--output-dir",
        default="runtime/agent_runs",
        help="Directory to save sessions, logs, and stage outputs",
    )
    parser.add_argument(
        "--stages",
        default=",".join(s.key for s in DEFAULT_STAGES),
        help="Comma-separated stage keys to run",
    )
    parser.add_argument(
        "--stop-before-stage",
        default="",
        help="Hard stop before this stage key (example: dev).",
    )
    parser.add_argument(
        "--start-from",
        default="",
        help="Optional stage key to start from (applies after stage filtering)",
    )
    parser.add_argument(
        "--skip-pass-stages",
        action="store_true",
        default=True,
        help="Skip stages already marked PASS in existing summary (default: true)",
    )
    parser.add_argument(
        "--no-skip-pass-stages",
        dest="skip_pass_stages",
        action="store_false",
        help="Do not skip stages even if they already passed",
    )
    parser.add_argument(
        "--halt-on-fail",
        action="store_true",
        default=True,
        help="Stop workflow on stage Status FAIL/BLOCKED (default: true)",
    )
    parser.add_argument(
        "--no-halt-on-fail",
        dest="halt_on_fail",
        action="store_false",
        help="Continue even when a stage reports FAIL/BLOCKED",
    )
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"project root not found: {project_root}")

    objective = _load_text(args.objective_file or None, args.objective)
    if not objective:
        objective = "Design, validate, implement, and accept a multi-strategy execution engine workflow."

    run_name = _sanitize(args.run_name or f"agent_run_{_now_slug()}")
    run_dir = (project_root / args.output_dir / run_name).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    stage_keys = [x.strip() for x in args.stages.split(",") if x.strip()]
    stage_map = {s.key: s for s in DEFAULT_STAGES}
    stages: List[Stage] = []
    for k in stage_keys:
        if k not in stage_map:
            raise ValueError(f"Unknown stage key: {k}")
        stages.append(stage_map[k])

    _print(f"[orchestrator] project_root={project_root}")
    _print(f"[orchestrator] run_dir={run_dir}")
    _print(f"[orchestrator] stages={[s.key for s in stages]}")

    sessions_path = run_dir / "sessions.json"
    summary_path = run_dir / "summary.json"
    state_path = run_dir / "state.json"
    sessions = _load_json(sessions_path, {})
    if not isinstance(sessions, dict):
        sessions = {}

    existing_summary = _load_json(summary_path, {})
    if not isinstance(existing_summary, dict):
        existing_summary = {}
    existing_stages: List[dict] = existing_summary.get("stages", []) if isinstance(existing_summary.get("stages", []), list) else []
    existing_stage_map: Dict[str, dict] = {}
    for item in existing_stages:
        if isinstance(item, dict) and isinstance(item.get("stage"), str):
            existing_stage_map[item["stage"]] = item

    summary: Dict[str, object] = {
        "run_name": run_name,
        "project_root": str(project_root),
        "objective": objective,
        "stages": list(existing_stages),
        "created_at": existing_summary.get("created_at", dt.datetime.now().isoformat()),
        "updated_at": dt.datetime.now().isoformat(),
    }

    default_order = {s.key: idx for idx, s in enumerate(DEFAULT_STAGES, start=1)}

    if args.start_from:
        if args.start_from not in {s.key for s in stages}:
            raise ValueError(f"--start-from stage not selected: {args.start_from}")
        start_idx = next(i for i, s in enumerate(stages) if s.key == args.start_from)
        stages = stages[start_idx:]

    previous_stage_note = "(none)"
    for stage in stages:
        prev = existing_stage_map.get(stage.key)
        if not prev:
            continue
        msg_file = prev.get("message_file")
        status = prev.get("status")
        if isinstance(msg_file, str) and Path(msg_file).exists():
            msg = Path(msg_file).read_text(encoding="utf-8")
            previous_stage_note = (
                f"Stage: {stage.key}\\n"
                f"Status: {status or 'UNKNOWN'}\\n"
                f"Summary:\\n{msg[:4000]}"
            )

    for i, stage in enumerate(stages, start=1):
        if args.stop_before_stage and stage.key == args.stop_before_stage:
            _print(f"[orchestrator] hard stop before stage={stage.key}")
            break

        _print(f"[orchestrator] stage {i}/{len(stages)}: {stage.key}")
        ordinal = default_order.get(stage.key, i)
        stage_dir = run_dir / f"{ordinal:02d}_{stage.key}"
        stage_dir.mkdir(parents=True, exist_ok=True)

        prev_stage = existing_stage_map.get(stage.key)
        prev_status = str(prev_stage.get("status")) if isinstance(prev_stage, dict) else ""
        if args.skip_pass_stages and prev_status.upper() == "PASS":
            _print(f"[orchestrator] skip stage={stage.key} (already PASS)")
            msg_file = prev_stage.get("message_file")
            if isinstance(msg_file, str) and Path(msg_file).exists():
                msg = Path(msg_file).read_text(encoding="utf-8")
                previous_stage_note = (
                    f"Stage: {stage.key}\\n"
                    f"Status: PASS\\n"
                    f"Summary:\\n{msg[:4000]}"
                )
            continue

        session_id = sessions.get(stage.key)
        prompt = _build_prompt(
            stage=stage,
            objective=objective,
            project_root=project_root,
            artifact_dir=run_dir,
            previous_stage_note=previous_stage_note,
            run_name=run_name,
        )

        thread_id, stdout_jsonl, stderr_text, status, events = run_stage(
            stage=stage,
            session_id=session_id,
            prompt=prompt,
            project_root=project_root,
        )

        if thread_id:
            sessions[stage.key] = thread_id
            _write(sessions_path, json.dumps(sessions, indent=2, ensure_ascii=False) + "\n")

        _write(stage_dir / "prompt.txt", prompt)
        _write(stage_dir / "codex_events.jsonl", stdout_jsonl)
        _write(stage_dir / "stderr.txt", stderr_text or "")

        thread_id_from_events, last_message, _ = _parse_codex_jsonl(stdout_jsonl)
        stage_thread = thread_id or thread_id_from_events
        stage_message = last_message or ""
        if not status:
            status = _extract_status(stage_message)
        if not status:
            status = "BLOCKED"
            if stage_message.strip():
                stage_message = (
                    "Status: BLOCKED\n\n"
                    "Auto-gate: missing explicit `Status: PASS|FAIL|BLOCKED` in stage response.\n\n"
                    + stage_message
                )
            else:
                stage_message = (
                    "Status: BLOCKED\n\n"
                    "Auto-gate: empty stage response and missing explicit status."
                )
        _write(stage_dir / "agent_message.md", stage_message)

        stage_item = {
            "stage": stage.key,
            "role": stage.role,
            "session_id": stage_thread,
            "status": status,
            "message_file": str(stage_dir / "agent_message.md"),
            "events_file": str(stage_dir / "codex_events.jsonl"),
        }
        summary_stages: List[dict] = [x for x in summary["stages"] if isinstance(x, dict)]  # type: ignore[index]
        replaced = False
        for idx, old in enumerate(summary_stages):
            if old.get("stage") == stage.key:
                summary_stages[idx] = stage_item
                replaced = True
                break
        if not replaced:
            summary_stages.append(stage_item)
        summary["stages"] = summary_stages
        summary["updated_at"] = dt.datetime.now().isoformat()
        _write(summary_path, json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

        state = {
            "run_name": run_name,
            "current_stage": stage.key,
            "last_status": status,
            "updated_at": dt.datetime.now().isoformat(),
        }
        _write(state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")

        short_msg = (stage_message[:220] + "...") if len(stage_message) > 220 else stage_message
        _print(f"[orchestrator] stage={stage.key} status={status} session={stage_thread}")
        _print(f"[orchestrator] msg={short_msg.replace(os.linesep, ' ')}")

        previous_stage_note = (
            f"Stage: {stage.key}\\n"
            f"Status: {status}\\n"
            f"Summary:\\n{stage_message[:4000]}"
        )

        if args.halt_on_fail and status in {"FAIL", "BLOCKED"}:
            _print(f"[orchestrator] halted at stage={stage.key} due to status={status}")
            break

    _print(f"[orchestrator] done. summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
