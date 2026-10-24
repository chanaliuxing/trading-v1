import json
from datetime import datetime, timezone
from pathlib import Path

from libs.evolution.evolution_chamber import EvolutionChamber
from libs.evolution.governance import GovernanceDecision
from libs.skills.runtime import SkillRuntime


class _AllowAllGovernance:
    def __init__(self):
        self.recorded: list[dict] = []

    def check_code_evolution_allowed(self, target_module: str, experiment_type: str, test_coverage_pct=None):
        return GovernanceDecision(
            allowed=True,
            reason="ok",
            gate_triggered=None,
            metadata={"target_module": target_module, "experiment_type": experiment_type, "coverage": test_coverage_pct},
        )

    def record_patch_submission(self, metadata):
        self.recorded.append(metadata)


def _runner_with_coverage(coverage_pct: float):
    def _runner(cmd, cwd=None, text=True, capture_output=True, env=None):
        cov_path = None
        for token in cmd:
            if isinstance(token, str) and token.startswith("--cov-report=") and "json:" in token:
                cov_path = token.split("json:", 1)[1]
                break
        if cov_path:
            cov_file = Path(cov_path)
            cov_file.parent.mkdir(parents=True, exist_ok=True)
            cov_file.write_text(json.dumps({"totals": {"percent_covered": coverage_pct}}), encoding="utf-8")

        class Proc:
            def __init__(self):
                self.returncode = 0
                self.stdout = "ok"
                self.stderr = ""

        return Proc()

    return _runner


def _write_skill(tmp_path: Path) -> Path:
    skill_path = tmp_path / "libs" / "skills" / "generated" / "risk" / "meta_skill.py"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        """def on_plan(plan, ctx):
    plan["skills_injected"] = plan.get("skills_injected", 0) + 1
    return plan


def on_decision(decision, ctx):
    decision["decision"] = "SELL"
    decision["quantity"] = max(1, int(decision.get("quantity") or 0) - 1)
    decision["note"] = "decision_hook"
    return decision


def adjust_risk(payload, ctx):
    decision = dict(payload.get("decision") or {})
    decision["quantity"] = 1
    payload["decision"] = decision
    payload["risk_flag"] = ctx.get("symbol")
    return payload
""",
        encoding="utf-8",
    )
    return skill_path


def test_meta_brain_skill_pipeline_round_trip(tmp_path):
    queue = tmp_path / "queue.jsonl"
    results = tmp_path / "results"
    registry = tmp_path / "registry.json"
    runs_dir = tmp_path / "runs"

    skill_path = _write_skill(tmp_path)
    test_path = tmp_path / "tests" / "unit" / "skills" / "test_meta_skill.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")

    queue.write_text(
        json.dumps(
            {
                "skill_id": "meta_skill",
                "file_path": str(skill_path),
                "test_path": str(test_path),
                "description": "meta brain integration skill",
                "category": "risk_control",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    chamber = EvolutionChamber(
        queue_path=queue,
        results_dir=results,
        registry_path=registry,
        runs_dir=runs_dir,
        governance=_AllowAllGovernance(),
        runner=_runner_with_coverage(coverage_pct=93.0),
    )
    chamber._which = lambda *_: None  # Skip ruff for isolated test

    outcomes = chamber.process_queue(auto_register=True, enable_on_pass=True)
    assert outcomes and outcomes[0].status == "approved"
    registry_payload = json.loads(registry.read_text(encoding="utf-8"))
    assert registry_payload["skills"]["meta_skill"]["enabled"] is True

    runtime = SkillRuntime(registry_path=registry, runs_dir=runs_dir)
    runtime.refresh()

    plan = runtime.apply_plan_hooks({"session": "overnight"})
    assert plan.get("skills_injected") == 1

    decision = runtime.apply_decision_hooks({"decision": "BUY", "quantity": 3, "symbol": "TSLA"}, context={"symbol": "TSLA"})
    assert decision["decision"] == "SELL"
    assert decision["quantity"] == 2
    assert decision["note"] == "decision_hook"

    risk = runtime.apply_risk_hooks({"decision": decision, "symbol": "TSLA"}, context={"symbol": "TSLA"})
    assert risk["decision"]["quantity"] == 1
    assert risk["risk_flag"] == "TSLA"

    day_dir = runs_dir / datetime.now(timezone.utc).date().isoformat()
    flow_file = day_dir / "skill_flow.jsonl"
    assert flow_file.exists()
    lines = flow_file.read_text(encoding="utf-8").splitlines()
    assert any("evaluation_started" in line for line in lines)
    assert any("evaluation_completed" in line for line in lines)
    assert any("loaded" in line for line in lines)
    assert any("on_decision_applied" in line for line in lines)
    assert any("adjust_risk_applied" in line for line in lines)
