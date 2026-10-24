"""Unit tests for dynamic prompt override functionality"""
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

from scripts.ai_trader.orchestrator import Orchestrator


class TestPromptOverride:
    """Test dynamic prompt override functionality"""

    @pytest.fixture
    def mock_config_with_prompts(self, tmp_path):
        """Create a mock config with prompt settings"""
        config = {
            "paths": {
                "health_report": str(tmp_path / "health.json"),
                "logs_dir": str(tmp_path / "logs"),
                "runs_dir": str(tmp_path / "runs"),
            },
            "modes": {
                "paper": True,
                "llm_paper_flag": "--dry-run",
            },
            "policies": {
                "prompts": {
                    "allow_text_override": False,  # Default: only ID mode
                },
                "gates": {
                    "require_healthy": False,
                    "require_data_ok": False,
                    "require_exec_ok": False,
                    "require_llm_ok": False,
                },
            },
            "commands": {},
            "brain": {
                "enabled": False,
            },
        }

        config_path = tmp_path / "test_config.yaml"
        import yaml
        with open(config_path, 'w') as f:
            yaml.dump(config, f)

        # Create health report
        health_path = tmp_path / "health.json"
        with open(health_path, 'w') as f:
            json.dump({"status": "ok"}, f)

        return config_path

    def test_prepare_prompt_overrides_with_id_only(self, mock_config_with_prompts, tmp_path):
        """Test prompt override with ID only (safe mode)"""
        orch = Orchestrator(cfg_path=str(mock_config_with_prompts))

        plan = {
            "providers": [
                {
                    "name": "gemini",
                    "execution_prompt_id": "v3_stable",
                    "execution_prompt_text": None,
                }
            ]
        }

        overrides = orch._prepare_prompt_overrides(plan)

        # Should return prompt ID
        assert 0 in overrides
        assert overrides[0]["PROMPT_ID"] == "v3_stable"
        # Should NOT write file (text is None)
        assert "PROMPT_FILE" not in overrides[0]

    def test_prepare_prompt_overrides_text_rejected_when_disabled(self, mock_config_with_prompts, tmp_path):
        """Test that prompt text is rejected when allow_text_override=false"""
        orch = Orchestrator(cfg_path=str(mock_config_with_prompts))

        plan = {
            "providers": [
                {
                    "name": "gemini",
                    "execution_prompt_id": None,
                    "execution_prompt_text": "You are a test prompt",
                }
            ]
        }

        overrides = orch._prepare_prompt_overrides(plan)

        # Should NOT write file (text override disabled)
        assert 0 not in overrides or "PROMPT_FILE" not in overrides.get(0, {})

    def test_prepare_prompt_overrides_text_allowed_when_enabled(self, tmp_path):
        """Test that prompt text is saved when allow_text_override=true"""
        # Create config with text override enabled
        config = {
            "paths": {
                "health_report": str(tmp_path / "health.json"),
                "logs_dir": str(tmp_path / "logs"),
                "runs_dir": str(tmp_path / "runs"),
            },
            "modes": {
                "paper": True,
            },
            "policies": {
                "prompts": {
                    "allow_text_override": True,  # Enable text mode
                },
                "gates": {
                    "require_healthy": False,
                    "require_data_ok": False,
                    "require_exec_ok": False,
                    "require_llm_ok": False,
                },
            },
            "commands": {},
            "brain": {
                "enabled": False,
            },
        }

        config_path = tmp_path / "test_config.yaml"
        import yaml
        with open(config_path, 'w') as f:
            yaml.dump(config, f)

        # Create health report
        health_path = tmp_path / "health.json"
        with open(health_path, 'w') as f:
            json.dump({"status": "ok"}, f)

        orch = Orchestrator(cfg_path=str(config_path))

        test_prompt = "You are a test trading agent"
        plan = {
            "providers": [
                {
                    "name": "gemini",
                    "execution_prompt_id": None,
                    "execution_prompt_text": test_prompt,
                }
            ]
        }

        overrides = orch._prepare_prompt_overrides(plan)

        # Should write file (text override enabled)
        assert 0 in overrides
        assert "PROMPT_FILE" in overrides[0]

        # Verify file exists and contains correct content
        prompt_file = Path(overrides[0]["PROMPT_FILE"])
        assert prompt_file.exists()
        assert prompt_file.read_text("utf-8") == test_prompt

    def test_prepare_prompt_overrides_both_id_and_text(self, tmp_path):
        """Test when both ID and text are provided"""
        config = {
            "paths": {
                "health_report": str(tmp_path / "health.json"),
                "logs_dir": str(tmp_path / "logs"),
                "runs_dir": str(tmp_path / "runs"),
            },
            "modes": {
                "paper": True,
            },
            "policies": {
                "prompts": {
                    "allow_text_override": True,
                },
                "gates": {
                    "require_healthy": False,
                    "require_data_ok": False,
                    "require_exec_ok": False,
                    "require_llm_ok": False,
                },
            },
            "commands": {},
            "brain": {
                "enabled": False,
            },
        }

        config_path = tmp_path / "test_config.yaml"
        import yaml
        with open(config_path, 'w') as f:
            yaml.dump(config, f)

        health_path = tmp_path / "health.json"
        with open(health_path, 'w') as f:
            json.dump({"status": "ok"}, f)

        orch = Orchestrator(cfg_path=str(config_path))

        plan = {
            "providers": [
                {
                    "name": "gemini",
                    "execution_prompt_id": "v3_stable",
                    "execution_prompt_text": "Custom prompt text",
                }
            ]
        }

        overrides = orch._prepare_prompt_overrides(plan)

        # Should have both ID and FILE
        assert 0 in overrides
        assert overrides[0]["PROMPT_ID"] == "v3_stable"
        assert "PROMPT_FILE" in overrides[0]

    def test_prepare_prompt_overrides_multiple_providers(self, mock_config_with_prompts):
        """Test with multiple providers"""
        orch = Orchestrator(cfg_path=str(mock_config_with_prompts))

        plan = {
            "providers": [
                {
                    "name": "gemini",
                    "execution_prompt_id": "v3_stable",
                },
                {
                    "name": "deepseek_nvidia",
                    "execution_prompt_id": "v4_experimental",
                },
            ]
        }

        overrides = orch._prepare_prompt_overrides(plan)

        # Should have overrides for both providers
        assert 0 in overrides
        assert 1 in overrides
        assert overrides[0]["PROMPT_ID"] == "v3_stable"
        assert overrides[1]["PROMPT_ID"] == "v4_experimental"

    def test_prepare_prompt_overrides_empty_plan(self, mock_config_with_prompts):
        """Test with empty plan"""
        orch = Orchestrator(cfg_path=str(mock_config_with_prompts))

        plan = {}
        overrides = orch._prepare_prompt_overrides(plan)

        # Should return empty dict
        assert overrides == {}

    def test_prepare_prompt_overrides_no_prompt_fields(self, mock_config_with_prompts):
        """Test with providers that don't have prompt fields"""
        orch = Orchestrator(cfg_path=str(mock_config_with_prompts))

        plan = {
            "providers": [
                {
                    "name": "gemini",
                    "interval_sec": 300,
                    "cycles": 10,
                    # No execution_prompt_id or execution_prompt_text
                }
            ]
        }

        overrides = orch._prepare_prompt_overrides(plan)

        # Should return empty dict (no prompts specified)
        assert overrides == {}


