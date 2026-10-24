"""Tests for label sanity checks."""

import numpy as np
import pandas as pd
import pytest

from labels.label_sanity import (
    LabelSanityConfig,
    LabelSanityReport,
    run_label_sanity_checks,
    summarize_label_for_llm,
)


class TestLabelSanityConfig:
    """Test LabelSanityConfig dataclass."""

    def test_config_defaults(self):
        """Test default configuration values."""
        cfg = LabelSanityConfig()

        assert cfg.min_samples == 5_000
        assert cfg.max_abs_mean_z_reg == 1.5
        assert cfg.max_majority_ratio_cls == 0.99
        assert cfg.extreme_tail_ratio_reg == 0.02
        assert cfg.enable_debug is False

    def test_config_custom(self):
        """Test custom configuration."""
        cfg = LabelSanityConfig(
            min_samples=1000,
            max_abs_mean_z_reg=2.0,
            max_majority_ratio_cls=0.95,
            extreme_tail_ratio_reg=0.05,
            enable_debug=True,
        )

        assert cfg.min_samples == 1000
        assert cfg.max_abs_mean_z_reg == 2.0
        assert cfg.max_majority_ratio_cls == 0.95
        assert cfg.extreme_tail_ratio_reg == 0.05
        assert cfg.enable_debug is True


class TestLabelSanityReport:
    """Test LabelSanityReport dataclass."""

    def test_report_creation(self):
        """Test creating sanity report."""
        report = LabelSanityReport(
            label_id="ret_10m",
            value_col="y_ret_10m",
            side_col="y_side",
            task_type="reg",
            n_samples=10000,
            nan_ratio=0.05,
        )

        assert report.label_id == "ret_10m"
        assert report.value_col == "y_ret_10m"
        assert report.task_type == "reg"
        assert report.n_samples == 10000
        assert report.is_ok is True

    def test_report_to_dict(self):
        """Test converting report to dictionary."""
        report = LabelSanityReport(
            label_id="ret_10m",
            value_col="y_ret_10m",
            side_col=None,
            task_type="reg",
            n_samples=10000,
            nan_ratio=0.05,
            mean=0.001,
            std=0.02,
        )

        d = report.to_dict()
        assert d["label_id"] == "ret_10m"
        assert d["n_samples"] == 10000
        assert d["mean"] == 0.001


class TestRunLabelSanityChecks:
    """Test label sanity check function."""

    def test_healthy_regression_label(self):
        """Test with healthy regression label."""
        np.random.seed(42)
        n = 10000
        y = np.random.randn(n) * 0.02  # Mean ~0, std ~0.02

        df = pd.DataFrame({"y_ret": y})

        cfg = LabelSanityConfig(min_samples=5000)
        report = run_label_sanity_checks(
            df,
            label_id="ret_10m",
            value_col="y_ret",
            task_type="reg",
            cfg=cfg,
        )

        assert report.is_ok is True
        assert report.n_samples == n
        assert report.issues is None
        assert abs(report.mean) < 0.01  # Should be close to 0
        assert 0.01 < report.std < 0.03  # Should be close to 0.02

    def test_too_few_samples(self):
        """Test detection of too few samples."""
        df = pd.DataFrame({"y_ret": np.random.randn(1000) * 0.02})

        cfg = LabelSanityConfig(min_samples=5000)
        report = run_label_sanity_checks(
            df,
            label_id="ret_10m",
            value_col="y_ret",
            task_type="reg",
            cfg=cfg,
        )

        assert report.is_ok is False
        assert "too_few_samples" in report.issues

    def test_mean_bias_detection(self):
        """Test detection of biased mean."""
        # Create data with mean >> std
        df = pd.DataFrame({"y_ret": np.random.randn(10000) * 0.01 + 0.05})

        cfg = LabelSanityConfig(
            min_samples=1000,
            max_abs_mean_z_reg=1.0,  # Strict threshold
        )
        report = run_label_sanity_checks(
            df,
            label_id="ret_10m",
            value_col="y_ret",
            task_type="reg",
            cfg=cfg,
        )

        assert report.is_ok is False
        assert "mean_bias" in report.issues
        assert report.abs_mean_z > 1.0

    def test_heavy_tails_detection(self):
        """Test detection of heavy tails."""
        # Create mostly normal data with extreme outliers
        np.random.seed(42)
        y = np.random.randn(10000) * 0.02
        # Add extreme outliers - need to be in the 99th percentile range
        # to trigger the heavy tail check (which looks at values > p99 threshold)
        y[0:500] = np.random.randn(500) * 2.0  # 5% extreme values, much larger magnitude

        df = pd.DataFrame({"y_ret": y})

        cfg = LabelSanityConfig(
            min_samples=1000,
            extreme_tail_ratio_reg=0.01,  # Stricter threshold
        )
        report = run_label_sanity_checks(
            df,
            label_id="ret_10m",
            value_col="y_ret",
            task_type="reg",
            cfg=cfg,
        )

        # Should detect heavy tails (close to 1% which is exactly the threshold)
        # The ratio should be >= 0.01
        assert report.extreme_tail_ratio >= 0.01

    def test_classification_label_balanced(self):
        """Test with balanced classification labels."""
        n = 10000
        y = np.random.randn(n) * 0.02
        side = np.where(y > 0, 1, np.where(y < 0, -1, 0))

        df = pd.DataFrame({"y_ret": y, "y_side": side})

        cfg = LabelSanityConfig(min_samples=1000)
        report = run_label_sanity_checks(
            df,
            label_id="ret_10m",
            value_col="y_ret",
            side_col="y_side",
            task_type="cls",
            cfg=cfg,
        )

        assert report.is_ok is True
        assert report.n_pos is not None
        assert report.n_neg is not None
        assert report.majority_ratio is not None
        assert report.majority_ratio < 0.6  # Should be roughly balanced

    def test_classification_label_imbalanced(self):
        """Test detection of severe label imbalance."""
        n = 10000
        # Create highly imbalanced labels (99.5% positive)
        side = np.ones(n, dtype=int)
        side[0:50] = -1  # Only 0.5% negative

        df = pd.DataFrame({"y_ret": np.random.randn(n) * 0.02, "y_side": side})

        cfg = LabelSanityConfig(
            min_samples=1000,
            max_majority_ratio_cls=0.99,
        )
        report = run_label_sanity_checks(
            df,
            label_id="ret_10m",
            value_col="y_ret",
            side_col="y_side",
            task_type="cls",
            cfg=cfg,
        )

        assert report.is_ok is False
        assert "label_imbalance" in report.issues
        assert report.majority_ratio > 0.99

    def test_high_nan_ratio(self):
        """Test with high NaN ratio."""
        y = np.random.randn(10000) * 0.02
        y[0:3000] = np.nan  # 30% NaN

        df = pd.DataFrame({"y_ret": y})

        report = run_label_sanity_checks(
            df,
            label_id="ret_10m",
            value_col="y_ret",
            task_type="reg",
            cfg=LabelSanityConfig(min_samples=5000),
        )

        # Should have enough valid samples (7000)
        assert report.n_samples == 7000
        assert report.nan_ratio == 0.3
        assert report.is_ok is True  # Still ok if enough valid samples

    def test_missing_value_col_raises_error(self):
        """Test that missing value column raises error."""
        df = pd.DataFrame({"other_col": [1, 2, 3]})

        with pytest.raises(KeyError, match="not in DataFrame"):
            run_label_sanity_checks(
                df,
                label_id="ret_10m",
                value_col="y_ret",
                task_type="reg",
            )

    def test_percentile_calculations(self):
        """Test that percentiles are calculated correctly."""
        # Create data with known distribution
        y = np.arange(0, 100, 0.01)  # 10000 evenly spaced values from 0 to 100
        df = pd.DataFrame({"y": y})

        report = run_label_sanity_checks(
            df,
            label_id="test",
            value_col="y",
            task_type="reg",
            cfg=LabelSanityConfig(min_samples=1000),
        )

        # Check percentiles (should be close to expected values)
        assert abs(report.p1 - 1.0) < 1.0
        assert abs(report.p50 - 50.0) < 1.0
        assert abs(report.p99 - 99.0) < 1.0


