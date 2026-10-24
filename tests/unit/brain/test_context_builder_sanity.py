# tests/unit/brain/test_context_builder_sanity.py
"""
Tests for Brain Context Builder Sanity Functions (UF 21 Part 2).

Tests compression of sanity reports for LLM consumption.
"""

import pytest
from brain.context_builder import sanity_for_brain, build_brain_context


class TestSanityForBrain:
    """Test sanity_for_brain compression."""

    def test_basic_compression(self):
        """Basic sanity report compression."""
        full_report = {
            "level": "ok",
            "summary": "rows=1000 | features=10 | label=ok | features_health=ok | risks=none",
            "label_health": "ok",
            "feature_health": "ok",
            "risk_flags": [],
            "label_notes": [],
            "feature_notes": [],
            "stats": {"n_rows": 1000, "n_cols": 10},
            "raw_issues": [],
        }

        compressed = sanity_for_brain(full_report)

        # Should only have essential fields
        assert compressed["level"] == "ok"
        assert compressed["label_health"] == "ok"
        assert compressed["feature_health"] == "ok"
        assert compressed["risk_flags"] == []
        assert compressed["summary"] == full_report["summary"]

        # Should NOT have stats or raw_issues
        assert "stats" not in compressed
        assert "raw_issues" not in compressed

    def test_compression_with_warnings(self):
        """Compress report with warnings."""
        full_report = {
            "level": "warn",
            "summary": "Has warnings",
            "label_health": "warn",
            "feature_health": "ok",
            "risk_flags": ["label_class_imbalance", "ch_too_few_rows"],
            "label_notes": [
                "Label 'y' highly imbalanced",
                "Label has only 2 classes",
            ],
            "feature_notes": ["Too few rows (100 < 500)"],
        }

        compressed = sanity_for_brain(full_report)

        assert compressed["level"] == "warn"
        assert compressed["label_health"] == "warn"
        assert len(compressed["risk_flags"]) == 2
        assert len(compressed["label_notes"]) == 2
        assert len(compressed["feature_notes"]) == 1

    def test_notes_truncation(self):
        """Notes truncated to top 5."""
        full_report = {
            "level": "warn",
            "summary": "Many warnings",
            "label_health": "ok",
            "feature_health": "warn",
            "risk_flags": [],
            "label_notes": [],
            "feature_notes": [f"Feature issue {i}" for i in range(10)],
        }

        compressed = sanity_for_brain(full_report)

        # Should only keep first 5
        assert len(compressed["feature_notes"]) == 5
        assert compressed["feature_notes"][0] == "Feature issue 0"
        assert compressed["feature_notes"][4] == "Feature issue 4"

    def test_missing_fields_handled(self):
        """Missing fields handled gracefully."""
        partial_report = {
            "level": "ok",
        }

        compressed = sanity_for_brain(partial_report)

        assert compressed["level"] == "ok"
        assert compressed["label_health"] == "unknown"
        assert compressed["feature_health"] == "unknown"
        assert compressed["risk_flags"] == []
        assert compressed["summary"] == ""
        assert compressed["label_notes"] == []
        assert compressed["feature_notes"] == []

    def test_empty_report(self):
        """Empty report handled."""
        compressed = sanity_for_brain({})

        assert compressed["level"] == "unknown"
        assert compressed["label_health"] == "unknown"
        assert compressed["feature_health"] == "unknown"


class TestBuildBrainContext:
    """Test build_brain_context function."""

    def test_basic_context_no_sanity(self):
        """Basic context without sanity report."""
        ctx = build_brain_context(
            run_id="test_run_001",
            symbol="AAPL",
            metrics={"sharpe": 1.5, "ic": 0.03},
        )

        assert ctx["run_id"] == "test_run_001"
        assert ctx["symbol"] == "AAPL"
        assert ctx["metrics"]["sharpe"] == 1.5
        assert ctx["sanity"]["level"] == "unknown"
        assert ctx["sanity"]["risk_flags"] == []

    def test_context_with_sanity_report(self):
        """Context with sanity report."""
        sanity_report = {
            "level": "warn",
            "summary": "Some warnings",
            "label_health": "warn",
            "feature_health": "ok",
            "risk_flags": ["label_class_imbalance"],
            "label_notes": ["Label imbalanced"],
            "feature_notes": [],
        }

        ctx = build_brain_context(
            run_id="test_run_002",
            symbol="NVDA",
            metrics={"sharpe": 2.0},
            sanity_report=sanity_report,
        )

        assert ctx["run_id"] == "test_run_002"
        assert ctx["sanity"]["level"] == "warn"
        assert ctx["sanity"]["label_health"] == "warn"
        assert "label_class_imbalance" in ctx["sanity"]["risk_flags"]

    def test_context_with_additional_kwargs(self):
        """Context with additional keyword arguments."""
        ctx = build_brain_context(
            run_id="test_run_003",
            symbol="TSLA",
            metrics={"sharpe": 1.8},
            config={"model": "xgb", "features": 10},
            artifacts={"model_path": "/path/to/model"},
        )

        assert ctx["config"]["model"] == "xgb"
        assert ctx["artifacts"]["model_path"] == "/path/to/model"

    def test_sanity_compression_in_context(self):
        """Sanity report properly compressed in context."""
        full_sanity = {
            "level": "ok",
            "summary": "All good",
            "label_health": "ok",
            "feature_health": "ok",
            "risk_flags": [],
            "label_notes": [],
            "feature_notes": [],
            "stats": {"n_rows": 10000, "n_cols": 50},  # Should be removed
            "raw_issues": [{"code": "test"}],  # Should be removed
        }

        ctx = build_brain_context(
            run_id="test_run_004",
            symbol="AAPL",
            metrics={"sharpe": 1.5},
            sanity_report=full_sanity,
        )

        # Sanity should be compressed (no stats, no raw_issues)
        assert "stats" not in ctx["sanity"]
        assert "raw_issues" not in ctx["sanity"]
        assert "level" in ctx["sanity"]
        assert "label_health" in ctx["sanity"]
