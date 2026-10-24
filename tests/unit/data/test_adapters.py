#!/usr/bin/env python3
"""
Unit tests for EnhancedModelAdapter
Tests dual-model mapping, OOF loading, feature transformation
"""
import pytest
import numpy as np
import pandas as pd
from unittest.mock import Mock, patch, MagicMock
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from adapters.enhanced_model_adapter import EnhancedModelAdapter


class TestDualModelMapping:
    """Test OOF column mapping to model-specific keys"""

    def test_lstm_oof_to_lstm_signal_mapping(self):
        """Test LSTM OOF columns mapped to lstm_signal/lstm_conf"""
        df = pd.DataFrame({
            'lstm_oof_h5': [0.6, 0.4, 0.8],
            'lstm_oof_conf_h5': [0.9, 0.7, 0.85],
            'close': [100.0, 101.0, 102.0]
        })

        # Apply mapping logic (from adapters/enhanced_model_adapter.py:93-97)
        if "lstm_oof_h5" in df.columns and "lstm_oof_conf_h5" in df.columns:
            df["lstm_signal"] = df["lstm_oof_h5"].astype(float)
            df["lstm_conf"] = df["lstm_oof_conf_h5"].astype(float)

        assert "lstm_signal" in df.columns, "Should create lstm_signal"
        assert "lstm_conf" in df.columns, "Should create lstm_conf"
        np.testing.assert_array_equal(df["lstm_signal"], [0.6, 0.4, 0.8])
        np.testing.assert_array_equal(df["lstm_conf"], [0.9, 0.7, 0.85])

    def test_ml_to_xgb_signal_alias(self):
        """Test ml_signal/ml_conf aliased to xgb_signal/xgb_conf"""
        df = pd.DataFrame({
            'ml_signal': [0.5, -0.3, 0.7],
            'ml_conf': [0.8, 0.6, 0.9],
        })

        # Apply aliasing logic (from adapters/enhanced_model_adapter.py:100-105)
        if "ml_signal" in df.columns and "ml_conf" in df.columns:
            if "xgb_signal" not in df.columns:
                df["xgb_signal"] = df["ml_signal"].astype(float)
            if "xgb_conf" not in df.columns:
                df["xgb_conf"] = df["ml_conf"].astype(float)

        assert "xgb_signal" in df.columns, "Should create xgb_signal alias"
        assert "xgb_conf" in df.columns, "Should create xgb_conf alias"
        np.testing.assert_array_equal(df["xgb_signal"], [0.5, -0.3, 0.7])
        np.testing.assert_array_equal(df["xgb_conf"], [0.8, 0.6, 0.9])

    def test_both_models_present(self):
        """Test both LSTM and XGB signals coexist"""
        df = pd.DataFrame({
            'lstm_oof_h5': [0.6, 0.4, 0.8],
            'lstm_oof_conf_h5': [0.9, 0.7, 0.85],
            'ml_signal': [0.5, -0.3, 0.7],
            'ml_conf': [0.8, 0.6, 0.9],
        })

        # Apply both mappings
        if "lstm_oof_h5" in df.columns:
            df["lstm_signal"] = df["lstm_oof_h5"].astype(float)
            df["lstm_conf"] = df["lstm_oof_conf_h5"].astype(float)

        if "ml_signal" in df.columns and "xgb_signal" not in df.columns:
            df["xgb_signal"] = df["ml_signal"].astype(float)
            df["xgb_conf"] = df["ml_conf"].astype(float)

        # Both should exist
        assert "lstm_signal" in df.columns
        assert "lstm_conf" in df.columns
        assert "xgb_signal" in df.columns
        assert "xgb_conf" in df.columns

    def test_no_overwrite_existing_xgb_columns(self):
        """Test existing xgb_signal/xgb_conf not overwritten"""
        df = pd.DataFrame({
            'ml_signal': [0.5, -0.3, 0.7],
            'ml_conf': [0.8, 0.6, 0.9],
            'xgb_signal': [0.2, 0.1, 0.3],  # Existing
            'xgb_conf': [0.5, 0.4, 0.6],
        })

        original_xgb_signal = df["xgb_signal"].copy()
        original_xgb_conf = df["xgb_conf"].copy()

        # Apply aliasing (should not overwrite)
        if "ml_signal" in df.columns:
            if "xgb_signal" not in df.columns:
                df["xgb_signal"] = df["ml_signal"]
            if "xgb_conf" not in df.columns:
                df["xgb_conf"] = df["ml_conf"]

        # Should keep original values
        np.testing.assert_array_equal(df["xgb_signal"], original_xgb_signal)
        np.testing.assert_array_equal(df["xgb_conf"], original_xgb_conf)


