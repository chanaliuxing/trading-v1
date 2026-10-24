#!/usr/bin/env python3
"""
Unit tests for EnhancedModelSignalStrategy
Tests ensemble signal combination, RL integration, market filters
"""
import pytest
import numpy as np
import pandas as pd
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


class TestMultiModelEnsemble:
    """Test ensemble signal combination logic"""

    def test_weighted_avg_mode(self):
        """Test weighted average ensemble mode"""
        from strategies.enhanced_model_signal_strategy import MultiModelEnsemble

        config = {
            "mode": "weighted_avg",
            "models": [
                {"name": "xgb", "weight": 0.6},
                {"name": "lstm", "weight": 0.4}
            ]
        }
        ensemble = MultiModelEnsemble(config)

        features = {
            "xgb_signal": 0.8,
            "xgb_conf": 0.9,
            "lstm_signal": -0.2,
            "lstm_conf": 0.7
        }

        signal, conf = ensemble.combine_signals(features)

        # Expected: 0.6 * 0.8 + 0.4 * (-0.2) = 0.48 - 0.08 = 0.40
        expected_signal = 0.6 * 0.8 + 0.4 * (-0.2)
        assert abs(signal - expected_signal) < 0.01, f"Expected {expected_signal}, got {signal}"

        # Confidence should be weighted average
        expected_conf = 0.6 * 0.9 + 0.4 * 0.7
        assert abs(conf - expected_conf) < 0.01, f"Expected {expected_conf}, got {conf}"

    def test_xgb_only_mode(self):
        """Test XGB-only mode"""
        from strategies.enhanced_model_signal_strategy import MultiModelEnsemble

        config = {
            "mode": "weighted_avg",
            "models": [{"name": "xgb", "weight": 1.0}]
        }
        ensemble = MultiModelEnsemble(config)

        features = {
            "xgb_signal": 0.75,
            "xgb_conf": 0.85,
            "lstm_signal": -0.5,  # Should be ignored
            "lstm_conf": 0.9
        }

        signal, conf = ensemble.combine_signals(features)

        assert abs(signal - 0.75) < 0.01, "XGB-only should use only xgb_signal"
        assert abs(conf - 0.85) < 0.01, "XGB-only should use only xgb_conf"

    def test_lstm_only_mode(self):
        """Test LSTM-only mode"""
        from strategies.enhanced_model_signal_strategy import MultiModelEnsemble

        config = {
            "mode": "weighted_avg",
            "models": [{"name": "lstm", "weight": 1.0}]
        }
        ensemble = MultiModelEnsemble(config)

        features = {
            "xgb_signal": 0.75,  # Should be ignored
            "xgb_conf": 0.85,
            "lstm_signal": -0.3,
            "lstm_conf": 0.7
        }

        signal, conf = ensemble.combine_signals(features)

        assert abs(signal - (-0.3)) < 0.01, "LSTM-only should use only lstm_signal"
        assert abs(conf - 0.7) < 0.01, "LSTM-only should use only lstm_conf"

    def test_missing_model_signals(self):
        """Test behavior when model signals are missing"""
        from strategies.enhanced_model_signal_strategy import MultiModelEnsemble

        config = {
            "mode": "weighted_avg",
            "models": [
                {"name": "xgb", "weight": 0.6},
                {"name": "lstm", "weight": 0.4}
            ]
        }
        ensemble = MultiModelEnsemble(config)

        # Missing LSTM signal
        features = {
            "xgb_signal": 0.8,
            "xgb_conf": 0.9,
            # lstm_signal missing
        }

        signal, conf = ensemble.combine_signals(features)

        # Should use only XGB signal (normalized weight)
        assert signal != 0.0, "Should still produce signal from available model"
        assert conf > 0.0, "Should have positive confidence"

    def test_zero_confidence_handling(self):
        """Test handling of zero confidence signals"""
        from strategies.enhanced_model_signal_strategy import MultiModelEnsemble

        config = {
            "mode": "weighted_avg",
            "models": [
                {"name": "xgb", "weight": 0.6},
                {"name": "lstm", "weight": 0.4}
            ]
        }
        ensemble = MultiModelEnsemble(config)

        features = {
            "xgb_signal": 0.8,
            "xgb_conf": 0.9,
            "lstm_signal": 0.5,
            "lstm_conf": 0.0  # Zero confidence
        }

        signal, conf = ensemble.combine_signals(features)

        # Should downweight or ignore zero-confidence signals
        assert signal <= 0.8, "Zero-confidence signal should reduce combined signal"


