from __future__ import annotations
import json
import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from libs.db.ch_pool import get_client
from libs.logger import get_logger


logger = get_logger(__name__)


def _brain_tools_dir() -> Path:
    """Return today's brain_tools directory under runs/ai_trader."""
    dstr = date.today().isoformat()
    out_dir = Path("runs/ai_trader") / dstr / "brain_tools"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def inspect_table_schema(table: str, save: bool = True) -> Dict[str, Any]:
    """
    Inspect ClickHouse table schema via DESCRIBE and optionally save to brain_tools.

    Returns:
        {"table": <table>, "columns": [{"name": ..., "type": ...}, ...]}
    """
    cli = get_client()
    rows = cli.query(f"DESCRIBE TABLE {table}")
    cols = [{"name": r.get("name") or r.get("column"), "type": r.get("type")} for r in rows]
    result: Dict[str, Any] = {"table": table, "columns": cols}

    if save:
        out_dir = _brain_tools_dir()
        safe_name = table.replace(".", "_")
        out_path = out_dir / f"inspect_schema_{safe_name}.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        logger.info("brain_tool_inspect_schema_saved", table=table, file=str(out_path))

    return result


def dry_run_feature_expr(
    expr: str,
    sources: Optional[Sequence[str]] = None,
    sample_symbol: str = "TSLA",
    limit: int = 10,
    save: bool = True,
) -> Dict[str, Any]:
    """
    Best-effort dry-run of a feature expression against ClickHouse.

    Builds a minimal SELECT using trading.features_1m as base and optional
    joins to trading.features_5m (m5) and trading.features_1d_prev (d).

    Args:
        expr: Raw SQL expression to evaluate (will be wrapped in parentheses).
        sources: Optional list of required_sources, e.g. ["m5", "d"].
        sample_symbol: Symbol to use for WHERE filter.
        limit: Row limit for the test query.
        save: Whether to persist results under brain_tools.

    Returns:
        {
          "ok": bool,
          "expr": <expr>,
          "table": "trading.features_1m",
          "sources": [...],
          "sample_symbol": "...",
          "limit": <int>,
          "rows_preview": [...],   # when ok
          "error_type": "...",     # when not ok
          "error_message": "...",
        }
    """
    expr_str = (expr or "").strip().rstrip(";")
    srcs = list({s for s in (sources or []) if s})

    # --- PRE-FLIGHT SCHEMA VALIDATION ---
    # 1. Fetch schemas for all required sources
    schemas = {}
    try:
        schemas["f"] = {c["name"] for c in inspect_table_schema("trading.features_1m", save=False)["columns"]}
        if "m5" in srcs:
            schemas["m5"] = {c["name"] for c in inspect_table_schema("trading.features_5m", save=False)["columns"]}
        if "d" in srcs:
            schemas["d"] = {c["name"] for c in inspect_table_schema("trading.features_1d_prev", save=False)["columns"]}
    except Exception as e:
        result = {
            "ok": False, "expr": expr_str, "sources": srcs,
            "error_type": "SchemaFetchError", "error_message": f"Failed to fetch schema: {e}"
        }
        # Save result even on pre-flight failure
        if save:
            _save_dry_run_result(result, expr_str, srcs)
        return result

    # 2. Parse expression and validate columns
    # Regex to find all occurrences of alias.column (e.g., f.close, m5.vwap, d.atr_14)
    # Note: the alias can be "f", "m5", or "d" – we must treat "m5" as a single token,
    # not two separate characters as in the older character-class pattern.
    referenced_cols = re.findall(r'\b(f|m5|d)\.([a-zA-Z0-9_]+)\b', expr_str)
    for alias, col_name in referenced_cols:
        if alias not in schemas:
            # This should not happen if sources are correctly specified
            continue
        if col_name not in schemas[alias]:
            table_map = {"f": "trading.features_1m", "m5": "trading.features_5m", "d": "trading.features_1d_prev"}
            error_msg = f"Validation failed: column '{col_name}' not found for alias '{alias}' in table {table_map.get(alias)}"
            result = {
                "ok": False, "expr": expr_str, "sources": srcs,
                "error_type": "SchemaValidationError", "error_message": error_msg
            }
            if save:
                _save_dry_run_result(result, expr_str, srcs)
            return result
    # --- END VALIDATION ---

    base_table = "trading.features_1m"
    joins_sql = ""
    if "m5" in srcs:
        joins_sql += (
            "\n        LEFT JOIN trading.features_5m AS m5"
            "\n            ON m5.symbol = f.symbol"
            "\n           AND m5.ts = toStartOfInterval(f.ts, INTERVAL 5 MINUTE)"
        )
    if "d" in srcs:
        joins_sql += (
            "\n        LEFT JOIN trading.features_1d_prev AS d"
            "\n            ON d.symbol = f.symbol"
            "\n           AND d.ts = toStartOfDay(f.ts)"
        )

    # Sample from the most recent bars so that warm‑up periods (where many
    # derived features are legitimately NULL) do not cause false
    # feature_spec_dry_run_all_null flags. ORDER BY ts DESC ensures we see
    # the latest, production‑relevant rows.
    sql = f"""
    SELECT
        ({expr_str}) AS value
    FROM {base_table} AS f
    {joins_sql}
    WHERE f.symbol = {{symbol:String}}
    ORDER BY f.ts DESC
    LIMIT {int(limit)}
    """
    cli = get_client()
    result: Dict[str, Any] = {
        "ok": False,
        "expr": expr_str,
        "table": base_table,
        "sources": srcs,
        "sample_symbol": sample_symbol,
        "limit": int(limit),
    }

    try:
        rows = cli.query(sql, {"symbol": sample_symbol})
        result["ok"] = True
        result["rows_preview"] = rows
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_message"] = str(exc)

    if save:
        _save_dry_run_result(result, expr_str, srcs)

    return result

