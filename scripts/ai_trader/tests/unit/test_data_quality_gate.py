"""Unit tests for DataQualityGate"""
import datetime
from unittest.mock import Mock, patch, MagicMock
import pandas as pd
import pytest

from scripts.ai_trader.gates.data_quality_gate import DataQualityGate


class TestDataQualityGateInit:
    """Test DataQualityGate initialization"""

    def test_init_with_config(self):
        """Test initialization with custom config"""
        config = {
            "table": "custom.bars_1m",
            "expected_bars_per_day": 390,
            "completeness_threshold": 0.98
        }
        gate = DataQualityGate(config)

        assert gate.config == config
        assert gate.table == "custom.bars_1m"

    def test_init_with_empty_config(self):
        """Test initialization with empty config uses defaults"""
        gate = DataQualityGate({})

        assert gate.table == "trading.bars_1m"

    def test_init_no_clickhouse(self):
        """Test initialization when ClickHouse unavailable"""
        with patch('scripts.ai_trader.gates.data_quality_gate.ClickHouseDB', None):
            gate = DataQualityGate({})
            assert gate.client is None


class TestDataQualityGateCheck:
    """Test main check method"""

    def test_check_empty_symbols(self):
        """Test check with empty symbols list"""
        gate = DataQualityGate({})
        result = gate.check([], datetime.date(2025, 10, 29))

        assert result["ok"] is True
        assert result["passed"] == []
        assert result["failed"] == {}

    def test_check_no_clickhouse_client(self):
        """Test check when ClickHouse client unavailable"""
        with patch('scripts.ai_trader.gates.data_quality_gate.ClickHouseDB', None):
            gate = DataQualityGate({})
            result = gate.check(["AAPL"], datetime.date(2025, 10, 29))

            assert result["ok"] is True  # Best-effort: passes if DB unavailable

    def test_check_all_symbols_pass(self):
        """Test check when all symbols pass quality checks"""
        gate = DataQualityGate({
            "expected_bars_per_day": 390,
            "completeness_threshold": 0.98
        })

        # Mock good quality data
        mock_df = pd.DataFrame({
            "ts_end": pd.date_range("2025-10-29 09:30", periods=390, freq="1min", tz="America/New_York"),
            "open": [100.0] * 390,
            "high": [101.0] * 390,
            "low": [99.0] * 390,
            "close": [100.5] * 390,
            "volume": [1000] * 390
        })

        with patch.object(gate, '_fetch_1m_bars', return_value=mock_df):
            result = gate.check(["AAPL", "TSLA"], datetime.date(2025, 10, 29))

        assert result["ok"] is True
        assert "AAPL" in result["passed"]
        assert "TSLA" in result["passed"]
        assert len(result["failed"]) == 0

    def test_check_some_symbols_fail(self):
        """Test check when some symbols fail quality checks"""
        gate = DataQualityGate({
            "expected_bars_per_day": 390,
            "completeness_threshold": 0.98
        })

        # Mock data with gaps (only 300 bars instead of 390)
        mock_df_bad = pd.DataFrame({
            "ts_end": pd.date_range("2025-10-29 09:30", periods=300, freq="1min", tz="America/New_York"),
            "open": [100.0] * 300,
            "high": [101.0] * 300,
            "low": [99.0] * 300,
            "close": [100.5] * 300,
            "volume": [1000] * 300
        })

        mock_df_good = pd.DataFrame({
            "ts_end": pd.date_range("2025-10-29 09:30", periods=390, freq="1min", tz="America/New_York"),
            "open": [100.0] * 390,
            "high": [101.0] * 390,
            "low": [99.0] * 390,
            "close": [100.5] * 390,
            "volume": [1000] * 390
        })

        def mock_fetch(symbol, trade_date):
            if symbol == "AAPL":
                return mock_df_bad
            else:
                return mock_df_good

        with patch.object(gate, '_fetch_1m_bars', side_effect=mock_fetch):
            result = gate.check(["AAPL", "TSLA"], datetime.date(2025, 10, 29))

        assert result["ok"] is False
        assert "TSLA" in result["passed"]
        assert "AAPL" in result["failed"]
        assert len(result["failed"]["AAPL"]) > 0

    def test_check_no_data_for_symbol(self):
        """Test check when no data available for symbol"""
        gate = DataQualityGate({})

        with patch.object(gate, '_fetch_1m_bars', return_value=None):
            result = gate.check(["AAPL"], datetime.date(2025, 10, 29))

        assert result["ok"] is False
        assert "AAPL" in result["failed"]
        assert "no_data" in result["failed"]["AAPL"][0]

    def test_check_fetch_exception(self):
        """Test check when fetch raises exception"""
        gate = DataQualityGate({})

        with patch.object(gate, '_fetch_1m_bars', side_effect=Exception("DB error")):
            result = gate.check(["AAPL"], datetime.date(2025, 10, 29))

        # Should handle exception gracefully - no data means failure
        assert result["ok"] is False


