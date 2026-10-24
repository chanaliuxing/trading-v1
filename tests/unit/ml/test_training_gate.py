# tests/unit/ml/test_training_gate.py

"""
Comprehensive tests for training gate validation.

Tests cover:
- Default configuration
- Row count validation
- NaN fraction validation
- Constant column detection
- Label quality checks
- Label balance checks
- Edge cases (empty data, all NaN labels, etc.)
"""

import numpy as np
import pandas as pd
import pytest

from ml.training_gate import (
    TrainingGateConfig,
    TrainingGateResult,
    run_training_gate,
)


class TestTrainingGateConfig:
    """Test TrainingGateConfig dataclass and defaults."""

    def test_default_config(self):
        """Test default configuration values."""
        cfg = TrainingGateConfig()
        assert cfg.min_rows == 3_000
        assert cfg.max_null_frac_per_col == 0.2
        assert cfg.max_constant_col_frac == 0.5
        assert cfg.min_label_nonnull_frac == 0.9
        assert cfg.min_label_pos_frac == 0.02
        assert cfg.min_label_neg_frac == 0.02

    def test_custom_config(self):
        """Test custom configuration values."""
        cfg = TrainingGateConfig(
            min_rows=1000,
            max_null_frac_per_col=0.1,
            max_constant_col_frac=0.3,
            min_label_nonnull_frac=0.95,
            min_label_pos_frac=0.1,
            min_label_neg_frac=0.1,
        )
        assert cfg.min_rows == 1000
        assert cfg.max_null_frac_per_col == 0.1
        assert cfg.max_constant_col_frac == 0.3
        assert cfg.min_label_nonnull_frac == 0.95
        assert cfg.min_label_pos_frac == 0.1
        assert cfg.min_label_neg_frac == 0.1


class TestHappyPath:
    """Test cases that should pass training gate."""

    def test_clean_dataset_passes(self):
        """Clean dataset with sufficient rows and balanced labels should pass."""
        # Create clean dataset with 6000 rows
        np.random.seed(42)
        df = pd.DataFrame({
            "feature1": np.random.randn(6000),
            "feature2": np.random.randn(6000),
            "feature3": np.random.randn(6000),
            "label": np.random.randn(6000),  # Mean ~0, balanced pos/neg
        })

        result = run_training_gate(df, label_col="label")

        assert result.ok is True
        assert len(result.reasons) == 0
        assert result.metrics["n_rows"] == 6000
        assert result.metrics["label_nonnull_frac"] == 1.0
        assert 0.4 < result.metrics["label_pos_frac"] < 0.6  # Roughly balanced
        assert 0.4 < result.metrics["label_neg_frac"] < 0.6

    def test_minimal_passing_dataset(self):
        """Dataset at minimum thresholds should pass."""
        cfg = TrainingGateConfig(
            min_rows=100,
            min_label_pos_frac=0.05,
            min_label_neg_frac=0.05,
        )

        # Create dataset with exactly 100 rows, 10% positive, 10% negative
        df = pd.DataFrame({
            "feature1": np.ones(100),
            "label": [1.0] * 10 + [-1.0] * 10 + [0.0] * 80,
        })

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is True
        assert result.metrics["n_rows"] == 100
        assert result.metrics["label_pos_frac"] == 0.1
        assert result.metrics["label_neg_frac"] == 0.1


class TestRowCountValidation:
    """Test minimum row count validation."""

    def test_insufficient_rows_fails(self):
        """Dataset with too few rows should fail."""
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
            "label": np.random.randn(100),
        })

        result = run_training_gate(df, label_col="label")

        assert result.ok is False
        assert any("rows < min_rows" in reason for reason in result.reasons)
        assert result.metrics["n_rows"] == 100

    def test_exactly_min_rows_passes(self):
        """Dataset with exactly min_rows should pass."""
        cfg = TrainingGateConfig(min_rows=1000)
        np.random.seed(42)
        df = pd.DataFrame({
            "feature1": np.random.randn(1000),
            "label": np.random.randn(1000),
        })

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is True
        assert result.metrics["n_rows"] == 1000


