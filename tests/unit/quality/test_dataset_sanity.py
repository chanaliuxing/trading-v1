"""Tests for dataset sanity checks."""

import numpy as np
import pandas as pd
import pytest

from quality.dataset_sanity import run_dataset_sanity


class TestRunDatasetSanity:
    """Test dataset sanity check function."""

    def test_empty_dataframe(self):
        """Test handling of empty dataframe."""
        df = pd.DataFrame()
        report, summary = run_dataset_sanity(df)

        assert summary["overall_status"] == "fail"
        assert summary["reason"] == "empty_dataframe"
        assert report["basic"]["n_rows"] == 0

    def test_basic_healthy_dataframe(self):
        """Test a healthy dataframe with no issues."""
        df = pd.DataFrame({
            "ts": pd.date_range("2024-01-01", periods=100, freq="1min"),
            "symbol": ["TSLA"] * 100,
            "close": np.random.randn(100) * 10 + 100,
            "volume": np.random.randint(1000, 10000, 100),
        })

        report, summary = run_dataset_sanity(df, label_cols=None)

        assert summary["overall_status"] in ["ok", "warn"]
        assert report["basic"]["n_rows"] == 100
        assert report["basic"]["n_cols"] == 4
        assert len(report["columns"]) > 0

    def test_high_missing_values(self):
        """Test detection of high missing values."""
        df = pd.DataFrame({
            "feature1": [1.0] * 30 + [np.nan] * 70,  # 70% missing
            "feature2": np.random.randn(100),
        })

        report, summary = run_dataset_sanity(df, high_missing_threshold=0.30)

        assert summary["overall_status"] == "warn"
        assert any("high_missing:feature1" in issue for issue in summary["issues"])
        assert report["columns"]["feature1"]["null_ratio"] == 0.7

    def test_zero_variance_detection(self):
        """Test detection of zero variance columns."""
        df = pd.DataFrame({
            "constant_col": [5.0] * 100,
            "normal_col": np.random.randn(100),
        })

        report, summary = run_dataset_sanity(df)

        assert any("zero_variance:constant_col" in issue for issue in summary["issues"])
        assert report["columns"]["constant_col"]["variance"] == 0.0

    def test_non_finite_values_detection(self):
        """Test detection of inf/nan values."""
        df = pd.DataFrame({
            "bad_col": [1.0, 2.0, np.inf, 3.0, -np.inf] + [4.0] * 95,
            "good_col": np.random.randn(100),
        })

        report, summary = run_dataset_sanity(df)

        # Should detect non-finite values
        assert any("non_finite_values:bad_col" in issue for issue in summary["issues"])

    def test_high_correlation_detection(self):
        """Test detection of highly correlated features."""
        df = pd.DataFrame({
            "feature1": np.random.randn(100),
        })
        df["feature2"] = df["feature1"] * 1.001  # Almost identical

        report, summary = run_dataset_sanity(df, high_corr_threshold=0.98)

        # Should detect high correlation
        assert any("high_multicollinearity" in issue for issue in summary["issues"])
        if "high_corr_pairs" in report:
            assert len(report["high_corr_pairs"]) > 0

    def test_label_classification_detection(self):
        """Test label detection for classification task."""
        df = pd.DataFrame({
            "feature": np.random.randn(100),
            "label": np.random.choice([0, 1, 2], 100),
        })

        report, summary = run_dataset_sanity(df, label_cols=["label"])

        assert "label" in report["labels"]
        assert report["labels"]["label"]["task"] == "cls"
        assert report["labels"]["label"]["n_unique"] <= 10

    def test_label_regression_detection(self):
        """Test label detection for regression task."""
        df = pd.DataFrame({
            "feature": np.random.randn(100),
            "label": np.random.randn(100),
        })

        report, summary = run_dataset_sanity(df, label_cols=["label"])

        assert "label" in report["labels"]
        assert report["labels"]["label"]["task"] == "reg"
        assert "mean" in report["labels"]["label"]
        assert "std" in report["labels"]["label"]

    def test_label_imbalance_detection(self):
        """Test detection of heavily imbalanced labels."""
        # 98% class 0, 2% class 1
        labels = [0] * 98 + [1] * 2
        df = pd.DataFrame({
            "feature": np.random.randn(100),
            "label": labels,
        })

        report, summary = run_dataset_sanity(df, label_cols=["label"])

        # Should detect imbalance
        assert any("label_heavy_imbalance:label" in issue for issue in summary["issues"])
        assert summary["overall_status"] == "warn"

    def test_missing_label_column(self):
        """Test handling of missing label column."""
        df = pd.DataFrame({
            "feature": np.random.randn(100),
        })

        report, summary = run_dataset_sanity(df, label_cols=["nonexistent_label"])

        assert any("label_missing:nonexistent_label" in issue for issue in summary["issues"])
        assert summary["overall_status"] == "fail"

    def test_all_nan_label(self):
        """Test handling of all-NaN label."""
        df = pd.DataFrame({
            "feature": np.random.randn(100),
            "label": [np.nan] * 100,
        })

        report, summary = run_dataset_sanity(df, label_cols=["label"])

        assert any("label_all_nan:label" in issue for issue in summary["issues"])
        assert summary["overall_status"] == "fail"

    def test_sampling_large_dataframe(self):
        """Test that large dataframes are sampled."""
        # Create dataframe larger than max_rows
        df = pd.DataFrame({
            "feature": np.random.randn(100000),
            "label": np.random.randn(100000),
        })

        report, summary = run_dataset_sanity(df, max_rows=50000, label_cols=["label"])

        # Should complete without error and report full size
        assert report["basic"]["n_rows"] == 100000
        assert summary["basic"]["n_rows"] == 100000

    def test_multiple_labels(self):
        """Test handling of multiple label columns."""
        df = pd.DataFrame({
            "feature": np.random.randn(100),
            "label1": np.random.choice([0, 1], 100),
            "label2": np.random.randn(100),
        })

        report, summary = run_dataset_sanity(df, label_cols=["label1", "label2"])

        assert "label1" in report["labels"]
        assert "label2" in report["labels"]
        assert report["labels"]["label1"]["task"] == "cls"
        assert report["labels"]["label2"]["task"] == "reg"

    def test_column_statistics_completeness(self):
        """Test that all expected column statistics are computed."""
        df = pd.DataFrame({
            "feature": np.random.randn(100),
        })

        report, summary = run_dataset_sanity(df)

        col_stats = report["columns"]["feature"]
        required_keys = ["null_ratio", "finite_ratio", "variance", "min", "p01", "median", "p99", "max"]
        for key in required_keys:
            assert key in col_stats, f"Missing statistic: {key}"
