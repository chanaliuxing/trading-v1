#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ML Training with Proper Institutional Objective: IC/IR

Following professional quant fund framework:
- Model Layer: Optimize IC (Information Coefficient), NOT AUC
- Strategy Layer: Evaluated separately with portfolio optimizer
- Portfolio Layer: Risk controls applied post-hoc

Key Insights from Institutional Framework:
1. IC measures rank correlation between predictions and forward returns
2. IR = mean(IC) / std(IC) - measures consistency
3. AUC is a ranking metric but doesn't measure magnitude
4. Sharpe Ratio is ex-post result, NOT a training objective

This script implements proper model-layer optimization.

Author: Claude (based on institutional quant fund framework)
Date: 2025-11-06
Reference: User's correction on metrics hierarchy
"""

from __future__ import annotations
import sys
import argparse
import json
from datetime import datetime
from typing import Dict, Any, Tuple
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit
from scipy.stats import spearmanr
import optuna
import clickhouse_connect as ch
import structlog

logger = structlog.get_logger(__name__)


def fetch_training_data(
    client: ch.Client,
    symbol: str,
    start: str,
    end: str,
    features: list[str],
    label_col: str = "label_h5",
    horizon: int = 5
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fetch training data with features and forward returns.

    Returns:
        X: Features DataFrame
        y: Forward returns (NOT binary labels!)
    """

    features_str = ", ".join([f"f.{feat}" for feat in features])

    query = f"""
    SELECT
        f.ts,
        f.symbol,
        {features_str},
        -- Forward return (continuous target for IC optimization)
        (lead(f.close, {horizon}) OVER (PARTITION BY f.symbol ORDER BY f.ts) - f.close) / f.close AS fwd_return,
        -- Binary label (for AUC comparison)
        if(fwd_return > 0, 1, 0) AS {label_col}
    FROM trading.features_1m FINAL AS f
    WHERE f.symbol = '{symbol}'
      AND f.ts >= toDateTime('{start}')
      AND f.ts < toDateTime('{end}')
      AND f.close IS NOT NULL
      AND toHour(f.ts) >= 9 AND toHour(f.ts) < 16
      AND {" AND ".join([f"f.{feat} IS NOT NULL" for feat in features])}
    ORDER BY f.ts
    """

    df = client.query_df(query)
    logger.info("Fetched training data", rows=len(df), features=len(features))

    if len(df) == 0:
        raise ValueError(f"No training data found for {symbol}")

    # Features
    X = df[features].copy()

    # Target: Use continuous forward returns for IC optimization
    y_continuous = df['fwd_return'].copy()
    y_binary = df[label_col].copy()

    # Add metadata columns
    X['ts'] = df['ts']
    X['symbol'] = df['symbol']

    return X, y_continuous, y_binary


