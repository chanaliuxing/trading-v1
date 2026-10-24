import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from libs.evolution.evolution_chamber import EvolutionChamber
from libs.skills.registry import SkillRegistry
from libs.skills.runtime import SkillRuntime

# Dummy skill content for testing
DUMMY_SKILL_CODE = """
def on_decision(decision, context):
    # Test hook: invert decision confidence
    if 'confidence' in decision:
        decision['confidence'] = 0.99
    return decision
"""

DUMMY_TEST_CODE = """
def test_dummy_logic():
    assert 1 + 1 == 2
"""

@pytest.fixture
def e2e_env():
    """Setup a temporary environment for E2E testing."""
    # Create temp dirs
    base = Path("tests/tmp_e2e")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    
    runs_dir = base / "runs"
    runs_dir.mkdir()
    (runs_dir / "memory").mkdir()
    (runs_dir / "evolution" / "results").mkdir(parents=True)
    
    config_dir = base / "config"
    config_dir.mkdir()
    
    libs_dir = base / "libs" / "skills" / "generated" / "test"
    libs_dir.mkdir(parents=True)
    
    # Create dummy registry
    reg_path = config_dir / "skill_registry.json"
    reg_path.write_text(json.dumps({"skills": {}}))
    
    # Create dummy skill files
    skill_path = libs_dir / "e2e_skill.py"
    skill_path.write_text(DUMMY_SKILL_CODE)
    
    test_path = libs_dir / "test_e2e_skill.py"
    test_path.write_text(DUMMY_TEST_CODE)
    
    yield {
        "base": base,
        "runs": runs_dir,
        "config": config_dir,
        "libs": libs_dir,
        "registry": reg_path,
        "queue": runs_dir / "memory" / "skill_approval_queue.jsonl",
        "skill_path": str(skill_path),
        "test_path": str(test_path)
    }
    
    # Cleanup
    # shutil.rmtree(base)

def test_meta_brain_e2e_flow(e2e_env):
    """
    End-to-End Test: Queue -> Approve -> Register -> Load -> Execute
    """
    print("\n[E2E] Starting Meta Brain E2E Flow Test...")
    
    # 1. Simulate Sleep Loop output (Queue a skill)
    skill_def = {
        "skill_id": "e2e_test_skill_v1",
        "file_path": e2e_env["skill_path"],
        "test_path": e2e_env["test_path"],
        "description": "E2E test skill",
        "type": "hooks",
        "enabled": True # Request enable
    }
    
    with open(e2e_env["queue"], "w") as f:
        f.write(json.dumps(skill_def) + "\n")
    
    print("[E2E] 1. Skill queued.")
    
    # 2. Run Evolution Chamber (Validation & Approval)
    # We mock governance to allow everything for this test
    from libs.evolution.governance import GovernanceDecision

    def mock_run_tests(skill_id, file_path, test_path):
        """Mock pytest to always pass with 100% coverage for E2E test."""
        return True, 100.0, {
            "returncode": 0,
            "stdout": "1 passed in 0.00s",
            "stderr": ""
        }

    with patch("libs.evolution.governance.EvolutionGovernance.check_code_evolution_allowed") as mock_gov, \
         patch("libs.evolution.evolution_chamber.EvolutionChamber._run_tests", side_effect=mock_run_tests):
        mock_gov.return_value = GovernanceDecision(allowed=True, reason="E2E Test")

        chamber = EvolutionChamber(
            queue_path=e2e_env["queue"],
            results_dir=e2e_env["runs"] / "evolution" / "results",
            registry_path=e2e_env["registry"],
            runs_dir=e2e_env["runs"]
        )

        chamber.min_coverage = 0.0  # Not needed since we mock 100%, but keep for clarity

        outcomes = chamber.process_queue(auto_register=True, enable_on_pass=True)
        
    assert len(outcomes) == 1
    assert outcomes[0].status == "approved"
    print(f"[E2E] 2. Skill approved: {outcomes[0].reason}")
    
    # 3. Verify Registry Update
    reg = SkillRegistry(e2e_env["registry"])
    skills = reg.list_enabled()
    assert "e2e_test_skill_v1" in skills
    assert skills["e2e_test_skill_v1"].enabled is True
    print("[E2E] 3. Registry updated and enabled.")
    
    # 4. Verify Runtime Loading & Execution
    # We need to temporarily add the temp libs dir to sys.path so importlib can find it
    # The skill path in registry is absolute/relative to cwd, we need to ensure it resolves
    import sys
    sys.path.insert(0, str(e2e_env["base"]))
    
    try:
        runtime = SkillRuntime(registry_path=e2e_env["registry"], runs_dir=e2e_env["runs"])
        
        # Verify hook execution
        dummy_decision = {"decision": "BUY", "confidence": 0.5}
        result = runtime.apply_decision_hooks(dummy_decision, context={})
        
        # Our dummy skill sets confidence to 0.99
        assert result["confidence"] == 0.99
        print(f"[E2E] 4. Runtime loaded skill and executed hook. Result confidence: {result['confidence']}")
        
    finally:
        sys.path.pop(0)

    print("[E2E] ✅ Test Complete: Meta Brain Cycle is Operational.")

if __name__ == "__main__":
    # Allow running directly
    pytest.main([__file__])
