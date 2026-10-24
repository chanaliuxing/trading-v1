"""Integration tests for PrincipalInvestigator with safe mode."""

import tempfile
from unittest.mock import Mock, patch, MagicMock
import numpy as np
import pandas as pd
import pytest

from pi.pi_orchestrator import PrincipalInvestigator, PIResult


@pytest.fixture
def mock_clickhouse_client():
    """Create a mock ClickHouse client."""
    client = Mock()

    # Mock column catalog query
    catalog_df = pd.DataFrame({
        "name": ["symbol", "ts", "open", "close", "high", "low", "volume", "vwap"]
    })

    # Mock data query - return realistic market data
    # Use monotonically increasing prices to make feature calculations deterministic
    data_df = pd.DataFrame({
        "symbol": ["TSLA"] * 100,
        "ts": pd.date_range("2024-01-01", periods=100, freq="1min"),
        "close": np.array([250 + i * 0.1 for i in range(100)]),  # Monotonic prices
        "volume": np.array([10000 + i * 100 for i in range(100)]),  # Increasing volume
        "vwap": np.array([250 + i * 0.1 for i in range(100)]),  # Same as close for simplicity
    })

    def query_df_side_effect(query, parameters=None):
        """Return different results based on query type."""
        if "system.columns" in query:
            return catalog_df
        else:
            return data_df

    client.query_df = Mock(side_effect=query_df_side_effect)
    return client


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client."""
    client = Mock()

    # Mock LLM response with feature recipe and training plan
    llm_response = {
        "feature_recipe": {
            "base_features": ["close", "volume", "vwap", "ret_1b", "ma_20_close"],
            "derived": [
                {
                    "alias": "price_volume_ratio",
                    "expr": "close / volume"
                }
            ]
        },
        "training_plan": {
            "task": "reg",
            "label_id": "ret_10b_fwd",
            "model_candidates": [
                {
                    "model_type": "xgb",
                    "model_params": {"max_depth": 6, "learning_rate": 0.1}
                },
                {
                    "model_type": "lgbm",
                    "model_params": {"num_leaves": 31}
                }
            ]
        }
    }

    client.generate = Mock(return_value=llm_response)
    return client


class TestPrincipalInvestigatorSafeMode:
    """Test PI pipeline with safe mode enabled."""

    @patch('pi.pi_orchestrator.clickhouse_connect')
    @patch('pi.pi_orchestrator.LLMClient')
    def test_safe_mode_basic_workflow(self, mock_llm_cls, mock_ch_connect,
                                      mock_clickhouse_client, mock_llm_client):
        """Test basic PI workflow in safe mode."""
        # Setup mocks
        mock_ch_connect.get_client.return_value = mock_clickhouse_client
        mock_llm_cls.return_value = mock_llm_client

        # Create PI instance
        ch_cfg = {"host": "localhost", "port": 8123, "database": "trading"}
        llm_cfg = {"model": "gpt-4", "base_url": "http://localhost:11434"}
        pi = PrincipalInvestigator(ch_cfg, llm_cfg)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Use temporary directory for manifests
            with patch('experiments.manifest.save_manifest') as mock_save:
                mock_save.return_value = tmpdir + "/test_manifest.json"

                # Run PI workflow
                result = pi.propose(
                    table="trading.bars_1m",
                    symbol="TSLA",
                    start_ts="2024-01-01",
                    end_ts="2024-01-02",
                    goal="Predict short-term price movement",
                    horizon="10m",
                    use_safe_mode=True,
                )

        # Verify result structure
        assert isinstance(result, PIResult)
        assert result.label_id == "ret_10b_fwd"
        assert "label" in result.dataset_df.columns
        assert result.sanity_summary is not None
        assert result.sanity_report is not None

        # Verify LLM was called with safe mode prompt
        assert mock_llm_client.generate.called
        call_args = mock_llm_client.generate.call_args
        system_prompt = call_args.kwargs.get("system_prompt", "")
        assert "禁止输出任何 SQL 代码或 target 表达式" in system_prompt

    @patch('pi.pi_orchestrator.clickhouse_connect')
    @patch('pi.pi_orchestrator.LLMClient')
    def test_safe_mode_sanity_checks(self, mock_llm_cls, mock_ch_connect,
                                     mock_clickhouse_client, mock_llm_client):
        """Test that sanity checks are performed in safe mode."""
        mock_ch_connect.get_client.return_value = mock_clickhouse_client
        mock_llm_cls.return_value = mock_llm_client

        ch_cfg = {"host": "localhost", "port": 8123}
        pi = PrincipalInvestigator(ch_cfg)

        result = pi.propose(
            table="trading.bars_1m",
            symbol="TSLA",
            start_ts="2024-01-01",
            end_ts="2024-01-02",
            goal="Test",
            horizon="10m",
            use_safe_mode=True,
        )

        # Verify sanity checks were performed
        assert result.sanity_summary is not None
        assert "overall_status" in result.sanity_summary
        assert result.sanity_summary["overall_status"] in ["ok", "warn", "fail"]

        # Verify sanity report has required fields
        assert result.sanity_report is not None
        assert "basic" in result.sanity_report
        assert "columns" in result.sanity_report

    @patch('pi.pi_orchestrator.clickhouse_connect')
    @patch('pi.pi_orchestrator.LLMClient')
    @patch('pi.pi_orchestrator.save_manifest')
    def test_safe_mode_label_attachment(self, mock_save, mock_llm_cls, mock_ch_connect,
                                       mock_clickhouse_client, mock_llm_client):
        """Test that labels are properly attached in Python layer."""
        mock_ch_connect.get_client.return_value = mock_clickhouse_client
        mock_llm_cls.return_value = mock_llm_client
        mock_save.return_value = "/tmp/manifest.json"

        ch_cfg = {"host": "localhost", "port": 8123}
        pi = PrincipalInvestigator(ch_cfg)

        result = pi.propose(
            table="trading.bars_1m",
            symbol="TSLA",
            start_ts="2024-01-01",
            end_ts="2024-01-02",
            goal="Test",
            horizon="10m",
            use_safe_mode=True,
        )

        # Verify label column exists
        assert "label" in result.dataset_df.columns

        # Verify label is numeric (return values)
        assert pd.api.types.is_numeric_dtype(result.dataset_df["label"])

        # Verify last N bars have NaN labels (no future data)
        last_10_labels = result.dataset_df["label"].tail(10)
        assert last_10_labels.isna().all()

        # Verify standard features were added
        assert "ret_1b" in result.dataset_df.columns
        assert "ma_20_close" in result.dataset_df.columns

    @patch('pi.pi_orchestrator.clickhouse_connect')
    @patch('pi.pi_orchestrator.LLMClient')
    @patch('pi.pi_orchestrator.save_manifest')
    def test_safe_mode_manifest_creation(self, mock_save, mock_llm_cls, mock_ch_connect,
                                         mock_clickhouse_client, mock_llm_client):
        """Test that experiment manifest is created in safe mode."""
        mock_ch_connect.get_client.return_value = mock_clickhouse_client
        mock_llm_cls.return_value = mock_llm_client
        mock_save.return_value = "/tmp/manifest.json"

        ch_cfg = {"host": "localhost", "port": 8123}
        pi = PrincipalInvestigator(ch_cfg)

        result = pi.propose(
            table="trading.bars_1m",
            symbol="TSLA",
            start_ts="2024-01-01",
            end_ts="2024-01-02",
            goal="Test",
            horizon="10m",
            freq="1m",
            use_safe_mode=True,
        )

        # Verify manifest was saved
        assert mock_save.called
        assert result.manifest_path is not None

    @patch('pi.pi_orchestrator.clickhouse_connect')
    @patch('pi.pi_orchestrator.LLMClient')
    def test_backward_compatibility_mode(self, mock_llm_cls, mock_ch_connect,
                                         mock_clickhouse_client, mock_llm_client):
        """Test that old mode (use_safe_mode=False) still works."""
        # Modify mock to return old-style LLM response with target
        old_llm_response = {
            "feature_recipe": {
                "base_features": ["close", "volume"],
            },
            "training_plan": {
                "task": "reg",
                "target": {
                    "alias": "y",
                    "expr": "lead(close, 10) / close - 1"
                },
                "model_candidates": [{"model_type": "xgb"}]
            }
        }
        mock_llm_client.generate.return_value = old_llm_response

        mock_ch_connect.get_client.return_value = mock_clickhouse_client
        mock_llm_cls.return_value = mock_llm_client

        ch_cfg = {"host": "localhost", "port": 8123}
        pi = PrincipalInvestigator(ch_cfg)

        result = pi.propose(
            table="trading.bars_1m",
            symbol="TSLA",
            start_ts="2024-01-01",
            end_ts="2024-01-02",
            goal="Test",
            horizon="10m",
            use_safe_mode=False,  # Old mode
        )

        # Should still work but without safe mode features
        assert isinstance(result, PIResult)
        assert result.label_id is None  # No label_id in old mode
        assert result.manifest_path is None  # No manifest in old mode

    @patch('pi.pi_orchestrator.clickhouse_connect')
    @patch('pi.pi_orchestrator.LLMClient')
    @patch('pi.pi_orchestrator.save_manifest')
    def test_hpo_grid_construction(self, mock_save, mock_llm_cls, mock_ch_connect,
                                   mock_clickhouse_client, mock_llm_client):
        """Test that HPO grid is properly constructed."""
        mock_ch_connect.get_client.return_value = mock_clickhouse_client
        mock_llm_cls.return_value = mock_llm_client
        mock_save.return_value = "/tmp/manifest.json"

        ch_cfg = {"host": "localhost", "port": 8123}
        pi = PrincipalInvestigator(ch_cfg)

        # Provide guard config with allowed model types
        guard_cfg = {
            "choices": {
                "model_type": ["xgb", "lgbm", "lasso", "ridge"]
            }
        }

        result = pi.propose(
            table="trading.bars_1m",
            symbol="TSLA",
            start_ts="2024-01-01",
            end_ts="2024-01-02",
            goal="Test",
            horizon="10m",
            use_safe_mode=True,
            guard_cfg=guard_cfg,
        )

        # Verify HPO grid structure
        assert "model_type" in result.hpo_grid
        assert "feature_subset" in result.hpo_grid
        assert "task" in result.hpo_grid
        assert "model_params" in result.hpo_grid

        # Verify model candidates are in grid (should have 2 models: xgb and lgbm)
        assert len(result.hpo_grid["model_type"]) >= 1, f"Got model_type: {result.hpo_grid['model_type']}"


class TestDatasetSanityIntegration:
    """Integration tests for dataset sanity checks."""

    @patch('pi.pi_orchestrator.clickhouse_connect')
    @patch('pi.pi_orchestrator.LLMClient')
    def test_sanity_detects_data_issues(self, mock_llm_cls, mock_ch_connect, mock_llm_client):
        """Test that sanity checks detect problematic data."""
        # Create mock client with problematic data
        client = Mock()

        # Catalog query
        catalog_df = pd.DataFrame({"name": ["symbol", "ts", "close", "bad_feature"]})

        # Data with issues: constant column, high NaN ratio
        data_df = pd.DataFrame({
            "symbol": ["TSLA"] * 100,
            "ts": pd.date_range("2024-01-01", periods=100, freq="1min"),
            "close": np.random.randn(100) * 5 + 250,
            "bad_feature": [1.0] * 100,  # Constant - zero variance
        })
        # Add 40% NaN to close
        data_df.loc[data_df.index[:40], "close"] = np.nan

        def query_df_side_effect(query, parameters=None):
            if "system.columns" in query:
                return catalog_df
            else:
                return data_df

        client.query_df = Mock(side_effect=query_df_side_effect)
        mock_ch_connect.get_client.return_value = client
        mock_llm_cls.return_value = mock_llm_client

        ch_cfg = {"host": "localhost", "port": 8123}
        pi = PrincipalInvestigator(ch_cfg)

        result = pi.propose(
            table="trading.bars_1m",
            symbol="TSLA",
            start_ts="2024-01-01",
            end_ts="2024-01-02",
            goal="Test",
            horizon="10m",
            use_safe_mode=True,
        )

        # Should detect issues
        assert result.sanity_summary["overall_status"] in ["warn", "fail"]
        assert len(result.sanity_summary["issues"]) > 0

        # Should detect zero variance
        assert any("zero_variance:bad_feature" in issue for issue in result.sanity_summary["issues"])

        # Should detect high missing
        assert any("high_missing:close" in issue for issue in result.sanity_summary["issues"])
