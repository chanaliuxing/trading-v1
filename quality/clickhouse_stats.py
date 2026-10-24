# quality/clickhouse_stats.py
"""
ClickHouse Pre-check Layer (UF 20 Part 3)

Lightweight table statistics collection before fetching full DataFrame.

功能:
- collect_basic_stats: 在 ClickHouse 端统计基础信息
- quick_table_sanity_from_stats: 基于 TableStats 做预体检
- Prevent pulling large datasets if basic sanity fails

Design goal: Fail fast before expensive operations
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

try:
    import clickhouse_connect
except ImportError:
    clickhouse_connect = None  # type: ignore


@dataclass
class ColumnStats:
    """Basic column statistics from ClickHouse aggregation"""
    name: str
    nulls: int
    null_ratio: float
    uniq: int
    min: float | None
    max: float | None
    mean: float | None
    std: float | None


@dataclass
class TableStats:
    """Table-level statistics"""
    n_rows: int
    cols: Dict[str, ColumnStats]


def build_basic_stats_query(
    table: str,
    *,
    symbol: Optional[str],
    ts_col: str,
    start_ts: str,
    end_ts: str,
    numeric_cols: Iterable[str],
) -> str:
    """
    在 ClickHouse 端生成一条聚合查询,统计基础信息。
    只针对基础列（不含派生表达式）,以减少复杂度。

    Args:
        table: Table name
        symbol: Optional symbol filter
        ts_col: Timestamp column name
        start_ts: Start timestamp (ISO format)
        end_ts: End timestamp (ISO format)
        numeric_cols: Numeric columns to analyze

    Returns:
        ClickHouse SQL query string
    """
    where_clauses = [
        f"{ts_col} >= toDateTime64('{start_ts}', 3)",
        f"{ts_col} < toDateTime64('{end_ts}', 3)"
    ]
    if symbol is not None:
        where_clauses.append(f"symbol = '{symbol}'")

    where_sql = " AND ".join(where_clauses)

    parts = ["COUNT() AS n_rows"]
    for col in numeric_cols:
        safe = col  # TODO: 如果有特殊字符可加反引号
        parts.extend([
            f"countIf(isNull({safe})) AS `{col}__nulls`",
            f"uniqExact({safe}) AS `{col}__uniq`",
            f"min({safe}) AS `{col}__min`",
            f"max({safe}) AS `{col}__max`",
            f"avg({safe}) AS `{col}__mean`",
            f"stddevPop({safe}) AS `{col}__std`",
        ])

    select_sql = ",\n  ".join(parts)

    q = f"""
    SELECT
      {select_sql}
    FROM {table}
    WHERE {where_sql}
    """
    return q.strip()


def collect_basic_stats(
    client: Any,  # clickhouse_connect.driver.Client
    table: str,
    *,
    symbol: Optional[str],
    ts_col: str,
    start_ts: str,
    end_ts: str,
    numeric_cols: Iterable[str],
) -> TableStats:
    """
    在 ClickHouse 端执行基础体检查询。

    Args:
        client: ClickHouse client instance
        table: Table name
        symbol: Optional symbol filter
        ts_col: Timestamp column name
        start_ts: Start timestamp (ISO format)
        end_ts: End timestamp (ISO format)
        numeric_cols: Numeric columns to analyze

    Returns:
        TableStats with basic statistics
    """
    q = build_basic_stats_query(
        table=table,
        symbol=symbol,
        ts_col=ts_col,
        start_ts=start_ts,
        end_ts=end_ts,
        numeric_cols=numeric_cols,
    )
    df = client.query_df(q)
    if df.empty:
        return TableStats(n_rows=0, cols={})

    row = df.iloc[0]
    n_rows = int(row["n_rows"])

    cols_stats: Dict[str, ColumnStats] = {}
    for col in numeric_cols:
        nulls = int(row.get(f"{col}__nulls", 0))
        uniq = int(row.get(f"{col}__uniq", 0))
        col_min = row.get(f"{col}__min")
        col_max = row.get(f"{col}__max")
        mean = row.get(f"{col}__mean")
        std = row.get(f"{col}__std")

        null_ratio = nulls / max(1, n_rows)

        cols_stats[col] = ColumnStats(
            name=col,
            nulls=nulls,
            null_ratio=null_ratio,
            uniq=uniq,
            min=float(col_min) if col_min is not None else None,
            max=float(col_max) if col_max is not None else None,
            mean=float(mean) if mean is not None else None,
            std=float(std) if std is not None else None,
        )

    return TableStats(n_rows=n_rows, cols=cols_stats)


def quick_table_sanity_from_stats(
    stats: TableStats,
    *,
    min_rows: int = 500,
    max_null_ratio: float = 0.5,
    min_std_threshold: float = 1e-9,
) -> List[Dict[str, Any]]:
    """
    输出简单的 issue 列表（用 dict,便于和现有 SanityIssue 结构拼接）。

    Args:
        stats: TableStats from ClickHouse
        min_rows: Minimum required rows
        max_null_ratio: Maximum allowed null ratio
        min_std_threshold: Minimum standard deviation threshold

    Returns:
        List of issue dicts with keys: severity, code, message, column (optional), extra (optional)
    """
    issues: List[Dict[str, Any]] = []

    if stats.n_rows == 0:
        issues.append({
            "severity": "error",
            "code": "ch_zero_rows",
            "message": "ClickHouse stats: filtered dataset has zero rows.",
        })
        return issues

    if stats.n_rows < min_rows:
        issues.append({
            "severity": "warning",
            "code": "ch_too_few_rows",
            "message": f"ClickHouse stats: n_rows={stats.n_rows} < min_rows={min_rows}.",
            "extra": {"n_rows": stats.n_rows, "min_rows": min_rows},
        })

    for col, cs in stats.cols.items():
        if cs.null_ratio > max_null_ratio:
            issues.append({
                "severity": "warning",
                "code": "ch_high_null_ratio",
                "message": (
                    f"Column {col!r} null_ratio={cs.null_ratio:.2%} > "
                    f"allowed {max_null_ratio:.2%} (ClickHouse stats)."
                ),
                "column": col,
                "extra": {"null_ratio": cs.null_ratio},
            })

        # 零方差 / 非变化列
        if cs.std is not None and cs.std < min_std_threshold:
            issues.append({
                "severity": "warning",
                "code": "ch_near_zero_var",
                "message": (
                    f"Column {col!r} std={cs.std:.3e} (≈0), "
                    "may be near-constant (ClickHouse stats)."
                ),
                "column": col,
                "extra": {"std": cs.std},
            })

    return issues
