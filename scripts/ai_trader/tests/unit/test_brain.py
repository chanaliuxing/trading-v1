"""Unit tests for ControlBrain class"""
import json
import asyncio
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, AsyncMock
import pytest

from scripts.ai_trader.brain.brain import ControlBrain


class TestControlBrainInit:
    """Test ControlBrain initialization"""

    def test_init_with_defaults(self, temp_dir):
        """Test initialization with default provider"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        assert brain.runs_dir == temp_dir
        assert brain.provider == "deepseek_nvidia"
        assert brain.model == "deepseek-ai/deepseek-r1"
        assert brain._prov is None

    def test_init_with_custom_provider(self, temp_dir):
        """Test initialization with custom provider"""
        brain = ControlBrain(
            runs_dir=str(temp_dir),
            provider="openai",
            model="gpt-4"
        )

        assert brain.provider == "openai"
        assert brain.model == "gpt-4"

    def test_ensure_provider_missing_api_key(self, temp_dir, monkeypatch):
        """Test provider initialization fails without API key"""
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

        brain = ControlBrain(runs_dir=str(temp_dir))

        with pytest.raises(RuntimeError, match="Missing API key"):
            brain._ensure_provider()

    def test_ensure_provider_success(self, temp_dir):
        """Test provider initialization succeeds with API key"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        # Provider will be created from real import, we just verify no error
        result = brain._ensure_provider()

        # Should return a provider instance
        assert result is not None
        assert brain._prov == result

    def test_ensure_provider_caches_instance(self, temp_dir):
        """Test provider is cached after first call"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_provider = MagicMock()
        brain._prov = mock_provider

        result = brain._ensure_provider()

        assert result == mock_provider


class TestControlBrainAsk:
    """Test ControlBrain._ask method"""

    def test_ask_success(self, temp_dir):
        """Test successful LLM query"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_response = MagicMock()
        mock_response.raw_response = '{"proceed": true, "notes": "All clear"}'

        mock_provider = MagicMock()

        async def mock_analyze(symbol, prompt):
            return mock_response

        mock_provider.analyze_market_with_prompt = mock_analyze
        brain._prov = mock_provider

        result = asyncio.run(brain._ask("test prompt"))

        assert result["proceed"] is True
        assert result["notes"] == "All clear"

    def test_ask_timeout(self, temp_dir):
        """Test LLM query timeout"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_provider = MagicMock()

        async def mock_analyze_timeout(symbol, prompt):
            await asyncio.sleep(100)  # Will be cancelled by wait_for

        mock_provider.analyze_market_with_prompt = mock_analyze_timeout
        brain._prov = mock_provider

        result = asyncio.run(brain._ask("test prompt", timeout=0.1))

        assert result["error"] == "timeout"

    def test_ask_llm_error(self, temp_dir):
        """Test LLM query error"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_provider = MagicMock()

        async def mock_analyze_error(symbol, prompt):
            raise ValueError("API error")

        mock_provider.analyze_market_with_prompt = mock_analyze_error
        brain._prov = mock_provider

        result = asyncio.run(brain._ask("test prompt"))

        assert "error" in result
        assert "API error" in result["error"]

    def test_ask_invalid_json_fallback(self, temp_dir):
        """Test JSON extraction from malformed response"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_response = MagicMock()
        # Simulate markdown-wrapped JSON response
        mock_response.raw_response = 'Sure! Here is the result:\n```json\n{"proceed": true}\n```'

        mock_provider = MagicMock()

        async def mock_analyze(symbol, prompt):
            return mock_response

        mock_provider.analyze_market_with_prompt = mock_analyze
        brain._prov = mock_provider

        result = asyncio.run(brain._ask("test prompt"))

        # Should extract the JSON object from within the text
        assert "proceed" in result

    def test_ask_empty_response(self, temp_dir):
        """Test handling of empty response"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_response = MagicMock()
        mock_response.raw_response = None

        mock_provider = MagicMock()

        async def mock_analyze(symbol, prompt):
            return mock_response

        mock_provider.analyze_market_with_prompt = mock_analyze
        brain._prov = mock_provider

        result = asyncio.run(brain._ask("test prompt"))

        # Should return empty dict when response is None
        assert result == {}


