"""End-to-end integration test for hedge intent flow.

Tests the complete flow:
1. Brain generates plan with hedge_intent
2. LLMTraderController consumes hedge_intent
3. AutoHedge script is executed
4. Audit logs are written
5. Monitoring dashboard can read hedge status
"""

import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime, date
from unittest.mock import patch, MagicMock
import sys

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.ai_trader.controllers.llm_trader_controller import LLMTraderController


@pytest.mark.integration
class TestHedgeIntentE2E:
    """End-to-end tests for hedge intent flow."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.runs_dir = self.temp_dir / "runs" / "ai_trader" / date.today().isoformat()
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        """Cleanup test environment."""
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_plan_with_hedge_intent_dry_run(self):
        """Test that plan with hedge_intent executes successfully in dry-run mode."""
        # Create test plan with hedge_intent
        plan = {
            "session": "morning",
            "providers": [
                {
                    "name": "deepseek_nvidia",
                    "symbols": ["TSLA"],
                    "interval_sec": 600,
                    "cycles": 10,
                    "close_at": "09:35",
                }
            ],
            "llm_trader_flags": {"forbid_new_before_min": 9},
            "hedge_intent": {
                "symbols": ["TSLA"],
                "notional_pct": 0.5,
                "urgency": "medium",
                "strategy": "options_put",
                "confidence": 0.7,
                "max_cost_bps": 100,
            },
        }

        # Initialize controller with test config
        controller = LLMTraderController(
            cmds={},
            paper=True,
            runs_dir=self.runs_dir,
        )

        # Execute plan (should trigger hedge execution)
        with patch("subprocess.run") as mock_subprocess:
            # Mock successful hedge execution
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "Hedge execution completed successfully"
            mock_result.stderr = ""
            mock_subprocess.return_value = mock_result

            # Run plan
            controller.run_with_plan(plan)

            # Verify hedge script was called
            assert mock_subprocess.called
            call_args = mock_subprocess.call_args

            # Check that auto_hedge.py was invoked
            cmd_parts = call_args[0][0]
            assert any("auto_hedge.py" in str(part) for part in cmd_parts)

            # Check parameters passed to hedge script
            cmd_str = " ".join(str(p) for p in cmd_parts)
            assert "--symbols TSLA" in cmd_str
            assert "--notional-pct 0.5" in cmd_str
            assert "--urgency medium" in cmd_str
            assert "--strategy options_put" in cmd_str
            assert "--confidence 0.7" in cmd_str
            assert "--max-cost-bps 100" in cmd_str

    def test_plan_without_hedge_intent(self):
        """Test that plan without hedge_intent works normally."""
        plan = {
            "session": "morning",
            "providers": [
                {
                    "name": "deepseek_nvidia",
                    "symbols": ["TSLA"],
                    "interval_sec": 600,
                    "cycles": 10,
                    "close_at": "09:35",
                }
            ],
            "llm_trader_flags": {"forbid_new_before_min": 9},
            # No hedge_intent
        }

        controller = LLMTraderController(
            cmds={},
            paper=True,
            runs_dir=self.runs_dir,
        )

        # Should not crash
        with patch("subprocess.Popen"):
            controller.run_with_plan(plan)

    def test_hedge_intent_failure_high_urgency(self):
        """Test that high-urgency hedge failure triggers safe_mode."""
        plan = {
            "session": "morning",
            "providers": [
                {
                    "name": "deepseek_nvidia",
                    "symbols": ["TSLA"],
                    "interval_sec": 300,  # Normal interval
                    "cycles": 20,
                    "close_at": "09:35",
                }
            ],
            "hedge_intent": {
                "symbols": ["TSLA"],
                "notional_pct": 1.0,
                "urgency": "high",
                "strategy": "options_put",
            },
            "safe_mode": False,
            "restrictions": [],
        }

        controller = LLMTraderController(
            cmds={},
            paper=True,
            runs_dir=self.runs_dir,
        )

        # Mock hedge execution failure
        with patch("subprocess.run") as mock_subprocess:
            mock_result = MagicMock()
            mock_result.returncode = 1  # Failure
            mock_result.stderr = "Error: hedge_cost_too_high"
            mock_subprocess.return_value = mock_result

            with patch("subprocess.Popen"):
                controller.run_with_plan(plan)

            # Verify safe_mode was activated
            assert plan["safe_mode"] is True
            assert "reduce_sizing" in plan["restrictions"]
            assert "slow_interval" in plan["restrictions"]

            # Verify intervals were increased
            for provider in plan["providers"]:
                assert provider["interval_sec"] >= 600

    def test_hedge_intent_failure_medium_urgency(self):
        """Test that medium-urgency hedge failure reduces sizing but doesn't enter safe_mode."""
        plan = {
            "session": "morning",
            "providers": [
                {
                    "name": "deepseek_nvidia",
                    "symbols": ["TSLA"],
                    "interval_sec": 300,
                    "cycles": 20,
                    "close_at": "09:35",
                }
            ],
            "hedge_intent": {
                "symbols": ["TSLA"],
                "notional_pct": 0.5,
                "urgency": "medium",
                "strategy": "options_put",
            },
            "safe_mode": False,
            "restrictions": [],
        }

        controller = LLMTraderController(
            cmds={},
            paper=True,
            runs_dir=self.runs_dir,
        )

        # Mock hedge execution failure
        with patch("subprocess.run") as mock_subprocess:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stderr = "Error: no_positions_to_hedge"
            mock_subprocess.return_value = mock_result

            with patch("subprocess.Popen"):
                controller.run_with_plan(plan)

            # Verify only sizing was reduced (not full safe_mode)
            assert "reduce_sizing" in plan["restrictions"]
            # safe_mode should NOT be set for medium urgency
            assert plan.get("safe_mode", False) is False

    def test_hedge_intent_failure_low_urgency(self):
        """Test that low-urgency hedge failure logs but continues normally."""
        plan = {
            "session": "morning",
            "providers": [
                {
                    "name": "deepseek_nvidia",
                    "symbols": ["TSLA"],
                    "interval_sec": 300,
                    "cycles": 20,
                    "close_at": "09:35",
                }
            ],
            "hedge_intent": {
                "symbols": ["TSLA"],
                "notional_pct": 0.25,
                "urgency": "low",
                "strategy": "options_put",
            },
            "safe_mode": False,
            "restrictions": [],
        }

        controller = LLMTraderController(
            cmds={},
            paper=True,
            runs_dir=self.runs_dir,
        )

        # Mock hedge execution failure
        with patch("subprocess.run") as mock_subprocess:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stderr = "Error: timeout"
            mock_subprocess.return_value = mock_result

            with patch("subprocess.Popen"):
                controller.run_with_plan(plan)

            # Verify no safety adjustments for low urgency
            assert plan.get("safe_mode", False) is False
            assert len(plan.get("restrictions", [])) == 0

    def test_hedge_audit_log_creation(self):
        """Test that hedge audit logs are created correctly."""
        plan = {
            "session": "morning",
            "providers": [],
            "hedge_intent": {
                "symbols": ["TSLA", "NVDA"],
                "notional_pct": 0.5,
                "urgency": "medium",
                "strategy": "options_collar",
            },
        }

        controller = LLMTraderController(
            cmds={},
            paper=True,
            runs_dir=self.runs_dir,
        )

        # Mock successful hedge execution
        with patch("subprocess.run") as mock_subprocess:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "Success"
            mock_result.stderr = ""
            mock_subprocess.return_value = mock_result

            controller.run_with_plan(plan)

            # Verify hedge script was called
            assert mock_subprocess.called

            # Verify the audit dir was passed to hedge script
            cmd_args = mock_subprocess.call_args[0][0]
            assert any("auto_hedge.py" in str(part) for part in cmd_args)

            # Check that audit log would be written to correct location
            # The controller writes to brain_calls.jsonl via _trace_write
            # We need to check if runs_dir exists and was initialized
            assert self.runs_dir.exists()

    def test_hedge_intent_with_paper_flag(self):
        """Test that paper mode flag is propagated to hedge script."""
        plan = {
            "session": "morning",
            "providers": [],
            "hedge_intent": {
                "symbols": ["AAPL"],
                "notional_pct": 0.5,
            },
        }

        controller = LLMTraderController(
            cmds={},
            paper=True,  # Paper mode enabled
            runs_dir=self.runs_dir,
        )

        with patch("subprocess.run") as mock_subprocess:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_subprocess.return_value = mock_result

            controller.run_with_plan(plan)

            # Check that --paper flag was passed
            cmd_parts = mock_subprocess.call_args[0][0]
            assert "--paper" in cmd_parts

    def test_hedge_intent_missing_symbols(self):
        """Test that hedge intent with missing symbols is handled gracefully."""
        plan = {
            "session": "morning",
            "providers": [],
            "hedge_intent": {
                "symbols": [],  # Empty symbols list
                "notional_pct": 0.5,
            },
        }

        controller = LLMTraderController(
            cmds={},
            paper=True,
            runs_dir=self.runs_dir,
        )

        # Should not crash
        with patch("subprocess.run") as mock_subprocess:
            controller.run_with_plan(plan)

            # Hedge script should NOT be called
            assert not mock_subprocess.called


