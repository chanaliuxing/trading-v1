"""Tests for experiment registry."""

import tempfile
from pathlib import Path

import pytest

from experiments.types import Metrics, ExperimentRecord
from experiments.registry import (
    append_experiment_record,
    load_experiment_records,
    top_n_by_metric,
)


class TestAppendExperimentRecord:
    """Test appending experiment records."""

    def test_append_single_record(self, tmp_path):
        """Test appending a single record."""
        index_path = tmp_path / "test_index.jsonl"

        metrics = Metrics(sharpe=2.5, trades=100)
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

        assert index_path.exists()
        lines = index_path.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_append_multiple_records(self, tmp_path):
        """Test appending multiple records."""
        index_path = tmp_path / "test_index.jsonl"

        for i in range(5):
            metrics = Metrics(sharpe=1.0 + i * 0.5, trades=100 + i * 10)
            rec = ExperimentRecord(
                run_id=f"test_{i:03d}",
                symbol="NVDA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=metrics,
                run_ts=f"2025-11-17T{10+i}:00:00Z",
            )
            append_experiment_record(rec, index_path)

        lines = index_path.read_text().strip().split("\n")
        assert len(lines) == 5

    def test_append_creates_parent_directory(self, tmp_path):
        """Test that append creates parent directories."""
        index_path = tmp_path / "subdir" / "experiments" / "index.jsonl"

        metrics = Metrics(sharpe=1.5)
        rec = ExperimentRecord(
            run_id="test_001",
            symbol="AAPL",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            model_type="lgbm",
            feature_subset=["close"],
            params={},
            metrics=metrics,
            run_ts="2025-11-17T10:00:00Z",
        )

        append_experiment_record(rec, index_path)

        assert index_path.exists()
        assert index_path.parent.exists()


class TestLoadExperimentRecords:
    """Test loading experiment records."""

    def test_load_from_nonexistent_file(self, tmp_path):
        """Test loading from non-existent file returns empty list."""
        index_path = tmp_path / "nonexistent.jsonl"
        records = load_experiment_records(index_path)

        assert records == []

    def test_load_all_records(self, tmp_path):
        """Test loading all records without filters."""
        index_path = tmp_path / "test_index.jsonl"

        # Write 3 records
        for i in range(3):
            metrics = Metrics(sharpe=2.0 + i)
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
                run_ts=f"2025-11-17T{10+i}:00:00Z",
            )
            append_experiment_record(rec, index_path)

        records = load_experiment_records(index_path)
        assert len(records) == 3

    def test_load_filtered_by_symbol(self, tmp_path):
        """Test filtering by symbol."""
        index_path = tmp_path / "test_index.jsonl"

        # Write records for different symbols
        for symbol in ["TSLA", "NVDA", "TSLA", "AAPL"]:
            metrics = Metrics(sharpe=2.0)
            rec = ExperimentRecord(
                run_id=f"test_{symbol}",
                symbol=symbol,
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

        records = load_experiment_records(index_path, symbol="TSLA")
        assert len(records) == 2
        assert all(r.symbol == "TSLA" for r in records)

    def test_load_filtered_by_multiple_criteria(self, tmp_path):
        """Test filtering by multiple criteria."""
        index_path = tmp_path / "test_index.jsonl"

        # Write various records
        configs = [
            ("TSLA", "1m", "10m", "ret_10m_fwd"),
            ("TSLA", "1m", "10m", "ret_10m_fwd"),  # Should match
            ("TSLA", "1m", "5m", "ret_10m_fwd"),   # Different horizon
            ("NVDA", "1m", "10m", "ret_10m_fwd"),  # Different symbol
            ("TSLA", "5s", "10m", "ret_10m_fwd"),  # Different freq
        ]

        for i, (symbol, freq, horizon, label_id) in enumerate(configs):
            metrics = Metrics(sharpe=2.0)
            rec = ExperimentRecord(
                run_id=f"test_{i}",
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

        records = load_experiment_records(
            index_path,
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
        )
        assert len(records) == 2

    def test_load_skips_invalid_json_lines(self, tmp_path):
        """Test that invalid JSON lines are skipped."""
        index_path = tmp_path / "test_index.jsonl"

        # Write a valid record
        metrics = Metrics(sharpe=2.0)
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

        # Manually append invalid JSON
        with index_path.open("a") as f:
            f.write("this is not json\n")
            f.write("\n")  # Empty line

        # Should still load the valid record
        records = load_experiment_records(index_path)
        assert len(records) == 1


class TestTopNByMetric:
    """Test top_n_by_metric function."""

    def test_top_n_by_sharpe(self):
        """Test getting top N by Sharpe ratio."""
        records = [
            ExperimentRecord(
                run_id=f"test_{i}",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=Metrics(sharpe=1.0 + i * 0.5),
                run_ts="2025-11-17T10:00:00Z",
            )
            for i in range(5)
        ]

        top_3 = top_n_by_metric(records, "sharpe", n=3)

        assert len(top_3) == 3
        assert top_3[0].metrics.sharpe == 3.0  # Highest
        assert top_3[1].metrics.sharpe == 2.5
        assert top_3[2].metrics.sharpe == 2.0

    def test_top_n_with_none_values(self):
        """Test handling of None metric values."""
        records = [
            ExperimentRecord(
                run_id="test_1",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=Metrics(sharpe=2.5),
                run_ts="2025-11-17T10:00:00Z",
            ),
            ExperimentRecord(
                run_id="test_2",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="lgbm",
                feature_subset=["close"],
                params={},
                metrics=Metrics(sharpe=None),  # No sharpe
                run_ts="2025-11-17T11:00:00Z",
            ),
            ExperimentRecord(
                run_id="test_3",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="ridge",
                feature_subset=["close"],
                params={},
                metrics=Metrics(sharpe=1.8),
                run_ts="2025-11-17T12:00:00Z",
            ),
        ]

        top_2 = top_n_by_metric(records, "sharpe", n=2)

        # Should get the two with values, sorted by value
        assert len(top_2) == 2
        assert top_2[0].metrics.sharpe == 2.5
        assert top_2[1].metrics.sharpe == 1.8

    def test_top_n_reverse_false(self):
        """Test getting top N with reverse=False (smallest first)."""
        records = [
            ExperimentRecord(
                run_id=f"test_{i}",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=Metrics(max_drawdown=-0.05 - i * 0.02),
                run_ts="2025-11-17T10:00:00Z",
            )
            for i in range(3)
        ]

        # For drawdown, more negative is worse
        # reverse=False sorts ascending: -0.09 < -0.07 < -0.05
        # So reverse=False gives most negative (worst) first
        top_2 = top_n_by_metric(records, "max_drawdown", n=2, reverse=False)

        # With reverse=False, we get most negative first (worst for drawdown)
        assert top_2[0].metrics.max_drawdown == -0.09
        assert top_2[1].metrics.max_drawdown == -0.07

    def test_top_n_fewer_records_than_n(self):
        """Test when there are fewer records than requested."""
        records = [
            ExperimentRecord(
                run_id="test_1",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=Metrics(sharpe=2.0),
                run_ts="2025-11-17T10:00:00Z",
            )
        ]

        top_5 = top_n_by_metric(records, "sharpe", n=5)

        assert len(top_5) == 1  # Only 1 record available
