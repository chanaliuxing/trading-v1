"""
ml/train_intraday_long_options.py

Baseline training script for intraday long-only options ML:
- Universe: SPY, QQQ, Tech7
- Timeframe: 1m
- Labels: y_dir (Up/Down/Flat), y_move (Move/NoMove)

Responsibilities:
- Pull labeled data from ClickHouse (trading.features_1m_labeled + features_1m)
- Build train/validation splits by time
- Train two models:
    * clf_dir  (multi-class: Up/Down/Flat)
    * clf_move (binary: Move/NoMove)
- Run basic validation and compute coverage / conditional hit metrics
- Register model in trading.option_ml_models

NOTE:
- This is a skeleton; concrete feature list, model hyperparameters, and
  persistence formats should be filled in iteratively.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Tuple

import numpy as np
import pandas as pd
import os
import sys
from pathlib import Path
from sklearn.base import BaseEstimator, ClassifierMixin

# Ensure project root is on sys.path when invoked as a script
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger
from ml.training_gate import TrainingGateConfig, run_training_gate

logger = get_logger(__name__)


class MappedWrapper(BaseEstimator, ClassifierMixin):
    """Wrapper to map negative labels (e.g. -1) to 0-based indices for XGB/LGBM."""
    _estimator_type = "classifier"

    def __init__(self, base_model):
        self.base_model = base_model
        # Map -1 -> 0, 0 -> 1, 1 -> 2
        self.mapping = {-1: 0, 0: 1, 1: 2}
        self.classes_ = np.array([-1, 0, 1])

    def __sklearn_tags__(self):
        tags = super().__sklearn_tags__()
        tags.estimator_type = "classifier"
        return tags

    def fit(self, X, y):
        # Only map if y contains negative values
        y_mapped = pd.Series(y).map(self.mapping).fillna(y) 
        self.base_model.fit(X, y_mapped)
        return self

    def predict(self, X):
        pred = self.base_model.predict(X)
        # Map back: 0 -> -1, 1 -> 0, 2 -> 1
        inverse = {v: k for k, v in self.mapping.items()}
        return np.vectorize(inverse.get)(pred)

    def predict_proba(self, X):
        return self.base_model.predict_proba(X)


try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset
except ImportError:
    torch = None

if torch:
    class _LSTM(nn.Module):
        def __init__(self, inp, hid, lay, out):
            super().__init__()
            self.lstm = nn.LSTM(inp, hid, lay, batch_first=True)
            self.fc = nn.Linear(hid, out)
        def forward(self, x):
            o, _ = self.lstm(x)
            return self.fc(o[:, -1, :])

    class _Transformer(nn.Module):
        def __init__(self, inp, hid, lay, out, nhead=4, lookback=30):
            super().__init__()
            self.embedding = nn.Linear(inp, hid)
            # Learnable positional encoding
            self.pos_encoder = nn.Parameter(torch.randn(1, lookback, hid))
            
            enc_layer = nn.TransformerEncoderLayer(d_model=hid, nhead=nhead, batch_first=True)
            self.transformer = nn.TransformerEncoder(enc_layer, num_layers=lay)
            self.fc = nn.Linear(hid, out)

        def forward(self, x):
            # x: (batch, seq, inp)
            x = self.embedding(x) # (batch, seq, hid)
            x = x + self.pos_encoder[:, :x.size(1), :]
            out = self.transformer(x)
            return self.fc(out[:, -1, :])

class LSTMWrapper(BaseEstimator, ClassifierMixin):
    """
    Lightweight LSTM Classifier compatible with sklearn API.
    Handles sequence generation from 2D input.
    """
    _estimator_type = "classifier"

    def __init__(self, lookback=30, hidden_size=64, num_layers=1, epochs=10, batch_size=256, lr=0.001):
        self.lookback = lookback
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.epochs = epochs
        self.batch_size = batch_size
        self.lr = lr
        self.model = None
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu") if torch else None

    def __sklearn_tags__(self):
        tags = super().__sklearn_tags__()
        tags.estimator_type = "classifier"
        return tags

    def _create_sequences(self, X, y=None):
        # X is (N, F). Output (N, lookback, F)
        # We zero-pad the beginning to keep output length = input length
        # This aligns with the 'current row prediction' paradigm
        N, F = X.shape
        out_X = np.zeros((N, self.lookback, F), dtype=np.float32)
        
        # Vectorized or rolling approach is faster but complex. Simple loop for clarity/robustness.
        # Actually, for speed, let's just loop. N is ~100k, loop might be slow in python.
        # Use stride_tricks? No, let's use a simple sliding window generator or just simple padding.
        # Ideally, we want X[i] to be the sequence X[i-lookback+1 : i+1]
        
        # Fast implementation using stride_tricks
        # Pad X with (lookback-1) zeros at start
        X_pad = np.pad(X, ((self.lookback-1, 0), (0, 0)), mode='constant')
        # Create sliding window view
        # shape: (N, lookback, F)
        # strides: (stride_row, stride_row, stride_col)
        from numpy.lib.stride_tricks import as_strided
        s0, s1 = X_pad.strides
        out_X = as_strided(X_pad, shape=(N, self.lookback, F), strides=(s0, s0, s1))
        
        return out_X, y

    def fit(self, X, y):
        if torch is None:
            raise RuntimeError("torch not installed")
        
        # Convert X to numpy if DataFrame
        if hasattr(X, "values"):
            X = X.values.astype(np.float32)
        
        y = np.array(y)
        self.classes_ = np.unique(y)
        num_classes = len(self.classes_)
        # Map labels to 0..N-1
        self.label_map_ = {c: i for i, c in enumerate(self.classes_)}
        y_mapped = np.vectorize(self.label_map_.get)(y)

        X_seq, y_seq = self._create_sequences(X, y_mapped)
        
        # Build Model
        input_size = X.shape[1]
        
        self.model = _LSTM(input_size, self.hidden_size, self.num_layers, num_classes).to(self.device)
        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr)
        
        dataset = TensorDataset(torch.tensor(X_seq), torch.tensor(y_seq, dtype=torch.long))
        loader = DataLoader(dataset, batch_size=self.batch_size, shuffle=True)
        
        self.model.train()
        for epoch in range(self.epochs):
            for xb, yb in loader:
                xb, yb = xb.to(self.device), yb.to(self.device)
                optimizer.zero_grad()
                outputs = self.model(xb)
                loss = criterion(outputs, yb)
                loss.backward()
                optimizer.step()
        return self

    def predict_proba(self, X):
        if self.model is None:
            raise RuntimeError("Model not fitted")
        
        if hasattr(X, "values"):
            X = X.values.astype(np.float32)
            
        X_seq, _ = self._create_sequences(X)
        dataset = TensorDataset(torch.tensor(X_seq))
        loader = DataLoader(dataset, batch_size=self.batch_size, shuffle=False)
        
        self.model.eval()
        probs = []
        with torch.no_grad():
            for xb, in loader:
                xb = xb.to(self.device)
                out = self.model(xb)
                probs.append(torch.softmax(out, dim=1).cpu().numpy())
        
        return np.vstack(probs)

    def predict(self, X):
        proba = self.predict_proba(X)
        indices = np.argmax(proba, axis=1)
        return self.classes_[indices]


class TransformerWrapper(LSTMWrapper):
    """
    Lightweight Transformer Classifier.
    """
    def fit(self, X, y):
        if torch is None:
            raise RuntimeError("torch not installed")
        
        if hasattr(X, "values"):
            X = X.values.astype(np.float32)
        
        y = np.array(y)
        self.classes_ = np.unique(y)
        num_classes = len(self.classes_)
        self.label_map_ = {c: i for i, c in enumerate(self.classes_)}
        y_mapped = np.vectorize(self.label_map_.get)(y)

        X_seq, y_seq = self._create_sequences(X, y_mapped)
        
        input_size = X.shape[1]
        lookback = self.lookback
        
        self.model = _Transformer(input_size, self.hidden_size, self.num_layers, num_classes, lookback=lookback).to(self.device)
        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr)
        
        dataset = TensorDataset(torch.tensor(X_seq), torch.tensor(y_seq, dtype=torch.long))
        loader = DataLoader(dataset, batch_size=self.batch_size, shuffle=True)
        
        self.model.train()
        for epoch in range(self.epochs):
            for xb, yb in loader:
                xb, yb = xb.to(self.device), yb.to(self.device)
                optimizer.zero_grad()
                outputs = self.model(xb)
                loss = criterion(outputs, yb)
                loss.backward()
                optimizer.step()
        return self


@dataclass
class TrainConfig:
    universe: List[str]
    horizon_min: int = 30
    ret_threshold: float = 0.0015  # 0.15%
    range_threshold: float = 0.004  # 0.4%
    val_frac: float = 0.2


def _fetch_labeled_data(cfg: TrainConfig, start: str, end: str) -> pd.DataFrame:
    """
    Fetch labeled 1m features from ClickHouse.

    Expected:
    - trading.features_1m_labeled holds labels (one row per symbol, ts).
    - trading.features_1m holds feature columns (aligned by symbol, ts).
    """
    client = get_client()

    universe_list = ",".join(f"'{s}'" for s in cfg.universe)

    sql = f"""
    SELECT
        f.symbol,
        f.ts,
        l.horizon_min,
        l.y_dir,
        l.y_move,
        -- TODO: select concrete feature columns instead of '*'
        f.*
    FROM trading.features_1m AS f
    INNER JOIN trading.features_1m_labeled AS l
        ON f.symbol = l.symbol
       AND f.ts = l.ts
       AND l.horizon_min = {cfg.horizon_min}
    WHERE f.symbol IN ({universe_list})
      AND f.ts >= toDateTime('{start}', 'America/New_York')
      AND f.ts < toDateTime('{end}', 'America/New_York')
    """

    logger.info("Fetching labeled data from ClickHouse", start=start, end=end, horizon_min=cfg.horizon_min)
    rows = client.query(sql)
    if not rows:
        logger.warning("No labeled rows fetched from ClickHouse", start=start, end=end)
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    logger.info("Fetched labeled rows", rows=len(df))
    if not df.empty:
        counts = df.groupby("symbol")["ts"].count().to_dict()
        min_rows = min(counts.values()) if counts else 0
        if min_rows < 1000:
            logger.warning("Low rows for some symbols", counts=counts, horizon_min=cfg.horizon_min)
    return df


def _train_val_split(df: pd.DataFrame, val_frac: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Time-based train/validation split."""
    df_sorted = df.sort_values(["ts"])
    n = len(df_sorted)
    if n == 0:
        return df_sorted, df_sorted
    split_idx = int(n * (1.0 - val_frac))
    train_df = df_sorted.iloc[:split_idx]
    val_df = df_sorted.iloc[split_idx:]
    return train_df, val_df


