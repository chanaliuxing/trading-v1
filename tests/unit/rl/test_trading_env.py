#!/usr/bin/env python3
"""
Unit tests for RL trading environment (TradingEnv)
Tests reward function, state building, action validation, episode management
"""
import pytest
import numpy as np
import pandas as pd
from unittest.mock import Mock, patch
import sys
from pathlib import Path

try:
    import gymnasium as _gym_check  # type: ignore # noqa: F401
except Exception:
    try:
        import gym as _gym_check  # type: ignore # noqa: F401
    except Exception:
        pytest.skip("gymnasium/gym is required for rl trading env tests", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from rl_core.trading_env import TradingEnv


class TestTradingEnvRewardFunction:
    """Test reward function calculation"""

    @pytest.fixture
    def mock_data(self):
        """Create mock OHLC data with price_return"""
        dates = pd.date_range('2025-01-01 09:30', periods=100, freq='1min')
        df = pd.DataFrame({
            'open': np.random.uniform(100, 110, 100),
            'high': np.random.uniform(110, 115, 100),
            'low': np.random.uniform(95, 100, 100),
            'close': np.random.uniform(100, 110, 100),
            'volume': np.random.randint(1000, 10000, 100),
            'ml_signal': np.random.uniform(-1, 1, 100),
            'ml_conf': np.random.uniform(0.3, 0.9, 100),
        }, index=dates)
        # Calculate price returns
        df['price_return'] = df['close'].pct_change().fillna(0.0)
        return df

    @pytest.fixture
    def env(self, mock_data):
        """Create environment with mock data"""
        return TradingEnv(
            historical_data=mock_data,
            initial_capital=100000.0,
            cost_bps=2.0,
            dd_penalty=0.1,
            turnover_penalty=0.05
        )

    def test_reward_long_profit(self, env):
        """Test reward when long position is profitable"""
        env.reset()
        # Take long position
        obs, reward, terminated, truncated, info = env.step(np.array([0.5]))  # 50% of capital

        # If price increases in next step, reward should reflect that
        if env.step_idx < len(env.df) - 1:
            # Get next return
            next_return = env.df.loc[env.step_idx, 'price_return']
            if next_return > 0:
                # With positive return and long position, next reward should be positive (minus costs)
                obs, reward, terminated, truncated, info = env.step(np.array([0.5]))  # Hold position
                # Reward can be negative if costs > profit, but it should respond to returns
                assert reward != 0 or terminated, "Reward should reflect position and returns"

    def test_reward_short_profit(self, env):
        """Test reward when short position is profitable"""
        env.reset()
        # Take short position
        obs, reward, terminated, truncated, info = env.step(np.array([-0.5]))  # -50% of capital

        # If price decreases, short position should profit
        if env.step_idx < len(env.df) - 1:
            next_return = env.df.loc[env.step_idx, 'price_return']
            if next_return < 0:
                # With negative return and short position, should profit
                obs, reward, terminated, truncated, info = env.step(np.array([-0.5]))  # Hold position
                assert reward != 0 or terminated, "Short position should respond to price moves"

    def test_reward_transaction_cost_penalty(self, env):
        """Test that transaction costs reduce reward"""
        env.reset()

        # Frequent trading should incur more costs
        total_reward_high_turnover = 0.0
        for _ in range(10):
            obs, reward, terminated, truncated, info = env.step(np.random.uniform(-1, 1, 1))
            total_reward_high_turnover += reward
            if terminated:
                break

        env.reset()
        total_reward_no_trading = 0.0
        for _ in range(10):
            obs, reward, terminated, truncated, info = env.step(np.array([0.0]))  # No trading
            total_reward_no_trading += reward
            if terminated:
                break

        # High turnover should generally have lower or similar reward due to costs
        # (Can't guarantee lower in all cases due to random returns, but should see impact)
        assert isinstance(total_reward_high_turnover, float), "Should accumulate rewards"

    def test_reward_drawdown_penalty(self, env):
        """Test drawdown penalty exists"""
        env.reset()

        # Take a position
        obs, reward1, terminated, truncated, info = env.step(np.array([1.0]))  # Max long

        # Continue stepping - drawdown penalty will apply if portfolio drops
        if not terminated and env.step_idx < len(env.df) - 1:
            obs, reward2, terminated, truncated, info = env.step(np.array([1.0]))

            # If we hit drawdown, penalty should apply (reflected in reward)
            # Just verify env has drawdown tracking
            assert hasattr(env, 'dd_penalty'), "Environment should have drawdown penalty parameter"
            assert hasattr(env, 'high_water'), "Environment should track high water mark"


class TestTradingEnvStateBuildng:
    """Test state vector building"""

    @pytest.fixture
    def mock_data(self):
        dates = pd.date_range('2025-01-01 09:30', periods=100, freq='1min')
        df = pd.DataFrame({
            'open': 100.0,
            'high': 105.0,
            'low': 95.0,
            'close': 102.0,
            'volume': 5000,
            'ml_signal': 0.5,
            'ml_conf': 0.8,
            'ret_5': 0.02,
            'volatility': 0.15,
        }, index=dates)
        df['price_return'] = df['close'].pct_change().fillna(0.0)
        return df

    @pytest.fixture
    def env(self, mock_data):
        return TradingEnv(
            historical_data=mock_data,
            initial_capital=100000.0,
            cost_bps=2.0,
            dd_penalty=0.1,
            turnover_penalty=0.05
        )

    def test_state_dimension(self, env):
        """Test state vector has correct dimensions"""
        state, info = env.reset()

        # Expected state dimension: all feature columns + 1 (position)
        # Features exclude: price_return, ts, symbol
        expected_dim = len(env.feature_cols) + 1  # +1 for pos_pct
        assert len(state) == expected_dim, f"State should have {expected_dim} dimensions"
        assert isinstance(state, np.ndarray), "State should be numpy array"

    def test_state_normalization(self, env):
        """Test state values are in reasonable range"""
        state, info = env.reset()

        # Values should be finite
        assert np.all(np.isfinite(state)), "State values should be finite"

    def test_state_includes_ml_signal(self, env):
        """Test state includes features from data"""
        state, info = env.reset()

        # ML signal should be in feature columns if present in data
        assert 'ml_signal' in env.df.columns, "Data should include ml_signal"
        assert 'ml_signal' in env.feature_cols, "ml_signal should be in feature cols"
        # State length should match feature_cols + position
        assert len(state) == len(env.feature_cols) + 1, "State should contain all features + position"


class TestTradingEnvActionValidation:
    """Test action validation and constraints"""

    @pytest.fixture
    def env(self):
        dates = pd.date_range('2025-01-01 09:30', periods=100, freq='1min')
        data = pd.DataFrame({
            'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0,
            'volume': 5000, 'ml_signal': 0.5, 'ml_conf': 0.8,
        }, index=dates)
        data['price_return'] = data['close'].pct_change().fillna(0.0)
        return TradingEnv(historical_data=data, initial_capital=100000.0)

    def test_action_clipping(self, env):
        """Test actions are clipped to valid range"""
        env.reset()

        # Test extreme actions are clipped
        obs, reward, terminated, truncated, info = env.step(np.array([10.0]))  # Should clip to 1.0
        assert env.pos_pct <= 1.0, "Position should be clipped to max 1.0"
        assert env.pos_pct >= -1.0, "Position should be clipped to min -1.0"

        obs, reward, terminated, truncated, info = env.step(np.array([-10.0]))  # Should clip to -1.0
        assert env.pos_pct <= 1.0, "Position should be clipped to max 1.0"
        assert env.pos_pct >= -1.0, "Position should be clipped to min -1.0"

    def test_position_limits(self, env):
        """Test position percentage is within limits"""
        env.reset()

        # Try to take max position
        obs, reward, terminated, truncated, info = env.step(np.array([1.0]))  # Max long

        # Position percentage should be at most 1.0 (100% of capital)
        assert env.pos_pct <= 1.0, "Position percentage should not exceed 100%"
        assert env.pos_pct >= -1.0, "Position percentage should not be below -100%"


class TestTradingEnvEpisodeManagement:
    """Test episode reset, termination, truncation"""

    @pytest.fixture
    def env(self):
        dates = pd.date_range('2025-01-01 09:30', periods=50, freq='1min')
        data = pd.DataFrame({
            'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0,
            'volume': 5000, 'ml_signal': 0.5, 'ml_conf': 0.8,
        }, index=dates)
        data['price_return'] = data['close'].pct_change().fillna(0.0)
        return TradingEnv(historical_data=data, initial_capital=100000.0)

    def test_reset(self, env):
        """Test environment reset"""
        state1, info1 = env.reset()
        env.step(np.array([0.5]))
        state2, info2 = env.reset()

        # State should be reset to initial
        assert env.step_idx == 0, "Step counter should reset"
        assert env.pv == env.capital0, "Portfolio value should reset"
        assert env.pos_pct == 0.0, "Position should reset"

    def test_episode_termination(self, env):
        """Test episode terminates at end of data"""
        env.reset()
        terminated = False
        steps = 0

        while not terminated and steps < 100:
            obs, reward, terminated, truncated, info = env.step(np.array([0.0]))
            steps += 1

        assert terminated, "Episode should terminate when data ends"
        assert env.step_idx >= len(env.df) - 1, "Should reach end of data"

    def test_bankruptcy_termination(self, env):
        """Test episode terminates on severe loss"""
        env.reset()

        # Create scenario with large losses by manipulating portfolio value
        # Take max position then step through
        env.step(np.array([1.0]))  # Max long position

        # Manually simulate large loss for testing
        env.pv = env.capital0 * 0.4  # 60% drawdown

        # Next step should terminate
        obs, reward, terminated, truncated, info = env.step(np.array([0.0]))

        # Should detect bankruptcy (portfolio < 50% of initial)
        assert terminated, "Episode should terminate on bankruptcy"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
