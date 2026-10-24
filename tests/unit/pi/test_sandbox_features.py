# tests/unit/pi/test_sandbox_features.py
"""
Tests for Sandbox Features (UF 20 Part 1).

Tests safe AI-generated feature expression validation.
"""

import pytest
from pi.sandbox_features import (
    SandboxFeature,
    SandboxValidationError,
    validate_sandbox_expr,
    build_sandbox_features_from_recipe,
)


class TestSandboxFeature:
    """Test SandboxFeature dataclass."""

    def test_basic_creation(self):
        """Create basic sandbox feature."""
        sf = SandboxFeature(
            alias="my_feat",
            expr="close - open",
            description="price change",
        )

        assert sf.alias == "my_feat"
        assert sf.expr == "close - open"
        assert sf.source == "sandbox"
        assert sf.approved is True


class TestValidateSandboxExpr:
    """Test sandbox expression validation."""

    def test_valid_simple_expr(self):
        """Valid simple arithmetic expression."""
        sf = validate_sandbox_expr(
            expr="close - open",
            alias="price_change",
            allowed_columns=["close", "open", "high", "low"],
        )

        assert sf.alias == "price_change"
        assert sf.expr == "close - open"
        assert sf.approved is True

    def test_valid_complex_expr(self):
        """Valid complex expression with functions."""
        sf = validate_sandbox_expr(
            expr="log(close / open) + sqrt(abs(high - low))",
            alias="complex_feat",
            allowed_columns=["close", "open", "high", "low"],
        )

        assert sf.alias == "complex_feat"
        assert "log" in sf.expr
        assert "sqrt" in sf.expr

    def test_empty_expr_rejected(self):
        """Empty expression rejected."""
        with pytest.raises(SandboxValidationError, match="empty expression"):
            validate_sandbox_expr(
                expr="",
                alias="bad",
                allowed_columns=["close"],
            )

    def test_too_long_expr_rejected(self):
        """Expression exceeding length limit rejected."""
        long_expr = "close + " * 100 + "open"

        with pytest.raises(SandboxValidationError, match="too long"):
            validate_sandbox_expr(
                expr=long_expr,
                alias="too_long",
                allowed_columns=["close", "open"],
                max_length=256,
            )

    def test_forbidden_token_select(self):
        """Expression with SELECT rejected."""
        with pytest.raises(SandboxValidationError, match="forbidden token"):
            validate_sandbox_expr(
                expr="select close from table",
                alias="bad",
                allowed_columns=["close"],
            )

    def test_forbidden_token_semicolon(self):
        """Expression with semicolon rejected."""
        with pytest.raises(SandboxValidationError, match="forbidden token"):
            validate_sandbox_expr(
                expr="close - open; drop table users",
                alias="injection",
                allowed_columns=["close", "open"],
            )

    def test_forbidden_token_comment(self):
        """Expression with SQL comment rejected."""
        with pytest.raises(SandboxValidationError, match="forbidden token.*--"):
            validate_sandbox_expr(
                expr="close -- this is a comment",
                alias="bad",
                allowed_columns=["close"],
            )

    def test_forbidden_name_pattern_label(self):
        """Expression referencing label column rejected."""
        with pytest.raises(SandboxValidationError, match="matched forbidden pattern"):
            validate_sandbox_expr(
                expr="close + target_label",
                alias="leaking",
                allowed_columns=["close", "target_label"],
            )

    def test_forbidden_name_pattern_future(self):
        """Expression referencing future column rejected."""
        with pytest.raises(SandboxValidationError, match="matched forbidden pattern"):
            validate_sandbox_expr(
                expr="close + future_return",
                alias="leaking",
                allowed_columns=["close", "future_return"],
            )

    def test_unknown_column_rejected(self):
        """Expression using unknown column rejected."""
        with pytest.raises(SandboxValidationError, match="is neither allowed column nor allowed function"):
            validate_sandbox_expr(
                expr="close + unknown_column",
                alias="bad",
                allowed_columns=["close"],
            )

    def test_no_columns_used_rejected(self):
        """Expression not using any columns rejected."""
        with pytest.raises(SandboxValidationError, match="does not reference any allowed columns"):
            validate_sandbox_expr(
                expr="1 + 2",
                alias="constant",
                allowed_columns=["close"],
            )

    def test_allowed_functions_whitelist(self):
        """Custom allowed functions work."""
        sf = validate_sandbox_expr(
            expr="close + custom_func(open)",
            alias="feat",
            allowed_columns=["close", "open"],
            allowed_funcs=["custom_func"],
        )

        assert sf.expr == "close + custom_func(open)"

    def test_custom_forbidden_patterns(self):
        """Custom forbidden patterns work."""
        with pytest.raises(SandboxValidationError, match="matched forbidden pattern"):
            validate_sandbox_expr(
                expr="close + sensitive_col",
                alias="bad",
                allowed_columns=["close", "sensitive_col"],
                forbidden_name_patterns=["sensitive"],
            )


