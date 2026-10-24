#!/usr/bin/env python3
"""
Unified Preflight Check - Training Readiness Validation

Uses SystemHealthSnapshot to validate training readiness with unified protocols.
This replaces scripts/ai_trader/brain/preflight.py with a unified interface.

Integration with AI Brain diagnostic tools and DataHealer.

Usage:
    from tools.preflight_check import preflight_check

    ready, snapshot = preflight_check(symbols=["TSLA"], horizons=[120])
    if not ready:
        logger.error("preflight_failed", blocking_issues=snapshot.blocking_issues)
"""

import os
import sys

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from typing import List, Tuple, Dict, Any
from datetime import datetime
from libs.health import (
    SystemHealthSnapshot,
    build_system_health,
    Issue,
    IssueType,
    Severity,
    make_issue_id,
)
import structlog

logger = structlog.get_logger(__name__)


def preflight_check_all(
    symbols: List[str],
    horizons: List[int] = [120],
    table: str = "trading.features_1m_with_labels",
    strict: bool = True
) -> Tuple[bool, SystemHealthSnapshot]:
    """
    Unified preflight check using SystemHealthSnapshot

    Args:
        symbols: List of symbols to check
        horizons: Label horizons to check
        table: Feature table to check
        strict: If True, all components must be healthy; if False, only labels are blocking

    Returns:
        (ready: bool, snapshot: SystemHealthSnapshot)

        ready=True means system is ready for training
        ready=False means blocking issues exist (check snapshot.blocking_issues)

    Example:
        >>> ready, snapshot = preflight_check(["TSLA"], horizons=[120])
        >>> if not ready:
        >>>     print(f"Blocking issues: {snapshot.blocking_issues}")
        >>>     # Attempt auto-healing with DataHealer
    """
    logger.info("preflight_starting", symbols=symbols, horizons=horizons, strict=strict)

    # Build unified health snapshot
    snapshot = build_system_health(
        symbols=symbols,
        horizons=horizons,
        table=table
    )

    # Determine readiness based on strict mode
    if strict:
        # All components must be healthy
        ready = snapshot.overall_status == "healthy"
    else:
        # Only labels and config are blocking
        ready = (
            snapshot.labels.status != "blocked" and
            snapshot.config.status != "blocked"
        )

    # Log result
    if ready:
        logger.info(
            "preflight_complete",
            ready=True,
            overall_status=snapshot.overall_status,
            bars_status=snapshot.bars.status,
            features_status=snapshot.features.status,
            labels_status=snapshot.labels.status,
            config_status=snapshot.config.status,
            blocking_issues_count=len(snapshot.blocking_issues)
        )
    else:
        logger.warning(
            "preflight_failed",
            ready=False,
            overall_status=snapshot.overall_status,
            bars_status=snapshot.bars.status,
            features_status=snapshot.features.status,
            labels_status=snapshot.labels.status,
            config_status=snapshot.config.status,
            blocking_issues_count=len(snapshot.blocking_issues),
            blocking_issues=snapshot.blocking_issues
        )

    return ready, snapshot


def preflight_check_with_issues(
    symbols: List[str],
    horizons: List[int] = [120],
    table: str = "trading.features_1m_with_labels",
    strict: bool = True
) -> Tuple[bool, SystemHealthSnapshot, List[Issue]]:
    """
    Preflight check with structured Issue list for DataHealer consumption

    This is an enhanced version that converts blocking issues into standard Issue objects,
    making them easy for DataHealer to parse and act on.

    Args:
        symbols: List of symbols to check
        horizons: Label horizons to check
        table: Feature table to check
        strict: If True, all components must be healthy

    Returns:
        (ready: bool, snapshot: SystemHealthSnapshot, issues: List[Issue])
    """
    ready, snapshot = preflight_check_all(
        symbols=symbols,
        horizons=horizons,
        table=table,
        strict=strict
    )

    # Convert blocking issues to Issue objects
    issues = []

    # Bars issues
    if snapshot.bars.status == "blocked":
        issue = Issue(
            issue_id=make_issue_id("bars", IssueType.DATA_STALE),
            timestamp=datetime.now().isoformat(),
            component="bars",
            issue_type=IssueType.DATA_STALE,
            severity=Severity.CRITICAL,
            title="Bars data stale or missing",
            description="; ".join(snapshot.bars.issues),
            affected_symbols=snapshot.bars.missing_symbols,
            affected_tables=["trading.bars_1m"],
            metrics={
                "age_hours": snapshot.bars.age_hours,
                "coverage_pct": snapshot.bars.coverage_pct,
            }
        )
        issues.append(issue)

    # Features issues
    if snapshot.features.status == "blocked":
        issue_type = IssueType.DATA_STALE if snapshot.features.age_hours > 24 else IssueType.DATA_QUALITY_POOR
        issue = Issue(
            issue_id=make_issue_id("features", issue_type),
            timestamp=datetime.now().isoformat(),
            component="features",
            issue_type=issue_type,
            severity=Severity.CRITICAL,
            title="Features data quality issue",
            description="; ".join(snapshot.features.issues),
            affected_symbols=symbols,
            affected_tables=[table],
            metrics={
                "age_hours": snapshot.features.age_hours,
                "null_ratio": snapshot.features.null_ratio,
                "rebuild_needed": snapshot.features.rebuild_needed,
            }
        )
        issues.append(issue)

    # Labels issues (per symbol-horizon)
    for key, sym_status in snapshot.labels.by_symbol.items():
        if sym_status.status == "blocked":
            issue = Issue(
                issue_id=make_issue_id("labels", IssueType.DATA_QUALITY_POOR),
                timestamp=datetime.now().isoformat(),
                component="labels",
                issue_type=IssueType.DATA_QUALITY_POOR,
                severity=Severity.CRITICAL,
                title=f"Label quality issue for {key}",
                description="; ".join(sym_status.issues),
                affected_symbols=[sym_status.symbol],
                affected_tables=[table],
                metrics={
                    "horizon_min": sym_status.horizon_min,
                    "rows": sym_status.rows,
                    "null_ratio": sym_status.null_ratio,
                    "zero_ratio": sym_status.zero_ratio,
                    "std_bps": sym_status.std_bps,
                    "mean_magnitude": sym_status.mean_magnitude,
                    "age_hours": sym_status.age_hours,
                }
            )
            issues.append(issue)

    # Config issues
    if snapshot.config.status == "blocked":
        for problem in snapshot.config.problems:
            issue = Issue(
                issue_id=make_issue_id("config", IssueType.CONFIG_MISMATCH),
                timestamp=datetime.now().isoformat(),
                component="config",
                issue_type=IssueType.CONFIG_MISMATCH,
                severity=Severity.CRITICAL,
                title="ML config mismatch",
                description=problem.get("message", "Unknown config issue"),
                affected_symbols=symbols,
                affected_tables=[table],
                metadata=problem
            )
            issues.append(issue)

    return ready, snapshot, issues


