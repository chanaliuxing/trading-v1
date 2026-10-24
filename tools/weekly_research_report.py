#!/usr/bin/env python3
"""
Weekly Research Report Generator

Aggregates key metrics from ml.events and ClickHouse to generate a comprehensive
weekly summary of research pipeline performance.

Usage:
  PYTHONPATH=. venv/bin/python tools/weekly_research_report.py --start 2025-11-25 --end 2025-12-01
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

import clickhouse_connect


def ch_client():
    """Get ClickHouse client."""
    try:
        from libs.config import config
        ccfg = config.clickhouse_config
        return clickhouse_connect.get_client(
            host=ccfg.get("host", "localhost"),
            port=int(ccfg.get("port", 8123)),
            database=ccfg.get("database", "trading"),
            username=ccfg.get("user", "default"),
            password=ccfg.get("password", ""),
        )
    except Exception:
        return clickhouse_connect.get_client(host="localhost", port=8123, database="trading")


def query_training_jobs(cli, start_date: str, end_date: str) -> Dict[str, Any]:
    """Query training job statistics from ml.events."""
    query = """
    SELECT
        countIf(status = 'completed') AS success_count,
        countIf(status = 'failed') AS failed_count,
        count() AS total_jobs,
        round(avg(duration_sec) / 60, 2) AS avg_duration_min,
        round(sum(duration_sec) / 3600, 2) AS compute_hours
    FROM ml.events
    WHERE event = 'ml.job'
      AND timestamp >= toDateTime(%(start)s)
      AND timestamp <= toDateTime(%(end)s)
    """
    rows = cli.query_df(query, parameters={"start": start_date, "end": end_date})

    if rows.empty:
        return {
            "total": 0,
            "success_count": 0,
            "failed_count": 0,
            "success_rate": 0.0,
            "avg_duration_min": 0.0,
            "compute_hours": 0.0
        }

    row = rows.iloc[0]
    total = int(row["total_jobs"])
    success = int(row["success_count"])

    return {
        "total": total,
        "success_count": success,
        "failed_count": int(row["failed_count"]),
        "success_rate": round(success / total, 2) if total > 0 else 0.0,
        "avg_duration_min": float(row["avg_duration_min"]) if row["avg_duration_min"] else 0.0,
        "compute_hours": float(row["compute_hours"]) if row["compute_hours"] else 0.0
    }


def query_sync_operations(cli, start_date: str, end_date: str) -> Dict[str, Any]:
    """Query active table sync operations."""
    query = """
    SELECT
        countIf(status = 'completed') AS success_count,
        countIf(status = 'failed') AS failed_count,
        countIf(JSON_VALUE(params, '$.action') = 'fallback') AS fallback_count,
        count() AS total_syncs
    FROM ml.events
    WHERE event = 'active_table.sync'
      AND timestamp >= toDateTime(%(start)s)
      AND timestamp <= toDateTime(%(end)s)
    """
    rows = cli.query_df(query, parameters={"start": start_date, "end": end_date})

    if rows.empty:
        return {
            "triggered": 0,
            "success_count": 0,
            "failed_count": 0,
            "fallback_count": 0,
            "success_rate": 0.0
        }

    row = rows.iloc[0]
    total = int(row["total_syncs"])
    success = int(row["success_count"])

    return {
        "triggered": total,
        "success_count": success,
        "failed_count": int(row["failed_count"]),
        "fallback_count": int(row["fallback_count"]),
        "success_rate": round(success / total, 2) if total > 0 else 0.0
    }


def query_data_quality(cli, start_date: str, end_date: str) -> Dict[str, Any]:
    """Query data quality metrics (from data_quality_monitor events)."""
    query = """
    SELECT
        avg(JSON_VALUE(params, '$.avg_coverage')) AS avg_coverage,
        avg(JSON_VALUE(params, '$.rth_compliance')) AS rth_compliance,
        sum(JSON_VALUE(params, '$.bad_symbol_count')) AS bad_symbol_days
    FROM ml.events
    WHERE event = 'data_quality.check_completed'
      AND timestamp >= toDateTime(%(start)s)
      AND timestamp <= toDateTime(%(end)s)
    """
    try:
        rows = cli.query_df(query, parameters={"start": start_date, "end": end_date})

        if rows.empty:
            return {
                "avg_coverage": 0.0,
                "rth_compliance": 0.0,
                "bad_symbol_days": 0
            }

        row = rows.iloc[0]
        return {
            "avg_coverage": round(float(row["avg_coverage"]) if row["avg_coverage"] else 0.0, 2),
            "rth_compliance": round(float(row["rth_compliance"]) if row["rth_compliance"] else 0.0, 2),
            "bad_symbol_days": int(row["bad_symbol_days"]) if row["bad_symbol_days"] else 0
        }
    except Exception:
        # Fallback if data_quality events not yet available
        return {
            "avg_coverage": 0.0,
            "rth_compliance": 0.0,
            "bad_symbol_days": 0
        }


def query_top_failures(cli, start_date: str, end_date: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Query top failure reasons."""
    query = """
    SELECT
        JSON_VALUE(metrics, '$.error_code') AS error_code,
        count() AS count
    FROM ml.events
    WHERE event = 'ml.job'
      AND status = 'failed'
      AND timestamp >= toDateTime(%(start)s)
      AND timestamp <= toDateTime(%(end)s)
    GROUP BY error_code
    ORDER BY count DESC
    LIMIT %(limit)d
    """
    rows = cli.query_df(query, parameters={"start": start_date, "end": end_date, "limit": limit})

    return [
        {"reason": row["error_code"] or "unknown", "count": int(row["count"])}
        for _, row in rows.iterrows()
    ]