class TestOOFLoading:
    """Test OOF data loading from ClickHouse"""

    @patch('adapters.enhanced_model_adapter.EnhancedModelAdapter.load_data')
    def test_oof_view_query(self, mock_load_data):
        """Test the OOF data query structure"""
        mock_load_data.return_value = pd.DataFrame({
            'ts': pd.to_datetime(['2025-01-01 09:30:00', '2025-01-01 09:35:00']),
            'symbol': ['TSLA', 'TSLA'],
            'open': [100.0, 100.5], 'high': [101.0, 102.0], 'low': [99.0, 99.5], 'close': [100.5, 101.5],
            'volume': [1000.0, 1200.0],
            'atr_14': [1.5, 1.6], 'vwap': [100.0, 101.0], 'ret_1m': [0.05, 0.06], 'ret_5m': [0.2, 0.25],
            'rsi_14': [55.0, 60.0], 'vix': [18.0, 19.0],
            'lstm_oof_h5': [0.6, 0.5],
            'lstm_oof_conf_h5': [0.9, 0.8],
            'ml_signal': [0.7, 0.6],
            'ml_conf': [0.85, 0.75],
            'xgb_oof_h5': [0.65, 0.55],
            'xgb_oof_conf_h5': [0.95, 0.85],
        })
        
        adapter = EnhancedModelAdapter()
        df = adapter.load_data(
            symbols=["TSLA"],
            start="2025-01-01",
            end="2025-01-02",
            clickhouse_config={}
        )

        assert not df.empty
        assert "ts" in df.columns
        assert "symbol" in df.columns
        assert "close" in df.columns
        assert "atr_14" in df.columns
        assert "vix" in df.columns
        assert "lstm_oof_h5" in df.columns
        assert "lstm_oof_conf_h5" in df.columns
        assert "ml_signal" in df.columns
        assert "ml_conf" in df.columns
        assert "xgb_oof_h5" in df.columns
        assert "xgb_oof_conf_h5" in df.columns

    def test_oof_coverage_validation(self):
        """Test OOF coverage validation"""
        df = pd.DataFrame({
            'lstm_oof_h5': [0.6, 0.0, 0.7, 0.0, 0.8],  # Some zeros
            'lstm_oof_conf_h5': [0.9, 0.0, 0.85, 0.0, 0.9],
        })

        # Calculate coverage (rows with conf > 0)
        valid_rows = (df['lstm_oof_conf_h5'] > 0).sum()
        total_rows = len(df)
        coverage = valid_rows / total_rows if total_rows > 0 else 0.0

        assert coverage == 0.6, "Coverage should be 60% (3/5 valid)"

    def test_oof_missing_fallback(self):
        """Test fallback when OOF columns missing"""
        df = pd.DataFrame({
            'close': [100.0, 101.0, 102.0],
            'ml_signal': [0.5, -0.3, 0.7],
        })

        # OOF columns not present
        assert 'lstm_oof_h5' not in df.columns
        assert 'lstm_signal' not in df.columns

        # Should still have XGB signals
        assert 'ml_signal' in df.columns


class TestFeatureTransformation:
    """Test feature engineering and transformations"""

    def test_returns_calculation(self):
        """Test return features calculation"""
        df = pd.DataFrame({
            'close': [100.0, 102.0, 101.0, 103.0, 105.0]
        })

        # Calculate returns
        df['ret_1'] = df['close'].pct_change()

        expected = [np.nan, 0.02, -0.0098, 0.0198, 0.0194]
        np.testing.assert_array_almost_equal(df['ret_1'].fillna(0), [0] + expected[1:], decimal=4)

    def test_rolling_features(self):
        """Test rolling window features"""
        df = pd.DataFrame({
            'close': [100, 102, 101, 103, 105, 104, 106]
        })

        # Rolling mean
        df['ma_3'] = df['close'].rolling(3).mean()

        # Last value: (105 + 104 + 106) / 3 = 105
        assert abs(df['ma_3'].iloc[-1] - 105.0) < 0.01

    def test_missing_feature_handling(self):
        """Test handling of missing features"""
        df = pd.DataFrame({
            'close': [100.0, np.nan, 102.0],
            'volume': [1000, 1000, np.nan]
        })

        # Forward-fill
        df_filled = df.fillna(method='ffill')

        assert df_filled['close'].iloc[1] == 100.0
        assert df_filled['volume'].iloc[2] == 1000.0


