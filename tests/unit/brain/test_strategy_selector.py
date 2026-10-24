"""Tests for brain strategy selector."""

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.strategy_selector import StrategySelector
from brain.decision_schema import BrainDecision
from brain.prompt_library import PromptLibrary
from experiments.types import Metrics, ExperimentRecord
from experiments.registry import append_experiment_record
from features.registry import FeatureRegistry
from llm.llm_client import LLMClient


class TestStrategySelector:
    """Test StrategySelector class."""

    @pytest.fixture
    def mock_llm(self):
        """Create mock LLM client."""
        llm = MagicMock(spec=LLMClient)
        return llm

    @pytest.fixture
    def prompt_lib(self):
        """Create prompt library."""
        prompts = {"test_prompt": "Test prompt for strategy selection"}
        lib = PromptLibrary(raw={}, prompts=prompts, meta={})
        return lib

    @pytest.fixture
    def feature_registry(self):
        """Create feature registry."""
        from features.registry import FeatureDef

        features = {
            "close": FeatureDef(
                name="close",
                kind="native",
                freq=["1m", "5m"],
                base_cols=["close"],
                tags=["price"],
                desc="Close price",
            )
        }
        registry = FeatureRegistry(features=features)
        return registry

    @pytest.fixture
    def test_experiments(self, tmp_path):
        """Create test experiments in index."""
        index_path = tmp_path / "test_index.jsonl"

        # Create 3 test experiments
        for i in range(3):
            metrics = Metrics(sharpe=2.0 + i * 0.5, ic=0.10 + i * 0.02, trades=100)
            rec = ExperimentRecord(
                run_id=f"test_run_{i:03d}",
                symbol="TSLA",
                freq="1m",
                horizon="10m",
                label_id="ret_10m_fwd",
                model_type="xgb",
                feature_subset=["close"],
                params={"max_depth": 5 + i},
                metrics=metrics,
                run_ts=f"2025-11-17T{10+i}:00:00Z",
            )
            append_experiment_record(rec, index_path)

        return index_path

    def test_selector_creation(self, mock_llm, prompt_lib, tmp_path):
        """Test creating selector."""
        index_path = tmp_path / "index.jsonl"
        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=index_path,
        )

        assert selector._llm is mock_llm
        assert selector._prompt_lib is prompt_lib
        assert selector._prompt_id == "test_prompt"
        assert selector._index_path == index_path

    def test_select_no_experiments_returns_hold(
        self, mock_llm, prompt_lib, feature_registry, tmp_path
    ):
        """Test that select returns HOLD when no experiments found."""
        index_path = tmp_path / "empty_index.jsonl"
        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=index_path,
        )

        decision = selector.select(
            symbol="TSLA",
            now_ts="2025-11-18T10:00:00Z",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            market_snapshot={"last_price": 300.0},
            risk_gates={"max_position_size": 0.1},
            dataset_sanity={"status": "ok"},
            registry=feature_registry,
        )

        assert decision.decision == "HOLD"
        assert decision.chosen_run_id is None
        assert "No historical experiments" in decision.notes

    def test_select_with_experiments(
        self, mock_llm, prompt_lib, feature_registry, test_experiments
    ):
        """Test select with valid experiments."""
        # Mock LLM to return valid decision
        mock_llm.generate.return_value = {
            "decision": "ENTER",
            "chosen_run_id": "test_run_001",
            "side": "LONG",
            "size_frac": 0.5,
            "notes": "Good market conditions",
        }

        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=test_experiments,
        )

        decision = selector.select(
            symbol="TSLA",
            now_ts="2025-11-18T10:00:00Z",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            market_snapshot={"last_price": 300.0, "rvol_20": 1.5},
            risk_gates={"max_position_size": 0.1},
            dataset_sanity={"status": "ok", "bars": 10000},
            registry=feature_registry,
        )

        # Verify LLM was called
        assert mock_llm.generate.call_count == 1
        call_args = mock_llm.generate.call_args[1]
        assert "system_prompt" in call_args
        assert "user_prompt" in call_args

        # Verify decision
        assert decision.decision == "ENTER"
        assert decision.chosen_run_id == "test_run_001"
        assert decision.side == "LONG"
        assert decision.size_frac == 0.5

    def test_select_llm_returns_string_json(
        self, mock_llm, prompt_lib, feature_registry, test_experiments
    ):
        """Test handling when LLM returns JSON string instead of dict."""
        # Mock LLM to return JSON string
        response_dict = {
            "decision": "ADJUST",
            "chosen_run_id": "test_run_002",
            "side": "SHORT",
            "size_frac": 0.3,
            "notes": "Adjust position",
        }
        mock_llm.generate.return_value = json.dumps(response_dict)

        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=test_experiments,
        )

        decision = selector.select(
            symbol="TSLA",
            now_ts="2025-11-18T10:00:00Z",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            market_snapshot={"last_price": 300.0},
            risk_gates={"max_position_size": 0.1},
            dataset_sanity={"status": "ok"},
            registry=feature_registry,
        )

        assert decision.decision == "ADJUST"
        assert decision.chosen_run_id == "test_run_002"
        assert decision.side == "SHORT"

    def test_select_llm_returns_invalid_json(
        self, mock_llm, prompt_lib, feature_registry, test_experiments
    ):
        """Test handling when LLM returns invalid JSON."""
        mock_llm.generate.return_value = "This is not JSON"

        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=test_experiments,
        )

        decision = selector.select(
            symbol="TSLA",
            now_ts="2025-11-18T10:00:00Z",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            market_snapshot={"last_price": 300.0},
            risk_gates={"max_position_size": 0.1},
            dataset_sanity={"status": "ok"},
            registry=feature_registry,
        )

        assert decision.decision == "HOLD"
        assert "invalid JSON" in decision.notes

    def test_select_llm_returns_invalid_run_id(
        self, mock_llm, prompt_lib, feature_registry, test_experiments
    ):
        """Test handling when LLM returns run_id not in allowed list."""
        mock_llm.generate.return_value = {
            "decision": "ENTER",
            "chosen_run_id": "invalid_run_999",
            "side": "LONG",
            "size_frac": 0.5,
            "notes": "Test",
        }

        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=test_experiments,
        )

        decision = selector.select(
            symbol="TSLA",
            now_ts="2025-11-18T10:00:00Z",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            market_snapshot={"last_price": 300.0},
            risk_gates={"max_position_size": 0.1},
            dataset_sanity={"status": "ok"},
            registry=feature_registry,
        )

        assert decision.decision == "HOLD"
        assert "invalid" in decision.notes.lower()

    def test_select_llm_returns_missing_fields(
        self, mock_llm, prompt_lib, feature_registry, test_experiments
    ):
        """Test handling when LLM returns incomplete decision."""
        mock_llm.generate.return_value = {
            "decision": "ENTER",
            # Missing chosen_run_id, side, size_frac
        }

        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=test_experiments,
        )

        decision = selector.select(
            symbol="TSLA",
            now_ts="2025-11-18T10:00:00Z",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            market_snapshot={"last_price": 300.0},
            risk_gates={"max_position_size": 0.1},
            dataset_sanity={"status": "ok"},
            registry=feature_registry,
        )

        assert decision.decision == "HOLD"
        assert "invalid" in decision.notes.lower()

    def test_select_prompts_include_context(
        self, mock_llm, prompt_lib, feature_registry, test_experiments
    ):
        """Test that prompts include all context sections."""
        mock_llm.generate.return_value = {
            "decision": "HOLD",
            "chosen_run_id": None,
            "side": None,
            "size_frac": 0.0,
            "notes": "Test",
        }

        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=test_experiments,
        )

        selector.select(
            symbol="TSLA",
            now_ts="2025-11-18T10:00:00Z",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            market_snapshot={"last_price": 300.0, "change_pct": 2.5},
            risk_gates={"max_position_size": 0.1, "min_ic": 0.05},
            dataset_sanity={"status": "ok", "warnings": []},
            registry=feature_registry,
        )

        # Verify prompt contains expected sections
        call_args = mock_llm.generate.call_args[1]
        user_prompt = call_args["user_prompt"]

        assert "Symbol: TSLA" in user_prompt
        assert "[Market State]" in user_prompt
        assert "[Risk Gates]" in user_prompt
        assert "[Dataset Quality]" in user_prompt
        assert "[Experiment Summary]" in user_prompt
        assert "[Feature Registry]" in user_prompt
        assert "[Strategy Universe]" in user_prompt
        assert "test_run_000" in user_prompt
        assert "test_run_001" in user_prompt
        assert "test_run_002" in user_prompt

    def test_select_filters_by_symbol_freq_horizon(
        self, mock_llm, prompt_lib, feature_registry, tmp_path
    ):
        """Test that select only considers matching experiments."""
        index_path = tmp_path / "index.jsonl"

        # Create experiments with different parameters
        configs = [
            ("TSLA", "1m", "10m", "ret_10m_fwd"),  # Match
            ("NVDA", "1m", "10m", "ret_10m_fwd"),  # Different symbol
            ("TSLA", "5m", "10m", "ret_10m_fwd"),  # Different freq
            ("TSLA", "1m", "5m", "ret_10m_fwd"),   # Different horizon
            ("TSLA", "1m", "10m", "ret_5m_fwd"),   # Different label
        ]

        for i, (symbol, freq, horizon, label_id) in enumerate(configs):
            metrics = Metrics(sharpe=2.0)
            rec = ExperimentRecord(
                run_id=f"run_{i}",
                symbol=symbol,
                freq=freq,
                horizon=horizon,
                label_id=label_id,
                model_type="xgb",
                feature_subset=["close"],
                params={},
                metrics=metrics,
                run_ts="2025-11-17T10:00:00Z",
            )
            append_experiment_record(rec, index_path)

        mock_llm.generate.return_value = {
            "decision": "HOLD",
            "chosen_run_id": None,
            "side": None,
            "size_frac": 0.0,
            "notes": "Test",
        }

        selector = StrategySelector(
            mock_llm,
            prompt_lib,
            "test_prompt",
            index_path=index_path,
        )

        selector.select(
            symbol="TSLA",
            now_ts="2025-11-18T10:00:00Z",
            freq="1m",
            horizon="10m",
            label_id="ret_10m_fwd",
            market_snapshot={"last_price": 300.0},
            risk_gates={"max_position_size": 0.1},
            dataset_sanity={"status": "ok"},
            registry=feature_registry,
        )

        # Verify prompt only includes matching run_id
        call_args = mock_llm.generate.call_args[1]
        user_prompt = call_args["user_prompt"]

        assert "run_0" in user_prompt  # Should be included
        assert "run_1" not in user_prompt  # Different symbol
        assert "run_2" not in user_prompt  # Different freq
        assert "run_3" not in user_prompt  # Different horizon
        assert "run_4" not in user_prompt  # Different label
