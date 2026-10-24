"""Unit tests for gate modules"""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

from scripts.ai_trader.gates.health_gate import HealthGate
from scripts.ai_trader.gates.data_gate import DataGate
from scripts.ai_trader.gates.exec_gate import ExecGate
from scripts.ai_trader.gates.llm_gate import LLMGate


class TestHealthGate:
    """Test HealthGate class"""

    def test_check_missing_file(self, temp_dir):
        """Test when health report file is missing"""
        gate = HealthGate(str(temp_dir / "nonexistent.json"))
        result = gate.check()

        assert result["ok"] is False
        assert result["status"] == "missing"
        assert "not found" in result["reason"]

    def test_check_healthy_status(self, mock_health_report):
        """Test when system is healthy"""
        gate = HealthGate(str(mock_health_report))
        result = gate.check()

        assert result["ok"] is True
        assert result["status"] == "healthy"
        assert "raw" in result
        assert result["raw"]["overall_status"] == "healthy"

    def test_check_unhealthy_status(self, temp_dir):
        """Test when system is degraded"""
        report = {
            "overall_status": "degraded",
            "checks": {"clickhouse": {"status": "error"}}
        }
        report_path = temp_dir / "unhealthy.json"
        report_path.write_text(json.dumps(report))

        gate = HealthGate(str(report_path))
        result = gate.check()

        assert result["ok"] is False
        assert result["status"] == "degraded"

    def test_check_invalid_json(self, temp_dir):
        """Test when health report contains invalid JSON"""
        report_path = temp_dir / "invalid.json"
        report_path.write_text("{ invalid json }")

        gate = HealthGate(str(report_path))
        result = gate.check()

        assert result["ok"] is False
        assert result["status"] == "error"
        assert "invalid_json" in result["reason"]


class TestDataGate:
    """Test DataGate class"""

    def test_check_no_recent_ticks(self):
        """Test when no recent ticks found"""
        gate = DataGate(
            ck={"client": "clickhouse-client", "dsn": ""},
            policy={"last_10min_ticks": True, "last_day_1m_bars_ge": 390}
        )

        with patch.object(gate, '_run', return_value="0"):
            result = gate.check()

        assert result["ok"] is False
        assert "no_recent_ticks" in result["reasons"]

    def test_check_with_recent_ticks(self):
        """Test when recent ticks exist"""
        gate = DataGate(
            ck={"client": "clickhouse-client", "dsn": ""},
            policy={"last_10min_ticks": True, "last_day_1m_bars_ge": 390}
        )

        # Mock: 1000 recent ticks, 0 missing symbols
        with patch.object(gate, '_run', side_effect=["1000", "0"]):
            result = gate.check()

        assert result["ok"] is True
        assert len(result["reasons"]) == 0

    def test_check_missing_bars_yesterday(self):
        """Test when yesterday's bars are incomplete"""
        gate = DataGate(
            ck={"client": "clickhouse-client", "dsn": ""},
            policy={"last_10min_ticks": True, "last_day_1m_bars_ge": 390}
        )

        # Mock: 1000 ticks, 5 symbols with missing bars
        with patch.object(gate, '_run', side_effect=["1000", "5"]):
            result = gate.check()

        assert result["ok"] is False
        assert any("bars_1m_yesterday_missing_symbols=5" in r for r in result["reasons"])

    def test_check_clickhouse_error(self):
        """Test when ClickHouse query fails"""
        gate = DataGate(
            ck={"client": "clickhouse-client", "dsn": ""},
            policy={"last_10min_ticks": True}
        )

        with patch.object(gate, '_run', side_effect=subprocess.CalledProcessError(1, "clickhouse")):
            result = gate.check()

        assert result["ok"] is False
        assert any("clickhouse_query_error" in r for r in result["reasons"])

    def test_check_skip_policies(self):
        """Test when tick check is disabled"""
        gate = DataGate(
            ck={"client": "clickhouse-client", "dsn": ""},
            policy={"last_10min_ticks": False, "last_day_1m_bars_ge": 390}
        )

        # Mock bars check to pass (0 missing symbols)
        with patch.object(gate, '_run', return_value="0"):
            result = gate.check()

        # Should pass since ticks check disabled and bars check passes
        assert result["ok"] is True


