"""Tests for experiment types."""

import pytest

from experiments.types import Metrics, ExperimentRecord


class TestMetrics:
    """Test Metrics dataclass."""

    def test_metrics_creation(self):
        """Test creating metrics with all fields."""
        m = Metrics(
            sharpe=2.5,
            sortino=3.0,
            ic=0.15,
            hit_rate=0.58,
            max_drawdown=-0.12,
            pnl=0.25,
            pnl_annualized=0.45,
            trades=150,
            avg_trade_pnl=0.00167,
        )

        assert m.sharpe == 2.5
        assert m.sortino == 3.0
        assert m.ic == 0.15
        assert m.hit_rate == 0.58
        assert m.max_drawdown == -0.12
        assert m.pnl == 0.25
        assert m.trades == 150

    def test_metrics_partial_fields(self):
        """Test metrics with only some fields set."""
        m = Metrics(sharpe=1.8, trades=100)

        assert m.sharpe == 1.8
        assert m.trades == 100
        assert m.ic is None
        assert m.sortino is None

    def test_metrics_from_dict(self):
        """Test creating metrics from dict."""
        d = {"sharpe": 2.0, "ic": 0.12, "trades": 200}
        m = Metrics.from_dict(d)

        assert m.sharpe == 2.0
        assert m.ic == 0.12
        assert m.trades == 200
        assert m.sortino is None  # Not in dict

    def test_metrics_to_dict(self):
        """Test converting metrics to dict."""
        m = Metrics(sharpe=1.5, ic=0.10, trades=50)
        d = m.to_dict()

        assert d["sharpe"] == 1.5
        assert d["ic"] == 0.10
        assert d["trades"] == 50
        assert "sortino" in d  # Should have all fields, even if None


class TestExperimentRecord:
    """Test ExperimentRecord dataclass."""

    def test_experiment_record_creation(self):
        """Test creating experiment record."""
        metrics = Metrics(sharpe=2.5, ic=0.15, trades=100)
        rec = ExperimentRecord(
            run_id="test_run_001",
            symbol="TSLA",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            model_type="xgb",
            feature_subset=["close", "volume", "rvol_20"],
            params={"max_depth": 5, "learning_rate": 0.1},
            metrics=metrics,
            run_ts="2025-11-17T10:00:00Z",
            data_start_ts="2025-01-01T00:00:00Z",
            data_end_ts="2025-11-15T00:00:00Z",
            manifest_path="/path/to/manifest.json",
            tags=["trend", "fast"],
        )

        assert rec.run_id == "test_run_001"
        assert rec.symbol == "TSLA"
        assert rec.freq == "1m"
        assert rec.horizon == "10m"
        assert rec.label_id == "ret_10m_fwd"
        assert rec.model_type == "xgb"
        assert rec.feature_subset == ["close", "volume", "rvol_20"]
        assert rec.params == {"max_depth": 5, "learning_rate": 0.1}
        assert rec.metrics.sharpe == 2.5
        assert rec.tags == ["trend", "fast"]

    def test_experiment_record_to_dict(self):
        """Test converting record to dict."""
        metrics = Metrics(sharpe=1.8, trades=80)
        rec = ExperimentRecord(
            run_id="test_002",
            symbol="NVDA",
            freq="5s",
            horizon="5m",
            label_id="ret_5m_fwd",
            model_type="lgbm",
            feature_subset=["close"],
            params={},
            metrics=metrics,
            run_ts="2025-11-17T11:00:00Z",
        )

        d = rec.to_dict()
        assert d["run_id"] == "test_002"
        assert d["symbol"] == "NVDA"
        assert d["metrics"]["sharpe"] == 1.8
        assert d["metrics"]["trades"] == 80

    def test_experiment_record_from_dict(self):
        """Test creating record from dict."""
        d = {
            "run_id": "test_003",
            "symbol": "AAPL",
            "freq": "1m",
            "horizon": "15m",
            "label_id": "ret_15m_fwd",
            "model_type": "xgb",
            "feature_subset": ["close", "volume"],
            "params": {"n_estimators": 100},
            "metrics": {"sharpe": 2.2, "ic": 0.18},
            "run_ts": "2025-11-17T12:00:00Z",
            "tags": ["momentum"],
        }

        rec = ExperimentRecord.from_dict(d)
        assert rec.run_id == "test_003"
        assert rec.symbol == "AAPL"
        assert rec.metrics.sharpe == 2.2
        assert rec.metrics.ic == 0.18
        assert rec.tags == ["momentum"]

    def test_experiment_record_from_dict_string_feature_subset(self):
        """Test handling of string feature_subset."""
        d = {
            "run_id": "test_004",
            "symbol": "GOOGL",
            "freq": "1m",
            "horizon": "10m",
            "label_id": "ret_10m_fwd",
            "model_type": "ridge",
            "feature_subset": "close",  # String instead of list
            "params": {},
            "metrics": {},
            "run_ts": "2025-11-17T13:00:00Z",
        }

        rec = ExperimentRecord.from_dict(d)
        assert rec.feature_subset == ["close"]  # Should be converted to list

    def test_experiment_record_from_dict_string_tags(self):
        """Test handling of string tags."""
        d = {
            "run_id": "test_005",
            "symbol": "MSFT",
            "freq": "1m",
            "horizon": "10m",
            "label_id": "ret_10m_fwd",
            "model_type": "xgb",
            "feature_subset": ["close"],
            "params": {},
            "metrics": {},
            "run_ts": "2025-11-17T14:00:00Z",
            "tags": "trend",  # String instead of list
        }

        rec = ExperimentRecord.from_dict(d)
        assert rec.tags == ["trend"]  # Should be converted to list

    def test_experiment_record_from_dict_none_tags(self):
        """Test handling of None tags."""
        d = {
            "run_id": "test_006",
            "symbol": "META",
            "freq": "1m",
            "horizon": "10m",
            "label_id": "ret_10m_fwd",
            "model_type": "lgbm",
            "feature_subset": ["close"],
            "params": {},
            "metrics": {},
            "run_ts": "2025-11-17T15:00:00Z",
            # tags not provided
        }

        rec = ExperimentRecord.from_dict(d)
        assert rec.tags == []  # Should default to empty list
