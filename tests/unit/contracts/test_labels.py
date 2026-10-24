"""Tests for label contracts."""

import numpy as np
import pandas as pd
import pytest

from contracts.labels import (
    LabelConfig,
    LABEL_REGISTRY,
    get_label_config,
    build_label_series,
    attach_label,
    get_label_required_columns,
)


class TestLabelConfig:
    """Test LabelConfig dataclass."""

    def test_default_label_config(self):
        """Test creating a label config with defaults."""
        config = LabelConfig(
            id="test_label",
            kind="horizon_return",
            horizon_bars=5,
        )

        assert config.id == "test_label"
        assert config.kind == "horizon_return"
        assert config.horizon_bars == 5
        assert config.price_col == "close"
        assert config.group_key == ["symbol"]
        assert config.ts_col == "ts"

    def test_custom_label_config(self):
        """Test creating a label config with custom values."""
        config = LabelConfig(
            id="custom_label",
            kind="horizon_return",
            horizon_bars=20,
            price_col="vwap",
            group_key=["symbol", "exchange"],
            ts_col="timestamp",
            required_columns=["symbol", "timestamp", "vwap"],
            task_types=["reg", "rank", "cls"],
        )

        assert config.price_col == "vwap"
        assert config.group_key == ["symbol", "exchange"]
        assert config.ts_col == "timestamp"
        assert "vwap" in config.required_columns
        assert len(config.task_types) == 3


class TestLabelRegistry:
    """Test label registry functionality."""

    def test_registry_has_default_label(self):
        """Test that registry contains default label."""
        assert "ret_10b_fwd" in LABEL_REGISTRY

    def test_get_label_config_success(self):
        """Test getting a label config from registry."""
        config = get_label_config("ret_10b_fwd")

        assert isinstance(config, LabelConfig)
        assert config.id == "ret_10b_fwd"
        assert config.horizon_bars == 10

    def test_get_label_config_unknown(self):
        """Test getting unknown label raises error."""
        with pytest.raises(KeyError, match="Unknown label_id"):
            get_label_config("nonexistent_label")


class TestBuildLabelSeries:
    """Test label series construction."""

    def test_horizon_return_single_symbol(self):
        """Test building horizon return label for single symbol."""
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 20,
            "ts": pd.date_range("2024-01-01", periods=20, freq="1min"),
            "close": [100 + i for i in range(20)],  # Increasing prices
        })

        config = LabelConfig(
            id="test",
            kind="horizon_return",
            horizon_bars=5,
            price_col="close",
        )

        label_series = build_label_series(df, config)

        # First label should be (close[5] / close[0]) - 1 = (105 / 100) - 1 = 0.05
        assert len(label_series) == 20
        assert not pd.isna(label_series.iloc[0])
        assert label_series.iloc[0] == pytest.approx(0.05, rel=1e-6)

        # Last 5 bars should be NaN (no future data)
        assert pd.isna(label_series.iloc[-5:]).all()

    def test_horizon_return_multiple_symbols(self):
        """Test building horizon return label for multiple symbols."""
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 15 + ["AAPL"] * 15,
            "ts": pd.date_range("2024-01-01", periods=15, freq="1min").tolist() * 2,
            "close": [100 + i for i in range(15)] + [200 + i*2 for i in range(15)],
        })

        config = LabelConfig(
            id="test",
            kind="horizon_return",
            horizon_bars=5,
        )

        label_series = build_label_series(df, config)

        # Check that labels are computed separately for each symbol
        assert len(label_series) == 30

        # TSLA first label: (105 / 100) - 1 = 0.05
        tsla_label_0 = label_series.iloc[0]
        assert tsla_label_0 == pytest.approx(0.05, rel=1e-6)

        # AAPL first label: (210 / 200) - 1 = 0.05
        aapl_label_0 = label_series.iloc[15]
        assert aapl_label_0 == pytest.approx(0.05, rel=1e-6)

    def test_horizon_return_preserves_index(self):
        """Test that label series preserves original dataframe index."""
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 10,
            "ts": pd.date_range("2024-01-01", periods=10, freq="1min"),
            "close": np.random.randn(10) * 10 + 100,
        }, index=range(100, 110))  # Custom index

        config = get_label_config("ret_10b_fwd")
        label_series = build_label_series(df, config)

        # Index should be preserved
        assert label_series.index.tolist() == list(range(100, 110))


