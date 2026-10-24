#!/usr/bin/env python3
from __future__ import annotations
"""
Circuit Breaker

Aggregates simple risk signals and triggers protective actions:
  - BLOCK_NEW / SIZE_DOWN / EXIT_ALL
  - Optional hedging via scripts/ai_trader/portfolio/execute_hedge.py

Heuristics v1 (best-effort, no DB hard dependency):
  - If runs/.../risk_status.json shows HARD_BREACH → EXIT_ALL
  - Else if guardian metrics file indicates severe dd (> config threshold) → SIZE_DOWN
  - Else NOOP

Optionally, if Postgres is reachable, aggregate last-N minutes trades/min and P&L to refine decision (TODO).
"""
import argparse
import json
import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


def _decide(runs_dir: str) -> Dict[str, Any]:
    d = Path(runs_dir) / date.today().isoformat()
    status = _read_json(d / "risk_status.json")
    if status and str(status.get("risk_status", "")).upper() == "HARD_BREACH":
        return {"action": "EXIT_ALL", "reason": "risk_status_hard_breach"}
    # guardian snapshot (optional)
    guard = _read_json(d / "guardian_metrics.json")
    try:
        dd = float(guard.get("max_drawdown_pct", 0.0) or 0.0)
        thr = float(os.getenv("CB_DD_THR", "0.02"))
        if dd >= thr:
            return {"action": "SIZE_DOWN", "reason": f"guardian_dd>{thr}"}
    except Exception:
        pass
    return {"action": "NONE", "reason": "ok"}


def _maybe_hedge(runs_dir: str, action: str) -> None:
    if action not in {"EXIT_ALL", "SIZE_DOWN"}:
        return
    # Use execute_hedge if config requests auto_execute
    try:
        cmd = [
            "python", "scripts/ai_trader/portfolio/execute_hedge.py",
            "--runs-dir", runs_dir,
        ]
        if os.getenv("HEDGE_EXECUTE", "").strip():
            cmd.append("--execute")
        subprocess.run(cmd, check=False)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="runs/ai_trader")
    ap.add_argument("--apply", action="store_true", help="Apply action (prints by default)")
    args = ap.parse_args()

    decision = _decide(args.runs_dir)
    print(json.dumps(decision))
    # Persist audit
    try:
        outd = Path(args.runs_dir) / date.today().isoformat() / "audits"
        outd.mkdir(parents=True, exist_ok=True)
        (outd / "circuit_breaker.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    except Exception:
        pass
    if args.apply and decision.get("action") in {"EXIT_ALL", "SIZE_DOWN"}:
        _maybe_hedge(args.runs_dir, decision["action"])  # best-effort


if __name__ == "__main__":
    main()