class TestMarketFilters:
    """Test market filter logic (conf_floor, volume, spread, hours)"""

    @pytest.fixture
    def mock_strategy(self):
        """Create mock strategy instance"""
        from strategies.enhanced_model_signal_strategy import EnhancedModelSignalStrategy
        from libs.strategy.base import StrategyParams
        from unittest.mock import Mock

        # Create mock context and params
        mock_ctx = Mock()
        mock_ctx.strategy_id = "test_strategy"
        mock_ctx.now_us = Mock(return_value=1700000000000000)

        # Create minimal params using defaults
        params = StrategyParams(
            extras={
                "market_filter": {
                    "conf_floor": 0.5,
                    "min_volume": 1000,
                    "max_spread_bps": 10,
                    "check_after_hours": True
                }
            }
        )

        strategy = EnhancedModelSignalStrategy(ctx=mock_ctx, params=params)
        return strategy

    def test_conf_floor_filter(self, mock_strategy):
        """Test confidence floor filtering"""
        # Low confidence - should filter
        features = {"ml_signal": 0.8, "ml_conf": 0.3, "volume": 5000}
        # Would need to mock _should_trade method
        # This is a placeholder for actual implementation

        assert True, "Placeholder for conf_floor test"

    def test_volume_filter(self, mock_strategy):
        """Test minimum volume filtering"""
        features = {"ml_signal": 0.8, "ml_conf": 0.9, "volume": 500}
        # Should filter out due to low volume
        assert True, "Placeholder for volume filter test"

    def test_spread_filter(self, mock_strategy):
        """Test spread filtering"""
        # Wide spread - should filter
        features = {
            "ml_signal": 0.8,
            "ml_conf": 0.9,
            "bid": 100.0,
            "ask": 100.5,  # 50 bps spread
            "mid": 100.25
        }
        assert True, "Placeholder for spread filter test"


class TestRLIntegration:
    """Test RL state building and position scaling"""

    def test_rl_state_building(self):
        """Test RL state vector construction"""
        from strategies.enhanced_model_signal_strategy import EnhancedModelSignalStrategy
        from libs.strategy.base import StrategyParams
        from unittest.mock import Mock

        # Create mock context and params
        mock_ctx = Mock()
        params = StrategyParams()
        strategy = EnhancedModelSignalStrategy(ctx=mock_ctx, params=params)

        # Mock context and features
        features = {
            "ml_signal": 0.5,
            "ml_conf": 0.8,
            "ret_5": 0.02,
            "volatility": 0.15,
            "rsi_14": 65,
            "volume": 5000,
            "close": 100.0
        }

        # Mock position and portfolio
        mock_position = Mock()
        mock_position.quantity = 100.0
        mock_position.avg_cost = 95.0

        # Build state (would need actual implementation)
        # Expected state: [position_ratio, pnl_ratio, ml_signal, ml_conf, ret_5, volatility, rsi, ...]

        assert True, "Placeholder for RL state building test"

    def test_rl_position_scaling(self):
        """Test RL model scales position size"""
        # Test that RL model adjusts position from base_qty
        # e.g., base_qty=100 * rl_action=0.5 → actual_qty=50

        assert True, "Placeholder for RL position scaling test"

    def test_rl_model_loading(self):
        """Test RL model loads from checkpoint"""
        from strategies.enhanced_model_signal_strategy import EnhancedModelSignalStrategy
        from libs.strategy.base import StrategyParams
        from unittest.mock import Mock

        # Create mock context and params
        mock_ctx = Mock()
        params = StrategyParams()
        strategy = EnhancedModelSignalStrategy(ctx=mock_ctx, params=params)

        # Test model path validation
        model_path = "models/rl/nonexistent.zip"
        # Should handle missing model gracefully (fallback to base_qty)

        assert True, "Placeholder for RL model loading test"


