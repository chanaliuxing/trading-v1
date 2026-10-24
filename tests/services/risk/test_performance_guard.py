"""
Tests for PerformanceGuard service.

Tests:
- Sharpe threshold triggering
- EV threshold triggering
- Cool-off period enforcement
- Dry-run mode
- Action logging
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from services.risk.performance_guard import PerformanceGuard, PerformanceGuardConfig

EASTERN = ZoneInfo("America/New_York")


@pytest.fixture
def sample_performance_data():
    """Generate sample performance data for testing."""
    dates = pd.date_range(end=datetime.now(EASTERN).date(), periods=30, freq="D")
    data = {
        "date": dates,
        "ret": [0.01, -0.005, 0.015, -0.002, 0.008] * 6,  # Mixed returns
        "sharpe_20": [0.5] * 10 + [0.2] * 10 + [0.4] * 10,  # Low Sharpe in middle
        "sharpe_60": [0.5] * 30,
        "wins": [1, 0, 1, 0, 1] * 6,
        "losses": [0, 1, 0, 1, 0] * 6,
        "pnl": [100, -50, 150, -20, 80] * 6,
        "pnl_cum": list(range(0, 3000, 100)),
    }
    return pd.DataFrame(data)


@pytest.fixture
def low_sharpe_data():
    """Performance data with consistently low Sharpe."""
    dates = pd.date_range(end=datetime.now(EASTERN).date(), periods=30, freq="D")
    data = {
        "date": dates,
        "ret": [-0.01] * 30,  # Losing returns
        "sharpe_20": [0.1] * 30,  # Below threshold
        "sharpe_60": [0.1] * 30,
        "wins": [0] * 30,
        "losses": [1] * 30,
        "pnl": [-100] * 30,
        "pnl_cum": list(range(0, -3000, -100)),
    }
    return pd.DataFrame(data)


@pytest.fixture
def config_reduce():
    """Configuration for reduce action."""
    return PerformanceGuardConfig({
        "enabled": True,
        "window_days": 20,
        "min_sharpe": 0.3,
        "min_ev": 0.0,
        "action": "reduce",
        "reduce_factor": 0.5,
        "cool_off_days": 3,
        "dry_run": True,
        "data_source": "csv",
        "csv_path": "test_data.csv",
    })


@pytest.fixture
def config_pause():
    """Configuration for pause action."""
    return PerformanceGuardConfig({
        "enabled": True,
        "window_days": 20,
        "min_sharpe": 0.3,
        "min_ev": 0.0,
        "action": "pause",
        "reduce_factor": 0.0,
        "cool_off_days": 3,
        "dry_run": True,
        "data_source": "csv",
        "csv_path": "test_data.csv",
    })


class TestPerformanceGuardConfig:
    """Test configuration parsing."""

    def test_default_values(self):
        """Test default configuration values."""
        config = PerformanceGuardConfig({})
        assert config.enabled is False
        assert config.window_days == 20
        assert config.min_sharpe == 0.3
        assert config.dry_run is True  # Safety default

    def test_custom_values(self):
        """Test custom configuration."""
        config = PerformanceGuardConfig({
            "enabled": True,
            "window_days": 60,
            "min_sharpe": 0.5,
            "action": "pause",
        })
        assert config.enabled is True
        assert config.window_days == 60
        assert config.min_sharpe == 0.5
        assert config.action == "pause"


class TestPerformanceGuard:
    """Test PerformanceGuard service logic."""

    @pytest.mark.asyncio
    async def test_initialization(self, config_reduce, tmp_path):
        """Test service initialization."""
        guard = PerformanceGuard(config_reduce)
        guard.state_file = tmp_path / "state.json"

        await guard.initialize()

        assert guard.current_state == "normal"
        assert guard.last_action_date is None

    @pytest.mark.asyncio
    async def test_initialization_with_existing_state(self, config_reduce, tmp_path):
        """Test loading existing state."""
        # Create state file
        state_file = tmp_path / "state.json"
        last_action = datetime.now(EASTERN) - timedelta(days=1)
        state = {
            "state": "reduced",
            "last_action_date": last_action.isoformat(),
        }
        with open(state_file, "w") as f:
            json.dump(state, f)

        guard = PerformanceGuard(config_reduce)
        guard.state_file = state_file
        await guard.initialize()

        assert guard.current_state == "reduced"
        assert guard.last_action_date is not None

    def test_calculate_recent_metrics_healthy(self, sample_performance_data):
        """Test metrics calculation with healthy data."""
        guard = PerformanceGuard(PerformanceGuardConfig({}))

        sharpe, ev = guard.calculate_recent_metrics(sample_performance_data)

        assert sharpe > 0
        assert ev != 0  # Should have some expectancy

    def test_calculate_recent_metrics_poor(self, low_sharpe_data):
        """Test metrics calculation with poor performance."""
        guard = PerformanceGuard(PerformanceGuardConfig({}))

        sharpe, ev = guard.calculate_recent_metrics(low_sharpe_data)

        assert sharpe < 0.3  # Below threshold
        assert ev < 0  # Negative expectancy

    def test_should_take_action_low_sharpe(self, config_reduce):
        """Test action trigger on low Sharpe."""
        guard = PerformanceGuard(config_reduce)

        should_act, reason = guard.should_take_action(sharpe=0.2, ev=50.0)

        assert should_act is True
        assert "low_sharpe" in reason

    def test_should_take_action_low_ev(self, config_reduce):
        """Test action trigger on low EV."""
        config = PerformanceGuardConfig({
            "min_sharpe": 0.3,
            "min_ev": 100.0,
        })
        guard = PerformanceGuard(config)

        should_act, reason = guard.should_take_action(sharpe=0.5, ev=50.0)

        assert should_act is True
        assert "low_ev" in reason

    def test_should_take_action_healthy(self, config_reduce):
        """Test no action when metrics are healthy."""
        guard = PerformanceGuard(config_reduce)

        should_act, reason = guard.should_take_action(sharpe=0.5, ev=100.0)

        assert should_act is False
        assert "healthy" in reason

    def test_should_take_action_cool_off(self, config_reduce):
        """Test cool-off period prevents actions."""
        guard = PerformanceGuard(config_reduce)
        guard.last_action_date = datetime.now(EASTERN)  # Just acted

        should_act, reason = guard.should_take_action(sharpe=0.1, ev=-50.0)

        assert should_act is False
        assert "cool_off" in reason

    def test_should_take_action_after_cool_off(self, config_reduce):
        """Test action allowed after cool-off expires."""
        guard = PerformanceGuard(config_reduce)
        guard.last_action_date = datetime.now(EASTERN) - timedelta(days=4)  # Past cool-off

        should_act, reason = guard.should_take_action(sharpe=0.1, ev=-50.0)

        assert should_act is True
        assert "low_sharpe" in reason

    @pytest.mark.asyncio
    async def test_apply_action_dry_run(self, config_reduce, tmp_path):
        """Test dry-run mode logs but doesn't apply changes."""
        guard = PerformanceGuard(config_reduce)
        guard.state_file = tmp_path / "state.json"
        guard.ch_client = None  # No ClickHouse in test

        await guard.apply_action("reduce", "low_sharpe", 0.2, 50.0)

        # State should not change in dry-run
        assert guard.current_state == "normal"
        assert guard.last_action_date is None

    @pytest.mark.asyncio
    async def test_apply_action_reduce(self, config_reduce, tmp_path):
        """Test actual reduce action (non-dry-run)."""
        config = PerformanceGuardConfig({
            **config_reduce.__dict__,
            "dry_run": False,
        })
        guard = PerformanceGuard(config)
        guard.state_file = tmp_path / "state.json"
        guard.ch_client = None

        risk_state_file = tmp_path / "risk_reduction_active.json"

        # Mock the risk state file path
        with patch("services.risk.performance_guard.Path") as mock_path:
            mock_path.return_value = risk_state_file

            await guard.apply_action("reduce", "low_sharpe", 0.2, 50.0)

        assert guard.current_state == "reduced"
        assert guard.last_action_date is not None

    @pytest.mark.asyncio
    async def test_apply_action_pause(self, config_pause, tmp_path):
        """Test pause action."""
        config = PerformanceGuardConfig({
            **config_pause.__dict__,
            "dry_run": False,
        })
        guard = PerformanceGuard(config)
        guard.state_file = tmp_path / "state.json"
        guard.ch_client = None

        pause_file = tmp_path / "execution_paused.json"

        # Mock the pause file path
        with patch("services.risk.performance_guard.Path") as mock_path:
            mock_path.return_value = pause_file

            await guard.apply_action("pause", "low_sharpe", 0.1, -50.0)

        assert guard.current_state == "paused"

    @pytest.mark.asyncio
    async def test_fetch_from_csv(self, config_reduce, tmp_path, sample_performance_data):
        """Test fetching data from CSV."""
        csv_path = tmp_path / "test_data.csv"
        sample_performance_data.to_csv(csv_path, index=False)

        config = PerformanceGuardConfig({
            **config_reduce.__dict__,
            "csv_path": str(csv_path),
        })
        guard = PerformanceGuard(config)

        df = guard._fetch_from_csv()

        assert df is not None
        assert len(df) == 20  # Should limit to window_days

    @pytest.mark.asyncio
    async def test_check_and_apply_insufficient_data(self, config_reduce):
        """Test check with insufficient data."""
        guard = PerformanceGuard(config_reduce)

        # Mock fetch to return too little data
        async def mock_fetch():
            return pd.DataFrame({"date": [], "ret": []})

        guard.fetch_performance_data = mock_fetch

        # Should not raise, just log warning
        await guard.check_and_apply()

    @pytest.mark.asyncio
    async def test_check_and_apply_triggers_action(self, config_reduce, low_sharpe_data, tmp_path):
        """Test full check triggers action on poor performance."""
        csv_path = tmp_path / "test_data.csv"
        low_sharpe_data.to_csv(csv_path, index=False)

        config = PerformanceGuardConfig({
            **config_reduce.__dict__,
            "csv_path": str(csv_path),
            "dry_run": True,  # Keep dry-run for test
        })
        guard = PerformanceGuard(config)
        guard.state_file = tmp_path / "state.json"
        guard.ch_client = None
        await guard.initialize()

        await guard.check_and_apply()

        # In dry-run, state doesn't change but action would have been triggered
        # We can verify by checking logs (would need log capture)

    @pytest.mark.asyncio
    async def test_check_and_apply_healthy(self, config_reduce, sample_performance_data, tmp_path):
        """Test check passes with healthy metrics."""
        csv_path = tmp_path / "test_data.csv"
        sample_performance_data.to_csv(csv_path, index=False)

        config = PerformanceGuardConfig({
            **config_reduce.__dict__,
            "csv_path": str(csv_path),
            "min_sharpe": 0.1,  # Low threshold so data passes
        })
        guard = PerformanceGuard(config)
        guard.state_file = tmp_path / "state.json"
        await guard.initialize()

        await guard.check_and_apply()

        # Should remain in normal state
        assert guard.current_state == "normal"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
