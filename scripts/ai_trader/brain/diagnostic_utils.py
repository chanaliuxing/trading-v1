#!/usr/bin/env python3
"""
Diagnostic utilities for Brain context.

Simplifies complex system diagnostics into Brain-friendly formats.
"""
from typing import Dict, Any, List, Optional


def simplify_label_health_for_brain(label_health: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Simplify label health data for Brain consumption.

    Takes full label_health dict from SystemMonitor.check_label_health()
    and converts it to a simplified format that's easier for LLM to understand.

    Args:
        label_health: Full label health dict with 'by_symbol' and 'summary' keys

    Returns:
        Simplified diagnostic dict with:
        - overall_status: "healthy" | "degraded" | "critical"
        - total_labels: Total label rows
        - healthy_symbols: Number of healthy symbols
        - issues: List of actionable issues
        - top_symbols: Top 5 symbols by coverage

    Example:
        >>> health = monitor.check_label_health()
        >>> simplified = simplify_label_health_for_brain(health)
        >>> print(simplified['overall_status'])  # "healthy"
        >>> print(simplified['issues'])  # []
    """
    if not label_health:
        return None

    summary = label_health.get("summary", {})
    by_symbol = label_health.get("by_symbol", {})

    # Determine overall status based on issues
    overall_status = summary.get("overall_status", "unknown")
    missing = summary.get("missing_symbols", [])
    low_coverage = summary.get("low_coverage_symbols", [])
    stale = summary.get("stale_symbols", [])

    # Map to severity
    if missing or len(low_coverage) > 3:
        status = "critical"
    elif low_coverage or stale:
        status = "degraded"
    else:
        status = "healthy"

    # Build actionable issues list
    issues = []

    if missing:
        issues.append({
            "severity": "critical",
            "issue": "missing_labels",
            "symbols": missing,
            "recommendation": "Run label generation for these symbols immediately"
        })

    if low_coverage:
        issues.append({
            "severity": "warning",
            "issue": "low_label_coverage",
            "symbols": low_coverage,
            "recommendation": f"{len(low_coverage)} symbols have <1000 label rows. May impact ML training quality."
        })

    if stale:
        issues.append({
            "severity": "warning",
            "issue": "stale_labels",
            "symbols": stale,
            "recommendation": f"{len(stale)} symbols have labels >48h old. Consider refreshing."
        })

    # Top 5 symbols by coverage (for Brain awareness)
    top_symbols = []
    if by_symbol:
        sorted_symbols = sorted(
            by_symbol.items(),
            key=lambda x: x[1].get("rows", 0),
            reverse=True
        )[:5]

        for sym, data in sorted_symbols:
            top_symbols.append({
                "symbol": sym,
                "rows": data.get("rows", 0),
                "null_ratio": data.get("null_ratio", 0.0),
                "age_hours": data.get("age_hours", 0.0),
                "status": data.get("status", "unknown")
            })

    return {
        "overall_status": status,
        "total_labels": summary.get("total_rows", 0),
        "healthy_symbols": summary.get("healthy_count", 0),
        "issues": issues,
        "top_symbols": top_symbols,
        "can_train_ml": status != "critical",  # Quick decision helper
    }


def format_label_health_for_prompt(simplified: Optional[Dict[str, Any]]) -> str:
    """
    Format simplified label health into human-readable text for Brain prompts.

    Args:
        simplified: Output from simplify_label_health_for_brain()

    Returns:
        Formatted string suitable for inclusion in Brain prompt context

    Example:
        >>> health = monitor.check_label_health()
        >>> simplified = simplify_label_health_for_brain(health)
        >>> text = format_label_health_for_prompt(simplified)
        >>> print(text)
        # ML TRAINING DATA READINESS
        # Status: HEALTHY
        # Total Labels: 1,315,213 rows
        # Healthy Symbols: 15
        # ML Training: READY ✅
    """
    if not simplified:
        return "# ML TRAINING DATA READINESS\nStatus: UNKNOWN (label health check failed)"

    status = simplified.get("overall_status", "unknown").upper()
    total = simplified.get("total_labels", 0)
    healthy = simplified.get("healthy_symbols", 0)
    can_train = simplified.get("can_train_ml", False)
    issues = simplified.get("issues", [])
    top_symbols = simplified.get("top_symbols", [])

    # Status emoji
    status_emoji = {
        "HEALTHY": "✅",
        "DEGRADED": "⚠️",
        "CRITICAL": "❌",
        "UNKNOWN": "❓"
    }.get(status, "❓")

    lines = [
        "# ML TRAINING DATA READINESS",
        f"Status: {status} {status_emoji}",
        f"Total Labels: {total:,} rows",
        f"Healthy Symbols: {healthy}",
        f"ML Training: {'READY ✅' if can_train else 'NOT READY ❌'}",
    ]

    # Add issues if any
    if issues:
        lines.append("\n## Issues Detected:")
        for issue in issues:
            severity = issue.get("severity", "info").upper()
            issue_type = issue.get("issue", "unknown")
            symbols = issue.get("symbols", [])
            rec = issue.get("recommendation", "")

            lines.append(f"- [{severity}] {issue_type}")
            lines.append(f"  Symbols: {', '.join(symbols[:5])}")
            if len(symbols) > 5:
                lines.append(f"  ... and {len(symbols) - 5} more")
            lines.append(f"  → {rec}")

    # Add top symbols
    if top_symbols:
        lines.append("\n## Top 5 Symbols by Label Coverage:")
        for s in top_symbols:
            sym = s.get("symbol", "???")
            rows = s.get("rows", 0)
            null_pct = s.get("null_ratio", 0.0) * 100
            age_h = s.get("age_hours", 0.0)
            sym_status = s.get("status", "unknown")

            status_icon = "✅" if sym_status == "healthy" else "⚠️"
            lines.append(
                f"  {status_icon} {sym:6} {rows:7,} rows | "
                f"NULL: {null_pct:5.2f}% | Age: {age_h:.1f}h"
            )

    return "\n".join(lines)


def create_brain_diagnostic_context(
    label_health: Optional[Dict[str, Any]] = None,
    preflight_report: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Create comprehensive diagnostic context for Brain decision-making.

    Combines multiple diagnostic sources into a unified context dict.

    Args:
        label_health: Full label health from SystemMonitor.check_label_health()
        preflight_report: PreFlight report from preflight_check_all()
        **kwargs: Additional diagnostic data

    Returns:
        Unified diagnostic context dict with:
        - label_health_simplified: Simplified label health
        - label_health_text: Human-readable text
        - preflight_summary: Simplified preflight report (if provided)
        - ...other kwargs

    Example:
        >>> from scripts.monitor_system import SystemMonitor
        >>> from scripts.ai_trader.brain.preflight import preflight_check_all
        >>>
        >>> monitor = SystemMonitor()
        >>> health = monitor.check_label_health()
        >>> ready, report = preflight_check_all(["TSLA", "AAPL"])
        >>>
        >>> ctx = create_brain_diagnostic_context(
        ...     label_health=health,
        ...     preflight_report=report
        ... )
        >>> print(ctx['label_health_text'])
    """
    context = {}

    # Label health diagnostics
    if label_health:
        simplified = simplify_label_health_for_brain(label_health)
        context["label_health_simplified"] = simplified
        context["label_health_text"] = format_label_health_for_prompt(simplified)

    # PreFlight diagnostics (simplified)
    if preflight_report:
        context["preflight_summary"] = {
            "ready": preflight_report.get("overall_ready", False),
            "labels_ready": preflight_report.get("labels", {}).get("overall_ready", False),
            "features_ready": preflight_report.get("features", {}).get("overall_ready", False),
            "blocking_issues": preflight_report.get("blocking_issues", []),
        }

    # Add any additional diagnostics
    for key, value in kwargs.items():
        context[key] = value

    return context