class TestNaNValidation:
    """Test NaN fraction validation."""

    def test_excessive_nan_fails(self):
        """Column with >20% NaN should fail."""
        df = pd.DataFrame({
            "feature1": [1.0] * 80 + [np.nan] * 20,  # 20% NaN (at limit)
            "feature2": [1.0] * 70 + [np.nan] * 30,  # 30% NaN (exceeds limit)
            "label": np.random.randn(100),
        })
        cfg = TrainingGateConfig(min_rows=50)  # Lower min_rows to focus on NaN check

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        assert any("max_null_frac_per_col" in reason for reason in result.reasons)
        assert result.metrics["max_null_frac_per_col"] == 0.3
        assert "feature2" in result.reasons[0]  # Should identify worst column

    def test_acceptable_nan_passes(self):
        """Column with ≤20% NaN should pass."""
        np.random.seed(42)
        df = pd.DataFrame({
            "feature1": list(np.random.randn(85)) + [np.nan] * 15,  # 15% NaN (OK), variable values
            "feature2": list(np.random.randn(90)) + [np.nan] * 10,  # 10% NaN (OK), variable values
            "label": np.random.randn(100),
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is True
        assert result.metrics["max_null_frac_per_col"] == 0.15


class TestConstantColumnValidation:
    """Test constant column detection."""

    def test_excessive_constant_cols_fails(self):
        """Dataset with >50% constant columns should fail."""
        np.random.seed(42)
        df = pd.DataFrame({
            "const1": [1.0] * 100,  # Constant
            "const2": [2.0] * 100,  # Constant
            "const3": [3.0] * 100,  # Constant
            "const4": [4.0] * 100,  # Constant - now 4/6 = 66.7% > 50%
            "variable1": np.random.randn(100),
            "label": np.random.randn(100),
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        assert any("constant_col_frac" in reason for reason in result.reasons)
        assert result.metrics["constant_col_frac"] > 0.5  # 4/6 = 0.667

    def test_excessive_constant_cols_fails_clearly(self):
        """Dataset with >50% constant columns should fail."""
        df = pd.DataFrame({
            "const1": [1.0] * 100,
            "const2": [2.0] * 100,
            "const3": [3.0] * 100,
            "const4": [4.0] * 100,  # 4 constant out of 6 = 66.7%
            "variable1": np.random.randn(100),
            "label": np.random.randn(100),
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        assert any("constant_col_frac" in reason for reason in result.reasons)
        assert result.metrics["constant_col_frac"] > 0.5
        assert len(result.metrics["constant_cols"]) == 4

    def test_acceptable_constant_cols_passes(self):
        """Dataset with ≤50% constant columns should pass."""
        df = pd.DataFrame({
            "const1": [1.0] * 100,
            "const2": [2.0] * 100,  # 2 constant out of 6 = 33.3%
            "variable1": np.random.randn(100),
            "variable2": np.random.randn(100),
            "variable3": np.random.randn(100),
            "label": np.random.randn(100),
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is True
        assert result.metrics["constant_col_frac"] < 0.5

    def test_all_nan_column_is_constant(self):
        """Column with all NaN values should be considered constant."""
        df = pd.DataFrame({
            "feature1": [np.nan] * 100,  # All NaN = 0 unique values (constant)
            "feature2": np.random.randn(100),
            "label": np.random.randn(100),
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert "feature1" in result.metrics["constant_cols"]


class TestLabelValidation:
    """Test label column validation."""

    def test_missing_label_col_fails(self):
        """Missing label column should fail immediately."""
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
            "feature2": np.random.randn(100),
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="nonexistent", cfg=cfg)

        assert result.ok is False
        assert any("not in DataFrame columns" in reason for reason in result.reasons)

    def test_all_nan_labels_fails(self):
        """All NaN labels should fail."""
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
            "label": [np.nan] * 100,
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        assert any("All labels are NaN" in reason for reason in result.reasons)
        assert result.metrics["label_nonnull_frac"] == 0.0

    def test_insufficient_nonnull_labels_fails(self):
        """< 90% non-null labels should fail."""
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
            "label": [1.0] * 80 + [np.nan] * 20,  # 80% non-null
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        assert any("label_nonnull_frac" in reason for reason in result.reasons)
        assert result.metrics["label_nonnull_frac"] == 0.8


class TestLabelBalanceValidation:
    """Test label balance (pos/neg) validation."""

    def test_all_positive_labels_fails(self):
        """All positive labels (no negatives) should fail."""
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
            "label": np.abs(np.random.randn(100)) + 0.1,  # All positive
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        assert any("label_neg_frac" in reason for reason in result.reasons)
        assert result.metrics["label_pos_frac"] > 0.9
        assert result.metrics["label_neg_frac"] == 0.0

    def test_all_negative_labels_fails(self):
        """All negative labels (no positives) should fail."""
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
            "label": -np.abs(np.random.randn(100)) - 0.1,  # All negative
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        assert any("label_pos_frac" in reason for reason in result.reasons)
        assert result.metrics["label_pos_frac"] == 0.0
        assert result.metrics["label_neg_frac"] > 0.9

    def test_mostly_zero_labels_fails(self):
        """Labels that are mostly zero should fail."""
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
            "label": [1.0] * 1 + [-1.0] * 1 + [0.0] * 98,  # 1% pos, 1% neg, 98% zero
        })
        cfg = TrainingGateConfig(min_rows=50)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        # Should fail both pos and neg checks
        assert any("label_pos_frac" in reason for reason in result.reasons)
        assert any("label_neg_frac" in reason for reason in result.reasons)
        assert result.metrics["label_zero_frac"] == 0.98


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_dataframe_fails(self):
        """Empty DataFrame should fail."""
        df = pd.DataFrame()
        cfg = TrainingGateConfig(min_rows=1)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        # Will fail on missing label column

    def test_single_row_fails(self):
        """Single row should fail min_rows check."""
        df = pd.DataFrame({
            "feature1": [1.0],
            "label": [1.0],
        })

        result = run_training_gate(df, label_col="label")

        assert result.ok is False
        assert any("rows < min_rows" in reason for reason in result.reasons)

    def test_exact_threshold_values_pass(self):
        """Values exactly at thresholds should pass."""
        cfg = TrainingGateConfig(
            min_rows=100,
            min_label_pos_frac=0.05,
            min_label_neg_frac=0.05,
        )

        # Exactly 5% positive, 5% negative
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
            "label": [1.0] * 5 + [-1.0] * 5 + [0.0] * 90,
        })

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is True

    def test_multiple_failures_reported(self):
        """Multiple failures should all be reported."""
        df = pd.DataFrame({
            "const1": [1.0] * 50,  # Constant column
            "const2": [2.0] * 50,  # Constant column
            "feature1": [np.nan] * 30 + [1.0] * 20,  # 60% NaN
            "label": [1.0] * 50,  # All positive (no negatives)
        })
        cfg = TrainingGateConfig(min_rows=10)  # Will pass row check

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert result.ok is False
        # Should have multiple reasons
        assert len(result.reasons) >= 2
        # Check that different types of failures are detected
        reason_text = " ".join(result.reasons)
        assert "null_frac" in reason_text or "constant" in reason_text or "neg_frac" in reason_text


class TestMetricsLogging:
    """Test that metrics are properly logged."""

    def test_metrics_contain_all_required_fields(self):
        """Result metrics should contain all required fields."""
        np.random.seed(42)
        df = pd.DataFrame({
            "feature1": np.random.randn(1000),
            "label": np.random.randn(1000),
        })
        cfg = TrainingGateConfig(min_rows=500)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        # Required metrics
        assert "n_rows" in result.metrics
        assert "max_null_frac_per_col" in result.metrics
        assert "constant_col_frac" in result.metrics
        assert "constant_cols" in result.metrics
        assert "label_nonnull_frac" in result.metrics
        assert "label_pos_frac" in result.metrics
        assert "label_neg_frac" in result.metrics
        assert "label_zero_frac" in result.metrics

    def test_metrics_types_are_correct(self):
        """Metrics should have correct Python types (not numpy types)."""
        df = pd.DataFrame({
            "feature1": np.random.randn(1000),
            "label": np.random.randn(1000),
        })
        cfg = TrainingGateConfig(min_rows=500)

        result = run_training_gate(df, label_col="label", cfg=cfg)

        assert isinstance(result.metrics["n_rows"], int)
        assert isinstance(result.metrics["max_null_frac_per_col"], float)
        assert isinstance(result.metrics["constant_col_frac"], float)
        assert isinstance(result.metrics["constant_cols"], list)
        assert isinstance(result.metrics["label_nonnull_frac"], float)
        assert isinstance(result.metrics["label_pos_frac"], float)
        assert isinstance(result.metrics["label_neg_frac"], float)
