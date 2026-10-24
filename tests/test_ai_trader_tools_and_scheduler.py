
import unittest
import json
import os
import sys
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, ANY

# Ensure the project root is in the sys.path for imports to work
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Now we can import the modules we want to test
from scripts.ai_trader.tools import review_ml_job
from scripts.ai_trader import ml_scheduler

class TestReviewMlJob(unittest.TestCase):
    def setUp(self):
        """Set up a temporary directory and mock run files."""
        self.temp_dir = tempfile.mkdtemp(prefix="test_review_ml_job_")
        self.runs_dir = Path(self.temp_dir)
        self.today_dir = self.runs_dir / "2025-11-17"
        self.oof_eval_dir = self.today_dir / "oof_eval"
        self.oof_eval_dir.mkdir(parents=True)

        self.job_id = "20251117080000-testoof"
        self.hpo_job_id = "20251117090000-testhpo"

        # Mock ml_events.jsonl
        events = [
            {"event": "ml.job", "status": "completed", "job_id": self.job_id, "job_type": "oof", "ok": True,
             "metrics": {"fusion_auc": 0.56, "fusion_ic": 0.04}, "params": {"symbols": ["TSLA"]}, "duration_sec": 120},
            {"event": "ml.job", "status": "completed", "job_id": self.hpo_job_id, "job_type": "hpo_cli", "ok": True,
             "metrics": {
                 "sharpe": 1.5, "win_rate": 0.58, "trades": 150, "pnl": 12345.6, "mdd": 0.05,
                 "best_params": {"A1_thr": 0.5, "k_atr": 2.0}
             }, "params": {"n_trials": 50}, "duration_sec": 3600},
        ]
        with open(self.today_dir / "ml_events.jsonl", "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        # Mock oof_eval file
        self.oof_metrics = {"per_symbol": {"TSLA": {"auc": 0.57, "ic": 0.045, "coverage": 0.99}}}
        with open(self.oof_eval_dir / f"oof_results_{self.job_id}.json", "w") as f:
            json.dump(self.oof_metrics, f)

    def tearDown(self):
        """Clean up the temporary directory."""
        shutil.rmtree(self.temp_dir)

    @patch('scripts.ai_trader.brain.ControlBrain')
    def test_review_oof_job_success(self, MockControlBrain):
        """Test reviewing a completed OOF job."""
        # Arrange
        mock_brain_instance = MockControlBrain.return_value
        mock_review = {"summary": "OOF job looks promising", "next_action": "do_nothing"}
        mock_brain_instance.review_ml.return_value = mock_review

        output_file = self.today_dir / f"review_test_{self.job_id}.json"

        # Act
        args = [
            "--job-id", self.job_id,
            "--runs-dir", str(self.runs_dir),
            "--output", str(output_file),
            "--days", "1"
        ]
        review_ml_job.main(args)

        # Assert
        # 1. Brain was called with the correct context
        mock_brain_instance.review_ml.assert_called_once()
        call_args, _ = mock_brain_instance.review_ml.call_args
        context = call_args[0]

        self.assertEqual(context['job']['job_id'], self.job_id)
        self.assertEqual(context['job']['job_type'], 'oof')
        self.assertEqual(context['job']['metrics']['fusion_ic'], 0.04)
        self.assertIn('oof_details', context)
        self.assertEqual(context['oof_details']['per_symbol']['TSLA']['auc'], 0.57)

        # 2. Output file was written with the brain's response
        self.assertTrue(output_file.exists())
        with open(output_file, "r") as f:
            written_data = json.load(f)
        self.assertEqual(written_data['review'], mock_review)
        self.assertEqual(written_data['job_id'], self.job_id)

    @patch('scripts.ai_trader.brain.ControlBrain')
    def test_review_hpo_job_success(self, MockControlBrain):
        """Test reviewing a completed HPO job with projected metrics."""
        # Arrange
        mock_brain_instance = MockControlBrain.return_value
        mock_review = {"summary": "HPO run was profitable", "next_action": "apply_config"}
        mock_brain_instance.review_ml.return_value = mock_review

        output_file = self.today_dir / f"review_test_{self.hpo_job_id}.json"

        # Act
        args = [
            "--job-id", self.hpo_job_id,
            "--runs-dir", str(self.runs_dir),
            "--output", str(output_file),
            "--days", "1"
        ]
        review_ml_job.main(args)

        # Assert
        # 1. Brain was called with the correct context
        mock_brain_instance.review_ml.assert_called_once()
        call_args, _ = mock_brain_instance.review_ml.call_args
        context = call_args[0]

        self.assertEqual(context['job']['job_id'], self.hpo_job_id)
        self.assertEqual(context['job']['job_type'], 'hpo_cli')
        self.assertEqual(context['job']['metrics']['sharpe'], 1.5)
        self.assertEqual(context['job']['metrics']['win_rate'], 0.58)
        self.assertEqual(context['job']['metrics']['trades'], 150)
        self.assertNotIn('oof_details', context) # Should not have OOF details

        # 2. Output file was written
        self.assertTrue(output_file.exists())
        with open(output_file, "r") as f:
            written_data = json.load(f)
        self.assertEqual(written_data['review'], mock_review)


class TestHpoMetricsProjection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_hpo_metrics_")
        self.hpo_output_dir = Path(self.temp_dir) / "hpo_cli_12345"
        self.hpo_output_dir.mkdir()

        # This is the file the scheduler will look for
        self.best_params_file = self.hpo_output_dir / "alpha_loop_best_params.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    @patch('subprocess.run')
    async def test_hpo_metrics_are_projected(self, mock_subprocess_run):
        """Verify that Sharpe, WinRate etc. are projected from best_params.json into the result metrics."""
        # Arrange
        # 1. Mock the output of the hpo_cli script
        best_params_content = {
            "sharpe": 2.1,
            "sortino": 3.5,
            "total_return": 0.15,
            "pnl": 5432.1,
            "max_drawdown": 0.08, # will be mapped to 'mdd'
            "win_rate": 0.62,
            "trades": 210,
            "profit_factor": 1.8,
            "params": {"x": 1, "y": "a"},
            "oos_summary": {"oos_avg_sharpe": 1.9}
        }

        # This function simulates the hpo_cli script creating its output file
        def create_mock_output(*args, **kwargs):
            cmd = args[0] if args else []
            cwd = Path(kwargs.get("cwd") or ".")
            study_name = "alpha_loop"
            try:
                if "--study-name" in cmd:
                    i = cmd.index("--study-name")
                    if i + 1 < len(cmd):
                        study_name = str(cmd[i + 1])
            except Exception:
                pass
            out_file = cwd / f"{study_name}_best_params.json"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            with open(out_file, "w") as f:
                json.dump(best_params_content, f)
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_subprocess_run.side_effect = create_mock_output

        # 2. Prepare the job for the scheduler function
        job = {
            "job_id": "hpo_job_1",
            "job_type": "hpo_cli",
            "params": {
                "config": "config/hpo_test.json",
                "n_trials": 10,
                "study_name": "alpha_loop" # This is used to find the output file
            }
        }

        # Act
        # We test the internal function directly
        result = await ml_scheduler._execute_hpo_cli(job['job_id'], job['params'])

        # Assert
        self.assertTrue(result['ok'])
        metrics = result['metrics']

        # Check that key metrics were projected to the top level
        self.assertAlmostEqual(metrics['sharpe'], 2.1)
        self.assertAlmostEqual(metrics['sortino'], 3.5)
        self.assertAlmostEqual(metrics['total_return'], 0.15)
        self.assertAlmostEqual(metrics['pnl'], 5432.1)
        self.assertAlmostEqual(metrics['mdd'], 0.08) # Check mapping from max_drawdown
        self.assertAlmostEqual(metrics['win_rate'], 0.62)
        self.assertEqual(metrics['trades'], 210)
        self.assertAlmostEqual(metrics['profit_factor'], 1.8)

        # Check that the original best_params are still nested
        self.assertIn('best_params', metrics)
        self.assertEqual(metrics['best_params']['sharpe'], 2.1)
        self.assertEqual(metrics['best_params']['params'], {"x": 1, "y": "a"})

        # Check that a metric not in the projection list is NOT at the top level
        self.assertNotIn('oos_summary', metrics)
        self.assertIn('oos_summary', metrics['best_params'])


class TestStrategyExperimentExecution(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_strategy_experiment_")
        self.runs_dir = Path(self.temp_dir) / "runs" / "2026-04-04"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _job(self, **overrides):
        job = {
            "job_id": "strategy_job_1",
            "job_type": "strategy_experiment",
            "params": {
                "market": "stocks",
                "strategy_family": "intraday_scalp",
                "engine": {
                    "adapter": "stocks_kernel_backtest",
                    "mode": "backtest",
                },
                "strategy_ref": {
                    "template_id": "qqq_intraday_core",
                },
                "command": "echo strategy_experiment",
                "acceptance": {
                    "min_trades": 1,
                    "max_drawdown_pct": 5.0,
                },
            },
        }
        job["params"].update(overrides)
        return job

    @patch.object(ml_scheduler, "_publish_ml_event", return_value=True)
    @patch.object(ml_scheduler, "mark_job_status", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "fail_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "block_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "complete_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "run_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "create_experiment")
    @patch.object(ml_scheduler, "_run_strategy_experiment_robustness_hook")
    @patch.object(ml_scheduler, "_run_strategy_experiment_adapter")
    @patch.object(ml_scheduler, "_runs_dir")
    async def test_strategy_experiment_success_persists_result_and_robustness(
        self,
        mock_runs_dir,
        mock_adapter,
        mock_robustness,
        mock_create_experiment,
        mock_run_experiment,
        mock_complete_experiment,
        mock_block_experiment,
        mock_fail_experiment,
        mock_mark_job_status,
        mock_publish_ml_event,
    ):
        mock_runs_dir.return_value = self.runs_dir
        mock_create_experiment.side_effect = lambda config: config["experiment_id"] if isinstance(config, dict) else str(config)
        mock_adapter.return_value = {
            "ok": True,
            "status": "COMPLETED",
            "artifacts": ["runs/ai_trader/research/trades.csv"],
            "summary_metrics": {
                "trades": 8,
                "win_rate": 0.75,
                "net_pnl": 12.5,
                "sharpe": 1.4,
                "max_drawdown_pct": 1.1,
            },
            "quality_flags": {"data_ok": True},
            "execution_reality": {"modeled_slippage_bps": 7.5},
        }
        mock_robustness.return_value = {
            "ok": True,
            "status": "COMPLETED",
            "wf_pass": True,
            "embargo_pass": True,
            "leakage_score": 0.01,
            "regime_stability": 0.9,
            "symbol_transfer_score": 0.7,
        }

        result = await ml_scheduler.execute_job(self._job())

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["metrics"]["trades"], 8)
        self.assertTrue(result["metrics"]["robustness_pass"])
        self.assertTrue(result["metrics"]["passed_acceptance"])
        self.assertTrue(result["experiment_result"]["robustness"]["ok"])
        self.assertTrue(result["experiment_result"]["robustness"]["wf_pass"])
        self.assertTrue(result["experiment_result"]["robustness"]["embargo_pass"])
        self.assertIsNotNone(result["experiment_result"]["robustness"]["leakage_score"])
        self.assertIsNotNone(result["experiment_result"]["robustness"]["regime_stability"])

        experiment_id = result["experiment_result"]["experiment_id"]
        self.assertNotEqual(experiment_id, self._job()["job_id"])
        result_path = self.runs_dir / "strategy_experiments" / experiment_id / "experiment_result.json"
        self.assertTrue(result_path.exists())
        with result_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["summary_metrics"]["sharpe"], 1.4)
        self.assertTrue(payload["robustness"]["ok"])
        self.assertTrue(payload["robustness"]["wf_pass"])
        self.assertTrue(payload["robustness"]["embargo_pass"])

        mock_create_experiment.assert_called_once()
        self.assertEqual(mock_create_experiment.call_args.args[0]["experiment_id"], experiment_id)
        self.assertGreaterEqual(mock_run_experiment.call_count, 3)
        mock_complete_experiment.assert_called_once()
        mock_block_experiment.assert_not_called()
        mock_fail_experiment.assert_not_called()
        self.assertTrue(mock_mark_job_status.called)
        self.assertTrue(mock_publish_ml_event.called)

    @patch.object(ml_scheduler, "_publish_ml_event", return_value=True)
    @patch.object(ml_scheduler, "mark_job_status", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "fail_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "block_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "complete_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "run_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "create_experiment")
    @patch.object(ml_scheduler, "_run_strategy_experiment_robustness_hook")
    @patch.object(ml_scheduler, "_run_strategy_experiment_adapter")
    @patch.object(ml_scheduler, "_runs_dir")
    async def test_strategy_experiment_validation_failure_persists_blocked_result(
        self,
        mock_runs_dir,
        mock_adapter,
        mock_robustness,
        mock_create_experiment,
        mock_run_experiment,
        mock_complete_experiment,
        mock_block_experiment,
        mock_fail_experiment,
        mock_mark_job_status,
        mock_publish_ml_event,
    ):
        mock_runs_dir.return_value = self.runs_dir
        mock_create_experiment.side_effect = lambda config: config["experiment_id"] if isinstance(config, dict) else str(config)
        mock_adapter.return_value = {
            "ok": True,
            "status": "COMPLETED",
            "artifacts": [],
            "summary_metrics": {"trades": 0, "net_pnl": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0},
            "quality_flags": {"data_ok": True},
        }
        mock_robustness.return_value = {
            "ok": True,
            "status": "COMPLETED",
            "wf_pass": True,
            "embargo_pass": True,
            "leakage_score": 0.01,
            "regime_stability": 0.9,
            "symbol_transfer_score": 0.7,
        }

        bad_job = self._job(market="", command=None)
        bad_job["params"].pop("market", None)
        bad_job["params"].pop("command", None)
        bad_job["params"]["engine"]["adapter"] = ""

        result = await ml_scheduler.execute_job(bad_job)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_market", result["error"])
        self.assertIn("missing_runner_hint", result["error"])
        self.assertEqual(result["experiment_result"]["status"], "blocked")
        self.assertFalse(result["experiment_result"]["robustness"]["ok"])
        self.assertEqual(result["experiment_result"]["robustness"]["status"], "BLOCKED")
        self.assertTrue(result["experiment_result"]["robustness"]["skipped"])
        self.assertFalse(result["experiment_result"]["robustness"]["wf_pass"])
        self.assertFalse(result["experiment_result"]["robustness"]["embargo_pass"])
        self.assertIsNotNone(result["experiment_result"]["robustness"]["leakage_score"])
        self.assertIsNotNone(result["experiment_result"]["robustness"]["regime_stability"])

        experiment_id = result["experiment_result"]["experiment_id"]
        result_path = self.runs_dir / "strategy_experiments" / experiment_id / "experiment_result.json"
        self.assertTrue(result_path.exists())

        mock_adapter.assert_not_called()
        mock_robustness.assert_not_called()
        mock_create_experiment.assert_called_once()
        self.assertEqual(mock_create_experiment.call_args.args[0]["experiment_id"], experiment_id)
        mock_block_experiment.assert_called_once()
        mock_complete_experiment.assert_not_called()
        mock_fail_experiment.assert_not_called()
        self.assertTrue(mock_mark_job_status.called)
        self.assertTrue(mock_publish_ml_event.called)

    @patch.object(ml_scheduler, "_publish_ml_event", return_value=True)
    @patch.object(ml_scheduler, "mark_job_status", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "fail_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "block_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "complete_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "run_experiment", return_value=None)
    @patch.object(ml_scheduler.experiment_api, "create_experiment")
    @patch.object(ml_scheduler, "_run_strategy_experiment_robustness_hook")
    @patch.object(ml_scheduler, "_run_strategy_experiment_adapter")
    @patch.object(ml_scheduler, "_runs_dir")
    async def test_strategy_experiment_same_spec_reuses_stable_experiment_id_and_path(
        self,
        mock_runs_dir,
        mock_adapter,
        mock_robustness,
        mock_create_experiment,
        mock_run_experiment,
        mock_complete_experiment,
        mock_block_experiment,
        mock_fail_experiment,
        mock_mark_job_status,
        mock_publish_ml_event,
    ):
        mock_runs_dir.return_value = self.runs_dir
        mock_create_experiment.side_effect = lambda config: config["experiment_id"] if isinstance(config, dict) else str(config)
        mock_adapter.side_effect = [
            {
                "ok": True,
                "status": "COMPLETED",
                "artifacts": ["runs/ai_trader/research/first.csv"],
                "summary_metrics": {
                    "trades": 5,
                    "win_rate": 0.6,
                    "net_pnl": 9.0,
                    "sharpe": 1.1,
                    "max_drawdown_pct": 1.0,
                },
                "quality_flags": {"data_ok": True},
                "execution_reality": {"modeled_slippage_bps": 6.0},
            },
            {
                "ok": True,
                "status": "COMPLETED",
                "artifacts": ["runs/ai_trader/research/second.csv"],
                "summary_metrics": {
                    "trades": 7,
                    "win_rate": 0.8,
                    "net_pnl": 17.0,
                    "sharpe": 1.9,
                    "max_drawdown_pct": 0.8,
                },
                "quality_flags": {"data_ok": True},
                "execution_reality": {"modeled_slippage_bps": 4.5},
            },
        ]
        mock_robustness.side_effect = [
            {
                "ok": True,
                "status": "COMPLETED",
                "wf_pass": True,
                "embargo_pass": True,
                "leakage_score": 0.01,
                "regime_stability": 0.9,
                "symbol_transfer_score": 0.7,
            },
            {
                "ok": True,
                "status": "COMPLETED",
                "wf_pass": True,
                "embargo_pass": True,
                "leakage_score": 0.01,
                "regime_stability": 0.9,
                "symbol_transfer_score": 0.7,
            },
        ]

        first_job = self._job()
        first_job["job_id"] = "strategy_job_a"
        second_job = self._job()
        second_job["job_id"] = "strategy_job_b"

        first_result = await ml_scheduler.execute_job(first_job)
        second_result = await ml_scheduler.execute_job(second_job)

        self.assertTrue(first_result["ok"])
        self.assertTrue(second_result["ok"])
        self.assertEqual(first_result["experiment_result"]["experiment_id"], second_result["experiment_result"]["experiment_id"])

        experiment_id = first_result["experiment_result"]["experiment_id"]
        result_path = self.runs_dir / "strategy_experiments" / experiment_id / "experiment_result.json"
        self.assertTrue(result_path.exists())
        with result_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["summary_metrics"]["net_pnl"], 17.0)
        self.assertEqual(payload["execution_reality"]["modeled_slippage_bps"], 4.5)
        self.assertEqual(mock_create_experiment.call_count, 2)
        self.assertEqual(mock_complete_experiment.call_count, 2)
        mock_block_experiment.assert_not_called()
        mock_fail_experiment.assert_not_called()


if __name__ == '__main__':
    unittest.main(argv=['first-arg-is-ignored'], exit=False)
