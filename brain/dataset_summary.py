# brain/dataset_summary.py

"""
Dataset Quality Summary Agent.

Compresses dataset_sanity + label_sanity results into a unified quality score (0-100)
and generates LLM-friendly text summary for AI Brain decision making.

Based on upgradeFeatures16 specification (Part 3: Dataset Quality Summary Agent).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from quality.dataset_sanity import DatasetSanityResult


@dataclass
class DatasetQualitySummary:
    """
    Unified dataset quality summary for AI Brain.

    Compresses dataset sanity + label sanity into:
    - quality_score: 0-100 numeric score
    - llm_text: Human-readable summary for LLM
    - should_skip: Boolean recommendation

    Design philosophy:
    - Quality score combines multiple dimensions:
      - Dataset health (missing values, schema, data types)
      - Label health (distribution, variance, balance)
      - Feature health (correlation, leakage, variance)
    - LLM text provides actionable insights, not just metrics
    - Threshold-based decision support (skip if score < 60)

    Attributes:
        dataset_sanity: DatasetSanityResult from quality checks
        label_sanity: Optional label sanity dict
        quality_score: Computed 0-100 score
        llm_text: Human-readable summary
        should_skip: Recommendation to skip training
    """

    dataset_sanity: DatasetSanityResult
    label_sanity: Optional[Dict[str, Any]] = None
    quality_score: int = 0
    llm_text: str = ""
    should_skip: bool = False

    def __post_init__(self) -> None:
        """Compute quality metrics after initialization."""
        self.quality_score = self.compute_quality_score()
        self.llm_text = self.generate_llm_text()
        self.should_skip = self.compute_should_skip()

    def compute_quality_score(self) -> int:
        """
        Compute unified quality score (0-100).

        Scoring algorithm:
        - Start with 100 points
        - Deduct points for dataset issues:
          - Error severity: -30 points per error
          - Warning severity: -10 points per warning
          - Info severity: -2 points per info
        - Deduct points for label issues:
          - Label not OK: -20 points
          - Too few samples: -15 points
          - Mean bias: -10 points
          - Heavy tails: -10 points
          - Label imbalance: -15 points

        Returns:
            Integer score from 0 to 100
        """
        score = 100

        # Dataset sanity deductions
        # Process all issues regardless of ok status (info issues don't set ok=False)
        for issue in self.dataset_sanity.issues:
            if issue.severity == "error":
                score -= 30
            elif issue.severity == "warning":
                score -= 10
            elif issue.severity == "info":
                score -= 2

        # Label sanity deductions
        if self.label_sanity:
            if not self.label_sanity.get("is_ok", True):
                score -= 20

            issues = self.label_sanity.get("issues") or {}

            if "too_few_samples" in issues:
                score -= 15
            if "mean_bias" in issues:
                score -= 10
            if "too_heavy_tails" in issues:
                score -= 10
            if "label_imbalance" in issues:
                score -= 15

        # Ensure score stays in [0, 100]
        return max(0, min(100, score))

    def generate_llm_text(self) -> str:
        """
        Generate human-readable summary for AI Brain.

        Output format:
        - Overall quality score
        - Dataset health summary
        - Label health summary (if available)
        - Key issues
        - Recommendation

        Returns:
            Multi-line string summary
        """
        lines = []

        # Header
        lines.append(f"=== Dataset Quality Summary (Score: {self.quality_score}/100) ===")
        lines.append("")

        # Dataset health
        lines.append("Dataset Health:")
        summary = self.dataset_sanity.summary
        lines.append(f"  - Rows: {summary.get('n_rows', 0):,}")
        lines.append(f"  - Columns: {summary.get('n_cols', 0):,}")
        lines.append(f"  - Overall status: {'✓ OK' if self.dataset_sanity.ok else '✗ ISSUES DETECTED'}")

        if not self.dataset_sanity.ok:
            lines.append("  - Issues:")
            # Show top 5 issues
            for issue in self.dataset_sanity.issues[:5]:
                severity_symbol = "❌" if issue.severity == "error" else "⚠️" if issue.severity == "warning" else "ℹ️"
                lines.append(f"    {severity_symbol} [{issue.code}] {issue.message}")

        lines.append("")

        # Label health
        if self.label_sanity:
            lines.append("Label Health:")
            label_id = self.label_sanity.get("label_id", "unknown")
            task_type = self.label_sanity.get("task_type", "reg")
            n_samples = self.label_sanity.get("n_samples", 0)
            nan_ratio = self.label_sanity.get("nan_ratio", 0.0)

            lines.append(f"  - Label ID: {label_id}")
            lines.append(f"  - Task type: {task_type}")
            lines.append(f"  - Valid samples: {n_samples:,} (NaN ratio: {nan_ratio:.1%})")

            is_ok = self.label_sanity.get("is_ok", True)
            lines.append(f"  - Status: {'✓ OK' if is_ok else '✗ ISSUES DETECTED'}")

            if task_type == "reg":
                mean = self.label_sanity.get("mean")
                std = self.label_sanity.get("std")
                if mean is not None and std is not None:
                    lines.append(f"  - Distribution: mean={mean:.5f}, std={std:.5f}")
                    abs_mean_z = self.label_sanity.get("abs_mean_z")
                    if abs_mean_z is not None:
                        lines.append(f"  - Mean bias: |z|={abs_mean_z:.2f}")

            if self.label_sanity.get("majority_ratio") is not None:
                majority_ratio = self.label_sanity.get("majority_ratio", 0.0)
                lines.append(f"  - Class balance: majority={majority_ratio:.1%}")

            issues = self.label_sanity.get("issues") or {}
            if issues:
                lines.append("  - Issues:")
                for issue_key, issue_detail in issues.items():
                    lines.append(f"    ❌ {issue_key}: {issue_detail}")

        else:
            lines.append("Label Health: Not evaluated")

        lines.append("")

        # Recommendation
        lines.append("Recommendation:")
        if self.quality_score >= 80:
            lines.append("  ✅ Excellent data quality. Proceed with training.")
        elif self.quality_score >= 60:
            lines.append("  ⚠️ Acceptable data quality. Training may proceed with caution.")
        else:
            lines.append("  ❌ Poor data quality. Recommend skip_training or ask_human for review.")

        return "\n".join(lines)

    def compute_should_skip(self) -> bool:
        """
        Determine if training should be skipped based on quality score.

        Threshold logic:
        - Score < 60: Recommend skip (poor quality)
        - Score >= 60: OK to train (acceptable or good quality)

        Returns:
            True if training should be skipped
        """
        return self.quality_score < 60

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert summary to dictionary for serialization.

        Returns:
            Dict containing all summary fields
        """
        return {
            "quality_score": self.quality_score,
            "should_skip": self.should_skip,
            "dataset_sanity": self.dataset_sanity.brief_summary(),
            "label_sanity": self.label_sanity,
            "llm_text": self.llm_text,
        }