class TestControlBrainProposePlan:
    """Test ControlBrain.propose_plan method"""

    def test_propose_plan_morning_success(self, temp_dir):
        """Test proposing morning plan"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_plan = {
            "session": "morning",
            "providers": [{"name": "deepseek_nvidia", "interval_sec": 600, "cycles": 3}],
            "notes": "Standard morning plan"
        }

        with patch.object(brain, '_ask', return_value=mock_plan):
            context = {"date": "2025-10-29", "health": "ok"}
            result = brain.propose_plan("morning", context)

            assert result["session"] == "morning"
            assert len(result["providers"]) == 1
            assert result["providers"][0]["name"] == "deepseek_nvidia"

    def test_propose_plan_saves_to_file(self, temp_dir):
        """Test plan is saved to runs directory"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_plan = {
            "session": "intraday",
            "providers": [{"name": "openai", "interval_sec": 60}]
        }

        with patch.object(brain, '_ask', return_value=mock_plan):
            context = {"date": "2025-10-29"}
            brain.propose_plan("intraday", context)

            # Check file was created
            plan_file = temp_dir / "2025-10-29" / "brain_plan_intraday.json"
            assert plan_file.exists()

            saved_plan = json.loads(plan_file.read_text())
            assert saved_plan["session"] == "intraday"

    def test_propose_plan_uses_today_when_no_date(self, temp_dir):
        """Test plan uses today's date when context missing date"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_plan = {"session": "overnight"}

        with patch.object(brain, '_ask', return_value=mock_plan):
            brain.propose_plan("overnight", {})

            # Should create directory with today's date (use actual date)
            from datetime import date
            today = date.today().isoformat()
            plan_file = temp_dir / today / "brain_plan_overnight.json"
            assert plan_file.exists()

    def test_propose_plan_with_memory_context(self, temp_dir):
        """Test plan includes memory context"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_plan = {
            "session": "morning",
            "providers": [{"name": "gemini", "symbols": ["AAPL", "TSLA"]}]
        }

        with patch.object(brain, '_ask', return_value=mock_plan) as mock_ask:
            context = {
                "date": "2025-10-29",
                "top_providers": [
                    {"provider": "gemini", "avg_sharpe": 1.35}
                ],
                "top_symbols": [
                    {"symbol": "AAPL", "provider": "gemini"}
                ]
            }
            brain.propose_plan("morning", context)

            # Verify memory context was included in prompt
            call_args = mock_ask.call_args[0][0]
            assert "top_providers" in call_args
            assert "top_symbols" in call_args


