"""Tests for AI brain context builder."""

import pytest

from brain.context_builder import build_brain_prompts, _format_dict_block, _format_list_block
from brain.prompt_library import PromptLibrary
from features.registry import FeatureRegistry, FeatureDef


class TestFormatDictBlock:
    """Test _format_dict_block helper."""

    def test_format_dict_block(self):
        """Test formatting dictionary as block."""
        data = {"price": 100.5, "volume": 1000, "status": "active"}
        result = _format_dict_block("Market Data", data)

        assert "[Market Data]" in result
        assert "- price: 100.5" in result
        assert "- volume: 1000" in result
        assert "- status: active" in result

    def test_format_empty_dict(self):
        """Test formatting empty dictionary."""
        result = _format_dict_block("Empty", {})
        assert "[Empty]" in result
        assert "- n/a" in result

    def test_format_none_dict(self):
        """Test formatting None as dictionary."""
        result = _format_dict_block("None Data", None)
        assert "[None Data]" in result
        assert "- n/a" in result


class TestFormatListBlock:
    """Test _format_list_block helper."""

    def test_format_list_block(self):
        """Test formatting list as block."""
        data = ["item1", "item2", "item3"]
        result = _format_list_block("Items", data)

        assert "[Items]" in result
        assert "- item1" in result
        assert "- item2" in result
        assert "- item3" in result

    def test_format_empty_list(self):
        """Test formatting empty list."""
        result = _format_list_block("Empty", [])
        assert "[Empty]" in result
        assert "- n/a" in result

    def test_format_none_list(self):
        """Test formatting None as list."""
        result = _format_list_block("None List", None)
        assert "[None List]" in result
        assert "- n/a" in result