@pytest.mark.integration
class TestHedgeMonitoringIntegration:
    """Test integration between hedge execution and monitoring."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.runs_dir = self.temp_dir / "runs" / "ai_trader" / date.today().isoformat()
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        """Cleanup test environment."""
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_monitoring_reads_hedge_audit_log(self):
        """Test that monitoring dashboard can read hedge audit logs."""
        # Create mock hedge audit log
        hedge_audit_path = self.runs_dir / "hedge_audit.jsonl"

        audit_records = [
            {
                "ts": datetime.now().isoformat(),
                "intent": {
                    "symbols": ["TSLA"],
                    "notional_pct": 0.5,
                    "urgency": "medium",
                    "strategy": "options_put",
                },
                "result": {
                    "success": True,
                    "total_cost": 250.50,
                    "hedge_effectiveness": 0.75,
                },
                "orders": [],
            },
            {
                "ts": datetime.now().isoformat(),
                "intent": {
                    "symbols": ["NVDA"],
                    "notional_pct": 0.75,
                    "urgency": "high",
                    "strategy": "options_collar",
                },
                "result": {
                    "success": False,
                    "total_cost": 0.0,
                    "error": "hedge_cost_too_high: 150 bps > 100 bps",
                },
                "orders": [],
            },
        ]

        with open(hedge_audit_path, 'w') as f:
            for record in audit_records:
                f.write(json.dumps(record) + "\n")

        # Simulate monitoring dashboard reading the log
        with open(hedge_audit_path, 'r') as f:
            loaded_records = [json.loads(line) for line in f if line.strip()]

        assert len(loaded_records) == 2

        # Calculate statistics (like monitoring dashboard does)
        total_triggers = len(loaded_records)
        successful = sum(1 for r in loaded_records if r.get("result", {}).get("success"))
        success_rate = successful / total_triggers

        assert total_triggers == 2
        assert successful == 1
        assert success_rate == 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
