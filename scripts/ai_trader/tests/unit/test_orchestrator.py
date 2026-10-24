"""Unit tests for Orchestrator class"""
import json
import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import pytest
import yaml

from scripts.ai_trader.orchestrator import Orchestrator


class TestOrchestratorInit:
    """Test Orchestrator initialization"""

    def test_init_with_custom_config(self, mock_config, mock_health_report):
        """Test initialization with custom config path"""
        orch = Orchestrator(cfg_path=str(mock_config))

        assert orch.cfg is not None
        assert orch.paper_mode is True
        assert orch.brain_enabled is False  # Disabled in mock config

    def test_init_gates_created(self, mock_config):
        """Test that all gates are initialized"""
        orch = Orchestrator(cfg_path=str(mock_config))

        assert orch.health is not None
        assert orch.data is not None
        assert orch.exec is not None
        assert orch.llm_gate is not None

    def test_init_controllers_created(self, mock_config):
        """Test that controllers are initialized"""
        orch = Orchestrator(cfg_path=str(mock_config))

        assert orch.rs is not None  # RunSystemController
        assert orch.llm is not None  # LLMTraderController
        assert orch.ab is not None  # ABDeployer


class TestHPODeployGate:
    """Test HPO deployment gate logic"""

    def test_hpo_gate_all_pass(self, mock_config):
        """Test when all OOS metrics pass thresholds"""
        orch = Orchestrator(cfg_path=str(mock_config))

        oos = {
            "oos_sharpe": 1.25,
            "oos_avg_max_drawdown": 0.08,
            "oos_trades": 45
        }
        cfg = {
            "min_oos_sharpe": 1.10,
            "max_oos_mdd": 0.10,
            "min_oos_trades": 30
        }

        result = orch._hpo_deploy_gate(oos, cfg)

        assert result["ok"] is True
        assert len(result["reasons"]) == 0
        assert result["metrics"]["oos_sharpe"] == 1.25
        assert result["metrics"]["oos_mdd"] == 0.08
        assert result["metrics"]["oos_trades"] == 45

    def test_hpo_gate_sharpe_fail(self, mock_config):
        """Test when Sharpe ratio is below threshold"""
        orch = Orchestrator(cfg_path=str(mock_config))

        oos = {"oos_sharpe": 0.95, "oos_mdd": 0.08, "oos_trades": 50}
        cfg = {"min_oos_sharpe": 1.10, "max_oos_mdd": 0.10, "min_oos_trades": 30}

        result = orch._hpo_deploy_gate(oos, cfg)

        assert result["ok"] is False
        assert any("oos_sharpe<1.1" in r for r in result["reasons"])

    def test_hpo_gate_mdd_fail(self, mock_config):
        """Test when drawdown exceeds threshold"""
        orch = Orchestrator(cfg_path=str(mock_config))

        oos = {"sharpe": 1.25, "max_drawdown": 0.15, "trades": 50}
        cfg = {"min_oos_sharpe": 1.10, "max_oos_mdd": 0.10, "min_oos_trades": 30}

        result = orch._hpo_deploy_gate(oos, cfg)

        assert result["ok"] is False
        assert any("oos_mdd>0.1" in r for r in result["reasons"])

    def test_hpo_gate_trades_fail(self, mock_config):
        """Test when sample size is too small"""
        orch = Orchestrator(cfg_path=str(mock_config))

        oos = {"oos_sharpe": 1.25, "oos_mdd": 0.08, "oos_trades": 20}
        cfg = {"min_oos_sharpe": 1.10, "max_oos_mdd": 0.10, "min_oos_trades": 30}

        result = orch._hpo_deploy_gate(oos, cfg)

        assert result["ok"] is False
        assert any("oos_trades<30" in r for r in result["reasons"])

    def test_hpo_gate_fallback_keys(self, mock_config):
        """Test fallback to alternative OOS key names"""
        orch = Orchestrator(cfg_path=str(mock_config))

        # Use alternative key names (sharpe, mdd, n)
        oos = {"sharpe": 1.25, "mdd": 0.08, "n": 50}
        cfg = {"min_oos_sharpe": 1.10, "max_oos_mdd": 0.10, "min_oos_trades": 30}

        result = orch._hpo_deploy_gate(oos, cfg)

        assert result["ok"] is True
        assert result["metrics"]["oos_sharpe"] == 1.25
        assert result["metrics"]["oos_mdd"] == 0.08
        assert result["metrics"]["oos_trades"] == 50

    def test_hpo_gate_no_config(self, mock_config):
        """Test when no config thresholds provided"""
        orch = Orchestrator(cfg_path=str(mock_config))

        oos = {"oos_sharpe": 0.5, "oos_mdd": 0.30, "oos_trades": 5}
        cfg = {}

        result = orch._hpo_deploy_gate(oos, cfg)

        # Should pass when no thresholds configured
        assert result["ok"] is True