class TestBuildSandboxFeaturesFromRecipe:
    """Test building sandbox features from recipe."""

    def test_empty_recipe_no_sandbox(self):
        """Recipe without sandbox returns empty."""
        sandbox, errors = build_sandbox_features_from_recipe(
            feature_recipe={},
            feature_catalog=["close", "open"],
        )

        assert len(sandbox) == 0
        assert len(errors) == 0

    def test_single_valid_feature(self):
        """Single valid sandbox feature."""
        recipe = {
            "sandbox": [
                {"alias": "price_change", "expr": "close - open"}
            ]
        }

        sandbox, errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=["close", "open", "high", "low"],
        )

        assert len(sandbox) == 1
        assert len(errors) == 0
        assert sandbox[0].alias == "price_change"
        assert sandbox[0].expr == "close - open"

    def test_multiple_valid_features(self):
        """Multiple valid sandbox features."""
        recipe = {
            "sandbox": [
                {"alias": "feat1", "expr": "close - open"},
                {"alias": "feat2", "expr": "high - low"},
                {"alias": "feat3", "expr": "log(close / open)"},
            ]
        }

        sandbox, errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=["close", "open", "high", "low"],
        )

        assert len(sandbox) == 3
        assert len(errors) == 0
        assert {s.alias for s in sandbox} == {"feat1", "feat2", "feat3"}

    def test_too_many_features_rejected(self):
        """Exceeding max_sandbox_features rejected."""
        recipe = {
            "sandbox": [
                {"alias": f"feat{i}", "expr": "close - open"}
                for i in range(10)
            ]
        }

        sandbox, errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=["close", "open"],
            max_sandbox_features=3,
        )

        assert len(sandbox) == 3  # Only first 3 accepted
        assert len(errors) >= 1
        assert any("too_many_sandbox_features" in e["error"] for e in errors)

    def test_invalid_expr_collected_as_error(self):
        """Invalid expression collected as error, doesn't crash."""
        recipe = {
            "sandbox": [
                {"alias": "good", "expr": "close - open"},
                {"alias": "bad", "expr": "select * from table"},
                {"alias": "good2", "expr": "high - low"},
            ]
        }

        sandbox, errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=["close", "open", "high", "low"],
        )

        assert len(sandbox) == 2  # Only 2 valid
        assert len(errors) == 1
        assert errors[0]["alias"] == "bad"
        assert "forbidden token" in errors[0]["error"]

    def test_missing_alias_auto_generated(self):
        """Missing alias auto-generated."""
        recipe = {
            "sandbox": [
                {"expr": "close - open"}
            ]
        }

        sandbox, errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=["close", "open"],
        )

        assert len(sandbox) == 1
        assert sandbox[0].alias == "sandbox_0"

    def test_sandbox_not_list_error(self):
        """Sandbox field not a list returns error."""
        recipe = {
            "sandbox": "invalid_string"
        }

        sandbox, errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=["close"],
        )

        assert len(sandbox) == 0
        assert len(errors) == 1
        assert "not a list" in errors[0]["error"]

    def test_sandbox_item_not_dict_error(self):
        """Sandbox item not a dict returns error."""
        recipe = {
            "sandbox": [
                {"alias": "good", "expr": "close - open"},
                "invalid_string",
            ]
        }

        sandbox, errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=["close", "open"],
        )

        assert len(sandbox) == 1  # Only valid one
        assert len(errors) == 1
        assert "invalid_sandbox_item_type" in errors[0]["error"]
