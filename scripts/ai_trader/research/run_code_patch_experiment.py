#!/usr/bin/env python3
from __future__ import annotations

"""
Bridge runner to execute a code patch experiment using the existing code_evolver_git.py.
Intended to be used by queued code_patch_experiment jobs triggered from the ResearchAgent.

Usage example:
PYTHONPATH=. venv/bin/python scripts/ai_trader/research/run_code_patch_experiment.py \
    --target tool:backtest_ml_engine \
    --task-id exp123 \
    --patch-output runs/ai_trader/patches/exp123.diff \
    --dry-run
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Tuple
from datetime import datetime
from scripts.ai_trader.research import experiment_api


def load_skill_registry() -> dict:
    reg_path = Path("config/skill_registry.json")
    if not reg_path.exists():
        return {}
    try:
        data = json.loads(reg_path.read_text())
        if isinstance(data, dict) and "skills" in data:
            return data["skills"]
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_target_health(target_id: str) -> dict:
    """Load health metrics for a mutable target from snapshot."""
    snapshot_path = Path("runs/memory/mutable_targets.json")
    if not snapshot_path.exists():
        return {"health_score": 1.0, "recommendation": "healthy"}

    try:
        snapshot = json.loads(snapshot_path.read_text())
        target_health = snapshot.get(target_id, {})
        metrics = target_health.get("metrics", {})
        return {
            "health_score": metrics.get("health_score", 1.0),
            "recommendation": metrics.get("recommendation", "healthy"),
            "stale_days": metrics.get("stale_days", 0),
        }
    except Exception:
        return {"health_score": 1.0, "recommendation": "healthy"}


def _attempt_auto_repair(target_id: str) -> Tuple[bool, str]:
    """
    Best-effort auto-repair for common targets.
    Returns (success, reason_tag)
    """
    # Feature tables: try schema/row sync
    if "features_1m_active" in target_id or "features_1m" in target_id:
        cmd = [
            sys.executable,
            "tools/sync_features_active.py",
            "--symbols",
            "TSLA,NVDA",
            "--lookback-days",
            "30",
            "--chunk-days",
            "1",
        ]
        rc = subprocess.run(cmd, check=False).returncode
        return rc == 0, "sync_features_active"

    # Labels generation: attempt recompute for common horizons
    if "labels" in target_id:
        cmd = [
            sys.executable,
            "tools/recompute_labels_1m.py",
            "--symbols",
            "TSLA,NVDA",
            "--start",
            "2025-11-01 09:30:00",
            "--end",
            "2025-12-01 16:00:00",
            "--horizons",
            "30,60,120",
        ]
        rc = subprocess.run(cmd, check=False).returncode
        return rc == 0, "recompute_labels"

    # Data quality backfill: bars
    if "bars_1m" in target_id:
        cmd = [
            sys.executable,
            "tool/load_hist_multi.py",
            "--symbols",
            "TSLA,NVDA",
            "--days",
            "5",
            "--bar-size",
            "1m",
            "--fill-mode",
            "backfill",
        ]
        rc = subprocess.run(cmd, check=False).returncode
        return rc == 0, "backfill_bars"

    # No plan for this target
    return False, "no_repair_plan"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="mutable target id or skill name")
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--patch-output", default=None, help="Path to write generated patch")
    ap.add_argument("--test-cmd", default=None, help="Optional test command")
    ap.add_argument("--llm-model", default=None, help="Override LLM model for patch generation")
    ap.add_argument("--dry-run", action="store_true", help="Generate patch only, skip tests/commits")
    ap.add_argument("--params", default=None, help="Optional JSON string with extra params (unused placeholder)")
    ap.add_argument("--skip-health-check", action="store_true", help="Skip health check (for manual overrides)")
    ap.add_argument("--auto-merge", action="store_true", help="Auto-merge patch for whitelist targets if tests pass")
    ap.add_argument("--attempt-auto-repair", action="store_true", help="Attempt sync/fix when target is unhealthy before patching")
    args = ap.parse_args()

    exp_id = args.task_id or Path(args.patch_output).stem if args.patch_output else None
    if exp_id:
        experiment_api.create_experiment({"experiment_id": exp_id, "target": args.target, "kind": "code_patch_experiment"})

    # === Health check: try auto-repair if unhealthy ===
    repair_reason = None
    if not args.skip_health_check:
        health = load_target_health(args.target)
        health_score = health.get("health_score", 1.0)
        recommendation = health.get("recommendation", "healthy")

        print(f"[health] target={args.target} health_score={health_score:.2f} recommendation={recommendation}")

        unhealthy = (health_score < 0.5 or recommendation == "sync_or_fallback")
        if unhealthy:
            repaired = False
            if args.attempt_auto_repair:
                repaired, repair_reason = _attempt_auto_repair(args.target)
                # reload health after repair attempt
                if repaired:
                    health = load_target_health(args.target)
                    health_score = health.get("health_score", 1.0)
                    recommendation = health.get("recommendation", "healthy")
                    unhealthy = (health_score < 0.5 or recommendation == "sync_or_fallback")
            if unhealthy:
                # Emit event for tracking
                try:
                    from tools.nightly_oof_flow import publish_ml_event
                    publish_ml_event(
                        "aborted",
                        "code_patch_experiment",
                        exp_id or "unknown",
                        params={
                            "target": args.target,
                            "reason": "unhealthy_target",
                            "repair_attempted": args.attempt_auto_repair,
                            "repair_reason": repair_reason if args.attempt_auto_repair else None,
                            "health_score": health_score,
                            "recommendation": recommendation,
                        }
                    )
                except Exception:
                    pass

                if exp_id:
                    experiment_api.complete_experiment(exp_id, status="ABORTED", metrics={"health_score": health_score})
                print(f"[error] Target '{args.target}' is unhealthy (score={health_score:.2f}, rec={recommendation})")
                return 2  # Exit code 2 = aborted due to health check

    # Resolve skill name: if target is skill in registry, use it; otherwise try exact match
    registry = load_skill_registry()
    skill = None
    candidates = [args.target, args.target.split(":", 1)[-1]]
    for cand in candidates:
        if cand in registry:
            skill = cand
            break
    if not skill:
        print(f"[error] target '{args.target}' not found in config/skill_registry.json", file=sys.stderr)
        return 1
    if registry.get(skill, {}).get("allow_evolution") is False:
        print(f"[error] evolution disabled for skill '{skill}'", file=sys.stderr)
        return 1
    # Additional safety: forbid certain prefixes
    forbidden_paths = ["libs/exec", "services/execution", "services/risk", "schema", "infra", "kernel", "execution", "risk"]
    skill_path = registry.get(skill, {}).get("file_path", "")
    if any(skill_path.startswith(fp) for fp in forbidden_paths):
        print(f"[error] evolution forbidden for path '{skill_path}'", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        "scripts/ai_trader/evolvers/code_evolver_git.py",
        "--skill",
        skill,
    ]
    if args.task_id:
        cmd.extend(["--task-id", args.task_id])
    if args.patch_output:
        cmd.extend(["--patch-output", args.patch_output])
    if args.test_cmd:
        cmd.extend(["--test-cmd", args.test_cmd])
    if args.llm_model:
        cmd.extend(["--llm-model", args.llm_model])
    if args.dry_run:
        cmd.append("--dry-run")

    # params currently unused but accepted for forward compatibility
    # Default: enable auto-merge unless explicitly disabled via flag override
    if args.auto_merge or not args.dry_run:
        cmd.append("--auto-merge")

    rc = subprocess.call(cmd)

    # === P5.2: Automatic Patch Evaluation ===
    evaluation_result = None
    if rc == 0 and args.patch_output and Path(args.patch_output).exists():
        try:
            from tools.patch_evaluator import PatchEvaluator

            print(f"[eval] Running automatic patch evaluation for {args.patch_output}")

            evaluator = PatchEvaluator(
                patch_id=exp_id or "unknown",
                patch_file=Path(args.patch_output),
                target_module=args.target
            )

            # Run full evaluation
            evaluation_result = evaluator.evaluate_all()

            # Log results
            scores = evaluation_result.get("scores", {})
            decision = evaluation_result.get("decision", {})

            print(f"[eval] Benefit score: {scores.get('benefit_score', 0.0):.3f}")
            print(f"[eval] Risk score: {scores.get('risk_score', 0.0):.3f}")
            print(f"[eval] Confidence: {scores.get('confidence', 0.0):.3f}")
            print(f"[eval] Decision: {decision.get('recommendation', 'unknown')}")

            # Store evaluation in patch approval queue
            import clickhouse_connect
            try:
                from libs.config import config
                ccfg = config.clickhouse_config
                ch_client = clickhouse_connect.get_client(
                    host=ccfg.get("host", "localhost"),
                    port=int(ccfg.get("port", 8123)),
                    database=ccfg.get("database", "trading"),
                    username=ccfg.get("user", "default"),
                    password=ccfg.get("password", ""),
                )
            except Exception:
                ch_client = clickhouse_connect.get_client(
                    host="localhost",
                    port=8123,
                    database="trading"
                )

            # Insert to patch approval queue
            patch_data = {
                "patch_id": exp_id or "unknown",
                "target_module": args.target,
                "target_file": registry.get(skill, {}).get("file_path", ""),
                "patch_content": Path(args.patch_output).read_text()[:10000],  # Truncate
                "benefit_score": scores.get("benefit_score", 0.0),
                "risk_score": scores.get("risk_score", 0.0),
                "confidence": scores.get("confidence", 0.0),
                "unit_tests_passed": evaluation_result.get("stages", {}).get("unit_tests", {}).get("passed", False),
                "unit_tests_total": evaluation_result.get("stages", {}).get("unit_tests", {}).get("total", 0),
                "unit_tests_failed": evaluation_result.get("stages", {}).get("unit_tests", {}).get("failed", 0),
                "integration_tests_passed": evaluation_result.get("stages", {}).get("integration_tests", {}).get("passed", False),
                "regression_tests_passed": evaluation_result.get("stages", {}).get("regression_tests", {}).get("passed", False),
                "test_coverage": evaluation_result.get("stages", {}).get("unit_tests", {}).get("coverage", 0.0),
                "static_analysis_passed": evaluation_result.get("stages", {}).get("static_analysis", {}).get("passed", False),
                "ruff_issues": evaluation_result.get("stages", {}).get("static_analysis", {}).get("ruff_issues", 0),
                "mypy_issues": evaluation_result.get("stages", {}).get("static_analysis", {}).get("mypy_issues", 0),
                "bandit_issues": evaluation_result.get("stages", {}).get("static_analysis", {}).get("bandit_issues", 0),
                "complexity_score": evaluation_result.get("stages", {}).get("static_analysis", {}).get("complexity_score", 0.0),
                "performance_impact": json.dumps(evaluation_result.get("stages", {}).get("performance_tests", {})),
                "domain": "trading",
                "priority": 2 if decision.get("recommendation") == "auto_approve" else 3,
                "tags": ["auto_generated", args.target.split(":")[0] if ":" in args.target else "unknown"],
            }

            ch_client.insert(
                "evolution.patch_approval_queue",
                [list(patch_data.values())],
                column_names=list(patch_data.keys())
            )

            print(f"[eval] Patch evaluation stored in evolution.patch_approval_queue")

            # Auto-approve if recommendation is auto_approve
            if decision.get("recommendation") == "auto_approve":
                print(f"[eval] ✅ Auto-approved based on evaluation (benefit={scores.get('benefit_score'):.3f}, risk={scores.get('risk_score'):.3f})")
                # TODO: Implement auto-apply logic here
            elif decision.get("recommendation") == "reject":
                print(f"[eval] ❌ Rejected based on evaluation (high risk={scores.get('risk_score'):.3f})")
            else:
                print(f"[eval] ⏸️  Manual review required")

        except Exception as e:
            print(f"[eval] Warning: Patch evaluation failed: {e}")
            import traceback
            traceback.print_exc()

    if exp_id:
        status = "PASS" if rc == 0 else "FAIL"
        metrics = {"rc": rc}

        # Add evaluation results to experiment metrics
        if evaluation_result:
            metrics.update({
                "benefit_score": evaluation_result.get("scores", {}).get("benefit_score", 0.0),
                "risk_score": evaluation_result.get("scores", {}).get("risk_score", 0.0),
                "confidence": evaluation_result.get("scores", {}).get("confidence", 0.0),
                "evaluation_decision": evaluation_result.get("decision", {}).get("recommendation", "unknown"),
            })

        experiment_api.run_experiment(exp_id, {"target": args.target, "skill": skill, "rc": rc})
        experiment_api.complete_experiment(exp_id, status=status, metrics=metrics)
        try:
            from tools.nightly_oof_flow import publish_ml_event
            publish_ml_event(status.lower(), "code_patch_experiment", exp_id, params={"target": args.target, "skill": skill})
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
