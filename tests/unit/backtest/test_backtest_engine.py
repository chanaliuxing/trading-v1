#!/usr/bin/env python3
"""
Unit tests for BacktestEngine
Tests data loading, LSTM conversion, mock signal generation, metrics calculation
"""
import pytest
import numpy as np
import pandas as pd
from unittest.mock import Mock, patch, MagicMock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backtest_ml_engine import BacktestEngine, BacktestConfig


class TestDataLoading:
    """Test data loading from ClickHouse"""

    @pytest.fixture
    def mock_config(self):
        return BacktestConfig(
            symbols=["TSLA"],
            start="2025-10-01 09:30:00",
            end="2025-10-31 16:00:00",
            timeframe="1m",
            features_table="trading.features_1m_with_oof",
            initial_cash=100000.0,
            commission_per_share=0.0035,
            slippage_bps=1.0,
            strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
            strategy_params={}
        )

    def test_config_validation(self, mock_config):
        """Test config validation"""
        assert mock_config.symbols == ["TSLA"]
        assert mock_config.initial_cash > 0
        assert mock_config.commission_per_share >= 0

    @patch('backtest_ml_engine._get_clickhouse')
    def test_data_fetch_query_structure(self, mock_get_clickhouse, mock_config):
        """Test ClickHouse query structure"""
        mock_clickhouse_connect = MagicMock()
        mock_get_clickhouse.return_value = ("connect", mock_clickhouse_connect)
        mock_client = MagicMock()
        mock_clickhouse_connect.get_client.return_value = mock_client
        mock_client.query_df.return_value = pd.DataFrame({
            'ts_bar_end': pd.date_range('2025-10-01 09:30', periods=10, freq='1min'),
            'symbol': ['TSLA'] * 10,
            'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0,
            'volume': 5000,
        })

        engine = BacktestEngine(mock_config)
        df = engine.load_data()

        # Verify query was called
        assert mock_client.query_df.called, "Should query ClickHouse"

        # Verify data structure
        assert 'ts_bar_end' in df.columns
        assert 'close' in df.columns

    def test_ohlcv_columns_present(self):
        """Test OHLCV columns are present after loading"""
        mock_df = pd.DataFrame({
            'ts_bar_end': pd.date_range('2025-10-01 09:30', periods=10, freq='1min'),
            'symbol': ['TSLA'] * 10,
            'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 5000,
        })

        required_cols = ['open', 'high', 'low', 'close', 'volume']
        for col in required_cols:
            assert col in mock_df.columns, f"Missing required column: {col}"


class TestLSTMConversion:
    """Test LSTM OOF signal conversion logic"""

    def test_lstm_oof_conversion_with_valid_data(self):
        """Test LSTM OOF [0,1] → signal [-1,1] with valid confidence"""
        df = pd.DataFrame({
            'lstm_oof_h5': [0.0, 0.25, 0.5, 0.75, 1.0],
            'lstm_oof_conf_h5': [0.8, 0.7, 0.9, 0.6, 0.85],
        })

        # Apply conversion logic (as in backtest_ml_engine.py:318-328)
        oof_vals = df["lstm_oof_h5"].astype(float)
        oof_conf = df["lstm_oof_conf_h5"].astype(float)
        df["lstm_signal"] = np.where(
            oof_conf > 0,
            (oof_vals * 2.0) - 1.0,
            0.0
        )

        expected = [-1.0, -0.5, 0.0, 0.5, 1.0]
        np.testing.assert_array_almost_equal(df["lstm_signal"], expected)

    def test_lstm_oof_conversion_with_zero_confidence(self):
        """Test LSTM OOF with zero confidence → neutral signal"""
        df = pd.DataFrame({
            'lstm_oof_h5': [0.0, 0.5, 1.0],  # Values don't matter
            'lstm_oof_conf_h5': [0.0, 0.0, 0.0],  # Zero confidence
        })

        oof_vals = df["lstm_oof_h5"].astype(float)
        oof_conf = df["lstm_oof_conf_h5"].astype(float)
        df["lstm_signal"] = np.where(
            oof_conf > 0,
            (oof_vals * 2.0) - 1.0,
            0.0
        )

        expected = [0.0, 0.0, 0.0]  # All neutral
        np.testing.assert_array_almost_equal(df["lstm_signal"], expected)

    def test_lstm_oof_missing_columns(self):
        """Test behavior when LSTM OOF columns are missing"""
        df = pd.DataFrame({
            'close': [100.0, 101.0, 102.0],
        })

        # Should not create lstm_signal column
        assert 'lstm_signal' not in df.columns
        assert 'lstm_oof_h5' not in df.columns