def _save_dry_run_result(result: Dict[str, Any], expr_str: str, srcs: List[str]):
    """Helper to save dry run JSON output."""
    out_dir = _brain_tools_dir()
    h = hashlib.sha256(expr_str.encode("utf-8")).hexdigest()[:16]
    suffix = "_".join(sorted(srcs)) if srcs else "f"
    out_path = out_dir / f"dry_run_{suffix}_{h}.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info(
        "brain_tool_dry_run_saved",
        expr=expr_str[:80],
        sources=srcs,
        file=str(out_path),
        ok=result.get("ok", False),
    )


def diagnose_data_integrity(
    symbols: List[str],
    date: str = "today",
    auto_fix: bool = True,
    min_coverage: float = 0.95,
    save: bool = True,
) -> Dict[str, Any]:
    """
    Check if features_1m_active is properly synced with raw data.

    This tool wraps check_active_completeness.py and provides AI-friendly output.
    Use this when Gateway blocks trades due to 'invalid_features' errors.

    Args:
        symbols: List of symbols to check (e.g., ['TSLA', 'SPY'])
        date: Date to check in YYYY-MM-DD format, or 'today'
        auto_fix: If True, attempts to auto-seed the active table
        min_coverage: Minimum acceptable coverage ratio (0.0-1.0)
        save: Whether to persist results under brain_tools

    Returns:
        {
          "ok": bool,
          "symbols": [...],
          "date": "...",
          "coverage_results": [...],
          "repairs_applied": bool,
          "recommendation": "...",
          "error_message": "..."  # when not ok
        }
    """
    import subprocess
    from datetime import datetime as dt

    # Handle 'today' date
    if date == "today":
        date = dt.now().strftime("%Y-%m-%d")

    symbols_str = ",".join(symbols)

    # Build command
    cmd = [
        "venv/bin/python",
        "tools/check_active_completeness.py",
        "--symbols", symbols_str,
        "--date", date,
        "--min-coverage", str(min_coverage)
    ]

    if auto_fix:
        cmd.append("--auto-seed-active")

    result: Dict[str, Any] = {
        "ok": False,
        "symbols": symbols,
        "date": date,
        "min_coverage": min_coverage,
        "repairs_applied": False,
    }

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120
        )

        # Parse JSON output
        output_json = json.loads(proc.stdout)

        result["ok"] = proc.returncode == 0
        result["coverage_results"] = output_json.get("symbols", [])
        result["repairs_applied"] = auto_fix

        # Generate recommendation
        if result["ok"]:
            result["recommendation"] = "All symbols have ≥{:.0%} coverage. System is healthy.".format(min_coverage)
        else:
            failed = [s for s in result["coverage_results"] if s.get("coverage", 0) < min_coverage]
            if failed:
                result["recommendation"] = f"Coverage below threshold for {len(failed)} symbols. " + \
                    ("Auto-seed was applied but coverage still insufficient. " if auto_fix else "") + \
                    "Check if source table (features_1m) has data, or if bars_1m_ibkr is empty."
            else:
                result["recommendation"] = "Check failed for unknown reason. See error_message."

    except subprocess.TimeoutExpired:
        result["error_message"] = "Command timed out after 120 seconds"
        result["recommendation"] = "ClickHouse may be slow or unresponsive. Check database health."
    except json.JSONDecodeError as e:
        result["error_message"] = f"Failed to parse output: {e}"
        result["recommendation"] = "Tool output was not valid JSON. Check tool logs."
    except Exception as e:
        result["error_message"] = str(e)
        result["recommendation"] = "Unexpected error occurred. Check system logs."

    if save:
        out_dir = _brain_tools_dir()
        out_path = out_dir / f"diagnose_data_integrity_{date}_{hash(symbols_str) % 10000}.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        logger.info("brain_tool_diagnose_saved", date=date, symbols_count=len(symbols), file=str(out_path))

    return result


