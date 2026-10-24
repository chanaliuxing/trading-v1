#!/usr/bin/env python3
"""
ML Actions Dashboard - Monitor Brain's ML action execution

Features:
- Daily/weekly action statistics
- Success rates by action type
- Quota utilization tracking
- Recent action timeline
- Error analysis

Usage:
    python scripts/ai_trader/ml_actions_dashboard.py --days 7
    python scripts/ai_trader/ml_actions_dashboard.py --today
"""
import argparse
import json
from collections import defaultdict, Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any


def _runs_dir() -> Path:
    """Get runs directory."""
    return Path("runs/ai_trader")


def _read_actions(start_date: date, end_date: date) -> List[Dict[str, Any]]:
    """Read all actions between start and end dates.

    Args:
        start_date: Start date (inclusive)
        end_date: End date (inclusive)

    Returns:
        List of action entries
    """
    actions = []
    current = start_date

    while current <= end_date:
        date_str = current.isoformat()
        actions_file = _runs_dir() / date_str / "ml_actions.jsonl"

        if actions_file.exists():
            with actions_file.open("r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        entry["date"] = date_str
                        actions.append(entry)
                    except json.JSONDecodeError:
                        continue

        current += timedelta(days=1)

    return actions


def _compute_stats(actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute dashboard statistics.

    Args:
        actions: List of action entries

    Returns:
        Statistics dictionary
    """
    stats = {
        "total_actions": len(actions),
        "by_action": defaultdict(lambda: {"total": 0, "ok": 0, "skipped": 0, "failed": 0, "error": 0}),
        "by_date": defaultdict(lambda: {"total": 0, "ok": 0}),
        "by_status": Counter(),
        "by_reason": Counter(),
        "recent_errors": [],
        "quota_usage": defaultdict(int)
    }

    for action in actions:
        action_type = action.get("action", "unknown")
        status = action.get("status", "unknown")
        reason = action.get("reason", "")
        action_date = action.get("date", "unknown")
        ts = action.get("ts", "")

        # By action type
        stats["by_action"][action_type]["total"] += 1
        stats["by_action"][action_type][status] += 1

        # By date
        stats["by_date"][action_date]["total"] += 1
        if status == "ok":
            stats["by_date"][action_date]["ok"] += 1

        # By status
        stats["by_status"][status] += 1

        # Skip reasons
        if reason and status != "ok":
            stats["by_reason"][reason[:50]] += 1  # Truncate long reasons

        # Recent errors
        if status in ("failed", "error"):
            stats["recent_errors"].append({
                "ts": ts,
                "action": action_type,
                "reason": reason[:100],
                "job_id": action.get("job_id", "unknown")
            })

        # Quota usage (only count successful actions)
        if status == "ok":
            stats["quota_usage"][action_type] += 1

    # Sort recent errors by timestamp (most recent first)
    stats["recent_errors"] = sorted(
        stats["recent_errors"],
        key=lambda x: x["ts"],
        reverse=True
    )[:10]  # Keep top 10

    return stats


def _print_dashboard(stats: Dict[str, Any], start_date: date, end_date: date):
    """Print formatted dashboard.

    Args:
        stats: Statistics dictionary
        start_date: Report start date
        end_date: Report end date
    """
    days_span = (end_date - start_date).days + 1

    print("=" * 80)
    print(f"ML Actions Dashboard: {start_date} to {end_date} ({days_span} days)")
    print("=" * 80)

    # Summary
    print(f"\n📊 Summary")
    print(f"  Total Actions: {stats['total_actions']}")
    print(f"  Success Rate: {stats['by_status']['ok']}/{stats['total_actions']} ({stats['by_status']['ok']/max(stats['total_actions'],1)*100:.1f}%)")
    print(f"  Skipped: {stats['by_status']['skipped']}")
    print(f"  Failed: {stats['by_status']['failed'] + stats['by_status']['error']}")

    # By action type
    print(f"\n📋 By Action Type")
    DAILY_QUOTAS = {
        "backfill": 5,
        "fix_config": 10,
        "queue_job": 3,
        "pause_ml": 2,
        "extend_window": 5
    }

    for action_type in sorted(stats["by_action"].keys()):
        data = stats["by_action"][action_type]
        quota = DAILY_QUOTAS.get(action_type, "-")
        usage = stats["quota_usage"][action_type]
        success_rate = data["ok"] / max(data["total"], 1) * 100

        print(f"  {action_type:15s}: {data['ok']:3d} ok / {data['total']:3d} total ({success_rate:5.1f}%) | Quota: {usage}/{quota}/day avg")

    # Daily trend
    print(f"\n📈 Daily Trend")
    for action_date in sorted(stats["by_date"].keys())[-7:]:  # Last 7 days
        data = stats["by_date"][action_date]
        print(f"  {action_date}: {data['ok']:3d} ok / {data['total']:3d} total")

    # Skip reasons
    if stats["by_reason"]:
        print(f"\n⚠️  Top Skip Reasons")
        for reason, count in stats["by_reason"].most_common(5):
            print(f"  [{count:2d}x] {reason}")

    # Recent errors
    if stats["recent_errors"]:
        print(f"\n❌ Recent Errors (Last 10)")
        for error in stats["recent_errors"]:
            ts_short = error["ts"][:19] if len(error["ts"]) >= 19 else error["ts"]
            print(f"  [{ts_short}] {error['action']:15s} | {error['reason']}")
            print(f"    Job ID: {error['job_id']}")

    # Quota utilization
    print(f"\n💾 Quota Utilization (per day avg over {days_span} days)")
    for action_type, quota_limit in sorted(DAILY_QUOTAS.items()):
        total_usage = stats["quota_usage"][action_type]
        avg_daily = total_usage / days_span
        utilization = avg_daily / quota_limit * 100

        bar_length = int(utilization / 5)  # 20 chars = 100%
        bar = "█" * bar_length + "░" * (20 - bar_length)

        print(f"  {action_type:15s}: {bar} {avg_daily:.1f}/{quota_limit} ({utilization:5.1f}%)")

    print("\n" + "=" * 80)


def main():
    ap = argparse.ArgumentParser(description="ML Actions Dashboard")
    ap.add_argument("--days", type=int, default=7, help="Number of days to analyze (default: 7)")
    ap.add_argument("--today", action="store_true", help="Only show today's data")
    ap.add_argument("--start", help="Start date (YYYY-MM-DD)")
    ap.add_argument("--end", help="End date (YYYY-MM-DD)")
    args = ap.parse_args()

    # Determine date range
    if args.today:
        start_date = end_date = date.today()
    elif args.start and args.end:
        start_date = date.fromisoformat(args.start)
        end_date = date.fromisoformat(args.end)
    else:
        end_date = date.today()
        start_date = end_date - timedelta(days=args.days - 1)

    # Read actions
    actions = _read_actions(start_date, end_date)

    if not actions:
        print(f"No actions found between {start_date} and {end_date}")
        return

    # Compute stats
    stats = _compute_stats(actions)

    # Print dashboard
    _print_dashboard(stats, start_date, end_date)

    # Export JSON (optional)
    export_file = _runs_dir() / "ml_actions_stats.json"
    export_file.parent.mkdir(parents=True, exist_ok=True)
    with export_file.open("w") as f:
        json.dump({
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "stats": {
                "total_actions": stats["total_actions"],
                "by_action": dict(stats["by_action"]),
                "by_status": dict(stats["by_status"]),
                "quota_usage": dict(stats["quota_usage"])
            }
        }, f, indent=2)

    print(f"\n📁 Stats exported to: {export_file}")


if __name__ == "__main__":
    main()
