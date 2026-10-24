"""Tests for label engine."""

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labels.label_engine import (
    LabelEngine,
    LabelRegistry,
    LabelSpec,
    LabelSanityConfig,
)


class TestLabelSpec:
    """Test LabelSpec dataclass."""

    def test_label_spec_creation(self):
        """Test creating label specification."""
        spec = LabelSpec(
            name="ret_10m",
            kind="forward_return",
            horizon_min=10,
            params={},
        )

        assert spec.name == "ret_10m"
        assert spec.kind == "forward_return"
        assert spec.horizon_min == 10
        assert spec.params == {}


class TestLabelSanityConfig:
    """Test LabelSanityConfig dataclass."""

    def test_sanity_config_defaults(self):
        """Test default sanity configuration."""
        config = LabelSanityConfig()

        assert config.max_nan_frac == 0.05
        assert config.min_variance == 1e-6
        assert config.check_balance is False
        assert config.min_class_ratio == 0.1

    def test_sanity_config_custom(self):
        """Test custom sanity configuration."""
        config = LabelSanityConfig(
            max_nan_frac=0.1,
            min_variance=1e-4,
            check_balance=True,
            min_class_ratio=0.2,
        )

        assert config.max_nan_frac == 0.1
        assert config.min_variance == 1e-4
        assert config.check_balance is True
        assert config.min_class_ratio == 0.2


