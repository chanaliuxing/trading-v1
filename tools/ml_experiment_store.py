#!/usr/bin/env python3
from __future__ import annotations
"""
Persist ML experiment results (from ml_experiments.jsonl) into ClickHouse.

Table: llm.ai_ml_experiments
Columns (minimal):
  - ts DateTime
  - job_id String
  - job_type String
  - window_days Int32
  - horizon_min Int32
  - label_bps Int32
  - fusion_auc Float64
  - fusion_ic Float64
  - coverage Float64
  - n_bars Int32
  - feature_families_enabled Array(String)

Usage (from repo root, with venv):
  venv/bin/python tools/ml_experiment_store.py --days 1
"""

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

try:
    import clickhouse_connect  # type: ignore
except Exception as e:  # pragma: no cover
    clickhouse_connect = None


def _cfg_ai_trader() -> Dict[str, Any]:
    p = Path("scripts/ai_trader/config/ai_trader.yaml")
    return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}


def _ch_client():
    if clickhouse_connect is None:
        raise RuntimeError("clickhouse_connect not installed. pip install clickhouse-connect")
    cfg = _cfg_ai_trader().get("clickhouse", {}) or {}
    host = "localhost"
    port = 8123
    user = "default"
    password = ""
    database = "llm"
    try:
        dsn = (cfg.get("dsn") or "").strip()
        toks = dsn.split()
        for i, t in enumerate(toks):
            if t == "--host" and i + 1 < len(toks):
                host = toks[i + 1]
            if t == "--port" and i + 1 < len(toks):
                port = int(toks[i + 1])
            if t == "--user" and i + 1 < len(toks):
                user = toks[i + 1]
            if t == "--password" and i + 1 < len(toks):
                password = toks[i + 1]
            if t == "--database" and i + 1 < len(toks):
                database = toks[i + 1]
    except Exception:
        pass
    return clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password, database=database
    )


def _ensure_table(cli) -> None:
    ddl_db = "CREATE DATABASE IF NOT EXISTS llm"
    ddl_tbl = """
    CREATE TABLE IF NOT EXISTS llm.ai_ml_experiments (
        ts DateTime,
        job_id String,
        job_type String,
        window_days Int32,
        horizon_min Int32,
        label_bps Int32,
        fusion_auc Float64,
        fusion_ic Float64,
        coverage Float64,
        n_bars Int32,
        feature_families_enabled Array(String)
    )
    ENGINE = MergeTree()
    ORDER BY (ts, job_id)
    """
    cli.command(ddl_db)
    cli.command(ddl_tbl)


def _iter_experiments(runs_dir: Path, days: int):
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    cur = end_date
    while cur >= start_date:
        day_dir = runs_dir / cur.isoformat()
        exp_file = day_dir / "ml_experiments.jsonl"
        if exp_file.exists():
            for line in exp_file.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                yield row
        cur -= timedelta(days=1)


def _parse_row(row: Dict[str, Any]) -> Dict[str, Any] | None:
    if (row.get("job_type") or "").lower() != "oof":
        return None
    metrics = row.get("metrics") or {}
    params = row.get("params") or {}
    try:
        ts_raw = row.get("ts")
        ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now()
    except Exception:
        ts = datetime.now()
    job_id = str(row.get("job_id") or "")
    horizon = params.get("horizon_min", metrics.get("horizon_min"))
    label_bps = params.get("label_bps", metrics.get("label_bps"))
    window_days = params.get("window_days", metrics.get("window_days"))
    try:
        h = int(horizon) if horizon is not None else None
        b = int(label_bps) if label_bps is not None else None
        w = int(window_days) if window_days is not None else None
    except Exception:
        return None
    if h is None or b is None or w is None:
        return None
    auc = metrics.get("fusion_auc", metrics.get("auc"))
    ic = metrics.get("fusion_ic", metrics.get("ic"))
    cov = metrics.get("coverage")
    n_bars = metrics.get("n_bars") or metrics.get("fusion_n")
    try:
        auc_f = float(auc)
        ic_f = float(ic)
    except Exception:
        return None
    cov_f = float(cov) if isinstance(cov, (int, float)) else 0.0
    try:
        n_bars_i = int(n_bars) if n_bars is not None else 0
    except Exception:
        n_bars_i = 0
    ff = row.get("feature_families") or {}
    enabled_fams = ff.get("enabled") or []
    if not isinstance(enabled_fams, list):
        enabled_fams = []
    enabled_fams = [str(x) for x in enabled_fams]
    return {
        "ts": ts,
        "job_id": job_id,
        "job_type": str(row.get("job_type") or ""),
        "window_days": w,
        "horizon_min": h,
        "label_bps": b,
        "fusion_auc": auc_f,
        "fusion_ic": ic_f,
        "coverage": cov_f,
        "n_bars": n_bars_i,
        "feature_families_enabled": enabled_fams,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="回溯最近 N 天的 ml_experiments.jsonl")
    ap.add_argument("--runs-dir", default="runs/ai_trader", help="runs 根目录")
    args = ap.parse_args()

    cli = _ch_client()
    _ensure_table(cli)

    runs_dir = Path(args.runs_dir)
    rows: List[Dict[str, Any]] = []
    for row in _iter_experiments(runs_dir, args.days):
        parsed = _parse_row(row)
        if parsed:
            rows.append(parsed)

    if not rows:
        print("No OOF experiments to insert.")
        return

    # Prepare data as a list of row tuples in explicit column order to avoid
    # KeyError issues with different clickhouse_connect versions.
    cols = [
        "ts",
        "job_id",
        "job_type",
        "window_days",
        "horizon_min",
        "label_bps",
        "fusion_auc",
        "fusion_ic",
        "coverage",
        "n_bars",
        "feature_families_enabled",
    ]
    data = [[r[c] for c in cols] for r in rows]

    cli.insert("llm.ai_ml_experiments", data, column_names=cols)
    print(f"Inserted {len(rows)} rows into llm.ai_ml_experiments.")


if __name__ == "__main__":
    main()
