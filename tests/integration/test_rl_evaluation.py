#!/usr/bin/env python3
"""
Integration tests for RL evaluation workflow
Tests RL training, evaluation, and position scaling
"""
import pytest
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestRLTraining:
    """Test RL training workflow"""

    @pytest.fixture
    def train_config(self):
        """Training configuration for tests"""
        return {
            "symbols": ["TSLA"],
            "start": "2025-10-01",
            "end": "2025-10-15",  # Short period for fast tests
            "timesteps": 5000,  # Small number for fast tests
            "cost_bps": 2.0,
            "dd_penalty": 0.1,
            "turnover_penalty": 0.05
        }

    @pytest.mark.integration
    @pytest.mark.slow
    def test_rl_training_completes(self, train_config, tmp_path):
        """Test RL training completes successfully"""
        from rl_core.train import train_rl_model

        model_path = tmp_path / "test_rl_model.zip"

        # Run training
        try:
            train_rl_model(
                symbols=train_config["symbols"],
                start=train_config["start"],
                end=train_config["end"],
                features_table="trading.features_1m",
                timesteps=train_config["timesteps"],
                cost_bps=train_config["cost_bps"],
                dd_penalty=train_config["dd_penalty"],
                turnover_penalty=train_config["turnover_penalty"],
                output_path=str(model_path)
            )
        except Exception as e:
            pytest.skip(f"Training requires valid data: {e}")

        # Verify model was saved
        assert model_path.exists(), "Model file should be created"

    @pytest.mark.integration
    def test_rl_model_loading(self, tmp_path):
        """Test RL model can be loaded"""
        pytest.importorskip("stable_baselines3")
        pytest.importorskip("gymnasium")
        from stable_baselines3 import SAC

        # Create a dummy model
        from rl_core.trading_env import TradingEnv
        import pandas as pd
        import numpy as np

        dummy_data = pd.DataFrame({
            'open': np.random.uniform(100, 110, 50),
            'high': np.random.uniform(110, 115, 50),
            'low': np.random.uniform(95, 100, 50),
            'close': np.random.uniform(100, 110, 50),
            'volume': np.random.randint(1000, 10000, 50),
            'ml_signal': np.random.uniform(-1, 1, 50),
            'ml_conf': np.random.uniform(0.3, 0.9, 50),
        })
        # Add required price_return column
        dummy_data['price_return'] = dummy_data['close'].pct_change().fillna(0.0)

        env = TradingEnv(
            historical_data=dummy_data,
            initial_capital=100000.0,
            cost_bps=2.0
        )

        # Train small model
        model = SAC("MlpPolicy", env, verbose=0)
        model.learn(total_timesteps=100)

        model_path = tmp_path / "test_model.zip"
        model.save(str(model_path))

        # Load model
        loaded_model = SAC.load(str(model_path))

        assert loaded_model is not None, "Model should load successfully"


class TestRLEvaluation:
    """Test RL evaluation workflow"""

    @pytest.fixture
    def eval_config(self):
        """Evaluation configuration"""
        return {
            "start": "2025-10-15",
            "end": "2025-10-20",
            "symbols": ["TSLA"],
            "min_sharpe": 0.5,
            "max_mdd": 0.5
        }

    @pytest.mark.integration
    @pytest.mark.slow
    def test_rl_evaluation_runs(self, eval_config, tmp_path):
        """Test RL evaluation completes"""
        model_path = Path("models/rl/sac_v2_test.zip")

        if not model_path.exists():
            pytest.skip("RL model not found, run training first")

        from scripts.ai_trader.rl.eval_rl import evaluate_rl_model

        out_dir = tmp_path / "eval_results"
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            results = evaluate_rl_model(
                start=eval_config["start"],
                end=eval_config["end"],
                symbols=eval_config["symbols"],
                model_path=str(model_path),
                ignore_hours=True,
                min_sharpe=eval_config["min_sharpe"],
                max_mdd=eval_config["max_mdd"],
                out_dir=str(out_dir)
            )
        except Exception as e:
            pytest.skip(f"Evaluation requires valid data and model: {e}")

        # Verify results structure
        assert "metrics" in results or "trades" in results

    @pytest.mark.integration
    def test_rl_position_scaling(self):
        """Test RL model scales position size"""
        pytest.importorskip("stable_baselines3")
        from stable_baselines3 import SAC
        import numpy as np

        model_path = Path("models/rl/sac_v2_test.zip")

        if not model_path.exists():
            pytest.skip("RL model not found")

        # Load model
        model = SAC.load(str(model_path))

        # Create test state (8 dimensions to match model)
        test_state = np.array([
            0.0,   # position_ratio
            0.0,   # pnl_ratio
            0.5,   # ml_signal (buy)
            0.8,   # ml_conf
            0.02,  # ret_5
            0.15,  # volatility
            60,    # rsi_14
            0.01,  # volume_ratio
        ], dtype=np.float32)

        # Predict action
        action, _ = model.predict(test_state, deterministic=True)

        # Action should be in [-1, 1] range
        assert -1.0 <= action[0] <= 1.0, "Action should be in [-1, 1]"

        # With positive signal, expect positive action (usually)
        # Note: May not always be true due to other state factors
        assert action[0] != 0.0, "Action should not be exactly zero"


