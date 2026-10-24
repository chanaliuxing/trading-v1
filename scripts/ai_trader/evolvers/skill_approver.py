#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from typing import List

from libs.evolution.evolution_chamber import EvolutionChamber, EvaluationOutcome
from libs.skills.runtime import append_skill_flow_event
from libs.logger import get_logger

logger = get_logger("skill_approver")


def _process_once(
    chamber: EvolutionChamber,
    auto_register: bool,
    enable_on_pass: bool,
    require_backtest: bool,
    min_ev: float,
    min_sharpe: float,
) -> List[EvaluationOutcome]:
    """Process queue once with optional backtest/shadow gating before enable."""
    outcomes = chamber.process_queue(auto_register=False, enable_on_pass=False)
    if not outcomes:
        logger.info("No pending skills found.")
        return []

    gated: List[EvaluationOutcome] = []
    pending: List[dict] = []

    for outcome in outcomes:
        status = "✅" if outcome.status == "approved" else "❌"
        logger.info(f"{status} {outcome.skill_id} — {outcome.reason} (coverage={outcome.coverage_pct})")

        if outcome.status == "approved" and require_backtest:
            metrics = {}
            if outcome.artifacts:
                metrics = outcome.artifacts.get("backtest_metrics", {}) or outcome.artifacts.get("shadow_metrics", {})
            ev = None
            sharpe = None
            try:
                ev = float(metrics.get("ev") or metrics.get("ev_realized") or metrics.get("pnl", 0))
            except Exception:
                ev = None
            try:
                sharpe = float(metrics.get("sharpe")) if metrics and metrics.get("sharpe") is not None else None
            except Exception:
                sharpe = None
            if ev is None or sharpe is None or ev < min_ev or sharpe < min_sharpe:
                pending.append({"skill_id": outcome.skill_id, "last_status": "pending_backtest"})
                append_skill_flow_event(
                    {
                        "skill_id": outcome.skill_id,
                        "event": "enable_blocked",
                        "reason": "missing_or_low_backtest",
                        "ev": ev,
                        "sharpe": sharpe,
                        "min_ev": min_ev,
                        "min_sharpe": min_sharpe,
                    },
                    runs_dir=chamber.runs_dir,
                )
                continue

        gated.append(outcome)

    # Re-append pending entries if any
    if pending:
        chamber.save_queue(pending + chamber.load_queue())

    # Auto-register gated approvals only
    for outcome in gated:
        if outcome.status == "approved" and auto_register:
            chamber._register_skill(
                {"skill_id": outcome.skill_id},
                outcome,
                enable_on_pass=enable_on_pass,
            )
    return gated


def main() -> None:
    ap = argparse.ArgumentParser(description="Skill approver daemon — validates queued skills via EvolutionChamber.")
    ap.add_argument("--queue", default="runs/memory/skill_approval_queue.jsonl", help="Path to the pending skill queue (JSONL).")
    ap.add_argument("--results", default="runs/evolution/results", help="Output directory for evaluation artifacts.")
    ap.add_argument("--registry", default="config/skill_registry.json", help="Skill registry file.")
    ap.add_argument("--runs-dir", default="runs/ai_trader", help="Base runs directory for audit logging.")
    ap.add_argument("--interval-sec", type=int, default=300, help="Polling interval when running continuously.")
    ap.add_argument("--once", action="store_true", help="Process the queue once and exit.")
    ap.add_argument("--auto-register", action="store_true", help="Automatically register skills that pass validation.")
    ap.add_argument("--enable-on-pass", action="store_true", help="Set enabled=true when auto-registering approved skills.")
    ap.add_argument("--require-backtest", action="store_true", help="Require backtest/shadow metrics before enabling skills.")
    ap.add_argument("--min-backtest-ev", type=float, default=0.0, help="Minimum backtest EV required to enable.")
    ap.add_argument("--min-backtest-sharpe", type=float, default=0.2, help="Minimum backtest Sharpe required to enable.")
    args = ap.parse_args()

    try:
        chamber = EvolutionChamber(
            queue_path=args.queue,
            results_dir=args.results,
            registry_path=args.registry,
            runs_dir=args.runs_dir,
        )

        append_skill_flow_event({"event": "skill_approver_started"}, runs_dir=args.runs_dir)
        logger.info("Skill approver started", mode="once" if args.once else "continuous")

        while True:
            _process_once(
                chamber,
                auto_register=args.auto_register,
                enable_on_pass=args.enable_on_pass,
                require_backtest=args.require_backtest,
                min_ev=args.min_backtest_ev,
                min_sharpe=args.min_backtest_sharpe,
            )
            if args.once:
                break
            time.sleep(max(args.interval_sec, 30))
    except Exception as e:
        logger.exception("skill_approver_crash", error=str(e))
        raise


if __name__ == "__main__":
    main()
