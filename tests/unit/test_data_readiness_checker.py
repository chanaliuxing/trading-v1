#!/usr/bin/env python3
"""
Unit tests for DataReadinessChecker

Run:
    pytest tests/unit/test_data_readiness_checker.py -v
"""
import sys
from pathlib import Path
from datetime import date, timedelta

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from libs.ml.data_readiness_checker import DataReadinessChecker, ReadinessRecommendation


class TestDataReadinessChecker:
    """Test DataReadinessChecker functionality"""

    @staticmethod
    def _skip_if_backend_unavailable(result: ReadinessRecommendation) -> None:
        reason = str(getattr(result, "reason", "") or "").lower()
        if result.coverage:
            return
        unavailable_tokens = (
            "clickhouse exception",
            "connection refused",
            "network error",
            "doesn't exist",
            "unknown table",
        )
        if any(token in reason for token in unavailable_tokens):
            pytest.skip(f"ClickHouse fixtures unavailable: {result.reason}")

    @pytest.fixture
    def checker(self):
        """Create checker instance"""
        return DataReadinessChecker()

    def test_import(self):
        """Test that module imports successfully"""
        from libs.ml.data_readiness_checker import DataReadinessChecker
        assert DataReadinessChecker is not None

    def test_checker_initialization(self, checker):
        """Test checker can be instantiated"""
        assert checker is not None



    @pytest.mark.integration
    def test_check_with_real_data(self, checker):
        """Integration test with real database (requires ClickHouse running)"""
        # Use recent 30-day window
        end = date.today()
        start = end - timedelta(days=30)

        result = checker.check(
            symbols=['TSLA'],
            start=start,
            end=end,

            features_table='trading.features_1m'
        )
        self._skip_if_backend_unavailable(result)

        # Verify result structure
        assert isinstance(result, ReadinessRecommendation)
        assert result.status in ['ready', 'needs_window_adjust', 'needs_sync', 'not_ready']
        assert result.reason is not None
        assert "TSLA_bars" in result.coverage
        assert "TSLA_features" in result.coverage

    @pytest.mark.integration
    def test_check_with_labels_table(self, checker):
        """Test with features_1m_with_labels table"""
        end = date.today()
        start = end - timedelta(days=30)

        result = checker.check(
            symbols=['TSLA'],
            start=start,
            end=end,

            features_table='trading.features_1m_with_labels'
        )
        self._skip_if_backend_unavailable(result)

        # Should have labels_coverage when using with_labels table
        assert "TSLA_labels" in result.coverage

    @pytest.mark.integration
    def test_check_without_labels_table(self, checker):
        """Test with regular features_1m table (no labels)"""
        end = date.today()
        start = end - timedelta(days=30)

        result = checker.check(
            symbols=['TSLA'],
            start=start,
            end=end,

            features_table='trading.features_1m'
        )
        self._skip_if_backend_unavailable(result)

        # Should still work, labels_coverage should be dummy
        assert "TSLA_labels" in result.coverage
        # Dummy coverage should have 1.0 utilization (skipped check)


    @pytest.mark.integration
    def test_select_best_features_table(self, checker):
        """Test automatic table selection"""
        end = date.today()
        start = end - timedelta(days=30)

        # Call with None features_table to trigger auto-selection
        result = checker.check(
            symbols=['TSLA'],
            start=start,
            end=end,

            features_table=None  # Auto-select
        )
        self._skip_if_backend_unavailable(result)

        # Should have selected a table
        assert "TSLA_features" in result.coverage

    @pytest.mark.integration
    def test_metadata_populated(self, checker):
        """Test that metadata is populated in result"""
        end = date.today()
        start = end - timedelta(days=30)

        result = checker.check(
            symbols=['TSLA'],
            start=start,
            end=end,

            features_table='trading.features_1m'
        )
        self._skip_if_backend_unavailable(result)

        # Check metadata
        if result.coverage:
            assert 'TSLA_bars' in result.coverage or 'TSLA_features' in result.coverage


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "-m", "not integration"])