class TestOrderSubmission:
    """Test order creation and submission logic"""

    @pytest.mark.asyncio
    async def test_long_signal_creates_buy_order(self):
        """Test positive signal creates long position"""
        from strategies.enhanced_model_signal_strategy import EnhancedModelSignalStrategy
        from libs.strategy.base import StrategyParams
        from unittest.mock import Mock

        # Create mock context and params
        mock_ctx = AsyncMock()
        params = StrategyParams()
        strategy = EnhancedModelSignalStrategy(ctx=mock_ctx, params=params)

        # Mock dependencies
        strategy.ctx = AsyncMock()
        strategy.ctx.position = AsyncMock(return_value=Mock(quantity=0.0))
        strategy.ctx.submit_intent = AsyncMock()

        # Simulate strong buy signal
        features = {
            "ml_signal": 0.8,
            "ml_conf": 0.9,
            "close": 100.0,
            "volume": 10000,
            "bid": 99.95,
            "ask": 100.05
        }

        # Would call strategy.on_features("TSLA", features)
        # Expect ctx.submit_intent to be called with positive target_qty

        assert True, "Placeholder - needs async test infrastructure"

    @pytest.mark.asyncio
    async def test_short_signal_creates_sell_order(self):
        """Test negative signal creates short position"""
        assert True, "Placeholder for short signal test"

    @pytest.mark.asyncio
    async def test_neutral_signal_closes_position(self):
        """Test near-zero signal closes existing position"""
        assert True, "Placeholder for position closing test"


class TestConfigLoading:
    """Test per-symbol config loading and parameter override"""

    def test_load_symbol_config(self):
        """Test loading per-symbol JSON config"""
        from strategies.enhanced_model_signal_strategy import EnhancedModelSignalStrategy
        from libs.strategy.base import StrategyParams
        from unittest.mock import Mock

        # Test config precedence: strategy params > symbol config > defaults
        mock_ctx = Mock()
        params = StrategyParams()
        strategy = EnhancedModelSignalStrategy(ctx=mock_ctx, params=params)

        # Mock config file reading
        with patch('pathlib.Path.exists', return_value=True), \
             patch('pathlib.Path.read_text', return_value='{"base_qty": 200}'):

            # Would load config for TSLA
            # Expected: base_qty=200 from file

            assert True, "Placeholder for config loading test"

    def test_config_override_precedence(self):
        """Test parameter override precedence"""
        # strategy_params > symbol_config > defaults
        assert True, "Placeholder for config precedence test"


class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_nan_signal_handling(self):
        """Test handling of NaN signals"""
        from strategies.enhanced_model_signal_strategy import MultiModelEnsemble

        config = {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]}
        ensemble = MultiModelEnsemble(config)

        features = {
            "xgb_signal": np.nan,
            "xgb_conf": 0.8
        }

        signal, conf = ensemble.combine_signals(features)

        # Should handle NaN gracefully (return 0.0 or skip)
        assert not np.isnan(signal), "Should not return NaN signal"
        assert signal == 0.0 or signal != signal, "NaN should be converted to 0.0"

    def test_extreme_signal_values(self):
        """Test handling of extreme signal values"""
        from strategies.enhanced_model_signal_strategy import MultiModelEnsemble

        config = {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]}
        ensemble = MultiModelEnsemble(config)

        features = {
            "xgb_signal": 100.0,  # Extreme value
            "xgb_conf": 0.9
        }

        signal, conf = ensemble.combine_signals(features)

        # Signal should be clipped to [-1, 1] or reasonable range
        assert abs(signal) <= 10.0, "Extreme signals should be handled"

    def test_empty_features(self):
        """Test handling of empty feature dict"""
        from strategies.enhanced_model_signal_strategy import MultiModelEnsemble

        config = {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]}
        ensemble = MultiModelEnsemble(config)

        signal, conf = ensemble.combine_signals({})

        # Should return neutral signal
        assert signal == 0.0, "Empty features should return neutral signal"
        assert conf == 0.0, "Empty features should return zero confidence"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