def check_feature_coverage(
    symbols: List[str],
    days: int = 5,
    table: str = "trading.features_1m_active",
    save: bool = True,
) -> Dict[str, Any]:
    """
    Check which features have NULL values (air columns) in the specified table.

    This tool wraps check_feature_column_coverage.py to identify which specific
    features are causing gateway errors.

    Args:
        symbols: List of symbols to analyze
        days: Number of days to look back
        table: Table to check (features_1m or features_1m_active)
        save: Whether to persist results under brain_tools

    Returns:
        {
          "ok": bool,
          "symbols": [...],
          "days": int,
          "table": "...",
          "coverage_by_feature": {...},  # feature -> {total, non_null, ratio}
          "air_columns": [...],  # features with 0% coverage
          "partial_columns": [...],  # features with <90% coverage
          "recommendation": "...",
          "error_message": "..."  # when not ok
        }
    """
    import subprocess

    symbols_str = ",".join(symbols)

    cmd = [
        "venv/bin/python",
        "tools/check_feature_column_coverage.py",
        "--symbols", symbols_str,
        "--days", str(days),
        "--table", table
    ]

    result: Dict[str, Any] = {
        "ok": False,
        "symbols": symbols,
        "days": days,
        "table": table,
    }

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=180
        )

        # Parse JSON output (tool outputs JSON array of coverage results)
        coverage_data = json.loads(proc.stdout)

        # Analyze results
        air_columns = []
        partial_columns = []
        coverage_by_feature = {}

        for item in coverage_data:
            col = item.get("column")
            ratio = item.get("non_null_ratio", 0)

            if col not in coverage_by_feature:
                coverage_by_feature[col] = {
                    "total": item.get("total", 0),
                    "non_null": item.get("non_null", 0),
                    "ratio": ratio
                }

            if ratio == 0:
                air_columns.append(col)
            elif ratio < 0.9:
                partial_columns.append(col)

        result["ok"] = True
        result["coverage_by_feature"] = coverage_by_feature
        result["air_columns"] = list(set(air_columns))
        result["partial_columns"] = list(set(partial_columns))

        # Generate recommendation
        if air_columns:
            result["recommendation"] = f"Found {len(air_columns)} air columns (0% coverage): {', '.join(air_columns[:5])}... " + \
                "These features are completely missing. Check feature_build pipeline or DSL compilation."
        elif partial_columns:
            result["recommendation"] = f"Found {len(partial_columns)} partially missing columns (<90% coverage). " + \
                "May indicate sporadic feature calculation failures."
        else:
            result["recommendation"] = "All features have ≥90% coverage. Feature quality is good."

    except subprocess.TimeoutExpired:
        result["error_message"] = "Command timed out after 180 seconds"
        result["recommendation"] = "Large date range or slow ClickHouse. Try reducing --days parameter."
    except json.JSONDecodeError as e:
        result["error_message"] = f"Failed to parse output: {e}"
        result["recommendation"] = "Tool output was not valid JSON. Check tool logs."
    except Exception as e:
        result["error_message"] = str(e)
        result["recommendation"] = "Unexpected error occurred. Check system logs."

    if save:
        out_dir = _brain_tools_dir()
        out_path = out_dir / f"check_feature_coverage_{hash(symbols_str) % 10000}_{days}d.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        logger.info("brain_tool_feature_coverage_saved", symbols_count=len(symbols), days=days, file=str(out_path))

    return result


