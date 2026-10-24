"""Unit tests for controller modules"""
import subprocess
from unittest.mock import Mock, patch, call
import pytest

from scripts.ai_trader.controllers.llm_trader_controller import LLMTraderController
from scripts.ai_trader.controllers.run_system_controller import RunSystemController


class TestLLMTraderController:
    """Test LLMTraderController class"""

    def test_init_paper_mode(self):
        """Test initialization in paper mode"""
        cmds = {"premarket_fallback": "python llm_trader/run_bot.py"}
        controller = LLMTraderController(cmds, paper=True, paper_flag="--dry-run")

        assert controller.paper is True
        assert controller.paper_flag == "--dry-run"

    def test_start_premarket_paper_mode(self):
        """Test starting premarket in paper mode (uses fallback)"""
        cmds = {
            "premarket_sh": "./llm_trader/run_premarket.sh",
            "premarket_fallback": "python llm_trader/run_bot.py --provider deepseek"
        }
        controller = LLMTraderController(cmds, paper=True, paper_flag="--dry-run")

        with patch('subprocess.Popen') as mock_popen:
            controller.start_premarket()

            # Should use fallback in paper mode and append --dry-run
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert "python" in args
            assert "--dry-run" in args

    def test_start_rth_fast_non_paper(self):
        """Test starting RTH fast in non-paper mode (tries shell script)"""
        cmds = {
            "first_hour_sh": "./llm_trader/run_first_hour.sh",
            "first_hour_fallback": "python llm_trader/run_bot.py"
        }
        controller = LLMTraderController(cmds, paper=False)

        with patch('os.path.exists', return_value=True):
            with patch('subprocess.Popen') as mock_popen:
                controller.start_rth_fast()

                mock_popen.assert_called_once()

    def test_run_with_plan_single_provider(self):
        """Test running with a plan containing single provider"""
        cmds = {}
        controller = LLMTraderController(cmds, paper=True, paper_flag="--dry-run")

        plan = {
            "session": "morning",
            "providers": [
                {
                    "name": "deepseek_nvidia",
                    "symbols": ["TSLA", "AAPL"],
                    "interval_sec": 600,
                    "cycles": 30,
                    "wait_for": "premarket",
                    "close_at": "09:35"
                }
            ],
            "llm_trader_flags": {"forbid_new_before_min": 9}
        }

        with patch('subprocess.Popen') as mock_popen:
            controller.run_with_plan(plan)

            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]

            assert "python" in args
            assert "llm_trader/run_single_bot.py" in args[1]
            assert "--provider" in args
            assert "deepseek_nvidia" in args
            assert "--symbols" in args
            assert "TSLA" in args and "AAPL" in args
            assert "--interval" in args
            assert "600" in args
            assert "--wait-for-premarket" in args
            assert "--close-at" in args
            assert "09:35" in args
            assert "--forbid-new-positions-before" in args
            assert "9" in args
            assert "--dry-run" in args  # Paper flag

    def test_run_with_plan_multiple_providers(self):
        """Test running with multiple providers in plan"""
        cmds = {}
        controller = LLMTraderController(cmds, paper=False)

        plan = {
            "providers": [
                {"name": "openai", "symbols": ["TSLA"], "interval_sec": 60},
                {"name": "gemini", "symbols": ["AAPL"], "interval_sec": 120}
            ]
        }

        with patch('subprocess.Popen') as mock_popen:
            controller.run_with_plan(plan)

            # Should spawn 2 processes
            assert mock_popen.call_count == 2

    def test_run_with_plan_empty_providers(self):
        """Test running with plan containing no providers"""
        cmds = {}
        controller = LLMTraderController(cmds, paper=False)

        plan = {"providers": []}

        with patch('subprocess.Popen') as mock_popen:
            controller.run_with_plan(plan)

            # Should not spawn any processes
            mock_popen.assert_not_called()

    def test_run_with_plan_wait_for_variations(self):
        """Test different wait_for values"""
        cmds = {}
        controller = LLMTraderController(cmds, paper=False)

        test_cases = [
            ("market", "--wait-for-market"),
            ("afterhours", "--wait-for-afterhours"),
            ("overnight", "--wait-for-overnight"),
            ("invalid", None)  # Should not add any flag
        ]

        for wait_for, expected_flag in test_cases:
            plan = {
                "providers": [
                    {"name": "openai", "symbols": ["TSLA"], "wait_for": wait_for}
                ]
            }

            with patch('subprocess.Popen') as mock_popen:
                controller.run_with_plan(plan)

                args = mock_popen.call_args[0][0]
                if expected_flag:
                    assert expected_flag in args
                else:
                    assert not any("--wait-for" in str(a) for a in args)


