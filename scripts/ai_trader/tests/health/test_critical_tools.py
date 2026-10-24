"""Critical tools health check tests - ensures AI Brain dependencies work"""
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import subprocess


class TestReflectionToolchain:
    """Test reflection toolchain that AI Brain depends on"""

    def test_consistency_job_imports(self):
        """Test consistency_job can be imported"""
        try:
            from scripts.ai_trader.reflection import consistency_job
            assert consistency_job is not None
        except ImportError as e:
            pytest.fail(f"consistency_job import failed: {e}")

    def test_analyzer_imports(self):
        """Test analyzer can be imported"""
        try:
            from scripts.ai_trader.reflection import analyzer
            assert analyzer is not None
        except ImportError as e:
            pytest.fail(f"analyzer import failed: {e}")

    def test_report_builder_imports(self):
        """Test report_builder can be imported"""
        try:
            from scripts.ai_trader.reflection import report_builder
            assert report_builder is not None
        except ImportError as e:
            pytest.fail(f"report_builder import failed: {e}")

    def test_reflection_module_has_extract_claims(self):
        """Test reflection module has critical functions"""
        from scripts.ai_trader.evaluators import reflection

        assert hasattr(reflection, 'extract_claims')
        assert hasattr(reflection, 'check_claims')

    def test_extract_claims_basic(self):
        """Test claim extraction works"""
        from scripts.ai_trader.evaluators.reflection import extract_claims

        text = "Price is above VWAP and rvol > 2"
        claims = extract_claims(text)

        assert isinstance(claims, list)
        assert len(claims) > 0

    def test_check_claims_basic(self):
        """Test claim checking works"""
        from scripts.ai_trader.evaluators.reflection import check_claims

        claims = [{"rule": "above_vwap", "stated": True}]
        features = {"close": 100, "vwap": 95}

        result = check_claims(claims, features)

        assert "consistency" in result
        assert isinstance(result["consistency"], float)


class TestEvaluatorTools:
    """Test evaluator tools that measure performance"""

    def test_scorecard_imports(self):
        """Test scorecard can be imported"""
        try:
            from scripts.ai_trader.evaluators import scorecard
            assert scorecard is not None
        except ImportError as e:
            pytest.fail(f"scorecard import failed: {e}")


class TestPortfolioTools:
    """Test portfolio management tools"""

    def test_overlay_imports(self):
        """Test overlay module imports"""
        try:
            from scripts.ai_trader.portfolio import overlay
            assert overlay is not None
        except ImportError as e:
            pytest.fail(f"overlay import failed: {e}")

    def test_optimizer_imports(self):
        """Test optimizer module imports"""
        try:
            from scripts.ai_trader.portfolio import optimizer
            assert optimizer is not None
        except ImportError as e:
            pytest.fail(f"optimizer import failed: {e}")


class TestContextAnalyzer:
    """Test context analyzer for AI Brain"""

    def test_context_analyzer_imports(self):
        """Test context_analyzer can be imported"""
        try:
            from scripts.ai_trader.context import context_analyzer
            assert context_analyzer is not None
        except ImportError as e:
            pytest.fail(f"context_analyzer import failed: {e}")


class TestEvolverTools:
    """Test evolver tools for prompt optimization"""

    def test_prompt_evolver_imports(self):
        """Test prompt_evolver can be imported"""
        try:
            from scripts.ai_trader.evolvers import prompt_evolver
            assert prompt_evolver is not None
        except ImportError as e:
            pytest.fail(f"prompt_evolver import failed: {e}")

    def test_decision_engine_imports(self):
        """Test decision_engine can be imported"""
        try:
            from scripts.ai_trader.evolvers import decision_engine
            assert decision_engine is not None
        except ImportError as e:
            pytest.fail(f"decision_engine import failed: {e}")


class TestRLTools:
    """Test RL evaluation tools"""

    def test_rl_eval_imports(self):
        """Test RL eval can be imported"""
        try:
            from scripts.ai_trader.rl import eval_rl
            assert eval_rl is not None
        except ImportError as e:
            pytest.fail(f"eval_rl import failed: {e}")


class TestMLflowIntegration:
    """Test MLflow integration"""

    def test_mlflow_utils_imports(self):
        """Test mlflow_utils can be imported"""
        try:
            from scripts.ai_trader import mlflow_utils
            assert mlflow_utils is not None
        except ImportError as e:
            pytest.fail(f"mlflow_utils import failed: {e}")


class TestCLITools:
    """Test CLI entry points"""

    def test_cli_imports(self):
        """Test CLI can be imported"""
        try:
            from scripts.ai_trader import cli
            assert cli is not None
        except ImportError as e:
            pytest.fail(f"cli import failed: {e}")

    def test_cli_has_commands(self):
        """Test CLI has expected commands"""
        from scripts.ai_trader.cli import cli

        # CLI should have commands
        assert hasattr(cli, 'commands')


