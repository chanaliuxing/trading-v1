#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Brain Self-Check Report
-----------------------------

Summarise, for recent days (default: yesterday):
  - What config fixes (fix_config) the AI Brain executed.
  - How the latest HPO/alpha_loop metrics (Sharpe/IC) compare to the
    previous run.

Usage:
    python tools/brain_daily_report.py
    python tools/brain_daily_report.py --days 3 --runs-dir runs/ai_trader --output reports/brain_daily_report.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Daily Brain self-check report")
    ap.add_argument(
        "--days",
        type=int,
        default=2,
        help="Number of recent days to analyse (including today). Default: 2",
    )
    ap.add_argument(
        "--runs-dir",
        default="runs/ai_trader",
        help="Root runs directory (default: runs/ai_trader)",
    )
    ap.add_argument(
        "--output",
        default=None,
        help="Optional JSON output path (default: print to stdout)",
    )
    return ap.parse_args()


@dataclass
class JobMetrics:
    job_id: str
    job_type: str
    ts: datetime
    sharpe: Optional[float]
    sortino: Optional[float]
    mdd: Optional[float]
    ic: Optional[float]
    raw_metrics: Dict[str, Any]


def _load_events(days: int, runs_dir: Path) -> List[Dict[str, Any]]:
    """Load completed ML events for recent N days."""
    out: List[Dict[str, Any]] = []
    end_date = date.today()

    for offset in range(days):
        d = end_date - timedelta(days=offset)
        fp = runs_dir / d.isoformat() / "ml_events.jsonl"
        if not fp.exists():
            continue
        with fp.open("r", encoding="utf-8") as f:
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
                ts = evt.get("ts")
                if not ts:
                    continue
                try:
                    evt["_ts_parsed"] = datetime.fromisoformat(ts)
                except Exception:
                    continue
                out.append(evt)

    out.sort(key=lambda e: e["_ts_parsed"])
    return out


def _load_actions(days: int, runs_dir: Path) -> List[Dict[str, Any]]:
    """Load executed ML actions (Brain actions) for recent N days."""
    out: List[Dict[str, Any]] = []
    end_date = date.today()

    for offset in range(days):
        d = end_date - timedelta(days=offset)
        fp = runs_dir / d.isoformat() / "ml_actions.jsonl"
        if not fp.exists():
            continue
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    act = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Normalised fields: ts / executed_at
                ts = act.get("ts")
                executed = act.get("executed_at")
                if not ts or not executed:
                    continue
                try:
                    act["_ts_parsed"] = datetime.fromisoformat(ts)
                    act["_executed_at_parsed"] = datetime.fromisoformat(executed)
                except Exception:
                    continue
                out.append(act)

    out.sort(key=lambda a: a["_executed_at_parsed"])
    return out


def _extract_job_metrics(evt: Dict[str, Any]) -> JobMetrics:
    """Best-effort extraction of Sharpe/Sortino/MDD/IC from a job event."""
    metrics = evt.get("metrics") or {}
    job_id = str(evt.get("job_id") or "")
    job_type = str(evt.get("job_type") or "")
    ts = evt.get("_ts_parsed") or datetime.fromisoformat(evt["ts"])

    # Sharpe: try OOS keys / HPO keys first, then generic
    sharpe = None
    for k in ("oos_avg_sharpe", "sharpe"):
        if k in metrics and isinstance(metrics[k], (int, float)):
            sharpe = float(metrics[k])
            break

    # Sortino: prefer OOS/HPO-style keys
    sortino = None
    for k in ("oos_avg_sortino", "sortino"):
        if k in metrics and isinstance(metrics[k], (int, float)):
            sortino = float(metrics[k])
            break

    # MDD (Maximum Drawdown): support multiple key conventions
    mdd = None
    for k in ("oos_avg_max_drawdown", "mdd", "max_drawdown"):
        if k in metrics and isinstance(metrics[k], (int, float)):
            mdd = float(metrics[k])
            break

    # IC: try fusion_ic (OOF) first, then OOS IC
    ic = None
    for k in ("fusion_ic", "oos_avg_ic", "ic"):
        if k in metrics and isinstance(metrics[k], (int, float)):
            ic = float(metrics[k])
            break

    return JobMetrics(
        job_id=job_id,
        job_type=job_type,
        ts=ts,
        sharpe=sharpe,
        sortino=sortino,
        mdd=mdd,
        ic=ic,
        raw_metrics=metrics,
    )


def _find_last_two_alpha_jobs(events: List[Dict[str, Any]]) -> Tuple[Optional[JobMetrics], Optional[JobMetrics]]:
    """Return (prev, last) for alpha_loop / hpo_cli / oof jobs."""
    # Filter to relevant job types
    relevant = [
        e for e in events
        if str(e.get("job_type")) in {"alpha_loop", "hpo_cli", "oof"}
        and e.get("status") == "completed"
    ]
    if len(relevant) < 2:
        return None, None
    last_two = relevant[-2:]
    prev = _extract_job_metrics(last_two[0])
    last = _extract_job_metrics(last_two[1])
    return prev, last