def calculate_ic(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """
    Calculate Information Coefficient (IC).

    IC = Spearman rank correlation between predictions and forward returns.

    This is the PROPER model-layer metric used by institutional quant funds.
    """
    # Handle edge cases
    if len(y_true) < 5:  # Need at least 5 samples
        return 0.0

    # Remove NaNs
    mask = ~(np.isnan(y_true) | np.isnan(y_pred))
    y_true_clean = y_true[mask]
    y_pred_clean = y_pred[mask]

    if len(y_true_clean) < 5:
        return 0.0

    # Check for constant arrays
    if np.std(y_true_clean) < 1e-9 or np.std(y_pred_clean) < 1e-9:
        return 0.0

    ic, _ = spearmanr(y_pred_clean, y_true_clean)

    # Handle NaN result from spearmanr
    if np.isnan(ic):
        return 0.0

    return float(ic)


def calculate_rank_ic(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """
    Calculate Rank IC with quantile-based ranking.

    More robust to outliers than raw IC.
    """
    # Rank predictions and returns into quintiles
    pred_rank = pd.qcut(y_pred, q=5, labels=False, duplicates='drop')
    true_rank = pd.qcut(y_true, q=5, labels=False, duplicates='drop')

    return calculate_ic(true_rank, pred_rank)


def objective_ic(trial: optuna.Trial, X: pd.DataFrame, y_continuous: pd.Series, y_binary: pd.Series, n_splits: int = 3):
    """
    Optuna objective function optimizing IC (NOT AUC).

    This is the proper institutional approach:
    - Model Layer: Optimize IC/IR
    - Strategy Layer: Evaluated separately with backtest
    """

    # Hyperparameters
    params = {
        "eta": trial.suggest_float("eta", 0.01, 0.3, log=True),
        "max_depth": trial.suggest_int("max_depth", 3, 10),
        "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "min_child_weight": trial.suggest_float("min_child_weight", 0.5, 10.0),
        "gamma": trial.suggest_float("gamma", 0.0, 5.0),
        "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 10.0),
        "reg_lambda": trial.suggest_float("reg_lambda", 0.0, 10.0),
        "objective": "reg:squarederror",  # Regression for continuous returns
        "tree_method": "hist",
        "random_state": 42,
    }

    # Time series cross-validation
    tscv = TimeSeriesSplit(n_splits=n_splits)

    # Features (exclude metadata)
    feature_cols = [col for col in X.columns if col not in ['ts', 'symbol']]
    X_features = X[feature_cols].values

    ic_scores = []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X_features)):
        X_train, X_val = X_features[train_idx], X_features[val_idx]
        y_train, y_val = y_continuous.iloc[train_idx], y_continuous.iloc[val_idx]

        # Train model
        dtrain = xgb.DMatrix(X_train, label=y_train)
        dval = xgb.DMatrix(X_val, label=y_val)

        model = xgb.train(
            params,
            dtrain,
            num_boost_round=100,
            evals=[(dval, "val")],
            early_stopping_rounds=10,
            verbose_eval=False,
        )

        # Predict
        y_pred = model.predict(dval)

        # Calculate IC (NOT AUC!)
        ic = calculate_ic(y_val.values, y_pred)
        ic_scores.append(ic)

        logger.info(
            "Fold completed",
            fold=fold + 1,
            ic=ic,
            train_samples=len(train_idx),
            val_samples=len(val_idx)
        )

    # IR = mean(IC) / std(IC)
    mean_ic = np.mean(ic_scores)
    std_ic = np.std(ic_scores)
    ir = mean_ic / std_ic if std_ic > 0 else 0.0

    logger.info(
        "Trial completed",
        trial=trial.number,
        mean_ic=mean_ic,
        std_ic=std_ic,
        ir=ir,
        ic_scores=ic_scores
    )

    # Optimize IR (Information Ratio)
    # Alternative: optimize mean_ic if you want higher IC at cost of consistency
    return ir


def train_best_model(
    X: pd.DataFrame,
    y_continuous: pd.Series,
    y_binary: pd.Series,
    best_params: Dict[str, Any],
    n_splits: int = 3
) -> Tuple[xgb.Booster, pd.DataFrame]:
    """
    Train final model with best hyperparameters and generate OOF predictions.
    """

    # Features (exclude metadata)
    feature_cols = [col for col in X.columns if col not in ['ts', 'symbol']]
    X_features = X[feature_cols].values

    # Time series cross-validation
    tscv = TimeSeriesSplit(n_splits=n_splits)

    # OOF predictions
    oof_preds = np.zeros(len(X))
    oof_ic = []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X_features)):
        X_train, X_val = X_features[train_idx], X_features[val_idx]
        y_train, y_val = y_continuous.iloc[train_idx], y_continuous.iloc[val_idx]

        # Train model
        dtrain = xgb.DMatrix(X_train, label=y_train)
        dval = xgb.DMatrix(X_val, label=y_val)

        model = xgb.train(
            best_params,
            dtrain,
            num_boost_round=150,
            evals=[(dval, "val")],
            early_stopping_rounds=15,
            verbose_eval=False,
        )

        # Predict
        y_pred = model.predict(dval)
        oof_preds[val_idx] = y_pred

        # Calculate IC
        ic = calculate_ic(y_val.values, y_pred)
        oof_ic.append(ic)

        logger.info(
            "Fold OOF completed",
            fold=fold + 1,
            ic=ic,
            val_samples=len(val_idx)
        )

    # Calculate overall IC/IR
    mask = oof_preds != 0  # Only consider samples that were in validation set
    overall_ic = calculate_ic(y_continuous.values[mask], oof_preds[mask])
    mean_ic = np.mean(oof_ic)
    std_ic = np.std(oof_ic)
    ir = mean_ic / std_ic if std_ic > 0 else 0.0

    logger.info(
        "OOF Training completed",
        overall_ic=overall_ic,
        mean_ic=mean_ic,
        std_ic=std_ic,
        ir=ir
    )

    # Create OOF DataFrame
    oof_df = pd.DataFrame({
        'ts': X['ts'],
        'symbol': X['symbol'],
        'pred': oof_preds,
        'conf': np.abs(oof_preds),  # Confidence = magnitude of prediction
        'fwd_return': y_continuous.values,
        'label': y_binary.values,
    })

    # Only keep validation samples
    oof_df = oof_df[mask].copy()

    # Train final model on all data
    dtrain_all = xgb.DMatrix(X_features, label=y_continuous.values)
    final_model = xgb.train(
        best_params,
        dtrain_all,
        num_boost_round=150,
        verbose_eval=False,
    )

    return final_model, oof_df