class TestFetch1mBars:
    """Test data fetching"""

    def test_fetch_success(self):
        """Test successful data fetch"""
        gate = DataQualityGate({"table": "trading.bars_1m"})

        mock_rows = [
            {"ts_end": "2025-10-29 09:30:00", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000},
            {"ts_end": "2025-10-29 09:31:00", "open": 100.5, "high": 101.5, "low": 100.0, "close": 101.0, "volume": 1500}
        ]

        with patch.object(gate.client, 'query', return_value=mock_rows):
            df = gate._fetch_1m_bars("AAPL", datetime.date(2025, 10, 29))

        assert df is not None
        assert len(df) == 2
        assert "ts_end" in df.columns
        assert "close" in df.columns

    def test_fetch_no_data(self):
        """Test fetch when no data returned"""
        gate = DataQualityGate({})

        with patch.object(gate.client, 'query', return_value=[]):
            df = gate._fetch_1m_bars("AAPL", datetime.date(2025, 10, 29))

        assert df is not None
        assert df.empty

    def test_fetch_query_exception(self):
        """Test fetch when query raises exception"""
        gate = DataQualityGate({})

        with patch.object(gate.client, 'query', side_effect=Exception("Connection error")):
            df = gate._fetch_1m_bars("AAPL", datetime.date(2025, 10, 29))

        assert df is None

    def test_fetch_normalizes_dtypes(self):
        """Test fetch normalizes data types"""
        gate = DataQualityGate({})

        mock_rows = [
            {"ts_end": "2025-10-29 09:30:00", "open": "100.0", "high": "101.0", "low": "99.0", "close": "100.5", "volume": "1000"}
        ]

        with patch.object(gate.client, 'query', return_value=mock_rows):
            df = gate._fetch_1m_bars("AAPL", datetime.date(2025, 10, 29))

        # Should convert strings to numeric
        assert pd.api.types.is_numeric_dtype(df["close"])
        assert pd.api.types.is_numeric_dtype(df["volume"])


class TestCheckGaps:
    """Test gap detection"""

    def test_check_gaps_complete_data(self):
        """Test no gaps with complete data"""
        gate = DataQualityGate({
            "expected_bars_per_day": 390,
            "completeness_threshold": 0.98
        })

        df = pd.DataFrame({
            "ts_end": pd.date_range("2025-10-29 09:30", periods=390, freq="1min", tz="America/New_York"),
            "close": [100.0] * 390
        })

        errors = gate._check_gaps(df)
        assert len(errors) == 0

    def test_check_gaps_incomplete_data(self):
        """Test gaps detected with incomplete data"""
        gate = DataQualityGate({
            "expected_bars_per_day": 390,
            "completeness_threshold": 0.98
        })

        # Only 300 bars instead of 390
        df = pd.DataFrame({
            "ts_end": pd.date_range("2025-10-29 09:30", periods=300, freq="1min", tz="America/New_York"),
            "close": [100.0] * 300
        })

        errors = gate._check_gaps(df)
        assert len(errors) > 0
        assert "gap_detection" in errors[0]
        assert "300/390" in errors[0]

    def test_check_gaps_at_threshold(self):
        """Test gaps at exactly threshold (should pass)"""
        gate = DataQualityGate({
            "expected_bars_per_day": 100,
            "completeness_threshold": 0.98
        })

        # 98 bars = exactly 98% of 100
        df = pd.DataFrame({
            "ts_end": pd.date_range("2025-10-29 09:30", periods=98, freq="1min", tz="America/New_York"),
            "close": [100.0] * 98
        })

        errors = gate._check_gaps(df)
        assert len(errors) == 0


