"""Tests for sandbox feature executor."""

import numpy as np
import pandas as pd
import pytest

from features.sandbox_executor import (
    SandboxExpressionError,
    _validate_expr,
    apply_sandbox_features,
)


class TestValidateExpr:
    """Test expression validation."""

    def test_simple_arithmetic_allowed(self):
        """Test that simple arithmetic is allowed."""
        allowed = {"close", "volume"}

        # Should not raise
        _validate_expr("close + volume", allowed)
        _validate_expr("close * 2", allowed)
        _validate_expr("(close - volume) / 100", allowed)

    def test_allowed_functions(self):
        """Test that allowed functions work."""
        allowed = {"close", "volume"}

        _validate_expr("abs(close)", allowed)
        _validate_expr("log(volume + 1)", allowed)
        _validate_expr("exp(close / 100)", allowed)
        _validate_expr("sqrt(volume)", allowed)

    def test_forbidden_tokens_raise_error(self):
        """Test that forbidden tokens are rejected."""
        allowed = {"close"}

        with pytest.raises(SandboxExpressionError, match="Forbidden token"):
            _validate_expr("__import__('os')", allowed)

        with pytest.raises(SandboxExpressionError, match="Forbidden token"):
            _validate_expr("eval('1+1')", allowed)

        with pytest.raises(SandboxExpressionError, match="Forbidden token"):
            _validate_expr("lambda x: x+1", allowed)

    def test_disallowed_operations_raise_error(self):
        """Test that time-dependent operations are rejected."""
        allowed = {"close"}

        with pytest.raises(SandboxExpressionError, match="Disallowed operation"):
            _validate_expr("close.rolling(10).mean()", allowed)

        with pytest.raises(SandboxExpressionError, match="Disallowed operation"):
            _validate_expr("close.shift(1)", allowed)

        with pytest.raises(SandboxExpressionError, match="Disallowed operation"):
            _validate_expr("close.ewm(span=10).mean()", allowed)

    def test_unknown_identifier_raises_error(self):
        """Test that unknown identifiers are rejected."""
        allowed = {"close"}

        with pytest.raises(SandboxExpressionError, match="Unknown identifier"):
            _validate_expr("close + unknown_column", allowed)

    def test_disallowed_function_raises_error(self):
        """Test that non-whitelisted functions are rejected."""
        allowed = {"close"}

        with pytest.raises(SandboxExpressionError, match="not in allowed list"):
            _validate_expr("sum(close)", allowed)

    def test_invalid_syntax_raises_error(self):
        """Test that invalid syntax is rejected."""
        allowed = {"close"}

        with pytest.raises(SandboxExpressionError, match="Invalid expression syntax"):
            _validate_expr("close +", allowed)


