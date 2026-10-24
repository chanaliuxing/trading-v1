# tests/unit/brain/test_plan.py
"""
Tests for BrainExecutionPlan (UF 18 Part 1).

Tests AI Brain's execution plan structure.
"""

import pytest
from brain.plan import BrainExecutionPlan


class TestBrainExecutionPlan:
    """Test BrainExecutionPlan dataclass."""

    def test_create_minimal_plan(self):
        """Create minimal execution plan."""
        plan = BrainExecutionPlan(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            execution_prompt_id="v3_stable",
        )

        assert plan.symbol == "AAPL"
        assert plan.label_id == "ret_10b_fwd"
        assert plan.execution_prompt_id == "v3_stable"
        assert plan.max_trials == 30  # default

    def test_create_full_plan(self):
        """Create full execution plan with all fields."""
        plan = BrainExecutionPlan(
            symbol="TSLA",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            execution_prompt_id="v3_fast_market_following_tactical_20251117",
            base_prompt_id="v3_fast_market_following",
            prompt_selection_reason="Selected by history: mean=2.3, median=2.1",
            allowed_model_types=["xgb", "lgb"],
            max_trials=50,
            require_clean_dataset=True,
            min_rows=10000,
            max_missing_ratio=0.3,
            mode="training",
            notes="Test run",
            extra_cfg={"custom_param": "value"},
        )

        assert plan.base_prompt_id == "v3_fast_market_following"
        assert len(plan.allowed_model_types) == 2
        assert plan.max_trials == 50
        assert plan.min_rows == 10000

    def test_to_dict(self):
        """Convert plan to dict."""
        plan = BrainExecutionPlan(
            symbol="NVDA",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            execution_prompt_id="v3_stable",
        )

        d = plan.to_dict()
        assert isinstance(d, dict)
        assert d["symbol"] == "NVDA"
        assert d["execution_prompt_id"] == "v3_stable"

    def test_from_dict(self):
        """Create plan from dict."""
        data = {
            "symbol": "AAPL",
            "table": "bars_1m",
            "start_ts": "2024-01-01",
            "end_ts": "2024-03-01",
            "label_id": "ret_10b_fwd",
            "horizon": "10b",
            "execution_prompt_id": "v3_stable",
            "base_prompt_id": "v3",
            "prompt_selection_reason": "fallback",
            "allowed_model_types": ["xgb"],
            "max_trials": 40,
            "require_clean_dataset": False,
            "min_rows": 5000,
            "max_missing_ratio": 0.5,
            "mode": "training",
            "notes": "",
            "extra_cfg": {},
        }

        plan = BrainExecutionPlan.from_dict(data)
        assert plan.symbol == "AAPL"
        assert plan.max_trials == 40
        assert plan.require_clean_dataset is False

    def test_validate_valid_plan(self):
        """Validate a valid plan."""
        plan = BrainExecutionPlan(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            execution_prompt_id="v3_stable",
        )

        errors = plan.validate()
        assert errors == []

    def test_validate_missing_symbol(self):
        """Validate fails when symbol missing."""
        plan = BrainExecutionPlan(
            symbol="",  # Invalid
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            execution_prompt_id="v3_stable",
        )

        errors = plan.validate()
        assert len(errors) > 0
        assert any("symbol" in e for e in errors)

    def test_validate_missing_label_id(self):
        """Validate fails when label_id missing."""
        plan = BrainExecutionPlan(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="",  # Invalid
            horizon="10b",
            execution_prompt_id="v3_stable",
        )

        errors = plan.validate()
        assert any("label_id" in e for e in errors)

    def test_validate_invalid_max_trials(self):
        """Validate fails when max_trials <= 0."""
        plan = BrainExecutionPlan(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            execution_prompt_id="v3_stable",
            max_trials=0,  # Invalid
        )

        errors = plan.validate()
        assert any("max_trials" in e for e in errors)

    def test_validate_invalid_max_missing_ratio(self):
        """Validate fails when max_missing_ratio out of range."""
        plan = BrainExecutionPlan(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            execution_prompt_id="v3_stable",
            max_missing_ratio=1.5,  # Invalid (> 1)
        )

        errors = plan.validate()
        assert any("max_missing_ratio" in e for e in errors)

    def test_validate_unknown_mode(self):
        """Validate fails for unknown mode."""
        plan = BrainExecutionPlan(
            symbol="AAPL",
            table="bars_1m",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            label_id="ret_10b_fwd",
            horizon="10b",
            execution_prompt_id="v3_stable",
            mode="unknown_mode",  # Invalid
        )

        errors = plan.validate()
        assert any("mode" in e for e in errors)
