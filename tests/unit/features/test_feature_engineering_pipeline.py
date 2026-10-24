#!/usr/bin/env python3
"""
Tests for Feature Engineering Pipeline

Tests the three-layer feature testing approach:
1. Single operator layer: Individual feature calculations
2. Pipeline layer: ClickHouse + ETL integration (requires DB)
3. Brain proposal validation: Experimental features

This file focuses on layers 1 and 2. Layer 3 is tested through
integration tests with the Brain.
"""

from __future__ import annotations

import datetime as dt
import numpy as np
import pandas as pd
import pytest

from libs.features.feature_engineer_v2 import FeatureEngineerV2


class TestFeatureOperatorCorrectness:
    """
    Layer 1: Single operator tests

    Tests that each feature calculation function produces correct outputs
    given synthetic input data with known properties.
    """

    def test_multiframe_features_with_linear_price_increase(self):
        """
        Test multiframe features with linearly increasing prices.

        Expected behavior:
        - ret_5m, ret_15m, ret_1h should be positive
        - vol_5m, vol_15m, vol_1h should match constant volume
        """
        idx = pd.date_range(
            start=dt.datetime(2025, 11, 20, 9, 30),
            periods=120,
            freq="1min",
            tz="America/New_York",
        )
        # Linear price increase: 100 -> 110
        close = np.linspace(100.0, 110.0, len(idx))
        volume = np.full(len(idx), 1000.0)

        df = pd.DataFrame({
            "close": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "volume": volume,
        }, index=idx)

        result = FeatureEngineerV2.add_multiframe_features(df)

        # Check returns are positive (after warmup)
        assert result['ret_5m'].iloc[-60:].mean() > 0, "Expected positive 5m returns"
        assert result['ret_15m'].iloc[-60:].mean() > 0, "Expected positive 15m returns"
        assert result['ret_1h'].iloc[-60:].mean() > 0, "Expected positive 1h returns"

        # Check volume features are reasonable
        assert result['vol_5m'].iloc[-60:].mean() == pytest.approx(1000.0, abs=10)
        assert result['vol_15m'].iloc[-60:].mean() == pytest.approx(1000.0, abs=10)
        assert result['vol_1h'].iloc[-60:].mean() == pytest.approx(1000.0, abs=10)

    def test_microstructure_features_with_volume_surge(self):
        """
        Test microstructure features with volume surge pattern.

        Pattern: Low volume first, then volume spike
        Expected: volume_surge > 1 during spike
        """
        idx = pd.date_range(
            start=dt.datetime(2025, 11, 20, 9, 30),
            periods=120,
            freq="1min",
            tz="America/New_York",
        )
        close = np.full(len(idx), 100.0)
        # Constant low volume, then spike at the end
        volume = np.full(len(idx), 1000.0)
        volume[-10:] = 5000.0  # 5x spike in last 10 minutes

        df = pd.DataFrame({
            "close": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "volume": volume,
            "vwap": close,
            "atr_14": np.full(len(idx), 1.0),
        }, index=idx)

        result = FeatureEngineerV2.add_microstructure_features(df)

        # Volume surge should be significantly > 1 during spike
        # Peak should be > 2 (first bar of spike before rolling average dilutes it)
        assert result['volume_surge'].iloc[-10:].max() > 2.0, \
            "Expected volume surge peak > 2 during 5x volume spike"
        # Average during spike should be > 1.5
        assert result['volume_surge'].iloc[-5:].mean() > 1.5, \
            "Expected average volume surge > 1.5 during spike period"

    def test_time_features_session_timing(self):
        """
        Test time features correctly identify session periods.

        Expected:
        - is_first_30min = 1 for 09:30-10:00
        - is_lunch_time = 1 for 12:00-13:00
        - is_last_30min = 1 for 15:30-16:00
        """
        # Create full trading day
        idx = pd.date_range(
            start=dt.datetime(2025, 11, 20, 9, 30),
            end=dt.datetime(2025, 11, 20, 16, 0),
            freq="1min",
            tz="America/New_York",
        )
        df = pd.DataFrame({
            "close": np.full(len(idx), 100.0),
            "volume": np.full(len(idx), 1000.0),
        }, index=idx)

        result = FeatureEngineerV2.add_time_features(df)

        # Check first 30 minutes (09:30-10:00)
        first_30 = result.between_time('09:30', '09:59')
        assert first_30['is_first_30min'].all(), "First 30 min flag should be 1"

        # Check lunch time (12:00-13:00)
        lunch = result.between_time('12:00', '13:00')
        assert lunch['is_lunch_time'].sum() > 0, "Lunch time flag should be set"

        # Check last 30 minutes (15:30-16:00)
        last_30 = result.between_time('15:30', '16:00')
        assert last_30['is_last_30min'].sum() > 0, "Last 30 min flag should be set"

    def test_volatility_features_realized_vol_scaling(self):
        """
        Test that realized volatility scales correctly with price changes.

        Higher price volatility should produce higher realized_vol.
        """
        idx = pd.date_range(
            start=dt.datetime(2025, 11, 20, 9, 30),
            periods=120,
            freq="1min",
            tz="America/New_York",
        )

        # Low volatility: small random walk
        np.random.seed(42)
        close_low = 100 + np.cumsum(np.random.randn(len(idx)) * 0.01)

        # High volatility: large random walk
        np.random.seed(42)
        close_high = 100 + np.cumsum(np.random.randn(len(idx)) * 0.1)

        df_low = pd.DataFrame({
            "close": close_low,
            "high": close_low + 0.1,
            "low": close_low - 0.1,
        }, index=idx)

        df_high = pd.DataFrame({
            "close": close_high,
            "high": close_high + 0.1,
            "low": close_high - 0.1,
        }, index=idx)

        result_low = FeatureEngineerV2.add_volatility_features(df_low)
        result_high = FeatureEngineerV2.add_volatility_features(df_high)

        # High vol should have higher realized vol (after warmup)
        low_vol = result_low['realized_vol_5m'].iloc[-30:].mean()
        high_vol = result_high['realized_vol_5m'].iloc[-30:].mean()

        assert high_vol > low_vol, \
            f"High volatility ({high_vol}) should exceed low volatility ({low_vol})"

    def test_nan_handling_in_ohlcv(self):
        """
        Test that NaNs in OHLCV data are handled gracefully.

        compute_all_features should fill NaNs with close values where appropriate.
        """
        idx = pd.date_range(
            start=dt.datetime(2025, 11, 20, 9, 30),
            periods=120,
            freq="1min",
            tz="America/New_York",
        )
        close = np.linspace(100.0, 110.0, len(idx))

        df = pd.DataFrame({
            "symbol": ["TSLA"] * len(idx),
            "open": close.copy(),
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "vwap": close + 0.1,
            "volume": np.full(len(idx), 1000.0),
            "atr_14": np.full(len(idx), 1.0),
        }, index=idx)

        # Inject NaNs
        df.loc[idx[5:10], "open"] = np.nan
        df.loc[idx[10:15], "high"] = np.nan
        df.loc[idx[15:20], "vwap"] = np.nan

        result = FeatureEngineerV2.compute_all_features(df, spy_df=None)

        # Check that enhanced features exist and are not all NaN
        enhanced_cols = ['vol_5m', 'vwap_dist', 'volume_surge', 'realized_vol_5m']
        for col in enhanced_cols:
            assert col in result.columns, f"Missing enhanced column: {col}"
            # After warmup period, should have non-NaN values
            non_nan_count = result[col].iloc[60:].notna().sum()
            assert non_nan_count > 0, \
                f"Column {col} is all NaN after warmup period"


