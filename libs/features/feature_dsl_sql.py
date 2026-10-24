#!/usr/bin/env python3
from __future__ import annotations
"""
Feature DSL → ClickHouse SQL converter.

This module provides a minimal, safe mapping from a restricted JSON DSL
into ClickHouse-compatible SQL expressions over the standard aliases:

  - f.<col>  : 1m baseline features (trading.features_1m)
  - m5.<col> : 5m resampled features (trading.features_5m)
  - d.<col>  : daily/previous-day features (trading.features_1d_prev)

The goal is to let the AI Brain propose *structured* feature definitions
while the host is responsible for producing concrete SQL expressions
that obey engine constraints (no arbitrary functions, no invented columns).

Supported ops (subset, intentionally small and composable):
  - ratio          : num / nullIf(den, eps)
  - ratio_minus_1  : num / nullIf(den, eps) - 1
  - log1p          : log1p(col)
  - product        : col1 * col2
  - sum            : col1 + col2
  - abs            : abs(col)

The DSL is recursive; operands may themselves be nested DSL nodes or
plain strings such as "f.ret_5" or "m5.tick_imbalance".

Example:
    spec = {
        "op": "ratio",
        "num": "f.ret_5",
        "den": {"op": "abs", "col": "f.atr_14"},
        "eps": 1e-6,
    }
    sql = dsl_to_sql(spec)
"""

from typing import Any, Dict
import re


class FeatureDSLCompileError(ValueError):
    """Raised when a DSL spec cannot be translated to SQL."""


# Allowed keys per op for basic schema enforcement. This mimics a JSON
# Schema "additionalProperties: false" contract so that Brain cannot add
# arbitrary fields (e.g. "cols_extra", "columns") without the host noticing.
_OP_ALLOWED_KEYS = {
    "ratio": {"op", "num", "den", "eps"},
    "ratio_minus_1": {"op", "num", "den", "eps"},
    "log1p": {"op", "col"},
    "product": {"op", "cols", "col1", "col2", "num", "den"},  # num/den tolerated for legacy normalization
    "sum": {"op", "cols", "col1", "col2", "num", "den"},       # num/den tolerated for legacy normalization
    "abs": {"op", "col"},
}