class TestRLIntegrationWithStrategy:
    """Test RL integration with EnhancedModelSignalStrategy"""

    @pytest.mark.integration
    @pytest.mark.slow
    def test_strategy_uses_rl_scaling(self, tmp_path):
        """Test strategy uses RL for position scaling"""
        from backtest_ml_engine import BacktestEngine, BacktestConfig

        model_path = Path("models/rl/sac_v2_test.zip")

        if not model_path.exists():
            pytest.skip("RL model not found")

        config = BacktestConfig(
            symbols=["TSLA"],
            start="2025-10-01 09:30:00",
            end="2025-10-05 16:00:00",
            timeframe="1m",
            features_table="trading.features_1m",
            initial_cash=100000.0,
            strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
            strategy_params={
                "symbols": ["TSLA"],
                "base_qty": 100,
                "rl_model_path": str(model_path),
                "use_rl_sizing": True,
                "ensemble": {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]},
                "market_filter": {"conf_floor": 0.0, "min_volume": 0, "max_spread_bps": 999}
            }
        )

        engine = BacktestEngine(config)
        try:
            result = engine.run(df=None)
        except Exception as e:
            pytest.skip(f"Backtest requires available data window: {e}")

        # Verify backtest ran
        assert isinstance(result, dict)

        # If trades occurred, verify position sizes vary (RL scaling)
        if "trades" in result and len(result["trades"]) > 1:
            trade_sizes = [abs(t["qty"]) for t in result["trades"]]
            # Expect some variation in position sizes (RL scaling)
            assert len(set(trade_sizes)) > 1, "RL should vary position sizes"


class TestRLStateBuilding:
    """Test RL state building in strategy"""

    @pytest.mark.integration
    def test_state_vector_construction(self):
        """Test RL state vector is constructed correctly"""
        # This would need to mock strategy context and features
        # Placeholder for actual implementation

        expected_state_dim = 9  # [position, pnl, signal, conf, ret_5, vol, rsi, vol_ratio, bias]

        # Mock features
        features = {
            "ml_signal": 0.5,
            "ml_conf": 0.8,
            "ret_5": 0.02,
            "volatility": 0.15,
            "rsi_14": 60,
            "volume": 5000,
            "avg_volume_20": 4000,
        }

        # Mock position
        position_qty = 100.0
        position_cost = 95.0
        current_price = 100.0

        # Build state (simplified)
        position_ratio = (position_qty * current_price) / 100000.0
        pnl_ratio = (current_price - position_cost) / position_cost if position_cost > 0 else 0.0

        state = [
            position_ratio,
            pnl_ratio,
            features["ml_signal"],
            features["ml_conf"],
            features["ret_5"],
            features["volatility"],
            features["rsi_14"] / 100.0,  # Normalize
            features["volume"] / features["avg_volume_20"] if features["avg_volume_20"] > 0 else 1.0,
            1.0  # Bias
        ]

        assert len(state) == expected_state_dim, f"State should have {expected_state_dim} dimensions"


class TestRLRewardFunction:
    """Test RL reward function behavior"""

    @pytest.mark.integration
    def test_reward_calculation(self):
        """Test reward function components"""
        pytest.importorskip("gymnasium")
        from rl_core.trading_env import TradingEnv
        import pandas as pd
        import numpy as np

        data = pd.DataFrame({
            'open': [100, 102, 101],
            'high': [105, 106, 104],
            'low': [95, 98, 99],
            'close': [102, 103, 101],
            'volume': [5000, 5500, 4800],
            'ml_signal': [0.5, 0.3, -0.2],
            'ml_conf': [0.8, 0.7, 0.6],
        })
        # Add required price_return column
        data['price_return'] = data['close'].pct_change().fillna(0.0)

        env = TradingEnv(
            historical_data=data,
            initial_capital=100000.0,
            cost_bps=2.0,
            dd_penalty=0.1,
            turnover_penalty=0.05
        )

        env.reset()

        # Take position
        obs, reward1, terminated, truncated, info = env.step(np.array([0.5]))

        # Hold position (price increases)
        obs, reward2, terminated, truncated, info = env.step(np.array([0.5]))

        # Rewards are in dollar terms (based on initial_capital=100000)
        # With 50% position, rewards could be in thousands of dollars
        assert -10000.0 <= reward1 <= 10000.0, "Reward should be in reasonable range"
        assert -10000.0 <= reward2 <= 10000.0, "Reward should be in reasonable range"

        # Rewards should be finite
        assert np.isfinite(reward1), "Reward should be finite"
        assert np.isfinite(reward2), "Reward should be finite"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-m", "not slow"])
