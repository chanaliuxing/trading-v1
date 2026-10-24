"""Integration tests for morning sequence workflow"""
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest
import yaml

from scripts.ai_trader.orchestrator import Orchestrator


class TestMorningSequenceIntegration:
    """Integration tests for complete morning sequence"""

    def test_morning_sequence_all_gates_pass(self, mock_config, mock_health_report, temp_dir):
        """Test morning sequence when all gates pass"""
        # Setup config with proper paths
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        # Mock all gates to pass
        with patch.object(orch.health, 'check', return_value={"ok": True, "status": "healthy"}):
            with patch.object(orch.data, 'check', return_value={"ok": True, "reasons": []}):
                with patch.object(orch.exec, 'check', return_value={"ok": True}):
                    with patch.object(orch.llm, 'start_premarket'):
                        result = orch.morning_sequence()

        assert result["gate"] == "all"
        assert result["status"] == "ok"

    def test_morning_sequence_health_gate_fails_no_brain(self, mock_config, temp_dir):
        """Test morning sequence when health gate fails and brain disabled"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        cfg["brain"]["enabled"] = False
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        with patch.object(orch.health, 'check', return_value={"ok": False, "status": "degraded"}):
            result = orch.morning_sequence()

        assert result["gate"] == "health"
        assert result["status"] == "block"

    def test_morning_sequence_data_gate_fails_with_backfill(self, mock_config, temp_dir):
        """Test morning sequence when data gate fails, triggers backfill"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        # First check fails, second check after backfill passes
        with patch.object(orch.health, 'check', return_value={"ok": True}):
            with patch.object(orch.data, 'check', side_effect=[
                {"ok": False, "reasons": ["missing_bars"]},
                {"ok": True, "reasons": []}
            ]):
                with patch.object(orch.rs, 'backfill_yesterday') as mock_backfill:
                    with patch.object(orch.llm, 'start_premarket'):
                        result = orch.morning_sequence()

        mock_backfill.assert_called_once()
        assert result["status"] == "ok"

    def test_morning_sequence_exec_gate_skipped_in_paper(self, mock_config, temp_dir):
        """Test morning sequence skips exec gate in paper mode"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["modes"]["paper"] = True
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        with patch.object(orch.health, 'check', return_value={"ok": True}):
            with patch.object(orch.data, 'check', return_value={"ok": True}):
                with patch.object(orch.exec, 'check') as mock_exec:
                    with patch.object(orch.llm, 'start_premarket'):
                        result = orch.morning_sequence()

        # Exec gate should not be called in paper mode
        mock_exec.assert_not_called()
        assert result["status"] == "ok"

    def test_morning_sequence_with_brain_approval(self, mock_config, mock_health_report, temp_dir):
        """Test morning sequence with brain approval flow"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        cfg["brain"]["enabled"] = True
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        # Create mock brain
        mock_brain = Mock()
        mock_brain.decide.return_value = {"proceed": True, "notes": "All clear"}
        mock_brain.propose_plan.return_value = {
            "session": "morning",
            "providers": [{"name": "deepseek_nvidia", "symbols": ["TSLA"]}]
        }
        orch.brain = mock_brain

        with patch.object(orch.health, 'check', return_value={"ok": True}):
            with patch.object(orch.data, 'check', return_value={"ok": True}):
                with patch.object(orch.llm, 'run_with_plan'):
                    with patch('scripts.ai_trader.memory.aggregator.memory_summary', return_value={}):
                        with patch.object(orch, '_execute_pending_actions'):
                            result = orch.morning_sequence()

        mock_brain.decide.assert_called()
        mock_brain.propose_plan.assert_called()
        assert result["status"] == "ok"

    def test_morning_sequence_brain_blocks(self, mock_config, temp_dir):
        """Test morning sequence when brain decides to block"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        cfg["brain"]["enabled"] = True
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        mock_brain = Mock()
        mock_brain.decide.return_value = {"proceed": False, "notes": "Market conditions unfavorable"}
        mock_brain.propose_plan.return_value = {"session": "morning", "providers": []}
        orch.brain = mock_brain

        with patch.object(orch.health, 'check', return_value={"ok": True}):
            with patch.object(orch.data, 'check', return_value={"ok": True}):
                with patch('scripts.ai_trader.memory.aggregator.memory_summary', return_value={}):
                    with patch.object(orch, '_execute_pending_actions'):
                        result = orch.morning_sequence()

        assert result["gate"] == "brain"
        assert result["status"] == "block"

    def test_morning_sequence_writes_decision_artifacts(self, mock_config, temp_dir):
        """Test that morning sequence writes decision artifacts"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        cfg["brain"]["enabled"] = True
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        mock_brain = Mock()
        mock_brain.decide.return_value = {"proceed": True}
        mock_brain.propose_plan.return_value = {"session": "morning", "providers": []}
        orch.brain = mock_brain

        import datetime

        with patch.object(orch.health, 'check', return_value={"ok": True}):
            with patch.object(orch.data, 'check', return_value={"ok": True}):
                with patch.object(orch.llm, 'run_with_plan'):
                    with patch('scripts.ai_trader.memory.aggregator.memory_summary', return_value={}):
                        with patch.object(orch, '_execute_pending_actions'):
                            orch.morning_sequence()

        # Check decision file was created (use actual date)
        today = datetime.date.today()
        decision_file = temp_dir / "runs" / today.isoformat() / "morning_decision.json"
        assert decision_file.exists()

        decision_data = json.loads(decision_file.read_text())
        assert "input" in decision_data
        assert "decision" in decision_data


class TestEveningLearningIntegration:
    """Integration tests for evening learning workflow"""

    def test_evening_learning_full_flow(self, mock_config, mock_gen_summary, temp_dir):
        """Test complete evening learning flow"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        # Change to temp directory for gen_summary discovery
        import os
        orig_cwd = os.getcwd()
        os.chdir(mock_gen_summary.parent.parent.parent.parent)

        try:
            with patch('subprocess.check_call'):  # Mock all subprocess calls
                with patch.object(orch.rs, 'alpha_loop'):
                    with patch.object(orch.rs, 'apply_params'):
                        with patch.object(orch.rs, 'rl_train'):
                            with patch.object(orch.rs, 'rl_eval', return_value={"gate": {"ok": True}}):
                                with patch('scripts.ai_trader.memory.episode_store.store_episode', return_value={"saved": True}):
                                    result = orch.evening_learning()

            assert result["status"] == "ok"
            assert "actions" in result
            assert "alpha_loop" in result["actions"]
        finally:
            os.chdir(orig_cwd)

    def test_evening_learning_hpo_gate_blocks(self, mock_config, mock_gen_summary, temp_dir):
        """Test evening learning when HPO gate blocks deployment"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        cfg["policies"]["hpo_deploy"]["min_oos_sharpe"] = 2.0  # Very high threshold
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        import os
        orig_cwd = os.getcwd()
        os.chdir(mock_gen_summary.parent.parent.parent.parent)

        try:
            with patch('subprocess.check_call'):
                with patch.object(orch.rs, 'alpha_loop'):
                    with patch.object(orch.rs, 'apply_params') as mock_apply:
                        with patch('scripts.ai_trader.memory.episode_store.store_episode', return_value={"saved": True}):
                            result = orch.evening_learning()

            # Should not apply params due to gate failure
            mock_apply.assert_not_called()
            assert result["actions"]["apply_params_skipped"] is True
        finally:
            os.chdir(orig_cwd)
