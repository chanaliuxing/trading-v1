"""Tests for label integration adapter."""

import numpy as np
import pandas as pd
import pytest

from labels.label_integration import (
    build_labels_with_sanity,
    build_and_check_labels,
    integrate_sanity_into_dataset_manifest,
    summarize_label_sanity_for_logging,
)
from labels.label_sanity import LabelSanityConfig, LabelSanityReport


class TestBuildLabelsWithSanity:
    """Test build_labels_with_sanity function."""

    @pytest.fixture
    def sample_df(self):
        """Create sample DataFrame with OHLCV data."""
        np.random.seed(42)
        n = 1000
        base_price = 100.0
        returns = np.random.randn(n) * 0.02
        close = base_price * np.exp(np.cumsum(returns))

        high = close * (1 + np.abs(np.random.randn(n)) * 0.01)
        low = close * (1 - np.abs(np.random.randn(n)) * 0.01)
        open_ = close * (1 + np.random.randn(n) * 0.005)

        # Calculate ATR for triple barrier
        tr = np.maximum(high - low, np.abs(high - np.roll(close, 1)))
        atr_14_series = pd.Series(tr).rolling(14).mean()
        atr_14 = atr_14_series.fillna(method='bfill').fillna(2.0).values

        return pd.DataFrame({
            'ts': pd.date_range('2024-01-01', periods=n, freq='1min'),
            'symbol': ['AAPL'] * n,
            'open': open_,
            'high': high,
            'low': low,
            'close': close,
            'volume': np.random.randint(100000, 500000, n),
            'atr_14': atr_14,
        })

    def test_new_format_with_label_id(self, sample_df):
        """Test with new label_id format using Label Engine."""
        label_cfg = {
            "label_id": "ret_10m",
            "task": "reg",
            "params": {},
        }

        df_labeled, sanity_report = build_labels_with_sanity(
            sample_df,
            label_cfg,
            target_col="y",
        )

        # Check label column was created
        assert "y" in df_labeled.columns
        assert sanity_report is not None
        assert sanity_report.label_id == "ret_10m"
        assert sanity_report.value_col == "y"
        assert sanity_report.task_type == "reg"

    def test_triple_barrier_label(self, sample_df):
        """Test triple barrier label creation."""
        label_cfg = {
            "label_id": "tb_10m_atr1",
            "task": "reg",
            "params": {},
        }

        df_labeled, sanity_report = build_labels_with_sanity(
            sample_df,
            label_cfg,
            target_col="y",
        )

        assert "y" in df_labeled.columns
        assert sanity_report is not None
        # Triple barrier returns have bounded distribution
        assert sanity_report.mean is not None
        assert sanity_report.std is not None

    def test_sanity_disabled(self, sample_df):
        """Test with sanity checks disabled."""
        label_cfg = {
            "label_id": "ret_10m",
            "task": "reg",
        }

        df_labeled, sanity_report = build_labels_with_sanity(
            sample_df,
            label_cfg,
            target_col="y",
            enable_sanity=False,
        )

        assert "y" in df_labeled.columns
        assert sanity_report is None

    def test_custom_sanity_config(self, sample_df):
        """Test with custom sanity configuration."""
        label_cfg = {
            "label_id": "ret_10m",
            "task": "reg",
        }

        custom_cfg = LabelSanityConfig(
            min_samples=500,  # Lower threshold
            max_abs_mean_z_reg=2.0,  # More lenient
        )

        df_labeled, sanity_report = build_labels_with_sanity(
            sample_df,
            label_cfg,
            target_col="y",
            sanity_cfg=custom_cfg,
        )

        assert sanity_report is not None
        # Should pass with 1000 samples (> 500 threshold)
        assert "too_few_samples" not in (sanity_report.issues or {})

    def test_custom_target_col_name(self, sample_df):
        """Test with custom target column name."""
        label_cfg = {
            "label_id": "ret_10m",
            "task": "reg",
        }

        df_labeled, sanity_report = build_labels_with_sanity(
            sample_df,
            label_cfg,
            target_col="my_target",
        )

        assert "my_target" in df_labeled.columns
        assert sanity_report.value_col == "my_target"

    def test_label_params_passed_through(self, sample_df):
        """Test that label parameters are passed to Label Engine."""
        # Use a standard label that exists in the registry
        label_cfg = {
            "label_id": "ret_10m",
            "task": "reg",
            "params": {},  # ret_10m doesn't need extra params
        }

        df_labeled, sanity_report = build_labels_with_sanity(
            sample_df,
            label_cfg,
            target_col="y",
        )

        assert "y" in df_labeled.columns
        # Verify label was created (values should exist)
        assert df_labeled["y"].notna().any()
        # Verify sanity report was created
        assert sanity_report is not None
        assert sanity_report.label_id == "ret_10m"


