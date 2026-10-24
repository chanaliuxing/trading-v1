import json
from pathlib import Path
from datetime import date

from libs.evolution.evolution_chamber import EvolutionChamber
from libs.evolution.governance import GovernanceDecision


class _FakeGovernance:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.recorded = []

    def check_code_evolution_allowed(self, target_module: str, experiment_type: str, test_coverage_pct=None):
        return GovernanceDecision(
            allowed=self.allowed,
            reason="ok",
            gate_triggered=None,
            metadata={"target_module": target_module, "experiment_type": experiment_type, "coverage": test_coverage_pct},
        )

    def record_patch_submission(self, metadata):
        self.recorded.append(metadata)


def _make_runner(coverage_pct: float):
    def _runner(cmd, cwd=None, text=True, capture_output=True, env=None):
        cov_path = None
        for token in cmd:
            if isinstance(token, str) and token.startswith("--cov-report=") and "json:" in token:
                cov_path = token.split("json:", 1)[1]
                break
        if cov_path:
            cov_file = Path(cov_path)
            cov_file.parent.mkdir(parents=True, exist_ok=True)
            cov_file.write_text(json.dumps({"totals": {"percent_covered": coverage_pct}}))

        class Proc:
            def __init__(self):
                self.returncode = 0
                self.stdout = "ok"
                self.stderr = ""

        return Proc()

    return _runner


def test_process_queue_auto_register(tmp_path):
    queue = tmp_path / "queue.jsonl"
    results = tmp_path / "results"
    registry = tmp_path / "registry.json"
    runs_dir = tmp_path / "runs"
    skill_file = tmp_path / "libs" / "skills" / "generated" / "alpha.py"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text("def on_plan(plan, ctx):\n    plan['alpha'] = True\n    return plan\n")

    queue.write_text(
        json.dumps(
            {
                "skill_id": "alpha_skill",
                "file_path": str(skill_file),
                "test_path": "tests/unit/skills/test_alpha.py",
                "description": "demo skill",
                "skill_type": "PURE",
            }
        )
        + "\n"
    )

    chamber = EvolutionChamber(
        queue_path=queue,
        results_dir=results,
        registry_path=registry,
        runs_dir=runs_dir,
        governance=_FakeGovernance(),
        runner=_make_runner(coverage_pct=95.0),
    )
    chamber._which = lambda _: None  # Skip ruff if not installed

    outcomes = chamber.process_queue(auto_register=True, enable_on_pass=True)
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == "approved"
    assert outcome.coverage_pct == 95.0

    registry_payload = json.loads(registry.read_text())
    skill_entry = registry_payload["skills"]["alpha_skill"]
    assert skill_entry["enabled"] is True
    assert (results / "alpha_skill.json").exists()

    flow_files = list((runs_dir).rglob("skill_flow.jsonl"))
    assert flow_files, "skill_flow.jsonl should be written for lifecycle audit"


def test_rejects_low_coverage(tmp_path):
    chamber = EvolutionChamber(
        queue_path=tmp_path / "queue.jsonl",
        results_dir=tmp_path / "results",
        registry_path=tmp_path / "registry.json",
        runs_dir=tmp_path / "runs",
        governance=_FakeGovernance(),
        runner=_make_runner(coverage_pct=50.0),
    )
    chamber._which = lambda _: None

    outcome = chamber.evaluate_skill(
        {"skill_id": "lowcov", "file_path": "libs/skills/generated/lowcov.py", "test_path": "tests/test_lowcov.py"}
    )
    assert outcome.status == "rejected"
    assert "coverage_below_threshold" in outcome.reason
