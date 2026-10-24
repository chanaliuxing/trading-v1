"""Tests for experiment summary builder."""

import pytest

from experiments.types import Metrics, ExperimentRecord
from experiments.registry import append_experiment_record
from experiments.summary import build_experiment_summary_for_llm


class TestBuildExperimentSummary:
    """Test experiment summary builder."""

    def test_summary_empty_index(self, tmp_path):
        """Test summary with no experiments."""
        index_path = tmp_path / "empty.jsonl"

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        assert summary["status"] == "no_history"
        assert "No experiments found" in summary["message"]

    def test_summary_single_experiment(self, tmp_path):
        """Test summary with single experiment."""
        index_path = tmp_path / "index.jsonl"

        metrics = Metrics(sharpe=2.5, ic=0.15, trades=100)
        rec = ExperimentRecord(
            run_id="test_001",
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            model_type="xgb",
            feature_subset=["close"],
            params={},
            metrics=metrics,
            run_ts="2025-11-17T10:00:00Z",
        )
        append_experiment_record(rec, index_path)

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        assert summary["status"] == "ok"
        assert summary["num_experiments"] == 1
        assert len(summary["top_by_sharpe"]) == 1
        assert summary["top_by_sharpe"][0]["run_id"] == "test_001"
        assert summary["top_by_sharpe"][0]["sharpe"] == 2.5
        assert summary["sharpe_range"]["min"] == 2.5
        assert summary["sharpe_range"]["max"] == 2.5

    def test_summary_multiple_experiments(self, tmp_path):
        """Test summary with multiple experiments."""
        index_path = tmp_path / "index.jsonl"

        # Create 5 experiments with different metrics
        for i in range(5):
            metrics = Metrics(
                sharpe=1.0 + i * 0.5,
                ic=0.05 + i * 0.03,
                trades=80 + i * 10,
            )
            rec = ExperimentRecord(
                run_id=f"test_{i:03d}",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={"max_depth": 5 + i},
                metrics=metrics,
                run_ts=f"2025-11-17T{10+i}:00:00Z",
            )
            append_experiment_record(rec, index_path)

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        assert summary["status"] == "ok"
        assert summary["num_experiments"] == 5
        assert len(summary["top_by_sharpe"]) == 3

        # Top 3 should be highest sharpe (4, 3, 2)
        top_3 = summary["top_by_sharpe"]
        assert top_3[0]["run_id"] == "test_004"
        assert top_3[0]["sharpe"] == 3.0
        assert top_3[1]["run_id"] == "test_003"
        assert top_3[1]["sharpe"] == 2.5
        assert top_3[2]["run_id"] == "test_002"
        assert top_3[2]["sharpe"] == 2.0

        # Check ranges
        assert summary["sharpe_range"]["min"] == 1.0
        assert summary["sharpe_range"]["max"] == 3.0

    def test_summary_filters_by_all_criteria(self, tmp_path):
        """Test that summary only includes matching experiments."""
        index_path = tmp_path / "index.jsonl"

        # Create experiments with different parameters
        configs = [
            ("TSLA", "1m", "10m", "ret_10m_fwd", 2.5),  # Match
            ("TSLA", "1m", "10m", "ret_10m_fwd", 2.2),  # Match
            ("NVDA", "1m", "10m", "ret_10m_fwd", 3.0),  # Different symbol
            ("TSLA", "5m", "10m", "ret_10m_fwd", 2.8),  # Different freq
            ("TSLA", "1m", "5m", "ret_10m_fwd", 2.6),   # Different horizon
            ("TSLA", "1m", "10m", "ret_5m_fwd", 2.7),   # Different label
        ]

        for i, (symbol, freq, horizon, label_id, sharpe) in enumerate(configs):
            metrics = Metrics(sharpe=sharpe, ic=0.10)
            rec = ExperimentRecord(
                run_id=f"run_{i}",
                symbol=symbol,
                freq=freq,
                horizon=horizon,
                label_id=label_id,
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=metrics,
                run_ts="2025-11-17T10:00:00Z",
            )
            append_experiment_record(rec, index_path)

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        # Should only include first two experiments
        assert summary["num_experiments"] == 2
        assert len(summary["top_by_sharpe"]) == 2

    def test_summary_includes_all_fields(self, tmp_path):
        """Test that summary includes all expected fields."""
        index_path = tmp_path / "index.jsonl"

        metrics = Metrics(
            sharpe=2.5,
            ic=0.15,
            sortino=3.0,
            hit_rate=0.58,
            max_drawdown=-0.10,
            pnl=0.25,
            trades=150,
        )
        rec = ExperimentRecord(
            run_id="test_001",
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            model_type="xgb",
            feature_subset=["close", "volume"],
            params={"max_depth": 5},
            metrics=metrics,
            run_ts="2025-11-17T10:00:00Z",
        )
        append_experiment_record(rec, index_path)

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        # Check top experiment includes expected fields
        top_exp = summary["top_by_sharpe"][0]
        assert top_exp["run_id"] == "test_001"
        assert top_exp["model_type"] == "xgb"
        assert top_exp["sharpe"] == 2.5
        assert top_exp["ic"] == 0.15
        assert top_exp["hit_rate"] == 0.58
        assert top_exp["max_drawdown"] == -0.10
        assert top_exp["pnl"] == 0.25
        assert top_exp["trades"] == 150

    def test_summary_handles_none_metrics(self, tmp_path):
        """Test handling of None metric values."""
        index_path = tmp_path / "index.jsonl"

        # Create experiments with some None metrics
        for i in range(3):
            if i == 1:
                # Second experiment has None sharpe
                metrics = Metrics(sharpe=None, ic=0.10, trades=100)
            else:
                metrics = Metrics(sharpe=2.0 + i, ic=0.10 + i * 0.02, trades=100)

            rec = ExperimentRecord(
                run_id=f"test_{i}",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=metrics,
                run_ts="2025-11-17T10:00:00Z",
            )
            append_experiment_record(rec, index_path)

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        # Should still work - top_by_sharpe includes all 3 (even with None)
        assert summary["num_experiments"] == 3
        assert len(summary["top_by_sharpe"]) == 3
        # sharpe_range should only include non-None values
        assert summary["sharpe_range"]["min"] == 2.0
        assert summary["sharpe_range"]["max"] == 4.0

    def test_summary_top_3_ordering(self, tmp_path):
        """Test that top 3 are correctly ordered by sharpe."""
        index_path = tmp_path / "index.jsonl"

        # Create experiments with specific sharpe values
        sharpe_values = [1.5, 3.2, 2.1, 2.8, 1.9, 3.5, 2.5]
        for i, sharpe in enumerate(sharpe_values):
            metrics = Metrics(sharpe=sharpe, ic=0.10, trades=100)
            rec = ExperimentRecord(
                run_id=f"test_{i:03d}",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=metrics,
                run_ts="2025-11-17T10:00:00Z",
            )
            append_experiment_record(rec, index_path)

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        top_3 = summary["top_by_sharpe"]
        assert len(top_3) == 3

        # Should be ordered: 3.5, 3.2, 2.8
        assert top_3[0]["sharpe"] == 3.5
        assert top_3[1]["sharpe"] == 3.2
        assert top_3[2]["sharpe"] == 2.8

    def test_summary_with_all_none_sharpe(self, tmp_path):
        """Test summary when all experiments have None sharpe."""
        index_path = tmp_path / "index.jsonl"

        for i in range(3):
            metrics = Metrics(sharpe=None, ic=0.10, trades=100)
            rec = ExperimentRecord(
                run_id=f"test_{i}",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=metrics,
                run_ts="2025-11-17T10:00:00Z",
            )
            append_experiment_record(rec, index_path)

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        assert summary["num_experiments"] == 3
        # top_by_sharpe will include experiments with None sharpe
        assert len(summary["top_by_sharpe"]) == 3
        # But sharpe_range will be None since all values are None
        assert summary["sharpe_range"] is None

    def test_summary_top_by_ic(self, tmp_path):
        """Test that summary includes top_by_ic."""
        index_path = tmp_path / "index.jsonl"

        # Create experiments with different IC values
        for i in range(5):
            metrics = Metrics(
                sharpe=2.0,
                ic=0.05 + i * 0.03,
                trades=100,
            )
            rec = ExperimentRecord(
                run_id=f"test_{i:03d}",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=metrics,
                run_ts="2025-11-17T10:00:00Z",
            )
            append_experiment_record(rec, index_path)

        summary = build_experiment_summary_for_llm(
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            index_path=index_path,
        )

        # Check top_by_ic
        assert len(summary["top_by_ic"]) == 3
        top_by_ic = summary["top_by_ic"]
        # Should be ordered by IC: 0.17, 0.14, 0.11
        assert top_by_ic[0]["ic"] == pytest.approx(0.17, rel=1e-6)
        assert top_by_ic[1]["ic"] == pytest.approx(0.14, rel=1e-6)
        assert top_by_ic[2]["ic"] == pytest.approx(0.11, rel=1e-6)