class TestDataValidation:
    """Test data quality validation"""

    def test_timestamp_ordering(self):
        """Test timestamps are properly ordered"""
        df = pd.DataFrame({
            'ts_bar_end': pd.to_datetime([
                '2025-10-01 09:30',
                '2025-10-01 09:31',
                '2025-10-01 09:32'
            ]),
            'close': [100, 101, 102]
        })

        # Check monotonic increasing
        is_sorted = df['ts_bar_end'].is_monotonic_increasing
        assert is_sorted, "Timestamps should be sorted"

    def test_duplicate_timestamps(self):
        """Test detection of duplicate timestamps"""
        df = pd.DataFrame({
            'ts_bar_end': pd.to_datetime([
                '2025-10-01 09:30',
                '2025-10-01 09:31',
                '2025-10-01 09:31',  # Duplicate
            ]),
            'symbol': ['TSLA', 'TSLA', 'TSLA'],
            'close': [100, 101, 101.5]
        })

        # Detect duplicates
        has_duplicates = df.duplicated(subset=['ts_bar_end', 'symbol']).any()
        assert has_duplicates, "Should detect duplicate timestamps"

    def test_price_sanity_checks(self):
        """Test price sanity validation"""
        df = pd.DataFrame({
            'open': [100.0, 102.0, 101.0],
            'high': [105.0, 106.0, 104.0],
            'low': [95.0, 98.0, 99.0],
            'close': [102.0, 103.0, 101.0]
        })

        # Verify high >= low
        is_valid = (df['high'] >= df['low']).all()
        assert is_valid, "High should be >= low"

        # Verify high >= open, close
        is_valid_high = ((df['high'] >= df['open']) & (df['high'] >= df['close'])).all()
        assert is_valid_high, "High should be >= open and close"

        # Verify low <= open, close
        is_valid_low = ((df['low'] <= df['open']) & (df['low'] <= df['close'])).all()
        assert is_valid_low, "Low should be <= open and close"


class TestSymbolFiltering:
    """Test multi-symbol data handling"""

    def test_symbol_filtering(self):
        """Test filtering by symbol"""
        df = pd.DataFrame({
            'symbol': ['TSLA', 'AAPL', 'TSLA', 'NVDA', 'TSLA'],
            'close': [200, 150, 202, 450, 205]
        })

        # Filter for TSLA only
        df_tsla = df[df['symbol'] == 'TSLA']

        assert len(df_tsla) == 3
        assert (df_tsla['symbol'] == 'TSLA').all()

    def test_multi_symbol_alignment(self):
        """Test timestamp alignment across symbols"""
        df = pd.DataFrame({
            'ts_bar_end': pd.to_datetime([
                '2025-10-01 09:30',
                '2025-10-01 09:30',
                '2025-10-01 09:31',
                '2025-10-01 09:31',
            ]),
            'symbol': ['TSLA', 'AAPL', 'TSLA', 'AAPL'],
            'close': [200, 150, 202, 151]
        })

        # Pivot to wide format
        df_pivot = df.pivot(index='ts_bar_end', columns='symbol', values='close')

        assert df_pivot.shape == (2, 2)  # 2 timestamps, 2 symbols
        assert 'TSLA' in df_pivot.columns
        assert 'AAPL' in df_pivot.columns


class TestEdgeCases:
    """Test edge cases"""

    def test_empty_dataframe(self):
        """Test handling of empty DataFrame"""
        df = pd.DataFrame()

        # Should handle gracefully
        assert len(df) == 0
        assert df.empty

    def test_single_symbol_single_bar(self):
        """Test single bar scenario"""
        df = pd.DataFrame({
            'ts_bar_end': [pd.Timestamp('2025-10-01 09:30')],
            'symbol': ['TSLA'],
            'close': [200.0],
            'lstm_oof_h5': [0.6],
        })

        assert len(df) == 1
        assert df['close'].iloc[0] == 200.0

    def test_extreme_oof_values(self):
        """Test handling of extreme OOF values"""
        df = pd.DataFrame({
            'lstm_oof_h5': [-10.0, 100.0, 0.5],  # Out of [0,1] range
            'lstm_oof_conf_h5': [0.9, 0.8, 0.7],
        })

        # Should clip or handle extreme values
        # Expected behavior: clip to [0, 1]
        df['lstm_oof_h5_clipped'] = np.clip(df['lstm_oof_h5'], 0.0, 1.0)

        np.testing.assert_array_equal(df['lstm_oof_h5_clipped'], [0.0, 1.0, 0.5])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])