class TestApplySandboxFeatures:
    """Test sandbox feature application."""

    @pytest.fixture
    def sample_df(self):
        """Create sample DataFrame."""
        return pd.DataFrame(
            {
                "close": [100.0, 101.0, 102.0, 103.0, 104.0],
                "volume": [1000, 1100, 1200, 1300, 1400],
                "vwap": [100.5, 101.5, 102.5, 103.5, 104.5],
            }
        )

    def test_apply_simple_arithmetic(self, sample_df):
        """Test applying simple arithmetic expressions."""
        sandbox_defs = [
            {"alias": "close_pct_change", "expr": "(close - 100) / 100"},
            {"alias": "volume_ratio", "expr": "volume / 1000"},
        ]

        result_df, applied = apply_sandbox_features(
            sample_df,
            sandbox_defs,
            available_feature_cols=["close", "volume", "vwap"],
        )

        assert "close_pct_change" in result_df.columns
        assert "volume_ratio" in result_df.columns
        assert len(applied) == 2

        # Check values
        expected_pct = (sample_df["close"] - 100) / 100
        pd.testing.assert_series_equal(
            result_df["close_pct_change"],
            expected_pct,
            check_names=False,
        )

    def test_apply_with_functions(self, sample_df):
        """Test applying expressions with allowed functions."""
        sandbox_defs = [
            {"alias": "log_volume", "expr": "log(volume)"},
            {"alias": "abs_diff", "expr": "abs(close - vwap)"},
        ]

        result_df, applied = apply_sandbox_features(
            sample_df,
            sandbox_defs,
            available_feature_cols=["close", "volume", "vwap"],
        )

        assert "log_volume" in result_df.columns
        assert "abs_diff" in result_df.columns
        assert len(applied) == 2

    def test_chained_features(self, sample_df):
        """Test that features can reference previously created features."""
        sandbox_defs = [
            {"alias": "close_diff", "expr": "close - vwap"},
            {"alias": "close_diff_squared", "expr": "close_diff ** 2"},
        ]

        result_df, applied = apply_sandbox_features(
            sample_df,
            sandbox_defs,
            available_feature_cols=["close", "volume", "vwap"],
        )

        assert "close_diff" in result_df.columns
        assert "close_diff_squared" in result_df.columns
        assert len(applied) == 2

        # Verify chaining worked
        expected_squared = (sample_df["close"] - sample_df["vwap"]) ** 2
        pd.testing.assert_series_equal(
            result_df["close_diff_squared"],
            expected_squared,
            check_names=False,
        )

    def test_invalid_expression_skipped(self, sample_df):
        """Test that invalid expressions don't break the pipeline."""
        sandbox_defs = [
            {"alias": "good_feature", "expr": "close / 100"},
            {"alias": "bad_feature", "expr": "close.rolling(10).mean()"},  # Invalid
            {"alias": "another_good", "expr": "volume / 1000"},
        ]

        result_df, applied = apply_sandbox_features(
            sample_df,
            sandbox_defs,
            available_feature_cols=["close", "volume"],
        )

        # Only good features should be applied
        assert "good_feature" in result_df.columns
        assert "another_good" in result_df.columns
        assert "bad_feature" not in result_df.columns
        assert len(applied) == 2

    def test_runtime_error_skipped(self, sample_df):
        """Test that runtime errors don't break the pipeline."""
        sandbox_defs = [
            {"alias": "sqrt_negative", "expr": "sqrt(close - 200)"},  # Will produce NaN
            {"alias": "good_feature", "expr": "close * 2"},
        ]

        result_df, applied = apply_sandbox_features(
            sample_df,
            sandbox_defs,
            available_feature_cols=["close"],
        )

        # Both features should be applied (sqrt of negative just produces NaN, not an error)
        assert "good_feature" in result_df.columns
        assert len(applied) == 2

    def test_empty_sandbox_defs(self, sample_df):
        """Test that empty sandbox definitions return original df."""
        result_df, applied = apply_sandbox_features(
            sample_df,
            [],
            available_feature_cols=["close", "volume"],
        )

        assert result_df.equals(sample_df)
        assert applied == []

    def test_missing_alias_or_expr(self, sample_df):
        """Test that definitions without alias or expr are skipped."""
        sandbox_defs = [
            {"alias": "good", "expr": "close * 2"},
            {"alias": "missing_expr"},  # No expr
            {"expr": "close * 3"},  # No alias
        ]

        result_df, applied = apply_sandbox_features(
            sample_df,
            sandbox_defs,
            available_feature_cols=["close"],
        )

        assert len(applied) == 1
        assert "good" in result_df.columns

    def test_comparison_operators(self, sample_df):
        """Test that comparison operators work."""
        sandbox_defs = [
            {"alias": "is_high_volume", "expr": "volume > 1200"},
            {"alias": "close_above_vwap", "expr": "close > vwap"},
        ]

        result_df, applied = apply_sandbox_features(
            sample_df,
            sandbox_defs,
            available_feature_cols=["close", "volume", "vwap"],
        )

        assert "is_high_volume" in result_df.columns
        assert "close_above_vwap" in result_df.columns

        # Check boolean values (use == for numpy bool)
        assert result_df["is_high_volume"].iloc[0] == False
        assert result_df["is_high_volume"].iloc[-1] == True

    def test_original_df_unchanged(self, sample_df):
        """Test that original DataFrame is not modified."""
        original_columns = list(sample_df.columns)

        sandbox_defs = [
            {"alias": "new_feature", "expr": "close * 2"},
        ]

        result_df, applied = apply_sandbox_features(
            sample_df,
            sandbox_defs,
            available_feature_cols=["close"],
        )

        # Original DataFrame should be unchanged
        assert list(sample_df.columns) == original_columns
        assert "new_feature" not in sample_df.columns
        assert "new_feature" in result_df.columns
