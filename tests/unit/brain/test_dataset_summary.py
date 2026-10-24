# tests/unit/brain/test_dataset_summary.py

"""
Tests for Dataset Quality Summary Agent.

Tests comprehensive quality scoring, LLM text generation, and decision support.
"""

import pytest

from brain.dataset_summary import (
    DatasetQualitySummary,
    create_quality_summary,
    integrate_quality_summary_into_manifest,
)
from quality.dataset_sanity import DatasetSanityIssue, DatasetSanityResult


class TestDatasetQualitySummary:
    """Test DatasetQualitySummary class."""

    def test_perfect_quality_score(self):
        """Dataset with no issues should get score of 100."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(
            dataset_sanity=sanity,
            label_sanity={"is_ok": True, "label_id": "ret_10m"},
        )

        assert summary.quality_score == 100
        assert summary.should_skip is False
        assert "Excellent data quality" in summary.llm_text

    def test_dataset_with_errors(self):
        """Dataset with errors should lose 30 points per error."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(
                    code="label_missing",
                    message="Label column not found",
                    severity="error",
                ),
                DatasetSanityIssue(
                    code="high_missing",
                    message="High missing value ratio",
                    severity="error",
                ),
            ],
            summary={"n_rows": 1000, "n_cols": 10},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        # 100 - (30 * 2) = 40
        assert summary.quality_score == 40
        assert summary.should_skip is True

    def test_dataset_with_warnings(self):
        """Dataset with warnings should lose 10 points per warning."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(
                    code="few_rows",
                    message="Dataset has fewer than 5000 rows",
                    severity="warning",
                ),
                DatasetSanityIssue(
                    code="few_cols",
                    message="Dataset has fewer than 10 columns",
                    severity="warning",
                ),
            ],
            summary={"n_rows": 3000, "n_cols": 8},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        # 100 - (10 * 2) = 80
        assert summary.quality_score == 80
        assert summary.should_skip is False

    def test_dataset_with_info_issues(self):
        """Dataset with info issues should lose 2 points per info."""
        sanity = DatasetSanityResult(
            ok=True,  # Info doesn't make it fail
            issues=[
                DatasetSanityIssue(
                    code="low_variance_feature",
                    message="Feature close has low variance",
                    severity="info",
                ),
            ],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        # 100 - 2 = 98
        assert summary.quality_score == 98
        assert summary.should_skip is False

    def test_label_sanity_not_ok(self):
        """Label not OK should lose 20 points."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(
            dataset_sanity=sanity,
            label_sanity={
                "is_ok": False,
                "label_id": "ret_10m",
                "issues": {},
            },
        )

        # 100 - 20 = 80
        assert summary.quality_score == 80

    def test_label_sanity_too_few_samples(self):
        """Too few label samples should lose 15 points."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(
            dataset_sanity=sanity,
            label_sanity={
                "is_ok": False,
                "label_id": "ret_10m",
                "issues": {
                    "too_few_samples": {
                        "n_samples": 1000,
                        "min_required": 5000,
                    }
                },
            },
        )

        # 100 - 20 (not OK) - 15 (too few) = 65
        assert summary.quality_score == 65

    def test_label_sanity_mean_bias(self):
        """Mean bias should lose 10 points."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(
            dataset_sanity=sanity,
            label_sanity={
                "is_ok": False,
                "label_id": "ret_10m",
                "issues": {
                    "mean_bias": {
                        "abs_mean_z": 2.5,
                        "max_allowed": 1.5,
                    }
                },
            },
        )

        # 100 - 20 (not OK) - 10 (bias) = 70
        assert summary.quality_score == 70

    def test_label_sanity_heavy_tails(self):
        """Heavy tails should lose 10 points."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(
            dataset_sanity=sanity,
            label_sanity={
                "is_ok": False,
                "label_id": "ret_10m",
                "issues": {
                    "too_heavy_tails": {
                        "extreme_tail_ratio": 0.05,
                        "max_allowed": 0.02,
                    }
                },
            },
        )

        # 100 - 20 (not OK) - 10 (tails) = 70
        assert summary.quality_score == 70

    def test_label_sanity_imbalance(self):
        """Label imbalance should lose 15 points."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(
            dataset_sanity=sanity,
            label_sanity={
                "is_ok": False,
                "label_id": "tb_10m_atr1",
                "task_type": "cls",
                "issues": {
                    "label_imbalance": {
                        "majority_ratio": 0.995,
                        "max_allowed": 0.99,
                    }
                },
            },
        )

        # 100 - 20 (not OK) - 15 (imbalance) = 65
        assert summary.quality_score == 65

    def test_combined_issues(self):
        """Multiple issues should accumulate deductions."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(
                    code="error1",
                    message="Critical error",
                    severity="error",
                ),
                DatasetSanityIssue(
                    code="warning1",
                    message="Warning",
                    severity="warning",
                ),
            ],
            summary={"n_rows": 5000, "n_cols": 15},
        )

        summary = DatasetQualitySummary(
            dataset_sanity=sanity,
            label_sanity={
                "is_ok": False,
                "label_id": "ret_10m",
                "issues": {
                    "too_few_samples": {"n_samples": 1000},
                    "mean_bias": {"abs_mean_z": 2.0},
                },
            },
        )

        # 100 - 30 (error) - 10 (warning) - 20 (label not OK) - 15 (few samples) - 10 (bias) = 15
        assert summary.quality_score == 15
        assert summary.should_skip is True

    def test_score_cannot_go_below_zero(self):
        """Quality score should be clamped to [0, 100]."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(code=f"error{i}", message=f"Error {i}", severity="error")
                for i in range(10)  # 10 errors = -300 points
            ],
            summary={"n_rows": 100, "n_cols": 5},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        assert summary.quality_score == 0
        assert summary.should_skip is True


