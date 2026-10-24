# features/sandbox_executor.py

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set, Tuple

import ast
import pandas as pd


# 允许的 AST 节点类型（只允许四则运算、括号等简单表达式）
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Load,
    ast.Name,
    ast.Constant,
    ast.Call,
    ast.Compare,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.Eq,
    ast.NotEq,
    ast.BoolOp,
    ast.And,
    ast.Or,
)

# 允许的函数（如果你希望用 log / exp 等，可以在这里加）
_ALLOWED_FUNCS: Set[str] = {
    "abs",
    "log",
    "exp",
    "sqrt",
    "clip",
    "min",
    "max",
}

# 明确禁止的关键词（保险起见）
_FORBIDDEN_TOKENS: Tuple[str, ...] = (
    "__",
    "import",
    "os",
    "sys",
    "eval",
    "exec",
    "subprocess",
    "open",
    "pd",
    "np",
    "lambda",
)


class SandboxExpressionError(ValueError):
    """Raised when sandbox expression validation fails."""
    pass


def _validate_expr(expr: str, allowed_names: Set[str]) -> None:
    """
    用 Python AST 对表达式进行静态检查：
    - 只允许 _ALLOWED_NODES 中的节点
    - 所有 Name 必须在 allowed_names 或 _ALLOWED_FUNCS 列表里
    - 禁止出现一些危险关键词
    - 禁止出现 rolling/ewm/shift/lead/lag 等（避免时间泄露）

    Args:
        expr: Expression string to validate
        allowed_names: Set of allowed variable names

    Raises:
        SandboxExpressionError: If expression is invalid or unsafe
    """
    # Check for forbidden tokens (use word boundaries to avoid false positives like "close" containing "os")
    import re
    for token in _FORBIDDEN_TOKENS:
        # Skip "__" check for special pattern
        if token == "__":
            if "__" in expr:
                raise SandboxExpressionError(f"Forbidden token in expression: {token}")
        else:
            # Use word boundary check for other tokens
            pattern = r'\b' + re.escape(token) + r'\b'
            if re.search(pattern, expr):
                raise SandboxExpressionError(f"Forbidden token in expression: {token}")

    lower_expr = expr.lower()
    for bad in ("rolling", "ewm", "shift", "lead", "lag", "groupby", "resample"):
        if bad in lower_expr:
            raise SandboxExpressionError(f"Disallowed operation in sandbox expression: {bad}")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise SandboxExpressionError(f"Invalid expression syntax: {exc}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise SandboxExpressionError(f"Disallowed AST node type: {type(node).__name__}")

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise SandboxExpressionError("Only simple function calls allowed (e.g. log(x))")
            if node.func.id not in _ALLOWED_FUNCS:
                raise SandboxExpressionError(f"Function {node.func.id} not in allowed list")

        if isinstance(node, ast.Name):
            if node.id not in allowed_names and node.id not in _ALLOWED_FUNCS:
                raise SandboxExpressionError(f"Unknown identifier in expression: {node.id}")


def apply_sandbox_features(
    df: pd.DataFrame,
    sandbox_defs: Iterable[Dict[str, Any]],
    *,
    available_feature_cols: Iterable[str],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """
    在给定 DataFrame 上安全地计算 sandbox_derived 特征。

    Args:
        df: Already loaded features DataFrame
        sandbox_defs: 来自 feature_recipe["sandbox_derived"]
            形如: {"alias": "...", "expr": "..."}
        available_feature_cols: 当前 df 中可使用的列名（base + 传统 derived）

    Returns:
        Tuple of:
            - new_df: DataFrame with sandbox columns added
            - applied: List of successfully applied sandbox features (for audit)

    Design:
        - Validates expressions using AST before execution
        - Uses pandas.eval() for safe vectorized computation
        - Failed features don't break the pipeline
        - Each successful feature becomes available to subsequent features
    """
    if not sandbox_defs:
        return df, []

    allowed_names = set(available_feature_cols)
    applied: List[Dict[str, Any]] = []
    new_df = df.copy()

    for item in sandbox_defs:
        alias = item.get("alias")
        expr = item.get("expr")

        if not alias or not expr:
            continue

        # 1) 静态检查表达式安全性
        try:
            _validate_expr(expr, allowed_names)
        except SandboxExpressionError as exc:
            # Record error but don't break pipeline
            item_copy = dict(item)
            item_copy["error"] = str(exc)
            continue

        # 2) 利用 pandas.eval 对整个列向量进行计算
        #    这里不允许引用 df/np/pd，只能用列名和 _ALLOWED_FUNCS 里列出的函数
        try:
            new_df[alias] = new_df.eval(expr)
        except Exception as exc:
            # 如果某个 sandbox 特征失败，不要中断整个流程，记录一下就行
            item_copy = dict(item)
            item_copy["error"] = str(exc)
            continue

        applied.append(item)

        # 新增列也加入 allowed_names，后续 sandbox 表达式可以引用已成功前面的 alias
        allowed_names.add(alias)

    return new_df, applied