class TestRelativeStrengthFeatures:
    """Tests for relative strength features that require SPY data."""

    def test_relative_strength_with_spy_outperformance(self):
        """
        Test relative strength when stock outperforms SPY.

        Stock returns > SPY returns → ret_vs_spy should be positive
        """
        idx = pd.date_range(
            start=dt.datetime(2025, 11, 20, 9, 30),
            periods=120,
            freq="1min",
            tz="America/New_York",
        )

        # Stock: strong uptrend
        stock_close = np.linspace(100.0, 110.0, len(idx))
        # SPY: flat
        spy_close = np.full(len(idx), 400.0)

        df_stock = pd.DataFrame({
            "close": stock_close,
            "volume": np.full(len(idx), 1000.0),
        }, index=idx)

        df_spy = pd.DataFrame({
            "close": spy_close,
            "volume": np.full(len(idx), 10000.0),
        }, index=idx)

        result = FeatureEngineerV2.add_relative_strength_features(df_stock, df_spy)

        # Stock outperforms SPY, so ret_vs_spy should be positive
        assert result['ret_vs_spy_1m'].iloc[-60:].mean() > 0, \
            "Expected positive relative returns when stock outperforms SPY"

    def test_relative_strength_fallback_without_spy(self):
        """
        Test that relative strength features use fallback values when SPY is None.
        """
        idx = pd.date_range(
            start=dt.datetime(2025, 11, 20, 9, 30),
            periods=120,
            freq="1min",
            tz="America/New_York",
        )

        df = pd.DataFrame({
            "close": np.linspace(100.0, 110.0, len(idx)),
            "volume": np.full(len(idx), 1000.0),
        }, index=idx)

        result = FeatureEngineerV2.add_relative_strength_features(df, spy_df=None)

        # Should have fallback values
        assert 'ret_vs_spy_1m' in result.columns
        assert 'corr_spy_20' in result.columns
        assert 'beta_spy_20' in result.columns


@pytest.mark.integration
class TestFeaturePipelineIntegration:
    """
    Layer 2: Pipeline integration tests (requires ClickHouse)

    These tests verify the full feature engineering pipeline from
    ClickHouse bars to enhanced features.

    Run with: pytest -m integration
    """

    @pytest.mark.skip(reason="Requires live ClickHouse connection")
    def test_features_1m_to_enhanced_pipeline(self):
        """
        Test full pipeline: trading.features_1m → enhanced features

        This would query real ClickHouse data and verify coverage.
        """
        # Example structure (not executable without DB):
        # from libs.db.ch_pool import get_client
        # cli = get_client()
        #
        # # Fetch small window of data
        # rows = cli.query("""
        #     SELECT * FROM trading.features_1m
        #     WHERE symbol = 'TSLA'
        #       AND ts >= '2025-11-20'
        #       AND ts < '2025-11-21'
        #     ORDER BY ts
        # """)
        #
        # df = pd.DataFrame(rows)
        # result = FeatureEngineerV2.compute_all_features(df)
        #
        # # Verify coverage
        # assert len(result) > 0
        # assert result['vol_5m'].notna().sum() / len(result) > 0.9
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
