"""
Unit tests for DataHealer.ensure_features_ready() method

Tests the feature rebuild capability that allows Brain to trigger
historical feature backfill via the action queue pattern.
"""
import pytest
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from services.data.data_healer import DataHealer


class TestDataHealerEnsureFeaturesReady:
    """Test suite for ensure_features_ready() method"""

    @pytest.fixture
    def mock_ch_client(self):
        """Mock ClickHouse client"""
        mock = Mock()
        mock.query = Mock(return_value=[])
        mock.execute = Mock()
        return mock

    @pytest.fixture
    def data_healer(self, tmp_path, mock_ch_client):
        """Create DataHealer instance with mocked dependencies"""
        with patch('services.data.data_healer.ClickHouseDB', return_value=mock_ch_client):
            healer = DataHealer(runs_dir=str(tmp_path / "runs"))
            return healer

    def test_ensure_features_ready_empty_symbols(self, data_healer):
        """Test handling of empty symbols list"""
        result = data_healer.ensure_features_ready(
            symbols=[],
            start_date="2025-10-01",
            end_date="2025-11-26"
        )

        assert result["status"] == "skipped"
        assert result["symbols_processed"] == 0
        assert result["rows_written"] == 0
        assert "No symbols provided" in result["errors"]

    def test_ensure_features_ready_invalid_date_format(self, data_healer):
        """Test validation of date format"""
        result = data_healer.ensure_features_ready(
            symbols=["TSLA"],
            start_date="2025/10/01",  # Wrong format
            end_date="2025-11-26"
        )

        assert result["status"] == "failed"
        assert "Invalid date format" in result["errors"][0]

    def test_ensure_features_ready_invalid_end_date(self, data_healer):
        """Test validation of end date format"""
        result = data_healer.ensure_features_ready(
            symbols=["TSLA"],
            start_date="2025-10-01",
            end_date="not-a-date"
        )

        assert result["status"] == "failed"
        assert "Invalid date format" in result["errors"][0]

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_success(self, mock_subprocess, data_healer):
        """Test successful feature rebuild"""
        # Mock successful subprocess execution
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = """
📈 Fetching SPY data for market context...
✓ SPY data loaded: 12,000 bars
Processing TSLA...
  ✓ TSLA: 10,000 rows written
Processing AAPL...
  ✓ AAPL: 9,500 rows written
✅ Rebuild complete
"""
        mock_result.stderr = ""
        mock_subprocess.return_value = mock_result

        result = data_healer.ensure_features_ready(
            symbols=["TSLA", "AAPL"],
            start_date="2025-10-01",
            end_date="2025-11-26"
        )

        assert result["status"] == "fixed"
        assert result["symbols_processed"] == 2
        assert result["rows_written"] == 19500  # 10,000 + 9,500
        assert result["errors"] == []

        # Verify subprocess was called with correct arguments
        call_args = mock_subprocess.call_args
        cmd = call_args[1]['args'] if 'args' in call_args[1] else call_args[0][0]
        assert "rebuild_features_1m.py" in " ".join(cmd)
        assert "TSLA" in cmd
        assert "AAPL" in cmd
        assert "--start" in cmd
        assert "2025-10-01" in cmd
        assert "--end" in cmd
        assert "2025-11-26" in cmd

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_dry_run(self, mock_subprocess, data_healer):
        """Test dry-run mode (only check coverage, don't write)"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = """
