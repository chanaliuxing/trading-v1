"""Train XGBoost models using feature view (optionally with LSTM relay features)."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import xgboost as xgb
from clickhouse_driver import Client
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit


@dataclass
class TrainConfig:
    ch_host: str = "localhost"
    ch_port: int = 9000
    ch_user: str = "default"
    ch_password: str = ""
    ch_db: str = "trading"

    table_view: str = "features_1m_with_oof"
    symbol: str = "TSLA"
    start: str = "2024-06-01 00:00:00"
    end: str = "2025-10-01 00:00:00"

    label_col: str = "label_h30"
    base_feats: Tuple[str, ...] = ("vwap", "rsi_14", "ema_9", "atr_14", "close")
    relay_feats: Tuple[str, ...] = ("lstm_oof_h5", "lstm_oof_conf_h5")

    n_splits: int = 5
    num_round: int = 300
    learning_rate: float = 0.05
    max_depth: int = 5
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: float = 1.0

    use_relay: int = 1
    run_id: str = ""

    # optional experiment logging
    record_run: bool = False
    runs_table: str | None = None
    study: str = "relay_manual"
    trial_id: int = 0


def ch_client(cfg: TrainConfig) -> Client:
    return Client(
        host=cfg.ch_host,
        port=cfg.ch_port,
        user=cfg.ch_user,
        password=cfg.ch_password,
        database=cfg.ch_db,
        settings={"use_numpy": True},
    )


def fetch_frame(cfg: TrainConfig) -> pd.DataFrame:
    cols = ["ts", "symbol", cfg.label_col, *cfg.base_feats]
    if cfg.use_relay:
        cols += list(cfg.relay_feats)
    q = f"""
        SELECT {', '.join(cols)}
        FROM {cfg.table_view}
        WHERE symbol = %(sym)s
          AND ts >= %(st)s AND ts < %(ed)s
        ORDER BY ts
    """
    cli = ch_client(cfg)
    data = cli.execute(q, {"sym": cfg.symbol, "st": cfg.start, "ed": cfg.end})
    df = pd.DataFrame(data, columns=cols)
    df = df.dropna().reset_index(drop=True)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def timeseries_cv_scores(df: pd.DataFrame, cfg: TrainConfig) -> Tuple[List[float], List[float]]:
    features = list(cfg.base_feats) + (list(cfg.relay_feats) if cfg.use_relay else [])
    X = df[features].to_numpy(dtype=np.float32)
    y = (df[cfg.label_col].values > 0).astype(np.int32)

    params = {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "eta": cfg.learning_rate,
        "max_depth": cfg.max_depth,
        "subsample": cfg.subsample,
        "colsample_bytree": cfg.colsample_bytree,
        "min_child_weight": cfg.min_child_weight,
    }

    tscv = TimeSeriesSplit(n_splits=cfg.n_splits)
    aucs: List[float] = []
    losses: List[float] = []

    for fold, (tr, va) in enumerate(tscv.split(X)):
        dtr = xgb.DMatrix(X[tr], label=y[tr])
        dva = xgb.DMatrix(X[va], label=y[va])
        bst = xgb.train(params, dtr, cfg.num_round, evals=[(dva, "valid")], verbose_eval=False)
        proba = bst.predict(dva)
        aucs.append(roc_auc_score(y[va], proba))
        losses.append(log_loss(y[va], proba, labels=[0, 1]))
        print(f"[XGB] fold {fold+1}/{cfg.n_splits}: auc={aucs[-1]:.4f} logloss={losses[-1]:.4f}")

    return aucs, losses


def _record_run(cfg: TrainConfig, aucs: List[float], params: Dict[str, object]) -> None:
    if not cfg.record_run or not cfg.runs_table:
        return
    cli = ch_client(cfg)
    mean_auc = float(np.mean(aucs))
    payload = (
        cfg.study,
        int(cfg.trial_id),
        cfg.run_id or "",
        cfg.symbol,
        cfg.start,
        cfg.end,
        int(cfg.use_relay),
        mean_auc,
        mean_auc,
        [float(x) for x in aucs],
        json.dumps(params, sort_keys=True),
        subprocess.getoutput("git rev-parse --short HEAD") or "nogit",
        hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12],
    )
    q = f"""
        INSERT INTO {cfg.runs_table}
        (study, trial_id, run_id, symbol, start, end, use_relay,
         auc, mean_val, fold_scores, params_json, git_commit, config_sha)
        VALUES
    """
    cli.execute(q, [payload])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train XGB with optional relay features")
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--user", default="default")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="trading")
    p.add_argument("--table-view", "--table_view", default="features_1m_with_oof")
    p.add_argument("--base-feats", "--base_feats", nargs="+", default=["vwap", "rsi_14", "ema_9", "atr_14", "close"])
    p.add_argument("--relay-feats", "--relay_feats", nargs="+", default=["lstm_oof_h5", "lstm_oof_conf_h5"])
    p.add_argument("--label-col", "--label_col", default="label_h30")
    p.add_argument("--splits", type=int, default=5)
    p.add_argument("--rounds", type=int, default=300)
    p.add_argument("--lr", type=float, default=0.05)
    p.add_argument("--max-depth", type=int, default=5)
    p.add_argument("--subsample", type=float, default=0.8)
    p.add_argument("--colsample", type=float, default=0.8)
    p.add_argument("--min-child-weight", "--min_child_weight", type=float, default=1.0)
    p.add_argument("--use-relay", "--use_relay", type=int, default=1, choices=[0, 1])
    p.add_argument("--run-id", "--run_id", default="")
    p.add_argument("--record-run", "--record_run", action="store_true")
    p.add_argument("--runs-table", "--runs_table", default=None)
    p.add_argument("--study", default="relay_manual")
    p.add_argument("--trial-id", "--trial_id", type=int, default=0)
    p.add_argument("--horizon-min", "--horizon_min", type=int, default=30)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # 🛠️ [Review Fix] Add Unified Preflight Check
    from tools.preflight_check import preflight_check_all
    import sys

    # 1. Extract symbols and horizon from args
    symbols = [args.symbol]
    horizons = [int(args.label_col.replace('label_h', ''))] if 'label_h' in args.label_col else [30]

    # 2. Run Check
    ready, snapshot = preflight_check_all(symbols, horizons)

    if not ready:
        print(f"❌ Preflight Failed: {snapshot.blocking_issues}")
        # Write detailed snapshot for debugging
        import json
        with open(f"preflight_fail_{args.run_id or 'default'}.json", "w") as f:
            json.dump(snapshot.to_dict(), f, indent=2, default=str)
        sys.exit(1)
        
    print("✅ Preflight Passed")

    cfg = TrainConfig(
        ch_host=args.host,
        ch_port=args.port,
        ch_user=args.user,
        ch_password=args.password,
        ch_db=args.database,
        table_view=args.table_view,
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        base_feats=tuple(args.base_feats),
        relay_feats=tuple(args.relay_feats),
        label_col=args.label_col,
        n_splits=args.splits,
        num_round=args.rounds,
        learning_rate=args.lr,
        max_depth=args.max_depth,
        subsample=args.subsample,
        colsample_bytree=args.colsample,
        min_child_weight=args.min_child_weight,
        use_relay=args.use_relay,
        run_id=args.run_id,
        record_run=args.record_run,
        runs_table=args.runs_table,
        study=args.study,
        trial_id=args.trial_id,
    )

    df = fetch_frame(cfg)
    if df.empty:
        raise ValueError("No data fetched; check symbol/time range.")

    aucs, losses = timeseries_cv_scores(df, cfg)
    params_record: Dict[str, object] = {
        "symbol": cfg.symbol,
        "start": cfg.start,
        "end": cfg.end,
        "use_relay": cfg.use_relay,
        "base_feats": list(cfg.base_feats),
        "relay_feats": list(cfg.relay_feats),
        "label_col": cfg.label_col,
        "n_splits": cfg.n_splits,
        "num_round": cfg.num_round,
        "learning_rate": cfg.learning_rate,
        "max_depth": cfg.max_depth,
        "subsample": cfg.subsample,
        "colsample_bytree": cfg.colsample_bytree,
        "min_child_weight": cfg.min_child_weight,
        "run_id": cfg.run_id,
    }
    summary = {
        "symbol": cfg.symbol,
        "start": cfg.start,
        "end": cfg.end,
        "use_relay": cfg.use_relay,
        "auc_mean": float(np.mean(aucs)),
        "auc_std": float(np.std(aucs)),
        "logloss_mean": float(np.mean(losses)),
        "fold_aucs": aucs,
        "fold_logloss": losses,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    _record_run(cfg, aucs, params_record)
