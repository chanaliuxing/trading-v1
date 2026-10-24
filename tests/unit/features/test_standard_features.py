"""Tests for standard features module."""

import numpy as np
import pandas as pd
import pytest

from features.standard_features import (
    PYTHON_DERIVED_FEATURES,
    add_standard_features,
    _ensure_datetime,
)


class TestPythonDerivedFeaturesList:
    """Test the PYTHON_DERIVED_FEATURES constant."""

    def test_list_contains_expected_features(self):
        """Test that list contains key features."""
        assert "ret_1b" in PYTHON_DERIVED_FEATURES
        assert "ret_5b" in PYTHON_DERIVED_FEATURES
        assert "ret_10b" in PYTHON_DERIVED_FEATURES
        assert "vol_20b" in PYTHON_DERIVED_FEATURES
        assert "ma_20_close" in PYTHON_DERIVED_FEATURES
        assert "rvol_20" in PYTHON_DERIVED_FEATURES

    def test_no_duplicates(self):
        """Test that there are no duplicate feature names."""
        assert len(PYTHON_DERIVED_FEATURES) == len(set(PYTHON_DERIVED_FEATURES))


class TestEnsureDatetime:
    """Test datetime conversion helper."""

    def test_already_datetime(self):
        """Test that datetime columns are unchanged."""
        df = pd.DataFrame({
            "ts": pd.date_range("2024-01-01", periods=10, freq="1min"),
            "value": range(10)
        })

        result = _ensure_datetime(df, "ts")
        assert pd.api.types.is_datetime64_any_dtype(result["ts"])

    def test_string_to_datetime(self):
        """Test conversion from string to datetime."""
        df = pd.DataFrame({
            "ts": ["2024-01-01 09:30:00"] * 10,
            "value": range(10)
        })

        result = _ensure_datetime(df, "ts")
        assert pd.api.types.is_datetime64_any_dtype(result["ts"])

    def test_missing_column(self):
        """Test handling of missing ts column."""
        df = pd.DataFrame({"value": range(10)})

        result = _ensure_datetime(df, "ts")
        assert result.equals(df)  # Unchanged