class TestAutoLoop:
    """Test auto loop functionality"""

    def test_auto_loop_imports(self):
        """Test auto_loop can be imported"""
        try:
            from scripts.ai_trader import auto_loop
            assert auto_loop is not None
        except ImportError as e:
            pytest.fail(f"auto_loop import failed: {e}")


class TestFlowOrchestration:
    """Test Prefect flow integration"""

    def test_prefect_evening_imports(self):
        """Test Prefect evening flow imports"""
        try:
            from scripts.ai_trader.flows import prefect_evening
            assert prefect_evening is not None
        except ImportError as e:
            # Prefect may not be installed, that's OK
            pytest.skip(f"Prefect not available: {e}")

    def test_deploy_evening_imports(self):
        """Test deploy evening imports"""
        try:
            from scripts.ai_trader.flows import deploy_evening
            assert deploy_evening is not None
        except ImportError as e:
            pytest.skip(f"Deploy evening not available: {e}")


class TestCriticalDependencies:
    """Test critical external dependencies"""

    def test_llm_trader_imports(self):
        """Test llm_trader package is available"""
        try:
            import llm_trader
            assert llm_trader is not None
        except ImportError as e:
            pytest.fail(f"llm_trader package not available: {e}")

    def test_clickhouse_client_available(self):
        """Test ClickHouse client can be imported"""
        try:
            from libs.db.clickhouse import ClickHouseDB
            assert ClickHouseDB is not None
        except ImportError as e:
            pytest.skip(f"ClickHouse not configured: {e}")

    def test_postgres_client_available(self):
        """Test PostgreSQL client can be imported"""
        try:
            from libs.db.postgres import PostgresDB
            assert PostgresDB is not None
        except ImportError as e:
            pytest.skip(f"PostgreSQL not configured: {e}")


class TestSystemCommands:
    """Test system commands can execute"""

    def test_python_available(self):
        """Test Python is available"""
        result = subprocess.run(["python", "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "Python" in result.stdout or "Python" in result.stderr

    def test_working_directory_accessible(self):
        """Test working directory is accessible"""
        cwd = Path.cwd()
        assert cwd.exists()
        assert cwd.is_dir()

    def test_scripts_directory_exists(self):
        """Test scripts/ai_trader directory exists"""
        scripts_dir = Path("scripts/ai_trader")
        assert scripts_dir.exists()
        assert scripts_dir.is_dir()


class TestConfigurationFiles:
    """Test configuration files are accessible"""

    def test_ai_trader_yaml_exists(self):
        """Test ai_trader.yaml config file exists"""
        config_paths = [
            Path("scripts/ai_trader/ai_trader.yaml"),
            Path("config/ai_trader.yaml"),
            Path("ai_trader.yaml")
        ]

        exists = any(p.exists() for p in config_paths)
        if not exists:
            pytest.skip("ai_trader.yaml not found (may be environment-specific)")


class TestMemoryPersistence:
    """Test memory persistence can write"""

    def test_episode_store_can_build_episode(self, temp_dir):
        """Test episode building without DB"""
        from scripts.ai_trader.memory import episode_store

        episode = episode_store.build_episode(
            runs_dir=str(temp_dir),
            session="test",
            d="2025-10-29"
        )

        assert episode is not None
        assert episode["session"] == "test"
        assert episode["date"] == "2025-10-29"

    def test_aggregator_handles_no_db(self):
        """Test aggregator handles missing ClickHouse gracefully"""
        from scripts.ai_trader.memory import aggregator

        with patch('scripts.ai_trader.memory.aggregator.ClickHouseDB', None):
            result = aggregator.top_providers("morning")
            assert result == []


class TestBrainToolchain:
    """Test complete Brain toolchain end-to-end"""

    def test_brain_can_initialize(self, temp_dir):
        """Test Brain can initialize"""
        from scripts.ai_trader.brain.brain import ControlBrain

        brain = ControlBrain(runs_dir=str(temp_dir))
        assert brain is not None
        assert brain.runs_dir == temp_dir

    def test_orchestrator_can_initialize(self, temp_dir, mock_config):
        """Test Orchestrator can initialize with config"""
        from scripts.ai_trader.orchestrator import Orchestrator

        orch = Orchestrator(cfg_path=str(mock_config))
        assert orch is not None
        assert orch.cfg is not None

    def test_gates_can_initialize(self, mock_config, mock_health_report):
        """Test all gates can initialize"""
        from scripts.ai_trader.orchestrator import Orchestrator

        orch = Orchestrator(cfg_path=str(mock_config))

        assert orch.health is not None
        assert orch.data is not None
        assert orch.exec is not None
        assert orch.llm_gate is not None

    def test_controllers_can_initialize(self, mock_config):
        """Test all controllers can initialize"""
        from scripts.ai_trader.orchestrator import Orchestrator

        orch = Orchestrator(cfg_path=str(mock_config))

        assert orch.rs is not None
        assert orch.llm is not None
        assert orch.ab is not None
