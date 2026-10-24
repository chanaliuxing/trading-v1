# tests/unit/quality/test_clickhouse_stats.py
"""
Tests for ClickHouse Pre-checks (UF 20 Part 3).

Tests lightweight table statistics before DataFrame fetch.
"""

import pytest
from quality.clickhouse_stats import (
    ColumnStats,
    TableStats,
    build_basic_stats_query,
    quick_table_sanity_from_stats,
)


class TestBuildBasicStatsQuery:
    """Test ClickHouse stats query generation."""

    def test_basic_query_no_symbol(self):
        """Build basic stats query without symbol filter."""
        query = build_basic_stats_query(
            table="trading.bars_1m",
            symbol=None,
            ts_col="ts",
            start_ts="2024-01-01T00:00:00",
            end_ts="2024-01-02T00:00:00",
            numeric_cols=["close", "volume"],
        )

        assert "trading.bars_1m" in query
        assert "COUNT() AS n_rows" in query
        assert "close__nulls" in query
        assert "volume__mean" in query
        assert "ts >= toDateTime64" in query
        assert "ts < toDateTime64" in query
        assert "symbol =" not in query  # No symbol filter

    def test_basic_query_with_symbol(self):
        """Build basic stats query with symbol filter."""
        query = build_basic_stats_query(
            table="trading.bars_1m",
            symbol="AAPL",
            ts_col="ts",
            start_ts="2024-01-01T00:00:00",
            end_ts="2024-01-02T00:00:00",
            numeric_cols=["close"],
        )

        assert "symbol = 'AAPL'" in query

    def test_query_aggregation_functions(self):
        """Query includes all required aggregation functions."""
        query = build_basic_stats_query(
            table="trading.bars_1m",
            symbol=None,
            ts_col="ts",
            start_ts="2024-01-01T00:00:00",
            end_ts="2024-01-02T00:00:00",
            numeric_cols=["price"],
        )

        # Check all required aggregations
        assert "countIf(isNull(price))" in query
        assert "uniqExact(price)" in query
        assert "min(price)" in query
        assert "max(price)" in query
        assert "avg(price)" in query
        assert "stddevPop(price)" in query


class TestColumnStats:
    """Test ColumnStats dataclass."""

    def test_basic_creation(self):
        """Create basic column stats."""
        cs = ColumnStats(
            name="close",
            nulls=10,
            null_ratio=0.01,
            uniq=990,
            min=100.0,
            max=200.0,
            mean=150.0,
            std=15.0,
        )

        assert cs.name == "close"
        assert cs.nulls == 10
        assert cs.null_ratio == 0.01
        assert cs.uniq == 990


class TestTableStats:
    """Test TableStats dataclass."""

    def test_basic_creation(self):
        """Create basic table stats."""
        col_stats = {
            "close": ColumnStats(
                name="close",
                nulls=0,
                null_ratio=0.0,
                uniq=1000,
                min=100.0,
                max=200.0,
                mean=150.0,
                std=15.0,
            )
        }
        ts = TableStats(n_rows=1000, cols=col_stats)

        assert ts.n_rows == 1000
        assert len(ts.cols) == 1
        assert "close" in ts.cols


class TestQuickTableSanityFromStats:
    """Test quick sanity check from TableStats."""

    def test_zero_rows_error(self):
        """Zero rows triggers error."""
        stats = TableStats(n_rows=0, cols={})
        issues = quick_table_sanity_from_stats(stats)

        assert len(issues) == 1
        assert issues[0]["severity"] == "error"
        assert issues[0]["code"] == "ch_zero_rows"

    def test_too_few_rows_warning(self):
        """Too few rows triggers warning."""
        stats = TableStats(
            n_rows=100,
            cols={
                "close": ColumnStats(
                    name="close",
                    nulls=0,
                    null_ratio=0.0,
                    uniq=100,
                    min=100.0,
                    max=200.0,
                    mean=150.0,
                    std=15.0,
                )
            }
        )

        issues = quick_table_sanity_from_stats(stats, min_rows=500)

        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"
        assert issues[0]["code"] == "ch_too_few_rows"
        assert issues[0]["extra"]["n_rows"] == 100

    def test_high_null_ratio_warning(self):
        """High null ratio triggers warning."""
        stats = TableStats(
            n_rows=1000,
            cols={
                "close": ColumnStats(
                    name="close",
                    nulls=600,
                    null_ratio=0.6,
                    uniq=400,
                    min=100.0,
                    max=200.0,
                    mean=150.0,
                    std=15.0,
                )
            }
        )

        issues = quick_table_sanity_from_stats(stats, max_null_ratio=0.5)

        assert any(i["code"] == "ch_high_null_ratio" for i in issues)
        issue = [i for i in issues if i["code"] == "ch_high_null_ratio"][0]
        assert issue["column"] == "close"
        assert issue["extra"]["null_ratio"] == 0.6

    def test_near_zero_variance_warning(self):
        """Near-zero variance triggers warning."""
        stats = TableStats(
            n_rows=1000,
            cols={
                "const_col": ColumnStats(
                    name="const_col",
                    nulls=0,
                    null_ratio=0.0,
                    uniq=1,
                    min=100.0,
                    max=100.0,
                    mean=100.0,
                    std=1e-12,  # Near zero
                )
            }
        )

        issues = quick_table_sanity_from_stats(stats, min_std_threshold=1e-9)

        assert any(i["code"] == "ch_near_zero_var" for i in issues)
        issue = [i for i in issues if i["code"] == "ch_near_zero_var"][0]
        assert issue["column"] == "const_col"

    def test_healthy_stats_no_issues(self):
        """Healthy stats produce no issues."""
        stats = TableStats(
            n_rows=10000,
            cols={
                "close": ColumnStats(
                    name="close",
                    nulls=50,
                    null_ratio=0.005,
                    uniq=9950,
                    min=100.0,
                    max=200.0,
                    mean=150.0,
                    std=15.0,
                ),
                "volume": ColumnStats(
                    name="volume",
                    nulls=0,
                    null_ratio=0.0,
                    uniq=10000,
                    min=1000.0,
                    max=100000.0,
                    mean=50000.0,
                    std=20000.0,
                ),
            }
        )

        issues = quick_table_sanity_from_stats(stats)

        assert len(issues) == 0

    def test_multiple_column_issues(self):
        """Multiple columns with issues."""
        stats = TableStats(
            n_rows=1000,
            cols={
                "col1": ColumnStats(
                    name="col1",
                    nulls=800,
                    null_ratio=0.8,
                    uniq=200,
                    min=1.0,
                    max=10.0,
                    mean=5.0,
                    std=2.0,
                ),
                "col2": ColumnStats(
                    name="col2",
                    nulls=0,
                    null_ratio=0.0,
                    uniq=1,
                    min=42.0,
                    max=42.0,
                    mean=42.0,
                    std=0.0,
                ),
            }
        )

        issues = quick_table_sanity_from_stats(stats)

        # col1 should have high_null_ratio
        # col2 should have near_zero_var
        assert len(issues) >= 2
        codes = {i["code"] for i in issues}
        assert "ch_high_null_ratio" in codes
        assert "ch_near_zero_var" in codes