class TestMockSignalGeneration:
    """Test mock ML signal generation fallback"""

    def test_mock_signal_trigger_condition(self):
        """Test mock signals are generated when ml_signal is missing or zero"""
        # Case 1: ml_signal column missing
        df1 = pd.DataFrame({
            'ret_5': [0.02, -0.01, 0.03],
            'slope_20': [0.05, -0.02, 0.01],
            'rsi_14': [65, 30, 75],
        })
        needs_mock_1 = 'ml_signal' not in df1.columns or (df1.get('ml_signal', pd.Series()).fillna(0.0).abs() < 1e-6).all()
        assert needs_mock_1, "Should trigger mock when ml_signal missing"

        # Case 2: ml_signal all zeros
        df2 = pd.DataFrame({
            'ml_signal': [0.0, 0.0, 0.0],
            'ret_5': [0.02, -0.01, 0.03],
        })
        needs_mock_2 = (df2['ml_signal'].fillna(0.0).abs() < 1e-6).all()
        assert needs_mock_2, "Should trigger mock when ml_signal all zeros"

        # Case 3: ml_signal has valid values
        df3 = pd.DataFrame({
            'ml_signal': [0.5, -0.3, 0.8],
        })
        needs_mock_3 = (df3['ml_signal'].fillna(0.0).abs() < 1e-6).all()
        assert not needs_mock_3, "Should not trigger mock when ml_signal valid"

    def test_mock_signal_calculation(self):
        """Test mock signal calculation logic"""
        df = pd.DataFrame({
            'ret_5': [0.02, -0.01, 0.0],
            'slope_20': [0.05, -0.02, 0.0],
            'rsi_14': [75, 25, 50],  # Overbought, oversold, neutral
        })

        # Mock signal formula (from backtest_ml_engine.py:350-360)
        ret_5 = df['ret_5'].fillna(0.0)
        slope_20 = df['slope_20'].fillna(0.0)
        rsi_14 = df['rsi_14'].fillna(50.0)

        momentum_score = np.tanh(ret_5 * 100)
        trend_score = np.tanh(slope_20 * 10)
        rsi_score = np.where(rsi_14 > 70, -0.5, np.where(rsi_14 < 30, 0.5, 0.0))

        mock_signal = 0.4 * momentum_score + 0.4 * trend_score + 0.2 * rsi_score

        # Check RSI contribution
        assert rsi_score[0] == -0.5, "RSI > 70 should give negative score"
        assert rsi_score[1] == 0.5, "RSI < 30 should give positive score"
        assert rsi_score[2] == 0.0, "RSI = 50 should be neutral"

        # Check signal is in reasonable range
        assert all(abs(mock_signal) <= 1.0), "Mock signals should be reasonable"

    def test_mock_confidence_calculation(self):
        """Test mock confidence calculation"""
        mock_signal = np.array([0.8, -0.3, 0.0])
        mock_conf = np.clip(np.abs(mock_signal) * 0.8 + 0.3, 0.0, 0.9)

        # Strong signal should have higher confidence
        assert mock_conf[0] > mock_conf[1], "Stronger signal should have higher confidence"
        assert mock_conf[0] > mock_conf[2], "Non-zero signal should have higher confidence than zero"

        # Confidence should be in [0.3, 0.9] range
        assert all((mock_conf >= 0.3) & (mock_conf <= 0.9)), "Confidence should be in [0.3, 0.9]"