class TestDiscoverBestParams:
    """Test discovery of best HPO parameters"""

    def test_discover_best_params_found(self, mock_config, mock_gen_summary):
        """Test when gen_summary.json is found"""
        orch = Orchestrator(cfg_path=str(mock_config))

        # Change to temp directory with mock_gen_summary
        import os
        orig_cwd = os.getcwd()
        os.chdir(mock_gen_summary.parent.parent.parent.parent)

        try:
            result = orch._discover_best_params()

            assert result is not None
            assert "file" in result
            assert "best_params" in result
            assert "oos" in result
            assert result["best_params"]["rl.enabled"] is True
            assert result["oos"]["oos_sharpe"] == 1.25
        finally:
            os.chdir(orig_cwd)

    def test_discover_best_params_not_found(self, mock_config, temp_dir):
        """Test when no gen_summary.json exists"""
        orch = Orchestrator(cfg_path=str(mock_config))

        import os
        orig_cwd = os.getcwd()
        os.chdir(temp_dir)

        try:
            result = orch._discover_best_params()
            assert result is None
        finally:
            os.chdir(orig_cwd)


class TestBuildABInputs:
    """Test AB testing input construction"""

    def test_build_ab_from_registry(self, mock_config, temp_dir):
        """Test building AB inputs from existing registry"""
        orch = Orchestrator(cfg_path=str(mock_config))

        # Create mock registry
        registry = {
            "champion": {"id": "v1", "sharpe": 1.15, "mdd": 0.09},
            "challenger": {"id": "v2", "sharpe": 1.20, "mdd": 0.08}
        }
        reg_path = temp_dir / "scripts" / "ai_trader" / "registry" / "champion_challenger.json"
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps(registry))

        import os
        orig_cwd = os.getcwd()
        os.chdir(temp_dir)

        try:
            best = {
                "oos": {
                    "oos_sharpe": 1.30,
                    "oos_avg_max_drawdown": 0.07
                }
            }

            champ, chall = orch._build_ab_inputs(best)

            assert champ["id"] == "v1"
            assert champ["sharpe"] == 1.15
            assert champ["mdd"] == 0.09

            assert chall["id"] == "candidate"
            assert chall["sharpe"] == 1.30
            assert chall["mdd"] == 0.07
        finally:
            os.chdir(orig_cwd)

    def test_build_ab_no_registry(self, mock_config, temp_dir):
        """Test building AB inputs when no registry exists"""
        orch = Orchestrator(cfg_path=str(mock_config))

        import os
        orig_cwd = os.getcwd()
        os.chdir(temp_dir)

        try:
            best = {"oos": {"oos_sharpe": 1.25, "oos_avg_max_drawdown": 0.08}}
            champ, chall = orch._build_ab_inputs(best)

            # Should use default champion
            assert champ["id"] == "baseline"
            assert champ["sharpe"] == 1.0
            assert champ["mdd"] == 0.10

            assert chall["sharpe"] == 1.25
            assert chall["mdd"] == 0.08
        finally:
            os.chdir(orig_cwd)


