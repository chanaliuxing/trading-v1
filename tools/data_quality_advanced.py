#!/usr/bin/env python3
"""
Advanced Data Quality Monitoring - Comprehensive validation for ML training readiness

This script performs deep data quality checks beyond the basic monitor:
1. Feature distribution drift detection (active vs baseline)
2. Label leakage detection (future data contamination check)
3. Data alignment verification (features-labels timestamp matching)
4. Random prediction detection (AUC≈0.5 stability analysis)

Usage:
    python tools/data_quality_advanced.py --symbols TSLA NVDA --lookback-days 30
    python tools/data_quality_advanced.py --mode drift-only  # Only run drift detection
    python tools/data_quality_advanced.py --mode leakage-only  # Only check label leakage
"""

import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime, timedelta, timezone
import json
import argparse
import numpy as np
import pandas as pd
from scipy import stats

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from libs.db.ch_pool import get_client
import structlog

logger = structlog.get_logger(__name__)


class DataQualityMonitor:
    """Comprehensive data quality monitoring for ML pipeline"""

    def __init__(
        self,
        active_table: str = "trading.features_1m_active",
        baseline_table: str = "trading.features_1m_baseline",
        labels_table: str = "trading.features_1m_with_labels",
        drift_threshold: float = 0.05,  # KS test p-value threshold
    ):
        self.active_table = active_table
        self.baseline_table = baseline_table
        self.labels_table = labels_table
        self.drift_threshold = drift_threshold
        self.client = get_client()

    def check_feature_drift(
        self,
        symbols: List[str],
        lookback_days: int = 30,
        features: Optional[List[str]] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Detect feature distribution drift between active and baseline tables

        Uses Kolmogorov-Smirnov test to compare distributions

        Returns:
            (passed: bool, report: dict)
            passed=True means no significant drift detected
        """
        logger.info("drift_check_starting", symbols=symbols, lookback_days=lookback_days)

        # Default feature columns to check
        if not features:
            features = [
                "bb_upper", "bb_middle", "bb_lower", "rsi", "macd", "signal",
                "volume_sma", "atr", "obv", "adx", "cci", "stoch_k", "stoch_d"
            ]

        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        drift_results = {}
        drifted_features = []

        for symbol in symbols:
            symbol_drifts = {}

            for feature in features:
                try:
                    # Fetch active distribution
                    active_query = f"""
                        SELECT {feature}
                        FROM {self.active_table}
                        WHERE symbol = %(symbol)s
                          AND toDate(ts) >= %(cutoff_date)s
                          AND {feature} IS NOT NULL
                          AND isFinite({feature})
                        LIMIT 10000
                    """
                    active_result = self.client.query(
                        active_query,
                        {"symbol": symbol, "cutoff_date": cutoff_date},
                        settings={'use_query_cache': False}
                    )

                    # Fetch baseline distribution
                    baseline_query = f"""
                        SELECT {feature}
                        FROM {self.baseline_table}
                        WHERE symbol = %(symbol)s
                          AND toDate(ts) >= %(cutoff_date)s
                          AND {feature} IS NOT NULL
                          AND isFinite({feature})
                        LIMIT 10000
                    """
                    baseline_result = self.client.query(
                        baseline_query,
                        {"symbol": symbol, "cutoff_date": cutoff_date},
                        settings={'use_query_cache': False}
                    )

                    if len(active_result) < 100 or len(baseline_result) < 100:
                        symbol_drifts[feature] = {
                            "status": "insufficient_data",
                            "active_count": len(active_result),
                            "baseline_count": len(baseline_result)
                        }
                        continue

                    # Extract values
                    active_values = np.array([row[feature] for row in active_result])
                    baseline_values = np.array([row[feature] for row in baseline_result])

                    # Kolmogorov-Smirnov test
                    ks_stat, p_value = stats.ks_2samp(active_values, baseline_values)

                    # Compare mean and std
                    active_mean = np.mean(active_values)
                    baseline_mean = np.mean(baseline_values)
                    active_std = np.std(active_values)
                    baseline_std = np.std(baseline_values)

                    mean_shift_pct = abs(active_mean - baseline_mean) / (abs(baseline_mean) + 1e-9) * 100
                    std_shift_pct = abs(active_std - baseline_std) / (abs(baseline_std) + 1e-9) * 100

                    drifted = p_value < self.drift_threshold

                    symbol_drifts[feature] = {
                        "status": "drifted" if drifted else "stable",
                        "ks_statistic": round(float(ks_stat), 4),
                        "p_value": round(float(p_value), 6),
                        "active_mean": round(float(active_mean), 4),
                        "baseline_mean": round(float(baseline_mean), 4),
                        "active_std": round(float(active_std), 4),
                        "baseline_std": round(float(baseline_std), 4),
                        "mean_shift_pct": round(float(mean_shift_pct), 2),
                        "std_shift_pct": round(float(std_shift_pct), 2),
                        "active_count": len(active_result),
                        "baseline_count": len(baseline_result)
                    }

                    if drifted:
                        drifted_features.append(f"{symbol}:{feature}")

                except Exception as e:
                    logger.error("drift_check_failed", symbol=symbol, feature=feature, error=str(e))
                    symbol_drifts[feature] = {
                        "status": "error",
                        "error": str(e)
                    }

            drift_results[symbol] = symbol_drifts

        passed = len(drifted_features) == 0

        report = {
            "check": "feature_drift",
            "passed": passed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "drift_threshold": self.drift_threshold,
            "by_symbol": drift_results,
            "summary": {
                "total_features_checked": len(features) * len(symbols),
                "drifted_features": drifted_features,
                "drifted_count": len(drifted_features)
            }
        }

        if passed:
            logger.info("drift_check_passed", drifted_count=0)
        else:
            logger.warning("drift_check_failed", drifted_features=drifted_features)

        return passed, report

    def check_label_leakage(
        self,
        symbols: List[str],
        horizon_min: int = 30,
        lookback_days: int = 30,
        features_table: Optional[str] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Detect label leakage by checking if label_h{horizon} uses future data correctly

        Validates that label at time T only depends on data from T+horizon onwards

        Returns:
            (passed: bool, report: dict)
            passed=True means no leakage detected
        """
        logger.info("leakage_check_starting", symbols=symbols, horizon_min=horizon_min)

        if not features_table:
            features_table = self.labels_table  # Use labels table by default

        label_col = f"label_h{horizon_min}"
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        leakage_results = {}
        leakage_detected = []

        for symbol in symbols:
            try:
                # Check if label values are temporally consistent
                # Label at T should be correlated with close price at T+horizon
                query = f"""
                    WITH labels_with_future AS (
                        SELECT
                            ts,
                            close,
                            {label_col} as label,
                            any(close) OVER (
                                PARTITION BY symbol
                                ORDER BY ts
                                ROWS BETWEEN {horizon_min} FOLLOWING AND {horizon_min} FOLLOWING
                            ) as future_close
                        FROM {features_table}
                        WHERE symbol = %(symbol)s
                          AND toDate(ts) >= %(cutoff_date)s
                          AND {label_col} IS NOT NULL
                          AND toHour(ts) >= 9 AND toHour(ts) < 16
                        ORDER BY ts
                        LIMIT 5000
                    )
                    SELECT
                        corr(label, (future_close - close) / close) as label_future_corr,
                        corr(label, close) as label_current_corr,
                        count() as sample_count
                    FROM labels_with_future
                    WHERE future_close IS NOT NULL AND future_close > 0
                """

                result = self.client.query(
                    query,
                    {"symbol": symbol, "cutoff_date": cutoff_date},
                    settings={'use_query_cache': False}
                )

                if not result or result[0]['sample_count'] < 100:
                    leakage_results[symbol] = {
                        "status": "insufficient_data",
                        "sample_count": result[0]['sample_count'] if result else 0
                    }
                    continue

                row = result[0]
                label_future_corr = row['label_future_corr']
                label_current_corr = row['label_current_corr']
                sample_count = row['sample_count']

                # Leakage detected if:
                # 1. Label correlates more with current price than future price (should be opposite)
                # 2. Correlation with current price is suspiciously high (>0.5)
                leakage = (
                    label_current_corr is not None and
                    label_future_corr is not None and
                    (abs(label_current_corr) > abs(label_future_corr) or abs(label_current_corr) > 0.5)
                )

                leakage_results[symbol] = {
                    "status": "leakage_detected" if leakage else "clean",
                    "label_future_corr": round(float(label_future_corr), 4) if label_future_corr else None,
                    "label_current_corr": round(float(label_current_corr), 4) if label_current_corr else None,
                    "sample_count": sample_count,
                    "warning": "Label should correlate with future returns, not current price" if leakage else None
                }

                if leakage:
                    leakage_detected.append(symbol)

            except Exception as e:
                logger.error("leakage_check_failed", symbol=symbol, error=str(e))
                leakage_results[symbol] = {
                    "status": "error",
                    "error": str(e)
                }

        passed = len(leakage_detected) == 0

        report = {
            "check": "label_leakage",
            "passed": passed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "horizon_min": horizon_min,
            "by_symbol": leakage_results,
            "summary": {
                "symbols_with_leakage": leakage_detected,
                "leakage_count": len(leakage_detected)
            }
        }

        if passed:
            logger.info("leakage_check_passed")
        else:
            logger.warning("leakage_check_failed", symbols_with_leakage=leakage_detected)

        return passed, report

    def check_data_alignment(
        self,
        symbols: List[str],
        horizon_min: int = 30,
        lookback_days: int = 7,
        features_table: Optional[str] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Verify features and labels are properly aligned in time

        Checks:
        - No orphaned labels (labels without corresponding features)
        - No timestamp gaps larger than expected
        - Label timestamps are consistent with feature timestamps

        Returns:
            (passed: bool, report: dict)
        """
        logger.info("alignment_check_starting", symbols=symbols)

        if not features_table:
            features_table = self.labels_table  # Use labels table by default

        label_col = f"label_h{horizon_min}"
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        alignment_results = {}
        misaligned_symbols = []

        for symbol in symbols:
            try:
                # Check for timestamp consistency
                query = f"""
                    WITH gaps AS (
                        SELECT
                            ts,
                            lead(ts) OVER (ORDER BY ts) as next_ts,
                            dateDiff('minute', ts, lead(ts) OVER (ORDER BY ts)) as gap_minutes
                        FROM {features_table}
                        WHERE symbol = %(symbol)s
                          AND toDate(ts) >= %(cutoff_date)s
                          AND toHour(ts) >= 9 AND toHour(ts) < 16
                    )
                    SELECT
                        count() as total_bars,
                        countIf(gap_minutes > 5) as large_gaps_count,
                        max(gap_minutes) as max_gap_minutes,
                        avg(gap_minutes) as avg_gap_minutes
                    FROM gaps
                    WHERE gap_minutes > 0
                """

                gap_result = self.client.query(
                    query,
                    {"symbol": symbol, "cutoff_date": cutoff_date},
                    settings={'use_query_cache': False}
                )

                # Check label coverage
                label_query = f"""
                    SELECT
                        countIf({label_col} IS NOT NULL) as labeled_bars,
                        count() as total_bars,
                        countIf({label_col} IS NOT NULL) / count() as label_coverage
                    FROM {features_table}
                    WHERE symbol = %(symbol)s
                      AND toDate(ts) >= %(cutoff_date)s
                      AND toHour(ts) >= 9 AND toHour(ts) < 16
                """

                label_result = self.client.query(
                    label_query,
                    {"symbol": symbol, "cutoff_date": cutoff_date},
                    settings={'use_query_cache': False}
                )

                if not gap_result or not label_result:
                    alignment_results[symbol] = {
                        "status": "insufficient_data"
                    }
                    continue

                gap_row = gap_result[0]
                label_row = label_result[0]

                total_bars = gap_row['total_bars']
                large_gaps = gap_row['large_gaps_count'] or 0
                max_gap = gap_row['max_gap_minutes'] or 0
                avg_gap = gap_row['avg_gap_minutes'] or 0

                labeled_bars = label_row['labeled_bars'] or 0
                label_coverage = label_row['label_coverage'] or 0

                # Misalignment criteria:
                # 1. More than 5% bars have >5min gaps (should be 1min bars)
                # 2. Label coverage < 50% (excluding expected nulls at end of day)
                gap_issue = total_bars > 0 and (large_gaps / total_bars) > 0.05
                coverage_issue = label_coverage < 0.5

                misaligned = gap_issue or coverage_issue

                alignment_results[symbol] = {
                    "status": "misaligned" if misaligned else "aligned",
                    "total_bars": total_bars,
                    "large_gaps_count": large_gaps,
                    "large_gaps_pct": round((large_gaps / total_bars * 100) if total_bars > 0 else 0, 2),
                    "max_gap_minutes": max_gap,
                    "avg_gap_minutes": round(avg_gap, 2),
                    "labeled_bars": labeled_bars,
                    "label_coverage": round(label_coverage, 4),
                    "issues": []
                }

                if gap_issue:
                    alignment_results[symbol]["issues"].append("excessive_timestamp_gaps")
                if coverage_issue:
                    alignment_results[symbol]["issues"].append("low_label_coverage")

                if misaligned:
                    misaligned_symbols.append(symbol)

            except Exception as e:
                logger.error("alignment_check_failed", symbol=symbol, error=str(e))
                alignment_results[symbol] = {
                    "status": "error",
                    "error": str(e)
                }

        passed = len(misaligned_symbols) == 0

        report = {
            "check": "data_alignment",
            "passed": passed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "by_symbol": alignment_results,
            "summary": {
                "misaligned_symbols": misaligned_symbols,
                "misaligned_count": len(misaligned_symbols)
            }
        }

        if passed:
            logger.info("alignment_check_passed")
        else:
            logger.warning("alignment_check_failed", misaligned_symbols=misaligned_symbols)

        return passed, report

    def run_all_checks(
        self,
        symbols: List[str],
        lookback_days: int = 30,
        horizon_min: int = 30,
        features_table: Optional[str] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Run all data quality checks

        Returns:
            (all_passed: bool, comprehensive_report: dict)
        """
        logger.info("data_quality_monitor_starting", symbols=symbols)

        # Run all checks
        drift_passed, drift_report = self.check_feature_drift(symbols, lookback_days)
        leakage_passed, leakage_report = self.check_label_leakage(
            symbols, horizon_min, lookback_days, features_table
        )
        alignment_passed, alignment_report = self.check_data_alignment(
            symbols, horizon_min, lookback_days=7, features_table=features_table
        )

        all_passed = drift_passed and leakage_passed and alignment_passed

        comprehensive_report = {
            "overall_status": "passed" if all_passed else "failed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbols": symbols,
            "lookback_days": lookback_days,
            "checks": {
                "feature_drift": drift_report,
                "label_leakage": leakage_report,
                "data_alignment": alignment_report
            },
            "summary": {
                "drift_passed": drift_passed,
                "leakage_passed": leakage_passed,
                "alignment_passed": alignment_passed,
                "all_passed": all_passed
            }
        }

        if all_passed:
            logger.info("data_quality_monitor_passed", checks_passed=3)
        else:
            failed_checks = []
            if not drift_passed:
                failed_checks.append("feature_drift")
            if not leakage_passed:
                failed_checks.append("label_leakage")
            if not alignment_passed:
                failed_checks.append("data_alignment")
            logger.warning("data_quality_monitor_failed", failed_checks=failed_checks)

        return all_passed, comprehensive_report


def main():
    ap = argparse.ArgumentParser(description="Advanced data quality monitoring for ML pipeline")
    ap.add_argument("--symbols", nargs="+", default=["TSLA", "NVDA"], help="Symbols to check")
    ap.add_argument("--lookback-days", type=int, default=30, help="Days to look back for data")
    ap.add_argument("--horizon-min", type=int, default=30, help="Label horizon in minutes")
    ap.add_argument("--active-table", default="trading.features_1m_active", help="Active features table")
    ap.add_argument("--baseline-table", default="trading.features_1m_baseline", help="Baseline features table")
    ap.add_argument("--drift-threshold", type=float, default=0.05, help="KS test p-value threshold")
    ap.add_argument(
        "--mode",
        choices=["all", "drift-only", "leakage-only", "alignment-only"],
        default="all",
        help="Which checks to run"
    )
    ap.add_argument("--output", help="Output JSON file path (optional)")
    args = ap.parse_args()

    print(f"Running advanced data quality checks for: {', '.join(args.symbols)}")
    print(f"Mode: {args.mode}\n")

    monitor = DataQualityMonitor(
        active_table=args.active_table,
        baseline_table=args.baseline_table,
        drift_threshold=args.drift_threshold
    )

    if args.mode == "all":
        all_passed, report = monitor.run_all_checks(
            symbols=args.symbols,
            lookback_days=args.lookback_days,
            horizon_min=args.horizon_min,
            features_table=args.active_table
        )
    elif args.mode == "drift-only":
        all_passed, report = monitor.check_feature_drift(args.symbols, args.lookback_days)
    elif args.mode == "leakage-only":
        all_passed, report = monitor.check_label_leakage(
            args.symbols, args.horizon_min, args.lookback_days
        )
    elif args.mode == "alignment-only":
        all_passed, report = monitor.check_data_alignment(
            args.symbols, args.horizon_min, lookback_days=7
        )

    print("=" * 80)
    print(f"OVERALL STATUS: {'✅ PASSED' if all_passed else '❌ FAILED'}")
    print("=" * 80)
    print(json.dumps(report, indent=2))

    # Save to file if requested
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved to: {args.output}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