def create_quality_summary(
    dataset_sanity: DatasetSanityResult,
    label_sanity: Optional[Dict[str, Any]] = None,
) -> DatasetQualitySummary:
    """
    Convenience function to create DatasetQualitySummary.

    Args:
        dataset_sanity: DatasetSanityResult from quality checks
        label_sanity: Optional label sanity dict

    Returns:
        DatasetQualitySummary with computed metrics

    Example:
        >>> from quality.dataset_sanity import run_dataset_sanity_v2
        >>> sanity = run_dataset_sanity_v2(df)
        >>> summary = create_quality_summary(sanity, label_sanity)
        >>> if summary.should_skip:
        ...     print(summary.llm_text)
    """
    return DatasetQualitySummary(
        dataset_sanity=dataset_sanity,
        label_sanity=label_sanity,
    )


def integrate_quality_summary_into_manifest(
    manifest: Dict[str, Any],
    quality_summary: DatasetQualitySummary,
) -> Dict[str, Any]:
    """
    Add quality summary to dataset manifest.

    Args:
        manifest: Existing dataset manifest dict
        quality_summary: DatasetQualitySummary to integrate

    Returns:
        Updated manifest with quality_summary section

    Example:
        >>> manifest = {"dataset_sanity": {...}, "feature_sanity": {...}}
        >>> manifest = integrate_quality_summary_into_manifest(manifest, quality_summary)
        >>> print(manifest["quality_summary"]["quality_score"])
        75
    """
    updated_manifest = dict(manifest)
    updated_manifest["quality_summary"] = quality_summary.to_dict()
    return updated_manifest
