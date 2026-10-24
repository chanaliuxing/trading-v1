#!/usr/bin/env python3
from __future__ import annotations

"""
Run the strategy council end-to-end (proposal -> review -> synthesis -> paper stub).

Usage:
    python scripts/run_strategy_council.py --disable-llm
    python scripts/run_strategy_council.py --universe AAPL TSLA NVDA --runs-dir runs/ai_trader
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List
import importlib

# Ensure repo root on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.strategy_council import run_council


def main() -> None:
    ap = argparse.ArgumentParser(description="Run strategy council")
    ap.add_argument("--universe", nargs="*", default=[], help="Symbols universe")
    ap.add_argument("--runs-dir", default="runs/ai_trader", help="Where to write council logs")
    ap.add_argument("--disable-llm", action="store_true", help="Skip LLM calls and use fallback proposals")
    ap.add_argument("--paper-runner", default="", help="Import path to paper runner callable, e.g. module.sub:func")
    ap.add_argument("--paper-metrics-path", default=None, help="Load paper/backtest metrics from JSON file")
    ap.add_argument("--ev-plan", type=float, default=None, help="Override planned EV when logging outcome")
    ap.add_argument("--notify-slack", default=None, help="Slack webhook to send paper approval notice")
    ap.add_argument("--live-whitelist", nargs="*", default=[], help="Whitelist strategies allowed for auto-live switch")
    args = ap.parse_args()

    paper_runner = None
    if args.paper_runner:
        try:
            if ":" in args.paper_runner:
                mod, fn = args.paper_runner.split(":", 1)
            elif "#" in args.paper_runner:
                mod, fn = args.paper_runner.split("#", 1)
            else:
                mod, fn = args.paper_runner, None
            module = importlib.import_module(mod)
            paper_runner = getattr(module, fn) if fn else module
        except Exception as exc:
            print(f"[WARN] failed to import paper_runner {args.paper_runner}: {exc}")
            paper_runner = None

    ctx = {
        "universe": args.universe or None,
        "runs_dir": args.runs_dir,
        "disable_llm": bool(args.disable_llm),
        "paper_runner": paper_runner,
        "paper_metrics_path": args.paper_metrics_path,
        "ev_plan": args.ev_plan,
        "notify_slack": args.notify_slack,
        "live_whitelist": args.live_whitelist,
    }

    result = run_council(ctx)

    # Pretty-print summary
    summary = {
        "n_proposals": len(result.get("proposals") or []),
        "has_final_plan": result.get("final_plan") is not None,
        "paper_ok": result.get("paper_result").ok if result.get("paper_result") else None,
        "plan_vs_outcome": result.get("plan_vs_outcome"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
