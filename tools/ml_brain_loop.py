#!/usr/bin/env python3
from __future__ import annotations
"""
ML Brain Continuous Loop
------------------------

Run the AI Brain + ML scheduler loop continuously WITHOUT 触发交易序列：
- 不启动 auto_loop / Orchestrator session（不跑 morning/intraday/afterhours）。
- 只走 ML 路径：review_ml → Brain 决策 → enqueue ML job → 执行 1 个 job → 再 review_ml。

适用场景：
- 临近开盘 / 交易时间，不想跑真实交易，只想让大脑一直练模型、调数据/特征/HPO。

Usage:
  # 建议先停止 auto_loop / 交易流程
  #   venv/bin/python scripts/ai_trader/stop_brain.py
  #
  # 然后单独跑 ML Brain Loop（只做 OOF/HPO/特征实验，不下单）：
  #   ENV_MODE=paper venv/bin/python tools/ml_brain_loop.py \\
  #       --symbols TSLA AAPL \\
  #       --start-job-type oof \\
  #       --sleep-sec 10
"""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import sys
import os as _os
import subprocess

from tools.training_gates import enforce_training_gates

# Ensure project root is on sys.path so that "scripts.*" imports work
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Mark this process as training-only for control_event_consumer gating logic
_os.environ.setdefault("ML_BRAIN_LOOP", "1")


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


async def _trigger_brain_review(event: Dict[str, Any]) -> None:
    """Call the same completion handler used by Kafka consumer, but directly."""
    from scripts.ai_trader.control_event_consumer import _handle_ml_completed
    await _handle_ml_completed(event)


def _read_actions_tail(limit: int = 5) -> List[Dict[str, Any]]:
    """Best-effort read ml_actions.jsonl tail for debugging."""
    day = datetime.now().date().isoformat()
    p = Path("runs/ai_trader") / day / "ml_actions.jsonl"
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        tail = lines[-limit:]
        out: List[Dict[str, Any]] = []
        for ln in tail:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


async def _run_one_queued_job() -> Optional[Dict[str, Any]]:
    """Run exactly one highest-priority scheduler job and return its result."""
    from scripts.ai_trader.ml_scheduler import load_queue, execute_job

    jobs = load_queue()
    if not jobs:
        return None
    job = jobs[0]

    # Mark running in queue file (best-effort, not critical)
    try:
        job["status"] = "running"
        job["running_at"] = datetime.now().isoformat()
        qf = Path("runs/ai_trader/queue/ml_jobs.jsonl")
        if qf.exists():
            existing: List[Dict[str, Any]] = []
            with qf.open("r") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        existing.append(json.loads(ln))
                    except Exception:
                        continue
            out: List[Dict[str, Any]] = []
            for j in existing:
                if j.get("job_id") == job.get("job_id"):
                    out.append(job)
                else:
                    out.append(j)
            with qf.open("w") as f:
                for j in out:
                    f.write(json.dumps(j, ensure_ascii=False) + "\n")
    except Exception:
        pass

    res = await execute_job(job)
    return res


