#!/usr/bin/env python3
from __future__ import annotations

"""
LLM-based ML Job Reviewer
-------------------------

Use the AI Brain (ControlBrain.review_ml) to review a single ML job
(`oof`, `hpo_cli`, or `alpha_loop`) based on its ml_events.jsonl entry
and write a structured review JSON.

Usage:
    ENV_MODE=paper venv/bin/python scripts/ai_trader/tools/review_ml_job.py \
        --job-id 20251117024258-o3d933 \
        --runs-dir runs/ai_trader
"""

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import sys as _sys

# Ensure project root on sys.path so that "scripts.*" and "libs.*" imports work.
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parents[3]
if str(_PROJECT_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PROJECT_ROOT))

# Import config to trigger .env loading (dotenv) so provider API keys from .env are available.
from libs.config import config as _cfg  # noqa: F401  # type: ignore


def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="LLM-based review for a single ML job (oof/hpo_cli/alpha_loop)")
    ap.add_argument("--job-id", required=True, help="Job ID to review (from ml_events.jsonl)")
    ap.add_argument(
        "--runs-dir",
        default="runs/ai_trader",
        help="Root runs directory (default: runs/ai_trader)",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=3,
        help="Look back this many days when searching for job (default: 3)",
    )
    ap.add_argument(
        "--output",
        default=None,
        help="Optional explicit output path. Default: runs/<date>/ml_review_<job_id>.json",
    )
    return ap.parse_args(argv)


def _find_job_event(job_id: str, runs_dir: Path, days: int) -> Optional[Dict[str, Any]]:
    """Search ml_events.jsonl for the given job_id in recent N days."""
    end_date = date.today()
    for offset in range(days):
        d = end_date - timedelta(days=offset)
        evt_path = runs_dir / d.isoformat() / "ml_events.jsonl"
        if not evt_path.exists():
            continue
        try:
            with evt_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("job_id") == job_id and evt.get("status") == "completed":
                        evt["_date"] = d.isoformat()
                        return evt
        except Exception:
            continue
    # Backward-compatible fallback for tests/ad-hoc runs with historical folders.
    try:
        for evt_path in sorted(runs_dir.glob("*/ml_events.jsonl"), reverse=True):
            try:
                dstr = evt_path.parent.name
                with evt_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if evt.get("job_id") == job_id and evt.get("status") == "completed":
                            evt["_date"] = dstr
                            return evt
            except Exception:
                continue
    except Exception:
        pass
    return None


def _build_context(evt: Dict[str, Any], runs_dir: Path) -> Dict[str, Any]:
    """Build a compact context for ControlBrain.review_ml() from a single ML job event."""
    job_id = evt.get("job_id")
    job_type = evt.get("job_type")
    metrics = evt.get("metrics") or {}
    params = evt.get("params") or {}
    dstr = evt.get("_date") or date.today().isoformat()

    ctx: Dict[str, Any] = {
        "date": dstr,
        "job": {
            "job_id": job_id,
            "job_type": job_type,
            "metrics": metrics,
            "params": params,
            "duration_sec": evt.get("duration_sec"),
        },
    }

    # Attach OOF summary if this is an OOF job
    try:
        if job_type == "oof":
            oof_dir = runs_dir / dstr / "oof_eval"
            if oof_dir.exists():
                # Prefer job-specific full metrics when available.
                job_oof = oof_dir / f"oof_results_{job_id}.json"
                if job_oof.exists():
                    try:
                        ctx["oof_details"] = json.loads(job_oof.read_text("utf-8"))
                    except Exception:
                        pass
                oof_sum: Dict[str, Any] = {}
                for fp in oof_dir.glob("*.json"):
                    try:
                        j = json.loads(fp.read_text("utf-8"))
                        oof_sum[fp.stem] = {k: j.get(k) for k in ("auc", "ic", "coverage")}
                    except Exception:
                        continue
                if oof_sum:
                    ctx["oof_eval"] = oof_sum
    except Exception:
        pass

    # Attach HPO best_params if this is an hpo_cli job
    try:
        if job_type == "hpo_cli":
            best_params = metrics.get("best_params")
            if isinstance(best_params, dict):
                ctx.setdefault("hpo_cli", {})["best_params"] = best_params
    except Exception:
        pass

    return ctx


def main(argv=None) -> None:
    args = _parse_args(argv)
    runs_dir = Path(args.runs_dir)

    evt = _find_job_event(args.job_id, runs_dir, args.days)
    if not evt:
        print(f"Job {args.job_id} not found in ml_events.jsonl over last {args.days} days.")
        return

    job_type = evt.get("job_type")
    print(f"Found job {args.job_id} (type={job_type}) on date={evt.get('_date')}")

    ctx = _build_context(evt, runs_dir)

    # Import here so test-time monkeypatch of scripts.ai_trader.brain.ControlBrain is effective.
    from scripts.ai_trader.brain import ControlBrain  # type: ignore
    brain = ControlBrain(runs_dir=str(runs_dir))
    review = brain.review_ml(ctx)

    out_date = evt.get("_date") or date.today().isoformat()
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = runs_dir / out_date / f"ml_review_{args.job_id}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": args.job_id,
        "date": out_date,
        "review": review,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"ML job review written to {out_path}")


if __name__ == "__main__":
    main()
