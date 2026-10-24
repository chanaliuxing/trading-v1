#!/usr/bin/env python3
"""
Integration test for P0.1: Data Readiness Checker integration in ml_scheduler

Verifies:
1. DataReadinessChecker is called before expensive preflight
2. Window adjustment happens when utilization < threshold
3. start_override and end_override are set correctly
4. Event metadata includes window adjustment info
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, AsyncMock
from typing import Dict, Any


class TestP01DataReadinessIntegration:
    """Test P0.1: 调度前数据防护 - DataReadinessChecker integration"""

    @pytest.fixture
    def mock_readiness_needs_adjust(self):
        """Mock DataReadinessChecker returning needs_window_adjust"""
        with patch("libs.ml.data_readiness_checker.DataReadinessChecker") as mock_cls:
            mock_instance = Mock()
            mock_rec = Mock()
            mock_rec.status = "needs_window_adjust"
            mock_rec.reason = "low_utilization"
            mock_rec.suggested_start = "2025-11-03 00:00:00"
            mock_rec.suggested_end = "2025-12-03 00:00:00"
            mock_rec.coverage = {"TSLA": {"rows": 100, "expected": 1000}}
            mock_instance.check.return_value = mock_rec
            mock_cls.return_value = mock_instance
            yield mock_instance

    @pytest.fixture
    def mock_readiness_ready(self):
        """Mock DataReadinessChecker returning ready"""
        with patch("libs.ml.data_readiness_checker.DataReadinessChecker") as mock_cls:
            mock_instance = Mock()
            mock_rec = Mock()
            mock_rec.status = "ready"
            mock_rec.reason = "sufficient_data"
            mock_rec.suggested_start = None
            mock_rec.suggested_end = None
            mock_rec.coverage = {"TSLA": {"rows": 5000, "expected": 5500}}
            mock_instance.check.return_value = mock_rec
            mock_cls.return_value = mock_instance
            yield mock_instance

    @pytest.fixture
    def mock_readiness_missing(self):
        """Mock DataReadinessChecker returning missing"""
        with patch("libs.ml.data_readiness_checker.DataReadinessChecker") as mock_cls:
            mock_instance = Mock()
            mock_rec = Mock()
            mock_rec.status = "missing"
            mock_rec.reason = "no_bars_found"
            mock_rec.suggested_start = None
            mock_rec.suggested_end = None
            mock_rec.coverage = {}
            mock_instance.check.return_value = mock_rec
            mock_cls.return_value = mock_instance
            yield mock_instance

    @pytest.mark.asyncio
    async def test_window_adjustment_on_low_utilization(self, mock_readiness_needs_adjust):
        """
        Test that window is adjusted when DataReadinessChecker returns needs_window_adjust

        Evidence:
        - ml_scheduler.py:757-767 implements window adjustment logic
        - params["window_days"], params["start_override"], params["end_override"] are updated
        """
        from scripts.ai_trader.ml_scheduler import _execute_oof

        params = {
            "symbols": ["TSLA"],
            "window_days": 60,  # Original request
            "horizon_min": 30,
        }

        # Mock all external dependencies
        with patch("scripts.ai_trader.ml_scheduler.is_market_closed", return_value=False), \
             patch("scripts.ai_trader.brain.preflight.preflight_check_all", return_value=(True, {})), \
             patch("scripts.ai_trader.ml_scheduler.subprocess.run") as mock_run:

            mock_run.return_value = Mock(returncode=0, stdout=b"", stderr=b"")

            result = await _execute_oof("test_job_001", params)

            # Verify window was adjusted
            assert params["window_days"] == 30, f"Expected window_days to be adjusted to 30, got {params['window_days']}"
            assert "start_override" in params, "start_override should be set"
            assert "end_override" in params, "end_override should be set"
            assert params["start_override"] == "2025-11-03 00:00:00"
            assert params["end_override"] == "2025-12-03 00:00:00"

            # Verify DataReadinessChecker was called
            assert mock_readiness_needs_adjust.check.called, "DataReadinessChecker.check() should be called"

    @pytest.mark.asyncio
    async def test_no_adjustment_when_ready(self, mock_readiness_ready):
        """
        Test that window is NOT adjusted when data is ready
        """
        from scripts.ai_trader.ml_scheduler import _execute_oof

        params = {
            "symbols": ["TSLA"],
            "window_days": 60,
            "horizon_min": 30,
        }

        with patch("scripts.ai_trader.ml_scheduler.is_market_closed", return_value=False), \
             patch("scripts.ai_trader.brain.preflight.preflight_check_all", return_value=(True, {})), \
             patch("scripts.ai_trader.ml_scheduler.subprocess.run") as mock_run:

            mock_run.return_value = Mock(returncode=0, stdout=b"", stderr=b"")

            result = await _execute_oof("test_job_002", params)

            # Verify window was NOT adjusted
            assert params["window_days"] == 60, "window_days should remain unchanged"
            assert "start_override" not in params or params["start_override"] is None

    @pytest.mark.asyncio
    async def test_fail_fast_on_missing_data(self, mock_readiness_missing):
        """
        Test that job fails fast when no data is available

        Evidence: ml_scheduler.py:768-777 returns early with error_code=data_not_ready
        """
        from scripts.ai_trader.ml_scheduler import _execute_oof

        params = {
            "symbols": ["TSLA"],
            "window_days": 60,
            "horizon_min": 30,
        }

        with patch("scripts.ai_trader.ml_scheduler.is_market_closed", return_value=False):
            result = await _execute_oof("test_job_003", params)

            # Verify fast failure
            assert result["ok"] is False, "Job should fail when data is missing"
            assert result["metrics"]["error_code"] == "data_not_ready"
            assert "no bars found" in result["stderr"].lower()

    def test_data_readiness_checker_integration_exists(self):
        """
        Verify that DataReadinessChecker is properly imported and integrated

        Evidence: ml_scheduler.py:727, 734-747
        """
        from scripts.ai_trader import ml_scheduler
        import inspect

        source = inspect.getsource(ml_scheduler._execute_oof)

        # Verify imports
        assert "from libs.ml.data_readiness_checker import DataReadinessChecker" in source

        # Verify instantiation
        assert "readiness_checker = DataReadinessChecker()" in source

        # Verify call
        assert "rec = readiness_checker.check(" in source

        # Verify window adjustment logic
        assert 'if rec.status == "needs_window_adjust"' in source
        assert 'params["window_days"]' in source
        assert 'params["start_override"]' in source
        assert 'params["end_override"]' in source

    def test_event_logging_includes_window_metadata(self):
        """
        Verify that window adjustment is logged for observability

        Evidence: ml_scheduler.py:765, 1181-1384
        """
        from scripts.ai_trader import ml_scheduler
        import inspect

        source = inspect.getsource(ml_scheduler._execute_oof)

        # Verify logging
        assert 'logger.info("window_days_adjusted"' in source
        assert "clamp_metadata" in source or "metrics[" in source


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