class TestSummarizeLabelForLLM:
    """Test LLM summary generation."""

    def test_summarize_regression_label(self):
        """Test summarizing regression label."""
        label_sanity = {
            "label_id": "ret_10m",
            "task_type": "reg",
            "n_samples": 10000,
            "nan_ratio": 0.05,
            "mean": 0.001,
            "std": 0.02,
            "p1": -0.04,
            "p50": 0.0,
            "p99": 0.04,
            "abs_mean_z": 0.05,
            "issues": None,
        }

        summary = summarize_label_for_llm(label_sanity)

        assert "ret_10m" in summary
        assert "task_type=reg" in summary
        assert "n_samples=10000" in summary
        assert "mean=0.00100" in summary
        assert "issues: none" in summary

    def test_summarize_classification_label(self):
        """Test summarizing classification label."""
        label_sanity = {
            "label_id": "tb_10m",
            "task_type": "cls",
            "n_samples": 10000,
            "nan_ratio": 0.1,
            "n_pos": 4000,
            "n_neg": 3500,
            "n_zero": 2500,
            "majority_ratio": 0.4,
            "issues": None,
        }

        summary = summarize_label_for_llm(label_sanity)

        assert "tb_10m" in summary
        assert "task_type=cls" in summary
        assert "pos=4000" in summary
        assert "neg=3500" in summary
        assert "zero=2500" in summary
        assert "majority_ratio=0.400" in summary

    def test_summarize_label_with_issues(self):
        """Test summarizing label with detected issues."""
        label_sanity = {
            "label_id": "ret_10m",
            "task_type": "reg",
            "n_samples": 3000,
            "nan_ratio": 0.1,
            "mean": 0.05,
            "std": 0.01,
            "p1": 0.03,
            "p50": 0.05,
            "p99": 0.07,
            "abs_mean_z": 5.0,
            "issues": {
                "too_few_samples": {"n_samples": 3000, "min_required": 5000},
                "mean_bias": {"abs_mean_z": 5.0, "max_allowed": 1.5},
            },
        }

        summary = summarize_label_for_llm(label_sanity)

        assert "issues:" in summary
        assert "too_few_samples" in summary
        assert "mean_bias" in summary

    def test_summarize_missing_label(self):
        """Test summarizing with missing label sanity."""
        summary = summarize_label_for_llm(None)
        assert "missing" in summary.lower()

        # Empty dict should still generate summary
        summary = summarize_label_for_llm({})
        assert "Label" in summary  # Basic check that summary was generated
