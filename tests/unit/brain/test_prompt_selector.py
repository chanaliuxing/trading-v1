# tests/unit/brain/test_prompt_selector.py
"""
Tests for PromptSelector (UF 18 Part 2).

Tests data-driven prompt selection logic.
"""

import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta

from prompts.registry import PromptRegistry
from runs.history_index import RunHistoryIndex
from brain.prompt_selector import PromptSelector, PromptSelectionContext


class TestPromptSelector:
    """Test PromptSelector."""

    def setup_method(self):
        """Setup test fixtures."""
        # Create test prompt registry
        self.prompt_spec = {
            "v3_stable": "Base stable prompt",
            "v3_fast": "Base fast prompt",
            "prompts": {
                "v3_stable_tactical_001": "Tactical variant 1",
                "v3_stable_tactical_002": "Tactical variant 2",
            },
            "meta": {
                "v3_stable_tactical_001": {
                    "base_id": "v3_stable",
                    "created_at": "2024-01-01T12:00:00",
                },
                "v3_stable_tactical_002": {
                    "base_id": "v3_stable",
                    "created_at": "2024-01-02T12:00:00",
                },
            },
        }
        self.registry = PromptRegistry(self.prompt_spec)

    def test_select_best_by_history(self, tmp_path):
        """Select best prompt based on historical performance."""
        # Create history with performance data
        runs_data = [
            {
                "run_id": f"run_{i:03d}",
                "created_at": (datetime.utcnow() - timedelta(days=i)).isoformat(),
                "symbol": "AAPL",
                "execution_prompt_id": "v3_stable_tactical_001",
                "base_prompt_id": "v3_stable",
                "label_id": "ret_10b_fwd",
                "best_score": 2.0 + (i % 2) * 0.2,  # Avg ~2.1
                "metric_name": "sharpe",
            }
            for i in range(5)  # 5 runs for tactical_001
        ] + [
            {
                "run_id": f"run_2_{i:03d}",
                "created_at": (datetime.utcnow() - timedelta(days=i)).isoformat(),
                "symbol": "AAPL",
                "execution_prompt_id": "v3_stable_tactical_002",
                "base_prompt_id": "v3_stable",
                "label_id": "ret_10b_fwd",
                "best_score": 2.5 + (i % 2) * 0.2,  # Avg ~2.6 (better)
                "metric_name": "sharpe",
            }
            for i in range(5)  # 5 runs for tactical_002
        ]

        jsonl_path = tmp_path / "runs.jsonl"
        with jsonl_path.open("w") as f:
            for run in runs_data:
                f.write(json.dumps(run) + "\n")

        history = RunHistoryIndex.from_jsonl(jsonl_path)
        selector = PromptSelector(self.registry, history)

        ctx = PromptSelectionContext(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            base_prompt_id="v3_stable",
            min_runs_for_stats=3,
        )

        plan = selector.make_execution_plan(ctx)

        # Should select tactical_002 (better performance)
        assert plan.execution_prompt_id == "v3_stable_tactical_002"
        assert "Selected by history" in plan.prompt_selection_reason
        assert plan.base_prompt_id == "v3_stable"

    def test_fallback_to_latest_when_insufficient_history(self, tmp_path):
        """Fallback to latest tactical when history insufficient."""
        # Create history with only 1 run (< min_runs_for_stats)
        runs_data = [
            {
                "run_id": "run_001",
                "created_at": datetime.utcnow().isoformat(),
                "symbol": "AAPL",
                "execution_prompt_id": "v3_stable_tactical_001",
                "base_prompt_id": "v3_stable",
                "label_id": "ret_10b_fwd",
                "best_score": 2.0,
                "metric_name": "sharpe",
            }
        ]

        jsonl_path = tmp_path / "runs.jsonl"
        with jsonl_path.open("w") as f:
            for run in runs_data:
                f.write(json.dumps(run) + "\n")

        history = RunHistoryIndex.from_jsonl(jsonl_path)
        selector = PromptSelector(self.registry, history)

        ctx = PromptSelectionContext(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            base_prompt_id="v3_stable",
            min_runs_for_stats=3,  # Requires 3+ runs
        )

        plan = selector.make_execution_plan(ctx)

        # Should fallback to latest (tactical_002)
        assert plan.execution_prompt_id == "v3_stable_tactical_002"
        assert "Fallback to latest_by_base" in plan.prompt_selection_reason

    def test_fallback_to_base_when_no_tactical(self, tmp_path):
        """Fallback to base prompt when no tactical variants."""
        # Use v3_fast which has no tactical variants
        jsonl_path = tmp_path / "runs.jsonl"
        jsonl_path.write_text("")  # Empty history

        history = RunHistoryIndex.from_jsonl(jsonl_path)
        selector = PromptSelector(self.registry, history)

        ctx = PromptSelectionContext(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            base_prompt_id="v3_fast",  # No tactical variants
        )

        plan = selector.make_execution_plan(ctx)

        # Should fallback to base prompt
        assert plan.execution_prompt_id == "v3_fast"
        assert "Fallback to base prompt itself" in plan.prompt_selection_reason

    def test_error_when_base_not_found(self, tmp_path):
        """Raise error when base_id doesn't exist."""
        jsonl_path = tmp_path / "runs.jsonl"
        jsonl_path.write_text("")

        history = RunHistoryIndex.from_jsonl(jsonl_path)
        selector = PromptSelector(self.registry, history)

        ctx = PromptSelectionContext(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            base_prompt_id="nonexistent_base",  # Doesn't exist
        )

        with pytest.raises(ValueError, match="Neither tactical nor base prompt found"):
            selector.make_execution_plan(ctx)

    def test_explain_selection(self, tmp_path):
        """Generate human-readable explanation."""
        jsonl_path = tmp_path / "runs.jsonl"
        jsonl_path.write_text("")

        history = RunHistoryIndex.from_jsonl(jsonl_path)
        selector = PromptSelector(self.registry, history)

        ctx = PromptSelectionContext(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            base_prompt_id="v3_stable",
        )

        explanation = selector.explain_selection(ctx)

        assert "Prompt Selection Explanation" in explanation
        assert "AAPL" in explanation
        assert "v3_stable" in explanation