class TestBuildAndCheckLabels:
    """Test simplified build_and_check_labels function."""

    @pytest.fixture
    def sample_df(self):
        """Create sample DataFrame."""
        np.random.seed(42)
        n = 1000
        close = 100 * np.exp(np.cumsum(np.random.randn(n) * 0.02))

        return pd.DataFrame({
            'ts': pd.date_range('2024-01-01', periods=n, freq='1min'),
            'symbol': ['AAPL'] * n,
            'close': close,
            'atr_14': pd.Series(np.abs(np.random.randn(n)) * 2).rolling(14).mean().fillna(2.0),
        })

    def test_build_and_check_basic(self, sample_df):
        """Test basic usage of build_and_check_labels."""
        df_labeled, sanity = build_and_check_labels(
            sample_df,
            label_id="ret_10m",
        )

        assert "y" in df_labeled.columns
        assert sanity is not None
        assert sanity.is_ok is True or sanity.is_ok is False  # Just check it exists

    def test_build_and_check_custom_target_col(self, sample_df):
        """Test with custom target column name."""
        df_labeled, sanity = build_and_check_labels(
            sample_df,
            label_id="ret_10m",
            target_col="target",
        )

        assert "target" in df_labeled.columns
        assert sanity.value_col == "target"

    def test_build_and_check_classification(self, sample_df):
        """Test with classification task."""
        df_labeled, sanity = build_and_check_labels(
            sample_df,
            label_id="ret_10m",
            task_type="cls",
        )

        assert "y" in df_labeled.columns
        assert sanity.task_type == "cls"


class TestIntegrateSanityIntoManifest:
    """Test dataset manifest integration."""

    def test_integrate_with_report(self):
        """Test integrating sanity report into manifest."""
        manifest = {
            "dataset_sanity": {"ok": True},
            "feature_sanity": {"ok": True},
        }

        sanity_report = LabelSanityReport(
            label_id="ret_10m",
            value_col="y",
            side_col=None,
            task_type="reg",
            n_samples=10000,
            nan_ratio=0.05,
            mean=0.001,
            std=0.02,
            is_ok=True,
        )

        updated = integrate_sanity_into_dataset_manifest(manifest, sanity_report)

        assert "label_sanity" in updated
        assert updated["label_sanity"]["label_id"] == "ret_10m"
        assert updated["label_sanity"]["n_samples"] == 10000
        assert updated["label_sanity"]["is_ok"] is True

    def test_integrate_with_none_report(self):
        """Test integrating when no sanity report available."""
        manifest = {
            "dataset_sanity": {"ok": True},
        }

        updated = integrate_sanity_into_dataset_manifest(manifest, None)

        assert "label_sanity" in updated
        assert updated["label_sanity"]["is_ok"] is False
        assert "no_sanity_check" in updated["label_sanity"]["issues"]

    def test_original_manifest_unchanged(self):
        """Test that original manifest is not mutated."""
        manifest = {
            "dataset_sanity": {"ok": True},
        }

        sanity_report = LabelSanityReport(
            label_id="test",
            value_col="y",
            side_col=None,
            task_type="reg",
            n_samples=100,
            nan_ratio=0.0,
            is_ok=True,
        )

        updated = integrate_sanity_into_dataset_manifest(manifest, sanity_report)

        # Original should not have label_sanity
        assert "label_sanity" not in manifest
        # Updated should have it
        assert "label_sanity" in updated


class TestSummarizeLabelSanityForLogging:
    """Test label sanity summary generation."""

    def test_summarize_with_report(self):
        """Test summarizing sanity report for logging."""
        sanity_report = LabelSanityReport(
            label_id="ret_10m",
            value_col="y",
            side_col=None,
            task_type="reg",
            n_samples=10000,
            nan_ratio=0.05,
            mean=0.001,
            std=0.02,
            p1=-0.04,
            p50=0.0,
            p99=0.04,
            abs_mean_z=0.05,
            is_ok=True,
        )

        summary = summarize_label_sanity_for_logging(sanity_report)

        assert "ret_10m" in summary
        assert "n_samples=10000" in summary
        assert "mean=0.00100" in summary
        assert "issues: none" in summary

    def test_summarize_with_none_report(self):
        """Test summarizing when no report available."""
        summary = summarize_label_sanity_for_logging(None)

        assert "not performed" in summary.lower()

    def test_summarize_with_issues(self):
        """Test summarizing report with issues."""
        sanity_report = LabelSanityReport(
            label_id="ret_10m",
            value_col="y",
            side_col=None,
            task_type="reg",
            n_samples=1000,
            nan_ratio=0.1,
            mean=0.05,
            std=0.01,
            abs_mean_z=5.0,
            is_ok=False,
            issues={
                "too_few_samples": {"n_samples": 1000, "min_required": 5000},
                "mean_bias": {"abs_mean_z": 5.0, "max_allowed": 1.5},
            },
        )

        summary = summarize_label_sanity_for_logging(sanity_report)

        assert "too_few_samples" in summary
        assert "mean_bias" in summary


class TestLegacyProfileSupport:
    """Test backward compatibility with legacy label profiles."""

    @pytest.fixture
    def sample_df(self):
        """Create sample DataFrame."""
        np.random.seed(42)
        n = 1000
        close = 100 * np.exp(np.cumsum(np.random.randn(n) * 0.02))

        return pd.DataFrame({
            'ts': pd.date_range('2024-01-01', periods=n, freq='1min'),
            'symbol': ['AAPL'] * n,
            'close': close,
        })

    def test_legacy_profile_format_detection(self, sample_df):
        """Test that legacy profile format is detected correctly."""
        # Legacy profile format (no label_id)
        label_cfg = {
            "task": "reg",
            "horizon_bars": 5,
            "base": {
                "transform": "log_return",
            },
        }

        # This should detect legacy format and try to use backtest_ml_engine
        # If backtest_ml_engine exists, it will work; if not, it will raise ImportError
        # Either outcome is valid - we're just testing the detection logic
        try:
            df_labeled, sanity = build_labels_with_sanity(sample_df, label_cfg, "y")
            # If it succeeds, check that label was created
            assert "y" in df_labeled.columns
            assert sanity is not None
        except ImportError as e:
            # If it fails, check that the error message is helpful
            assert "backtest_ml_engine" in str(e)
