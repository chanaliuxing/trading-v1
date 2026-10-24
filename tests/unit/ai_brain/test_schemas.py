# tests/unit/ai_brain/test_schemas.py

"""
Tests for AI Brain decision schemas.

Tests comprehensive validation of BrainContext, BrainPIParams, and BrainDecision.
"""

import pytest
from pydantic import ValidationError

from ai_brain.schemas import (
    BrainContext,
    BrainDecision,
    BrainPIParams,
    DatasetSanitySummary,
)


class TestDatasetSanitySummary:
    """Test DatasetSanitySummary schema."""

    def test_valid_summary(self):
        """Valid summary should parse correctly."""
        summary = DatasetSanitySummary(
            ok=True,
            severity="ok",
            n_rows=10000,
            n_cols=25,
            issue_counts={"info": 0, "warning": 0, "error": 0},
            top_issues=[],
            label_info=None,
        )

        assert summary.ok is True
        assert summary.severity == "ok"
        assert summary.n_rows == 10000

    def test_severity_must_be_literal(self):
        """Severity must be one of ok/warning/error."""
        with pytest.raises(ValidationError):
            DatasetSanitySummary(
                ok=True,
                severity="invalid",  # Not in Literal
                n_rows=1000,
                n_cols=10,
            )

    def test_with_issues(self):
        """Summary with issues should parse correctly."""
        summary = DatasetSanitySummary(
            ok=False,
            severity="error",
            n_rows=500,
            n_cols=10,
            issue_counts={"info": 0, "warning": 2, "error": 1},
            top_issues=[
                {"code": "label_missing", "severity": "error", "message": "Label not found"},
                {"code": "high_missing", "severity": "warning", "message": "High missing values"},
            ],
            label_info=None,
        )

        assert summary.ok is False
        assert len(summary.top_issues) == 2


class TestBrainContext:
    """Test BrainContext schema."""

    def test_valid_context(self):
        """Valid context should parse correctly."""
        ctx = BrainContext(
            run_id="run_20231115_001",
            symbol="NVDA",
            timeframe="1m",
            start_ts="2023-11-01T09:30:00",
            end_ts="2023-11-10T16:00:00",
            available_labels=["ret_10m_fwd", "ret_30m_fwd", "tb_10_20_10"],
            default_label_id="ret_10m_fwd",
            dataset_sanity=None,
        )

        assert ctx.run_id == "run_20231115_001"
        assert ctx.symbol == "NVDA"
        assert len(ctx.available_labels) == 3

    def test_symbol_auto_uppercase(self):
        """Symbol should be automatically uppercased."""
        ctx = BrainContext(
            run_id="run_001",
            symbol="nvda",  # Lowercase
            timeframe="1m",
            start_ts="2023-11-01T09:30:00",
            end_ts="2023-11-10T16:00:00",
            available_labels=["ret_10m_fwd"],
            default_label_id="ret_10m_fwd",
        )

        assert ctx.symbol == "NVDA"  # Auto-uppercased

    def test_empty_timestamp_fails(self):
        """Empty timestamp should fail validation."""
        with pytest.raises(ValidationError) as exc_info:
            BrainContext(
                run_id="run_001",
                symbol="NVDA",
                timeframe="1m",
                start_ts="",  # Empty
                end_ts="2023-11-10T16:00:00",
                available_labels=["ret_10m_fwd"],
                default_label_id="ret_10m_fwd",
            )
        assert "Timestamp cannot be empty" in str(exc_info.value)

    def test_empty_symbol_fails(self):
        """Empty symbol should fail validation."""
        with pytest.raises(ValidationError):
            BrainContext(
                run_id="run_001",
                symbol="",  # Empty
                timeframe="1m",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                available_labels=["ret_10m_fwd"],
                default_label_id="ret_10m_fwd",
            )

    def test_empty_available_labels_fails(self):
        """Empty available_labels should fail validation."""
        with pytest.raises(ValidationError) as exc_info:
            BrainContext(
                run_id="run_001",
                symbol="NVDA",
                timeframe="1m",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                available_labels=[],  # Empty
                default_label_id="ret_10m_fwd",
            )
        assert "available_labels cannot be empty" in str(exc_info.value)

    def test_with_dataset_sanity(self):
        """Context with dataset_sanity should work."""
        ctx = BrainContext(
            run_id="run_001",
            symbol="TSLA",
            timeframe="5s",
            start_ts="2023-11-01T09:30:00",
            end_ts="2023-11-10T16:00:00",
            available_labels=["ret_10m_fwd"],
            default_label_id="ret_10m_fwd",
            dataset_sanity=DatasetSanitySummary(
                ok=True,
                severity="ok",
                n_rows=5000,
                n_cols=15,
            ),
        )

        assert ctx.dataset_sanity is not None
        assert ctx.dataset_sanity.n_rows == 5000


