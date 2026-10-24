# tests/unit/quality/test_report.py
"""
Tests for Unified Sanity Report (UF 21 Part 1).

Tests aggregation of ClickHouse + DatasetSanity into SanityReport.
"""

import pytest
from quality.report import (
    SanityReport,
    build_sanity_report,
    sanity_report_to_dict,
)
from quality.dataset_sanity import DatasetSanityIssue
from quality.clickhouse_stats import TableStats, ColumnStats


class TestSanityReport:
    """Test SanityReport dataclass."""

    def test_basic_creation(self):
        """Create basic sanity report."""
        report = SanityReport(
            level="ok",
            summary="rows=1000 | features=10 | label=ok | features_health=ok | risks=none",
            label_health="ok",
            feature_health="ok",
        )

        assert report.level == "ok"
        assert report.label_health == "ok"
        assert report.feature_health == "ok"
        assert len(report.risk_flags) == 0

    def test_with_risk_flags(self):
        """Create report with risk flags."""
        report = SanityReport(
            level="warn",
            summary="Has warnings",
            label_health="warn",
            feature_health="ok",
            risk_flags=["label_class_imbalance", "ch_too_few_rows"],
        )

        assert report.level == "warn"
        assert len(report.risk_flags) == 2
        assert "label_class_imbalance" in report.risk_flags


class TestBuildSanityReport:
    """Test build_sanity_report function."""

    def test_healthy_dataset_no_issues(self):
        """Healthy dataset with no issues."""
        table_stats = TableStats(
            n_rows=10000,
            cols={
                "close": ColumnStats(
                    name="close",
                    nulls=0,
                    null_ratio=0.0,
                    uniq=10000,
                    min=100.0,
                    max=200.0,
                    mean=150.0,
                    std=15.0,
                )
            }
        )

        report = build_sanity_report(
            issues=[],
            ch_issues=[],
            table_stats=table_stats,
            label_col="y",
        )

        assert report.level == "ok"
        assert report.label_health == "ok"
        assert report.feature_health == "ok"
        assert len(report.risk_flags) == 0
        assert report.stats["n_rows"] == 10000

    def test_label_issues_detected(self):
        """Label issues properly categorized."""
        issues = [
            DatasetSanityIssue(
                code="label_class_imbalance",
                message="Label 'y' highly imbalanced",
                severity="warning",
            ),
            DatasetSanityIssue(
                code="label_too_many_nan",
                message="Label 'y' has too many NaNs",
                severity="error",
            ),
        ]

        report = build_sanity_report(
            issues=issues,
            ch_issues=[],
            table_stats=None,
            label_col="y",
        )

        assert report.level == "error"  # Has error severity
        assert report.label_health == "error"
        assert len(report.label_notes) == 2
        assert "label_class_imbalance" in report.risk_flags
        assert "label_too_many_nan" in report.risk_flags

    def test_feature_issues_detected(self):
        """Feature issues properly categorized."""
        ch_issues = [
            {
                "severity": "warning",
                "code": "ch_high_null_ratio",
                "message": "Column 'price' null_ratio=0.6",
                "column": "price",
            },
            {
                "severity": "warning",
                "code": "ch_near_zero_var",
                "message": "Column 'const_col' std≈0",
                "column": "const_col",
            },
        ]

        report = build_sanity_report(
            issues=[],
            ch_issues=ch_issues,
            table_stats=None,
            label_col="y",
        )

        assert report.level == "warn"
        assert report.feature_health == "warn"
        assert len(report.feature_notes) == 2
        assert "ch_high_null_ratio" in report.risk_flags
        assert "ch_near_zero_var" in report.risk_flags

    def test_mixed_label_and_feature_issues(self):
        """Mix of label and feature issues."""
        issues = [
            DatasetSanityIssue(
                code="label_class_imbalance",
                message="Label imbalanced",
                severity="warning",
            ),
        ]
        ch_issues = [
            {
                "severity": "error",
                "code": "ch_zero_rows",
                "message": "Zero rows",
            },
        ]

        report = build_sanity_report(
            issues=issues,
            ch_issues=ch_issues,
            table_stats=None,
            label_col="y",
        )

        assert report.level == "error"  # ch_zero_rows is error
        assert len(report.label_notes) == 1
        assert len(report.feature_notes) == 1
        assert "label_class_imbalance" in report.risk_flags
        assert "ch_zero_rows" in report.risk_flags

    def test_summary_generation(self):
        """Summary string properly generated."""
        table_stats = TableStats(
            n_rows=5000,
            cols={"f1": ColumnStats("f1", 0, 0.0, 5000, 1.0, 10.0, 5.0, 2.0)}
        )

        report = build_sanity_report(
            issues=[],
            ch_issues=[],
            table_stats=table_stats,
            label_col="y",
        )

        assert "rows=5000" in report.summary
        assert "features=1" in report.summary
        assert "label=ok" in report.summary
        assert "features_health=ok" in report.summary

    def test_high_null_cols_stats(self):
        """High null columns tracked in stats."""
        table_stats = TableStats(
            n_rows=1000,
            cols={
                "col1": ColumnStats("col1", 800, 0.8, 200, 1.0, 10.0, 5.0, 2.0),
                "col2": ColumnStats("col2", 600, 0.6, 400, 1.0, 10.0, 5.0, 2.0),
                "col3": ColumnStats("col3", 100, 0.1, 900, 1.0, 10.0, 5.0, 2.0),
            }
        )

        report = build_sanity_report(
            issues=[],
            ch_issues=[],
            table_stats=table_stats,
            label_col="y",
        )

        assert "high_null_cols" in report.stats
        # col1 (0.8) and col2 (0.6) should be included
        high_null = report.stats["high_null_cols"]
        assert len(high_null) == 2
        assert high_null[0]["name"] == "col1"  # Highest first
        assert high_null[0]["null_ratio"] == 0.8

    def test_risk_flags_deduplication(self):
        """Risk flags are deduplicated."""
        issues = [
            DatasetSanityIssue(
                code="label_class_imbalance",
                message="Imbalance 1",
                severity="warning",
            ),
            DatasetSanityIssue(
                code="label_class_imbalance",
                message="Imbalance 2",
                severity="warning",
            ),
        ]

        report = build_sanity_report(
            issues=issues,
            ch_issues=[],
            table_stats=None,
            label_col="y",
        )

        # Should only have one "label_class_imbalance"
        assert report.risk_flags.count("label_class_imbalance") == 1


