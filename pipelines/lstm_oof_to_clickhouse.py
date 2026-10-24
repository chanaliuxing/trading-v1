"""Generate strict out-of-fold LSTM predictions and write to ClickHouse."""
from __future__ import annotations

import argparse
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, List, Tuple

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from clickhouse_driver import Client
from torch.utils.data import DataLoader, Dataset

try:
    from sklearn.model_selection import TimeSeriesSplit
except Exception as exc:  # pragma: no cover
    raise ImportError(
        "pipelines.lstm_oof_to_clickhouse requires scikit-learn. "
        "Install it via `pip install scikit-learn`."
    ) from exc


class SeqFeatDataset(Dataset):
    """Sliding-window dataset returning (seq, label, ts, symbol)."""

    def __init__(self, df: pd.DataFrame, seq_cols: List[str], y_col: str | None, seq_len: int) -> None:
        self.df = df.reset_index(drop=True)
        self.seq_cols = list(seq_cols)
        self.y_col = y_col
        self.seq_len = seq_len
        self.indices = np.arange(seq_len - 1, len(df))

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, idx: int):
        j = int(self.indices[idx])
        window = self.df.iloc[j - self.seq_len + 1 : j + 1]
        x = window[self.seq_cols].to_numpy(dtype=np.float32)
        y = np.float32(window.iloc[-1][self.y_col]) if self.y_col else np.float32(0.0)
        ts = window.iloc[-1]["ts"]
        sym = window.iloc[-1]["symbol"]
        return x, y, ts, sym