class TestAddStandardFeatures:
    """Test standard feature generation."""

    def setup_method(self):
        """Create test dataframe before each test."""
        self.df = pd.DataFrame({
            "symbol": ["TSLA"] * 100,
            "ts": pd.date_range("2024-01-01", periods=100, freq="1min"),
            "close": 100 + np.arange(100) * 0.5 + np.random.randn(100) * 2,
            "volume": np.random.randint(10000, 100000, 100),
        })

    def test_return_features(self):
        """Test return feature generation."""
        result = add_standard_features(
            self.df,
            enabled=["ret_1b", "ret_5b", "ret_10b"]
        )

        assert "ret_1b" in result.columns
        assert "ret_5b" in result.columns
        assert "ret_10b" in result.columns

        # ret_1b should be close / close.shift(1) - 1
        expected_ret_1b = self.df["close"].pct_change(1)
        pd.testing.assert_series_equal(
            result["ret_1b"],
            expected_ret_1b,
            check_names=False
        )

    def test_volatility_features(self):
        """Test volatility feature generation."""
        result = add_standard_features(
            self.df,
            enabled=["ret_1b", "vol_20b"]
        )

        assert "vol_20b" in result.columns
        # vol_20b should be rolling std of ret_1b
        assert not result["vol_20b"].isna().all()
        # First 20 bars might be NaN due to min_periods
        assert result["vol_20b"].iloc[25:].notna().all()

    def test_moving_average_features(self):
        """Test moving average feature generation."""
        result = add_standard_features(
            self.df,
            enabled=["ma_20_close", "ma_50_close", "ma_200_close"]
        )

        assert "ma_20_close" in result.columns
        assert "ma_50_close" in result.columns
        assert "ma_200_close" in result.columns

        # MA should be rolling mean
        expected_ma_20 = self.df.groupby("symbol")["close"].rolling(20, min_periods=5).mean().values
        np.testing.assert_array_almost_equal(
            result["ma_20_close"].values,
            expected_ma_20,
            decimal=10
        )

    def test_volume_features(self):
        """Test volume-related features."""
        result = add_standard_features(
            self.df,
            enabled=["vol_ma_20", "rvol_20"]
        )

        assert "vol_ma_20" in result.columns
        assert "rvol_20" in result.columns

        # RVOL should be volume / vol_ma_20
        expected_rvol = result["volume"] / result["vol_ma_20"]
        pd.testing.assert_series_equal(
            result["rvol_20"],
            expected_rvol,
            check_names=False
        )

    def test_time_features(self):
        """Test time-based features."""
        result = add_standard_features(
            self.df,
            enabled=["minute_of_day", "day_of_week"]
        )

        assert "minute_of_day" in result.columns
        assert "day_of_week" in result.columns

        # minute_of_day should be hour * 60 + minute
        expected_minute = self.df["ts"].dt.hour * 60 + self.df["ts"].dt.minute
        pd.testing.assert_series_equal(
            result["minute_of_day"],
            expected_minute,
            check_names=False
        )

        # day_of_week should be 0-6
        assert result["day_of_week"].min() >= 0
        assert result["day_of_week"].max() <= 6

    def test_multiple_symbols(self):
        """Test feature generation with multiple symbols."""
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 50 + ["AAPL"] * 50,
            "ts": pd.date_range("2024-01-01", periods=50, freq="1min").tolist() * 2,
            "close": list(range(100, 150)) + list(range(200, 250)),
            "volume": np.random.randint(10000, 100000, 100),
        })

        result = add_standard_features(
            df,
            enabled=["ret_1b", "ma_20_close"],
            group_key=["symbol"]
        )

        # Features should be computed separately for each symbol
        tsla_data = result[result["symbol"] == "TSLA"]
        aapl_data = result[result["symbol"] == "AAPL"]

        assert not tsla_data["ret_1b"].isna().all()
        assert not aapl_data["ret_1b"].isna().all()

        # First bar of each symbol should have NaN return
        assert pd.isna(tsla_data["ret_1b"].iloc[0])
        assert pd.isna(aapl_data["ret_1b"].iloc[0])

    def test_no_leakage_in_features(self):
        """Test that features don't use future data."""
        # Create dataframe with price jump at bar 50
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 100,
            "ts": pd.date_range("2024-01-01", periods=100, freq="1min"),
            "close": [100.0] * 50 + [200.0] * 50,
            "volume": [10000] * 100,
        })

        result = add_standard_features(
            df,
            enabled=["ret_1b", "ma_20_close"]
        )

        # Return at bar 49 should be 0 (no price change yet)
        assert result["ret_1b"].iloc[49] == pytest.approx(0.0, abs=1e-10)

        # Return at bar 50 should be 1.0 (100% increase)
        assert result["ret_1b"].iloc[50] == pytest.approx(1.0, rel=1e-6)

        # MA at bar 49 should not include post-jump prices
        ma_49 = result["ma_20_close"].iloc[49]
        assert ma_49 == pytest.approx(100.0, rel=1e-6)

    def test_missing_required_columns(self):
        """Test handling of missing required columns."""
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 10,
            "ts": pd.date_range("2024-01-01", periods=10, freq="1min"),
            # Missing 'close' column
        })

        # Should not crash, just skip features that need close
        result = add_standard_features(
            df,
            enabled=["ret_1b", "ma_20_close"]
        )

        # Features requiring 'close' should not be added
        assert "ret_1b" not in result.columns
        assert "ma_20_close" not in result.columns

    def test_custom_group_key(self):
        """Test using custom group keys."""
        df = pd.DataFrame({
            "symbol": ["TSLA", "TSLA", "AAPL", "AAPL"],
            "exchange": ["NASDAQ", "NYSE", "NASDAQ", "NYSE"],
            "ts": pd.date_range("2024-01-01", periods=4, freq="1min"),
            "close": [100, 101, 200, 201],
            "volume": [10000, 11000, 20000, 21000],
        })

        result = add_standard_features(
            df,
            enabled=["ret_1b"],
            group_key=["symbol", "exchange"]
        )

        # Each symbol-exchange combination should be independent
        assert "ret_1b" in result.columns

    def test_all_features_enabled(self):
        """Test enabling all features at once."""
        result = add_standard_features(self.df)  # Default: all features

        # All features should be present
        for feature in PYTHON_DERIVED_FEATURES:
            if feature in ["ret_1b", "ret_5b", "ret_10b", "vol_20b",
                          "ma_20_close", "ma_50_close", "ma_200_close",
                          "vol_ma_20", "rvol_20", "minute_of_day", "day_of_week"]:
                assert feature in result.columns, f"Missing feature: {feature}"

    def test_empty_enabled_list(self):
        """Test with empty enabled list."""
        result = add_standard_features(self.df, enabled=[])

        # Should return unchanged dataframe
        pd.testing.assert_frame_equal(result, self.df)

    def test_preserves_original_columns(self):
        """Test that original columns are preserved."""
        result = add_standard_features(
            self.df,
            enabled=["ret_1b"]
        )

        # All original columns should still be there
        for col in self.df.columns:
            assert col in result.columns

    def test_feature_dependencies(self):
        """Test that dependent features are computed correctly."""
        # rvol_20 depends on vol_ma_20
        result = add_standard_features(
            self.df,
            enabled=["vol_ma_20", "rvol_20"]
        )

        # Both should be present
        assert "vol_ma_20" in result.columns
        assert "rvol_20" in result.columns

        # rvol_20 should be volume / vol_ma_20
        expected = result["volume"] / result["vol_ma_20"]
        pd.testing.assert_series_equal(
            result["rvol_20"],
            expected,
            check_names=False
        )