class TestCheckOutliers:
    """Test outlier detection"""

    def test_check_outliers_normal_returns(self):
        """Test no outliers with normal returns"""
        gate = DataQualityGate({
            "max_minute_return": 0.10,
            "max_outlier_count": 3
        })

        # Normal 1% returns
        df = pd.DataFrame({
            "close": [100.0, 101.0, 101.5, 102.0, 101.8]
        })

        errors = gate._check_outliers(df)
        assert len(errors) == 0

    def test_check_outliers_excessive_returns(self):
        """Test outliers detected with excessive returns"""
        gate = DataQualityGate({
            "max_minute_return": 0.10,
            "max_outlier_count": 3
        })

        # Multiple 15% returns
        df = pd.DataFrame({
            "close": [100.0, 115.0, 132.25, 152.0, 175.0]
        })

        errors = gate._check_outliers(df)
        assert len(errors) > 0
        assert "outlier_detection" in errors[0]

    def test_check_outliers_few_outliers_ok(self):
        """Test few outliers below threshold are OK"""
        gate = DataQualityGate({
            "max_minute_return": 0.10,
            "max_outlier_count": 3
        })

        # Exactly 3 outliers (at threshold, should pass)
        # Create graduated jumps to avoid multiple triggers
        close_values = [100.0, 115.0, 115.0, 130.0, 130.0, 145.0] + [145.0] * 94
        df = pd.DataFrame({"close": close_values})

        errors = gate._check_outliers(df)
        assert len(errors) == 0


class TestCheckStaleData:
    """Test stale data detection"""

    def test_check_stale_normal_volume(self):
        """Test no stale data with normal volume"""
        gate = DataQualityGate({"max_stale_minutes": 15})

        df = pd.DataFrame({
            "volume": [1000, 1500, 1200, 1800, 2000]
        })

        errors = gate._check_stale_data(df)
        assert len(errors) == 0

    def test_check_stale_excessive_zeros(self):
        """Test stale data detected with excessive zero-volume bars"""
        gate = DataQualityGate({"max_stale_minutes": 15})

        # 20 consecutive zero-volume bars
        volume = [1000] * 10 + [0] * 20 + [1000] * 10
        df = pd.DataFrame({"volume": volume})

        errors = gate._check_stale_data(df)
        assert len(errors) > 0
        assert "stale_data" in errors[0]
        assert "20m" in errors[0]

    def test_check_stale_at_threshold(self):
        """Test stale data at exactly threshold (should pass)"""
        gate = DataQualityGate({"max_stale_minutes": 15})

        # Exactly 15 consecutive zeros (at threshold, should pass)
        volume = [1000] * 10 + [0] * 15 + [1000] * 10
        df = pd.DataFrame({"volume": volume})

        errors = gate._check_stale_data(df)
        assert len(errors) == 0

    def test_check_stale_multiple_runs(self):
        """Test stale data with multiple zero runs"""
        gate = DataQualityGate({"max_stale_minutes": 15})

        # Two runs of 10 zeros each (both OK), but max is 10
        volume = [1000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1000, 0, 0, 0, 0, 0, 1000]
        df = pd.DataFrame({"volume": volume})

        errors = gate._check_stale_data(df)
        assert len(errors) == 0