class TestControlBrainDecide:
    """Test ControlBrain.decide method"""

    def test_decide_start_morning_proceed(self, temp_dir):
        """Test morning start decision to proceed"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_decision = {"proceed": True, "notes": "All gates passed"}

        with patch.object(brain, '_ask', return_value=mock_decision):
            context = {"gates": {"health": True, "data": True, "exec": True}}
            result = brain.decide("start_morning", context)

            assert result["proceed"] is True
            assert "notes" in result

    def test_decide_start_morning_block(self, temp_dir):
        """Test morning start decision to block"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_decision = {"proceed": False, "notes": "Data gate failed"}

        with patch.object(brain, '_ask', return_value=mock_decision):
            context = {"gates": {"health": True, "data": False}}
            result = brain.decide("start_morning", context)

            assert result["proceed"] is False
            assert "Data gate failed" in result["notes"]

    def test_decide_hpo_deploy_apply(self, temp_dir):
        """Test HPO deploy decision to apply"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_decision = {
            "apply_params": True,
            "ab_weights": {"champion": 0.6, "challenger": 0.4},
            "notes": "Challenger outperforms"
        }

        with patch.object(brain, '_ask', return_value=mock_decision):
            context = {
                "best": {"oos_sharpe": 1.35},
                "gate": {"ok": True}
            }
            result = brain.decide("hpo_deploy", context)

            assert result["apply_params"] is True
            assert result["ab_weights"]["champion"] == 0.6
            assert result["ab_weights"]["challenger"] == 0.4

    def test_decide_hpo_deploy_reject(self, temp_dir):
        """Test HPO deploy decision to reject"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_decision = {"apply_params": False, "notes": "Insufficient evidence"}

        with patch.object(brain, '_ask', return_value=mock_decision):
            context = {
                "best": {"oos_sharpe": 0.95},
                "gate": {"ok": False, "reasons": ["oos_sharpe<1.1"]}
            }
            result = brain.decide("hpo_deploy", context)

            assert result["apply_params"] is False

    def test_decide_no_file_persistence(self, temp_dir):
        """Test decide does not save to file (caller's responsibility)"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_decision = {"proceed": True}

        with patch.object(brain, '_ask', return_value=mock_decision):
            brain.decide("start_morning", {"date": "2025-10-29"})

            # No decision file should be created
            decision_file = temp_dir / "2025-10-29" / "brain_decision_start_morning.json"
            assert not decision_file.exists()

    def test_decide_with_complex_context(self, temp_dir):
        """Test decide with complex context data"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_decision = {"apply_params": True, "ab_weights": {"champion": 0.7, "challenger": 0.3}}

        with patch.object(brain, '_ask', return_value=mock_decision) as mock_ask:
            context = {
                "best": {"oos_sharpe": 1.25, "oos_mdd": 0.08, "oos_trades": 45},
                "gate": {"ok": True, "metrics": {"oos_sharpe": 1.25}},
                "registry": {"champion": {"id": "v1", "sharpe": 1.15}},
                "memory": {"top_providers": [{"provider": "deepseek_nvidia"}]}
            }
            brain.decide("hpo_deploy", context)

            # Verify complex context was included in prompt
            call_args = mock_ask.call_args[0][0]
            assert "best" in call_args
            assert "gate" in call_args
            assert "registry" in call_args


