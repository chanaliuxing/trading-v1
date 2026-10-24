"""Train XGBoost with optimal hyperparameters and Platt scaling calibration."""
from __future__ import annotations

import argparse
import json
import pickle
from datetime import datetime
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd
import xgboost as xgb
from clickhouse_driver import Client
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit


def fetch_data(
    symbol: str,
    start: str,
    end: str,
    label_col: str,
    features: list[str],
    host: str = "localhost",
    port: int = 9000,
) -> Tuple[pd.DataFrame, np.ndarray, np.ndarray]:
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
    df["ts"] = pd.to_datetime(df["ts"], utc=True)

    X = df[features].to_numpy(dtype=np.float32)
    y = (df[label_col].values > 0).astype(np.int32)

    print(f"✓ Loaded {len(df):,} samples for {symbol}")
    print(f"  Date range: {df['ts'].min()} to {df['ts'].max()}")
    print(f"  Label distribution: {y.sum():,} positive ({y.mean()*100:.1f}%)")

    return df, X, y


def train_with_calibration(
    X: np.ndarray,
    y: np.ndarray,
    params: dict,
    num_round: int,
    n_splits: int = 3,
) -> Tuple[list, list, list, list]:
    """Train XGBoost with time series CV and Platt scaling calibration."""

    tscv = TimeSeriesSplit(n_splits=n_splits)
    models = []
    calibrated_models = []
    oof_preds = np.zeros(len(y), dtype=np.float32)
    oof_preds_calibrated = np.zeros(len(y), dtype=np.float32)

    fold_metrics = []

    for fold, (tr, va) in enumerate(tscv.split(X)):
        print(f"\n=== Fold {fold+1}/{n_splits} ===")
        print(f"  Train: {len(tr):,} samples, Val: {len(va):,} samples")

        # Split train into train/calibration (80/20)
        n_train = int(len(tr) * 0.8)
        tr_train = tr[:n_train]
        tr_calib = tr[n_train:]

        # Train XGBoost
        dtr = xgb.DMatrix(X[tr_train], label=y[tr_train])
        dva = xgb.DMatrix(X[va], label=y[va])

        bst = xgb.train(
            params,
            dtr,
            num_round,
            evals=[(dva, "valid")],
            verbose_eval=False,
        )

        # Raw predictions
        proba_raw = bst.predict(dva)
        auc_raw = roc_auc_score(y[va], proba_raw)
        logloss_raw = log_loss(y[va], proba_raw)
        brier_raw = brier_score_loss(y[va], proba_raw)

        # Calibration on held-out calibration set
        proba_calib_set = bst.predict(xgb.DMatrix(X[tr_calib], label=y[tr_calib]))

        # Fit Platt scaling (logistic regression on raw scores)
        from sklearn.linear_model import LogisticRegression

        calibrator = LogisticRegression()
        calibrator.fit(proba_calib_set.reshape(-1, 1), y[tr_calib])

        # Apply calibration to validation set
        proba_calibrated = calibrator.predict_proba(proba_raw.reshape(-1, 1))[:, 1]

        auc_cal = roc_auc_score(y[va], proba_calibrated)
        logloss_cal = log_loss(y[va], proba_calibrated)
        brier_cal = brier_score_loss(y[va], proba_calibrated)

        print(f"  Raw      - AUC: {auc_raw:.4f}, LogLoss: {logloss_raw:.4f}, Brier: {brier_raw:.4f}")
        print(f"  Calibrated - AUC: {auc_cal:.4f}, LogLoss: {logloss_cal:.4f}, Brier: {brier_cal:.4f}")

        # Store OOF predictions
        oof_preds[va] = proba_raw
        oof_preds_calibrated[va] = proba_calibrated

        models.append(bst)
        calibrated_models.append(calibrator)

        fold_metrics.append({
            "fold": fold + 1,
            "auc_raw": float(auc_raw),
            "auc_calibrated": float(auc_cal),
            "logloss_raw": float(logloss_raw),
            "logloss_calibrated": float(logloss_cal),
            "brier_raw": float(brier_raw),
            "brier_calibrated": float(brier_cal),
        })

    # Overall OOF metrics
    oof_auc_raw = roc_auc_score(y, oof_preds)
    oof_auc_cal = roc_auc_score(y, oof_preds_calibrated)
    oof_logloss_raw = log_loss(y, oof_preds)
    oof_logloss_cal = log_loss(y, oof_preds_calibrated)

    print(f"\n{'='*60}")
    print("OVERALL OOF METRICS")
    print(f"{'='*60}")
    print(f"Raw        - AUC: {oof_auc_raw:.4f}, LogLoss: {oof_logloss_raw:.4f}")
    print(f"Calibrated - AUC: {oof_auc_cal:.4f}, LogLoss: {oof_logloss_cal:.4f}")

    return models, calibrated_models, oof_preds, oof_preds_calibrated, fold_metrics