class TestValidateDecisions:
    """Test decision validation logic"""

    def test_validate_start_morning_proceed(self, mock_config):
        """Test validating morning start decision to proceed"""
        orch = Orchestrator(cfg_path=str(mock_config))

        decision = {"proceed": True, "notes": "All gates passed"}
        result = orch._validate_start_morning_decision(decision)

        assert result["proceed"] is True
        assert "notes" in result

    def test_validate_start_morning_block(self, mock_config):
        """Test validating morning start decision to block"""
        orch = Orchestrator(cfg_path=str(mock_config))

        decision = {"proceed": False, "notes": "Data gate failed"}
        result = orch._validate_start_morning_decision(decision)

        assert result["proceed"] is False

    def test_validate_start_morning_invalid(self, mock_config):
        """Test validating invalid morning decision (missing proceed key)"""
        orch = Orchestrator(cfg_path=str(mock_config))

        decision = {"invalid": "data"}  # Missing "proceed" key
        result = orch._validate_start_morning_decision(decision)

        # When proceed key is missing, bool(None) = False
        assert result["proceed"] is False

    def test_validate_hpo_deploy_apply(self, mock_config):
        """Test validating HPO deploy decision to apply"""
        orch = Orchestrator(cfg_path=str(mock_config))

        decision = {
            "apply_params": True,
            "ab_weights": {"champion": 0.6, "challenger": 0.4},
            "notes": "Challenger shows improvement"
        }
        result = orch._validate_hpo_deploy_decision(decision)

        assert result["apply_params"] is True
        assert result["ab_weights"]["champion"] == 0.6
        assert result["ab_weights"]["challenger"] == 0.4
        assert "notes" in result

    def test_validate_hpo_deploy_reject(self, mock_config):
        """Test validating HPO deploy decision to reject"""
        orch = Orchestrator(cfg_path=str(mock_config))

        decision = {"apply_params": False, "notes": "Insufficient evidence"}
        result = orch._validate_hpo_deploy_decision(decision)

        assert result["apply_params"] is False

    def test_validate_hpo_deploy_normalize_weights(self, mock_config):
        """Test AB weights normalization with clamping"""
        orch = Orchestrator(cfg_path=str(mock_config))

        decision = {
            "apply_params": True,
            "ab_weights": {"champion": 2.0, "challenger": 3.0}  # Both > 1, will be clamped
        }
        result = orch._validate_hpo_deploy_decision(decision)

        # Should first clamp to [0,1], then normalize
        # 2.0 → 1.0, 3.0 → 1.0, then normalize: 1/(1+1) = 0.5 each
        assert abs(result["ab_weights"]["champion"] + result["ab_weights"]["challenger"] - 1.0) < 0.001

        champ = result["ab_weights"]["champion"]
        chall = result["ab_weights"]["challenger"]

        assert 0.0 <= champ <= 1.0
        assert 0.0 <= chall <= 1.0
        # After clamping and normalization, both should be 0.5
        assert abs(champ - 0.5) < 0.01
        assert abs(chall - 0.5) < 0.01

    def test_validate_hpo_deploy_clamp_weights(self, mock_config):
        """Test AB weights clamping to [0,1]"""
        orch = Orchestrator(cfg_path=str(mock_config))

        decision = {
            "apply_params": True,
            "ab_weights": {"champion": -0.5, "challenger": 1.5}  # Out of bounds
        }
        result = orch._validate_hpo_deploy_decision(decision)

        # Should clamp to [0,1] then normalize
        assert result["ab_weights"]["champion"] >= 0.0
        assert result["ab_weights"]["challenger"] <= 1.0


class TestNormalizePlan:
    """Test plan normalization and fallback logic"""

    def test_normalize_plan_complete(self, mock_config):
        """Test normalizing a complete plan"""
        orch = Orchestrator(cfg_path=str(mock_config))

        plan = {
            "session": "morning",
            "providers": [
                {"name": "deepseek_nvidia", "symbols": ["TSLA"], "interval_sec": 600}
            ]
        }
        mem = {}

        result = orch._normalize_or_build_plan("morning", plan, mem)

        assert result["session"] == "morning"
        assert len(result["providers"]) == 1
        assert result["providers"][0]["name"] == "deepseek_nvidia"
        assert result["providers"][0]["symbols"] == ["TSLA"]
        assert result["providers"][0]["wait_for"] == "premarket"  # Default for morning

    def test_normalize_plan_missing_providers(self, mock_config):
        """Test normalizing plan with missing providers"""
        orch = Orchestrator(cfg_path=str(mock_config))

        plan = {}
        mem = {
            "top_providers": [
                {"provider": "openai", "score": 0.85},
                {"provider": "gemini", "score": 0.78}
            ]
        }

        result = orch._normalize_or_build_plan("intraday", plan, mem)

        # Should use top providers from memory
        assert len(result["providers"]) == 2
        assert result["providers"][0]["name"] == "openai"
        assert result["providers"][1]["name"] == "gemini"

    def test_normalize_plan_fill_symbols_from_memory(self, mock_config):
        """Test filling symbols from memory"""
        orch = Orchestrator(cfg_path=str(mock_config))

        plan = {
            "providers": [{"name": "deepseek_nvidia"}]
        }
        mem = {
            "top_symbols": [
                {"provider": "deepseek_nvidia", "symbol": "TSLA"},
                {"provider": "deepseek_nvidia", "symbol": "AAPL"},
                {"provider": "deepseek_nvidia", "symbol": "NVDA"},
                {"provider": "deepseek_nvidia", "symbol": "MSFT"}
            ]
        }

        result = orch._normalize_or_build_plan("morning", plan, mem)

        # Should take top 3 symbols
        assert result["providers"][0]["symbols"] == ["TSLA", "AAPL", "NVDA"]

    def test_normalize_plan_default_fallback(self, mock_config):
        """Test fallback to defaults when no memory"""
        orch = Orchestrator(cfg_path=str(mock_config))

        plan = {}
        mem = {}

        result = orch._normalize_or_build_plan("overnight", plan, mem)

        # Should use default provider and symbol
        assert len(result["providers"]) == 1
        assert result["providers"][0]["name"] == "deepseek_nvidia"
        assert result["providers"][0]["symbols"] == ["TSLA"]
        assert result["providers"][0]["wait_for"] == "overnight"