class TestBrainPIParams:
    """Test BrainPIParams schema."""

    def test_valid_pi_params(self):
        """Valid PI params should parse correctly."""
        params = BrainPIParams(
            table="bars_1m",
            symbol="NVDA",
            start_ts="2023-11-01T09:30:00",
            end_ts="2023-11-10T16:00:00",
            goal="Predict 10-minute forward returns",
            label_id="ret_10m_fwd",
            label_horizon_bars=10,
            risk_profile="balanced",
            notes="Using balanced approach for this volatile stock",
        )

        assert params.table == "bars_1m"
        assert params.label_id == "ret_10m_fwd"
        assert params.risk_profile == "balanced"

    def test_minimal_pi_params(self):
        """Minimal PI params (no optional fields) should work."""
        params = BrainPIParams(
            table="bars_1m",
            symbol="AAPL",
            start_ts="2023-11-01T09:30:00",
            end_ts="2023-11-10T16:00:00",
            goal="Test goal",
            label_id="ret_10m_fwd",
        )

        assert params.label_horizon_bars is None
        assert params.risk_profile == "balanced"  # Default
        assert params.notes is None

    def test_empty_field_fails(self):
        """Empty required fields should fail."""
        with pytest.raises(ValidationError):
            BrainPIParams(
                table="",  # Empty
                symbol="NVDA",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                goal="Test",
                label_id="ret_10m_fwd",
            )

    def test_invalid_risk_profile_fails(self):
        """Invalid risk_profile should fail."""
        with pytest.raises(ValidationError):
            BrainPIParams(
                table="bars_1m",
                symbol="NVDA",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                goal="Test",
                label_id="ret_10m_fwd",
                risk_profile="ultra_aggressive",  # Not in Literal
            )

    def test_negative_horizon_fails(self):
        """Negative label_horizon_bars should fail."""
        with pytest.raises(ValidationError) as exc_info:
            BrainPIParams(
                table="bars_1m",
                symbol="NVDA",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                goal="Test",
                label_id="ret_10m_fwd",
                label_horizon_bars=-5,  # Negative
            )
        assert "must be positive" in str(exc_info.value)

    def test_zero_horizon_fails(self):
        """Zero label_horizon_bars should fail."""
        with pytest.raises(ValidationError):
            BrainPIParams(
                table="bars_1m",
                symbol="NVDA",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                goal="Test",
                label_id="ret_10m_fwd",
                label_horizon_bars=0,  # Zero
            )


