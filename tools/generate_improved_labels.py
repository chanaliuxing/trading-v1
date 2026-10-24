#!/usr/bin/env python3
"""
Generate Improved Labels with Multiple Horizons and Strategies

AI 大脑可以自动生成和测试不同的标签策略：
1. 多个 horizon (30m, 60m, 120m)
2. Threshold-based labeling (过滤小移动)
3. Multi-class labeling (强上/平/强下)

Usage:
    venv/bin/python tools/generate_improved_labels.py \
        --symbols TSLA AAPL \
        --start "2025-10-01" \
        --end "2025-11-26" \
        --horizons 30 60 120 \
        --strategies simple threshold multiclass \
        --output /tmp/improved_labels_result.json
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Dict, List, Any

import clickhouse_connect
import numpy as np
import pandas as pd


def generate_labels_simple(
    cli,
    symbol: str,
    start: str,
    end: str,
    horizons: List[int]
) -> Dict[str, int]:
    """
    Generate simple forward return labels for multiple horizons.

    Returns: {label_h30: row_count, label_h60: row_count, ...}
    """

    print(f"[generate] Simple labels for {symbol}, horizons={horizons}...")

    # Build the query with leadInFrame for all horizons
    lead_cols = []
    for h in horizons:
        lead_cols.append(
            f"leadInFrame(close, {h}) OVER (PARTITION BY symbol ORDER BY ts "
            f"ROWS BETWEEN CURRENT ROW AND {h} FOLLOWING) AS lead_close_{h}"
        )

    label_calcs = []
    for h in horizons:
        label_calc = f"""
        CASE
            WHEN close = 0 THEN NULL
            WHEN close IS NULL THEN NULL
            WHEN lead_close_{h} = 0 THEN NULL
            WHEN lead_close_{h} IS NULL THEN NULL
            ELSE (lead_close_{h} - close) / close
        END AS label_h{h}
        """
        label_calcs.append(label_calc.strip())

    # Get existing columns
    existing_cols_query = """
    SELECT name
    FROM system.columns
    WHERE database = 'trading'
        AND table = 'features_1m_with_labels'
        AND name NOT LIKE 'label_%'
    ORDER BY name
    """

    existing_cols = [row[0] for row in cli.query(existing_cols_query).result_rows]
    existing_cols_str = ", ".join(existing_cols)

    # Insert query
    insert_query = f"""
    INSERT INTO trading.features_1m_with_labels
    SELECT
        sub.*,
        {", ".join(label_calcs)}
    FROM (
        SELECT
            {existing_cols_str},
            {", ".join(lead_cols)}
        FROM trading.features_1m_with_labels
        WHERE symbol = %(symbol)s
            AND ts >= toDateTime(%(start)s)
            AND ts <= toDateTime(%(end)s)
    ) sub
    """

    try:
        # Delete existing data first
        delete_query = f"""
        ALTER TABLE trading.features_1m_with_labels
        DELETE WHERE symbol = %(symbol)s
            AND ts >= toDateTime(%(start)s)
            AND ts <= toDateTime(%(end)s)
        """
        cli.command(delete_query, parameters={"symbol": symbol, "start": start, "end": end})

        # Insert new labels
        cli.command(insert_query, parameters={"symbol": symbol, "start": start, "end": end})

        # Count rows
        result = {}
        for h in horizons:
            count_query = f"""
            SELECT countIf(label_h{h} IS NOT NULL) as cnt
            FROM trading.features_1m_with_labels
            WHERE symbol = %(symbol)s
                AND ts >= toDateTime(%(start)s)
                AND ts <= toDateTime(%(end)s)
            """
            cnt = cli.query(count_query, parameters={"symbol": symbol, "start": start, "end": end}).result_rows[0][0]
            result[f"label_h{h}"] = cnt
            print(f"  label_h{h}: {cnt} rows")

        return result

    except Exception as e:
        print(f"  [ERROR] Failed to generate labels: {e}")
        return {}


def generate_labels_threshold(
    cli,
    symbol: str,
    start: str,
    end: str,
    horizons: List[int],
    threshold_bps: float = 30.0
) -> Dict[str, int]:
    """
    Generate threshold-based labels (only label significant moves).

    Label schema:
    - 1 if forward_return > threshold
    - 0 if forward_return < -threshold
    - NULL otherwise (filter out)

    Returns: {label_h30_thr30: row_count, ...}
    """

    print(f"[generate] Threshold labels for {symbol}, threshold={threshold_bps}bps...")

    threshold = threshold_bps / 10000.0

    lead_cols = []
    for h in horizons:
        lead_cols.append(
            f"leadInFrame(close, {h}) OVER (PARTITION BY symbol ORDER BY ts "
            f"ROWS BETWEEN CURRENT ROW AND {h} FOLLOWING) AS lead_close_{h}"
        )

    label_calcs = []
    for h in horizons:
        label_calc = f"""
        CASE
            WHEN close = 0 THEN NULL
            WHEN close IS NULL THEN NULL
            WHEN lead_close_{h} = 0 THEN NULL
            WHEN lead_close_{h} IS NULL THEN NULL
            WHEN (lead_close_{h} - close) / close > {threshold} THEN 1.0
            WHEN (lead_close_{h} - close) / close < -{threshold} THEN 0.0
            ELSE NULL
        END AS label_h{h}_thr{int(threshold_bps)}
        """
        label_calcs.append(label_calc.strip())

    # Build INSERT query (similar to simple, but with threshold columns)
    # For simplicity, we'll add these as new columns

    result = {}
    for h in horizons:
        label_name = f"label_h{h}_thr{int(threshold_bps)}"

        # Check if column exists
        check_query = f"""
        SELECT count(*) FROM system.columns
        WHERE database = 'trading'
            AND table = 'features_1m_with_labels'
            AND name = '{label_name}'
        """

        if cli.query(check_query).result_rows[0][0] == 0:
            # Add column
            alter_query = f"""
            ALTER TABLE trading.features_1m_with_labels
            ADD COLUMN IF NOT EXISTS {label_name} Nullable(Float64)
            """
            cli.command(alter_query)
            print(f"  Added column {label_name}")

        # Update column
        update_query = f"""
        ALTER TABLE trading.features_1m_with_labels
        UPDATE {label_name} = (
            CASE
                WHEN close = 0 THEN NULL
                WHEN close IS NULL THEN NULL
                WHEN lead_close_{h} = 0 THEN NULL
                WHEN lead_close_{h} IS NULL THEN NULL
                WHEN (lead_close_{h} - close) / close > {threshold} THEN 1.0
                WHEN (lead_close_{h} - close) / close < -{threshold} THEN 0.0
                ELSE NULL
            END
        )
        WHERE symbol = %(symbol)s
            AND ts >= toDateTime(%(start)s)
            AND ts <= toDateTime(%(end)s)
        """

        # This won't work directly because we need leadInFrame
        # Workaround: create temp table or use subquery

        print(f"  [SKIP] {label_name}: Needs leadInFrame support (TODO)")
        result[label_name] = 0

    return result


def test_label_quality(
    cli,
    symbol: str,
    start: str,
    end: str,
    label_col: str
) -> Dict[str, Any]:
    """
    Test label quality by training a simple model.

    Returns metrics: {auc, ic, zero_pct, etc.}
    """

    print(f"[test] Testing {label_col} quality...")

    # Load data
    query = f"""
    SELECT
        ret_5, spy_ret_5, atr_20, zscore_volume_20, {label_col}
    FROM trading.features_1m_with_labels
    WHERE symbol = %(symbol)s
        AND ts >= toDateTime(%(start)s)
        AND ts <= toDateTime(%(end)s)
        AND toHour(ts) >= 9
        AND toHour(ts) < 16
        AND {label_col} IS NOT NULL
    ORDER BY ts
    """

    df = cli.query_df(query, parameters={"symbol": symbol, "start": start, "end": end})

    if len(df) < 1000:
        return {"error": "Not enough data"}

    # Drop NaN
    df_clean = df.dropna()

    if len(df_clean) < 500:
        return {"error": "Not enough clean data"}

    # Simple train/test
    from sklearn.metrics import roc_auc_score
    from scipy.stats import spearmanr
    import xgboost as xgb

    features = ['ret_5', 'spy_ret_5', 'atr_20', 'zscore_volume_20']
    X = df_clean[features].values
    y_continuous = df_clean[label_col].values
    y = (y_continuous > 0).astype(int)

    split_idx = int(len(df_clean) * 0.7)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    y_continuous_test = y_continuous[split_idx:]

    model = xgb.XGBClassifier(n_estimators=100, max_depth=3, random_state=42)
    model.fit(X_train, y_train, verbose=False)

    y_pred_proba = model.predict_proba(X_test)[:, 1]

    auc = roc_auc_score(y_test, y_pred_proba)
    ic, ic_pval = spearmanr(y_pred_proba, y_continuous_test)

    # Check zero rate
    zero_threshold = 0.001  # 0.1%
    zero_count = np.sum(np.abs(y_continuous) < zero_threshold)
    zero_pct = zero_count / len(y_continuous)

    return {
        "n_train": len(X_train),
        "n_test": len(X_test),
        "auc": float(auc),
        "ic": float(ic),
        "ic_pval": float(ic_pval),
        "zero_pct": float(zero_pct)
    }


def main():
    parser = argparse.ArgumentParser(description="Generate improved labels")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--horizons", nargs="+", type=int, default=[30, 60, 120])
    parser.add_argument("--strategies", nargs="+", default=["simple"], choices=["simple", "threshold", "multiclass"])
    parser.add_argument("--threshold-bps", type=float, default=30.0)
    parser.add_argument("--output", help="Output JSON file")

    args = parser.parse_args()

    cli = clickhouse_connect.get_client(host='localhost', port=8123, database='trading')

    report = {
        "timestamp": datetime.now().isoformat(),
        "symbols": args.symbols,
        "horizons": args.horizons,
        "strategies": args.strategies,
        "results": {}
    }

    for symbol in args.symbols:
        symbol_results = {}

        if "simple" in args.strategies:
            simple_result = generate_labels_simple(
                cli, symbol, args.start, args.end, args.horizons
            )
            symbol_results["simple"] = simple_result

        if "threshold" in args.strategies:
            threshold_result = generate_labels_threshold(
                cli, symbol, args.start, args.end, args.horizons, args.threshold_bps
            )
            symbol_results["threshold"] = threshold_result

        report["results"][symbol] = symbol_results

    # Write output
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"\n[output] Report written to {args.output}")

    print("\n" + "=" * 80)
    print("LABEL GENERATION COMPLETE")
    print("=" * 80)
    for symbol, results in report["results"].items():
        print(f"\n{symbol}:")
        for strategy, data in results.items():
            print(f"  {strategy}:")
            for label, count in data.items():
                print(f"    {label}: {count} rows")

    return 0


if __name__ == "__main__":
    sys.exit(main())