class TestWriteTempBestParams:
    """Test writing temporary best params file"""

    def test_write_temp_best_params(self, mock_config, temp_dir):
        """Test writing best params to temp file"""
        # Override runs_dir
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        params = {
            "rl.enabled": True,
            "market_filter.conf_floor": 0.70
        }

        with patch('datetime.date') as mock_date:
            mock_date.today.return_value = datetime.date(2025, 10, 29)
            result_path = orch._write_temp_best_params(params)

        result_file = Path(result_path)
        assert result_file.exists()
        assert "best_params.json" in result_file.name

        written_params = json.loads(result_file.read_text())
        assert written_params == params


class TestDecisionContext:
    """Test brain decision context for modular multi-target learning."""

    def test_decision_context_includes_targets_and_compat_fields(self, mock_config):
        orch = Orchestrator(cfg_path=str(mock_config))
        orch._learning_target_ids = Mock(return_value=["qqq_intraday_001", "options_spread_001"])
        orch._primary_learning_target = Mock(return_value="qqq_intraday_001")

        ctx = orch._decision_context({"foo": "bar"})

        assert ctx["foo"] == "bar"
        assert ctx["primary_strategy_id"] == "qqq_intraday_001"
        assert ctx["strategy_id"] == "qqq_intraday_001"
        assert ctx["learning_targets"] == ["qqq_intraday_001", "options_spread_001"]
        assert ctx["targets"] == [
            {"id": "qqq_intraday_001", "is_primary": True},
            {"id": "options_spread_001", "is_primary": False},
        ]

    def test_brain_decide_uses_enriched_context(self, mock_config):
        orch = Orchestrator(cfg_path=str(mock_config))
        orch._learning_target_ids = Mock(return_value=["qqq_intraday_001"])
        orch._primary_learning_target = Mock(return_value="qqq_intraday_001")
        orch.brain = Mock()
        orch.brain.decide.return_value = {"proceed": True}

        out = orch._brain_decide("start_morning", {"gates": {"health": {"ok": True}}})

        assert out == {"proceed": True}
        orch.brain.decide.assert_called_once()
        action, used_ctx = orch.brain.decide.call_args[0]
        assert action == "start_morning"
        assert used_ctx["primary_strategy_id"] == "qqq_intraday_001"
        assert used_ctx["strategy_id"] == "qqq_intraday_001"
        assert used_ctx["targets"] == [{"id": "qqq_intraday_001", "is_primary": True}]
        assert used_ctx["gates"]["health"]["ok"] is True

    def test_learning_observability_fields(self, mock_config):
        orch = Orchestrator(cfg_path=str(mock_config))
        orch._learning_target_ids = Mock(return_value=["qqq_intraday_001", "options_spread_001"])
        orch._primary_learning_target = Mock(return_value="qqq_intraday_001")
        orch.learning.profile_for = Mock(return_value="qqq_intraday")
        orch.learning.adapter_name_for = Mock(return_value="generic")
        orch.learning.pipeline_version = Mock(return_value="learning-pipeline-v2")

        obs = orch._learning_observability()

        assert obs["strategy_id"] == "qqq_intraday_001"
        assert obs["learning_profile"] == "qqq_intraday"
        assert obs["adapter_name"] == "generic"
        assert obs["pipeline_version"] == "learning-pipeline-v2"
        assert obs["learning_targets"] == ["qqq_intraday_001", "options_spread_001"]


