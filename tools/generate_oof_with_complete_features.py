#!/usr/bin/env python3
"""
Generate OOF predictions using COMPLETE features from features_1m_with_labels table.

This solves the IC=0.0 problem by:
1. Using features_1m_with_labels (includes spy_ret_5, etc.)
2. Filtering out rows with NULL features
3. Using ALL available data (not just 1614 bars)

AI Brain can call this via Orchestrator action: generate_oof_complete

Usage:
    venv/bin/python tools/generate_oof_with_complete_features.py \
        --symbols TSLA \
        --start "2025-10-27 16:00:00" \
        --end "2025-11-26 16:00:00" \
        --horizon-min 5 \
        --output /tmp/oof_complete.json
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Dict, List, Any

import clickhouse_connect
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score


def load_complete_features(
    cli,
    symbol: str,
    start: str,
    end: str,
    horizon_min: int = 5,
    market_hours_only: bool = True,
    require_complete: bool = True
) -> pd.DataFrame:
    """
    Load features with ALL columns from features_1m_with_labels.

    Args:
        market_hours_only: Filter to 9:30 AM - 4:00 PM only
        require_complete: Drop rows with ANY NULL feature
    """

    # Build comprehensive feature list
    feature_cols = [
        "ret_5", "spy_ret_5", "atr_20", "zscore_volume_20",
        "atr_14", "z_price_20", "slope_20", "rsi_14", "vwap"
    ]

    select_cols = ["ts", "symbol", "close"] + feature_cols + [f"label_h{horizon_min}"]

    sql = f"""
    SELECT
        {", ".join(select_cols)}
    FROM trading.features_1m_with_labels
    WHERE symbol = %(symbol)s
        AND ts >= toDateTime(%(start)s)
        AND ts <= toDateTime(%(end)s)
    """

    # Add market hours filter
    if market_hours_only:
        sql += """
        AND toHour(ts) >= 9
        AND toHour(ts) < 16
        """

    # Add completeness filter (critical!)
    if require_complete:
        null_checks = []
        for col in feature_cols + [f"label_h{horizon_min}"]:
            null_checks.append(f"{col} IS NOT NULL")
        sql += f"""
        AND {" AND ".join(null_checks)}
        """

    sql += " ORDER BY ts"

    print(f"[load] Fetching {symbol} from {start} to {end}...")
    print(f"[load]   market_hours_only={market_hours_only}, require_complete={require_complete}")

    df = cli.query_df(sql, parameters={
        "symbol": symbol,
        "start": start,
        "end": end
    })

    print(f"[load] Loaded {len(df)} bars for {symbol}")

    return df


def train_simple_xgb(
    df: pd.DataFrame,
    feature_cols: List[str],
    label_col: str
) -> Dict[str, Any]:
    """
    Train a simple XGBoost model and compute metrics.

    Returns:
        - n: number of samples
        - auc: ROC AUC
        - ic: Information Coefficient
        - mean_pred: average prediction
        - mean_conf: average confidence
    """

    try:
        import xgboost as xgb
    except ImportError:
        print("[train] XGBoost not installed, using LogisticRegression")
        from sklearn.linear_model import LogisticRegression
        model = LogisticRegression(max_iter=1000)
        use_xgb = False
    else:
        model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.1,
            random_state=42
        )
        use_xgb = True

    # Prepare features and labels
    X = df[feature_cols].values
    y_continuous = df[label_col].values

    # Convert to binary classification (up/down)
    y = (y_continuous > 0).astype(int)

    # Split: first 70% train, last 30% test
    split_idx = int(len(df) * 0.7)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    y_continuous_test = y_continuous[split_idx:]

    print(f"[train] Training on {len(X_train)} samples, testing on {len(X_test)} samples")

    # Train
    model.fit(X_train, y_train)

    # Predict
    if use_xgb:
        y_pred_proba = model.predict_proba(X_test)[:, 1]
    else:
        y_pred_proba = model.predict_proba(X_test)[:, 1]

    y_pred = (y_pred_proba > 0.5).astype(int)

    # Compute metrics
    auc = roc_auc_score(y_test, y_pred_proba)

    # IC: Spearman correlation between predicted probability and actual continuous return
    ic, ic_pval = spearmanr(y_pred_proba, y_continuous_test)

    # Confidence: distance from 0.5
    conf = np.abs(y_pred_proba - 0.5)

    metrics = {
        "n": len(X_test),
        "auc": float(auc),
        "ic": float(ic),
        "ic_pval": float(ic_pval),
        "mean_pred": float(y_pred_proba.mean()),
        "mean_conf": float(conf.mean()),
        "accuracy": float((y_pred == y_test).mean())
    }

    print(f"[train] Metrics: AUC={auc:.4f}, IC={ic:.4f} (p={ic_pval:.4f}), Accuracy={metrics['accuracy']:.4f}")

    return metrics


def generate_oof_complete(
    symbols: List[str],
    start: str,
    end: str,
    horizon_min: int = 5,
    market_hours_only: bool = True,
    output_file: str = None
) -> Dict[str, Any]:
    """
    Generate OOF predictions using complete features.

    Returns JSON report similar to oof_flow_status.json.
    """

    cli = clickhouse_connect.get_client(host='localhost', port=8123, database='trading')

    report = {
        "symbols": symbols,
        "start": start,
        "end": end,
        "horizon_min": horizon_min,
        "timestamp": datetime.now().isoformat(),
        "per_symbol": {}
    }

    for symbol in symbols:
        print(f"\n{'=' * 80}")
        print(f"Processing {symbol}")
        print(f"{'=' * 80}")

        try:
            # Load complete features
            df = load_complete_features(
                cli,
                symbol=symbol,
                start=start,
                end=end,
                horizon_min=horizon_min,
                market_hours_only=market_hours_only,
                require_complete=True
            )

            if len(df) < 1000:
                print(f"[WARN] {symbol}: Not enough data ({len(df)} bars), skipping")
                report["per_symbol"][symbol] = {
                    "error": f"Not enough data: {len(df)} bars"
                }
                continue

            # Define features
            feature_cols = ["ret_5", "spy_ret_5", "atr_20", "zscore_volume_20"]
            label_col = f"label_h{horizon_min}"

            # Train and evaluate
            metrics = train_simple_xgb(df, feature_cols, label_col)

            report["per_symbol"][symbol] = {
                "params": {
                    "start": start,
                    "end": end,
                    "horizon_min": horizon_min,
                    "n_bars": len(df)
                },
                "metrics": metrics,
                "passed": metrics["auc"] > 0.52 and abs(metrics["ic"]) > 0.01
            }

        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")
            import traceback
            traceback.print_exc()
            report["per_symbol"][symbol] = {
                "error": str(e)
            }

    # Write to file
    if output_file:
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"\n[output] Report written to {output_file}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Generate OOF with complete features")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to process")
    parser.add_argument("--start", required=True, help="Start datetime")
    parser.add_argument("--end", required=True, help="End datetime")
    parser.add_argument("--horizon-min", type=int, default=5, help="Prediction horizon in minutes")
    parser.add_argument("--market-hours-only", action="store_true", default=True, help="Filter to market hours")
    parser.add_argument("--output", help="Output JSON file")

    args = parser.parse_args()

    report = generate_oof_complete(
        symbols=args.symbols,
        start=args.start,
        end=args.end,
        horizon_min=args.horizon_min,
        market_hours_only=args.market_hours_only,
        output_file=args.output
    )

    # Print summary
    print("\n" + "=" * 80)
    print("OOF GENERATION COMPLETE")
    print("=" * 80)

    for symbol, data in report["per_symbol"].items():
        if "error" in data:
            print(f"{symbol}: ERROR - {data['error']}")
        else:
            m = data["metrics"]
            print(f"{symbol}: n={m['n']}, AUC={m['auc']:.4f}, IC={m['ic']:.4f}, Passed={data['passed']}")

    # Exit code: 0 if all passed, 1 otherwise
    all_passed = all(data.get("passed", False) for data in report["per_symbol"].values())
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