if __name__ == "__main__":
    # Simple CLI for manual testing:
    import argparse

    ap = argparse.ArgumentParser(description="Brain SQL tools (schema / dry-run helpers)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_schema = sub.add_parser("schema", help="Inspect ClickHouse table schema")
    p_schema.add_argument("table", help="Table name, e.g. trading.features_1m")

    p_dry = sub.add_parser("dry_run", help="Dry-run a feature expression")
    p_dry.add_argument("expr", help="Raw SQL expression to evaluate")
    p_dry.add_argument("--sources", nargs="*", default=[], help="Required sources, e.g. m5 d")
    p_dry.add_argument("--symbol", default="TSLA", help="Sample symbol filter (default: TSLA)")
    p_dry.add_argument("--limit", type=int, default=10, help="Row limit (default: 10)")

    p_diagnose = sub.add_parser("diagnose", help="Diagnose data integrity issues")
    p_diagnose.add_argument("--symbols", nargs="+", required=True, help="Symbols to check")
    p_diagnose.add_argument("--date", default="today", help="Date to check (YYYY-MM-DD or 'today')")
    p_diagnose.add_argument("--auto-fix", action="store_true", help="Auto-seed active table if needed")

    p_coverage = sub.add_parser("coverage", help="Check feature coverage (air columns)")
    p_coverage.add_argument("--symbols", nargs="+", required=True, help="Symbols to check")
    p_coverage.add_argument("--days", type=int, default=5, help="Days to look back")
    p_coverage.add_argument("--table", default="trading.features_1m_active", help="Table to check")

    args = ap.parse_args()

    if args.cmd == "schema":
        out = inspect_table_schema(args.table, save=True)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif args.cmd == "dry_run":
        out = dry_run_feature_expr(
            args.expr,
            sources=args.sources,
            sample_symbol=args.symbol,
            limit=args.limit,
            save=True,
        )
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif args.cmd == "diagnose":
        out = diagnose_data_integrity(
            symbols=args.symbols,
            date=args.date,
            auto_fix=args.auto_fix,
            save=True,
        )
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif args.cmd == "coverage":
        out = check_feature_coverage(
            symbols=args.symbols,
            days=args.days,
            table=args.table,
            save=True,
        )
        print(json.dumps(out, indent=2, ensure_ascii=False))
