# tests/unit/pi/test_schemas.py

"""
Comprehensive tests for PI contract schemas.

Tests cover:
- Valid schema parsing
- Invalid input rejection
- Validator logic (quantiles, model candidates, etc.)
- Edge cases and boundary conditions
"""

import pytest
from pydantic import ValidationError

from pi.schemas import (
    DecisionQuantileConfig,
    DerivedFeatureRef,
    FeatureRecipeV1,
    ModelCandidate,
    PIPlanV1,
    TrainingPlanV1,
)


class TestDerivedFeatureRef:
    """Test DerivedFeatureRef schema."""

    def test_valid_feature_ref(self):
        """Valid feature reference should parse."""
        ref = DerivedFeatureRef(id="ret_1", alias="ret_1")
        assert ref.id == "ret_1"
        assert ref.alias == "ret_1"

    def test_feature_ref_without_alias(self):
        """Feature reference without alias should parse (alias=None)."""
        ref = DerivedFeatureRef(id="rv_20")
        assert ref.id == "rv_20"
        assert ref.alias is None

    def test_missing_id_fails(self):
        """Missing id field should fail validation."""
        with pytest.raises(ValidationError):
            DerivedFeatureRef(alias="some_alias")


class TestFeatureRecipeV1:
    """Test FeatureRecipeV1 schema."""

    def test_valid_feature_recipe(self):
        """Valid feature recipe should parse."""
        recipe = FeatureRecipeV1(
            use_table="bars_1m",
            base_features=["close", "volume", "vwap"],
            derived=[
                DerivedFeatureRef(id="ret_1", alias="ret_1"),
                DerivedFeatureRef(id="rv_20", alias="rv_20"),
            ],
            notes="Test recipe",
        )
        assert recipe.use_table == "bars_1m"
        assert len(recipe.base_features) == 3
        assert len(recipe.derived) == 2
        assert recipe.notes == "Test recipe"

    def test_empty_feature_recipe(self):
        """Empty feature recipe should parse (all fields optional except derived)."""
        recipe = FeatureRecipeV1()
        assert recipe.use_table is None
        assert recipe.base_features == []
        assert recipe.derived == []
        assert recipe.notes is None

    def test_recipe_dict_conversion(self):
        """Feature recipe should convert to dict correctly."""
        recipe = FeatureRecipeV1(
            base_features=["close", "volume"],
            derived=[DerivedFeatureRef(id="ret_1")],
        )
        d = recipe.dict()
        assert d["base_features"] == ["close", "volume"]
        assert len(d["derived"]) == 1
        assert d["derived"][0]["id"] == "ret_1"


class TestDecisionQuantileConfig:
    """Test DecisionQuantileConfig schema and validators."""

    def test_valid_decision_config(self):
        """Valid decision config should parse."""
        config = DecisionQuantileConfig(q_long=0.7, q_short=0.3)
        assert config.mode == "quantile"
        assert config.q_long == 0.7
        assert config.q_short == 0.3

    def test_default_values(self):
        """Default values should be applied."""
        config = DecisionQuantileConfig()
        assert config.mode == "quantile"
        assert config.q_long == 0.7
        assert config.q_short == 0.3

    def test_q_long_out_of_range_fails(self):
        """q_long outside (0, 1) should fail."""
        with pytest.raises(ValidationError) as exc_info:
            DecisionQuantileConfig(q_long=1.5, q_short=0.3)
        assert "must be in (0, 1) range" in str(exc_info.value)

    def test_q_short_out_of_range_fails(self):
        """q_short outside (0, 1) should fail."""
        with pytest.raises(ValidationError) as exc_info:
            DecisionQuantileConfig(q_long=0.7, q_short=-0.1)
        assert "must be in (0, 1) range" in str(exc_info.value)

    def test_q_long_zero_fails(self):
        """q_long = 0 should fail (must be > 0)."""
        with pytest.raises(ValidationError):
            DecisionQuantileConfig(q_long=0.0, q_short=0.3)

    def test_q_long_one_fails(self):
        """q_long = 1 should fail (must be < 1)."""
        with pytest.raises(ValidationError):
            DecisionQuantileConfig(q_long=1.0, q_short=0.3)

    def test_q_short_greater_than_q_long_fails(self):
        """q_short >= q_long should fail."""
        with pytest.raises(ValidationError) as exc_info:
            DecisionQuantileConfig(q_long=0.3, q_short=0.7)
        assert "q_short must be < q_long" in str(exc_info.value)

    def test_q_short_equal_to_q_long_fails(self):
        """q_short == q_long should fail."""
        with pytest.raises(ValidationError):
            DecisionQuantileConfig(q_long=0.5, q_short=0.5)

    def test_boundary_values_pass(self):
        """Values at boundaries should pass."""
        config = DecisionQuantileConfig(q_long=0.99, q_short=0.01)
        assert config.q_long == 0.99
        assert config.q_short == 0.01