def save_models_and_oof(
    df: pd.DataFrame,
    models: list,
    calibrators: list,
    oof_raw: np.ndarray,
    oof_calibrated: np.ndarray,
    symbol: str,
    label_col: str,
    features: list[str],
    params: dict,
    fold_metrics: list,
    output_dir: str = "/tmp/ml_models",
):
    """Save trained models and OOF predictions."""
    output_path = Path(output_dir)
    output_path.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_dir = output_path / f"xgb_{symbol}_{label_col}_{timestamp}"
    model_dir.mkdir(exist_ok=True)

    # Save models
    for i, (bst, calibrator) in enumerate(zip(models, calibrators)):
        bst.save_model(str(model_dir / f"xgb_fold_{i}.json"))
        with open(model_dir / f"calibrator_fold_{i}.pkl", "wb") as f:
            pickle.dump(calibrator, f)

    # Save metadata
    metadata = {
        "symbol": symbol,
        "label_col": label_col,
        "features": features,
        "params": params,
        "n_folds": len(models),
        "fold_metrics": fold_metrics,
        "timestamp": timestamp,
    }

    with open(model_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    # Save OOF predictions
    oof_df = df.copy()
    oof_df["oof_raw"] = oof_raw
    oof_df["oof_calibrated"] = oof_calibrated
    oof_df["label"] = (df[label_col] > 0).astype(int)

    oof_file = model_dir / "oof_predictions.parquet"
    oof_df[["ts", "symbol", "oof_raw", "oof_calibrated", "label"]].to_parquet(oof_file)

    print(f"\n✓ Models saved to: {model_dir}")
    print(f"✓ OOF predictions saved to: {oof_file}")

    return model_dir


def upload_oof_to_clickhouse(
    df: pd.DataFrame,
    oof_preds: np.ndarray,
    label_col: str,
    symbol: str,
    model_ver: str = "v1",
    host: str = "localhost",
    port: int = 9000,
):
    """Upload OOF predictions to ClickHouse trading.oof_xgb_h5 table."""
    client = Client(host=host, port=port, database="trading")

    # Prepare data with correct column names
    from datetime import datetime
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    data_rows = []
    for i in range(len(df)):
        data_rows.append((
            df["ts"].iloc[i].to_pydatetime(),
            str(symbol),
            float(oof_preds[i]),  # pred
            float(oof_preds[i]),  # conf (use same as pred)
            timestamp_str,        # run_id
            model_ver,            # model_ver
        ))

    # Drop existing data for this symbol
    print(f"\n✓ Uploading {len(data_rows):,} OOF predictions to ClickHouse...")
    client.execute(f"DELETE FROM oof_xgb_h5 WHERE symbol = '{symbol}'")

    # Insert new data with correct columns
    client.execute(
        """
        INSERT INTO oof_xgb_h5 (ts, symbol, pred, conf, run_id, model_ver)
        VALUES
        """,
        data_rows,
    )

    print(f"✓ Uploaded to trading.oof_xgb_h5")

    # Verify
    count = client.execute(f"SELECT count(*) FROM oof_xgb_h5 WHERE symbol = '{symbol}'")[0][0]
    avg_pred = client.execute(f"SELECT avg(pred) FROM oof_xgb_h5 WHERE symbol = '{symbol}'")[0][0]
    print(f"✓ Verified {count:,} rows, avg pred: {avg_pred:.4f}")


def main():
    parser = argparse.ArgumentParser(description="Train calibrated XGBoost model")
    parser.add_argument("--symbol", default="TSLA")
    parser.add_argument("--start", default="2025-04-18 00:00:00")
    parser.add_argument("--end", default="2025-11-05 00:00:00")
    parser.add_argument("--label-col", default="label_h30")
    parser.add_argument("--features", nargs="+", default=["vwap", "rsi_14", "ema_9", "atr_14", "close"])
    parser.add_argument("--n-splits", type=int, default=3)
    parser.add_argument("--output-dir", default="/tmp/ml_models")
    parser.add_argument("--upload-oof", action="store_true", help="Upload OOF to ClickHouse")

    # Load optimal hyperparameters from Optuna results
    parser.add_argument("--params-file", default="/tmp/optuna_best_params_TSLA_label_h30.json")

    args = parser.parse_args()

    # Load optimal hyperparameters
    with open(args.params_file) as f:
        optuna_results = json.load(f)

    best_params = optuna_results["best_params"]
    xgb_params = {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "eta": best_params["eta"],
        "max_depth": best_params["max_depth"],
        "subsample": best_params["subsample"],
        "colsample_bytree": best_params["colsample_bytree"],
        "min_child_weight": best_params["min_child_weight"],
        "gamma": best_params["gamma"],
        "reg_alpha": best_params["reg_alpha"],
        "reg_lambda": best_params["reg_lambda"],
    }
    num_round = best_params["num_round"]

    print("="*60)
    print("TRAINING CALIBRATED XGBOOST MODEL")
    print("="*60)
    print(f"Symbol: {args.symbol}")
    print(f"Label: {args.label_col}")
    print(f"Features: {args.features}")
    print(f"Optimal params from: {args.params_file}")
    print(f"CV Folds: {args.n_splits}")
    print()

    # Load data
    df, X, y = fetch_data(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        label_col=args.label_col,
        features=args.features,
    )

    # Train with calibration
    models, calibrators, oof_raw, oof_calibrated, fold_metrics = train_with_calibration(
        X, y, xgb_params, num_round, args.n_splits
    )

    # Save models and OOF
    model_dir = save_models_and_oof(
        df, models, calibrators, oof_raw, oof_calibrated,
        args.symbol, args.label_col, args.features, xgb_params, fold_metrics,
        args.output_dir,
    )

    # Upload OOF to ClickHouse (use raw predictions - better performance)
    if args.upload_oof:
        upload_oof_to_clickhouse(df, oof_raw, args.label_col, args.symbol, model_ver="xgb_h30_optimized")

    print("\n" + "="*60)
    print("✓ TRAINING COMPLETE")
    print("="*60)
    print(f"Model directory: {model_dir}")
    print(f"\nTo use this model in production:")
    print(f"1. Copy models to production: {model_dir}")
    print(f"2. Update model path in config")
    print(f"3. Restart trading system")


if __name__ == "__main__":
    main()
