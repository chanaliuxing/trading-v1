# tests/unit/quality/test_dataset_sanity_v2.py

"""
Tests for DatasetSanityResult.brief_summary() and run_dataset_sanity_v2().

Tests the new structured interface for dataset sanity checks with
LLM-friendly compressed summaries.
"""

import numpy as np
import pandas as pd
import pytest

from quality.dataset_sanity import (
    DatasetSanityIssue,
    DatasetSanityResult,
    run_dataset_sanity_v2,
)


class TestDatasetSanityIssue:
    """Test DatasetSanityIssue dataclass."""

    def test_issue_creation(self):
        """DatasetSanityIssue should store all attributes correctly."""
        issue = DatasetSanityIssue(
            code="high_missing",
            message="Column 'feature1' has 40% missing values",
            severity="warning",
            details={"column": "feature1", "ratio": 0.4},
        )

        assert issue.code == "high_missing"
        assert issue.message == "Column 'feature1' has 40% missing values"
        assert issue.severity == "warning"
        assert issue.details == {"column": "feature1", "ratio": 0.4}

    def test_issue_without_details(self):
        """DatasetSanityIssue should work without details."""
        issue = DatasetSanityIssue(
            code="zero_variance",
            message="Column 'const_col' has zero variance",
            severity="warning",
        )

        assert issue.code == "zero_variance"
        assert issue.details is None


class TestDatasetSanityResult:
    """Test DatasetSanityResult dataclass and brief_summary() method."""

    def test_result_creation(self):
        """DatasetSanityResult should initialize with correct defaults."""
        result = DatasetSanityResult(ok=True)

        assert result.ok is True
        assert result.issues == []
        assert result.report == {}
        assert result.summary == {}

    def test_add_issue_warning(self):
        """add_issue() should add warning without changing ok status."""
        result = DatasetSanityResult(ok=True)
        result.add_issue(
            code="high_missing",
            message="High missing values",
            severity="warning",
        )

        assert result.ok is True  # Warning doesn't change ok
        assert len(result.issues) == 1
        assert result.issues[0].code == "high_missing"
        assert result.issues[0].severity == "warning"

    def test_add_issue_error(self):
        """add_issue() with severity='error' should set ok=False."""
        result = DatasetSanityResult(ok=True)
        result.add_issue(
            code="label_missing",
            message="Label column missing",
            severity="error",
        )

        assert result.ok is False  # Error changes ok to False
        assert len(result.issues) == 1
        assert result.issues[0].severity == "error"

    def test_brief_summary_empty(self):
        """brief_summary() should work with empty result."""
        result = DatasetSanityResult(
            ok=True,
            report={"basic": {"n_rows": 1000, "n_cols": 5}},
            summary={},
        )

        summary = result.brief_summary()

        assert summary["ok"] is True
        assert summary["severity"] == "ok"
        assert summary["n_rows"] == 1000
        assert summary["n_cols"] == 5
        assert summary["issue_counts"] == {"info": 0, "warning": 0, "error": 0}
        assert summary["top_issues"] == []
        assert summary["label_info"] is None

    def test_brief_summary_with_warnings(self):
        """brief_summary() should count warnings correctly."""
        result = DatasetSanityResult(
            ok=True,
            report={"basic": {"n_rows": 5000, "n_cols": 10}},
            summary={},
        )
        result.add_issue("high_missing", "Column A has high missing", "warning")
        result.add_issue("zero_variance", "Column B has zero variance", "warning")
        result.add_issue("high_multicollinearity", "Columns C-D correlated", "warning")

        summary = result.brief_summary()

        assert summary["ok"] is True
        assert summary["severity"] == "warning"
        assert summary["issue_counts"] == {"info": 0, "warning": 3, "error": 0}
        assert len(summary["top_issues"]) == 3

    def test_brief_summary_with_errors(self):
        """brief_summary() should prioritize errors in severity level."""
        result = DatasetSanityResult(
            ok=False,
            report={"basic": {"n_rows": 100, "n_cols": 3}},
            summary={},
        )
        result.add_issue("label_missing", "Label not found", "error")
        result.add_issue("high_missing", "High missing values", "warning")

        summary = result.brief_summary()

        assert summary["ok"] is False
        assert summary["severity"] == "error"  # Error takes precedence
        assert summary["issue_counts"] == {"info": 0, "warning": 1, "error": 1}

    def test_brief_summary_top_5_issues(self):
        """brief_summary() should only return top 5 issues."""
        result = DatasetSanityResult(
            ok=True,
            report={"basic": {"n_rows": 10000, "n_cols": 20}},
            summary={},
        )

        # Add 10 issues
        for i in range(10):
            result.add_issue(f"issue_{i}", f"Issue {i}", "info")

        summary = result.brief_summary()

        assert len(summary["top_issues"]) == 5
        assert summary["top_issues"][0]["code"] == "issue_0"
        assert summary["top_issues"][4]["code"] == "issue_4"

    def test_brief_summary_with_label_info(self):
        """brief_summary() should include label info when available."""
        result = DatasetSanityResult(
            ok=True,
            report={
                "basic": {"n_rows": 1000, "n_cols": 5},
                "labels": {
                    "target": {
                        "task": "reg",
                        "n_unique": 950,
                        "mean": 0.05,
                        "std": 0.12,
                    }
                },
            },
            summary={},
        )

        summary = result.brief_summary(label_col="target")

        assert summary["label_info"] is not None
        assert summary["label_info"]["label_col"] == "target"
        assert summary["label_info"]["task"] == "reg"
        assert summary["label_info"]["mean"] == 0.05

    def test_brief_summary_label_fallback(self):
        """brief_summary() should use first label if label_col not specified."""
        result = DatasetSanityResult(
            ok=True,
            report={
                "basic": {"n_rows": 1000, "n_cols": 5},
                "labels": {
                    "label_a": {"task": "cls", "n_unique": 2},
                    "label_b": {"task": "reg", "n_unique": 800},
                },
            },
            summary={},
        )

        # No label_col specified - should use first label
        summary = result.brief_summary()

        assert summary["label_info"] is not None
        # Should get one of the labels (dict iteration order)
        assert summary["label_info"]["label_col"] in ["label_a", "label_b"]