def _calib_split(df: pd.DataFrame, calib_frac: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Split a DataFrame into train-fit and calibration segments (time-based)."""
    df_sorted = df.sort_values(["ts"])
    n = len(df_sorted)
    if n == 0:
        return df_sorted, df_sorted
    split_idx = int(n * (1.0 - calib_frac))
    train_df = df_sorted.iloc[:split_idx]
    calib_df = df_sorted.iloc[split_idx:]
    return train_df, calib_df


def _prepare_xy(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series, pd.Series]:
    """
    Prepare features X and labels y_dir, y_move.
    NOTE: This is a placeholder; in practice you will:
      - drop non-feature columns (symbol, ts, label columns, etc.)
      - handle NaNs, scaling, etc.
    """
    drop_cols = [
        "symbol",
        "ts",
        "y_dir",
        "y_move",
        "horizon_min",
        "ver",
        "created_at",
        "session_phase",
    ]
    drop_cols = [c for c in df.columns if c in drop_cols]
    X = df.drop(columns=drop_cols, errors="ignore")
    y_dir = df["y_dir"].astype(int)
    y_move = df["y_move"].astype(int)
    return X, y_dir, y_move


def _clean_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop obviously bad feature columns to satisfy the training gate:
    - Columns with high null fraction
    - Columns with a single unique value
    """
    non_feature_cols = {"symbol", "ts", "y_dir", "y_move", "horizon_min", "ver", "created_at"}
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


def _fit_models(
    X_train: pd.DataFrame,
    y_dir: pd.Series,
    y_move: pd.Series,
    model_type: str = "rf",
):
    """
    Train classifiers using specified model type.
    """
    logger.info(f"Training models (type={model_type})")

    if model_type == "xgb":
        from xgboost import XGBClassifier
        clf_dir_base = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05, n_jobs=-1, random_state=42)
        clf_move = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05, n_jobs=-1, random_state=42)
        clf_dir = MappedWrapper(clf_dir_base)

    elif model_type == "lgbm":
        from lightgbm import LGBMClassifier
        clf_dir_base = LGBMClassifier(n_estimators=200, max_depth=6, learning_rate=0.05, n_jobs=-1, random_state=42, verbose=-1)
        clf_move = LGBMClassifier(n_estimators=200, max_depth=6, learning_rate=0.05, n_jobs=-1, random_state=42, verbose=-1)
        clf_dir = MappedWrapper(clf_dir_base)

    elif model_type == "cat":
        from catboost import CatBoostClassifier
        clf_dir_base = CatBoostClassifier(iterations=500, depth=6, learning_rate=0.05, thread_count=-1, random_seed=42, verbose=0, allow_writing_files=False)
        clf_move = CatBoostClassifier(iterations=500, depth=6, learning_rate=0.05, thread_count=-1, random_seed=42, verbose=0, allow_writing_files=False)
        clf_dir = MappedWrapper(clf_dir_base)

    elif model_type == "lstm":
        clf_dir = LSTMWrapper(lookback=30, hidden_size=64, num_layers=1, epochs=10)
        clf_move = LSTMWrapper(lookback=30, hidden_size=64, num_layers=1, epochs=10)

    elif model_type == "transformer":
        clf_dir = TransformerWrapper(lookback=30, hidden_size=64, num_layers=1, epochs=10)
        clf_move = TransformerWrapper(lookback=30, hidden_size=64, num_layers=1, epochs=10)

    else:
        from sklearn.ensemble import RandomForestClassifier
        clf_dir = RandomForestClassifier(n_estimators=200, max_depth=8, n_jobs=-1, random_state=42)
        clf_move = RandomForestClassifier(n_estimators=200, max_depth=8, n_jobs=-1, random_state=42)

    clf_dir.fit(X_train, y_dir)
    clf_move.fit(X_train, y_move)

    return clf_dir, clf_move


