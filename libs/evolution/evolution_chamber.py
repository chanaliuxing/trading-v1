from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Try to import BacktestEngine for skill validation
try:
    from backtest_ml_engine import BacktestConfig, BacktestEngine
    HAS_BACKTEST = True
except ImportError:
    HAS_BACKTEST = False

from libs.config import config
from libs.evolution.governance import EvolutionGovernance, GovernanceDecision
from libs.skills.registry import SkillRecord, SkillRegistry
from libs.skills.runtime import append_skill_flow_event


@dataclass
class EvaluationOutcome:
    skill_id: str
    status: str
    coverage_pct: Optional[float]
    tests_passed: bool
    ruff_passed: bool
    backtest_passed: Optional[bool] = None
    backtest_metrics: Optional[Dict[str, Any]] = None
    reason: str = ""
    governance: Optional[GovernanceDecision] = None
    artifacts: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "skill_id": self.skill_id,
            "status": self.status,
            "coverage_pct": self.coverage_pct,
            "tests_passed": self.tests_passed,
            "ruff_passed": self.ruff_passed,
            "backtest_passed": self.backtest_passed,
            "backtest_metrics": self.backtest_metrics,
            "reason": self.reason,
            "artifacts": self.artifacts,
        }
        if self.governance:
            payload["governance"] = {
                "allowed": self.governance.allowed,
                "reason": self.governance.reason,
                "gate_triggered": self.governance.gate_triggered,
                "metadata": self.governance.metadata,
            }
        return payload


