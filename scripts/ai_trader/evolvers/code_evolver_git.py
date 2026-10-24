#!/usr/bin/env python3
"""
Code Evolver (Git-based): create branch, generate patch, run static checks/tests, record result.
Includes LLM scoring (code + reasoning), static analysis, and experiment logging.

v3.5 Upgrade: Added test coverage gate for auto-merge (requires >= 85% coverage by default)
"""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path
import argparse
import json
import uuid
from typing import Dict, Any, Optional

# Add project root to path to allow importing libs and scripts
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from libs.evolution.git_sandbox import sandbox
from libs.evolution.golden_runner import run_pure_skill, _load_registry
from libs.logger import get_logger
from llm.llm_client import LLMClient
from scripts.ai_trader.research import experiment_api
from tools.nightly_oof_flow import publish_ml_event

log = get_logger(__name__)


def _calculate_test_coverage(skill_path: str, test_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Calculate test coverage for a skill (v3.5 quality gate).

    Args:
        skill_path: Path to the skill module
        test_path: Optional path to test file

    Returns:
        Dict with coverage_pct and details
    """
    if not test_path or not Path(test_path).exists():
        log.warning("test_coverage_no_tests", skill_path=skill_path)
        return {"coverage_pct": 0.0, "reason": "no_tests_defined"}

    try:
        # Run pytest with coverage
        result = subprocess.run(
            [
                "pytest",
                test_path,
                f"--cov={Path(skill_path).parent}",
                "--cov-report=json:/tmp/coverage_temp.json",
                "--quiet"
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        coverage_file = Path("/tmp/coverage_temp.json")
        if coverage_file.exists():
            coverage_data = json.loads(coverage_file.read_text())
            total_pct = coverage_data.get("totals", {}).get("percent_covered", 0.0)

            log.info("test_coverage_calculated",
                    skill_path=skill_path,
                    coverage_pct=total_pct)

            return {
                "coverage_pct": total_pct,
                "lines_covered": coverage_data.get("totals", {}).get("covered_lines", 0),
                "lines_total": coverage_data.get("totals", {}).get("num_statements", 0),
                "details": coverage_data.get("files", {})
            }

    except subprocess.TimeoutExpired:
        log.warning("test_coverage_timeout", skill_path=skill_path)
        return {"coverage_pct": 0.0, "reason": "timeout"}
    except Exception as e:
        log.warning("test_coverage_failed", skill_path=skill_path, error=str(e))
        return {"coverage_pct": 0.0, "reason": str(e)}

    return {"coverage_pct": 0.0, "reason": "unknown"}


def _get_min_coverage_threshold() -> float:
    """Get minimum test coverage threshold from config.yml"""
    try:
        import yaml
        config_path = Path("config.yml")
        if config_path.exists():
            with open(config_path) as f:
                config = yaml.safe_load(f)
                return config.get("features", {}).get("v3_autonomy", {}).get(
                    "auto_merge_min_test_coverage_pct", 85.0
                )
    except Exception:
        pass

    return 85.0  # Default: 85%


def _load_backtest_metrics(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Load backtest metrics JSON from a file path.
    Expected keys: ev (or ev_realized), mdd
    """
    if not path:
        return None
    try:
        p = Path(path)
        if not p.exists():
            log.warning("backtest_metrics_missing", path=path)
            return None
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("backtest_metrics_load_failed", path=path, err=str(exc))
        return None


def _generate_patch(spec: Dict[str, Any], args: argparse.Namespace) -> str:
    """Build context and ask local LLM for a unified diff patch."""
    policy_path = Path("config/evolver_llm_policy.json")
    if not policy_path.exists():
        raise FileNotFoundError("config/evolver_llm_policy.json missing")
    policy = json.loads(policy_path.read_text())
    system_prompt = policy.get("base_prompt") or "You are a senior engineer generating a patch."
    primary_model = args.llm_model or policy.get("models", {}).get("primary")

    skill_path = spec.get("file_path")
    if not skill_path or not Path(skill_path).exists():
        raise FileNotFoundError(f"skill file not found: {skill_path}")
    code = Path(skill_path).read_text(encoding="utf-8")
    max_len = 8000
    if len(code) > max_len:
        code = code[:max_len] + "\n# ... truncated ..."

    test_path = spec.get("test_path")
    tests_snippet = ""
    if test_path and Path(test_path).exists():
        t = Path(test_path).read_text(encoding="utf-8")
        if len(t) > max_len:
            t = t[:max_len] + "\n# ... truncated ..."
        tests_snippet = t

    user_prompt = f"""
Skill: {args.skill}
File: {skill_path}
Current code:
```
{code}
```
Tests/fixtures (if provided):
```
{tests_snippet}
```
Produce a unified diff patch (only) that improves performance/clarity without changing behavior. Keep changes minimal.
"""
    client = LLMClient(model=primary_model)
    text_resp = ""
    for attempt in range(2):
        resp = client.generate(system_prompt, user_prompt, model=primary_model)
        if isinstance(resp, dict) and "text" in resp:
            text_resp = resp["text"]
        elif isinstance(resp, dict):
            for key in ("patch", "diff", "content"):
                if key in resp and isinstance(resp[key], str):
                    text_resp = resp[key]
                    break
            if not text_resp:
                text_resp = json.dumps(resp)
        else:
            text_resp = str(resp)

        if text_resp.lstrip().startswith(("diff", "---", "+++", "@@")):
            return text_resp
        user_prompt = "You MUST output a unified diff only. Do not wrap in JSON.\n" + user_prompt

    wrapped = (
        f"--- {skill_path}\n"
        f"+++ {skill_path}\n"
        f"@@\n"
        f"# LLM did not return a unified diff. Response below for manual editing:\n"
        f"{text_resp}\n"
    )
    return wrapped


def _score_patch(patch_text: str, skill: str, reasoning_model: str | None) -> Dict[str, Any]:
    """Optional LLM scoring: code-focused (primary model) and reasoning-focused (secondary)."""
    scores: Dict[str, Any] = {}
    try:
        policy_path = Path("config/evolver_llm_policy.json")
        if policy_path.exists():
            policy = json.loads(policy_path.read_text())
            primary_model = policy.get("models", {}).get("primary")
            critic_model = reasoning_model or policy.get("models", {}).get("critic")
        else:
            primary_model = None
            critic_model = reasoning_model

        if primary_model:
            client = LLMClient(model=primary_model)
            prompt = f"Rate this patch for skill {skill}. Return JSON {{\"code_quality\":0-1,\"risk\":0-1}}.\n{patch_text[:4000]}"
            resp = client.generate("", prompt, model=primary_model)
            scores["code_critic"] = resp if isinstance(resp, dict) else {"raw": str(resp)}
        if critic_model:
            client2 = LLMClient(model=critic_model)
            prompt2 = f"Critique this patch for risk/regression. Return JSON {{\"risk\":0-1,\"notes\":\"...\"}}.\n{patch_text[:4000]}"
            resp2 = client2.generate("", prompt2, model=critic_model)
            scores["reasoning_critic"] = resp2 if isinstance(resp2, dict) else {"raw": str(resp2)}
    except Exception:
        pass
    return scores


def _run_static_checks(path: str) -> Dict[str, Any]:
    """Best-effort static analysis (ruff) if available."""
    result = {"ok": True, "stderr": "", "duration_sec": None}
    try:
        cmd = ["ruff", "check", path]
        import time
        start = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        result["duration_sec"] = round(time.time() - start, 3)
        result["ok"] = proc.returncode == 0
        result["stderr"] = (proc.stderr or "")[-2000:]
    except FileNotFoundError:
        result["stderr"] = "ruff_not_installed"
    except Exception as e:
        result["ok"] = False
        result["stderr"] = str(e)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True, help="Skill name from config/skill_registry.json")
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--test-cmd", default=None, help="Optional shell test command")
    ap.add_argument("--experiment-log", default="runs/ai_trader/evolution_experiments.jsonl")
    ap.add_argument("--patch-output", default=None, help="Path to write suggested patch (for PR review)")
    ap.add_argument("--llm-model", default=None, help="Override primary LLM model for patch generation")
    ap.add_argument("--dry-run", action="store_true", help="Generate patch only, skip tests/commits")
    ap.add_argument("--reasoning-model", default=None, help="Optional secondary model for reasoning/critique")
    ap.add_argument("--backtest-metrics-path", default=None, help="Path to JSON file with backtest results (ev/mdd required)")
    ap.add_argument("--min-backtest-ev", type=float, default=0.0, help="Minimum EV required to auto-merge")
    ap.add_argument("--max-backtest-mdd", type=float, default=5.0, help="Maximum drawdown allowed to auto-merge")
    args = ap.parse_args()

    task_id = args.task_id or str(uuid.uuid4())[:8]
    result_record = {"task_id": task_id, "skill": args.skill, "status": "unknown"}

    skills = _load_registry()
    spec = skills.get(args.skill)
    if not spec:
        raise SystemExit(f"skill not found: {args.skill}")
    if spec.get("allow_evolution") is False:
        raise SystemExit(f"evolution disabled for skill {args.skill}")
    forbidden_paths = ["libs/exec", "services/execution", "services/risk", "schema", "infra", "kernel", "execution", "risk"]
    skill_path = spec.get("file_path", "")
    if any(skill_path.startswith(fp) for fp in forbidden_paths):
        raise SystemExit(f"evolution forbidden for path {skill_path}")

    branch_name = None
    patch_text = None
    # Emit started event
    try:
        publish_ml_event("started", "code_patch_experiment", task_id, params={"skill": args.skill})
    except Exception:
        pass

    with sandbox(task_id) as gs:
        log.info("sandbox_branch_created", branch=gs.current_branch)
        branch_name = gs.current_branch

        if args.patch_output:
            try:
                patch = _generate_patch(spec, args)
                Path(args.patch_output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.patch_output).write_text(patch, encoding="utf-8")
                patch_text = patch
                log.info("evolver_llm_patch_generated", path=args.patch_output)
            except Exception as exc:
                log.warning("evolver_llm_patch_failed", error=str(exc))

        if args.dry_run:
            log.info("evolver_dry_run_complete", skill=args.skill)
            return

        ok = False
        static_report = _run_static_checks(skill_path) if Path(skill_path).exists() else {"ok": True}
        test_report: Dict[str, Any] = {}
        if args.test_cmd:
            ok = gs.commit_and_test(f"evolve({args.skill})", args.test_cmd)
            # capture stderr tail from test cmd
            test_report = {"test_cmd": args.test_cmd, "ok": ok, "stderr_tail": getattr(gs, "last_test_stderr", None)}
            if not ok:
                result_record["error_code"] = "test_failed"
        else:
            golden = {}
            if spec.get("type") == "PURE":
                golden = run_pure_skill(args.skill)
                ok = golden.get("ok", False)
                if not ok:
                    result_record["error_code"] = "golden_failed"
            elif spec.get("test_path") and Path(spec["test_path"]).exists():
                test_cmd = f"pytest {spec['test_path']}"
                ok = gs.commit_and_test(f"evolve({args.skill})", test_cmd)
                golden = {"test_cmd": test_cmd, "ok": ok, "stderr_tail": getattr(gs, "last_test_stderr", None)}
                if not ok:
                    result_record["error_code"] = "pytest_failed"
            else:
                ok = False
                golden = {"error": "no_tests_defined"}
                result_record["error_code"] = "no_tests_defined"
            result_record["golden"] = golden
        result_record["test_report"] = test_report

        # 🆕 P1.2 + v3.5: Auto-merge if tests pass, path is whitelisted, AND test coverage >= threshold
        auto_merge_whitelist = [
            "prompts/", "docs/", "config/", "scripts/ai_trader/evolvers/",
            "libs/evolution/", "tools/data_quality", "tests/"
        ]
        auto_merge_blacklist = [
            "orchestrator.py", "risk/", "execution/", "schema/",
            "infra/", "kernel/", "services/execution", "services/risk"
        ]

        # v3.5: Calculate test coverage
        test_path = spec.get("test_path")
        coverage_report = _calculate_test_coverage(skill_path, test_path)
        coverage_pct = coverage_report.get("coverage_pct", 0.0)
        min_coverage = _get_min_coverage_threshold()

        backtest_ev = None
        backtest_mdd = None
        backtest_gate_reason = None
        backtest_gate_passed = False

        backtest_metrics_path = args.backtest_metrics_path or spec.get("backtest_metrics_path")
        backtest_report = _load_backtest_metrics(backtest_metrics_path)
        if backtest_report:
            backtest_ev = backtest_report.get("ev") if isinstance(backtest_report, dict) else None
            if backtest_ev is None:
                backtest_ev = backtest_report.get("ev_realized") if isinstance(backtest_report, dict) else None
            backtest_mdd = backtest_report.get("mdd") if isinstance(backtest_report, dict) else None
            try:
                ev_ok = float(backtest_ev) >= float(args.min_backtest_ev)
                mdd_ok = backtest_mdd is None or float(backtest_mdd) <= float(args.max_backtest_mdd)
                backtest_gate_passed = ev_ok and mdd_ok
                if not ev_ok:
                    backtest_gate_reason = f"ev_below_min ({backtest_ev} < {args.min_backtest_ev})"
                if not mdd_ok:
                    backtest_gate_reason = f"mdd_above_max ({backtest_mdd} > {args.max_backtest_mdd})"
            except Exception:
                backtest_gate_reason = "backtest_metrics_invalid"
        else:
            backtest_gate_reason = "backtest_metrics_missing"

        result_record["test_coverage"] = coverage_report
        result_record["backtest_metrics"] = backtest_report
        result_record["backtest_gate_reason"] = backtest_gate_reason
        result_record["auto_merged"] = False
        result_record["auto_merge_reason"] = None

        if ok and static_report.get("ok", True):
            # Check if skill_path is in whitelist and not in blacklist
            is_whitelisted = any(skill_path.startswith(w) for w in auto_merge_whitelist)
            is_blacklisted = any(skill_path.startswith(b) for b in auto_merge_blacklist)

            # v3.5: Check test coverage gate
            coverage_gate_passed = coverage_pct >= min_coverage
            # v3.6: Backtest gate (EV/MDD)

            if is_whitelisted and not is_blacklisted and coverage_gate_passed and backtest_gate_passed:
                log.info("auto_merge_eligible", skill=args.skill, path=skill_path)
                merge_success = gs.auto_merge(target_branch="main")
                if merge_success:
                    gs.should_auto_merge = True  # Prevent rollback
                    result_record["auto_merged"] = True
                    result_record["auto_merge_reason"] = "whitelist_with_coverage"
                    log.info("code_evolution_auto_merged", skill=args.skill, branch=gs.current_branch)

                    # Write memory: record successful evolution
                    try:
                        from scripts.ai_trader.memory.memory_fabric import write_memory
                        write_memory('STS', {
                            'summary': f'Code evolution succeeded for {args.skill}',
                            'skill': args.skill,
                            'file_path': skill_path,
                            'lesson': f'Auto-evolution and merge successful for {skill_path}',
                            'test_result': 'PASS',
                            'auto_merged': True
                        })
                    except Exception:
                        pass
                    try:
                        publish_ml_event(
                            "auto_merged",
                            "code_patch_experiment",
                            task_id,
                            metrics={
                                "coverage_pct": coverage_pct,
                                "min_coverage": min_coverage,
                                "static_ok": static_report.get("ok", True),
                                "tests_ok": ok,
                            },
                            params={"skill": args.skill, "file_path": skill_path},
                        )
                    except Exception:
                        pass
                else:
                    result_record["auto_merge_failed"] = True
                    result_record["auto_merge_reason"] = "git_merge_failed"
                    log.warning("auto_merge_failed_keeping_branch", branch=gs.current_branch)
            elif not coverage_gate_passed:
                # v3.5: Coverage gate blocked auto-merge
                log.warning("auto_merge_blocked_by_coverage_gate",
                           skill=args.skill,
                           coverage_pct=coverage_pct,
                           min_required=min_coverage)
                result_record["auto_merge_skipped"] = f"coverage_too_low ({coverage_pct:.1f}% < {min_coverage:.1f}%)"
                result_record["auto_merge_reason"] = "coverage_gate"
            elif not backtest_gate_passed:
                log.warning(
                    "auto_merge_blocked_by_backtest_gate",
                    skill=args.skill,
                    ev=backtest_ev,
                    mdd=backtest_mdd,
                    min_ev=args.min_backtest_ev,
                    max_mdd=args.max_backtest_mdd,
                    reason=backtest_gate_reason,
                )
                result_record["auto_merge_skipped"] = backtest_gate_reason or "backtest_gate"
                result_record["auto_merge_reason"] = "backtest_gate"
            else:
                log.info("auto_merge_skipped_not_whitelisted",
                        skill=args.skill, path=skill_path,
                        whitelisted=is_whitelisted, blacklisted=is_blacklisted)
                result_record["auto_merge_skipped"] = "not_whitelisted"
                result_record["auto_merge_reason"] = "not_whitelisted"

    result_record["static"] = static_report
    if patch_text:
        result_record["llm_scores"] = _score_patch(patch_text, args.skill, args.reasoning_model)
    result_record["status"] = "pass" if ok else "fail"
    log.info("evolver_result", **result_record)

    # If not auto-merged, enqueue a PR/audit artifact for manual review
    if not result_record.get("auto_merged"):
        pr_dir = Path("runs/ai_trader/evolution_pr_queue")
        pr_dir.mkdir(parents=True, exist_ok=True)
        pr_payload = {
            "task_id": task_id,
            "skill": args.skill,
            "branch": branch_name,
            "file_path": skill_path,
            "status": result_record["status"],
            "auto_merge_reason": result_record.get("auto_merge_reason"),
            "coverage_pct": result_record.get("test_coverage", {}).get("coverage_pct"),
            "backtest_ev": backtest_ev,
            "backtest_mdd": backtest_mdd,
            "backtest_gate_reason": backtest_gate_reason,
            "static_ok": static_report.get("ok", True),
            "tests_ok": ok,
            "patch_output": args.patch_output,
            "created_at": str(Path().stat().st_mtime),
        }
        pr_fp = pr_dir / f"{task_id}.json"
        try:
            pr_fp.write_text(json.dumps(pr_payload, indent=2), encoding="utf-8")
            log.info("evolver_pr_enqueued", file=str(pr_fp))
        except Exception as exc:
            log.warning("evolver_pr_enqueue_failed", error=str(exc))

    try:
        runs_dir = Path("runs/ai_trader")
        runs_dir.mkdir(parents=True, exist_ok=True)
        fp = runs_dir / f"evolver_{task_id}.json"
        fp.write_text(json.dumps(result_record, indent=2), encoding="utf-8")

        exp_fp = Path(args.experiment_log)
        exp_fp.parent.mkdir(parents=True, exist_ok=True)
        exp_row = {
            "experiment_id": task_id,
            "target_skill": args.skill,
            "objective": spec.get("objective", "PERFORMANCE"),
            "git_branch": branch_name or "",
            "test_result": "PASS" if ok else "FAIL",
            "static_ok": static_report.get("ok", True),
            "status": "COMPLETED",
            "test_report": result_record.get("test_report"),
        }
        if result_record.get("llm_scores"):
            exp_row["llm_scores"] = result_record["llm_scores"]
        with exp_fp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(exp_row) + "\n")

        if args.patch_output:
            patch_fp = Path(args.patch_output)
            patch_fp.parent.mkdir(parents=True, exist_ok=True)
            patch_fp.write_text(
                f"# Patch suggestion placeholder for skill {args.skill} (task {task_id})\n"
                "# TODO: integrate LLM-generated diff here; manual review required.\n",
                encoding="utf-8",
            )
    except Exception:
        pass

    # Record into experiment API (best effort)
    try:
        experiment_api.run_experiment(
            task_id,
            {
                "skill": args.skill,
                "status": result_record["status"],
                "static_ok": static_report.get("ok", True),
                "coverage_pct": result_record.get("test_coverage", {}).get("coverage_pct"),
                "backtest_ev": backtest_ev,
                "backtest_mdd": backtest_mdd,
                "backtest_gate_reason": backtest_gate_reason,
                "auto_merged": result_record.get("auto_merged"),
                "golden": result_record.get("golden"),
                "llm_scores": result_record.get("llm_scores"),
                "test_report": result_record.get("test_report"),
                "error_code": result_record.get("error_code"),
            },
        )
        experiment_api.complete_experiment(
            task_id,
            status=result_record["status"],
            metrics={
                "static_ok": static_report.get("ok", True),
                "test_result": result_record.get("test_report"),
                "coverage_pct": result_record.get("test_coverage", {}).get("coverage_pct"),
                "backtest_ev": backtest_ev,
                "backtest_mdd": backtest_mdd,
                "backtest_gate_reason": backtest_gate_reason,
                "auto_merged": result_record.get("auto_merged"),
                "error_code": result_record.get("error_code"),
            },
        )
    except Exception:
        pass
    try:
        publish_ml_event(
            result_record["status"],
            "code_patch_experiment",
            task_id,
            metrics={
                "static_ok": static_report.get("ok", True),
                "test_result": result_record.get("test_report"),
                "coverage_pct": result_record.get("test_coverage", {}).get("coverage_pct"),
                "backtest_ev": backtest_ev,
                "backtest_mdd": backtest_mdd,
                "backtest_gate_reason": backtest_gate_reason,
                "auto_merged": result_record.get("auto_merged"),
                "auto_merge_reason": result_record.get("auto_merge_reason"),
                "error_code": result_record.get("error_code"),
            },
            params={"skill": args.skill},
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