class TestLLMTextGeneration:
    """Test LLM text generation."""

    def test_llm_text_contains_score(self):
        """LLM text should contain quality score."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        assert "Score: 100/100" in summary.llm_text

    def test_llm_text_contains_dataset_stats(self):
        """LLM text should contain dataset statistics."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 15000, "n_cols": 30},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        assert "Rows: 15,000" in summary.llm_text
        assert "Columns: 30" in summary.llm_text

    def test_llm_text_shows_dataset_issues(self):
        """LLM text should list top 5 dataset issues."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(
                    code="label_missing",
                    message="Label column not found",
                    severity="error",
                ),
                DatasetSanityIssue(
                    code="high_missing",
                    message="High missing value ratio in feature_1",
                    severity="warning",
                ),
            ],
            summary={"n_rows": 5000, "n_cols": 15},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        assert "label_missing" in summary.llm_text
        assert "Label column not found" in summary.llm_text
        assert "high_missing" in summary.llm_text

    def test_llm_text_shows_label_stats(self):
        """LLM text should show label statistics."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(
            dataset_sanity=sanity,
            label_sanity={
                "is_ok": True,
                "label_id": "ret_10m",
                "task_type": "reg",
                "n_samples": 9500,
                "nan_ratio": 0.05,
                "mean": 0.0001,
                "std": 0.02,
                "abs_mean_z": 0.005,
            },
        )

        assert "Label ID: ret_10m" in summary.llm_text
        assert "Valid samples: 9,500" in summary.llm_text
        assert "mean=0.00010" in summary.llm_text
        assert "std=0.02000" in summary.llm_text

    def test_llm_text_recommendation_excellent(self):
        """Score >= 80 should get excellent recommendation."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        assert summary.quality_score >= 80
        assert "Excellent data quality" in summary.llm_text
        assert "Proceed with training" in summary.llm_text

    def test_llm_text_recommendation_acceptable(self):
        """Score 60-79 should get acceptable recommendation."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(
                    code="warning1",
                    message="Warning",
                    severity="warning",
                ),
                DatasetSanityIssue(
                    code="warning2",
                    message="Warning",
                    severity="warning",
                ),
                DatasetSanityIssue(
                    code="warning3",
                    message="Warning",
                    severity="warning",
                ),
            ],
            summary={"n_rows": 5000, "n_cols": 15},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        # 100 - (3 * 10) = 70, which is in acceptable range
        assert 60 <= summary.quality_score < 80
        assert "Acceptable data quality" in summary.llm_text
        assert "with caution" in summary.llm_text

    def test_llm_text_recommendation_poor(self):
        """Score < 60 should get poor recommendation."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(
                    code="error1",
                    message="Critical error",
                    severity="error",
                ),
                DatasetSanityIssue(
                    code="error2",
                    message="Critical error",
                    severity="error",
                ),
            ],
            summary={"n_rows": 1000, "n_cols": 5},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        assert summary.quality_score < 60
        assert "Poor data quality" in summary.llm_text
        assert "skip_training" in summary.llm_text or "ask_human" in summary.llm_text


class TestShouldSkipTraining:
    """Test should_skip decision logic."""

    def test_should_not_skip_excellent_quality(self):
        """Score >= 80 should not skip."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        assert summary.quality_score >= 80
        assert summary.should_skip is False

    def test_should_not_skip_acceptable_quality(self):
        """Score 60-79 should not skip."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(
                    code="warning1",
                    message="Minor issue",
                    severity="warning",
                ),
            ],
            summary={"n_rows": 5000, "n_cols": 15},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        # 100 - 10 = 90, should not skip
        assert summary.quality_score >= 60
        assert summary.should_skip is False

    def test_should_skip_poor_quality(self):
        """Score < 60 should skip."""
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(
                    code="error1",
                    message="Critical error",
                    severity="error",
                ),
                DatasetSanityIssue(
                    code="error2",
                    message="Critical error",
                    severity="error",
                ),
            ],
            summary={"n_rows": 1000, "n_cols": 5},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        # 100 - 60 = 40, should skip
        assert summary.quality_score < 60
        assert summary.should_skip is True

    def test_threshold_exactly_60(self):
        """Score exactly 60 should not skip (>= 60 passes)."""
        # Create scenario with exactly 60 score: 100 - 40 = 60
        sanity = DatasetSanityResult(
            ok=False,
            issues=[
                DatasetSanityIssue(code="warning1", message="W1", severity="warning"),
                DatasetSanityIssue(code="warning2", message="W2", severity="warning"),
                DatasetSanityIssue(code="warning3", message="W3", severity="warning"),
                DatasetSanityIssue(code="warning4", message="W4", severity="warning"),
            ],
            summary={"n_rows": 5000, "n_cols": 15},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        # 100 - (4 * 10) = 60
        assert summary.quality_score == 60
        assert summary.should_skip is False


class TestConvenienceFunctions:
    """Test convenience functions."""

    def test_create_quality_summary(self):
        """create_quality_summary() should create summary correctly."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = create_quality_summary(
            dataset_sanity=sanity,
            label_sanity={"is_ok": True, "label_id": "ret_10m"},
        )

        assert isinstance(summary, DatasetQualitySummary)
        assert summary.quality_score == 100
        assert summary.label_sanity is not None

    def test_create_quality_summary_without_label(self):
        """create_quality_summary() should work without label_sanity."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = create_quality_summary(dataset_sanity=sanity)

        assert isinstance(summary, DatasetQualitySummary)
        assert summary.quality_score == 100
        assert summary.label_sanity is None

    def test_to_dict(self):
        """to_dict() should return serializable dict."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)
        result = summary.to_dict()

        assert isinstance(result, dict)
        assert "quality_score" in result
        assert "should_skip" in result
        assert "dataset_sanity" in result
        assert "llm_text" in result
        assert result["quality_score"] == 100

    def test_integrate_quality_summary_into_manifest(self):
        """integrate_quality_summary_into_manifest() should add quality_summary key."""
        sanity = DatasetSanityResult(
            ok=True,
            issues=[],
            summary={"n_rows": 10000, "n_cols": 25},
        )

        summary = DatasetQualitySummary(dataset_sanity=sanity)

        manifest = {
            "dataset_sanity": {"ok": True},
            "feature_sanity": {"ok": True},
        }

        updated_manifest = integrate_quality_summary_into_manifest(manifest, summary)

        assert "quality_summary" in updated_manifest
        assert updated_manifest["quality_summary"]["quality_score"] == 100
        # Original keys should still exist
        assert "dataset_sanity" in updated_manifest
        assert "feature_sanity" in updated_manifest
