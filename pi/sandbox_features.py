# pi/sandbox_features.py
"""
Sandbox Features: Safe AI-generated feature expressions (UF 20 Part 1)

Allows LLM to propose new feature expressions, but must pass machine review:
- Only whitelisted columns
- Only whitelisted functions/operators
- No SQL injection
- No label leakage

功能:
- SandboxFeature: 表示一个合法的 sandbox 特征
- validate_sandbox_expr: 检查 LLM 给的 expr 是否安全
- build_sandbox_features_from_recipe: 从 feature_recipe 里解析/验证 sandbox 特征
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Sequence, Tuple

import re


@dataclass
class SandboxFeature:
    """Safe sandbox feature definition"""
    alias: str
    expr: str
    description: str | None = None
    source: str = "sandbox"   # 标记来源,方便后面审计
    approved: bool = True     # 通过验证的


class SandboxValidationError(Exception):
    """Raised when sandbox expression validation fails"""
    pass


# 允许的函数名（ClickHouse 中常见的一些安全函数）
DEFAULT_ALLOWED_FUNCS = {
    "abs", "log", "log10", "exp", "sqrt",
    "greatest", "least",
    "if", "multiIf",
    "coalesce", "nullIf",
    "toFloat32", "toFloat64",
    "toInt32", "toInt64",
    "round", "floor", "ceil",
    # lagInFrame - 如果你允许窗口,但不建议 Sandbox 用太复杂函数
}


# 禁止出现的关键词/符号（简单防注入）
FORBIDDEN_TOKENS = {
    "select", "from", "join", "union", "insert", "update", "delete",
    "drop", "alter", "create", "truncate",
    ";", "--", "/*", "*/",
}

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate_sandbox_expr(
    expr: str,
    alias: str,
    allowed_columns: Sequence[str],
    *,
    allowed_funcs: Sequence[str] | None = None,
    forbidden_name_patterns: Sequence[str] | None = None,
    max_length: int = 256,
) -> SandboxFeature:
    """
    验证 LLM 提出的 sandbox 表达式是否安全:
    - 长度限制
    - 无危险关键字
    - 标识符要么是允许的列,要么是允许的函数/常量
    - 不触碰 label/future 列（通过名称 pattern 拦截）

    Args:
        expr: Expression string from LLM
        alias: Feature alias/name
        allowed_columns: Whitelisted column names
        allowed_funcs: Whitelisted function names (optional)
        forbidden_name_patterns: Forbidden name patterns (e.g., "target", "label")
        max_length: Maximum expression length

    Returns:
        SandboxFeature if validation passes

    Raises:
        SandboxValidationError: If validation fails
    """

    expr_stripped = expr.strip()
    if not expr_stripped:
        raise SandboxValidationError("empty expression")

    if len(expr_stripped) > max_length:
        raise SandboxValidationError(f"expression too long ({len(expr_stripped)} > {max_length})")

    lowered = expr_stripped.lower()
    for tok in FORBIDDEN_TOKENS:
        if tok in lowered:
            raise SandboxValidationError(f"forbidden token in expression: {tok}")

    # 解析标识符
    allowed_cols_set = set(allowed_columns)
    allowed_funcs_set = set((allowed_funcs or DEFAULT_ALLOWED_FUNCS))

    # 禁止模式,比如包含 "target"/"label"/"fwd"/"future" 的名称
    bad_patterns = list(forbidden_name_patterns or [])
    if not bad_patterns:
        bad_patterns = ["target", "label", "fwd", "future"]

    # 按标识符扫描
    idents = set(IDENT_RE.findall(expr_stripped))
    used_cols: List[str] = []
    used_funcs: List[str] = []

    for ident in idents:
        ident_lower = ident.lower()

        # 禁止名字 pattern
        if any(pat in ident_lower for pat in bad_patterns):
            raise SandboxValidationError(f"identifier {ident} matched forbidden pattern")

        # 是允许的列
        if ident in allowed_cols_set:
            used_cols.append(ident)
            continue

        # 是允许函数
        if ident_lower in allowed_funcs_set:
            used_funcs.append(ident)
            continue

        # 否则,有可能是关键字、常量、运算符等 —— 我们暂时一律禁止,保持保守
        # 如果后面需要支持 AND/OR 等,可以在白名单里加
        raise SandboxValidationError(f"identifier {ident} is neither allowed column nor allowed function")

    if not used_cols:
        raise SandboxValidationError("sandbox expr does not reference any allowed columns")

    return SandboxFeature(
        alias=alias,
        expr=expr_stripped,
        description=None,
        source="sandbox",
        approved=True,
    )


def build_sandbox_features_from_recipe(
    feature_recipe: Dict[str, Any],
    feature_catalog: Sequence[str],
    *,
    max_sandbox_features: int = 5,
    allowed_funcs: Sequence[str] | None = None,
) -> Tuple[List[SandboxFeature], List[Dict[str, Any]]]:
    """
    从 feature_recipe 中解析 sandbox 特征。
    约定:
      feature_recipe["sandbox"] = [
        {"alias": "feat1", "expr": "(close - open) / vwap"},
        ...
      ]

    Args:
        feature_recipe: Feature recipe dict from LLM
        feature_catalog: Whitelisted column names
        max_sandbox_features: Maximum number of sandbox features allowed
        allowed_funcs: Whitelisted function names (optional)

    Returns:
        Tuple of (sandbox_features, errors)
        - sandbox_features: List of validated SandboxFeature
        - errors: List of validation error dicts
    """
    raw_sandbox = feature_recipe.get("sandbox") or []
    if not isinstance(raw_sandbox, list):
        return [], [{
            "alias": None,
            "expr": None,
            "error": "sandbox field is not a list",
        }]

    sandbox_feats: List[SandboxFeature] = []
    errors: List[Dict[str, Any]] = []

    for idx, item in enumerate(raw_sandbox):
        if len(sandbox_feats) >= max_sandbox_features:
            errors.append({
                "alias": None,
                "expr": None,
                "error": f"too_many_sandbox_features>{max_sandbox_features}",
            })
            break

        if not isinstance(item, dict):
            errors.append({
                "alias": None,
                "expr": str(item),
                "error": f"invalid_sandbox_item_type:{type(item)}",
            })
            continue

        alias = (item.get("alias") or f"sandbox_{idx}").strip()
        expr = (item.get("expr") or "").strip()

        if not alias:
            alias = f"sandbox_{idx}"

        try:
            sf = validate_sandbox_expr(
                expr=expr,
                alias=alias,
                allowed_columns=feature_catalog,
                allowed_funcs=allowed_funcs,
            )
            sandbox_feats.append(sf)
        except SandboxValidationError as e:
            errors.append({
                "alias": alias,
                "expr": expr,
                "error": str(e),
            })

    return sandbox_feats, errors