def query_top_performers(cli, start_date: str, end_date: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Query top performing strategies (by AUC improvement)."""
    query = """
    SELECT
        symbol,
        horizon_min,
        argMax(auc, ts) AS latest_auc,
        argMin(auc, ts) AS earliest_auc,
        latest_auc - earliest_auc AS improvement
    FROM trading.oof_xgb
    WHERE ts >= toDateTime(%(start)s)
      AND ts <= toDateTime(%(end)s)
      AND auc IS NOT NULL
    GROUP BY symbol, horizon_min
    HAVING improvement > 0
    ORDER BY improvement DESC
    LIMIT %(limit)d
    """
    rows = cli.query_df(query, parameters={"start": start_date, "end": end_date, "limit": limit})

    return [
        {
            "strategy": f"intraday_{row['symbol']}_h{row['horizon_min']}",
            "oos_auc": round(float(row["latest_auc"]), 4),
            "improvement": round(float(row["improvement"]), 4)
        }
        for _, row in rows.iterrows()
    ]


def generate_recommendations(report_data: Dict[str, Any]) -> List[str]:
    """Generate actionable recommendations based on report data."""
    recommendations = []

    # Training success rate
    training = report_data["summary"]["training_jobs"]
    if training["success_rate"] < 0.7:
        recommendations.append(
            f"⚠️  Training success rate is low ({training['success_rate']:.0%}). "
            "Investigate top failure reasons and fix root causes."
        )

    # Sync failures
    sync = report_data["summary"]["sync_operations"]
    if sync["triggered"] > 0 and sync["success_rate"] < 0.9:
        recommendations.append(
            f"⚠️  Active table sync success rate is low ({sync['success_rate']:.0%}). "
            "Check sync_features_active.py logs and ClickHouse performance."
        )

    # Fallback usage
    if sync.get("fallback_count", 0) > 3:
        recommendations.append(
            f"⚠️  High fallback count ({sync['fallback_count']}). "
            "Active table may be chronically stale. Increase sync frequency or investigate data pipeline."
        )

    # Data quality
    dq = report_data["summary"]["data_quality"]
    if dq.get("avg_coverage", 0) < 0.8:
        recommendations.append(
            f"⚠️  Average data coverage is low ({dq['avg_coverage']:.0%}). "
            "Check data ingestion pipeline and IBKR connection stability."
        )

    if dq.get("bad_symbol_days", 0) > 5:
        recommendations.append(
            f"⚠️  High number of bad symbol-days ({dq['bad_symbol_days']}). "
            "Review data_quality_monitor reports and consider symbol blacklist."
        )

    # Compute budget
    if training.get("compute_hours", 0) > 100:
        recommendations.append(
            "💡 High compute usage this week. Consider optimizing training window or HPO budget."
        )

    if not recommendations:
        recommendations.append("✅ All systems operating normally. No critical issues detected.")

    return recommendations


def generate_report(start_date: str, end_date: str) -> Dict[str, Any]:
    """Generate complete weekly research report."""
    cli = ch_client()

    # Collect all metrics
    training_jobs = query_training_jobs(cli, start_date, end_date)
    sync_ops = query_sync_operations(cli, start_date, end_date)
    data_quality = query_data_quality(cli, start_date, end_date)
    top_failures = query_top_failures(cli, start_date, end_date)
    top_performers = query_top_performers(cli, start_date, end_date)

    report = {
        "report_type": "weekly_research_summary",
        "period": {
            "start": start_date,
            "end": end_date
        },
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "training_jobs": training_jobs,
            "sync_operations": sync_ops,
            "data_quality": data_quality
        },
        "top_failures": top_failures,
        "top_performers": top_performers,
        "recommendations": []  # Will be filled after generation
    }

    # Generate recommendations
    report["recommendations"] = generate_recommendations(report)

    return report


def main():
    ap = argparse.ArgumentParser(description="Generate weekly research report")
    ap.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    ap.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    ap.add_argument("--output-dir", default="runs/reports", help="Output directory")
    args = ap.parse_args()

    # Generate report
    start_dt = datetime.strptime(args.start, "%Y-%m-%d")
    end_dt = datetime.strptime(args.end, "%Y-%m-%d")

    report = generate_report(
        f"{args.start} 00:00:00",
        f"{args.end} 23:59:59"
    )

    # Write to file
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    week_label = f"{start_dt.strftime('%Y%m%d')}_to_{end_dt.strftime('%Y%m%d')}"
    output_file = output_dir / f"weekly_report_{week_label}.json"

    output_file.write_text(json.dumps(report, indent=2))
    print(f"✅ Weekly report written to {output_file}")

    # Print summary to console
    print("\n" + "="*80)
    print(f"WEEKLY RESEARCH REPORT ({args.start} to {args.end})")
    print("="*80)

    print("\n📊 TRAINING JOBS")
    tj = report["summary"]["training_jobs"]
    print(f"  Total: {tj['total']}")
    print(f"  Success Rate: {tj['success_rate']:.0%} ({tj['success_count']} / {tj['total']})")
    print(f"  Avg Duration: {tj['avg_duration_min']:.1f} min")
    print(f"  Compute Hours: {tj['compute_hours']:.1f}h")

    print("\n🔄 SYNC OPERATIONS")
    so = report["summary"]["sync_operations"]
    print(f"  Triggered: {so['triggered']}")
    print(f"  Success Rate: {so['success_rate']:.0%}")
    print(f"  Fallback Count: {so['fallback_count']}")

    print("\n📈 DATA QUALITY")
    dq = report["summary"]["data_quality"]
    print(f"  Avg Coverage: {dq['avg_coverage']:.0%}")
    print(f"  RTH Compliance: {dq['rth_compliance']:.0%}")
    print(f"  Bad Symbol-Days: {dq['bad_symbol_days']}")

    if report["top_failures"]:
        print("\n❌ TOP FAILURE REASONS")
        for i, failure in enumerate(report["top_failures"][:5], 1):
            print(f"  {i}. {failure['reason']}: {failure['count']} occurrences")

    if report["top_performers"]:
        print("\n⭐ TOP PERFORMERS")
        for i, perf in enumerate(report["top_performers"][:5], 1):
            print(f"  {i}. {perf['strategy']}: AUC={perf['oos_auc']:.4f} (+{perf['improvement']:.4f})")

    print("\n💡 RECOMMENDATIONS")
    for rec in report["recommendations"]:
        print(f"  {rec}")

    print("\n" + "="*80)

    # Emit event
    try:
        from tools.nightly_oof_flow import publish_ml_event
        publish_ml_event(
            "completed",
            "weekly_report.generated",
            f"week_{week_label}",
            params={
                "start": args.start,
                "end": args.end,
                "training_success_rate": tj["success_rate"],
                "sync_success_rate": so["success_rate"]
            }
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