class TestExecGate:
    """Test ExecGate class"""

    def test_check_ibkr_connected(self, temp_dir):
        """Test when IBKR is connected"""
        gate = ExecGate(project_root=str(temp_dir))

        mock_output = "IBKR Connected: True\nAccount: DU123456"
        with patch('subprocess.check_output', return_value=mock_output):
            result = gate.check()

        assert result["ok"] is True
        assert "Connected" in result["sample"]

    def test_check_ibkr_disconnected(self, temp_dir):
        """Test when IBKR is disconnected"""
        gate = ExecGate(project_root=str(temp_dir))

        # Use output that doesn't contain "Connected" substring
        mock_output = "IBKR Status: Disconnected\nNo active connection"
        with patch('subprocess.check_output', return_value=mock_output):
            result = gate.check()

        assert result["ok"] is False

    def test_check_script_error(self, temp_dir):
        """Test when check script fails"""
        gate = ExecGate(project_root=str(temp_dir))

        with patch('subprocess.check_output', side_effect=subprocess.CalledProcessError(1, "python")):
            result = gate.check()

        assert result["ok"] is False
        assert "ibkr_check_failed" in result["reasons"][0]

    def test_check_timeout(self, temp_dir):
        """Test when check script times out"""
        gate = ExecGate(project_root=str(temp_dir))

        with patch('subprocess.check_output', side_effect=subprocess.TimeoutExpired("python", 30)):
            result = gate.check()

        assert result["ok"] is False
        assert "reasons" in result


class TestLLMGate:
    """Test LLMGate class"""

    def test_check_missing_api_keys(self, monkeypatch):
        """Test when API keys are missing"""
        # Clear all API keys
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        gate = LLMGate(providers=("deepseek_nvidia", "openai"), deep_check=False)
        result = gate.check()

        assert result["ok"] is False
        assert len(result["issues"]) >= 2  # Both providers missing keys
        assert any("missing_env:deepseek_nvidia" in i for i in result["issues"])
        assert any("missing_env:openai" in i for i in result["issues"])

    def test_check_all_keys_present(self):
        """Test when all API keys are present"""
        gate = LLMGate(providers=("deepseek_nvidia", "openai", "gemini", "deepseek"), deep_check=False)

        # Mock module imports
        with patch('importlib.import_module', return_value=MagicMock()):
            result = gate.check()

        # Should pass basic checks (keys + imports)
        assert result["ok"] is True
        assert len(result["issues"]) == 0

    def test_check_import_failure(self):
        """Test when provider module import fails"""
        gate = LLMGate(providers=("openai",), deep_check=False)

        with patch('importlib.import_module', side_effect=ImportError("Module not found")):
            result = gate.check()

        assert result["ok"] is False
        assert any("import_failed:openai" in i for i in result["issues"])
        assert result["results"]["openai"]["imported"] is False

    def test_check_deep_check_success(self):
        """Test deep check when provider health check succeeds"""
        gate = LLMGate(providers=("openai",), deep_check=True, deep_check_providers=["openai"])

        mock_provider = MagicMock()

        # Create async mock for health_check
        async def mock_health_check():
            return True
        mock_provider.health_check = mock_health_check

        mock_module = MagicMock()
        mock_module.OpenAIProvider = MagicMock(return_value=mock_provider)

        with patch('importlib.import_module', return_value=mock_module):
            # Mock asyncio.wait_for to return the awaitable result
            async def mock_wait_for(coro, timeout):
                return await coro

            with patch('asyncio.wait_for', side_effect=mock_wait_for):
                result = gate.check()

        assert result["ok"] is True
        assert result["results"]["openai"]["api_key"] is True
        assert result["results"]["openai"]["imported"] is True
        assert result["results"]["openai"]["deep"] is True

    def test_check_deep_check_failure(self):
        """Test deep check when provider health check fails"""
        gate = LLMGate(providers=("openai",), deep_check=True, deep_check_providers=["openai"])

        mock_provider = MagicMock()

        # Create async mock that returns False
        async def mock_health_check():
            return False
        mock_provider.health_check = mock_health_check

        mock_module = MagicMock()
        mock_module.OpenAIProvider = MagicMock(return_value=mock_provider)

        with patch('importlib.import_module', return_value=mock_module):
            # Mock asyncio.wait_for to return the awaitable result
            async def mock_wait_for(coro, timeout):
                return await coro

            with patch('asyncio.wait_for', side_effect=mock_wait_for):
                result = gate.check()

        assert result["ok"] is False
        assert any("deep_check_failed:openai" in i for i in result["issues"])

    def test_check_partial_providers(self):
        """Test with some providers working and some failing"""
        gate = LLMGate(providers=("deepseek_nvidia", "openai"), deep_check=False)

        def mock_import(name):
            if "deepseek_nvidia" in name:
                return MagicMock()
            else:
                raise ImportError("Not found")

        with patch('importlib.import_module', side_effect=mock_import):
            result = gate.check()

        assert result["ok"] is False  # At least one failed
        assert result["results"]["deepseek_nvidia"]["imported"] is True
        assert result["results"]["openai"]["imported"] is False
