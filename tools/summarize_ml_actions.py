#!/usr/bin/env python3
"""
Summarize ML actions effectiveness for AI Brain.

Reads ml_actions.jsonl and ml_events.jsonl for recent days and produces
an aggregated JSON report by stage / branch_id / action.

This is an offline tool and does not change any runtime behaviour. It is
intended to give the AI Brain (and humans) a compact view of:
  - how often different stages/branches/actions were executed
  - how often the next OOF/HPO job succeeded
  - average fusion_ic / fusion_auc for jobs following those actions
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="Number of recent days to analyse")
    ap.add_argument("--runs-dir", default="runs/ai_trader", help="Root runs directory")
    ap.add_argument("--output", default=None, help="Output JSON path (default: print to stdout)")
    ap.add_argument("--min-gap-min", type=float, default=0.0, help="Minimum minutes between action and next job")
    ap.add_argument("--max-gap-hours", type=float, default=6.0, help="Maximum hours between action and next job")
    return ap.parse_args()


def _load_events(days: int, runs_dir: Path) -> List[Dict[str, Any]]:
    """Load completed ML events for recent N days."""
    events: List[Dict[str, Any]] = []
    end_date = datetime.now().date()

    for day_offset in range(days + 1):
        d = end_date - timedelta(days=day_offset)
        events_file = runs_dir / str(d) / "ml_events.jsonl"
        if not events_file.exists():
            continue
        with events_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("status") != "completed":
                    continue
                # We primarily care about OOF / HPO-like jobs
                job_type = evt.get("job_type")
                if job_type not in {"oof", "hpo_cli", "alpha_loop", "ensemble_sweep"}:
                    continue
                ts = evt.get("ts")
                if not ts:
                    continue
                try:
                    evt["_ts_parsed"] = datetime.fromisoformat(ts)
                    events.append(evt)
                except Exception:
                    continue

    events.sort(key=lambda e: e["_ts_parsed"])
    return events


def _load_actions(days: int, runs_dir: Path) -> List[Dict[str, Any]]:
    """Load executed ML actions for recent N days."""
    actions: List[Dict[str, Any]] = []
    end_date = datetime.now().date()

    for day_offset in range(days + 1):
        d = end_date - timedelta(days=day_offset)
        actions_file = runs_dir / str(d) / "ml_actions.jsonl"
        if not actions_file.exists():
            continue
        with actions_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    act = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not act.get("executed_at"):
                    continue
                try:
                    act["_ts_parsed"] = datetime.fromisoformat(act["ts"])
                    act["_executed_at_parsed"] = datetime.fromisoformat(act["executed_at"])
                    actions.append(act)
                except Exception:
                    continue

    actions.sort(key=lambda a: a["_executed_at_parsed"])
    return actions


def _classify_job(evt: Dict[str, Any]) -> Dict[str, Any]:
    """Classify job outcome based on IC/AUC metrics."""
    ok = bool(evt.get("ok", False))
    metrics = evt.get("metrics", {}) or {}
    ic = metrics.get("fusion_ic")
    auc = metrics.get("fusion_auc")
    n_bars = metrics.get("n_bars")

    # Classification
    if ok:
        quality = "success"
    elif ic is None:
        quality = "data_corruption"
    elif isinstance(ic, float) and (abs(ic) != ic or ic != ic):  # NaN or Inf
        quality = "ic_nan"
    elif isinstance(ic, (int, float)) and abs(ic) < 0.001:
        quality = "no_signal"
    elif isinstance(ic, (int, float)) and ic < 0:
        quality = "inverse_signal"
    elif isinstance(ic, (int, float)) and 0 < ic <= 0.02:
        quality = "weak_signal"
    else:
        quality = "low_auc"

    return {
        "ok": ok,
        "quality": quality,
        "ic": ic,
        "auc": auc,
        "n_bars": n_bars,
        "ts": evt["_ts_parsed"],
    }


def _match_actions_to_jobs(
    actions: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    min_gap_min: float,
    max_gap_hours: float,
) -> List[Dict[str, Any]]:
    """Match each executed action to the next ML job within a time window."""
    matched: List[Dict[str, Any]] = []
    min_gap = timedelta(minutes=min_gap_min)
    max_gap = timedelta(hours=max_gap_hours)

    for act in actions:
        act_time = act["_executed_at_parsed"]
        # Find jobs strictly after this action
        candidates = [e for e in events if e["_ts_parsed"] > act_time]
        if not candidates:
            continue
        next_job = candidates[0]
        gap = next_job["_ts_parsed"] - act_time
        if gap < min_gap or gap > max_gap:
            continue
        job_result = _classify_job(next_job)
        matched.append(
            {
                "action": act.get("action"),
                "params": act.get("params", {}),
                "stage": act.get("stage"),
                "branch_id": act.get("branch_id"),
                "triggered_by": act.get("job_id"),
                "executed_at": act_time.isoformat(),
                "next_job_time": next_job["_ts_parsed"].isoformat(),
                "time_diff_min": gap.total_seconds() / 60.0,
                "next_job_result": job_result,
            }
        )

    return matched


def _aggregate(matched: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate stats by stage / branch_id / action."""
    def _ensure_bucket(container: Dict[str, Dict[str, Any]], key: str) -> Dict[str, Any]:
        if key not in container:
            container[key] = {
                "actions": 0,
                "matched_jobs": 0,
                "ok_jobs": 0,
                "ic_sum": 0.0,
                "ic_count": 0,
                "auc_sum": 0.0,
                "auc_count": 0,
                "qualities": defaultdict(int),
            }
        return container[key]

    by_stage: Dict[str, Dict[str, Any]] = {}
    by_branch: Dict[str, Dict[str, Any]] = {}
    by_action: Dict[str, Dict[str, Any]] = {}

    for m in matched:
        stage = (m.get("stage") or "unknown").lower()
        branch = m.get("branch_id") or "unknown"
        action = m.get("action") or "unknown"
        res = m["next_job_result"]

        for container, key in (
            (by_stage, stage),
            (by_branch, f"{stage}::{branch}"),
            (by_action, action),
        ):
            bucket = _ensure_bucket(container, key)
            bucket["actions"] += 1
            bucket["matched_jobs"] += 1
            if res["ok"]:
                bucket["ok_jobs"] += 1
            ic = res.get("ic")
            if isinstance(ic, (int, float)) and not (abs(ic) != ic or ic != ic):
                bucket["ic_sum"] += float(ic)
                bucket["ic_count"] += 1
            auc = res.get("auc")
            if isinstance(auc, (int, float)):
                bucket["auc_sum"] += float(auc)
                bucket["auc_count"] += 1
            q = res.get("quality") or "unknown"
            bucket["qualities"][q] += 1

    def _finalize(container: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, b in container.items():
            ic_avg = b["ic_sum"] / b["ic_count"] if b["ic_count"] > 0 else None
            auc_avg = b["auc_sum"] / b["auc_count"] if b["auc_count"] > 0 else None
            total = max(b["matched_jobs"], 1)
            ok_rate = b["ok_jobs"] / total
            out[key] = {
                "actions": b["actions"],
                "matched_jobs": b["matched_jobs"],
                "ok_jobs": b["ok_jobs"],
                "ok_rate": ok_rate,
                "avg_ic": ic_avg,
                "avg_auc": auc_avg,
                "qualities": dict(b["qualities"]),
            }
        return out

    return {
        "by_stage": _finalize(by_stage),
        "by_branch": _finalize(by_branch),
        "by_action": _finalize(by_action),
    }


def main() -> None:
    args = _parse_args()
    runs_dir = Path(args.runs_dir)

    events = _load_events(args.days, runs_dir)
    actions = _load_actions(args.days, runs_dir)
    matched = _match_actions_to_jobs(actions, events, args.min_gap_min, args.max_gap_hours)
    aggregates = _aggregate(matched)

    report: Dict[str, Any] = {
        "analysis_date": datetime.now().isoformat(),
        "days": args.days,
        "total_events": len(events),
        "total_actions": len(actions),
        "matched_pairs": len(matched),
        "aggregates": aggregates,
    }

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Report written to {out_path}")
    else:
        # Print to stdout
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