def _evaluate_models(
    clf_dir,
    clf_move,
    X_val: pd.DataFrame,
    y_dir_val: pd.Series,
    y_move_val: pd.Series,
    theta_dir: float,
    theta_move: float,
):
    """
    Basic validation including:
    - overall accuracy
    - conditional hit rate when signals are strong
    - coverage (fraction of minutes where a trade would be allowed)
    """
    from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score

    # Direction probabilities
    proba_dir = clf_dir.predict_proba(X_val)
    classes_dir = clf_dir.classes_
    # Map class -> index
    class_to_idx = {int(c): i for i, c in enumerate(classes_dir)}

    p_up = proba_dir[:, class_to_idx.get(1, 0)]
    p_down = proba_dir[:, class_to_idx.get(-1, 0)]
    p_flat = proba_dir[:, class_to_idx.get(0, 0)]

    # Move probabilities
    proba_move = clf_move.predict_proba(X_val)
    classes_move = clf_move.classes_
    idx_move1 = int(np.where(classes_move == 1)[0][0]) if 1 in classes_move else 1
    p_move = proba_move[:, idx_move1]

    # Overall accuracy
    y_dir_pred = clf_dir.predict(X_val)
    acc_dir = accuracy_score(y_dir_val, y_dir_pred)

    y_move_pred = clf_move.predict(X_val)
    acc_move = accuracy_score(y_move_val, y_move_pred)

    # Conditional coverage & hit-rate
    strong_mask = (p_move >= theta_move) & (np.maximum(p_up, p_down) >= theta_dir)
    coverage = float(strong_mask.mean())

    chosen_dir = np.where(p_up > p_down, 1, -1)
    chosen_dir[~strong_mask] = 0

    hit = (chosen_dir == y_dir_val.to_numpy()) & strong_mask
    conditional_hit_rate = float(hit.sum() / max(strong_mask.sum(), 1))

    precision_up = float(precision_score(y_dir_val, y_dir_pred, labels=[1], average="macro", zero_division=0))
    precision_down = float(precision_score(y_dir_val, y_dir_pred, labels=[-1], average="macro", zero_division=0))
    recall_up = float(recall_score(y_dir_val, y_dir_pred, labels=[1], average="macro", zero_division=0))
    recall_down = float(recall_score(y_dir_val, y_dir_pred, labels=[-1], average="macro", zero_division=0))

    try:
        auc_move = float(roc_auc_score(y_move_val, p_move))
    except ValueError:
        auc_move = 0.0

    metrics = {
        "acc_dir": float(acc_dir),
        "acc_move": float(acc_move),
        "coverage": coverage,
        "conditional_hit_rate": conditional_hit_rate,
        "precision_up": precision_up,
        "precision_down": precision_down,
        "recall_up": recall_up,
        "recall_down": recall_down,
        "auc_move": auc_move,
    }

    logger.info("Validation metrics", **metrics)
    return metrics


