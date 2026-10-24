"""
Unit tests for ConfigGate features_table validation

Tests the ClickHouse features_table validation functionality added to fix
the "dead code" issue identified by Gemini's self-assessment.
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import tempfile

from scripts.ai_trader.gates.config_gate import ConfigGate, ConfigGatePolicy
from scripts.ai_trader.gates.validators.consistency import ConsistencyValidator


class TestValidateFeaturesTable:
    """Tests for ConfigGate._validate_features_table() method"""

    def setup_method(self):
        """Setup test fixtures"""
        self.policy = ConfigGatePolicy()
        self.temp_dir = tempfile.mkdtemp()

    def _create_hpo_config(self, features_table=None, **kwargs):
        """Helper to create HPO config file"""
        config = {
            "symbols": ["TSLA"],
            "hpo": {
                "grid": {"test": {"type": "int", "low": 1, "high": 3}},
                "objective": {"metric": "sharpe", "maximize": True}
            }
        }

        if features_table:
            config["features_table"] = features_table

        config.update(kwargs)

        path = Path(self.temp_dir) / "test_hpo.json"
        with open(path, 'w') as f:
            json.dump(config, f)

        return str(path)

    def test_features_table_exists_with_clickhouse_client(self):
        """Test validation passes when table exists"""
        # Mock ClickHouse client
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = [(1,)]  # Table exists

        gate = ConfigGate(self.policy, clickhouse_client=mock_ch_client)

        hpo_config_path = self._create_hpo_config(
            features_table="trading.features_1m"
        )

        result = gate._validate_features_table(hpo_config_path)

        assert result["ok"] is True
        assert len(result["errors"]) == 0
        assert len(result["warnings"]) == 0

        # Verify ClickHouse was queried
        mock_ch_client.execute.assert_called_once()
        call_args = mock_ch_client.execute.call_args
        assert "system.tables" in call_args[0][0]
        # Check the parameters dictionary (second argument)
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        assert params["database"] == "trading"
        assert params["table"] == "features_1m"

    def test_features_table_not_exists_with_clickhouse_client(self):
        """Test validation fails when table doesn't exist"""
        # Mock ClickHouse client - table not found
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = [(0,)]  # Table doesn't exist

        gate = ConfigGate(self.policy, clickhouse_client=mock_ch_client)

        hpo_config_path = self._create_hpo_config(
            features_table="trading.nonexistent_table"
        )

        result = gate._validate_features_table(hpo_config_path)

        assert result["ok"] is False
        assert len(result["errors"]) == 1
        assert "features_table_not_found" in result["errors"][0]
        assert "nonexistent_table" in result["errors"][0]

    def test_features_table_without_clickhouse_client(self):
        """Test validation skips check when no ClickHouse client"""
        gate = ConfigGate(self.policy, clickhouse_client=None)

        hpo_config_path = self._create_hpo_config(
            features_table="trading.features_1m"
        )

        result = gate._validate_features_table(hpo_config_path)

        assert result["ok"] is True
        assert len(result["errors"]) == 0
        assert len(result["warnings"]) == 1
        assert "not_verified" in result["warnings"][0]

    def test_features_table_missing_in_config(self):
        """Test warning when features_table not specified"""
        gate = ConfigGate(self.policy, clickhouse_client=None)

        # HPO config without features_table
        hpo_config_path = self._create_hpo_config()

        result = gate._validate_features_table(hpo_config_path)

        assert result["ok"] is True
        assert len(result["warnings"]) == 1
        assert "not specified" in result["warnings"][0]

    def test_features_table_nested_format(self):
        """Test support for nested hpo.features_table format"""
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = [(1,)]

        gate = ConfigGate(self.policy, clickhouse_client=mock_ch_client)

        # Create config with nested features_table
        config = {
            "symbols": ["TSLA"],
            "hpo": {
                "features_table": "trading.features_1m",
                "grid": {"test": {"type": "int", "low": 1, "high": 3}},
                "objective": {"metric": "sharpe", "maximize": True}
            }
        }

        path = Path(self.temp_dir) / "nested_hpo.json"
        with open(path, 'w') as f:
            json.dump(config, f)

        result = gate._validate_features_table(str(path))

        assert result["ok"] is True
        mock_ch_client.execute.assert_called_once()

    def test_clickhouse_connection_error(self):
        """Test graceful handling of ClickHouse connection errors"""
        mock_ch_client = Mock()
        mock_ch_client.execute.side_effect = Exception("Connection refused")

        gate = ConfigGate(self.policy, clickhouse_client=mock_ch_client)

        hpo_config_path = self._create_hpo_config(
            features_table="trading.features_1m"
        )

        result = gate._validate_features_table(hpo_config_path)

        assert result["ok"] is False
        assert len(result["errors"]) == 1
        assert "Failed to check" in result["errors"][0]
        assert "Connection refused" in result["errors"][0]

    def test_invalid_hpo_config_file(self):
        """Test handling of invalid HPO config file"""
        gate = ConfigGate(self.policy, clickhouse_client=None)

        # Create invalid JSON file
        path = Path(self.temp_dir) / "invalid.json"
        with open(path, 'w') as f:
            f.write("{invalid json")

        result = gate._validate_features_table(str(path))

        assert result["ok"] is False
        assert len(result["errors"]) == 1
        assert "hpo_config_load_error" in result["errors"][0]


