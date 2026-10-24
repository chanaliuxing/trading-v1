"""
ml/train_structure_ranker.py

Train a structure-level ranker/regressor using option_structure_labels_1m
joined with features_1m. Outputs a model that scores candidate structures.

Usage:
  venv/bin/python ml/train_structure_ranker.py \
    --start 2025-12-01 --end 2026-01-01 \
    --universe SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA \
    --horizon-min 30 \
    --model-dir models/options_intraday
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
from typing import List, Tuple

import numpy as np
import pandas as pd

# Ensure project root is on sys.path when invoked as a script
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger
from ml.training_gate import TrainingGateConfig, run_training_gate

logger = get_logger(__name__)


@dataclass
class TrainConfig:
    universe: List[str]
    horizon_min: int = 30
    val_frac: float = 0.2


def _fetch_structure_data(cfg: TrainConfig, start: str, end: str) -> pd.DataFrame:
    client = get_client()
    universe_list = ",".join(f"'{s}'" for s in cfg.universe)
    
    # Parse dates
    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        # Fallback for ISO format with time if needed, though args usually YYYY-MM-DD
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)

    chunk_size = timedelta(days=1)
    current_start = start_dt
    all_dfs = []

    logger.info("Fetching structure-labeled data in chunks", start=start, end=end, horizon_min=cfg.horizon_min)

    while current_start < end_dt:
        current_end = min(current_start + chunk_size, end_dt)
        
        # Format for SQL (ClickHouse expects YYYY-MM-DD HH:MM:SS or similar)
        # Using YYYY-MM-DD is safe for toDateTime
        s_str = current_start.strftime("%Y-%m-%d %H:%M:%S")
        e_str = current_end.strftime("%Y-%m-%d %H:%M:%S")

        sql = f"""
        SELECT
          toDateTime(l.ts) AS ts,
          l.underlying AS symbol,
          l.structure_id,
          l.structure_type,
          l.y_rr,
          l.y_win,
          f.*
        FROM trading.option_structure_labels_1m AS l
        INNER JOIN trading.features_1m AS f
            ON f.symbol = l.underlying
           AND f.ts = toDateTime(l.ts)
        WHERE l.horizon_min = {cfg.horizon_min}
          AND l.ts >= toDateTime('{s_str}', 'America/New_York')
          AND l.ts <  toDateTime('{e_str}', 'America/New_York')
          AND l.underlying IN ({universe_list})
        """
        
        try:
            rows = client.query(sql)
            if rows:
                chunk_df = pd.DataFrame(rows)
                all_dfs.append(chunk_df)
                logger.info("Fetched chunk", start=s_str, end=e_str, rows=len(chunk_df))
            else:
                logger.debug("No rows in chunk", start=s_str, end=e_str)
        except Exception as e:
            logger.error("Failed to fetch chunk", start=s_str, end=e_str, error=str(e))
            raise

        current_start = current_end

    if not all_dfs:
        logger.warning("No structure rows fetched (all chunks empty)", start=start, end=end)
        return pd.DataFrame()

    df = pd.concat(all_dfs, ignore_index=True)
    logger.info("Fetched total structure rows", rows=len(df))
    return df


def _train_val_split(df: pd.DataFrame, val_frac: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df_sorted = df.sort_values(["ts"])
    n = len(df_sorted)
    if n == 0:
        return df_sorted, df_sorted
    split_idx = int(n * (1.0 - val_frac))
    return df_sorted.iloc[:split_idx], df_sorted.iloc[split_idx:]


def _clean_features(df: pd.DataFrame) -> pd.DataFrame:
    non_feature_cols = {
        "symbol",
        "ts",
        "structure_id",
        "structure_type",
        "y_rr",
        "y_win",
        "ver",
        "created_at",
    }
    feature_cols = [c for c in df.columns if c not in non_feature_cols]
    if not feature_cols:
        return df

    null_fracs = df[feature_cols].isna().mean()
    drop_null = [c for c in feature_cols if null_fracs.get(c, 0.0) > 0.2]

    constant_cols = []
    for c in feature_cols:
        if df[c].nunique(dropna=True) <= 1:
            constant_cols.append(c)

    drop_cols = sorted(set(drop_null + constant_cols))
    if drop_cols:
        logger.info("Dropping low-quality feature columns", cols=len(drop_cols))
        return df.drop(columns=drop_cols, errors="ignore")
    return df


def _prepare_xy(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
    non_feature_cols = {
        "symbol",
        "ts",
        "structure_id",
        "structure_type",
        "y_rr",
        "y_win",
        "ver",
        "created_at",
    }
    base = df.drop(columns=[c for c in non_feature_cols if c in df.columns], errors="ignore")
    base = base.select_dtypes(include=[np.number])

    struct_df = df[["structure_id", "structure_type"]].copy()
    struct_dummies = pd.get_dummies(struct_df, columns=["structure_id", "structure_type"], prefix=["sid", "stype"])
    X = pd.concat([base.reset_index(drop=True), struct_dummies.reset_index(drop=True)], axis=1)
    X = X.fillna(0.0)
    y = df["y_rr"].astype(float)
    return X, y


def _fit_model(X_train: pd.DataFrame, y_train: pd.Series):
    from sklearn.ensemble import RandomForestRegressor

    model = RandomForestRegressor(
        n_estimators=200,
        max_depth=10,
        n_jobs=-1,
        random_state=42,
    )
    model.fit(X_train, y_train)
    return model


def _evaluate(model, X_val: pd.DataFrame, y_val: pd.Series) -> dict:
    from sklearn.metrics import mean_absolute_error, r2_score

    preds = model.predict(X_val)
    mae = float(mean_absolute_error(y_val, preds))
    r2 = float(r2_score(y_val, preds))
    return {"mae": mae, "r2": r2}


def _save_model(model_dir: str, model_version: str, model, feature_cols: List[str]) -> None:
    import joblib

    os.makedirs(model_dir, exist_ok=True)
    path = os.path.join(model_dir, f"{model_version}_structure_rank.pkl")
    joblib.dump({"model": model, "columns": feature_cols}, path)
    logger.info("Saved structure ranker", path=path)


def _register_model(
    cfg: TrainConfig,
    start: str,
    end: str,
    metrics: dict,
    structure_set: List[str],
) -> str:
    client = get_client()
    model_version = f"options_struct_v1_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    meta = {
        "model_type": "structure_rank",
        "label": "y_rr",
        "structure_set": structure_set,
        "selection_policy": "top1_per_symbol",
        "metrics": metrics,
    }

    client.command(
        """
        INSERT INTO trading.option_ml_models
        (model_version, created_at,
         train_start, train_end, universe, horizon_min, feature_set_name,
         val_samples, val_accuracy_dir, val_precision_up, val_precision_down,
         val_recall_up, val_recall_down, val_auc_move, val_coverage,
         pnl_per_trade, max_dd_pct,
         theta_dir, theta_move, meta)
        VALUES
        (%(model_version)s, now('America/New_York'),
         toDateTime(%(train_start)s, 'America/New_York'),
         toDateTime(%(train_end)s, 'America/New_York'),
         %(universe)s, %(horizon_min)s, %(feature_set_name)s,
         %(val_samples)s, 0, 0, 0,
         0, 0, 0, 0,
         0, 0,
         0, 0, %(meta)s)
        """,
        parameters={
            "model_version": model_version,
            "train_start": start,
            "train_end": end,
            "universe": cfg.universe,
            "horizon_min": cfg.horizon_min,
            "feature_set_name": "features_1m",
            "val_samples": int(metrics.get("val_samples", 0)),
            "meta": json.dumps(meta),
        },
    )

    logger.info("Registered structure ranker", model_version=model_version)
    return model_version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--horizon-min", type=int, default=30)
    parser.add_argument("--val-frac", type=float, default=0.2)
    parser.add_argument("--model-dir", default="models/options_intraday")
    args = parser.parse_args()

    cfg = TrainConfig(universe=[s.strip().upper() for s in args.universe.split(",") if s.strip()], horizon_min=args.horizon_min, val_frac=args.val_frac)

    df = _fetch_structure_data(cfg, args.start, args.end)
    if df.empty:
        return

    df = _clean_features(df)
    gate_result = run_training_gate(df.assign(label=df["y_rr"]), label_col="label", cfg=TrainingGateConfig())
    if not gate_result.ok:
        logger.error("Training gate failed", reasons=gate_result.reasons, metrics=gate_result.metrics)
        return

    train_df, val_df = _train_val_split(df, cfg.val_frac)
    X_train, y_train = _prepare_xy(train_df)
    X_val, y_val = _prepare_xy(val_df)

    model = _fit_model(X_train, y_train)
    metrics = _evaluate(model, X_val, y_val)
    metrics["val_samples"] = int(len(val_df))

    structure_set = sorted(df["structure_id"].unique().tolist())
    model_version = _register_model(cfg, args.start, args.end, metrics, structure_set)
    _save_model(args.model_dir, model_version, model, list(X_train.columns))

    logger.info("Structure ranker training complete", model_version=model_version, metrics=metrics)


if __name__ == "__main__":
    main()