class TestAttachLabel:
    """Test attaching labels to dataframes."""

    def test_attach_label_basic(self):
        """Test basic label attachment."""
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 20,
            "ts": pd.date_range("2024-01-01", periods=20, freq="1min"),
            "close": [100 + i for i in range(20)],
            "volume": np.random.randint(1000, 10000, 20),
        })

        df_with_label = attach_label(df, label_id="ret_10b_fwd")

        assert "label" in df_with_label.columns
        assert len(df_with_label) == len(df)
        assert df_with_label["label"].iloc[0] == pytest.approx(0.1, rel=1e-6)

    def test_attach_label_custom_colname(self):
        """Test attaching label with custom column name."""
        df = pd.DataFrame({
            "symbol": ["AAPL"] * 15,
            "ts": pd.date_range("2024-01-01", periods=15, freq="1min"),
            "close": np.random.randn(15) * 10 + 150,
        })

        df_with_label = attach_label(df, label_id="ret_10b_fwd", colname="target")

        assert "target" in df_with_label.columns
        assert "label" not in df_with_label.columns

    def test_attach_label_missing_columns(self):
        """Test that attach_label raises error for missing columns."""
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 10,
            "ts": pd.date_range("2024-01-01", periods=10, freq="1min"),
            # Missing 'close' column
        })

        with pytest.raises(ValueError, match="missing required columns"):
            attach_label(df, label_id="ret_10b_fwd")

    def test_attach_label_preserves_original(self):
        """Test that attach_label doesn't modify original dataframe."""
        df_original = pd.DataFrame({
            "symbol": ["NVDA"] * 15,
            "ts": pd.date_range("2024-01-01", periods=15, freq="1min"),
            "close": np.random.randn(15) * 10 + 200,
        })

        df_with_label = attach_label(df_original, label_id="ret_10b_fwd")

        # Original should not have label column
        assert "label" not in df_original.columns
        # New dataframe should have label
        assert "label" in df_with_label.columns


class TestGetLabelRequiredColumns:
    """Test getting required columns for labels."""

    def test_get_required_columns(self):
        """Test getting required columns for a label."""
        required = get_label_required_columns("ret_10b_fwd")

        assert isinstance(required, list)
        assert "symbol" in required
        assert "ts" in required
        assert "close" in required

    def test_get_required_columns_unknown_label(self):
        """Test that unknown label raises error."""
        with pytest.raises(KeyError):
            get_label_required_columns("unknown_label")


class TestLabelLeakagePrevention:
    """Test that labels don't leak future information improperly."""

    def test_no_leakage_in_horizon_return(self):
        """Test that horizon return doesn't use data before current bar."""
        df = pd.DataFrame({
            "symbol": ["TSLA"] * 20,
            "ts": pd.date_range("2024-01-01", periods=20, freq="1min"),
            "close": [100.0] * 10 + [200.0] * 10,  # Price jump at bar 10
        })

        config = LabelConfig(
            id="test",
            kind="horizon_return",
            horizon_bars=5,
        )

        label_series = build_label_series(df, config)

        # Label at bar 0 should only depend on future prices (bars 1-5)
        # Not on the price jump at bar 10
        assert label_series.iloc[0] == 0.0  # No change in first 5 bars

        # Label at bar 5 should see the price jump
        # (close[10] / close[5]) - 1 = (200 / 100) - 1 = 1.0
        assert label_series.iloc[5] == pytest.approx(1.0, rel=1e-6)