class TestModelCandidate:
    """Test ModelCandidate schema."""

    def test_valid_model_candidate(self):
        """Valid model candidate should parse."""
        model = ModelCandidate(
            model_type="xgb",
            model_params={
                "max_depth": 5,
                "n_estimators": 200,
                "learning_rate": 0.05,
            },
        )
        assert model.model_type == "xgb"
        assert model.model_params["max_depth"] == 5

    def test_model_candidate_without_params(self):
        """Model candidate without params should use empty dict."""
        model = ModelCandidate(model_type="lasso")
        assert model.model_type == "lasso"
        assert model.model_params == {}

    def test_missing_model_type_fails(self):
        """Missing model_type should fail."""
        with pytest.raises(ValidationError):
            ModelCandidate(model_params={"max_depth": 5})


class TestTrainingPlanV1:
    """Test TrainingPlanV1 schema."""

    def test_valid_training_plan(self):
        """Valid training plan should parse."""
        plan = TrainingPlanV1(
            symbol="NVDA",
            task="reg",
            label_id="ret_10m_fwd",
            label_horizon_bars=10,
            decision=DecisionQuantileConfig(q_long=0.7, q_short=0.3),
            model_candidates=[
                ModelCandidate(model_type="xgb", model_params={"max_depth": 5})
            ],
            notes="Test plan",
        )
        assert plan.symbol == "NVDA"
        assert plan.task == "reg"
        assert plan.label_id == "ret_10m_fwd"
        assert plan.label_horizon_bars == 10
        assert len(plan.model_candidates) == 1

    def test_minimal_training_plan(self):
        """Minimal training plan should parse with defaults."""
        plan = TrainingPlanV1(symbol="AAPL", label_id="ret_10m_fwd")
        assert plan.symbol == "AAPL"
        assert plan.task == "reg"  # Default
        assert plan.label_id == "ret_10m_fwd"
        assert plan.label_horizon_bars is None  # Will use registry default
        assert isinstance(plan.decision, DecisionQuantileConfig)
        assert len(plan.model_candidates) == 1  # Auto-added default XGBoost

    def test_empty_model_candidates_adds_default_xgb(self):
        """Empty model_candidates should auto-add default XGBoost."""
        plan = TrainingPlanV1(
            symbol="TSLA", label_id="ret_10m_fwd", model_candidates=[]
        )
        assert len(plan.model_candidates) == 1
        assert plan.model_candidates[0].model_type == "xgb"
        assert "max_depth" in plan.model_candidates[0].model_params
        assert plan.model_candidates[0].model_params["max_depth"] == 5

    def test_task_must_be_reg_or_clf(self):
        """task must be 'reg' or 'clf'."""
        with pytest.raises(ValidationError):
            TrainingPlanV1(symbol="AAPL", task="invalid", label_id="ret_10m_fwd")

    def test_classification_task(self):
        """Classification task should parse."""
        plan = TrainingPlanV1(
            symbol="AAPL", task="clf", label_id="tb_10_20_10"  # Triple barrier label
        )
        assert plan.task == "clf"

    def test_missing_symbol_fails(self):
        """Missing symbol should fail."""
        with pytest.raises(ValidationError):
            TrainingPlanV1(label_id="ret_10m_fwd")

    def test_missing_label_id_fails(self):
        """Missing label_id should fail."""
        with pytest.raises(ValidationError):
            TrainingPlanV1(symbol="AAPL")