class TestBrainDecision:
    """Test BrainDecision schema."""

    def test_valid_train_new_model_decision(self):
        """Valid train_new_model decision should parse."""
        decision = BrainDecision(
            version="brain_decision.v1",
            action="train_new_model",
            pi_params=BrainPIParams(
                table="bars_1m",
                symbol="NVDA",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                goal="Predict returns",
                label_id="ret_10m_fwd",
            ),
            rationale="Data quality is good, sufficient historical data available",
        )

        assert decision.action == "train_new_model"
        assert decision.pi_params is not None
        assert decision.pi_params.symbol == "NVDA"

    def test_valid_skip_training_decision(self):
        """Valid skip_training decision should parse."""
        decision = BrainDecision(
            action="skip_training",
            rationale="Dataset sanity check failed with 3 critical errors",
        )

        assert decision.action == "skip_training"
        assert decision.pi_params is None
        assert decision.version == "brain_decision.v1"  # Default

    def test_valid_ask_human_decision(self):
        """Valid ask_human decision should parse."""
        decision = BrainDecision(
            action="ask_human",
            rationale="Ambiguous requirements - need clarification on target metric",
        )

        assert decision.action == "ask_human"

    def test_train_new_model_without_pi_params_fails(self):
        """train_new_model without pi_params should fail."""
        with pytest.raises(ValidationError) as exc_info:
            BrainDecision(
                action="train_new_model",
                pi_params=None,  # Missing
                rationale="Test",
            )
        assert "pi_params is required" in str(exc_info.value)

    def test_empty_rationale_fails(self):
        """Empty rationale should fail."""
        with pytest.raises(ValidationError) as exc_info:
            BrainDecision(
                action="skip_training",
                rationale="",  # Empty
            )
        assert "Rationale cannot be empty" in str(exc_info.value)

    def test_rationale_with_sql_fails(self):
        """Rationale containing SQL should fail."""
        with pytest.raises(ValidationError) as exc_info:
            BrainDecision(
                action="skip_training",
                rationale="SELECT * FROM users; DROP TABLE orders;",
            )
        assert "cannot contain code-like pattern" in str(exc_info.value)

    def test_rationale_with_python_fails(self):
        """Rationale containing Python code should fail."""
        with pytest.raises(ValidationError):
            BrainDecision(
                action="skip_training",
                rationale="import os; os.system('rm -rf /')",
            )

    def test_rationale_with_eval_fails(self):
        """Rationale containing eval should fail."""
        with pytest.raises(ValidationError):
            BrainDecision(
                action="skip_training",
                rationale="Use eval(user_input) to calculate score",
            )

    def test_require_pi_params_success(self):
        """require_pi_params() should return params for train_new_model."""
        decision = BrainDecision(
            action="train_new_model",
            pi_params=BrainPIParams(
                table="bars_1m",
                symbol="NVDA",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                goal="Test",
                label_id="ret_10m_fwd",
            ),
            rationale="Test",
        )

        params = decision.require_pi_params()
        assert params.symbol == "NVDA"

    def test_require_pi_params_fails_for_skip_training(self):
        """require_pi_params() should fail for skip_training."""
        decision = BrainDecision(
            action="skip_training",
            rationale="Test",
        )

        with pytest.raises(ValueError) as exc_info:
            decision.require_pi_params()
        assert "does not contain valid pi_params" in str(exc_info.value)

    def test_invalid_action_fails(self):
        """Invalid action should fail."""
        with pytest.raises(ValidationError):
            BrainDecision(
                action="custom_action",  # Not in Literal
                rationale="Test",
            )

    def test_reuse_existing_model_decision(self):
        """Valid reuse_existing_model decision should parse."""
        decision = BrainDecision(
            action="reuse_existing_model",
            reuse_model_id="model_20231110_nvda_001",
            rationale="Existing model performs well on recent data",
        )

        assert decision.action == "reuse_existing_model"
        assert decision.reuse_model_id == "model_20231110_nvda_001"


class TestParsing:
    """Test parsing from dict (simulating LLM output)."""

    def test_parse_from_dict_train_new_model(self):
        """Should parse train_new_model decision from dict."""
        llm_output = {
            "version": "brain_decision.v1",
            "action": "train_new_model",
            "pi_params": {
                "table": "bars_1m",
                "symbol": "NVDA",
                "start_ts": "2023-11-01T09:30:00",
                "end_ts": "2023-11-10T16:00:00",
                "goal": "Predict 10m returns",
                "label_id": "ret_10m_fwd",
                "label_horizon_bars": 10,
                "risk_profile": "balanced",
            },
            "rationale": "Good data quality, sufficient samples",
        }

        decision = BrainDecision.parse_obj(llm_output)

        assert decision.action == "train_new_model"
        assert decision.pi_params.symbol == "NVDA"
        assert decision.pi_params.label_horizon_bars == 10

    def test_parse_from_dict_skip_training(self):
        """Should parse skip_training decision from dict."""
        llm_output = {
            "action": "skip_training",
            "rationale": "Label distribution is degenerate - all values are zero",
        }

        decision = BrainDecision.parse_obj(llm_output)

        assert decision.action == "skip_training"
        assert "degenerate" in decision.rationale

    def test_parse_invalid_dict_fails(self):
        """Invalid dict should fail parsing."""
        llm_output = {
            "action": "train_new_model",
            # Missing pi_params
            "rationale": "Test",
        }

        with pytest.raises(ValidationError):
            BrainDecision.parse_obj(llm_output)

    def test_dict_round_trip(self):
        """Decision should survive dict round-trip."""
        original = BrainDecision(
            action="train_new_model",
            pi_params=BrainPIParams(
                table="bars_1m",
                symbol="AAPL",
                start_ts="2023-11-01T09:30:00",
                end_ts="2023-11-10T16:00:00",
                goal="Test goal",
                label_id="ret_10m_fwd",
                risk_profile="conservative",
            ),
            rationale="Conservative approach for stable stock",
        )

        # Convert to dict and back
        d = original.dict()
        restored = BrainDecision.parse_obj(d)

        assert restored.action == original.action
        assert restored.pi_params.symbol == original.pi_params.symbol
        assert restored.pi_params.risk_profile == original.pi_params.risk_profile
        assert restored.rationale == original.rationale
