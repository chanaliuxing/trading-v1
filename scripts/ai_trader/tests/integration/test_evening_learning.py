"""Integration tests for evening learning and other orchestrator sequences"""
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest
import yaml

from scripts.ai_trader.orchestrator import Orchestrator


class TestEveningLearningIntegration:
    """Test evening learning workflow"""

    def test_evening_learning_full_flow_with_best_params(self, mock_config, mock_gen_summary, temp_dir):
        """Test complete evening learning when best params found"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        import os
        orig_cwd = os.getcwd()
        os.chdir(mock_gen_summary.parent.parent.parent.parent)

        try:
            with patch.object(orch, '_run_cmd'):  # Mock reflection/prompt_evolver commands
                with patch.object(orch.rs, 'alpha_loop'):
                    with patch.object(orch.rs, 'apply_params'):
                        with patch('scripts.ai_trader.memory.episode_store.store_episode', return_value={"saved": True}):
                            result = orch.evening_learning()

            assert result["status"] == "ok"
            assert "actions" in result
            assert "alpha_loop" in result["actions"]
            assert result["actions"]["hpo_deploy_check"]["ok"] is True
        finally:
            os.chdir(orig_cwd)

    def test_evening_learning_hpo_gate_pass(self, mock_config, mock_gen_summary, temp_dir):
        """Test evening learning when HPO gate allows deployment"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        cfg["policies"]["hpo_deploy"]["min_oos_sharpe"] = 1.0  # Lower threshold
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        import os
        orig_cwd = os.getcwd()
        os.chdir(mock_gen_summary.parent.parent.parent.parent)

        try:
            with patch.object(orch, '_run_cmd'):
                with patch.object(orch.rs, 'alpha_loop'):
                    with patch.object(orch.rs, 'apply_params') as mock_apply:
                        with patch('scripts.ai_trader.memory.episode_store.store_episode', return_value={"saved": True}):
                            result = orch.evening_learning()

            # Should apply params when gate passes
            mock_apply.assert_called_once()
            assert "apply_params" in result["actions"]
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
            with patch.object(orch, '_run_cmd'):
                with patch.object(orch.rs, 'alpha_loop'):
                    with patch.object(orch.rs, 'apply_params') as mock_apply:
                        with patch('scripts.ai_trader.memory.episode_store.store_episode', return_value={"saved": True}):
                            result = orch.evening_learning()

            # Should NOT apply params when gate blocks
            mock_apply.assert_not_called()
            assert result["actions"]["hpo_deploy_check"]["ok"] is False
            assert "oos_sharpe<2.0" in result["actions"]["hpo_deploy_check"]["reasons"][0]
        finally:
            os.chdir(orig_cwd)

    def test_evening_learning_no_best_params(self, mock_config, temp_dir):
        """Test evening learning when no HPO results found"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        import os
        orig_cwd = os.getcwd()
        os.chdir(temp_dir)

        try:
            with patch.object(orch, '_run_cmd'):
                with patch.object(orch.rs, 'alpha_loop'):
                    with patch('scripts.ai_trader.memory.episode_store.store_episode', return_value={"saved": True}):
                        result = orch.evening_learning()

            assert result["status"] == "ok"
            # When no best params, gate check not performed
            assert "hpo_deploy_check" not in result["actions"]
        finally:
            os.chdir(orig_cwd)


    def test_evening_learning_memory_persistence(self, mock_config, mock_gen_summary, temp_dir):
        """Test episode is persisted to memory"""
        cfg = yaml.safe_load(mock_config.read_text())
        cfg["paths"]["runs_dir"] = str(temp_dir / "runs")
        mock_config.write_text(yaml.safe_dump(cfg))

        orch = Orchestrator(cfg_path=str(mock_config))

        import os
        orig_cwd = os.getcwd()
        os.chdir(mock_gen_summary.parent.parent.parent.parent)

        try:
            with patch.object(orch, '_run_cmd'):
                with patch.object(orch.rs, 'alpha_loop'):
                    with patch.object(orch.rs, 'apply_params'):  # Mock apply_params
                        with patch('scripts.ai_trader.memory.episode_store.store_episode') as mock_store:
                            mock_store.return_value = {"saved": True}
                            result = orch.evening_learning()

            # Episode should be stored
            mock_store.assert_called_once()
            call_kwargs = mock_store.call_args[1]
            assert call_kwargs["session"] == "evening"
        finally:
            os.chdir(orig_cwd)