class TestBuildBrainPrompts:
    """Test build_brain_prompts function."""

    @pytest.fixture
    def mock_prompt_library(self):
        """Create a mock prompt library."""
        prompts = {
            "v3_stable": "You are a conservative quantitative trader. Always output JSON."
        }
        return PromptLibrary(raw={}, prompts=prompts, meta={})

    @pytest.fixture
    def mock_registry(self):
        """Create a mock feature registry."""
        features = {
            "close": FeatureDef(
                name="close",
                kind="native",
                freq=["1m"],
                base_cols=["close"],
                tags=["price"],
                desc="Close price",
            ),
            "ret_1b": FeatureDef(
                name="ret_1b",
                kind="python",
                freq=["1m"],
                base_cols=["close"],
                tags=["return"],
                desc="1 bar return",
            ),
        }
        return FeatureRegistry(features=features, version="1.0")

    def test_build_basic_prompts(self, mock_prompt_library):
        """Test building basic prompts without optional data."""
        result = build_brain_prompts(
            prompt_lib=mock_prompt_library,
            prompt_id="v3_stable",
            symbol="TSLA",
            now_ts="2025-11-17T09:30:00",
            freq="1m",
            horizon="10m",
            market_snapshot={"last_price": 250.5, "change_pct": 1.2},
            risk_gates={"max_position": 100, "max_loss": 1000},
        )

        assert "system_prompt" in result
        assert "user_prompt" in result

        # Check system prompt
        assert "conservative quantitative trader" in result["system_prompt"]
        assert "JSON" in result["system_prompt"]
        assert "Market State" in result["system_prompt"]
        assert "Risk Gates" in result["system_prompt"]

        # Check user prompt
        assert "Symbol: TSLA" in result["user_prompt"]
        assert "2025-11-17T09:30:00" in result["user_prompt"]
        assert "1m" in result["user_prompt"]
        assert "10m" in result["user_prompt"]
        assert "last_price: 250.5" in result["user_prompt"]
        assert "max_position: 100" in result["user_prompt"]

    def test_build_with_dataset_sanity(self, mock_prompt_library):
        """Test building prompts with dataset sanity info."""
        result = build_brain_prompts(
            prompt_lib=mock_prompt_library,
            prompt_id="v3_stable",
            symbol="NVDA",
            now_ts="2025-11-17T10:00:00",
            freq="1m",
            horizon="5m",
            market_snapshot={"last_price": 500.0},
            risk_gates={"max_position": 50},
            dataset_sanity={
                "overall_status": "ok",
                "warnings": 0,
                "total_rows": 10000,
            },
        )

        user_prompt = result["user_prompt"]
        assert "[Dataset Quality]" in user_prompt
        assert "overall_status: ok" in user_prompt
        assert "warnings: 0" in user_prompt
        assert "total_rows: 10000" in user_prompt

    def test_build_with_experiment_summary(self, mock_prompt_library):
        """Test building prompts with experiment summary."""
        result = build_brain_prompts(
            prompt_lib=mock_prompt_library,
            prompt_id="v3_stable",
            symbol="AAPL",
            now_ts="2025-11-17T11:00:00",
            freq="1m",
            horizon="15m",
            market_snapshot={"last_price": 180.0},
            risk_gates={"max_position": 200},
            experiment_summary={
                "sharpe": 1.8,
                "win_rate": 0.58,
                "avg_trade_return": 0.012,
            },
        )

        user_prompt = result["user_prompt"]
        assert "[Experiment Summary]" in user_prompt
        assert "sharpe: 1.8" in user_prompt
        assert "win_rate: 0.58" in user_prompt

    def test_build_with_registry(self, mock_prompt_library, mock_registry):
        """Test building prompts with feature registry."""
        result = build_brain_prompts(
            prompt_lib=mock_prompt_library,
            prompt_id="v3_stable",
            symbol="GOOGL",
            now_ts="2025-11-17T12:00:00",
            freq="1m",
            horizon="20m",
            market_snapshot={"last_price": 150.0},
            risk_gates={"max_position": 75},
            registry=mock_registry,
        )

        user_prompt = result["user_prompt"]
        assert "[Feature Registry]" in user_prompt
        assert "close" in user_prompt
        assert "ret_1b" in user_prompt
        assert "Close price" in user_prompt
        assert "1 bar return" in user_prompt

    def test_build_without_registry(self, mock_prompt_library):
        """Test building prompts without registry."""
        result = build_brain_prompts(
            prompt_lib=mock_prompt_library,
            prompt_id="v3_stable",
            symbol="MSFT",
            now_ts="2025-11-17T13:00:00",
            freq="1m",
            horizon="30m",
            market_snapshot={"last_price": 420.0},
            risk_gates={"max_position": 150},
            registry=None,
        )

        user_prompt = result["user_prompt"]
        assert "[Feature Registry]" in user_prompt
        assert "n/a" in user_prompt
        assert "no registry loaded" in user_prompt

    def test_build_with_all_contexts(self, mock_prompt_library, mock_registry):
        """Test building prompts with all context sections."""
        result = build_brain_prompts(
            prompt_lib=mock_prompt_library,
            prompt_id="v3_stable",
            symbol="TSLA",
            now_ts="2025-11-17T14:00:00",
            freq="1m",
            horizon="10m",
            market_snapshot={"last_price": 255.0, "rvol": 2.5},
            risk_gates={"max_position": 100, "stop_loss": 500},
            dataset_sanity={"overall_status": "ok"},
            experiment_summary={"sharpe": 2.1},
            registry=mock_registry,
        )

        user_prompt = result["user_prompt"]

        # All sections should be present
        assert "[Market State]" in user_prompt
        assert "[Risk Gates]" in user_prompt
        assert "[Dataset Quality]" in user_prompt
        assert "[Experiment Summary]" in user_prompt
        assert "[Feature Registry]" in user_prompt

        # Verify content
        assert "last_price: 255.0" in user_prompt
        assert "max_position: 100" in user_prompt
        assert "overall_status: ok" in user_prompt
        assert "sharpe: 2.1" in user_prompt
        assert "close" in user_prompt

    def test_prompt_id_not_found(self, mock_prompt_library):
        """Test error when prompt_id not found."""
        with pytest.raises(KeyError):
            build_brain_prompts(
                prompt_lib=mock_prompt_library,
                prompt_id="nonexistent_prompt",
                symbol="TSLA",
                now_ts="2025-11-17T15:00:00",
                freq="1m",
                horizon="10m",
                market_snapshot={},
                risk_gates={},
            )

    def test_system_prompt_includes_instructions(self, mock_prompt_library):
        """Test that system prompt includes structured context instructions."""
        result = build_brain_prompts(
            prompt_lib=mock_prompt_library,
            prompt_id="v3_stable",
            symbol="TSLA",
            now_ts="2025-11-17T16:00:00",
            freq="1m",
            horizon="10m",
            market_snapshot={},
            risk_gates={},
        )

        system_prompt = result["system_prompt"]
        assert "structured context" in system_prompt
        assert "[Market State]" in system_prompt
        assert "[Risk Gates]" in system_prompt
        assert "JSON only" in system_prompt
