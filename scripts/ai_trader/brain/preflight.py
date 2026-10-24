#!/usr/bin/env python3
"""
PreFlight Validation - Training readiness checks

Validates that all required data dependencies are met before ML training starts.
Prevents wasted compute and ensures data quality.

Usage:
    from brain.preflight import preflight_check_all

    ready, report = preflight_check_all(symbols=["TSLA", "AAPL"])
    if not ready:
        logger.error("preflight_failed", report=report)
        return
"""
import sys
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from datetime import datetime, timedelta, timezone, date
import time
import os
import subprocess
import json

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from libs.db.ch_pool import get_client
from libs.utils.market_calendar import is_market_closed
import structlog

logger = structlog.get_logger(__name__)


def preflight_check_labels(
    symbols: List[str],
    horizons: List[int] = [30],
    min_rows_per_symbol: int = 1000,
    max_null_ratio: float = 0.1,
    max_age_hours: int = 48,
    lookback_days: int = 90,
    labels_table: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Check if labels are ready for training

    Args:
        symbols: List of symbols to check
        horizons: List of label horizons (e.g., [30] for 30-minute forward returns)
        min_rows_per_symbol: Minimum rows required per symbol
        max_null_ratio: Maximum allowed ratio of NULL label values
        max_age_hours: Maximum age of labels in hours
        lookback_days: Time window for coverage check (days), should match training window

    Returns:
        (ready: bool, details: dict)

        ready=True means labels are ready for training
        ready=False means blocking issues exist

    Example:
        >>> ready, details = preflight_check_labels(["TSLA", "AAPL"], lookback_days=30)
        >>> if not ready:
        >>>     print(f"Missing symbols: {details['missing_symbols']}")
    """
    client = get_client()

    # Allow env override for null ratio (e.g., MAX_NULL_RATIO=0.3)
    try:
        env_max_null = os.getenv("MAX_NULL_RATIO")
        if env_max_null is not None:
            max_null_ratio = float(env_max_null)
    except Exception:
        pass

    # Allow env override for null ratio (e.g., MAX_NULL_RATIO=0.3)
    try:
        env_max_null = os.getenv("MAX_NULL_RATIO")
        if env_max_null is not None:
            max_null_ratio = float(env_max_null)
    except Exception:
        pass
    relax_max_null_ratio = None
    try:
        env_relax = os.getenv("RELAX_MAX_NULL_RATIO")
        if env_relax is not None:
            relax_max_null_ratio = float(env_relax)
    except Exception:
        relax_max_null_ratio = None

    # Require explicit table to avoid wrong defaults for non-1m setups
    if not labels_table:
        return False, {
            "status": "table_missing",
            "message": "labels_table not provided to preflight_check_labels",
            "missing_symbols": symbols,
            "total_rows": 0
        }

    # Build label column names
    label_cols = [f"label_h{h}" for h in horizons]

    # Check table exists
    try:
        table_exists = client.query(f"""
            SELECT count() as cnt
            FROM system.tables
            WHERE database='trading' AND name='{labels_table.split('.')[-1]}'
        """, settings={'use_query_cache': False})

        if not table_exists or table_exists[0]['cnt'] == 0:
            return False, {
                "status": "table_missing",
                "message": f"Table {labels_table} does not exist",
                "missing_symbols": symbols,
                "total_rows": 0
            }
    except Exception as e:
        logger.error("preflight_table_check_failed", error=str(e))
        return False, {
            "status": "error",
            "message": f"Failed to check table: {str(e)}",
            "missing_symbols": symbols
        }

    # Check per-symbol stats
    by_symbol = {}
    missing_symbols = []
    low_coverage_symbols = []
    stale_symbols = []
    high_null_symbols = []
    warning_high_null_symbols = []

    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    if end:
        try:
            cutoff_time = datetime.fromisoformat(str(end).replace(" ", "T"))
        except Exception:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    start_filter = ""
    end_filter = ""
    params_extra: Dict[str, Any] = {}
    if start:
        try:
            params_extra["start"] = datetime.fromisoformat(str(start).replace(" ", "T"))
            start_filter = "AND toDate(ts) >= toDate(%(start)s)"
        except Exception:
            start_filter = ""
    if end:
        try:
            params_extra["end"] = datetime.fromisoformat(str(end).replace(" ", "T"))
            end_filter = "AND toDate(ts) <= toDate(%(end)s)"
        except Exception:
            end_filter = ""
    if end:
        try:
            cutoff_time = datetime.fromisoformat(str(end).replace(" ", "T"))
        except Exception:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    if end:
        try:
            cutoff_time = datetime.fromisoformat(str(end).replace(" ", "T"))
        except Exception:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    if start:
        try:
            cutoff_date = datetime.fromisoformat(str(start).replace(" ", "T")).date().isoformat()
        except Exception:
            pass

    for symbol in symbols:
        try:
            # Count rows and check freshness within the training window (lookback_days)
            ts_col = "ts"
            stats_query = f"""
                SELECT
                    count() as total_rows,
                    max({ts_col}) as latest_ts,
                    countIf({label_cols[0]} IS NULL) as null_count
                FROM {labels_table}
                WHERE symbol = %(symbol)s
                  AND toDate({ts_col}) >= %(cutoff_date)s
                  AND toHour({ts_col}) >= 9 AND toHour({ts_col}) < 16
            """

            result = client.query(stats_query, {"symbol": symbol, "cutoff_date": cutoff_date}, settings={'use_query_cache': False})

            if not result or result[0]['total_rows'] == 0:
                missing_symbols.append(symbol)
                by_symbol[symbol] = {
                    "status": "missing",
                    "rows": 0,
                    "null_ratio": 1.0,
                    "age_hours": None
                }
                continue

            row = result[0]
            total_rows = row['total_rows']
            null_count = row['null_count']
            null_ratio = null_count / total_rows if total_rows > 0 else 1.0

            latest_ts = row['latest_ts']
            if latest_ts:
                age_hours = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
            else:
                age_hours = None

            # Determine status (holiday-aware)
            status = "healthy"
            market_closed = is_market_closed()
            if total_rows < min_rows_per_symbol:
                status = "low_coverage"
                low_coverage_symbols.append(symbol)
            elif null_ratio > max_null_ratio and not market_closed:
                # Allow relaxed warning if under a relaxed threshold
                if relax_max_null_ratio and null_ratio <= relax_max_null_ratio:
                    status = "high_nulls_warning"
                    warning_high_null_symbols.append(symbol)
                else:
                    status = "high_nulls"
                    high_null_symbols.append(symbol)
            elif age_hours and age_hours > max_age_hours and not market_closed:
                status = "stale"
                stale_symbols.append(symbol)

            by_symbol[symbol] = {
                "status": status,
                "rows": total_rows,
                "null_ratio": round(null_ratio, 4),
                "age_hours": round(age_hours, 2) if age_hours else None,
                "latest_ts": str(latest_ts) if latest_ts else None
            }

        except Exception as e:
            logger.error("preflight_symbol_check_failed", symbol=symbol, error=str(e))
            missing_symbols.append(symbol)
            by_symbol[symbol] = {
                "status": "error",
                "error": str(e)
            }

    # Aggregate total stats
    total_rows = sum(s.get('rows', 0) for s in by_symbol.values())

    # Determine overall readiness
    ready = (
        len(missing_symbols) == 0 and
        len(low_coverage_symbols) == 0 and
        len(high_null_symbols) == 0 and
        len(stale_symbols) == 0
    )

    details = {
        "status": "ready" if ready else "not_ready",
        "by_symbol": by_symbol,
        "summary": {
            "total_rows": total_rows,
            "missing_symbols": missing_symbols,
            "low_coverage_symbols": low_coverage_symbols,
            "high_null_symbols": high_null_symbols,
            "warning_high_null_symbols": warning_high_null_symbols,
            "stale_symbols": stale_symbols,
            "healthy_count": len([s for s in by_symbol.values() if s.get('status') == 'healthy'])
        },
        "thresholds": {
            "min_rows_per_symbol": min_rows_per_symbol,
            "max_null_ratio": max_null_ratio,
            "relax_max_null_ratio": relax_max_null_ratio,
            "max_age_hours": max_age_hours,
            "lookback_days": lookback_days
        }
    }

    return ready, details


def preflight_check_features(
    symbols: List[str],
    min_rows_per_symbol: int = 1000,
    max_age_hours: int = 24,
    features_table: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Check if features are ready for training

    Args:
        symbols: List of symbols to check
        min_rows_per_symbol: Minimum rows required per symbol
        max_age_hours: Maximum age of features in hours

    Returns:
        (ready: bool, details: dict)
    """
    client = get_client()
    cache_bust = f"-- cache_bust={int(time.time())}"

    # Require explicit table; caller should pass active/baseline accordingly
    if not features_table:
        return False, {
            "status": "table_missing",
            "message": "features_table not provided to preflight_check_features",
            "missing_symbols": symbols,
            "total_rows": 0
        }

    by_symbol = {}
    missing_symbols = []
    low_coverage_symbols = []
    stale_symbols = []

    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    start_filter = ""
    end_filter = ""
    params_extra: Dict[str, Any] = {}
    if start:
        try:
            params_extra["start"] = datetime.fromisoformat(str(start).replace(" ", "T"))
            start_filter = "AND toDate(ts) >= toDate(%(start)s)"
        except Exception:
            start_filter = ""
    if end:
        try:
            params_extra["end"] = datetime.fromisoformat(str(end).replace(" ", "T"))
            end_filter = "AND toDate(ts) <= toDate(%(end)s)"
        except Exception:
            end_filter = ""

    for symbol in symbols:
        try:
            ts_col = "ts"
            stats_query = f"""
                SELECT
                    count() as total_rows,
                    max({ts_col}) as latest_ts
                FROM {features_table}
                WHERE symbol = %(symbol)s
                {start_filter}
                {end_filter}
                {cache_bust}
            """

            params_all = {"symbol": symbol}
            params_all.update(params_extra)
            result = client.query(stats_query, params_all, settings={'use_query_cache': False})

            if not result or result[0]['total_rows'] == 0:
                missing_symbols.append(symbol)
                by_symbol[symbol] = {
                    "status": "missing",
                    "rows": 0
                }
                continue

            row = result[0]
            total_rows = row['total_rows']
            latest_ts = row['latest_ts']

            if latest_ts:
                age_hours = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
            else:
                age_hours = None

            # Determine status (holiday-aware)
            status = "healthy"
            market_closed = is_market_closed()
            if total_rows < min_rows_per_symbol:
                status = "low_coverage"
                low_coverage_symbols.append(symbol)
            elif age_hours and age_hours > max_age_hours and not market_closed:
                status = "stale"
                stale_symbols.append(symbol)

            by_symbol[symbol] = {
                "status": status,
                "rows": total_rows,
                "age_hours": round(age_hours, 2) if age_hours else None,
                "latest_ts": str(latest_ts) if latest_ts else None
            }

        except Exception as e:
            logger.error("preflight_features_check_failed", symbol=symbol, error=str(e))
            missing_symbols.append(symbol)
            by_symbol[symbol] = {
                "status": "error",
                "error": str(e)
            }

    total_rows = sum(s.get('rows', 0) for s in by_symbol.values())

    ready = (
        len(missing_symbols) == 0 and
        len(low_coverage_symbols) == 0 and
        len(stale_symbols) == 0
    )

    details = {
        "status": "ready" if ready else "not_ready",
        "by_symbol": by_symbol,
        "summary": {
            "total_rows": total_rows,
            "missing_symbols": missing_symbols,
            "low_coverage_symbols": low_coverage_symbols,
            "stale_symbols": stale_symbols,
            "healthy_count": len([s for s in by_symbol.values() if s.get('status') == 'healthy'])
        },
        "thresholds": {
            "min_rows_per_symbol": min_rows_per_symbol,
            "max_age_hours": max_age_hours
        }
    }

    return ready, details


def preflight_check_all(
    symbols: List[str],
    horizons: List[int] = [30],
    min_rows_per_symbol: int = 1000,
    strict: bool = True,
    lookback_days: int = 90,
    features_table: Optional[str] = None,
    labels_table: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    data_quality_days: int = 3,
    data_quality_gate: bool = True,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Comprehensive pre-flight check for all training dependencies

    Args:
        symbols: List of symbols to train on
        horizons: Label horizons to check
        min_rows_per_symbol: Minimum rows required per symbol
        strict: If True, all checks must pass; if False, only labels must pass
        lookback_days: Time window for coverage check (days), should match training window

    Returns:
        (ready: bool, comprehensive_report: dict)

    Example:
        >>> ready, report = preflight_check_all(["TSLA", "AAPL", "MSFT"], lookback_days=30)
        >>> if not ready:
        >>>     logger.error("preflight_failed", report=report)
        >>>     # Attempt auto-healing
        >>>     healer = DataHealer()
        >>>     healer.ensure_labels_ready(symbols)
    """
    logger.info("preflight_starting", symbols=symbols, horizons=horizons, start=start, end=end)

    # Check features
    features_ready, features_report = preflight_check_features(
        symbols=symbols,
        min_rows_per_symbol=min_rows_per_symbol,
        features_table=features_table,
        start=start,
        end=end,
    )

    # Check labels (pass overrides for consistency)
    labels_ready, labels_report = preflight_check_labels(
        symbols=symbols,
        horizons=horizons,
        min_rows_per_symbol=min_rows_per_symbol,
        lookback_days=lookback_days,
        labels_table=labels_table,
        start=start,
        end=end,
    )

    # Determine overall readiness
    if strict:
        overall_ready = features_ready and labels_ready
    else:
        # In non-strict mode, only labels are blocking
        overall_ready = labels_ready

    report = {
        "overall_ready": overall_ready,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "features": features_report,
        "labels": labels_report,
        "blocking_issues": [],
        "window": {"start": start, "end": end, "lookback_days": lookback_days},
    }

    # Data quality gate (coverage/off-RTH) for symbols
    if data_quality_gate and symbols:
        try:
            dq_cmd = [
                sys.executable,
                "tools/data_quality_check.py",
                "--symbols",
                *symbols, # Pass symbols as individual arguments
                "--days",
                str(data_quality_days),
                "--json",
            ]
            dq_out = subprocess.run(dq_cmd, capture_output=True, text=True)
            if dq_out.stdout:
                # data_quality_check may emit log lines before JSON; use last non-empty line
                lines = [ln for ln in dq_out.stdout.splitlines() if ln.strip()]
                if not lines:
                    raise ValueError("data_quality_check produced no JSON output")
                dq_json = json.loads(lines[-1])
                if isinstance(dq_json, dict):
                    for item in dq_json.get("symbols", []):
                        if item.get("status") in ("low_coverage", "off_rth_excess", "no_data"):
                            report["blocking_issues"].append({
                                "severity": "high",
                                "component": "data_quality",
                                "issue": item.get("status"),
                                "symbols": [item.get("symbol")],
                            })
                            overall_ready = False
                            report["overall_ready"] = overall_ready
                            report["data_quality"] = dq_json
                            break
        except Exception as e:
            logger.warning("preflight_data_quality_gate_failed", error=str(e))

    # Collect blocking issues
    if not features_ready:
        if features_report['summary']['missing_symbols']:
            report['blocking_issues'].append({
                "severity": "high" if strict else "medium",
                "component": "features",
                "issue": "missing_symbols",
                "symbols": features_report['summary']['missing_symbols']
            })
        if features_report['summary']['low_coverage_symbols']:
            report['blocking_issues'].append({
                "severity": "medium",
                "component": "features",
                "issue": "low_coverage",
                "symbols": features_report['summary']['low_coverage_symbols']
            })

    if not labels_ready:
        if labels_report['summary']['missing_symbols']:
            report['blocking_issues'].append({
                "severity": "critical",
                "component": "labels",
                "issue": "missing_symbols",
                "symbols": labels_report['summary']['missing_symbols']
            })
        if labels_report['summary']['low_coverage_symbols']:
            report['blocking_issues'].append({
                "severity": "high",
                "component": "labels",
                "issue": "low_coverage",
                "symbols": labels_report['summary']['low_coverage_symbols']
            })
        if labels_report['summary']['high_null_symbols']:
            report['blocking_issues'].append({
                "severity": "high",
                "component": "labels",
                "issue": "high_null_ratio",
                "symbols": labels_report['summary']['high_null_symbols']
            })

    if overall_ready:
        logger.info(
            "preflight_complete",
            ready=overall_ready,
            features_ready=features_ready,
            labels_ready=labels_ready,
            blocking_issues_count=len(report['blocking_issues'])
        )
    else:
        logger.warning(
            "preflight_complete",
            ready=overall_ready,
            features_ready=features_ready,
            labels_ready=labels_ready,
            blocking_issues_count=len(report['blocking_issues'])
        )

    return overall_ready, report


if __name__ == "__main__":
    import json
    import argparse

    ap = argparse.ArgumentParser(description="Run preflight checks for ML readiness.")
    ap.add_argument("--symbols", nargs="+", default=["TSLA", "NVDA", "SPY"], help="Symbols to check")
    ap.add_argument("--horizons", nargs="+", type=int, default=[30], help="Label horizons to check (minutes)")
    ap.add_argument("--min-rows", type=int, default=1000, help="Minimum rows per symbol")
    ap.add_argument("--strict", action="store_true", help="Strict mode (features+labels must be ready)")
    args = ap.parse_args()

    print(f"Running preflight checks for: {', '.join(args.symbols)}\n")

    ready, report = preflight_check_all(
        symbols=args.symbols,
        horizons=args.horizons,
        min_rows_per_symbol=args.min_rows,
        strict=args.strict,
    )

    print("=" * 60)
    print(f"OVERALL READY: {ready}")
    print("=" * 60)
    print(json.dumps(report, indent=2))

    if not ready:
        print("\n" + "=" * 60)
        print("BLOCKING ISSUES:")
        print("=" * 60)
        for issue in report['blocking_issues']:
            print(f"[{issue['severity'].upper()}] {issue['component']}: {issue['issue']}")
            print(f"  Symbols: {', '.join(issue.get('symbols', []))}")
