
import json
import sys
import types
import yaml
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Since Orchestrator has a complex __init__, we will mock its dependencies
# before importing it.
@pytest.fixture
def mock_dependencies(monkeypatch):
    # Mock gates and controllers that are instantiated in Orchestrator.__init__
    monkeypatch.setattr('scripts.ai_trader.orchestrator.HealthGate', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.DataGate', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.DataQualityGate', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.FeatureFreshnessGate', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.RealtimeFeatureFreshnessGate', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.ExecGate', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.LLMGate', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.RunSystemController', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.LLMTraderController', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.ABDeployer', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.ControlBrain', MagicMock())
    # Mock methods that perform file I/O or external checks
    monkeypatch.setattr('scripts.ai_trader.orchestrator.Orchestrator._cleanup_stale_locks', MagicMock())
    monkeypatch.setattr('scripts.ai_trader.orchestrator.Orchestrator._validate_critical_config', MagicMock())


@pytest.fixture
def orchestrator_instance(tmp_path, mock_dependencies):
    """Creates a valid Orchestrator instance with a temporary config."""
    config_content = {
        "paths": {
            "runs_dir": str(tmp_path),
            "health_report": str(tmp_path / "health.json")
        },
        "policies": {
            "propose_plan": {},
            "gates": {},
            "tools": {},
            "health": {},
            "premarket": {},
            "hpo_deploy": {},
            "hpo_budget": {},
            "prompts": {},
        },
        "brain": {"enabled": True},
        "clickhouse": {},
        "commands": {},
        "modes": {},
        "providers": {},
        "mlflow": {"enabled": False},
    }
    config_path = tmp_path / "ai_trader.yaml"
    config_path.write_text(yaml.dump(config_content))
    
    from scripts.ai_trader.orchestrator import Orchestrator
    # This instantiation should now work because dependencies are mocked
    return Orchestrator(cfg_path=str(config_path))


def test_discover_best_params_extracts_diagnostics_from_gen_summary(orchestrator_instance, tmp_path):
    """
    Tests that _discover_best_params correctly finds the latest gen_summary.json,
    parses the model_meta JSON string, and extracts the diagnostics dictionary.
    """
    # 1. Create a dummy gen_summary.json file
    diagnostics_data = {"label_stats": {"mean": 0.1}, "feature_ic_spearman": {"feat1": 0.2}}
    model_meta_str = json.dumps({"diagnostics": diagnostics_data})
    
    summary_content = {
        "rows": [
            {"phase": "IS", "params": "{}", "model_meta": "{}"}, # A row that doesn't match
            {"phase": "OOS", "params": '{"param": "best"}', "model_meta": model_meta_str}
        ],
        "best_params": {"param": "best"},
        "oos_summary": {"sharpe": 1.5}
    }
    
    summary_dir = tmp_path / "alpha_out" / "daily" / "gen_0"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / "gen_summary.json"
    summary_path.write_text(json.dumps(summary_content))

    # 2. Patch glob to find our dummy file
    with patch('glob.glob', return_value=[str(summary_path)]):
        result = orchestrator_instance._discover_best_params()

    # 3. Assert the result
    assert result is not None
    assert "diagnostics" in result
    assert result["diagnostics"] == diagnostics_data
    assert result["best_params"] == {"param": "best"}


def test_ml_ctx_includes_alpha_best_diagnostics_for_review_ml(orchestrator_instance, tmp_path):
    """
    Tests that the context passed to brain.review_ml includes the diagnostics
    discovered from the best HPO run.
    """
    # 1. Mock the methods called by evening_learning
    diagnostics_data = {"label_stats": {"mean": 0.2}, "conf_stats": {"mean": 0.7}}
    best_params_data = {
        "file": "dummy_summary.json",
        "best_params": {"param": "best"},
        "oos": {"oos_sharpe": 2.0},
        "diagnostics": diagnostics_data
    }

    orchestrator_instance._discover_best_params = MagicMock(return_value=best_params_data)
    orchestrator_instance.brain.review_ml = MagicMock(return_value={"decision": "proceed"})
    
    # Mock other potentially problematic calls within evening_learning
    orchestrator_instance._run_cmd = MagicMock()
    orchestrator_instance._hpo_deploy_gate = MagicMock(return_value={"ok": True})
    orchestrator_instance._write_temp_best_params = MagicMock(return_value="dummy_path")
    orchestrator_instance._build_ab_inputs = MagicMock(return_value=({}, {}))
    orchestrator_instance.rs.apply_params = MagicMock()
    orchestrator_instance.ab.propose = MagicMock()
    orchestrator_instance.ab.save_registry = MagicMock()
    orchestrator_instance._current_label_profile = MagicMock(return_value="test_profile")
    orchestrator_instance._load_label_profiles_brief = MagicMock(return_value={})
    
    # Mock alpha wrapper/degrade modules to avoid path/import coupling.
    mock_run_alpha_loop_safe = MagicMock(return_value={
            "mem_stats": {"peak_rss_mb": 100},
            "best_oos": best_params_data # Directly return the data needed for best_oos
    })
    alpha_loop_wrapper_mod = types.ModuleType("utils.alpha_loop_wrapper")
    alpha_loop_wrapper_mod.apply_config_degradation = MagicMock(return_value=None)
    alpha_loop_wrapper_mod.run_alpha_loop_safe = mock_run_alpha_loop_safe

    class _FakeDegradeLadder:
        def apply(self, *_args, **_kwargs):
            return {}

    degrade_ladder_mod = types.ModuleType("utils.degrade_ladder")
    degrade_ladder_mod.DegradeLadder = _FakeDegradeLadder

    with patch.dict(sys.modules, {
        "utils.alpha_loop_wrapper": alpha_loop_wrapper_mod,
        "utils.degrade_ladder": degrade_ladder_mod,
    }):
        # 2. Run the evening_learning sequence
        orchestrator_instance.evening_learning()

    # 3. Assert that brain.review_ml was called with the correct context
    orchestrator_instance.brain.review_ml.assert_called_once()
    call_args, _ = orchestrator_instance.brain.review_ml.call_args
    ml_ctx = call_args[0]

    assert "alpha_best" in ml_ctx
    assert "diagnostics" in ml_ctx["alpha_best"]
    # Use a helper to compare nested dicts to avoid order issues
    assert ml_ctx["alpha_best"]["diagnostics"] == diagnostics_data