class TestBrainCallsTracing:
    """Test brain_calls.jsonl tracing functionality"""

    def test_trace_enabled_by_default(self, temp_dir):
        """Test trace is enabled by default"""
        brain = ControlBrain(runs_dir=str(temp_dir))
        assert brain.trace_enabled is True

    def test_trace_write_creates_file(self, temp_dir):
        """Test _trace_write creates brain_calls.jsonl file"""
        from datetime import date
        brain = ControlBrain(runs_dir=str(temp_dir))

        record = {
            "ts": "2025-10-29T10:30:00",
            "kind": "propose_plan",
            "session": "morning",
            "provider": "deepseek_nvidia",
            "model": "deepseek-ai/deepseek-r1",
            "input_keys": ["health", "gates"],
            "output": {"action": "proceed"}
        }

        brain._trace_write(record)

        # Check file was created
        today = date.today().isoformat()
        trace_file = temp_dir / today / "brain_calls.jsonl"
        assert trace_file.exists()

        # Verify content
        content = trace_file.read_text()
        assert "propose_plan" in content
        assert "morning" in content

    def test_trace_write_disabled(self, temp_dir):
        """Test _trace_write does nothing when disabled"""
        brain = ControlBrain(runs_dir=str(temp_dir))
        brain.trace_enabled = False

        record = {"ts": "2025-10-29T10:30:00", "kind": "test"}
        brain._trace_write(record)

        # No file should be created
        assert not any(temp_dir.glob("*/brain_calls.jsonl"))

    def test_trace_write_handles_errors_gracefully(self, temp_dir):
        """Test _trace_write doesn't crash on errors"""
        brain = ControlBrain(runs_dir=str(temp_dir))

        # Try to write invalid data
        brain._trace_write({"invalid": lambda x: x})  # Non-serializable

        # Should not raise exception (best-effort)

    def test_propose_plan_writes_trace(self, temp_dir):
        """Test propose_plan writes to brain_calls.jsonl"""
        from datetime import date
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_plan = {
            "action": "proceed",
            "checks": ["health_gate", "data_gate"]
        }

        with patch.object(brain, '_ask', return_value=mock_plan):
            context = {"date": date.today().isoformat(), "health": {"ok": True}}
            brain.propose_plan("morning", context)

        # Verify trace file exists
        today = date.today().isoformat()
        trace_file = temp_dir / today / "brain_calls.jsonl"
        assert trace_file.exists()

        # Parse and verify trace record
        lines = trace_file.read_text().strip().split('\n')
        assert len(lines) == 1
        trace_record = json.loads(lines[0])

        assert trace_record["kind"] == "propose_plan"
        assert trace_record["session"] == "morning"
        assert trace_record["provider"] == "deepseek_nvidia"
        assert "health" in trace_record["input_keys"]
        assert trace_record["output"] == mock_plan

    def test_decide_writes_trace(self, temp_dir):
        """Test decide writes to brain_calls.jsonl"""
        from datetime import date
        brain = ControlBrain(runs_dir=str(temp_dir))

        mock_decision = {
            "verdict": "approve",
            "confidence": 0.85
        }

        with patch.object(brain, '_ask', return_value=mock_decision):
            context = {"best_sharpe": 1.25, "threshold": 1.0}
            brain.decide("hpo_deploy", context)

        # Verify trace file exists
        today = date.today().isoformat()
        trace_file = temp_dir / today / "brain_calls.jsonl"
        assert trace_file.exists()

        # Parse and verify trace record
        lines = trace_file.read_text().strip().split('\n')
        assert len(lines) == 1
        trace_record = json.loads(lines[0])

        assert trace_record["kind"] == "decide"
        assert trace_record["action"] == "hpo_deploy"
        assert "best_sharpe" in trace_record["input_keys"]
        assert trace_record["output"] == mock_decision

    def test_multiple_calls_append_to_trace(self, temp_dir):
        """Test multiple brain calls append to same file"""
        from datetime import date
        brain = ControlBrain(runs_dir=str(temp_dir))

        with patch.object(brain, '_ask', return_value={"action": "proceed"}):
            # Make multiple calls
            brain.propose_plan("morning", {"date": date.today().isoformat()})
            brain.decide("start_morning", {})
            brain.decide("hpo_deploy", {})

        # All three calls should be in the file
        today = date.today().isoformat()
        trace_file = temp_dir / today / "brain_calls.jsonl"
        lines = trace_file.read_text().strip().split('\n')

        assert len(lines) == 3

        records = [json.loads(line) for line in lines]
        assert records[0]["kind"] == "propose_plan"
        assert records[1]["kind"] == "decide"
        assert records[1]["action"] == "start_morning"
        assert records[2]["kind"] == "decide"
        assert records[2]["action"] == "hpo_deploy"

    def test_trace_includes_timestamp(self, temp_dir):
        """Test trace records include ISO timestamp"""
        from datetime import date
        brain = ControlBrain(runs_dir=str(temp_dir))

        with patch.object(brain, '_ask', return_value={}):
            brain.propose_plan("morning", {"date": date.today().isoformat()})

        today = date.today().isoformat()
        trace_file = temp_dir / today / "brain_calls.jsonl"
        trace_record = json.loads(trace_file.read_text())

        # Verify timestamp format
        assert "ts" in trace_record
        assert "T" in trace_record["ts"]  # ISO format includes T
        assert len(trace_record["ts"]) >= 19  # YYYY-MM-DDTHH:MM:SS
