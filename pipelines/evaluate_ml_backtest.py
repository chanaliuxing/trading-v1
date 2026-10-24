#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Evaluate ML model with end-to-end backtest to measure business metrics.

This addresses the critical metrics mismatch: we've been optimizing AUC (technical)
without measuring Sharpe Ratio (business). This script runs backtest to evaluate
the actual trading performance of our ML predictions.

Following institutional best practices:
- Model Layer: Measure IC, IR, Precision@K (NOT just AUC)
- Strategy Layer: Measure Sharpe Ratio, Max Drawdown, Win Rate
- Portfolio Layer: Risk metrics (VaR, CVaR)

Author: Claude (based on institutional quant fund framework)
Date: 2025-11-06
"""

from __future__ import annotations
import sys
import json
import argparse
from datetime import datetime
from typing import Dict, Any, List
import pandas as pd
import numpy as np
from scipy.stats import spearmanr
import clickhouse_connect as ch
import structlog

logger = structlog.get_logger(__name__)


def calculate_ic_ir(predictions: pd.DataFrame, returns: pd.DataFrame) -> Dict[str, float]:
    """
    Calculate Information Coefficient (IC) and Information Ratio (IR).

    IC = correlation between predictions and forward returns
    IR = mean(IC) / std(IC) over time

    These are proper model-layer metrics used by institutional quant funds.
    """
    # Merge predictions with forward returns
    merged = predictions.merge(returns, on=['ts', 'symbol'], how='inner')

    # Calculate IC per time period (daily)
    merged['date'] = pd.to_datetime(merged['ts']).dt.date

    ic_by_date = []
    for date, group in merged.groupby('date'):
        if len(group) < 5:  # Need at least 5 samples for correlation
            continue

        # IC = spearman correlation between prediction and realized return
        ic, pval = spearmanr(group['pred'], group['fwd_return'])
        ic_by_date.append({
            'date': date,
            'ic': ic,
            'pval': pval,
            'n_samples': len(group)
        })

    if not ic_by_date:
        return {'ic_mean': 0.0, 'ic_std': 0.0, 'ir': 0.0, 'ic_hit_rate': 0.0}

    ic_df = pd.DataFrame(ic_by_date)

    # IR = mean(IC) / std(IC)
    ic_mean = ic_df['ic'].mean()
    ic_std = ic_df['ic'].std()
    ir = ic_mean / ic_std if ic_std > 0 else 0.0

    # Hit rate: % of days with positive IC
    ic_hit_rate = (ic_df['ic'] > 0).mean()

    return {
        'ic_mean': float(ic_mean),
        'ic_std': float(ic_std),
        'ir': float(ir),
        'ic_hit_rate': float(ic_hit_rate),
        'ic_pct_5': float(ic_df['ic'].quantile(0.05)),
        'ic_pct_95': float(ic_df['ic'].quantile(0.95)),
    }


def calculate_precision_at_k(predictions: pd.DataFrame, returns: pd.DataFrame, k: int = 10) -> Dict[str, float]:
    """
    Calculate Precision@K: accuracy of top-K predictions.

    Used by institutional funds to measure quality of high-confidence signals.
    """
    merged = predictions.merge(returns, on=['ts', 'symbol'], how='inner')

    # Sort by confidence descending
    merged = merged.sort_values('conf', ascending=False)

    # Top-K predictions
    top_k = merged.head(k)

    # Precision = % of top-K that were correct
    correct = (top_k['pred'] > 0.5) == (top_k['fwd_return'] > 0)
    precision_k = correct.mean()

    # Average return of top-K
    avg_return_k = top_k['fwd_return'].mean()

    return {
        f'precision_at_{k}': float(precision_k),
        f'avg_return_at_{k}': float(avg_return_k),
    }


def fetch_oof_predictions(client: ch.Client, symbol: str, start: str, end: str, table: str = "trading.oof_xgb_h5") -> pd.DataFrame:
    """Fetch out-of-fold predictions from ClickHouse."""

    query = f"""
    SELECT
        ts,
        symbol,
        pred,
        conf,
        run_id,
        model_ver
    FROM {table}
    WHERE symbol = '{symbol}'
      AND ts >= toDateTime('{start}')
      AND ts < toDateTime('{end}')
      AND pred IS NOT NULL
      AND conf IS NOT NULL
    ORDER BY ts
    """

    df = client.query_df(query)
    logger.info("Fetched OOF predictions", rows=len(df), table=table, symbol=symbol)
    return df


def fetch_forward_returns(client: ch.Client, symbol: str, start: str, end: str, horizon: int = 5) -> pd.DataFrame:
    """
    Fetch forward returns for calculating IC/IR.

    Forward return = (price[t+h] - price[t]) / price[t]
    """

    query = f"""
    SELECT
        ts,
        symbol,
        close,
        lead(close, {horizon}) OVER (PARTITION BY symbol ORDER BY ts) AS close_fwd
    FROM trading.features_1m
    WHERE symbol = '{symbol}'
      AND ts >= toDateTime('{start}')
      AND ts < toDateTime('{end}')
      AND close IS NOT NULL
    ORDER BY ts
    """

    df = client.query_df(query)

    # Calculate forward return
    df['fwd_return'] = (df['close_fwd'] - df['close']) / df['close']
    df = df.dropna(subset=['fwd_return'])

    logger.info("Fetched forward returns", rows=len(df), symbol=symbol, horizon=horizon)
    return df[['ts', 'symbol', 'fwd_return']]


def fetch_bars_for_backtest(client: ch.Client, symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch bars with features for backtest."""

    query = f"""
    SELECT
        f.ts,
        f.symbol,
        f.open,
        f.high,
        f.low,
        f.close,
        f.volume,
        -- Features
        f.ret_1,
        f.ret_5,
        f.atr_14,
        f.rsi_14,
        f.vwap,
        f.ema_9,
        f.ema_20,
        f.sma_20,
        f.bb_mid_20,
        f.bb_up_20,
        f.bb_dn_20,
        -- OOF predictions
        o.pred AS ml_pred,
        o.conf AS ml_conf
    FROM trading.features_1m FINAL f
    LEFT JOIN (
        SELECT ts, symbol, pred, conf
        FROM (
            SELECT ts, symbol, pred, conf,
                   row_number() OVER (PARTITION BY symbol, ts ORDER BY updated_at DESC) AS rn
            FROM trading.oof_xgb_h5
        )
        WHERE rn = 1
    ) o ON o.symbol = f.symbol AND o.ts = f.ts
    WHERE f.symbol = '{symbol}'
      AND f.ts >= toDateTime('{start}')
      AND f.ts < toDateTime('{end}')
      AND f.close IS NOT NULL
      AND toHour(f.ts) >= 9 AND toHour(f.ts) < 16
    ORDER BY f.ts
    """

    df = client.query_df(query)
    logger.info("Fetched bars for backtest", rows=len(df), symbol=symbol)
    return df


