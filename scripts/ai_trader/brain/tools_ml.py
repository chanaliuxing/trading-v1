from __future__ import annotations

"""
Brain tools for jobs/experiments inspection (host-side execution).

Functions:
 - list_recent_jobs(job_type: str | None, limit: int) -> list
 - get_job_details(job_id: str) -> dict
 - list_valid_experiment_ids(limit: int = 50) -> list

These are intentionally lightweight: they parse runs/ai_trader/*/ml_events.jsonl
and optional ml_experiments.jsonl to give the Brain a safe lookup surface
without hitting databases.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _iter_runs() -> List[Path]:
    base = Path("runs/ai_trader")
    if not base.exists():
        return []
    return sorted([p for p in base.iterdir() if p.is_dir()], reverse=True)


def list_recent_jobs(job_type: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for day_dir in _iter_runs():
        fp = day_dir / "ml_events.jsonl"
        if not fp.exists():
            continue
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if job_type and ev.get("job_type") != job_type:
                    continue
                if ev.get("event_type") not in ("job_started", "job_completed", "job_failed"):
                    continue
                out.append(
                    {
                        "job_id": ev.get("job_id"),
                        "job_type": ev.get("job_type"),
                        "status": ev.get("status") or ev.get("event_type"),
                        "timestamp": ev.get("ts") or ev.get("timestamp"),
                        "meta": ev,
                    }
                )
                if len(out) >= limit:
                    return out
    return out


def get_job_details(job_id: str) -> Dict[str, Any]:
    """
    Best-effort lookup of a job by id across runs/*/ml_events.jsonl.
    """
    res: Dict[str, Any] = {"job_id": job_id, "events": [], "artifacts": []}
    for day_dir in _iter_runs():
        fp = day_dir / "ml_events.jsonl"
        if not fp.exists():
            continue
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("job_id") == job_id:
                    res["events"].append(ev)
    # Add artifacts if exist
    art_dir = Path("runs") / job_id
    if art_dir.exists():
        artifacts = [str(p) for p in art_dir.glob("**/*") if p.is_file()]
        res["artifacts"] = artifacts
    res["ok"] = bool(res["events"])
    return res


def list_valid_experiment_ids(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Parse ml_experiments.jsonl if present across runs.
    """
    out: List[Dict[str, Any]] = []
    for day_dir in _iter_runs():
        fp = day_dir / "ml_experiments.jsonl"
        if not fp.exists():
            continue
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                exp_id = ev.get("experiment_id") or ev.get("id")
                if not exp_id:
                    continue
                out.append(
                    {
                        "experiment_id": exp_id,
                        "status": ev.get("status"),
                        "ts": ev.get("ts") or ev.get("timestamp"),
                        "meta": ev,
                    }
                )
                if len(out) >= limit:
                    return out
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Brain tools for jobs/experiments")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List recent jobs")
    p_list.add_argument("--job-type", default=None, help="Filter by job_type")
    p_list.add_argument("--limit", type=int, default=20)

    p_detail = sub.add_parser("detail", help="Get job details")
    p_detail.add_argument("job_id")

    p_exp = sub.add_parser("experiments", help="List valid experiment ids")
    p_exp.add_argument("--limit", type=int, default=50)

    args = ap.parse_args()
    if args.cmd == "list":
        print(json.dumps(list_recent_jobs(args.job_type, args.limit), indent=2, ensure_ascii=False))
    elif args.cmd == "detail":
        print(json.dumps(get_job_details(args.job_id), indent=2, ensure_ascii=False))
    elif args.cmd == "experiments":
        print(json.dumps(list_valid_experiment_ids(args.limit), indent=2, ensure_ascii=False))
