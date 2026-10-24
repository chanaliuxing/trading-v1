#!/usr/bin/env python3
from __future__ import annotations
"""
One-click N-cycle ML drill

Runs N review→execute cycles to exercise the Brain→Validator→Scheduler loop.

Cycle definition:
 1) Simulate a completed ML job event (default type: 'oof') to trigger Brain.review_ml
 2) If Brain enqueues a job, run exactly one job from the scheduler queue
 3) Publish a follow-up completion to Brain (direct call) to close the loop

This avoids requiring a live Kafka or a separately running scheduler process.

Usage:
  python tools/run_ml_drill_cycles.py --cycles 3 --symbols TSLA AAPL

Notes:
  - For realistic outcomes, ensure runs/ai_trader/<today>/oof_eval/*.json exists for target symbols.
  - The script prints a compact per-cycle summary and hints for final ClickHouse checks.
"""
import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


async def _trigger_brain_review(event: Dict[str, Any]) -> None:
    from scripts.ai_trader.control_event_consumer import _handle_ml_completed
    await _handle_ml_completed(event)


def _read_actions_tail(limit: int = 5) -> List[Dict[str, Any]]:
    # Best-effort read audit tail
    day = datetime.now().date().isoformat()
    p = Path("runs/ai_trader") / day / "ml_actions.jsonl"
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        tail = lines[-limit:]
        out = []
        for ln in tail:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


async def _run_one_queued_job() -> Dict[str, Any] | None:
    """Run exactly one highest-priority scheduler job and return its result."""
    from scripts.ai_trader.ml_scheduler import load_queue, execute_job
    jobs = load_queue()
    if not jobs:
        return None
    job = jobs[0]
    # Set running status quickly (optional), execute_job will mark completed
    try:
        job["status"] = "running"
        job["running_at"] = datetime.now().isoformat()
        # Rewrite queue minimally (best-effort)
        qf = Path("runs/ai_trader/queue/ml_jobs.jsonl")
        try:
            existing: List[Dict[str, Any]] = []
            if qf.exists():
                with qf.open("r") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            existing.append(json.loads(ln))
                        except Exception:
                            continue
            # Replace the first occurrence by job_id
            out: List[Dict[str, Any]] = []
            for j in existing:
                if j.get("job_id") == job.get("job_id"):
                    out.append(job)
                else:
                    out.append(j)
            if not existing:
                out = [job]
            with qf.open("w") as f:
                for j in out:
                    f.write(json.dumps(j, ensure_ascii=False) + "\n")
        except Exception:
            pass
    except Exception:
        pass

    res = await execute_job(job)
    return res


async def main() -> int:
    ap = argparse.ArgumentParser(description="N-cycle ML drill runner")
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--symbols", nargs="*", default=None, help="Optional symbols hint (informs Brain via context if used downstream)")
    ap.add_argument("--start-job-type", default="oof", choices=["oof","alpha_loop","feature_build","feature_eval"], help="Initial synthetic completion event type per cycle")
    args = ap.parse_args()

    N = max(1, int(args.cycles))
    print(f"[Drill] Starting {N} cycles at {datetime.now().isoformat()}\n")

    final_results: List[Dict[str, Any]] = []
    for i in range(1, N + 1):
        job_id = f"drill-{_now_ts()}-{i:02d}"
        print(f"[Cycle {i}/{N}] Trigger Brain review → plan actions…")

        # 1) Trigger Brain review with a synthetic completion event (best-effort)
        evt = {
            "job_id": job_id,
            "job_type": args.start_job_type,
            "ok": False,
            "duration_sec": 0,
            # Optionally include symbols hint in metrics (not strictly needed)
            "metrics": {"symbols_hint": args.symbols or []},
        }
        await _trigger_brain_review(evt)

        # 2) Run exactly one queued ML job (if any)
        res = await _run_one_queued_job()
        if res is None:
            print("  - No job enqueued by Brain (idle cycle)")
            acts_tail = _read_actions_tail(limit=3)
            if acts_tail:
                print("  - Recent actions tail:")
                for a in acts_tail:
                    print(f"    • {a.get('action')} status={a.get('status')} reason={a.get('reason','')} params={a.get('params',{})}")
            final_results.append({"cycle": i, "ran_job": False})
            continue

        # 3) Publish follow-up review from job completion (direct call)
        comp_evt = {
            "job_id": job_id,
            "job_type": res.get("job_type", "alpha_loop"),
            "ok": bool(res.get("ok", False)),
            "duration_sec": int(res.get("duration_sec", 0)),
            "metrics": res.get("metrics", {}),
            "artifacts": res.get("artifacts", []),
        }
        print(f"  - Ran job: ok={comp_evt['ok']} type={comp_evt['job_type']} metrics_keys={list(comp_evt['metrics'].keys())}")
        await _trigger_brain_review(comp_evt)

        # Optional: brief pause to avoid file races
        await asyncio.sleep(1.0)

        final_results.append({
            "cycle": i,
            "ran_job": True,
            "ok": comp_evt["ok"],
            "metrics_keys": list(comp_evt["metrics"].keys()),
        })

    print("\n[Drill] Completed. Summary:")
    for r in final_results:
        if r.get("ran_job"):
            print(f"  • Cycle {r['cycle']}: ran_job ok={r.get('ok')} metrics_keys={r.get('metrics_keys')}")
        else:
            print(f"  • Cycle {r['cycle']}: no job enqueued")

    print("\nNext:")
    print("  - Inspect actions: runs/ai_trader/<today>/ml_actions.jsonl")
    print("  - Inspect review: runs/ai_trader/<today>/ml_review.json")
    print("  - If ClickHouse is configured, query trading.ml_eval_with_delta for gates and deltas")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