def print_preflight_report(snapshot: SystemHealthSnapshot):
    """
    Print human-readable preflight report

    Args:
        snapshot: SystemHealthSnapshot from preflight_check()
    """
    print("=" * 80)
    print("PREFLIGHT CHECK REPORT")
    print("=" * 80)
    print(f"Timestamp:        {snapshot.timestamp}")
    print(f"Overall Status:   {snapshot.overall_status.upper()}")
    print(f"Symbols:          {', '.join(snapshot.symbols)}")
    print(f"Horizons:         {', '.join(map(str, snapshot.horizons))}")
    print()

    # Component statuses
    components = [
        ("Bars", snapshot.bars.status),
        ("Features", snapshot.features.status),
        ("Labels", snapshot.labels.status),
        ("Config", snapshot.config.status),
    ]

    print("Component Health:")
    for name, status in components:
        icon = "✅" if status == "healthy" else "⚠️" if status == "degraded" else "❌"
        print(f"  {icon} {name:12} {status.upper()}")
    print()

    # Blocking issues
    if snapshot.blocking_issues:
        print("🚨 BLOCKING ISSUES:")
        print("-" * 80)
        for i, issue in enumerate(snapshot.blocking_issues, 1):
            print(f"  {i}. {issue}")
        print()
    else:
        print("✅ No blocking issues detected")
        print()

    # Labels details
    if snapshot.labels.by_symbol:
        print("Label Details:")
        print("-" * 80)
        for key, sym_status in snapshot.labels.by_symbol.items():
            icon = "✅" if sym_status.status == "healthy" else "⚠️" if sym_status.status == "degraded" else "❌"
            print(f"  {icon} {key:15} rows={sym_status.rows:6,} | null={sym_status.null_ratio:5.1%} | "
                  f"std={sym_status.std_bps:6.1f}bps")
            if sym_status.issues:
                for issue in sym_status.issues:
                    print(f"       Issue: {issue}")
        print()

    print("=" * 80)


if __name__ == "__main__":
    """
    Standalone testing

    Usage:
        venv/bin/python tools/preflight_check.py --symbols TSLA AAPL --horizons 120
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Unified Preflight Check")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["TSLA"],
        help="Symbols to check"
    )
    parser.add_argument(
        "--horizons",
        type=int,
        nargs="+",
        default=[120],
        help="Label horizons to check"
    )
    parser.add_argument(
        "--table",
        default="trading.features_1m_with_labels",
        help="Feature table to check"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Strict mode: all components must be healthy"
    )
    parser.add_argument(
        "--output",
        help="Output JSON file path (optional)"
    )
    parser.add_argument(
        "--with-issues",
        action="store_true",
        default=False,
        help="Include structured Issue list in output"
    )

    args = parser.parse_args()

    print(f"Running preflight check for: {', '.join(args.symbols)}")
    print(f"Horizons: {', '.join(map(str, args.horizons))}")
    print(f"Strict mode: {args.strict}\n")

    if args.with_issues:
        ready, snapshot, issues = preflight_check_with_issues(
            symbols=args.symbols,
            horizons=args.horizons,
            table=args.table,
            strict=args.strict
        )
    else:
        ready, snapshot = preflight_check_all(
            symbols=args.symbols,
            horizons=args.horizons,
            table=args.table,
            strict=args.strict
        )
        issues = []

    # Print report
    print_preflight_report(snapshot)

    # Optionally save to JSON
    if args.output:
        output_data = {
            "ready": ready,
            "snapshot": snapshot.to_dict(),
        }
        if args.with_issues:
            output_data["issues"] = [issue.to_dict() for issue in issues]

        with open(args.output, 'w') as f:
            json.dump(output_data, f, indent=2)
        print(f"✅ Report saved to {args.output}")

    # Exit with appropriate code
    sys.exit(0 if ready else 1)
