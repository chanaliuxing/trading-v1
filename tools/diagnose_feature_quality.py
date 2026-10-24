#!/usr/bin/env python3
"""
Feature Quality Diagnostic Tool for AI Brain

AI 大脑可以通过 Orchestrator 调用此工具来自动诊断特征质量问题。

Usage:
    venv/bin/python tools/diagnose_feature_quality.py --symbols TSLA AAPL --start 2025-10-27 --end 2025-11-26

Returns JSON report with:
    - Feature completeness (NULL %)
    - Information Coefficient (IC)
    - Recommended data filters
    - Auto-fix suggestions
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Dict, List, Any

import clickhouse_connect
import pandas as pd
from scipy.stats import spearmanr


def diagnose_feature_quality(
    symbols: List[str],
    start: str,
    end: str,
    table: str = "trading.features_1m_with_labels",
    output_file: str = None
) -> Dict[str, Any]:
    """
    Diagnose feature quality issues for AI Brain.

    Returns comprehensive report with actionable recommendations.
    """

    cli = clickhouse_connect.get_client(host='localhost', port=8123, database='trading')

    report = {
        "timestamp": datetime.now().isoformat(),
        "symbols": symbols,
        "start": start,
        "end": end,
        "table": table,
        "per_symbol": {},
        "recommendations": [],
        "auto_fix_actions": []
    }

    for symbol in symbols:
        print(f"[diagnose] Analyzing {symbol}...")

        # 1. Check feature completeness
        completeness_query = """
        SELECT
            count(*) as total_bars,
            countIf(ret_5 IS NOT NULL) as ret5_not_null,
            countIf(spy_ret_5 IS NOT NULL) as spy_ret5_not_null,
            countIf(atr_20 IS NOT NULL) as atr20_not_null,
            countIf(zscore_volume_20 IS NOT NULL) as vol_zscore_not_null,
            countIf(label_h30 IS NOT NULL) as label_not_null,
            countIf(
                ret_5 IS NOT NULL AND
                spy_ret_5 IS NOT NULL AND
                atr_20 IS NOT NULL AND
                zscore_volume_20 IS NOT NULL AND
                label_h30 IS NOT NULL
            ) as complete_rows,
            min(ts) as earliest,
            max(ts) as latest
        FROM {table}
        WHERE symbol = %(symbol)s
            AND ts >= toDateTime(%(start)s)
            AND ts <= toDateTime(%(end)s)
        """.format(table=table)

        result = cli.query(completeness_query, parameters={
            "symbol": symbol,
            "start": start,
            "end": end
        })

        row = result.result_rows[0]
        total = row[0]

        completeness = {
            "total_bars": total,
            "ret_5_complete": row[1],
            "spy_ret_5_complete": row[2],
            "atr_20_complete": row[3],
            "vol_zscore_20_complete": row[4],
            "label_h30_complete": row[5],
            "all_features_complete": row[6],
            "earliest": str(row[7]),
            "latest": str(row[8]),
            "completeness_rate": row[6] / total if total > 0 else 0.0
        }

        # 2. Identify NULL patterns (by hour)
        null_pattern_query = """
        SELECT
            toHour(ts) as hour,
            countIf(spy_ret_5 IS NULL) as spy_ret5_null,
            count(*) as total
        FROM {table}
        WHERE symbol = %(symbol)s
            AND ts >= toDateTime(%(start)s)
            AND ts <= toDateTime(%(end)s)
        GROUP BY hour
        ORDER BY hour
        """.format(table=table)

        result2 = cli.query(null_pattern_query, parameters={
            "symbol": symbol,
            "start": start,
            "end": end
        })

        null_hours = []
        for row in result2.result_rows:
            hour = row[0]
            spy_null = row[1]
            total = row[2]
            null_rate = spy_null / total if total > 0 else 0.0
            if null_rate > 0.5:  # >50% NULL
                null_hours.append({
                    "hour": hour,
                    "null_count": spy_null,
                    "total": total,
                    "null_rate": null_rate
                })

        # 3. Calculate IC on complete rows only
        ic_query = """
        SELECT
            ret_5,
            spy_ret_5,
            label_h30
        FROM {table}
        WHERE symbol = %(symbol)s
            AND ts >= toDateTime(%(start)s)
            AND ts <= toDateTime(%(end)s)
            AND ret_5 IS NOT NULL
            AND spy_ret_5 IS NOT NULL
            AND label_h30 IS NOT NULL
        ORDER BY ts
        """.format(table=table)

        df = cli.query_df(ic_query, parameters={
            "symbol": symbol,
            "start": start,
            "end": end
        })

        ic_results = {}
        if len(df) > 100:  # Need enough data for IC
            ic_ret5, pval_ret5 = spearmanr(df['ret_5'], df['label_h30'])
            ic_spy_ret5, pval_spy_ret5 = spearmanr(df['spy_ret_5'], df['label_h30'])

            ic_results = {
                "n_bars": len(df),
                "ic_ret5": float(ic_ret5),
                "ic_ret5_pval": float(pval_ret5),
                "ic_spy_ret5": float(ic_spy_ret5),
                "ic_spy_ret5_pval": float(pval_spy_ret5),
                "ic_ret5_significant": bool(pval_ret5 < 0.05),
                "ic_spy_ret5_significant": bool(pval_spy_ret5 < 0.05)
            }
        else:
            ic_results = {
                "n_bars": len(df),
                "error": "Not enough data for IC calculation"
            }

        # 4. Market hours only analysis
        market_hours_query = """
        SELECT
            count(*) as total_bars,
            countIf(
                ret_5 IS NOT NULL AND
                spy_ret_5 IS NOT NULL AND
                label_h30 IS NOT NULL
            ) as complete_rows
        FROM {table}
        WHERE symbol = %(symbol)s
            AND ts >= toDateTime(%(start)s)
            AND ts <= toDateTime(%(end)s)
            AND toHour(ts) >= 9
            AND toHour(ts) < 16
        """.format(table=table)

        result3 = cli.query(market_hours_query, parameters={
            "symbol": symbol,
            "start": start,
            "end": end
        })

        market_hours = {
            "total_bars": result3.result_rows[0][0],
            "complete_bars": result3.result_rows[0][1],
            "completeness_rate": result3.result_rows[0][1] / result3.result_rows[0][0] if result3.result_rows[0][0] > 0 else 0.0
        }

        # Store results
        report["per_symbol"][symbol] = {
            "completeness": completeness,
            "null_hours": null_hours,
            "ic_analysis": ic_results,
            "market_hours_only": market_hours
        }

        # Generate recommendations for this symbol
        if completeness["completeness_rate"] < 0.7:
            report["recommendations"].append({
                "symbol": symbol,
                "issue": f"Low completeness rate: {completeness['completeness_rate']:.1%}",
                "solution": "Filter data to market hours only (9:30 AM - 4:00 PM)"
            })

        if null_hours:
            report["recommendations"].append({
                "symbol": symbol,
                "issue": f"High NULL rate during hours: {[h['hour'] for h in null_hours]}",
                "solution": "Apply time-based filter to exclude pre-market and after-hours"
            })

        if ic_results.get("ic_ret5") is not None and abs(ic_results["ic_ret5"]) < 0.01:
            report["recommendations"].append({
                "symbol": symbol,
                "issue": f"Weak IC for ret_5: {ic_results['ic_ret5']:.5f}",
                "solution": "Feature has weak predictive power, consider feature engineering"
            })

    # Generate auto-fix actions
    if any(s["completeness"]["completeness_rate"] < 0.7 for s in report["per_symbol"].values()):
        report["auto_fix_actions"].append({
            "action": "apply_market_hours_filter",
            "params": {
                "hour_start": 9,
                "hour_end": 16,
                "reason": "Improve data completeness by filtering to market hours"
            }
        })

    if any(len(s["null_hours"]) > 0 for s in report["per_symbol"].values()):
        report["auto_fix_actions"].append({
            "action": "apply_completeness_filter",
            "params": {
                "require_all_features": True,
                "reason": "Remove rows with NULL features to improve model training"
            }
        })

    # Overall assessment
    report["overall_assessment"] = {
        "data_quality": "GOOD" if all(s["completeness"]["completeness_rate"] > 0.8 for s in report["per_symbol"].values()) else "POOR",
        "requires_fix": len(report["auto_fix_actions"]) > 0,
        "fix_count": len(report["auto_fix_actions"])
    }

    # Write to file if specified
    if output_file:
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"[diagnose] Report written to {output_file}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Diagnose feature quality for AI Brain")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to analyze")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--table", default="trading.features_1m_with_labels", help="Table name")
    parser.add_argument("--output", help="Output JSON file path")

    args = parser.parse_args()

    report = diagnose_feature_quality(
        symbols=args.symbols,
        start=args.start,
        end=args.end,
        table=args.table,
        output_file=args.output
    )

    # Print summary
    print("\n" + "=" * 80)
    print("FEATURE QUALITY DIAGNOSTIC REPORT")
    print("=" * 80)

    for symbol, data in report["per_symbol"].items():
        print(f"\n{symbol}:")
        print(f"  Total bars:          {data['completeness']['total_bars']}")
        print(f"  Complete rows:       {data['completeness']['all_features_complete']} ({data['completeness']['completeness_rate']:.1%})")
        print(f"  Market hours only:   {data['market_hours_only']['complete_bars']} ({data['market_hours_only']['completeness_rate']:.1%})")

        if data['ic_analysis'].get('ic_ret5') is not None:
            print(f"  IC(ret_5):           {data['ic_analysis']['ic_ret5']:.5f} (p={data['ic_analysis']['ic_ret5_pval']:.4f})")
            print(f"  IC(spy_ret_5):       {data['ic_analysis']['ic_spy_ret5']:.5f} (p={data['ic_analysis']['ic_spy_ret5_pval']:.4f})")

        if data['null_hours']:
            print(f"  NULL hours:          {[h['hour'] for h in data['null_hours']]}")

    print(f"\n{'=' * 80}")
    print(f"Overall Assessment: {report['overall_assessment']['data_quality']}")
    print(f"Requires Fix:       {report['overall_assessment']['requires_fix']}")
    print(f"{'=' * 80}")

    if report['recommendations']:
        print("\nRECOMMENDATIONS:")
        for i, rec in enumerate(report['recommendations'], 1):
            print(f"  {i}. [{rec['symbol']}] {rec['issue']}")
            print(f"     → {rec['solution']}")

    if report['auto_fix_actions']:
        print("\nAUTO-FIX ACTIONS:")
        for i, action in enumerate(report['auto_fix_actions'], 1):
            print(f"  {i}. {action['action']}")
            print(f"     Params: {action['params']}")

    print()

    return 0 if report['overall_assessment']['data_quality'] == 'GOOD' else 1


if __name__ == "__main__":
    sys.exit(main())