def main():
    parser = argparse.ArgumentParser(description="Train XGBoost with IC/IR objective (institutional approach)")
    parser.add_argument("--symbol", default="TSLA", help="Symbol to train")
    parser.add_argument("--start", default="2025-04-18 00:00:00", help="Start date")
    parser.add_argument("--end", default="2025-11-05 00:00:00", help="End date")
    parser.add_argument("--features", nargs="+", required=True, help="Features to use")
    parser.add_argument("--label-col", default="label_h5", help="Label column name")
    parser.add_argument("--horizon", type=int, default=5, help="Forward return horizon")
    parser.add_argument("--n-trials", type=int, default=50, help="Number of Optuna trials")
    parser.add_argument("--n-splits", type=int, default=3, help="Number of CV folds")
    parser.add_argument("--host", default="localhost", help="ClickHouse host")

    args = parser.parse_args()

    # Connect to ClickHouse
    client = ch.get_client(host=args.host)

    logger.info("Starting IC-based training", symbol=args.symbol, features=args.features)

    # Fetch data
    X, y_continuous, y_binary = fetch_training_data(
        client, args.symbol, args.start, args.end, args.features, args.label_col, args.horizon
    )

    logger.info("Data loaded", samples=len(X), features=len(args.features))

    # Optuna optimization
    logger.info("Starting Optuna optimization (optimizing IR, NOT AUC)")

    study = optuna.create_study(direction="maximize", study_name=f"ic_optim_{args.symbol}")
    study.optimize(
        lambda trial: objective_ic(trial, X, y_continuous, y_binary, args.n_splits),
        n_trials=args.n_trials,
        show_progress_bar=True,
    )

    best_params = study.best_params
    best_params.update({
        "objective": "reg:squarederror",
        "tree_method": "hist",
        "random_state": 42,
    })

    logger.info("Optimization completed", best_ir=study.best_value, best_params=best_params)

    # Train final model
    logger.info("Training final model with best hyperparameters")

    final_model, oof_df = train_best_model(X, y_continuous, y_binary, best_params, args.n_splits)

    # Save model
    model_path = f"/tmp/xgb_ic_{args.symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    final_model.save_model(model_path)
    logger.info("Model saved", path=model_path)

    # Save OOF predictions
    oof_path = f"/tmp/oof_ic_{args.symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
    oof_df.to_parquet(oof_path, index=False)
    logger.info("OOF predictions saved", path=oof_path, rows=len(oof_df))

    # Generate report
    report = {
        'symbol': args.symbol,
        'period': {'start': args.start, 'end': args.end},
        'optimization': {
            'objective': 'IC/IR (Information Ratio)',
            'best_ir': float(study.best_value),
            'n_trials': args.n_trials,
        },
        'best_params': best_params,
        'model_path': model_path,
        'oof_path': oof_path,
        'next_steps': [
            "1. Evaluate OOF predictions with evaluate_ml_backtest.py to get Sharpe Ratio",
            "2. If IC > 0.3 but Sharpe < 1.0, optimize strategy layer (risk management)",
            "3. If IC < 0.3, need better features or different prediction task",
        ]
    }

    print("\n" + "="*80)
    print("TRAINING REPORT (IC-BASED OPTIMIZATION)")
    print("="*80)
    print(json.dumps(report, indent=2))
    print("="*80 + "\n")

    # Save report
    report_path = f"/tmp/train_report_ic_{args.symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)

    logger.info("Training completed", report_path=report_path)

    return report


if __name__ == "__main__":
    main()