class TestPIPlanV1:
    """Test PIPlanV1 top-level schema."""

    def test_valid_pi_plan(self):
        """Valid PI plan should parse."""
        plan = PIPlanV1(
            version="pi_plan_v1",
            feature_recipe=FeatureRecipeV1(
                base_features=["close", "volume"],
                derived=[DerivedFeatureRef(id="ret_1", alias="ret_1")],
            ),
            training_plan=TrainingPlanV1(symbol="NVDA", label_id="ret_10m_fwd"),
        )
        assert plan.version == "pi_plan_v1"
        assert len(plan.feature_recipe.base_features) == 2
        assert plan.training_plan.symbol == "NVDA"

    def test_minimal_pi_plan(self):
        """Minimal PI plan should parse with defaults."""
        plan = PIPlanV1(
            feature_recipe=FeatureRecipeV1(),
            training_plan=TrainingPlanV1(symbol="AAPL", label_id="ret_10m_fwd"),
        )
        assert plan.version == "pi_plan_v1"
        assert plan.feature_recipe.base_features == []
        assert plan.training_plan.model_candidates[0].model_type == "xgb"

    def test_parse_from_dict(self):
        """PI plan should parse from dict (LLM output)."""
        llm_output = {
            "version": "pi_plan_v1",
            "feature_recipe": {
                "base_features": ["close", "volume", "vwap"],
                "derived": [
                    {"id": "ret_1", "alias": "ret_1"},
                    {"id": "rv_20", "alias": "rv_20"},
                ],
            },
            "training_plan": {
                "symbol": "NVDA",
                "task": "reg",
                "label_id": "ret_10m_fwd",
                "label_horizon_bars": 10,
                "decision": {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},
                "model_candidates": [
                    {
                        "model_type": "xgb",
                        "model_params": {
                            "max_depth": 5,
                            "n_estimators": 400,
                            "learning_rate": 0.05,
                        },
                    }
                ],
            },
        }

        plan = PIPlanV1.parse_obj(llm_output)
        assert plan.version == "pi_plan_v1"
        assert len(plan.feature_recipe.base_features) == 3
        assert len(plan.feature_recipe.derived) == 2
        assert plan.training_plan.symbol == "NVDA"
        assert plan.training_plan.label_id == "ret_10m_fwd"
        assert len(plan.training_plan.model_candidates) == 1

    def test_invalid_version_fails(self):
        """Invalid version should fail."""
        with pytest.raises(ValidationError):
            PIPlanV1(
                version="wrong_version",
                feature_recipe=FeatureRecipeV1(),
                training_plan=TrainingPlanV1(symbol="AAPL", label_id="ret_10m_fwd"),
            )

    def test_missing_feature_recipe_fails(self):
        """Missing feature_recipe should fail."""
        with pytest.raises(ValidationError):
            PIPlanV1(
                training_plan=TrainingPlanV1(symbol="AAPL", label_id="ret_10m_fwd")
            )

    def test_missing_training_plan_fails(self):
        """Missing training_plan should fail."""
        with pytest.raises(ValidationError):
            PIPlanV1(feature_recipe=FeatureRecipeV1())