class TestCheckIntegrity:
    """Test OHLC integrity checks"""

    def test_check_integrity_valid_bars(self):
        """Test no errors with valid OHLC bars"""
        gate = DataQualityGate({})

        df = pd.DataFrame({
            "open": [100.0, 101.0, 102.0],
            "high": [101.5, 102.5, 103.5],
            "low": [99.5, 100.5, 101.5],
            "close": [101.0, 102.0, 103.0]
        })

        errors = gate._check_integrity(df)
        assert len(errors) == 0

    def test_check_integrity_high_less_than_low(self):
        """Test error when high < low"""
        gate = DataQualityGate({})

        df = pd.DataFrame({
            "open": [100.0],
            "high": [99.0],  # Invalid: high < low
            "low": [101.0],
            "close": [100.5]
        })

        errors = gate._check_integrity(df)
        assert len(errors) > 0
        assert "integrity_check" in errors[0]
        assert "1 invalid" in errors[0]

    def test_check_integrity_high_less_than_open(self):
        """Test error when high < open"""
        gate = DataQualityGate({})

        df = pd.DataFrame({
            "open": [102.0],
            "high": [101.0],  # Invalid: high < open
            "low": [99.0],
            "close": [100.5]
        })

        errors = gate._check_integrity(df)
        assert len(errors) > 0

    def test_check_integrity_high_less_than_close(self):
        """Test error when high < close"""
        gate = DataQualityGate({})

        df = pd.DataFrame({
            "open": [100.0],
            "high": [101.0],  # Invalid: high < close
            "low": [99.0],
            "close": [102.0]
        })

        errors = gate._check_integrity(df)
        assert len(errors) > 0

    def test_check_integrity_low_greater_than_open(self):
        """Test error when low > open"""
        gate = DataQualityGate({})

        df = pd.DataFrame({
            "open": [100.0],
            "high": [102.0],
            "low": [101.0],  # Invalid: low > open
            "close": [101.5]
        })

        errors = gate._check_integrity(df)
        assert len(errors) > 0

    def test_check_integrity_multiple_invalid_bars(self):
        """Test error with multiple invalid bars"""
        gate = DataQualityGate({})

        df = pd.DataFrame({
            "open": [100.0, 101.0, 102.0],
            "high": [99.0, 100.0, 101.0],  # All invalid: high < low
            "low": [101.0, 102.0, 103.0],
            "close": [100.5, 101.5, 102.5]
        })

        errors = gate._check_integrity(df)
        assert len(errors) > 0
        assert "3 invalid" in errors[0]


class TestCompleteIntegration:
    """Test complete data quality workflow"""

    def test_complete_check_with_all_issues(self):
        """Test check detecting all types of issues"""
        gate = DataQualityGate({
            "expected_bars_per_day": 100,
            "completeness_threshold": 0.98,
            "max_minute_return": 0.05,
            "max_outlier_count": 2,
            "max_stale_minutes": 5
        })

        # Create problematic data
        df = pd.DataFrame({
            "ts_end": pd.date_range("2025-10-29 09:30", periods=50, freq="1min", tz="America/New_York"),  # Gap: only 50/100
            "open": [100.0] * 50,
            "high": [99.0] * 50,  # Integrity issue: high < low
            "low": [101.0] * 50,
            "close": [100.0, 110.0, 121.0] + [100.0] * 47,  # Outliers: 10% returns
            "volume": [1000] * 10 + [0] * 15 + [1000] * 25  # Stale: 15 consecutive zeros
        })

        with patch.object(gate, '_fetch_1m_bars', return_value=df):
            result = gate.check(["PROBLEM"], datetime.date(2025, 10, 29))

        assert result["ok"] is False
        assert "PROBLEM" in result["failed"]
        errors = result["failed"]["PROBLEM"]

        # Should have multiple types of errors
        assert any("gap_detection" in e for e in errors)
        assert any("outlier_detection" in e for e in errors)
        assert any("stale_data" in e for e in errors)
        assert any("integrity_check" in e for e in errors)

    def test_complete_check_perfect_data(self):
        """Test check with perfect quality data"""
        gate = DataQualityGate({
            "expected_bars_per_day": 390,
            "completeness_threshold": 0.98,
            "max_minute_return": 0.10,
            "max_outlier_count": 3,
            "max_stale_minutes": 15
        })

        # Create perfect data
        df = pd.DataFrame({
            "ts_end": pd.date_range("2025-10-29 09:30", periods=390, freq="1min", tz="America/New_York"),
            "open": [100.0 + i * 0.01 for i in range(390)],
            "high": [100.5 + i * 0.01 for i in range(390)],
            "low": [99.5 + i * 0.01 for i in range(390)],
            "close": [100.2 + i * 0.01 for i in range(390)],
            "volume": [1000 + i * 10 for i in range(390)]
        })

        with patch.object(gate, '_fetch_1m_bars', return_value=df):
            result = gate.check(["PERFECT"], datetime.date(2025, 10, 29))

        assert result["ok"] is True
        assert "PERFECT" in result["passed"]
        assert len(result["failed"]) == 0
