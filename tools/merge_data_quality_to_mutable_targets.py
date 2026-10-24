#!/usr/bin/env python3
"""
Merge data quality check results into mutable_targets snapshot.

This script reads the latest data quality report and updates the health scores
and immune_cases in the mutable_targets.json snapshot.

Usage:
  PYTHONPATH=. venv/bin/python tools/merge_data_quality_to_mutable_targets.py
"""
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, List


def load_latest_data_quality_report() -> Dict[str, Any] | None:
    """Load the most recent data quality report."""
    base_dir = Path("runs/data_quality")
    if not base_dir.exists():
        return None

    # Check today and yesterday
    for days_ago in [0, 1]:
        check_date = date.today() - timedelta(days=days_ago)
        report_dir = base_dir / check_date.strftime("%Y%m%d")
        report_file = report_dir / "report.json"

        if report_file.exists():
            try:
                return json.loads(report_file.read_text())
            except Exception:
                continue

    return None


def load_mutable_targets_snapshot() -> Dict[str, Any]:
    """Load existing mutable_targets snapshot."""
    snapshot_path = Path("runs/memory/mutable_targets.json")
    if not snapshot_path.exists():
        return {}

    try:
        return json.loads(snapshot_path.read_text())
    except Exception:
        return {}


def merge_data_quality_into_targets(
    targets: Dict[str, Any],
    dq_report: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Merge data quality warnings into mutable_targets health scores.

    Logic:
    - For each bad symbol, lower the health_score of related targets
    - Add immune_cases for recurring data quality issues
    """
    if not dq_report:
        return targets

    bad_symbols = set(dq_report.get("bad_symbols", []))
    warnings = dq_report.get("warnings", [])

    # Count warnings by symbol
    symbol_warning_counts: Dict[str, int] = {}
    for warning in warnings:
        symbol = warning.get("symbol", "unknown")
        symbol_warning_counts[symbol] = symbol_warning_counts.get(symbol, 0) + 1

    # Update each target that relates to symbols
    for target_id, target_data in targets.items():
        # Skip non-data targets
        if not target_id.startswith("data:"):
            continue

        # Extract symbols from target (assume target_id format: data:table_name or data:symbol_TSLA)
        target_symbols = []
        if "_" in target_id:
            parts = target_id.split("_")
            for part in parts:
                if part.isupper() and len(part) <= 5:  # Likely a symbol
                    target_symbols.append(part)

        # If target has bad symbols, lower health score
        target_bad_symbols = [s for s in target_symbols if s in bad_symbols]
        if target_bad_symbols:
            metrics = target_data.get("metrics", {})
            current_health = metrics.get("health_score", 1.0)

            # Lower health by 0.2 for each bad symbol (cap at 0.1 minimum)
            new_health = max(0.1, current_health - 0.2 * len(target_bad_symbols))
            metrics["health_score"] = new_health
            metrics["recommendation"] = "data_quality_issue"
            metrics["bad_symbols"] = target_bad_symbols
            metrics["last_dq_check"] = datetime.now().isoformat()

            target_data["metrics"] = metrics

            # Add immune_case if this is recurring
            immune_cases = target_data.get("immune_cases", [])
            # Check if we already have a recent data quality immune case
            has_recent_dq_case = any(
                case.get("error_code") == "data_quality_failed"
                and (datetime.now() - datetime.fromisoformat(case.get("timestamp", "2020-01-01"))).days < 7
                for case in immune_cases
            )

            if not has_recent_dq_case:
                immune_cases.append({
                    "timestamp": datetime.now().isoformat(),
                    "error_code": "data_quality_failed",
                    "context": {
                        "bad_symbols": target_bad_symbols,
                        "warning_count": sum(symbol_warning_counts.get(s, 0) for s in target_bad_symbols),
                        "source": "data_quality_monitor"
                    }
                })
                target_data["immune_cases"] = immune_cases

    return targets


def main():
    print("[merge_dq] Loading latest data quality report...")
    dq_report = load_latest_data_quality_report()

    if not dq_report:
        print("[merge_dq] No recent data quality report found (checked today and yesterday)")
        return

    print(f"[merge_dq] Found report from {dq_report['timestamp']}")
    print(f"[merge_dq] Bad symbols: {dq_report.get('bad_symbols', [])}")
    print(f"[merge_dq] Total warnings: {dq_report['summary']['total_warnings']}")

    print("[merge_dq] Loading mutable_targets snapshot...")
    targets = load_mutable_targets_snapshot()

    if not targets:
        print("[merge_dq] No mutable_targets snapshot found, nothing to merge")
        return

    print(f"[merge_dq] Found {len(targets)} targets in snapshot")

    print("[merge_dq] Merging data quality results into targets...")
    updated_targets = merge_data_quality_into_targets(targets, dq_report)

    # Write back to snapshot
    snapshot_path = Path("runs/memory/mutable_targets.json")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(updated_targets, indent=2))

    print(f"[merge_dq] ✅ Updated mutable_targets snapshot: {snapshot_path}")

    # Count how many targets were affected
    affected_count = 0
    for target_data in updated_targets.values():
        if "bad_symbols" in target_data.get("metrics", {}):
            affected_count += 1

    print(f"[merge_dq] Affected targets: {affected_count}")

    # Emit event for tracking
    try:
        from tools.nightly_oof_flow import publish_ml_event
        publish_ml_event(
            "completed",
            "data_quality.merged_to_targets",
            f"merge_{datetime.now().strftime('%Y%m%d')}",
            params={
                "bad_symbols": dq_report.get("bad_symbols", []),
                "affected_targets": affected_count,
                "total_warnings": dq_report['summary']['total_warnings']
            }
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