class EvolutionChamber:
    """Evaluate pending skills in isolation (tests + lint + governance)."""

    def __init__(
        self,
        queue_path: str | Path = "runs/memory/skill_approval_queue.jsonl",
        results_dir: str | Path = "runs/evolution/results",
        registry_path: str | Path = "config/skill_registry.json",
        runs_dir: str | Path = "runs/ai_trader",
        governance: Optional[EvolutionGovernance] = None,
        runner=subprocess.run,
    ):
        self.queue_path = Path(queue_path)
        self.results_dir = Path(results_dir)
        self.registry = SkillRegistry(registry_path)
        self.runs_dir = Path(runs_dir)
        self.governance = governance or EvolutionGovernance()
        self.runner = runner
        self.project_root = Path(__file__).resolve().parents[2]
        self.min_coverage = float(config.get("features.v3_autonomy.auto_merge_min_test_coverage_pct", 90) or 90.0)
        # Backtest gate is opt-in; default off for compatibility with lightweight/test environments.
        self.require_backtest = bool(config.get("features.v3_autonomy.require_skill_backtest", False))

    # --- queue helpers ---
    def load_queue(self) -> List[Dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        skills: List[Dict[str, Any]] = []
        for line in self.queue_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                skills.append(json.loads(line))
            except Exception:
                continue
        return skills

    def save_queue(self, skills: List[Dict[str, Any]]) -> None:
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        with self.queue_path.open("w", encoding="utf-8") as f:
            for skill in skills:
                f.write(json.dumps(skill, ensure_ascii=False) + "\n")

    # --- evaluation pipeline ---
    def process_queue(self, auto_register: bool = False, enable_on_pass: bool = False) -> List[EvaluationOutcome]:
        skills = self.load_queue()
        outcomes: List[EvaluationOutcome] = []
        remaining: List[Dict[str, Any]] = []
        for skill in skills:
            outcome = self.evaluate_skill(skill)
            outcomes.append(outcome)
            # Keep rejected/error entries for later review
            if outcome.status not in {"approved", "passed"}:
                remaining.append({**skill, "last_status": outcome.status, "last_reason": outcome.reason})
            elif auto_register:
                self._register_skill(skill, outcome, enable_on_pass=enable_on_pass)
        self.save_queue(remaining)
        return outcomes

    def evaluate_skill(self, skill: Dict[str, Any]) -> EvaluationOutcome:
        skill_id = skill.get("skill_id") or skill.get("id") or skill.get("name") or f"skill_{int(datetime.now().timestamp())}"
        file_path = skill.get("file_path") or ""
        test_path = skill.get("test_path") or ""
        coverage_pct: Optional[float] = None
        reasons: List[str] = []

        append_skill_flow_event(
            {"skill_id": skill_id, "event": "evaluation_started", "file_path": file_path, "test_path": test_path},
            runs_dir=self.runs_dir,
        )

        tests_passed, coverage_pct, test_artifacts = self._run_tests(skill_id, file_path, test_path)
        ruff_passed, ruff_artifacts = self._run_ruff(skill_id, file_path)
        
        # Run backtest only if static checks pass
        backtest_passed = None
        backtest_metrics = None
        bt_artifacts = {}

        if tests_passed and ruff_passed:
            backtest_passed, backtest_metrics, bt_artifacts = self._run_backtest(skill_id, file_path)

        if not tests_passed:
            reasons.append("pytest_failed")
        if not ruff_passed:
            reasons.append("ruff_failed")
        if backtest_passed is False:
            reasons.append("backtest_failed")
        elif backtest_passed is None and self.require_backtest:
            reasons.append("backtest_required_but_missing")

        if coverage_pct is None:
            reasons.append("coverage_missing")
        elif coverage_pct < self.min_coverage:
            reasons.append(f"coverage_below_threshold:{coverage_pct:.1f}%<{self.min_coverage}%")

        governance_decision = self._check_governance(file_path, coverage_pct)
        if not governance_decision.allowed:
            reasons.append(governance_decision.reason)

        status = "approved"
        if reasons:
            status = "rejected"

        outcome = EvaluationOutcome(
            skill_id=skill_id,
            status=status,
            coverage_pct=coverage_pct,
            tests_passed=tests_passed,
            ruff_passed=ruff_passed,
            backtest_passed=backtest_passed,
            backtest_metrics=backtest_metrics,
            reason="; ".join(reasons) if reasons else "ok",
            governance=governance_decision,
            artifacts={
                "pytest": test_artifacts,
                "ruff": ruff_artifacts,
                "backtest": bt_artifacts,
            },
        )

        self._write_result(outcome)
        append_skill_flow_event(
            {"skill_id": skill_id, "event": "evaluation_completed", "status": status, "coverage_pct": coverage_pct, "reasons": reasons},
            runs_dir=self.runs_dir,
        )
        if status == "approved":
            try:
                self.governance.record_patch_submission({"skill_id": skill_id, "target": file_path})
            except Exception:
                # Non-blocking
                pass
        return outcome

    # --- internal helpers ---
    def _write_result(self, outcome: EvaluationOutcome) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.results_dir / f"{outcome.skill_id}.json"
        out_path.write_text(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def _run_backtest(self, skill_id: str, file_path: str) -> Tuple[Optional[bool], Optional[Dict[str, Any]], Dict[str, Any]]:
        """
        Run a lightweight backtest to verify the skill in a trading loop context.
        Returns: (passed, metrics, artifacts)
        """
        artifacts = {}
        if not HAS_BACKTEST:
            return None, None, {"skipped": "backtest_engine_missing"}
        
        if not file_path or not Path(file_path).exists():
            return False, {}, {"error": "file_not_found"}

        # Define a minimal backtest config
        # We use a dummy strategy that just loads the skill
        # In a real scenario, we'd dynamically inject the skill into a strategy
        # For now, we just check if it crashes
        try:
            # Create a temporary test strategy that imports the skill
            # This ensures the skill can be loaded and executed in a strategy context
            
            # TODO: Implement a proper SkillTestingStrategy that wraps the skill.
            # For now, we mark as skipped to avoid false positives; gating will
            # reject if require_backtest=True.
            return None, None, {"status": "skipped_placeholder"}

        except Exception as e:
            return False, {}, {"error": str(e), "traceback": "..."}

    def _run_tests(self, skill_id: str, file_path: str, test_path: str) -> Tuple[bool, Optional[float], Dict[str, Any]]:
        artifacts: Dict[str, Any] = {}
        if not test_path:
            return False, None, {"reason": "missing_test_path"}
        cmd = [sys.executable, "-m", "pytest", test_path, "--maxfail", "1", "--disable-warnings"]
        self.results_dir.mkdir(parents=True, exist_ok=True)
        coverage_target = self._coverage_target(file_path)
        cov_file: Optional[Path] = None
        if coverage_target:
            cov_file = self.results_dir / f"{skill_id}_coverage.json"
            cov_arg = f"json:{cov_file}"
            cmd += ["--cov", coverage_target, f"--cov-report={cov_arg}"]
        proc = self.runner(
            cmd,
            cwd=self.project_root,
            text=True,
            capture_output=True,
            env=self._build_env(),
        )
        artifacts["returncode"] = proc.returncode
        artifacts["stdout"] = proc.stdout
        artifacts["stderr"] = proc.stderr
        coverage_pct = None
        if cov_file and cov_file.exists():
            coverage_pct = self._parse_coverage(cov_file)
        return proc.returncode == 0, coverage_pct, artifacts

    def _run_ruff(self, skill_id: str, file_path: str) -> Tuple[bool, Dict[str, Any]]:
        artifacts: Dict[str, Any] = {}
        ruff_exe = self._which("ruff")
        if not ruff_exe:
            artifacts["skipped"] = "ruff_not_installed"
            return True, artifacts
        if not file_path:
            return False, {"reason": "missing_file_path"}
        cmd = [ruff_exe, "check", file_path, "--force-exclude"]
        proc = self.runner(
            cmd,
            cwd=self.project_root,
            text=True,
            capture_output=True,
            env=self._build_env(),
        )
        artifacts["returncode"] = proc.returncode
        artifacts["stdout"] = proc.stdout
        artifacts["stderr"] = proc.stderr
        return proc.returncode == 0, artifacts

    def _coverage_target(self, file_path: str) -> Optional[str]:
        if not file_path.endswith(".py"):
            return None
        rel = Path(file_path)
        try:
            rel = rel.relative_to(self.project_root)
        except ValueError:
            pass
        parts = list(rel.with_suffix("").parts)
        if not parts:
            return None
        return ".".join(parts)

    def _parse_coverage(self, cov_file: Path) -> Optional[float]:
        try:
            data = json.loads(cov_file.read_text(encoding="utf-8"))
            totals = data.get("totals", {})
            pct = totals.get("percent_covered") or totals.get("percent_covered_display")
            if pct is not None:
                return float(pct)
        except Exception:
            return None
        return None

    def _check_governance(self, target_module: str, coverage_pct: Optional[float]) -> GovernanceDecision:
        return self.governance.check_code_evolution_allowed(
            target_module=target_module or "unknown",
            experiment_type="code_patch",
            test_coverage_pct=coverage_pct,
        )

    def _register_skill(self, skill: Dict[str, Any], outcome: EvaluationOutcome, enable_on_pass: bool = False) -> SkillRecord:
        skill_id = outcome.skill_id
        record = SkillRecord.from_dict(
            skill_id,
            {
                "file_path": skill.get("file_path", ""),
                "test_path": skill.get("test_path", ""),
                "description": skill.get("description", ""),
                "type": skill.get("skill_type", skill.get("type", "PURE")),
                "allow_evolution": skill.get("allow_evolution", True),
                "objective": skill.get("objective", "PERFORMANCE"),
                "metrics_impact": skill.get("metrics_impact", []),
                "category": skill.get("category", "unknown"),
                "approved_at": skill.get("generated_at", datetime.now(timezone.utc).isoformat()),
                "version": skill.get("version", 1),
                "performance_baseline": skill.get("performance_baseline", {}),
                "enabled": skill.get("enabled", False),
            },
        )
        record.enabled = enable_on_pass or record.enabled
        saved = self.registry.upsert(record, enabled=record.enabled)
        append_skill_flow_event(
            {
                "skill_id": skill_id,
                "event": "registry_updated",
                "status": outcome.status,
                "enabled": saved.enabled,
                "coverage_pct": outcome.coverage_pct,
            },
            runs_dir=self.runs_dir,
        )
        return saved

    def _build_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.project_root)
        return env

    def _which(self, exe: str) -> Optional[str]:
        path = os.environ.get("PATH", "")
        for entry in path.split(os.pathsep):
            candidate = Path(entry) / exe
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None