def _compile_node(node: Any) -> str:
    """Recursively compile a DSL node into a ClickHouse SQL fragment."""
    # Leaf: strictly validated column reference
    if isinstance(node, str):
        col = node.strip()
        # 防止把 DSL 关键字当成列名泄漏到 SQL 里，例如 "op"、"num" 等
        if col.lower() in {"op", "num", "den", "cols", "col"}:
            raise FeatureDSLCompileError(f"Invalid column reference in DSL leaf: {col}")
        # 目前仅允许 f./m5./d. 前缀的列引用，避免 Brain 臆造裸标识符
        if not re.match(r"^(f|m5|d)\.[A-Za-z0-9_]+$", col):
            raise FeatureDSLCompileError(f"Unsupported DSL leaf string (must be f./m5./d. column): {col}")
        return col
    # Allow numeric literals directly in DSL (e.g. den: 1 or 0.5) by
    # compiling them to a Float64 literal. This avoids raising
    # Unsupported DSL node type for simple constants.
    if isinstance(node, (int, float)):
        return repr(float(node))
    if not isinstance(node, dict):
        raise FeatureDSLCompileError(f"Unsupported DSL node type: {type(node).__name__}")

    op = str(node.get("op") or "").strip()
    if not op:
        raise FeatureDSLCompileError("DSL node missing 'op' field")

    # Backwards compatibility: some older LLM outputs use num/den for sum/product.
    # Normalize them into col1/col2 before schema enforcement to keep strict validation.
    if op in {"sum", "product"} and ("num" in node or "den" in node):
        node = dict(node)  # shallow copy to avoid mutating caller data
        if "col1" not in node and "num" in node:
            node["col1"] = node.pop("num")
        if "col2" not in node and "den" in node:
            node["col2"] = node.pop("den")

    # Enforce a strict per-op key contract so that invented fields from the
    # LLM do not silently pass through. This is the DSL equivalent of a JSON
    # Schema with additionalProperties: false.
    allowed_keys = _OP_ALLOWED_KEYS.get(op)
    if allowed_keys is not None:
        extra = set(node.keys()) - allowed_keys
        if extra:
            raise FeatureDSLCompileError(
                f"Unknown fields for DSL op '{op}': {sorted(extra)}; allowed: {sorted(allowed_keys)}"
            )

    # --- Basic arithmetic-style ops ---
    if op == "ratio":
        num = _compile_node(node.get("num"))
        den = _compile_node(node.get("den"))
        eps = node.get("eps", 1e-6)
        try:
            eps_val = float(eps)
        except Exception:
            eps_val = 1e-6
        return f"({num}) / nullIf({den}, {eps_val})"

    if op == "ratio_minus_1":
        num = _compile_node(node.get("num"))
        den = _compile_node(node.get("den"))
        eps = node.get("eps", 1e-6)
        try:
            eps_val = float(eps)
        except Exception:
            eps_val = 1e-6
        return f"(({num}) / nullIf({den}, {eps_val})) - 1"

    if op == "log1p":
        col = _compile_node(node.get("col"))
        return f"log1p({col})"

    if op == "product":
        # Support both col1/col2 and a generic cols list for products.
        cols = None
        if "cols" in node and node.get("cols") is not None:
            raw = node.get("cols")
            # LLM 可能错误地把 cols 写成 dict；此时必须报错而不是迭代键名。
            if not isinstance(raw, (list, tuple)):
                raise FeatureDSLCompileError(
                    f"product 'cols' must be a list, got {type(raw).__name__}"
                )
            try:
                cols = [_compile_node(c) for c in raw]
            except Exception as exc:
                raise FeatureDSLCompileError(f"Invalid cols for product: {exc}") from exc
        else:
            c1 = node.get("col1")
            c2 = node.get("col2")
            if c1 is None or c2 is None:
                raise FeatureDSLCompileError(
                    "product op requires 'col1'/'col2' or a non-empty 'cols' list"
                )
            cols = [_compile_node(c1), _compile_node(c2)]
        if not cols:
            raise FeatureDSLCompileError("product op received empty 'cols' list")
        if len(cols) == 1:
            return cols[0]
        expr = " * ".join(f"({c})" for c in cols)
        return expr

    if op == "sum":
        # Support both col1/col2 and a generic cols list for sums.
        cols = None
        if "cols" in node and node.get("cols") is not None:
            raw = node.get("cols")
            if not isinstance(raw, (list, tuple)):
                raise FeatureDSLCompileError(
                    f"sum 'cols' must be a list, got {type(raw).__name__}"
                )
            try:
                cols = [_compile_node(c) for c in raw]
            except Exception as exc:
                raise FeatureDSLCompileError(f"Invalid cols for sum: {exc}") from exc
        else:
            c1 = node.get("col1")
            c2 = node.get("col2")
            if c1 is None or c2 is None:
                raise FeatureDSLCompileError(
                    "sum op requires 'col1'/'col2' or a non-empty 'cols' list"
                )
            cols = [_compile_node(c1), _compile_node(c2)]
        if not cols:
            raise FeatureDSLCompileError("sum op received empty 'cols' list")
        if len(cols) == 1:
            return cols[0]
        expr = " + ".join(f"({c})" for c in cols)
        return expr

    if op == "abs":
        col = _compile_node(node.get("col"))
        return f"abs({col})"

    # Future: we can add more ops (e.g., bounded clip, simple lag) once
    # they are proven safe and mapped to known-good ClickHouse idioms.

    raise FeatureDSLCompileError(f"Unknown DSL op: {op}")


def dsl_to_sql(dsl_spec: Dict[str, Any]) -> str:
    """
    Convert a DSL spec into a ClickHouse SQL expression string.

    Args:
        dsl_spec: Dict describing the feature in DSL form.

    Returns:
        SQL expression string suitable for use as the right-hand side in
        config/features_catalog.json candidate specs (the host will wrap
        it as `(expr) AS name` inside the feature view).

    Raises:
        FeatureDSLCompileError if the spec cannot be translated.
    """
    if not isinstance(dsl_spec, dict):
        raise FeatureDSLCompileError("dsl_spec must be a dict")
    return _compile_node(dsl_spec)


__all__ = ["dsl_to_sql", "FeatureDSLCompileError"]
