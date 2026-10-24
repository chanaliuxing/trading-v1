#!/usr/bin/env python3
"""
Label Quality Diagnostic Tool for AI Brain

AI 大脑可以自动诊断标签质量问题，识别：
1. 标签分布不均（太多 ZERO）
2. 标签噪音过大（std 太小）
3. 类别不平衡
4. 预测难度评估

Usage:
    venv/bin/python tools/diagnose_label_quality.py \
        --symbols TSLA AAPL \
        --start "2025-10-27 16:00:00" \
        --end "2025-11-26 16:00:00" \
        --horizons 30 60 120 \
        --output /tmp/label_quality_report.json
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Dict, List, Any

import clickhouse_connect
import numpy as np
import pandas as pd


def diagnose_label_quality(
    symbols: List[str],
    start: str,
    end: str,
    horizons: List[int] = [30, 60, 120],
    threshold_bps: float = 30.0,
    features_table: str = "trading.features_1m_with_labels",
    output_file: str = None,
    machine_readable: bool = False
) -> Dict[str, Any]:
    """
    Diagnose label quality for AI Brain.

    Returns comprehensive report with:
    - Label distribution (up/down/zero)
    - Label volatility
    - Predictability score
    - Recommended fixes
    """

    cli = clickhouse_connect.get_client(host='localhost', port=8123, database='trading')

    report = {
        "timestamp": datetime.now().isoformat(),
        "symbols": symbols,
        "start": start,
        "end": end,
        "horizons_tested": horizons,
        "threshold_bps": threshold_bps,
        "per_symbol": {},
        "recommendations": [],
        "auto_fix_actions": []
    }

    for symbol in symbols:
        if not machine_readable:
            print(f"[diagnose] Analyzing labels for {symbol}...")

        symbol_report = {}

        for horizon in horizons:
            label_col = f"label_h{horizon}"

            # Check if label exists
            check_query = f"""
            SELECT count(*) as cnt
            FROM system.columns
            WHERE database = 'trading'
                AND table = 'features_1m_with_labels'
                AND name = '{label_col}'
            """

            result = cli.query(check_query)
            if result.result_rows[0][0] == 0:
                if not machine_readable:
                    print(f"  [SKIP] {label_col} not found, skipping...")
                continue

            if not machine_readable:
                print(f"  [analyze] {label_col}...")

            # Analyze label distribution
            dist_query = f"""
            SELECT
                countIf({label_col} > {threshold_bps/10000}) as up_count,
                countIf({label_col} < -{threshold_bps/10000}) as down_count,
                countIf({label_col} >= -{threshold_bps/10000} AND {label_col} <= {threshold_bps/10000}) as zero_count,
                count(*) as total,
                avg({label_col}) as mean_label,
                stddevPop({label_col}) as std_label,
                min({label_col}) as min_label,
                max({label_col}) as max_label,
                quantile(0.05)({label_col}) as p5,
                quantile(0.95)({label_col}) as p95
            FROM {features_table}
            WHERE symbol = %(symbol)s
                AND ts >= toDateTime(%(start)s)
                AND ts <= toDateTime(%(end)s)
                AND toHour(ts) >= 9
                AND toHour(ts) < 16
                AND {label_col} IS NOT NULL
            """

            result = cli.query(dist_query, parameters={
                "symbol": symbol,
                "start": start,
                "end": end
            })

            row = result.result_rows[0]
            up = row[0]
            down = row[1]
            zero = row[2]
            total = row[3]
            mean = row[4]
            std = row[5]
            min_val = row[6]
            max_val = row[7]
            p5 = row[8]
            p95 = row[9]

            # Calculate metrics
            up_pct = up / total if total > 0 else 0
            down_pct = down / total if total > 0 else 0
            zero_pct = zero / total if total > 0 else 0

            # Predictability score (0-100)
            zero_score = max(0, 100 - zero_pct * 100)
            # Guard against missing std (e.g., empty dataset) to avoid TypeError
            if std is None:
                std_score = 0
                std = 0
            else:
                std_score = min(100, std * 10000)
            balance_score = 100 - abs(up_pct - down_pct) * 100

            predictability_score = (
                zero_score * 0.5 +      # 50% weight on low zero rate
                std_score * 0.3 +       # 30% weight on high volatility
                balance_score * 0.2     # 20% weight on balance
            )

            horizon_report = {
                "horizon_min": horizon,
                "total_samples": total,
                "distribution": {
                    "up": up,
                    "up_pct": float(up_pct),
                    "down": down,
                    "down_pct": float(down_pct),
                    "zero": zero,
                    "zero_pct": float(zero_pct)
                },
                "statistics": {
                    "mean": float(mean) if mean is not None else 0.0,
                    "std": float(std) if std is not None else 0.0,
                    "std_bps": float(std * 10000) if std is not None else 0.0,
                    "min": float(min_val) if min_val is not None else 0.0,
                    "max": float(max_val) if max_val is not None else 0.0,
                    "p5": float(p5) if p5 is not None else 0.0,
                    "p95": float(p95) if p95 is not None else 0.0,
                    "range_bps": float(((max_val or 0.0) - (min_val or 0.0)) * 10000)
                },
                "scores": {
                    "zero_score": float(zero_score),
                    "std_score": float(std_score),
                    "balance_score": float(balance_score),
                    "predictability_score": float(predictability_score)
                },
                "quality_rating": (
                    "EXCELLENT" if predictability_score >= 80 else
                    "GOOD" if predictability_score >= 60 else
                    "FAIR" if predictability_score >= 40 else
                    "POOR"
                )
            }

            symbol_report[f"h{horizon}"] = horizon_report

            issues = []
            if zero_pct > 0.5:
                issues.append(f"High zero rate: {zero_pct*100:.1f}% (threshold: {threshold_bps} bps)")
            if std * 10000 < 50:
                issues.append(f"Low volatility: {std*10000:.1f} bps")
            if abs(up_pct - down_pct) > 0.2:
                issues.append(f"Imbalanced: up={up_pct*100:.1f}%, down={down_pct*100:.1f}%")

            if issues:
                report["recommendations"].append({
                    "symbol": symbol,
                    "horizon": horizon,
                    "quality": horizon_report["quality_rating"],
                    "issues": issues
                })

        report["per_symbol"][symbol] = symbol_report

    # Generate auto-fix actions
    # Find best horizon for each symbol
    for symbol, horizons_data in report["per_symbol"].items():
        if not horizons_data:
            continue

        # Sort by predictability score
        sorted_horizons = sorted(
            horizons_data.items(),
            key=lambda x: x[1]["scores"]["predictability_score"],
            reverse=True
        )

        best_horizon_key, best_data = sorted_horizons[0]
        best_horizon = best_data["horizon_min"]
        best_score = best_data["scores"]["predictability_score"]

        current_horizon = horizons[0]  # Assume first is current

        if best_horizon != current_horizon and best_score > 50:
            report["auto_fix_actions"].append({
                "action": "change_horizon",
                "params": {
                    "symbol": symbol,
                    "from_horizon": current_horizon,
                    "to_horizon": best_horizon,
                    "reason": f"Better predictability score: {best_score:.1f} vs {horizons_data[f'h{current_horizon}']['scores']['predictability_score']:.1f}",
                    "improvements": {
                        "zero_rate": f"{horizons_data[f'h{current_horizon}']['distribution']['zero_pct']:.1%} → {best_data['distribution']['zero_pct']:.1%}",
                        "std_bps": f"{horizons_data[f'h{current_horizon}']['statistics']['std_bps']:.1f} → {best_data['statistics']['std_bps']:.1f}"
                    }
                }
            })

        # Check if threshold-based labeling would help
        current_zero_pct = horizons_data[f"h{current_horizon}"]["distribution"]["zero_pct"]
        if current_zero_pct > 0.4:  # >40% zero
            report["auto_fix_actions"].append({
                "action": "use_threshold_labeling",
                "params": {
                    "symbol": symbol,
                    "current_zero_pct": current_zero_pct,
                    "recommended_threshold_bps": threshold_bps,
                    "reason": f"High zero rate ({current_zero_pct*100:.1f}%), filter out small movements"
                }
            })

    # Overall assessment
    all_scores = []
    for sym_data in report["per_symbol"].values():
        for h_data in sym_data.values():
            all_scores.append(h_data["scores"]["predictability_score"])

    if all_scores:
        avg_score = sum(all_scores) / len(all_scores)
        report["overall_assessment"] = {
            "average_predictability_score": float(avg_score),
            "label_quality": (
                "EXCELLENT" if avg_score >= 80 else
                "GOOD" if avg_score >= 60 else
                "FAIR" if avg_score >= 40 else
                "POOR"
            ),
            "requires_fix": avg_score < 60
        }
    else:
        report["overall_assessment"] = {
            "label_quality": "UNKNOWN",
            "requires_fix": False
        }

    # Write to file
    if output_file:
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)
        if not machine_readable:
            print(f"\n[output] Report written to {output_file}")
    elif machine_readable:
        print(json.dumps(report, indent=2))

    return report


def main():
    parser = argparse.ArgumentParser(description="Diagnose label quality for AI Brain")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to analyze")
    parser.add_argument("--start", required=True, help="Start datetime")
    parser.add_argument("--end", required=True, help="End datetime")
    parser.add_argument("--horizons", nargs="+", type=int, default=[30, 60, 120], help="Horizons to test (minutes)")
    parser.add_argument("--threshold-bps", type=float, default=30.0, help="Threshold for zero/non-zero (bps)")
    parser.add_argument("--features-table", default="trading.features_1m_with_labels", help="Features table to use")
    parser.add_argument("--output", help="Output JSON file")
    parser.add_argument("--machine-readable", action="store_true", help="Output only JSON to stdout") # <-- ADDED

    args = parser.parse_args()

    report = diagnose_label_quality(
        symbols=args.symbols,
        start=args.start,
        end=args.end,
        horizons=args.horizons,
        threshold_bps=args.threshold_bps,
        features_table=args.features_table,
        output_file=args.output,
        machine_readable=args.machine_readable # <-- ADDED
    )

    if not args.machine_readable: # <-- ADDED
        # Print summary
        print("\n" + "=" * 80)
        print("LABEL QUALITY DIAGNOSTIC REPORT")
        print("=" * 80)

        for symbol, horizons_data in report["per_symbol"].items():
            print(f"\n{symbol}:")
            for h_key, h_data in sorted(horizons_data.items()):
                horizon = h_data["horizon_min"]
                score = h_data["scores"]["predictability_score"]
                quality = h_data["quality_rating"]
                zero_pct = h_data["distribution"]["zero_pct"]
                std_bps = h_data["statistics"]["std_bps"]

                print(f"  Horizon {horizon:3}m: Score={score:5.1f} ({quality:9}) | Zero={zero_pct:5.1%} | Std={std_bps:5.1f}bps")

        print(f"\n{'=' * 80}")
        print(f"Overall: {report['overall_assessment']['label_quality']}")
        print(f"Requires Fix: {report['overall_assessment']['requires_fix']}")
        print(f"{'=' * 80}")

        if report['auto_fix_actions']:
            print("\nAUTO-FIX ACTIONS:")
            for i, action in enumerate(report['auto_fix_actions'], 1):
                print(f"  {i}. {action['action']}")
                print(f"     → {action['params'].get('reason', '')}")

        if report['recommendations']:
            print("\nRECOMMENDATIONS:")
            for i, rec in enumerate(report['recommendations'], 1):
                print(f"  {i}. [{rec['symbol']}] H{rec['horizon']}: {rec['quality']}")
                for issue in rec['issues']:
                    print(f"     - {issue}")

        print()

    return 0 if report['overall_assessment']['label_quality'] in ['EXCELLENT', 'GOOD'] else 1


if __name__ == "__main__":
    sys.exit(main())