class TestConsistencyValidatorFeaturesTable:
    """Tests for ConsistencyValidator.validate_features_table_exists()"""

    def test_table_exists_in_default_database(self):
        """Test table existence check in default database"""
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = [(1,)]

        validator = ConsistencyValidator(clickhouse_client=mock_ch_client)
        result = validator.validate_features_table_exists("my_table")

        assert result["ok"] is True
        assert result["error"] is None

        call_args = mock_ch_client.execute.call_args
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        assert params["database"] == "default"
        assert params["table"] == "my_table"

    def test_table_exists_with_database_prefix(self):
        """Test table existence check with database.table format"""
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = [(1,)]

        validator = ConsistencyValidator(clickhouse_client=mock_ch_client)
        result = validator.validate_features_table_exists("trading.features_1m")

        assert result["ok"] is True

        call_args = mock_ch_client.execute.call_args
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        assert params["database"] == "trading"
        assert params["table"] == "features_1m"

    def test_table_not_exists(self):
        """Test table not found scenario"""
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = [(0,)]  # count = 0

        validator = ConsistencyValidator(clickhouse_client=mock_ch_client)
        result = validator.validate_features_table_exists("trading.nonexistent")

        assert result["ok"] is False
        assert "does not exist" in result["error"]
        assert "nonexistent" in result["error"]

    def test_empty_result_from_clickhouse(self):
        """Test empty result from ClickHouse query"""
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = []  # Empty result

        validator = ConsistencyValidator(clickhouse_client=mock_ch_client)
        result = validator.validate_features_table_exists("trading.test")

        assert result["ok"] is False
        assert "does not exist" in result["error"]

    def test_no_clickhouse_client(self):
        """Test skipping validation when no client provided"""
        validator = ConsistencyValidator(clickhouse_client=None)
        result = validator.validate_features_table_exists("trading.test")

        assert result["ok"] is True
        assert "not provided" in result["error"]

    def test_clickhouse_query_exception(self):
        """Test handling of ClickHouse query exceptions"""
        mock_ch_client = Mock()
        mock_ch_client.execute.side_effect = RuntimeError("Network error")

        validator = ConsistencyValidator(clickhouse_client=mock_ch_client)
        result = validator.validate_features_table_exists("trading.test")

        assert result["ok"] is False
        assert "Failed to check" in result["error"]
        assert "Network error" in result["error"]


class TestConfigGateIntegration:
    """Integration tests for ConfigGate with features_table validation"""

    def setup_method(self):
        """Setup test fixtures"""
        self.temp_dir = tempfile.mkdtemp()

        # Create a minimal config.yml
        self.config_path = Path(self.temp_dir) / "config.yml"
        with open(self.config_path, 'w') as f:
            f.write("""
mode: paper
universe: [TSLA]
strategies:
  - id: test_001
    enabled: true
    module: strategies.test
    class: TestStrategy
    params:
      extras:
        symbols: [TSLA]
risk:
  daily_trading_capital: 100000
  per_trade_risk_pct: 0.01
  max_concurrent_positions: 3
  max_daily_dd_pct: 0.05
execution:
  enabled: false
kafka:
  bootstrap_servers: localhost:9092
clickhouse:
  host: localhost
  port: 8123
""")

    def _create_hpo_config(self, features_table=None):
        """Helper to create HPO config"""
        config = {
            "symbols": ["TSLA"],
            "hpo": {
                "grid": {"test": {"type": "int", "low": 1, "high": 3}},
                "objective": {"metric": "sharpe", "maximize": True}
            }
        }

        if features_table:
            config["features_table"] = features_table

        path = Path(self.temp_dir) / "hpo.json"
        with open(path, 'w') as f:
            json.dump(config, f)

        return str(path)

    def test_full_validation_with_table_exists(self):
        """Test full validation flow when table exists"""
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = [(1,)]

        policy = ConfigGatePolicy()
        gate = ConfigGate(policy, clickhouse_client=mock_ch_client)

        hpo_config_path = self._create_hpo_config("trading.features_1m")

        result = gate.check(
            config_path=str(self.config_path),
            hpo_config_path=hpo_config_path
        )

        assert result["ok"] is True
        assert "features_table" in result["details"]
        assert result["details"]["features_table"]["ok"] is True

    def test_full_validation_with_table_not_exists(self):
        """Test full validation flow when table doesn't exist"""
        mock_ch_client = Mock()
        mock_ch_client.execute.return_value = [(0,)]

        policy = ConfigGatePolicy()
        gate = ConfigGate(policy, clickhouse_client=mock_ch_client)

        hpo_config_path = self._create_hpo_config("trading.nonexistent")

        result = gate.check(
            config_path=str(self.config_path),
            hpo_config_path=hpo_config_path
        )

        assert result["ok"] is False
        assert len(result["reasons"]) >= 1
        assert any("features_table_not_found" in r for r in result["reasons"])

    def test_full_validation_without_clickhouse(self):
        """Test full validation flow without ClickHouse client"""
        policy = ConfigGatePolicy()
        gate = ConfigGate(policy, clickhouse_client=None)

        hpo_config_path = self._create_hpo_config("trading.features_1m")

        result = gate.check(
            config_path=str(self.config_path),
            hpo_config_path=hpo_config_path
        )

        assert result["ok"] is True  # Should pass with warnings
        assert "features_table" in result["details"]
        # Check for features_table warnings specifically
        ft_warnings = result["details"]["features_table"].get("warnings", [])
        assert len(ft_warnings) >= 1
        assert any("not_verified" in w for w in ft_warnings)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