def _select_thetas(
    clf_dir,
    clf_move,
    X_val: pd.DataFrame,
    y_dir_val: pd.Series,
    y_move_val: pd.Series,
) -> Tuple[float, float, dict]:
    theta_grid = [0.5, 0.55, 0.6, 0.65, 0.7]
    best = {"conditional_hit_rate": -1.0, "coverage": -1.0}
    best_theta = (0.6, 0.65)
    best_metrics = {}

    for td in theta_grid:
        for tm in theta_grid:
            metrics = _evaluate_models(clf_dir, clf_move, X_val, y_dir_val, y_move_val, td, tm)
            if metrics["conditional_hit_rate"] > best["conditional_hit_rate"]:
                best = metrics
                best_theta = (td, tm)
                best_metrics = metrics
            elif metrics["conditional_hit_rate"] == best["conditional_hit_rate"] and metrics["coverage"] > best["coverage"]:
                best = metrics
                best_theta = (td, tm)
                best_metrics = metrics

    return best_theta[0], best_theta[1], best_metrics


def _register_model(
    cfg: TrainConfig,
    start: str,
    end: str,
    metrics: dict,
    theta_dir: float,
    theta_move: float,
) -> str:
    """Insert a row into trading.option_ml_models with basic metadata."""
    client = get_client()
    model_version = f"options_long_v1_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    sql = """
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
     %(val_samples)s, %(val_accuracy_dir)s, %(val_precision_up)s, %(val_precision_down)s,
     %(val_recall_up)s, %(val_recall_down)s, %(val_auc_move)s, %(val_coverage)s,
     %(pnl_per_trade)s, %(max_dd_pct)s,
     %(theta_dir)s, %(theta_move)s, %(meta)s)
    """

    params = {
        "model_version": model_version,
        "train_start": start,
        "train_end": end,
        "universe": cfg.universe,
        "horizon_min": cfg.horizon_min,
        "feature_set_name": "features_1m",
        "val_samples": int(metrics.get("val_samples", 0)),
        "val_accuracy_dir": float(metrics.get("acc_dir", 0.0)),
        "val_precision_up": float(metrics.get("precision_up", 0.0)),
        "val_precision_down": float(metrics.get("precision_down", 0.0)),
        "val_recall_up": float(metrics.get("recall_up", 0.0)),
        "val_recall_down": float(metrics.get("recall_down", 0.0)),
        "val_auc_move": float(metrics.get("auc_move", 0.0)),
        "val_coverage": float(metrics.get("coverage", 0.0)),
        "pnl_per_trade": float(metrics.get("pnl_per_trade", 0.0)),
        "max_dd_pct": float(metrics.get("max_dd_pct", 0.0)),
        "theta_dir": float(theta_dir),
        "theta_move": float(theta_move),
        "meta": json.dumps(
            {
                "config": asdict(cfg),
                "calibration_method": metrics.get("calibration_method"),
            },
            ensure_ascii=False,
        ),
    }

    logger.info("Registering option ML model", model_version=model_version)
    client.command(sql, parameters=params)
    return model_version


def _save_models(model_dir: str, model_version: str, clf_dir, clf_move) -> None:
    import joblib

    os.makedirs(model_dir, exist_ok=True)
    path_dir = os.path.join(model_dir, f"{model_version}_clf_dir.pkl")
    path_move = os.path.join(model_dir, f"{model_version}_clf_move.pkl")
    joblib.dump(clf_dir, path_dir)
    joblib.dump(clf_move, path_move)
    logger.info("Saved models", dir=path_dir, move=path_move)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="Training start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="Training end date (YYYY-MM-DD)")
    parser.add_argument(
        "--universe",
        default="SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA",
        help="Comma-separated universe (underlyings)",
    )
    parser.add_argument("--horizon-min", type=int, default=30)
    parser.add_argument("--ret-threshold", type=float, default=0.0015)
    parser.add_argument("--range-threshold", type=float, default=0.004)
    parser.add_argument("--val-frac", type=float, default=0.2)
    parser.add_argument("--calibration", default="sigmoid", choices=["none", "sigmoid", "isotonic"])
    parser.add_argument("--calib-frac", type=float, default=0.2)
    parser.add_argument("--theta-dir", type=float, default=0.6)
    parser.add_argument("--theta-move", type=float, default=0.65)
    parser.add_argument("--auto-theta", action="store_true")
    parser.add_argument("--model-dir", default="models/options_intraday")
    parser.add_argument("--model-type", default="rf", choices=["rf", "xgb", "lgbm", "cat", "lstm", "transformer"], help="Model type: rf, xgb, lgbm, cat, lstm, transformer")
    args = parser.parse_args()

    cfg = TrainConfig(
        universe=[s.strip().upper() for s in args.universe.split(",") if s.strip()],
        horizon_min=args.horizon_min,
        ret_threshold=args.ret_threshold,
        range_threshold=args.range_threshold,
        val_frac=args.val_frac,
    )

    df = _fetch_labeled_data(cfg, args.start, args.end)
    if df.empty:
        logger.error("No data fetched for training window", start=args.start, end=args.end)
        return

    # Basic training gate on y_dir forward returns as proxy
    df = _clean_features(df)
    gate_cfg = TrainingGateConfig(min_rows=3000, min_label_pos_frac=0.02, min_label_neg_frac=0.02)
    gate_result = run_training_gate(df.assign(label=df["y_dir"]), label_col="label", cfg=gate_cfg)
    if not gate_result.ok:
        logger.error("Training gate failed", reasons=gate_result.reasons, metrics=gate_result.metrics)
        return

    # Optional gate on y_move
    move_gate_cfg = TrainingGateConfig(min_rows=3000, min_label_pos_frac=0.05, min_label_neg_frac=0.05)
    move_gate_label = df["y_move"].map({0: -1, 1: 1})
    move_gate = run_training_gate(df.assign(label=move_gate_label), label_col="label", cfg=move_gate_cfg)
    if not move_gate.ok:
        logger.error("Move gate failed", reasons=move_gate.reasons, metrics=move_gate.metrics)
        return

    train_df, val_df = _train_val_split(df, cfg.val_frac)
    train_fit_df, calib_df = _calib_split(train_df, args.calib_frac)

    X_train, y_dir_train, y_move_train = _prepare_xy(train_fit_df)
    X_val, y_dir_val, y_move_val = _prepare_xy(val_df)
    X_calib, y_dir_calib, y_move_calib = _prepare_xy(calib_df)

    clf_dir, clf_move = _fit_models(X_train, y_dir_train, y_move_train, model_type=args.model_type)

    if args.calibration != "none":
        from sklearn.calibration import CalibratedClassifierCV

        logger.info("Calibrating classifiers", method=args.calibration, calib_rows=len(X_calib))
        clf_dir = CalibratedClassifierCV(clf_dir, method=args.calibration, cv="prefit")
        clf_move = CalibratedClassifierCV(clf_move, method=args.calibration, cv="prefit")
        clf_dir.fit(X_calib, y_dir_calib)
        clf_move.fit(X_calib, y_move_calib)

    if args.auto_theta:
        theta_dir, theta_move, metrics = _select_thetas(clf_dir, clf_move, X_val, y_dir_val, y_move_val)
    else:
        theta_dir, theta_move = args.theta_dir, args.theta_move
        metrics = _evaluate_models(
            clf_dir,
            clf_move,
            X_val,
            y_dir_val,
            y_move_val,
            theta_dir=theta_dir,
            theta_move=theta_move,
        )
    metrics["val_samples"] = int(len(X_val))
    metrics["calibration_method"] = args.calibration

    model_version = _register_model(
        cfg,
        start=args.start,
        end=args.end,
        metrics=metrics,
        theta_dir=theta_dir,
        theta_move=theta_move,
    )

    _save_models(args.model_dir, model_version, clf_dir, clf_move)

    logger.info("Training complete", model_version=model_version)


if __name__ == "__main__":
    main()
