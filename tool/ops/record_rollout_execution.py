#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ET = ZoneInfo("America/New_York")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _norm_list(values: list[str] | None) -> list[str]:
    return [str(value).strip() for value in (values or []) if str(value).strip()]


def _detect_git_commit() -> dict[str, Any]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        subject = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if sha:
            return {"sha": sha, "summary": subject or None}
    except Exception:
        pass
    return {}


def record_rollout_execution(
    *,
    state_path: str | Path | None = None,
    output_path: str | Path | None = None,
    task_id: str,
    status: str,
    summary: str,
    blockers: list[str] | None = None,
    commit_sha: str = "",
    commit_summary: str = "",
) -> dict[str, Any]:
    state_file = Path(state_path) if state_path is not None else REPO_ROOT / "runtime/state/trading_rollout_orchestrator_state.json"
    if not state_file.is_absolute():
        state_file = REPO_ROOT / state_file
    output_file = Path(output_path) if output_path is not None else state_file
    if not output_file.is_absolute():
        output_file = REPO_ROOT / output_file

    payload = _read_json(state_file)
    now = datetime.now(ET).isoformat(timespec="seconds")
    current_task = dict(payload.get("current_priority_task") or {})
    execution = {
        "recorded_at": now,
        "task_id": str(task_id).strip(),
        "status": str(status).strip().lower(),
        "summary": str(summary).strip(),
        "blockers": _norm_list(blockers),
    }
    commit = {}
    if str(commit_sha or "").strip():
        commit["sha"] = str(commit_sha).strip()
    if str(commit_summary or "").strip():
        commit["summary"] = str(commit_summary).strip()
    if not commit and execution["status"] == "success":
        commit = _detect_git_commit()
    if commit:
        execution["commit"] = commit
    history = list(payload.get("execution_history") or [])
    history.append(execution)
    history = history[-50:]

    task_attempts = dict(payload.get("task_attempts") or {})
    entry = dict(task_attempts.get(execution["task_id"]) or {})
    entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
    entry["last_status"] = execution["status"]
    entry["last_summary"] = execution["summary"]
    entry["last_recorded_at"] = now
    if execution.get("commit"):
        entry["last_commit"] = dict(execution["commit"])
    task_attempts[execution["task_id"]] = entry

    payload["updated_at"] = now
    payload["last_execution"] = execution
    payload["execution_history"] = history
    payload["task_attempts"] = task_attempts
    if execution["status"] == "success":
        payload["last_completed_task"] = {
            "task_id": execution["task_id"],
            "summary": execution["summary"],
            "recorded_at": now,
        }
        if execution.get("commit"):
            payload["last_completed_task"]["commit"] = dict(execution["commit"])
    if current_task and str(current_task.get("id") or "").strip() == execution["task_id"]:
        payload["current_priority_task"]["last_attempt_at"] = now
        payload["current_priority_task"]["last_attempt_status"] = execution["status"]
        payload["current_priority_task"]["last_attempt_summary"] = execution["summary"]

    _write_json(output_file, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record the result of an automated rollout task execution")
    parser.add_argument("--state", default="runtime/state/trading_rollout_orchestrator_state.json")
    parser.add_argument("--output", default="")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--status", required=True, choices=("success", "blocked", "failed", "no_action"))
    parser.add_argument("--summary", required=True)
    parser.add_argument("--blocker", action="append", default=[])
    parser.add_argument("--commit-sha", default="")
    parser.add_argument("--commit-summary", default="")
    args = parser.parse_args(argv)
    payload = record_rollout_execution(
        state_path=args.state,
        output_path=args.output or None,
        task_id=args.task_id,
        status=args.status,
        summary=args.summary,
        blockers=args.blocker,
        commit_sha=args.commit_sha,
        commit_summary=args.commit_summary,
    )
    print(
        json.dumps(
            {
                "task_id": payload.get("last_execution", {}).get("task_id"),
                "status": payload.get("last_execution", {}).get("status"),
                "attempt_count": dict(payload.get("task_attempts") or {}).get(args.task_id, {}).get("attempt_count"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