class TestLabelRegistry:
    """Test LabelRegistry class."""

    @pytest.fixture
    def registry_file(self):
        """Create temporary registry file."""
        registry_data = {
            "version": "1.0",
            "labels": {
                "ret_10m": {
                    "kind": "forward_return",
                    "horizon_min": 10,
                    "params": {},
                },
                "tb_10m_atr1": {
                    "kind": "triple_barrier",
                    "horizon_min": 10,
                    "params": {
                        "profit_pct": 1.0,
                        "loss_pct": 1.0,
                        "use_atr": True,
                        "atr_window": 14,
                    },
                },
            },
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            import json

            json.dump(registry_data, f)
            temp_path = f.name

        yield temp_path

        # Cleanup
        Path(temp_path).unlink()

    def test_registry_loads_from_file(self, registry_file):
        """Test loading registry from JSON file."""
        registry = LabelRegistry(registry_file)

        labels = registry.list_labels()
        assert "ret_10m" in labels
        assert "tb_10m_atr1" in labels

    def test_registry_get_label(self, registry_file):
        """Test getting label specification."""
        registry = LabelRegistry(registry_file)

        spec = registry.get("ret_10m")
        assert spec.name == "ret_10m"
        assert spec.kind == "forward_return"
        assert spec.horizon_min == 10

    def test_registry_get_nonexistent_raises_error(self, registry_file):
        """Test that getting non-existent label raises error."""
        registry = LabelRegistry(registry_file)

        with pytest.raises(KeyError, match="not found"):
            registry.get("nonexistent_label")

    def test_registry_nonexistent_file_raises_error(self):
        """Test that non-existent registry file raises error."""
        with pytest.raises(FileNotFoundError):
            LabelRegistry("/nonexistent/path/registry.json")


class TestLabelEngine:
    """Test LabelEngine class."""

    @pytest.fixture
    def sample_ohlcv_data(self):
        """Create sample OHLCV DataFrame."""
        np.random.seed(42)

        n = 100
        base_price = 100.0
        returns = np.random.randn(n) * 0.01

        close = base_price * np.exp(np.cumsum(returns))
        high = close * (1 + np.abs(np.random.randn(n)) * 0.005)
        low = close * (1 - np.abs(np.random.randn(n)) * 0.005)
        open_ = close * (1 + np.random.randn(n) * 0.003)
        volume = np.random.randint(1000, 10000, n)

        df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=n, freq="1min"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )

        return df

    @pytest.fixture
    def registry_file(self):
        """Create temporary registry file."""
        registry_data = {
            "version": "1.0",
            "labels": {
                "ret_10m": {
                    "kind": "forward_return",
                    "horizon_min": 10,
                    "params": {},
                },
                "tb_10m_fixed": {
                    "kind": "triple_barrier",
                    "horizon_min": 10,
                    "params": {
                        "profit_pct": 0.01,
                        "loss_pct": 0.01,
                        "use_atr": False,
                    },
                },
                "tb_10m_atr": {
                    "kind": "triple_barrier",
                    "horizon_min": 10,
                    "params": {
                        "profit_pct": 1.0,
                        "loss_pct": 1.0,
                        "use_atr": True,
                        "atr_window": 14,
                    },
                },
            },
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            import json

            json.dump(registry_data, f)
            temp_path = f.name

        yield temp_path

        Path(temp_path).unlink()

    @pytest.fixture
    def engine(self, registry_file):
        """Create label engine instance."""
        registry = LabelRegistry(registry_file)
        return LabelEngine(registry)

    # ---------- Forward Return Tests ----------

    def test_forward_return_computation(self, engine, sample_ohlcv_data):
        """Test forward return label computation."""
        df = sample_ohlcv_data.copy()

        result = engine.attach_label(df, "ret_10m")

        assert "ret_10m" in result.columns
        assert len(result) == len(df)

        # Last 10 rows should be NaN (no future data)
        assert result["ret_10m"].iloc[-10:].isna().all()

        # First rows should have valid returns
        valid_returns = result["ret_10m"].iloc[:-10]
        assert not valid_returns.isna().any()

        # Check return calculation for first row
        close_t0 = df["close"].iloc[0]
        close_t10 = df["close"].iloc[10]
        expected_return = (close_t10 - close_t0) / close_t0

        assert np.isclose(result["ret_10m"].iloc[0], expected_return, rtol=1e-6)

    def test_compute_label_only(self, engine, sample_ohlcv_data):
        """Test compute_label_only returns Series without modifying DataFrame."""
        df = sample_ohlcv_data.copy()

        label_series = engine.compute_label_only(df, "ret_10m")

        assert isinstance(label_series, pd.Series)
        assert len(label_series) == len(df)
        assert "ret_10m" not in df.columns  # Original DataFrame unchanged

    # ---------- Triple Barrier Tests ----------

    def test_triple_barrier_fixed_pct(self, engine, sample_ohlcv_data):
        """Test triple barrier with fixed percentage."""
        df = sample_ohlcv_data.copy()

        result = engine.attach_label(df, "tb_10m_fixed")

        assert "tb_10m_fixed" in result.columns

        # Last 10 rows should be NaN
        assert result["tb_10m_fixed"].iloc[-10:].isna().all()

        # Valid labels should be in {-1, 0, 1}
        valid_labels = result["tb_10m_fixed"].iloc[:-10].dropna()
        unique_values = valid_labels.unique()
        assert all(v in {-1.0, 0.0, 1.0} for v in unique_values)

    def test_triple_barrier_with_atr(self, engine, sample_ohlcv_data):
        """Test triple barrier with ATR-based barriers."""
        df = sample_ohlcv_data.copy()

        result = engine.attach_label(df, "tb_10m_atr")

        assert "tb_10m_atr" in result.columns

        # Valid labels should be in {-1, 0, 1}
        valid_labels = result["tb_10m_atr"].dropna()
        unique_values = valid_labels.unique()
        assert all(v in {-1.0, 0.0, 1.0} for v in unique_values)

    def test_triple_barrier_profit_scenario(self, engine):
        """Test triple barrier correctly identifies profit scenario."""
        # Create data where price rises 2% in 5 bars
        df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=20, freq="1min"),
                "open": [100.0] * 20,
                "high": [100.0, 100.5, 101.0, 101.5, 102.5] + [102.0] * 15,
                "low": [99.5] * 20,
                "close": [100.0] * 20,
                "volume": [1000] * 20,
            }
        )

        result = engine.attach_label(df, "tb_10m_fixed")

        # First bar should hit upper barrier (+1% = 101) within horizon
        # Since high reaches 102.5, this should be labeled as 1.0 (profit)
        assert result["tb_10m_fixed"].iloc[0] == 1.0

    def test_triple_barrier_loss_scenario(self, engine):
        """Test triple barrier correctly identifies loss scenario."""
        # Create data where price drops 2% in 5 bars
        df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=20, freq="1min"),
                "open": [100.0] * 20,
                "high": [100.5] * 20,
                "low": [100.0, 99.5, 99.0, 98.5, 97.5] + [98.0] * 15,
                "close": [100.0] * 20,
                "volume": [1000] * 20,
            }
        )

        result = engine.attach_label(df, "tb_10m_fixed")

        # First bar should hit lower barrier (-1% = 99) within horizon
        assert result["tb_10m_fixed"].iloc[0] == -1.0

    def test_triple_barrier_time_barrier(self, engine):
        """Test triple barrier time barrier scenario."""
        # Create data with no significant price movement
        df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=20, freq="1min"),
                "open": [100.0] * 20,
                "high": [100.2] * 20,  # Only 0.2% movement
                "low": [99.8] * 20,
                "close": [100.0] * 20,
                "volume": [1000] * 20,
            }
        )

        result = engine.attach_label(df, "tb_10m_fixed")

        # Should hit time barrier (0.0) since price stays within 1% range
        assert result["tb_10m_fixed"].iloc[0] == 0.0

    # ---------- Sanity Check Tests ----------

    def test_sanity_check_passes(self, engine, sample_ohlcv_data):
        """Test that valid labels pass sanity checks."""
        df = sample_ohlcv_data.copy()

        sanity = LabelSanityConfig(
            max_nan_frac=0.15,  # Allow 10 NaN out of 100
            min_variance=1e-8,
            check_balance=False,
        )

        # Should not raise error
        result = engine.attach_label(df, "ret_10m", sanity_config=sanity)
        assert "ret_10m" in result.columns

    def test_sanity_check_fails_too_many_nans(self, engine, sample_ohlcv_data):
        """Test sanity check fails with too many NaN values."""
        df = sample_ohlcv_data.copy()

        # Strict NaN limit
        sanity = LabelSanityConfig(max_nan_frac=0.05)

        # ret_10m will have 10 NaN out of 100 = 10%, which exceeds 5%
        with pytest.raises(ValueError, match="NaN values"):
            engine.attach_label(df, "ret_10m", sanity_config=sanity)

    def test_sanity_check_fails_low_variance(self, engine):
        """Test sanity check fails with low variance."""
        # Create constant price data with enough rows to avoid NaN check failure
        # ret_10m has 10 NaN at end, so with 200 rows = 5% NaN (acceptable)
        df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=200, freq="1min"),
                "open": [100.0] * 200,
                "high": [100.0] * 200,
                "low": [100.0] * 200,
                "close": [100.0] * 200,
                "volume": [1000] * 200,
            }
        )

        sanity = LabelSanityConfig(
            max_nan_frac=0.06,  # Allow 5% NaN
            min_variance=1e-6,
        )

        # Forward return should be all zeros (variance = 0)
        with pytest.raises(ValueError, match="variance"):
            engine.attach_label(df, "ret_10m", sanity_config=sanity)

    def test_sanity_check_class_balance(self, engine, sample_ohlcv_data):
        """Test class balance check for triple barrier labels."""
        df = sample_ohlcv_data.copy()

        sanity = LabelSanityConfig(
            max_nan_frac=0.15,
            check_balance=True,
            min_class_ratio=0.05,  # At least 5% for minority class
        )

        # Should pass with normal market data
        result = engine.attach_label(df, "tb_10m_fixed", sanity_config=sanity)
        assert "tb_10m_fixed" in result.columns

    def test_sanity_check_fails_single_class(self, engine):
        """Test sanity check fails when all labels are same value."""
        # Create data that always hits upper barrier
        # Need 200 rows so 10 NaN = 5% (acceptable for NaN check)
        df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=200, freq="1min"),
                "open": [100.0] * 200,
                "high": [105.0] * 200,  # Always +5%
                "low": [99.0] * 200,
                "close": [100.0] * 200,
                "volume": [1000] * 200,
            }
        )

        sanity = LabelSanityConfig(
            max_nan_frac=0.06,  # Allow 5% NaN
            min_variance=0.0,  # Disable variance check to test class balance
            check_balance=True,
            min_class_ratio=0.1,
        )

        # Will fail with "only one unique value" error
        with pytest.raises(ValueError, match="only one unique value"):
            engine.attach_label(df, "tb_10m_fixed", sanity_config=sanity)

    # ---------- ATR Computation Tests ----------

    def test_atr_computation(self, engine, sample_ohlcv_data):
        """Test ATR computation is reasonable."""
        df = sample_ohlcv_data.copy()

        atr = engine._compute_atr(df, window=14)

        assert len(atr) == len(df)
        # First 13 values should be NaN (rolling window)
        assert np.isnan(atr[:13]).all()
        # Rest should be positive
        assert (atr[13:] > 0).all()

    # ---------- Error Handling Tests ----------

    def test_unknown_label_raises_error(self, engine, sample_ohlcv_data):
        """Test that unknown label ID raises error."""
        df = sample_ohlcv_data.copy()

        with pytest.raises(KeyError, match="not found"):
            engine.attach_label(df, "nonexistent_label")

    def test_missing_columns_raises_error(self, engine):
        """Test that missing required columns raises error."""
        # DataFrame without required columns
        df = pd.DataFrame({"ts": pd.date_range("2024-01-01", periods=10, freq="1min")})

        with pytest.raises(KeyError):
            engine.attach_label(df, "ret_10m")