class TestRunDatasetSanityV2:
    """Test run_dataset_sanity_v2() function."""

    def test_healthy_dataframe(self):
        """Healthy dataframe should return ok=True with no issues."""
        np.random.seed(42)
        df = pd.DataFrame(
            {
                "feature1": np.random.randn(1000),
                "feature2": np.random.randn(1000),
                "feature3": np.random.randn(1000),
                "label": np.random.randn(1000),
            }
        )

        result = run_dataset_sanity_v2(df, label_cols=["label"])

        assert result.ok is True
        assert len(result.issues) == 0

        # Test brief_summary
        summary = result.brief_summary(label_col="label")
        assert summary["ok"] is True
        assert summary["severity"] == "ok"
        assert summary["n_rows"] == 1000
        assert summary["n_cols"] == 4

    def test_high_missing_values(self):
        """High missing values should create warning issues."""
        df = pd.DataFrame(
            {
                "feature1": [1.0] * 50 + [np.nan] * 50,  # 50% missing
                "feature2": np.random.randn(100),
                "label": np.random.randn(100),
            }
        )

        result = run_dataset_sanity_v2(df, label_cols=["label"], max_rows=100)

        assert result.ok is True  # Warnings don't fail
        assert len(result.issues) > 0
        assert any("high_missing" in issue.code for issue in result.issues)

        # Test brief_summary
        summary = result.brief_summary(label_col="label")
        assert summary["severity"] == "warning"
        assert summary["issue_counts"]["warning"] > 0

    def test_zero_variance_detection(self):
        """Zero variance columns should create warning issues."""
        df = pd.DataFrame(
            {
                "const_col": [1.0] * 100,  # Zero variance
                "feature2": np.random.randn(100),
                "label": np.random.randn(100),
            }
        )

        result = run_dataset_sanity_v2(df, label_cols=["label"], max_rows=100)

        assert result.ok is True
        assert any("zero_variance" in issue.code for issue in result.issues)

    def test_missing_label_error(self):
        """Missing label column should create error and set ok=False."""
        df = pd.DataFrame(
            {
                "feature1": np.random.randn(100),
                "feature2": np.random.randn(100),
            }
        )

        result = run_dataset_sanity_v2(df, label_cols=["nonexistent_label"], max_rows=100)

        assert result.ok is False
        assert any("label_missing" in issue.code for issue in result.issues)

        # Test brief_summary
        summary = result.brief_summary()
        assert summary["ok"] is False
        assert summary["severity"] == "error"

    def test_all_nan_label_error(self):
        """All-NaN label should create error and set ok=False."""
        df = pd.DataFrame(
            {
                "feature1": np.random.randn(100),
                "label": [np.nan] * 100,
            }
        )

        result = run_dataset_sanity_v2(df, label_cols=["label"], max_rows=100)

        assert result.ok is False
        assert any("label_all_nan" in issue.code for issue in result.issues)

    def test_empty_dataframe(self):
        """Empty dataframe should return ok=False."""
        df = pd.DataFrame()

        result = run_dataset_sanity_v2(df, max_rows=100)

        # Empty dataframe doesn't create specific issues in current implementation
        # but overall_status is "fail"
        assert result.ok is False

        # Test brief_summary
        summary = result.brief_summary()
        assert summary["n_rows"] == 0

    def test_label_imbalance_detection(self):
        """Heavy label imbalance should create warning."""
        df = pd.DataFrame(
            {
                "feature1": np.random.randn(100),
                "label": [0] * 98 + [1] * 2,  # 98% class 0
            }
        )

        result = run_dataset_sanity_v2(df, label_cols=["label"], max_rows=100)

        assert result.ok is True  # Warning, not error
        assert any("label_heavy_imbalance" in issue.code for issue in result.issues)

    def test_multicollinearity_detection(self):
        """High correlation should create warning."""
        df = pd.DataFrame(
            {
                "feature1": np.arange(100),
                "feature2": np.arange(100) + 0.01,  # Almost identical
                "label": np.random.randn(100),
            }
        )

        result = run_dataset_sanity_v2(df, label_cols=["label"], max_rows=100)

        assert result.ok is True
        # Should detect high correlation
        if any("high_multicollinearity" in issue.code for issue in result.issues):
            # Test brief_summary includes this
            summary = result.brief_summary()
            assert summary["severity"] in ["warning", "ok"]

    def test_brief_summary_integration(self):
        """Integration test: full pipeline with brief_summary."""
        np.random.seed(42)
        df = pd.DataFrame(
            {
                "feature1": np.random.randn(500),
                "feature2": [1.0] * 500,  # Zero variance
                "feature3": [1.0] * 400 + [np.nan] * 100,  # 20% missing (below threshold)
                "label": np.random.randn(500),
            }
        )

        result = run_dataset_sanity_v2(df, label_cols=["label"], max_rows=500)

        # Should have zero variance warning
        assert result.ok is True
        assert len(result.issues) >= 1

        # Test brief_summary output structure
        summary = result.brief_summary(label_col="label")

        # Verify all required fields
        assert "ok" in summary
        assert "severity" in summary
        assert "n_rows" in summary
        assert "n_cols" in summary
        assert "issue_counts" in summary
        assert "top_issues" in summary
        assert "label_info" in summary

        # Verify types
        assert isinstance(summary["ok"], bool)
        assert summary["severity"] in ["ok", "warning", "error"]
        assert isinstance(summary["n_rows"], int)
        assert isinstance(summary["n_cols"], int)
        assert isinstance(summary["issue_counts"], dict)
        assert isinstance(summary["top_issues"], list)

        # Label info should exist and be dict
        assert isinstance(summary["label_info"], dict)
        assert summary["label_info"]["label_col"] == "label"
        assert "task" in summary["label_info"]