class TestSanityReportToDict:
    """Test sanity_report_to_dict serialization."""

    def test_basic_serialization(self):
        """Basic report serialization."""
        report = SanityReport(
            level="ok",
            summary="All good",
            label_health="ok",
            feature_health="ok",
            risk_flags=[],
            stats={"n_rows": 1000},
        )

        d = sanity_report_to_dict(report)

        assert isinstance(d, dict)
        assert d["level"] == "ok"
        assert d["summary"] == "All good"
        assert d["stats"]["n_rows"] == 1000

    def test_serialization_with_nested_data(self):
        """Serialization with nested data."""
        report = SanityReport(
            level="warn",
            summary="Some warnings",
            label_health="warn",
            label_notes=["Note 1", "Note 2"],
            feature_health="ok",
            feature_notes=[],
            risk_flags=["ch_too_few_rows"],
            stats={"n_rows": 100, "n_cols": 5},
            raw_issues=[
                {
                    "severity": "warning",
                    "code": "ch_too_few_rows",
                    "message": "Too few rows",
                }
            ],
        )

        d = sanity_report_to_dict(report)

        assert d["level"] == "warn"
        assert len(d["label_notes"]) == 2
        assert len(d["risk_flags"]) == 1
        assert len(d["raw_issues"]) == 1
        assert d["raw_issues"][0]["code"] == "ch_too_few_rows"