def run_simple_backtest(bars: pd.DataFrame, conf_threshold: float = 0.6) -> Dict[str, Any]:
    """
    Simple vectorized backtest using ML predictions.

    Strategy:
    - Buy when ml_pred > 0.5 AND ml_conf > conf_threshold
    - Hold for 5 minutes
    - Commission: 0.005/share + $1 min
    """

    # Filter to high-confidence signals
    bars = bars.copy()
    bars['signal'] = ((bars['ml_pred'] > 0.5) & (bars['ml_conf'] > conf_threshold)).astype(int)

    # Calculate returns
    bars['ret'] = bars['close'].pct_change()

    # Strategy returns: only when signal = 1
    bars['strat_ret'] = bars['signal'].shift(1) * bars['ret']

    # Commission: assume 100 shares per trade
    bars['trade'] = bars['signal'].diff().abs()
    bars['commission'] = bars['trade'] * (0.005 * 100 + 1) / (100 * bars['close'])

    # Net returns
    bars['net_ret'] = bars['strat_ret'] - bars['commission']

    # Calculate metrics
    cumulative_ret = (1 + bars['net_ret']).cumprod()
    total_return = cumulative_ret.iloc[-1] - 1

    # Sharpe Ratio (annualized)
    mean_ret = bars['net_ret'].mean()
    std_ret = bars['net_ret'].std()
    sharpe = (mean_ret / std_ret) * np.sqrt(252 * 390) if std_ret > 0 else 0  # 252 trading days * 390 minutes

    # Max Drawdown
    cummax = cumulative_ret.cummax()
    drawdown = (cumulative_ret - cummax) / cummax
    max_drawdown = drawdown.min()

    # Win Rate
    trades = bars[bars['trade'] > 0]
    winning_trades = (bars['strat_ret'] > 0).sum()
    total_trades = (bars['signal'] > 0).sum()
    win_rate = winning_trades / total_trades if total_trades > 0 else 0

    # Number of signals
    n_signals = (bars['signal'] > 0).sum()

    return {
        'total_return': float(total_return),
        'sharpe_ratio': float(sharpe),
        'max_drawdown': float(max_drawdown),
        'win_rate': float(win_rate),
        'n_signals': int(n_signals),
        'total_trades': int(total_trades),
        'avg_return_per_signal': float(bars[bars['signal'] > 0]['strat_ret'].mean()) if n_signals > 0 else 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate ML model with end-to-end backtest")
    parser.add_argument("--symbol", default="TSLA", help="Symbol to evaluate")
    parser.add_argument("--start", default="2025-10-01 00:00:00", help="Start date")
    parser.add_argument("--end", default="2025-11-05 23:59:59", help="End date")
    parser.add_argument("--conf-threshold", type=float, default=0.6, help="Confidence threshold for signals")
    parser.add_argument("--oof-table", default="trading.oof_xgb_h5", help="OOF predictions table")
    parser.add_argument("--host", default="localhost", help="ClickHouse host")

    args = parser.parse_args()

    # Connect to ClickHouse
    client = ch.get_client(host=args.host)

    logger.info("Starting ML model evaluation", symbol=args.symbol, start=args.start, end=args.end)

    # Step 1: Model Layer Metrics (IC, IR, Precision@K)
    logger.info("=== STEP 1: Model Layer Metrics ===")

    predictions = fetch_oof_predictions(client, args.symbol, args.start, args.end, args.oof_table)
    forward_returns = fetch_forward_returns(client, args.symbol, args.start, args.end, horizon=5)

    if len(predictions) == 0:
        logger.error("No OOF predictions found", symbol=args.symbol, table=args.oof_table)
        sys.exit(1)

    # Calculate IC/IR
    ic_ir_metrics = calculate_ic_ir(predictions, forward_returns)
    logger.info("IC/IR Metrics", **ic_ir_metrics)

    # Calculate Precision@K
    precision_metrics = calculate_precision_at_k(predictions, forward_returns, k=10)
    logger.info("Precision@K Metrics", **precision_metrics)

    # Step 2: Strategy Layer Metrics (Sharpe, Max DD)
    logger.info("=== STEP 2: Strategy Layer Metrics (Backtest) ===")

    bars = fetch_bars_for_backtest(client, args.symbol, args.start, args.end)

    if len(bars) == 0:
        logger.error("No bars found for backtest", symbol=args.symbol)
        sys.exit(1)

    # Run backtest
    backtest_metrics = run_simple_backtest(bars, conf_threshold=args.conf_threshold)
    logger.info("Backtest Metrics", **backtest_metrics)

    # Step 3: Combined Report
    logger.info("=== COMPREHENSIVE EVALUATION REPORT ===")

    report = {
        'symbol': args.symbol,
        'period': {'start': args.start, 'end': args.end},
        'model_layer': {
            'ic_mean': ic_ir_metrics['ic_mean'],
            'ic_std': ic_ir_metrics['ic_std'],
            'ir': ic_ir_metrics['ir'],
            'ic_hit_rate': ic_ir_metrics['ic_hit_rate'],
            'precision_at_10': precision_metrics['precision_at_10'],
        },
        'strategy_layer': {
            'sharpe_ratio': backtest_metrics['sharpe_ratio'],
            'max_drawdown': backtest_metrics['max_drawdown'],
            'total_return': backtest_metrics['total_return'],
            'win_rate': backtest_metrics['win_rate'],
            'n_signals': backtest_metrics['n_signals'],
        },
        'evaluation': {
            'model_layer_quality': 'GOOD' if ic_ir_metrics['ir'] > 0.5 else 'POOR',
            'strategy_layer_quality': 'GOOD' if backtest_metrics['sharpe_ratio'] > 1.0 else 'POOR',
            'recommendation': '',
        }
    }

    # Generate recommendation
    if ic_ir_metrics['ir'] > 0.5 and backtest_metrics['sharpe_ratio'] > 1.0:
        report['evaluation']['recommendation'] = "Model is ready for trading. Both model and strategy metrics are strong."
    elif ic_ir_metrics['ir'] > 0.5 and backtest_metrics['sharpe_ratio'] <= 1.0:
        report['evaluation']['recommendation'] = "Model has good IC/IR but poor Sharpe. Optimize strategy layer (risk management, position sizing)."
    elif ic_ir_metrics['ir'] <= 0.5 and backtest_metrics['sharpe_ratio'] > 1.0:
        report['evaluation']['recommendation'] = "Model has poor IC/IR but good Sharpe. This is suspicious - check for data leakage or overfitting."
    else:
        report['evaluation']['recommendation'] = "Model not ready for trading. Need to improve both model (IC/IR) and strategy (Sharpe)."

    print("\n" + "="*80)
    print("ML MODEL EVALUATION REPORT")
    print("="*80)
    print(json.dumps(report, indent=2))
    print("="*80 + "\n")

    # Save report
    output_file = f"/tmp/ml_eval_{args.symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w') as f:
        json.dump(report, f, indent=2)

    logger.info("Report saved", file=output_file)

    return report


if __name__ == "__main__":
    main()