class TestRunSystemController:
    """Test RunSystemController class"""

    def test_backfill_yesterday(self):
        """Test triggering yesterday's backfill"""
        cmds = {"backfill_yesterday": "python docs/daily/trigger_backfill.py"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_call') as mock_call:
            # Use datetime module directly for proper mocking
            import datetime as dt
            mock_date = dt.date(2025, 10, 29)
            yesterday = dt.date(2025, 10, 28)

            with patch('datetime.datetime') as mock_dt:
                mock_now = Mock()
                mock_now.__sub__ = lambda self, other: yesterday if isinstance(other, dt.timedelta) else mock_now
                mock_now.strftime = Mock(return_value="2025-10-28")
                mock_dt.now.return_value = mock_now

                with patch('datetime.timedelta', dt.timedelta):
                    controller.backfill_yesterday()

                mock_call.assert_called_once()
                args = mock_call.call_args[0][0]
                assert "python" in args
                assert "trigger_backfill.py" in ' '.join(args)
                assert "--date" in args

    def test_backfill_yesterday_with_symbols(self):
        """Test backfilling specific symbols"""
        cmds = {"backfill_yesterday": "python docs/daily/trigger_backfill.py"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_call') as mock_call:
            controller.backfill_yesterday(symbols=["TSLA", "AAPL"])

            args = mock_call.call_args[0][0]
            assert "--symbols" in args
            assert "TSLA" in args
            assert "AAPL" in args

    def test_alpha_loop(self):
        """Test running alpha loop"""
        cmds = {"alpha_loop": "python alpha_loop.py --gens 2"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_call') as mock_call:
            controller.alpha_loop()

            mock_call.assert_called_once()
            args = mock_call.call_args[0][0]
            assert "python" in args
            assert "alpha_loop.py" in args

    def test_apply_params_with_hpo_params(self):
        """Test applying HPO parameters"""
        cmds = {"apply_params": "python apply_extras_params.py"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_call') as mock_call:
            controller.apply_params(hpo_params_path="/tmp/best_params.json")

            mock_call.assert_called_once()
            call_args = mock_call.call_args[0][0]
            assert "python" in call_args[0] or "apply_extras_params.py" in call_args[0]
            assert "--hpo-params" in call_args
            assert "/tmp/best_params.json" in call_args

    def test_apply_params_all_options(self):
        """Test applying params with all options"""
        cmds = {"apply_params": "python apply_extras_params.py"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_call') as mock_call:
            controller.apply_params(
                hpo_params_path="/tmp/params.json",
                strategy_config="/tmp/strategy.yml",
                output_path="/tmp/output.yml"
            )

            call_args = mock_call.call_args[0][0]
            assert "--hpo-params" in call_args
            assert "--strategy-config" in call_args
            assert "--output" in call_args

    def test_apply_params_override_strategy_id(self):
        """Test strategy_id override in apply_extras_params command."""
        cmds = {"apply_params": "python apply_extras_params.py --config-yaml config.yml --strategy-id enhanced_ml_001"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_call') as mock_call:
            controller.apply_params(hpo_params_path="/tmp/params.json", strategy_id="qqq_intraday_001")

            call_args = mock_call.call_args[0][0]
            assert "--strategy-id" in call_args
            idx = call_args.index("--strategy-id")
            assert call_args[idx + 1] == "qqq_intraday_001"

    def test_apply_params_cmd_override(self):
        """Test command override for per-strategy apply routing."""
        cmds = {"apply_params": "python apply_extras_params.py --strategy-id enhanced_ml_001"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_call') as mock_call:
            controller.apply_params(
                hpo_params_path="/tmp/params.json",
                cmd_override="python custom_apply.py --foo bar",
                strategy_id="qqq_intraday_001",
            )

            call_args = mock_call.call_args[0][0]
            # custom command should be respected (no strategy flag injection for unknown command)
            assert "custom_apply.py" in " ".join(call_args)
            assert "--strategy-id" not in call_args

    def test_rl_train(self):
        """Test RL training"""
        cmds = {"rl_train": "python rl_core/train.py --timesteps 10000"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_call') as mock_call:
            controller.rl_train()

            mock_call.assert_called_once()

    def test_rl_train_missing_command(self):
        """Test RL training when command not configured"""
        cmds = {}
        controller = RunSystemController(cmds)

        # Should not raise exception
        controller.rl_train()

    def test_rl_eval_success(self):
        """Test RL evaluation returning results"""
        cmds = {"rl_eval": "python scripts/ai_trader/rl/eval_rl.py"}
        controller = RunSystemController(cmds)

        mock_output = json.dumps({
            "gate": {"ok": True},
            "metrics": {"sharpe": 1.15, "mdd": 0.09}
        })

        with patch('subprocess.check_output', return_value=mock_output):
            result = controller.rl_eval(min_sharpe=1.0, max_mdd=0.20)

            assert result is not None
            assert result["gate"]["ok"] is True
            assert result["metrics"]["sharpe"] == 1.15

    def test_rl_eval_with_params(self):
        """Test RL eval passes parameters correctly"""
        cmds = {"rl_eval": "python scripts/ai_trader/rl/eval_rl.py"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_output', return_value="{}") as mock_call:
            controller.rl_eval(min_sharpe=1.5, max_mdd=0.15)

            args = mock_call.call_args[0][0]
            assert "--min_sharpe" in args
            assert "1.5" in args
            assert "--max_mdd" in args
            assert "0.15" in args

    def test_rl_eval_error(self):
        """Test RL eval error handling"""
        cmds = {"rl_eval": "python scripts/ai_trader/rl/eval_rl.py"}
        controller = RunSystemController(cmds)

        with patch('subprocess.check_output', side_effect=subprocess.CalledProcessError(1, "python")):
            result = controller.rl_eval()

            assert result is not None
            assert "error" in result


# Add import statement at top of file
import json