class TestLearningArtifactsIndex:
    """Test per-strategy learning artifact mirror and unified index."""

    def test_register_learning_artifact_updates_index(self, mock_config, temp_dir):
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))
        orch = Orchestrator(cfg_path=str(mock_config))

        dstr = "2025-10-29"
        artifact = Path(orch.runs_dir) / dstr / "hpo_decision.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('{"ok": true}', encoding="utf-8")

        orch._register_learning_artifact(
            artifact_name="hpo_decision",
            artifact_path=artifact,
            strategy_id="qqq_intraday_001",
            dstr=dstr,
        )

        index_path = Path(orch.runs_dir) / dstr / "learning" / "index.json"
        assert index_path.exists()
        idx = json.loads(index_path.read_text("utf-8"))
        assert idx["date"] == dstr
        assert idx["strategies"]["qqq_intraday_001"]["dir"] == "learning/qqq_intraday_001"
        assert idx["strategies"]["qqq_intraday_001"]["artifacts"]["hpo_decision"] == "hpo_decision.json"

    def test_mirror_learning_artifact_copies_and_indexes(self, mock_config, temp_dir):
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))
        orch = Orchestrator(cfg_path=str(mock_config))

        dstr = "2025-10-29"
        source = Path(orch.runs_dir) / dstr / "ml_review.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"score": 0.9}', encoding="utf-8")

        out = orch._mirror_learning_artifact(
            source_path=source,
            artifact_name="ml_review",
            strategy_id="qqq_intraday_001",
            dstr=dstr,
        )

        assert out is not None
        out_path = Path(out)
        assert out_path.exists()
        assert out_path.read_text("utf-8") == '{"score": 0.9}'
        assert str(out_path).endswith("/learning/qqq_intraday_001/ml_review.json")

        index_path = Path(orch.runs_dir) / dstr / "learning" / "index.json"
        idx = json.loads(index_path.read_text("utf-8"))
        assert idx["strategies"]["qqq_intraday_001"]["artifacts"]["ml_review"] == "learning/qqq_intraday_001/ml_review.json"


class TestApplyParamsScope:
    """Test multi-target writeback scope with isolation and aggregation."""

    def test_apply_params_with_scope_primary_default(self, mock_config):
        orch = Orchestrator(cfg_path=str(mock_config))
        orch._primary_learning_target = Mock(return_value="qqq_intraday_001")
        orch._apply_params_for_target = Mock()

        res = orch._apply_params_with_scope(hpo_params_path="/tmp/p.json")

        assert res["scope"] == "primary"
        assert res["applied"] == [{"strategy_id": "qqq_intraday_001"}]
        assert res["failed"] == []
        orch._apply_params_for_target.assert_called_once_with(
            hpo_params_path="/tmp/p.json",
            strategy_id="qqq_intraday_001",
            strategy_config=None,
            output_path=None,
        )

    def test_apply_params_with_scope_all_targets_isolates_failures(self, mock_config):
        cfg = yaml.safe_load(mock_config.read_text())
        cfg.setdefault("policies", {}).setdefault("learning", {})["writeback_scope"] = "all_targets"
        cfg["policies"]["learning"]["max_parallel_targets"] = 2
        mock_config.write_text(yaml.safe_dump(cfg))
        orch = Orchestrator(cfg_path=str(mock_config))
        orch._learning_target_ids = Mock(return_value=["qqq_intraday_001", "options_spread_001"])
        orch._primary_learning_target = Mock(return_value="qqq_intraday_001")

        def _apply(hpo_params_path, strategy_id, strategy_config=None, output_path=None):
            if strategy_id == "options_spread_001":
                raise RuntimeError("simulated apply failure")
            return None

        orch._apply_params_for_target = Mock(side_effect=_apply)
        res = orch._apply_params_with_scope(hpo_params_path="/tmp/p.json")

        assert res["scope"] == "all_targets"
        assert {"strategy_id": "qqq_intraday_001"} in res["applied"]
        assert any(x.get("strategy_id") == "options_spread_001" for x in res["failed"])