class TestComplexScenarios:
    """Test complex real-world scenarios."""

    def test_multi_model_hpo_grid(self):
        """Training plan with multiple model candidates."""
        plan = PIPlanV1(
            feature_recipe=FeatureRecipeV1(base_features=["close", "volume"]),
            training_plan=TrainingPlanV1(
                symbol="NVDA",
                label_id="ret_10m_fwd",
                model_candidates=[
                    ModelCandidate(
                        model_type="xgb",
                        model_params={"max_depth": 5, "n_estimators": 200},
                    ),
                    ModelCandidate(
                        model_type="lightgbm",
                        model_params={"max_depth": 7, "num_leaves": 31},
                    ),
                    ModelCandidate(model_type="lasso", model_params={"alpha": 0.01}),
                ],
            ),
        )
        assert len(plan.training_plan.model_candidates) == 3
        assert plan.training_plan.model_candidates[0].model_type == "xgb"
        assert plan.training_plan.model_candidates[1].model_type == "lightgbm"
        assert plan.training_plan.model_candidates[2].model_type == "lasso"

    def test_large_feature_recipe(self):
        """Feature recipe with many features."""
        plan = PIPlanV1(
            feature_recipe=FeatureRecipeV1(
                base_features=[
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "vwap",
                    "trades",
                ],
                derived=[
                    DerivedFeatureRef(id=f"ret_{i}b", alias=f"ret_{i}b")
                    for i in [1, 5, 10]
                ]
                + [
                    DerivedFeatureRef(id=f"rv_{w}", alias=f"rv_{w}")
                    for w in [10, 20, 50]
                ],
            ),
            training_plan=TrainingPlanV1(symbol="AAPL", label_id="ret_10m_fwd"),
        )
        assert len(plan.feature_recipe.base_features) == 7
        assert len(plan.feature_recipe.derived) == 6

    def test_dict_round_trip(self):
        """PI plan should survive dict round trip."""
        original = PIPlanV1(
            feature_recipe=FeatureRecipeV1(
                base_features=["close", "volume"],
                derived=[DerivedFeatureRef(id="ret_1", alias="ret_1")],
            ),
            training_plan=TrainingPlanV1(
                symbol="NVDA",
                label_id="ret_10m_fwd",
                decision=DecisionQuantileConfig(q_long=0.8, q_short=0.2),
            ),
        )

        # Convert to dict and back
        d = original.dict()
        restored = PIPlanV1.parse_obj(d)

        assert restored.version == original.version
        assert restored.feature_recipe.base_features == original.feature_recipe.base_features
        assert restored.training_plan.symbol == original.training_plan.symbol
        assert (
            restored.training_plan.decision.q_long
            == original.training_plan.decision.q_long
        )


class TestSecurityValidation:
    """Test that schemas prevent dangerous inputs."""

    def test_cannot_inject_sql_in_derived_features(self):
        """Derived features can only have id/alias, not SQL expressions."""
        # This should parse fine (just an ID)
        recipe = FeatureRecipeV1(
            derived=[DerivedFeatureRef(id="ret_1; DROP TABLE users;", alias="evil")]
        )
        # Schema doesn't validate SQL - that's done by FeatureRegistry lookup
        # But the schema ensures no freeform expression field exists
        assert recipe.derived[0].id == "ret_1; DROP TABLE users;"

    def test_cannot_use_arbitrary_label_id(self):
        """label_id is just a string in schema (validated by LabelRegistry later)."""
        # Schema allows any string - validation happens at LabelRegistry lookup
        plan = TrainingPlanV1(symbol="AAPL", label_id="arbitrary_custom_label")
        assert plan.label_id == "arbitrary_custom_label"
        # This will fail later when LabelRegistry.get_spec() is called

    def test_quantile_bounds_enforced(self):
        """Quantile bounds are strictly enforced."""
        # These should all fail
        with pytest.raises(ValidationError):
            DecisionQuantileConfig(q_long=1.5, q_short=0.3)

        with pytest.raises(ValidationError):
            DecisionQuantileConfig(q_long=0.7, q_short=-0.1)

        with pytest.raises(ValidationError):
            DecisionQuantileConfig(q_long=0.3, q_short=0.7)  # Wrong order

    def test_model_params_accepts_any_dict(self):
        """model_params accepts any dict (validated by model later)."""
        model = ModelCandidate(
            model_type="xgb",
            model_params={
                "dangerous_param": "rm -rf /",
                "nested": {"param": "value"},
            },
        )
        # Schema allows it - validation happens at model instantiation
        assert "dangerous_param" in model.model_params