class LSTMHead(nn.Module):
    def __init__(self, in_dim: int, hidden: int = 64, num_layers: int = 1, dropout: float = 0.1) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=in_dim,
            hidden_size=hidden,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.head = nn.Sequential(nn.LayerNorm(hidden), nn.ReLU(), nn.Linear(hidden, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # x: [B, T, F]
        out, _ = self.lstm(x)
        h = out[:, -1, :]
        return self.head(h).squeeze(-1)


@dataclass
class OOFConfig:
    ch_host: str = "localhost"
    ch_port: int = 9000
    ch_user: str = "default"
    ch_password: str = ""
    ch_db: str = "trading"

    src_table: str = "features_1m"
    oof_table: str = "oof_lstm_h5"

    symbol: str = "TSLA"
    start: str = "2024-01-01 00:00:00"
    end: str = "2025-10-01 00:00:00"

    label_col: str = "label_h5"
    seq_cols: Tuple[str, ...] = ("close", "vwap", "rsi_14", "ema_9", "atr_14")
    seq_len: int = 60

    batch: int = 512
    lr: float = 1e-3
    epochs: int = 2
    folds: int = 4
    embargo_min: int = 10
    hidden: int = 64
    num_layers: int = 1
    dropout: float = 0.1

    horizon_min: int = 5

    run_id: str | None = None
    model_ver: str | None = None

    seed: int = 1337


def set_seed(seed: int) -> None:
    import random

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def ch_client(cfg: OOFConfig) -> Client:
    return Client(
        host=cfg.ch_host,
        port=cfg.ch_port,
        user=cfg.ch_user,
        password=cfg.ch_password,
        database=cfg.ch_db,
        settings={"use_numpy": True},
    )


def load_frame(cfg: OOFConfig) -> pd.DataFrame:
    cols = ["ts", "symbol", cfg.label_col, *cfg.seq_cols]
    q = f"""
        SELECT {', '.join(cols)}
        FROM {cfg.src_table}
        WHERE symbol = %(sym)s
          AND ts >= %(st)s AND ts < %(ed)s
        ORDER BY ts
    """
    cli = ch_client(cfg)
    data = cli.execute(q, {"sym": cfg.symbol, "st": cfg.start, "ed": cfg.end})
    df = pd.DataFrame(data, columns=cols)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.dropna().reset_index(drop=True)


def iter_time_folds(df: pd.DataFrame, n_splits: int) -> Iterable[Tuple[np.ndarray, np.ndarray]]:
    splitter = TimeSeriesSplit(n_splits=n_splits)
    idx = np.arange(len(df))
    for train_idx, val_idx in splitter.split(idx):
        yield train_idx, val_idx


def upsert_oof(cfg: OOFConfig, oof_df: pd.DataFrame, run_id: str, model_ver: str) -> None:
    if oof_df.empty:
        print("[WARN] OOF dataframe empty; nothing written.")
        return
    cli = ch_client(cfg)
    cli.execute(
        f"ALTER TABLE {cfg.oof_table} DELETE WHERE run_id = %(rid)s",
        {"rid": run_id},
    )
    batch = 100_000
    cols = ["ts", "symbol", "pred", "conf", "run_id", "model_ver"]
    query = f"INSERT INTO {cfg.oof_table} (ts, symbol, pred, conf, run_id, model_ver) VALUES"
    for start in range(0, len(oof_df), batch):
        part = oof_df.iloc[start : start + batch].copy()
        part["run_id"] = run_id
        part["model_ver"] = model_ver
        rows = list(part[cols].itertuples(index=False, name=None))
        cli.execute(query, rows)
    print(f"[OOF] wrote {len(oof_df)} rows to {cfg.oof_table} run_id={run_id}")


def train_fold(train_df: pd.DataFrame, val_df: pd.DataFrame, cfg: OOFConfig, device: torch.device) -> pd.DataFrame:
    if len(train_df) < cfg.seq_len or len(val_df) < cfg.seq_len:
        return pd.DataFrame(columns=["ts", "symbol", "pred", "conf"])

    model = LSTMHead(len(cfg.seq_cols), cfg.hidden, cfg.num_layers, cfg.dropout).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr)
    loss_fn = nn.MSELoss()

    ds_tr = SeqFeatDataset(train_df, list(cfg.seq_cols), cfg.label_col, cfg.seq_len)
    ds_va = SeqFeatDataset(val_df, list(cfg.seq_cols), cfg.label_col, cfg.seq_len)
    if len(ds_tr) == 0 or len(ds_va) == 0:
        return pd.DataFrame(columns=["ts", "symbol", "pred", "conf"])

    dl_tr = DataLoader(ds_tr, batch_size=cfg.batch, shuffle=False)
    dl_va = DataLoader(ds_va, batch_size=cfg.batch, shuffle=False)

    for _ in range(cfg.epochs):
        model.train()
        for x, y, _, _ in dl_tr:
            x = x.to(device)
            y = y.to(device)
            opt.zero_grad()
            pred = model(x)
            loss = loss_fn(pred, y)
            loss.backward()
            opt.step()

    model.eval()
    preds: List[np.ndarray] = []
    ts_list: List[int] = []
    sym_list: List[str] = []
    with torch.no_grad():
        for x, _, ts, sym in dl_va:
            x = x.to(device)
            p = model(x).cpu().numpy()
            preds.append(p)
            ts_list.extend(ts.tolist())
            sym_list.extend(sym.tolist())

    if not preds:
        return pd.DataFrame(columns=["ts", "symbol", "pred", "conf"])

    pred = np.concatenate(preds).astype(np.float32)
    denom = np.percentile(np.abs(pred), 90) + 1e-6
    conf = np.clip(np.abs(pred) / denom, 0.0, 1.0).astype(np.float32)

    return (
        pd.DataFrame({"ts": ts_list, "symbol": sym_list, "pred": pred, "conf": conf})
        .sort_values("ts")
        .reset_index(drop=True)
    )


def run_oof_pipeline(cfg: OOFConfig) -> str:
    """Generate strict OOF predictions and write to ClickHouse. Returns run_id."""
    set_seed(cfg.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    df = load_frame(cfg)
    if df.empty:
        raise ValueError("No rows fetched for the given symbol/time range")

    embargo_delta = pd.Timedelta(minutes=max(cfg.embargo_min, 0))

    all_oof: List[pd.DataFrame] = []
    for idx, (train_idx, val_idx) in enumerate(iter_time_folds(df, cfg.folds)):
        train_df = df.iloc[train_idx].copy()
        val_df = df.iloc[val_idx].copy()
        if train_df.empty or val_df.empty:
            continue

        cutoff_ts = train_df["ts"].iloc[-1] + embargo_delta
        val_df = val_df[val_df["ts"] >= cutoff_ts]
        if val_df.empty:
            print(f"[OOF] fold {idx+1}/{cfg.folds} skipped due to embargo gap")
            continue

        print(
            f"[OOF] fold {idx+1}/{cfg.folds}: train={len(train_df)} val={len(val_df)} "
            f"(cutoff={cutoff_ts})"
        )
        fold_oof = train_fold(train_df, val_df, cfg, device)
        if not fold_oof.empty:
            all_oof.append(fold_oof)

    if not all_oof:
        raise RuntimeError("No OOF predictions were generated")

    oof_df = pd.concat(all_oof, axis=0).sort_values("ts").reset_index(drop=True)
    git_sha = subprocess.getoutput("git rev-parse --short HEAD") or "nogit"
    run_id = cfg.run_id or f"oof_{time.strftime('%Y%m%d_%H%M%S')}_{git_sha}"
    model_ver = cfg.model_ver or f"lstm_h{cfg.hidden}_l{cfg.num_layers}_seq{cfg.seq_len}_e{cfg.epochs}"
    upsert_oof(cfg, oof_df, run_id, model_ver)
    return run_id


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate LSTM OOF predictions into ClickHouse")
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--user", default="default")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="trading")
    p.add_argument("--src-table", "--src_table", default="features_1m")
    p.add_argument("--oof-table", "--oof_table", default="oof_lstm_h5")
    p.add_argument("--seq-len", "--seq_len", type=int, default=60)
    p.add_argument("--folds", type=int, default=4)
    p.add_argument("--embargo", "--embargo-min", "--embargo_min", type=int, default=10)
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--hidden", type=int, default=64)
    p.add_argument("--num-layers", type=int, default=1)
    p.add_argument("--dropout", type=float, default=0.1)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--label-col", "--label_col", default="label_h5")
    p.add_argument("--seq-cols", "--seq_cols", nargs="+", default=["close", "vwap", "rsi_14", "ema_9", "atr_14"])
    p.add_argument("--run-id", "--run_id", default=None)
    p.add_argument("--model-ver", "--model_ver", default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = OOFConfig(
        ch_host=args.host,
        ch_port=args.port,
        ch_user=args.user,
        ch_password=args.password,
        ch_db=args.database,
        src_table=args.src_table,
        oof_table=args.oof_table,
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        label_col=args.label_col,
        seq_cols=tuple(args.seq_cols),
        seq_len=args.seq_len,
        folds=args.folds,
        embargo_min=args.embargo,
        epochs=args.epochs,
        hidden=args.hidden,
        num_layers=args.num_layers,
        dropout=args.dropout,
        batch=args.batch,
        lr=args.lr,
        run_id=args.run_id,
        model_ver=args.model_ver,
    )
    run_id = run_oof_pipeline(cfg)
    print(f"Run complete. run_id={run_id}")
