"""Tests for feature planner."""

import pytest

from features.planner import plan_from_recipe, FeatureMaterializationPlan
from features.registry import FeatureRegistry, FeatureDef


class TestPlanFromRecipe:
    """Test plan_from_recipe function."""

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
            "volume": FeatureDef(
                name="volume",
                kind="native",
                freq=["1m"],
                base_cols=["volume"],
                tags=["volume"],
                desc="Volume",
            ),
            "ret_1b": FeatureDef(
                name="ret_1b",
                kind="python",
                freq=["1m"],
                base_cols=["close"],
                tags=["return"],
                desc="1 bar return",
            ),
            "ma_20_close": FeatureDef(
                name="ma_20_close",
                kind="python",
                freq=["1m"],
                base_cols=["close"],
                tags=["trend"],
                desc="20 bar MA",
            ),
        }
        return FeatureRegistry(features=features, version="1.0")

    def test_native_features_only(self, mock_registry):
        """Test planning with only native features."""
        recipe = {"base_features": ["close", "volume"]}
        available = ["symbol", "ts", "close", "volume", "open", "high", "low"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        assert "close" in plan.base_columns
        assert "volume" in plan.base_columns
        assert len(plan.python_features) == 0
        assert set(plan.final_feature_list) == {"close", "volume"}
        assert len(plan.dropped_features) == 0

    def test_python_features_only(self, mock_registry):
        """Test planning with only Python features."""
        recipe = {"base_features": ["ret_1b", "ma_20_close"]}
        available = ["symbol", "ts", "close", "volume"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        assert "close" in plan.base_columns  # Required for ret_1b and ma_20_close
        assert "ret_1b" in plan.python_features
        assert "ma_20_close" in plan.python_features
        assert "ret_1b" in plan.final_feature_list
        assert "ma_20_close" in plan.final_feature_list

    def test_mixed_features(self, mock_registry):
        """Test planning with mix of native and Python features."""
        recipe = {"base_features": ["close", "volume", "ret_1b"]}
        available = ["symbol", "ts", "close", "volume", "open"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        assert "close" in plan.base_columns
        assert "volume" in plan.base_columns
        assert "ret_1b" in plan.python_features
        assert set(plan.final_feature_list) == {"close", "volume", "ret_1b"}

    def test_derived_features_in_recipe(self, mock_registry):
        """Test planning with derived SQL features."""
        recipe = {
            "base_features": ["close", "volume"],
            "derived": [
                {"alias": "price_vol_ratio", "expr": "close / volume"},
                {"alias": "log_vol", "expr": "log(volume)"},
            ],
        }
        available = ["symbol", "ts", "close", "volume"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        assert "price_vol_ratio" in plan.final_feature_list
        assert "log_vol" in plan.final_feature_list
        assert "close" in plan.final_feature_list
        assert "volume" in plan.final_feature_list

    def test_missing_base_column_drops_python_feature(self, mock_registry):
        """Test that Python features are dropped if base columns missing."""
        recipe = {"base_features": ["ret_1b"]}  # Needs 'close'
        available = ["symbol", "ts", "volume"]  # No 'close'

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        assert "ret_1b" in plan.dropped_features
        assert "ret_1b" not in plan.python_features
        assert "ret_1b" not in plan.final_feature_list

    def test_unknown_feature_dropped(self, mock_registry):
        """Test that unknown features are dropped."""
        recipe = {"base_features": ["close", "unknown_feature", "ret_1b"]}
        available = ["symbol", "ts", "close", "volume"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        assert "unknown_feature" in plan.dropped_features
        assert "unknown_feature" not in plan.final_feature_list
        assert "close" in plan.final_feature_list
        assert "ret_1b" in plan.final_feature_list

    def test_fallback_for_unregistered_feature(self, mock_registry):
        """Test fallback logic for features not in registry."""
        # Feature not in registry but in PYTHON_DERIVED_FEATURES
        recipe = {"base_features": ["close", "vol_20b"]}  # vol_20b not in mock registry
        available = ["symbol", "ts", "close", "volume"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        # vol_20b is in PYTHON_DERIVED_FEATURES, so should be treated as python feature
        assert "vol_20b" in plan.python_features
        assert "vol_20b" in plan.final_feature_list

    def test_empty_recipe(self, mock_registry):
        """Test planning with empty recipe."""
        recipe = {}
        available = ["symbol", "ts", "close", "volume"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        assert len(plan.base_columns) == 0
        assert len(plan.python_features) == 0
        assert len(plan.final_feature_list) == 0
        assert len(plan.dropped_features) == 0

    def test_no_duplicates_in_final_features(self, mock_registry):
        """Test that final feature list has no duplicates."""
        recipe = {
            "base_features": ["close", "close", "ret_1b", "ret_1b"],  # Duplicates
        }
        available = ["symbol", "ts", "close", "volume"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        # Should deduplicate
        assert plan.final_feature_list.count("close") == 1
        assert plan.final_feature_list.count("ret_1b") == 1

    def test_feature_order_preserved(self, mock_registry):
        """Test that feature order is preserved in final list."""
        recipe = {
            "base_features": ["volume", "close", "ret_1b"],
            "derived": [{"alias": "ratio", "expr": "close / volume"}],
        }
        available = ["symbol", "ts", "close", "volume"]

        plan = plan_from_recipe(recipe, mock_registry, available_columns=available)

        # Base columns first, then python features, then derived
        final = plan.final_feature_list
        assert final.index("volume") < final.index("ret_1b")
        assert final.index("close") < final.index("ret_1b")
        assert final.index("ret_1b") < final.index("ratio")


class TestFeatureMaterializationPlan:
    """Test FeatureMaterializationPlan dataclass."""

    def test_plan_creation(self):
        """Test creating a materialization plan."""
        plan = FeatureMaterializationPlan(
            base_columns=["close", "volume"],
            python_features=["ret_1b"],
            final_feature_list=["close", "volume", "ret_1b"],
            dropped_features=["unknown"],
        )

        assert plan.base_columns == ["close", "volume"]
        assert plan.python_features == ["ret_1b"]
        assert plan.final_feature_list == ["close", "volume", "ret_1b"]
        assert plan.dropped_features == ["unknown"]
