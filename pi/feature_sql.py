"""Generate ClickHouse SQL from feature recipes with guardrails."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_ALLOWED_FUNCS = {
    "abs",
    "log",
    "exp",
    "pow",
    "least",
    "greatest",
    "lag",
    "lead",
    "coalesce",
    "nullif",
    "if",
    "caseWithExpression",
    "toFloat64",
    "toFloat32",
    "toInt32",
    "toDateTime",
    "toUnixTimestamp",
    "round",
    "roundBankers",
}

_ALLOWED_TOKENS = set("+-*/()=<>!%,")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_ident(token: str) -> bool:
    return bool(_IDENT.match(token))


def _sanitize_expr(expr: str, feature_catalog: List[str]) -> str:
    if ";" in expr or "--" in expr or "/*" in expr:
        raise ValueError("Expression contains forbidden tokens")
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+\.\d+|\d+|.", expr)
    out = []
    for tok in tokens:
        if tok.isspace():
            out.append(tok)
            continue
        if re.fullmatch(r"\d+\.\d+|\d+", tok):
            out.append(tok)
            continue
        if tok in _ALLOWED_TOKENS:
            out.append(tok)
            continue
        if tok.lower() in _ALLOWED_FUNCS:
            out.append(tok)
            continue
        if _is_ident(tok) and (tok in feature_catalog or tok.lower() in {"null", "nan"}):
            out.append(tok)
            continue
        if _is_ident(tok):  # allow aliases or column references to be added later
            out.append(tok)
            continue
        raise ValueError(f"Illegal token in expression: {tok}")
    return "".join(out)


def build_select_sql(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    target: Dict[str, Any] | None,
    feature_catalog: List[str],
    ts_col: str = "ts",
    symbol_col: str = "symbol",
    cutoff_col: str = "feature_cutoff_ts",
) -> str:
    base = recipe.get("base_features", []) or []
    derived = recipe.get("derived", []) or []
    filters = recipe.get("filters", []) or []

    select_cols: List[str] = [symbol_col, ts_col] + base

    # Derived features first
    for d in derived:
        alias = d["alias"]
        expr = _sanitize_expr(d["expr"], feature_catalog + base)
        select_cols.append(f"{expr} AS {alias}")
        base.append(alias)

    if target:
        t_alias = target.get("alias", "y")
        t_expr = _sanitize_expr(target.get("expr", ""), feature_catalog + base)
        select_cols.append(f"{t_expr} AS {t_alias}")

    where_clauses = [
        f"{symbol_col} = '{symbol}'",
        f"{ts_col} >= toDateTime('{start_ts}')",
        f"{ts_col} < toDateTime('{end_ts}')",
    ]
    if cutoff_col:
        where_clauses.append(f"{ts_col} >= {cutoff_col}")
    for f in filters:
        where_clauses.append(_sanitize_expr(f, feature_catalog + base))

    select_clause = ", ".join(select_cols)
    where_clause = " AND ".join(where_clauses)
    sql = f"SELECT {select_clause} FROM {table} WHERE {where_clause} ORDER BY {ts_col} ASC"
    return sql


def build_select_sql_safe(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    label_id: str,
    feature_catalog: List[str],
    python_derived_features: Optional[List[str]] = None,  # NEW
) -> str:
    """
    安全版 SQL 构造器:

    - 只选: label 需要的基础列 + base_features 中真实存在于表里的列 + 派生特征 expr
    - 对于 Python 衍生特征（例如 ret_1b / rvol_20 等），只在 Python 层计算，不写进 SQL
    - 完全禁止 LLM 提供任何 target 表达式（label 统一在 Python 层计算）
    """
    # Import here to avoid circular dependency
    # Get required columns for label from Label Registry (UF 16)
    from pathlib import Path
    from labels.label_engine import LabelRegistry

    python_derived_set = set(python_derived_features or [])

    # 1) label 需要的基础列（symbol/ts/close 等）
    label_registry_path = Path("config/label_registry.json")
    if label_registry_path.exists():
        try:
            label_registry = LabelRegistry(label_registry_path)
            label_spec = label_registry.get(label_id)
            # Labels need OHLC for both forward_return and triple_barrier
            required_cols = set(["symbol", "ts", "close", "high", "low"])
        except (FileNotFoundError, KeyError):
            # Fallback to old contract-based approach
            from contracts.labels import get_label_required_columns
            required_cols = set(get_label_required_columns(label_id))
    else:
        from contracts.labels import get_label_required_columns
        required_cols = set(get_label_required_columns(label_id))

    # 2) LLM 提供的 base_features:
    #    - 在表中存在的列: 加入 required_cols
    #    - 在 python_derived_set 中的: 不进 SQL，由 Python 计算
    #    - 其他不存在的列: 静默丢弃（可以以后加日志）
    base_features = recipe.get("base_features", []) or []
    for col in base_features:
        if col in feature_catalog:
            required_cols.add(col)
        elif col in python_derived_set:
            # Python 衍生特征，不写进 SQL
            continue
        else:
            # 既不是真实列也不是 Python 衍生特征: 丢弃
            continue

    # 3) 派生特征（LLM 给 SQL expr，只作为 SELECT 表达式，不当 target）
    derived_parts: List[str] = []
    for d in recipe.get("derived", []) or []:
        expr = d.get("expr")
        alias = d.get("alias")
        if not expr or not alias:
            continue
        # 这里默认 expr 已经过了你上游的 prompt 约束，只使用本表列，且是纯函数
        sanitized_expr = _sanitize_expr(expr, feature_catalog + list(required_cols))
        derived_parts.append(f"({sanitized_expr}) AS {alias}")

    # 4) 基础列 SELECT 片段
    base_select_cols = sorted(required_cols)
    base_select_sql = ", ".join(base_select_cols)

    select_parts = [base_select_sql]
    if derived_parts:
        select_parts.extend(derived_parts)

    select_clause = ", ".join(select_parts)

    sql = f"""
        SELECT
            {select_clause}
        FROM {table}
        WHERE symbol = %(symbol)s
          AND ts >= %(start_ts)s
          AND ts < %(end_ts)s
        ORDER BY ts ASC
    """

    return sql.strip()

