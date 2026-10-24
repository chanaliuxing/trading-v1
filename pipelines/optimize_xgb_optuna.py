"""Optuna hyperparameter optimization for XGBoost model."""
from __future__ import annotations

import argparse
import json
from typing import Tuple

import numpy as np
import optuna
import pandas as pd
import xgboost as xgb
from clickhouse_driver import Client
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit


def fetch_data(
    symbol: str,
    start: str,
    end: str,
    label_col: str,
    features: list[str],
    host: str = "localhost",
    port: int = 9000,
) -> Tuple[np.ndarray, np.ndarray]:
    """Fetch training data from ClickHouse."""
    client = Client(host=host, port=port, database="trading", settings={"use_numpy": True})

    cols = ["ts", "symbol", label_col, *features]
    query = f"""
        SELECT {', '.join(cols)}
        FROM features_1m_with_labels
        WHERE symbol = %(sym)s
          AND ts >= %(st)s AND ts < %(ed)s
        ORDER BY ts
    """

    data = client.execute(query, {"sym": symbol, "st": start, "ed": end})
    df = pd.DataFrame(data, columns=cols)
    df = df.dropna().reset_index(drop=True)

    X = df[features].to_numpy(dtype=np.float32)
    y = (df[label_col].values > 0).astype(np.int32)

    print(f"Loaded {len(df):,} samples for {symbol} ({start} to {end})")
    print(f"Label distribution: {y.sum():,} positive ({y.mean()*100:.1f}%)")

    return X, y


def objective(
    trial: optuna.Trial,
    X: np.ndarray,
    y: np.ndarray,
    n_splits: int = 3,
) -> float:
    """Optuna objective function for XGBoost hyperparameters."""

    # Hyperparameter search space
    params = {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "eta": trial.suggest_float("eta", 0.01, 0.3, log=True),
        "max_depth": trial.suggest_int("max_depth", 3, 10),
        "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "min_child_weight": trial.suggest_float("min_child_weight", 0.5, 10.0),
        "gamma": trial.suggest_float("gamma", 0.0, 5.0),
        "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 10.0),
        "reg_lambda": trial.suggest_float("reg_lambda", 0.0, 10.0),
    }

    num_round = trial.suggest_int("num_round", 50, 500, step=50)

    # Time series cross-validation
    tscv = TimeSeriesSplit(n_splits=n_splits)
    aucs = []

    for fold, (tr, va) in enumerate(tscv.split(X)):
        dtr = xgb.DMatrix(X[tr], label=y[tr])
        dva = xgb.DMatrix(X[va], label=y[va])

        bst = xgb.train(
            params,
            dtr,
            num_round,
            evals=[(dva, "valid")],
            verbose_eval=False,
        )

        proba = bst.predict(dva)
        auc = roc_auc_score(y[va], proba)
        aucs.append(auc)

    mean_auc = float(np.mean(aucs))
    std_auc = float(np.std(aucs))

    # Report fold results
    trial.set_user_attr("fold_aucs", aucs)
    trial.set_user_attr("auc_std", std_auc)

    return mean_auc


def main():
    parser = argparse.ArgumentParser(description="Optimize XGBoost with Optuna")
    parser.add_argument("--symbol", default="TSLA")
    parser.add_argument("--start", default="2025-04-18 00:00:00")
    parser.add_argument("--end", default="2025-11-05 00:00:00")
    parser.add_argument("--label-col", default="label_h30")
    parser.add_argument("--features", nargs="+", default=["vwap", "rsi_14", "ema_9", "atr_14", "close"])
    parser.add_argument("--n-trials", type=int, default=50)
    parser.add_argument("--n-splits", type=int, default=3)
    parser.add_argument("--study-name", default="xgb_optimization")

    args = parser.parse_args()

    # Load data
    print(f"Fetching data for {args.symbol}...")
    X, y = fetch_data(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        label_col=args.label_col,
        features=args.features,
    )

    # Create Optuna study
    study = optuna.create_study(
        direction="maximize",
        study_name=args.study_name,
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    print(f"\nStarting Optuna optimization ({args.n_trials} trials)...")
    print(f"Baseline AUC to beat: 0.574\n")

    # Optimize
    study.optimize(
        lambda trial: objective(trial, X, y, n_splits=args.n_splits),
        n_trials=args.n_trials,
        show_progress_bar=True,
    )

    # Results
    print("\n" + "="*60)
    print("OPTIMIZATION COMPLETE")
    print("="*60)

    best_trial = study.best_trial
    print(f"\nBest AUC: {best_trial.value:.4f}")
    print(f"AUC Std: {best_trial.user_attrs['auc_std']:.4f}")
    print(f"Fold AUCs: {[f'{x:.4f}' for x in best_trial.user_attrs['fold_aucs']]}")

    print(f"\nBest hyperparameters:")
    for key, value in best_trial.params.items():
        if isinstance(value, float):
            print(f"  {key:20} {value:.4f}")
        else:
            print(f"  {key:20} {value}")

    # Save results
    output_file = f"/tmp/optuna_best_params_{args.symbol}_{args.label_col}.json"
    with open(output_file, "w") as f:
        json.dump({
            "symbol": args.symbol,
            "label_col": args.label_col,
            "features": args.features,
            "best_auc": best_trial.value,
            "auc_std": best_trial.user_attrs["auc_std"],
            "fold_aucs": best_trial.user_attrs["fold_aucs"],
            "best_params": best_trial.params,
            "n_trials": args.n_trials,
            "n_splits": args.n_splits,
        }, f, indent=2)

    print(f"\nResults saved to: {output_file}")

    # Show improvement
    baseline_auc = 0.574
    improvement = (best_trial.value - baseline_auc) / baseline_auc * 100
    print(f"\nImprovement over baseline: {improvement:+.1f}%")

    if best_trial.value >= 0.68:
        print("✓ ACHIEVED TRADING VIABILITY (AUC >= 0.68)!")
    else:
        gap = 0.68 - best_trial.value
        print(f"✗ Still {gap:.3f} below trading threshold (0.68)")


if __name__ == "__main__":
    main()