async def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous ML Brain loop (no trading sequences)")
    ap.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Optional symbols hint for Brain (e.g., TSLA AAPL)",
    )
    ap.add_argument(
        "--start-job-type",
        default="oof",
        choices=["oof", "alpha_loop", "feature_build", "feature_eval"],
        help="Synthetic completion event type per cycle (default: oof)",
    )
    ap.add_argument(
        "--sleep-sec",
        type=float,
        default=10.0,
        help="Sleep between cycles (default 10s)",
    )
    ap.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Optional max cycles for testing (0 = infinite)",
    )
    args = ap.parse_args()

    max_cycles = int(args.max_cycles or 0)
    cycles_run = 0
    # Exponential backoff state for repeated failures
    error_backoff_sec = 5.0
    error_backoff_max = 60.0
    consecutive_errors = 0

    print(f"[ML Brain Loop] Starting at {datetime.now().isoformat()}")
    print(f"[ML Brain Loop] start_job_type={args.start_job_type} symbols={args.symbols or []}")

    # Unified gates (data quality + labels) with auto-heal + retry
    if args.symbols:
        gate = enforce_training_gates(
            symbols=args.symbols,
            horizons=[30],
            window_days=30,
            features_table="trading.features_1m_active",
            labels_table="trading.features_1m_with_labels",
            data_quality_days=3,
            auto_heal=True,
            heal_lookback_days=2,
        )
        if not gate.get("ready"):
            print(f"[ML Brain Loop] abort: training gates blocked: {gate}")
            return 1

    # One-time data self-healing pass so that OOF/HPO jobs do not run
    # on completely empty features_1m tables when symbols are provided.
    try:
        import os
        from datetime import datetime as _dt
        from libs.utils.market_calendar import is_market_closed
        from libs.utils.market_calendar import previous_trading_day
        from libs.db.clickhouse import ClickHouseDB
        if args.symbols:
            if is_market_closed():
                print("[ML Brain Loop] Holiday/weekend detected; skipping startup backfill/rebuild.")
            else:
                # Skip if already up to previous trading day
                skip_backfill = False
                try:
                    prev_td = previous_trading_day()
                    ch = ClickHouseDB()
                    up_to_date = 0
                    for sym in args.symbols:
                        res = ch.query("SELECT max(ts_start) as m FROM trading.bars_1m_ibkr WHERE symbol=%(s)s", {"s": sym})
                        if res and res[0].get("m"):
                            if res[0]["m"].date() >= prev_td:
                                up_to_date += 1
                    if up_to_date == len(args.symbols):
                        skip_backfill = True
                        print("[ML Brain Loop] Bars already up to previous trading day; skipping startup backfill/rebuild.")
                except Exception:
                    skip_backfill = False

                if not skip_backfill:
                    from libs.healer.data_healer_v2 import DataHealerV2
                    from libs.health import Action

                    runs_dir = Path("runs") / "ai_trader"
                    healer = DataHealerV2(str(runs_dir))
                    
                    print("[ML Brain Loop] Running one-time data self-healing for bars...")
                    backfill_action = Action(
                        action_id="ml_loop_startup_backfill",
                        action_type="backfill_bars",
                        priority=1,
                        title="Startup Backfill",
                        description="One-time data self-healing for bars at startup.",
                        params={"symbols": args.symbols, "days": 2},
                        is_safe_auto_execute=True,
                        requires_approval=False
                    )
                    backfill_result = healer.heal(backfill_action)

                    if not backfill_result.success:
                         print(f"[ML Brain Loop] WARNING: Backfill bars failed: {backfill_result.error}")

                    print("[ML Brain Loop] Running one-time data self-healing for features...")
                    rebuild_action = Action(
                        action_id="ml_loop_startup_rebuild",
                        action_type="rebuild_features",
                        priority=1,
                        title="Startup Feature Rebuild",
                        description="One-time data self-healing for features at startup.",
                        params={"symbols": args.symbols, "lookback_days": 2},
                        is_safe_auto_execute=True,
                        requires_approval=False
                    )
                    rebuild_result = healer.heal(rebuild_action)

                    if not rebuild_result.success:
                        print(
                            "[ML Brain Loop] WARNING: DataHealer could not fully repair "
                            f"feature coverage; ML jobs may still see data gaps. Error: {rebuild_result.error}"
                        )
    except Exception as e:
        print(f"[ML Brain Loop] DataHealer init/check failed: {e}")

    while True:
        cycles_run += 1
        print(f"\n[Cycle {cycles_run}] Trigger Brain review → maybe run 1 ML job…")

        try:
            # 1) Trigger Brain review with a synthetic completion event
            job_id = f"mlbrain-{_now_ts()}-{cycles_run:05d}"
            evt = {
                "job_id": job_id,
                "job_type": args.start_job_type,
                "ok": False,
                "duration_sec": 0,
                "metrics": {"symbols_hint": args.symbols or []},
            }
            await _trigger_brain_review(evt)

            # 2) Run exactly one queued ML job (if any)
            res = await _run_one_queued_job()
            # If Brain chose only 'wait'/noop and no job was queued, optionally
            # enqueue a fallback ML job so that training still makes progress in
            # training-only mode.
            if res is None:
                from scripts.ai_trader.ml_scheduler import enqueue_job

                fallback_params: Dict[str, Any] = {}
                # Use symbols hint when provided
                if args.symbols:
                    fallback_params["symbols"] = args.symbols

                if args.start_job_type == "oof":
                    fallback_params.setdefault("window_days", 30)
                    fallback_params.setdefault("mode", "light")
                elif args.start_job_type == "alpha_loop":
                    # Lightweight alpha_loop defaults for training mode
                    fallback_params.setdefault("days", 30)
                    fallback_params.setdefault("samples", 40)
                    fallback_params.setdefault("timeout_hours", 1.0)

                # For feature_build/feature_eval we rely on their internal defaults
                try:
                    fb_job_id = enqueue_job(
                        {
                            "job_type": args.start_job_type,
                            "params": fallback_params,
                        }
                    )
                    print(
                        "  - No ML job enqueued by Brain; "
                        f"queued fallback {args.start_job_type} job {fb_job_id}."
                    )
                    # Try running the newly enqueued job
                    res = await _run_one_queued_job()
                except Exception as e:
                    print(
                        "  - Failed to enqueue fallback job:"
                        f" {type(e).__name__}: {e}"
                    )

            if res is None:
                print("  - No ML job enqueued by Brain in this cycle.")
                acts_tail = _read_actions_tail(limit=3)
                if acts_tail:
                    print("  - Recent actions tail:")
                    for a in acts_tail:
                        print(
                            f"    • {a.get('action')} status={a.get('status')} "
                            f"reason={a.get('reason','')} params={a.get('params',{})}"
                        )
            else:
                comp_evt = {
                    "job_id": job_id,
                    "job_type": res.get("job_type", "alpha_loop"),
                    "ok": bool(res.get("ok", False)),
                    "duration_sec": int(res.get("duration_sec", 0)),
                    "metrics": res.get("metrics", {}),
                    "artifacts": res.get("artifacts", []),
                }
                print(
                    f"  - Ran job: type={comp_evt['job_type']} ok={comp_evt['ok']} "
                    f"metrics_keys={list(comp_evt['metrics'].keys())}"
                )
                await _trigger_brain_review(comp_evt)

            # If we reach here without exception, reset backoff
            if consecutive_errors:
                print(
                    f"[ML Brain Loop] Cycle succeeded after {consecutive_errors} error(s); "
                    "resetting backoff."
                )
            consecutive_errors = 0
            error_backoff_sec = 5.0

        except Exception as e:
            consecutive_errors += 1
            print(
                f"[ML Brain Loop] Cycle {cycles_run} failed with {type(e).__name__}: {e}"
            )
            # Exponential backoff on repeated failures to avoid hammering
            # ClickHouse/LLM/OS ephemeral ports.
            error_backoff_sec = min(
                error_backoff_max,
                error_backoff_sec * 2.0 if error_backoff_sec > 0 else 5.0,
            )
            print(
                f"[ML Brain Loop] Backing off for {error_backoff_sec:.1f}s "
                f"(consecutive_errors={consecutive_errors})."
            )
            await asyncio.sleep(error_backoff_sec)
            # Continue to next cycle
            continue

        if max_cycles and cycles_run >= max_cycles:
            print(f"\n[ML Brain Loop] Reached max_cycles={max_cycles}, exiting.")
            break

        await asyncio.sleep(float(args.sleep_sec or 0.0))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