def _collect_fix_config_actions(actions: List[Dict[str, Any]], target_date: date) -> List[Dict[str, Any]]:
    """Collect fix_config actions executed on target_date."""
    out: List[Dict[str, Any]] = []
    for act in actions:
        if act.get("action") != "fix_config":
            continue
        executed = act.get("_executed_at_parsed")
        if not isinstance(executed, datetime):
            continue
        if executed.date() != target_date:
            continue
        params = act.get("params") or {}
        out.append(
            {
                "ts": executed.isoformat(),
                "path": params.get("path"),
                "key": params.get("key"),
                "value": params.get("value"),
                "job_id": act.get("job_id"),
            }
        )
    # Sort by time for readability
    out.sort(key=lambda x: x["ts"])
    return out


def _build_report(args: argparse.Namespace) -> Dict[str, Any]:
    runs_dir = Path(args.runs_dir)
    events = _load_events(args.days, runs_dir)
    actions = _load_actions(args.days, runs_dir)

    today = date.today()
    target_date = today - timedelta(days=1)

    prev_job, last_job = _find_last_two_alpha_jobs(events)
    fixes = _collect_fix_config_actions(actions, target_date)

    delta_sharpe: Optional[float] = None
    delta_ic: Optional[float] = None
    delta_sortino: Optional[float] = None
    delta_mdd: Optional[float] = None

    if prev_job and last_job:
        if prev_job.sharpe is not None and last_job.sharpe is not None:
            delta_sharpe = last_job.sharpe - prev_job.sharpe
        if prev_job.ic is not None and last_job.ic is not None:
            delta_ic = last_job.ic - prev_job.ic
        if prev_job.sortino is not None and last_job.sortino is not None:
            delta_sortino = last_job.sortino - prev_job.sortino
        if prev_job.mdd is not None and last_job.mdd is not None:
            delta_mdd = last_job.mdd - prev_job.mdd

    summary_lines: List[str] = []
    summary_lines.append(f"Brain Daily Report for {target_date.isoformat()}")
    if last_job:
        parts = [
            f"- Last job: {last_job.job_type} id={last_job.job_id} ts={last_job.ts.isoformat()}",
            f"Sharpe={last_job.sharpe}",
        ]
        if last_job.sortino is not None:
            parts.append(f"Sortino={last_job.sortino}")
        if last_job.mdd is not None:
            parts.append(f"MDD={last_job.mdd}")
        parts.append(f"IC={last_job.ic}")
        summary_lines.append(" ".join(parts))
    if prev_job:
        parts = [
            f"- Prev job: {prev_job.job_type} id={prev_job.job_id} ts={prev_job.ts.isoformat()}",
            f"Sharpe={prev_job.sharpe}",
        ]
        if prev_job.sortino is not None:
            parts.append(f"Sortino={prev_job.sortino}")
        if prev_job.mdd is not None:
            parts.append(f"MDD={prev_job.mdd}")
        parts.append(f"IC={prev_job.ic}")
        summary_lines.append(" ".join(parts))
    if any(v is not None for v in (delta_sharpe, delta_sortino, delta_mdd, delta_ic)):
        summary_lines.append(
            f"- Delta vs prev: "
            f"ΔSharpe={delta_sharpe} "
            f"ΔSortino={delta_sortino} "
            f"ΔMDD={delta_mdd} "
            f"ΔIC={delta_ic}"
        )
    if fixes:
        summary_lines.append(f"- fix_config actions on {target_date.isoformat()}: {len(fixes)} change(s)")
    else:
        summary_lines.append(f"- fix_config actions on {target_date.isoformat()}: none")

    def _job_to_dict(job: JobMetrics) -> Dict[str, Any]:
        return {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "ts": job.ts.isoformat() if isinstance(job.ts, datetime) else job.ts,
            "sharpe": job.sharpe,
            "sortino": job.sortino,
            "mdd": job.mdd,
            "ic": job.ic,
            "raw_metrics": job.raw_metrics,
        }

    report: Dict[str, Any] = {
        "analysis_date": datetime.now().isoformat(),
        "target_date": target_date.isoformat(),
        "days_lookback": args.days,
        "summary_text": "\n".join(summary_lines),
        "jobs": {
            "prev": _job_to_dict(prev_job) if prev_job else None,
            "last": _job_to_dict(last_job) if last_job else None,
            "delta": {
                "delta_sharpe": delta_sharpe,
                "delta_sortino": delta_sortino,
                "delta_mdd": delta_mdd,
                "delta_ic": delta_ic,
            },
        },
        "fix_config_actions": fixes,
        "raw": {
            "events_count": len(events),
            "actions_count": len(actions),
        },
    }
    return report


def main() -> None:
    args = _parse_args()
    report = _build_report(args)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Brain daily report written to {out_path}")
    else:
        print(report["summary_text"])
        print()
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