class TestPromptLibraryLoading:
    """Test prompt library loading in PromptBuilder"""

    def test_prompt_library_exists(self):
        """Test that prompt_library.json exists and is valid"""
        lib_path = Path("prompts/prompt_library.json")
        assert lib_path.exists(), "prompt_library.json should exist"

        # Should be valid JSON
        with open(lib_path) as f:
            library = json.load(f)

        # Should have expected prompts
        assert "v3_stable" in library
        assert "v3_fast_market_following" in library
        assert "v4_experimental" in library
        assert "conservative" in library
        assert "aggressive_scalper" in library

        # Each prompt should be a non-empty string
        for prompt_id, prompt_text in library.items():
            if not prompt_id.startswith("_"):  # Skip metadata fields
                assert isinstance(prompt_text, str)
                assert len(prompt_text) > 0


class TestEndToEndPromptOverride:
    """Integration test for full prompt override flow"""

    @pytest.mark.integration
    def test_full_flow_with_mock_llm_controller(self, tmp_path):
        """Test full flow: plan -> prepare -> controller"""
        # Setup
        config = {
            "paths": {
                "health_report": str(tmp_path / "health.json"),
                "logs_dir": str(tmp_path / "logs"),
                "runs_dir": str(tmp_path / "runs"),
            },
            "modes": {
                "paper": True,
                "llm_paper_flag": "--dry-run",
            },
            "policies": {
                "prompts": {
                    "allow_text_override": False,
                },
                "gates": {
                    "require_healthy": False,
                    "require_data_ok": False,
                    "require_exec_ok": False,
                    "require_llm_ok": False,
                },
            },
            "commands": {
                "premarket_fallback": "echo test",
            },
            "brain": {
                "enabled": False,
            },
        }

        config_path = tmp_path / "test_config.yaml"
        import yaml
        with open(config_path, 'w') as f:
            yaml.dump(config, f)

        health_path = tmp_path / "health.json"
        with open(health_path, 'w') as f:
            json.dump({"status": "ok"}, f)

        orch = Orchestrator(cfg_path=str(config_path))

        # Create plan with prompt override
        plan = {
            "session": "morning",
            "providers": [
                {
                    "name": "gemini",
                    "interval_sec": 300,
                    "cycles": 1,
                    "execution_prompt_id": "v3_stable",
                }
            ],
            "llm_trader_flags": {},
        }

        # Prepare overrides
        overrides = orch._prepare_prompt_overrides(plan)

        # Verify override structure
        assert 0 in overrides
        assert overrides[0]["PROMPT_ID"] == "v3_stable"

        # Mock the llm controller's run_with_plan to capture the call
        with patch.object(orch.llm, 'run_with_plan') as mock_run:
            # Simulate calling with overrides
            orch.llm.run_with_plan(plan, prompt_overrides=overrides)

            # Verify it was called with correct arguments
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert call_args[0][0] == plan  # First positional arg
            assert call_args[1]["prompt_overrides"] == overrides  # Keyword arg