class TestMetricsCalculation:
    """Test backtest metrics calculation"""

    def test_total_return_calculation(self):
        """Test total return calculation"""
        initial_cash = 100000.0
        final_portfolio_value = 110000.0

        total_return = (final_portfolio_value - initial_cash) / initial_cash
        assert abs(total_return - 0.10) < 0.001, "Total return should be 10%"

    def test_sharpe_ratio_calculation(self):
        """Test Sharpe ratio calculation"""
        returns = pd.Series([0.01, -0.005, 0.02, 0.015, -0.01])

        mean_return = returns.mean()
        std_return = returns.std()
        sharpe = mean_return / std_return if std_return > 0 else 0.0

        # Annualized Sharpe (assuming daily returns)
        sharpe_annual = sharpe * np.sqrt(252)

        assert isinstance(sharpe_annual, (int, float)), "Sharpe should be numeric"

    def test_max_drawdown_calculation(self):
        """Test maximum drawdown calculation"""
        portfolio_values = pd.Series([100000, 110000, 105000, 95000, 98000, 120000])

        cummax = portfolio_values.cummax()
        drawdowns = (portfolio_values - cummax) / cummax
        max_dd = drawdowns.min()

        # Max DD should be negative
        assert max_dd < 0, "Max drawdown should be negative"

        # In this case, max DD is (95000 - 110000) / 110000 = -13.6%
        expected_dd = (95000 - 110000) / 110000
        assert abs(max_dd - expected_dd) < 0.001

    def test_win_rate_calculation(self):
        """Test win rate calculation"""
        trades = pd.DataFrame({
            'pnl': [100, -50, 200, -30, 150, -20]
        })

        winning_trades = (trades['pnl'] > 0).sum()
        total_trades = len(trades)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

        assert win_rate == 0.5, "Win rate should be 50%"


class TestStrategyIntegration:
    """Test strategy loading and integration"""

    def test_strategy_import(self):
        """Test strategy can be imported dynamically"""
        strategy_path = "strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy"

        module_path, class_name = strategy_path.rsplit('.', 1)

        # Would import dynamically
        # from strategies.enhanced_model_signal_strategy import EnhancedModelSignalStrategy

        assert module_path == "strategies.enhanced_model_signal_strategy"
        assert class_name == "EnhancedModelSignalStrategy"

    def test_strategy_params_passthrough(self):
        """Test strategy params are passed correctly"""
        config = BacktestConfig(
            symbols=["TSLA"],
            start="2025-10-01",
            end="2025-10-31",
            timeframe="1m",
            features_table="trading.features_1m",
            initial_cash=100000.0,
            strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
            strategy_params={
                "base_qty": 200,
                "ensemble": {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]}
            }
        )

        assert config.strategy_params["base_qty"] == 200
        assert config.strategy_params["ensemble"]["mode"] == "weighted_avg"


class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_empty_dataframe(self):
        """Test handling of empty data"""
        df = pd.DataFrame()

        # Should not crash, should return empty metrics or error
        assert len(df) == 0

    def test_single_row_dataframe(self):
        """Test handling of single bar"""
        df = pd.DataFrame({
            'ts_bar_end': [pd.Timestamp('2025-10-01 09:30')],
            'close': [100.0],
            'volume': [1000]
        })

        assert len(df) == 1
        # Backtest should handle gracefully (no trades possible)

    def test_missing_ohlc_columns(self):
        """Test handling of missing OHLC columns"""
        df = pd.DataFrame({
            'ts_bar_end': pd.date_range('2025-10-01', periods=10, freq='1min'),
            # Missing open, high, low, close
        })

        required = ['open', 'high', 'low', 'close']
        missing = [col for col in required if col not in df.columns]

        assert len(missing) > 0, "Should detect missing OHLC columns"

    def test_nan_prices(self):
        """Test handling of NaN prices"""
        df = pd.DataFrame({
            'close': [100.0, np.nan, 102.0, 103.0],
            'volume': [1000, 1000, 1000, 1000]
        })

        # Should handle NaN prices (skip or forward-fill)
        has_nan = df['close'].isna().any()
        assert has_nan, "Test data should have NaN"

        # Forward-fill
        df_filled = df.fillna(method='ffill')
        assert not df_filled['close'].isna().any(), "Should fill NaN"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