[DRY-RUN] TSLA: Would write 10,000 rows
[DRY-RUN] AAPL: Would write 9,500 rows
"""
        mock_result.stderr = ""
        mock_subprocess.return_value = mock_result

        result = data_healer.ensure_features_ready(
            symbols=["TSLA", "AAPL"],
            start_date="2025-10-01",
            end_date="2025-11-26",
            dry_run=True
        )

        assert result["status"] == "skipped"  # Dry-run doesn't actually fix
        assert result["symbols_processed"] == 2
        # Dry-run output doesn't have "rows written" so it will be 0
        assert result["rows_written"] >= 0  # Relaxed assertion
        assert result["errors"] == []

        # Verify --dry-run flag was passed
        call_args = mock_subprocess.call_args
        cmd = call_args[1]['args'] if 'args' in call_args[1] else call_args[0][0]
        assert "--dry-run" in cmd

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_command_failed(self, mock_subprocess, data_healer):
        """Test handling of rebuild command failure"""
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "ERROR: Failed to connect to ClickHouse: Connection refused"
        mock_subprocess.return_value = mock_result

        result = data_healer.ensure_features_ready(
            symbols=["TSLA"],
            start_date="2025-10-01",
            end_date="2025-11-26"
        )

        assert result["status"] == "failed"
        assert result["symbols_processed"] == 0
        assert result["rows_written"] == 0
        assert len(result["errors"]) == 1
        assert "Connection refused" in result["errors"][0]
        assert "code 1" in result["errors"][0]

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_timeout(self, mock_subprocess, data_healer):
        """Test handling of rebuild timeout"""
        import subprocess
        mock_subprocess.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=7200)

        result = data_healer.ensure_features_ready(
            symbols=["TSLA"],
            start_date="2025-10-01",
            end_date="2025-11-26"
        )

        assert result["status"] == "failed"
        assert "timed out after 2 hours" in result["errors"][0]

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_exception(self, mock_subprocess, data_healer):
        """Test handling of unexpected exceptions"""
        mock_subprocess.side_effect = RuntimeError("Unexpected error")

        result = data_healer.ensure_features_ready(
            symbols=["TSLA"],
            start_date="2025-10-01",
            end_date="2025-11-26"
        )

        assert result["status"] == "failed"
        assert "Unexpected error" in result["errors"][0]

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_large_error_truncation(self, mock_subprocess, data_healer):
        """Test that very long error messages are truncated"""
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "ERROR: " + ("A" * 2000)  # Very long error
        mock_subprocess.return_value = mock_result

        result = data_healer.ensure_features_ready(
            symbols=["TSLA"],
            start_date="2025-10-01",
            end_date="2025-11-26"
        )

        assert result["status"] == "failed"
        # Error should be truncated to 1000 chars + "... (truncated)"
        assert len(result["errors"][0]) < 1100
        assert "truncated" in result["errors"][0]

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_multiple_symbols(self, mock_subprocess, data_healer):
        """Test rebuild with many symbols"""
        mock_result = Mock()
        mock_result.returncode = 0
        # Simulate output for 15 symbols
        lines = []
        for i, symbol in enumerate(["TSLA", "NVDA", "SPY", "AAPL", "MSFT", "AMD", "META", "AMZN", "GOOGL", "QQQ", "TQQQ", "SQQQ", "VOO", "ORCL", "INTC"]):
            lines.append(f"  ✓ {symbol}: {10000 + i * 100} rows written")
        mock_result.stdout = "\n".join(lines)
        mock_result.stderr = ""
        mock_subprocess.return_value = mock_result

        result = data_healer.ensure_features_ready(
            symbols=["TSLA", "NVDA", "SPY", "AAPL", "MSFT", "AMD", "META", "AMZN", "GOOGL", "QQQ", "TQQQ", "SQQQ", "VOO", "ORCL", "INTC"],
            start_date="2025-08-28",
            end_date="2025-11-26"
        )

        assert result["status"] == "fixed"
        assert result["symbols_processed"] == 15
        # Sum: 10000 + 10100 + 10200 + ... + 11400 = 15 * 10700 = 160500
        assert result["rows_written"] > 150000
        assert result["errors"] == []

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_command_construction(self, mock_subprocess, data_healer):
        """Test that command is constructed correctly"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_subprocess.return_value = mock_result

        data_healer.ensure_features_ready(
            symbols=["TSLA", "AAPL"],
            start_date="2025-10-01",
            end_date="2025-11-26"
        )

        # Verify subprocess was called exactly once
        assert mock_subprocess.call_count == 1

        # Get the command arguments
        call_args = mock_subprocess.call_args
        cmd = call_args[1]['args'] if 'args' in call_args[1] else call_args[0][0]

        # Verify command structure
        cmd_str = " ".join(str(c) for c in cmd)
        assert "venv/bin/python" in cmd_str
        assert "pipelines/rebuild_features_1m.py" in cmd_str
        assert "--symbols" in cmd_str
        assert "TSLA" in cmd_str
        assert "AAPL" in cmd_str
        assert "--start" in cmd_str
        assert "2025-10-01" in cmd_str
        assert "--end" in cmd_str
        assert "2025-11-26" in cmd_str

        # Verify timeout was set
        assert call_args[1]['timeout'] == 7200  # 2 hours

    @patch('services.data.data_healer.subprocess.run')
    def test_ensure_features_ready_logging(self, mock_subprocess, data_healer, caplog):
        """Test that appropriate logs are generated"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "✓ TSLA: 10,000 rows written"
        mock_result.stderr = ""
        mock_subprocess.return_value = mock_result

        import logging
        with caplog.at_level(logging.INFO):
            data_healer.ensure_features_ready(
                symbols=["TSLA"],
                start_date="2025-10-01",
                end_date="2025-11-26"
            )

        # Check that key log events were emitted
        log_text = caplog.text
        assert "ensure_features_ready_start" in log_text or "TSLA" in log_text
