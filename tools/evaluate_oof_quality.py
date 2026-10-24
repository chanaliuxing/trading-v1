#!/usr/bin/env python3
from __future__ import annotations
"""
Evaluate OOF prediction quality before running backtests.

Computes signal quality metrics:
- AUC (Area Under Curve): discriminative power
- Brier Score: calibration error
- IC (Information Coefficient): correlation with forward returns
- Top-decile accuracy: % correct in highest-confidence predictions
- Top-decile mean return: average forward return of top 10% conf predictions

Usage:
  python tools/evaluate_oof_quality.py \
    --symbols TSLA,AAPL \
    --start '2025-10-01 09:30:00' \
    --end   '2025-10-29 16:00:00' \
    --features-table trading.features_1m \
    --horizon-min 5 \
    --out runs/ai_trader/oof_eval

Success criteria (per user spec):
- AUC ≥ 0.52 (better than random)
- IC > 0.02 (positive correlation with forward returns)
- Top-decile mean return > 0 (high-conf predictions profitable)
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import yaml

try:
    import clickhouse_connect
except Exception:
    clickhouse_connect = None

try:
    from sklearn.metrics import roc_auc_score, brier_score_loss
except Exception:
    roc_auc_score = None
    brier_score_loss = None


def _cfg() -> Dict[str, Any]:
    p = Path("scripts/ai_trader/config/ai_trader.yaml")
    return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}


def _ch_client():
    if clickhouse_connect is None:
        raise RuntimeError("clickhouse_connect not installed. pip install clickhouse-connect")
    cfg = _cfg().get("clickhouse", {})
    # Defaults
    host = "localhost"; port = 8123; user = "default"; password = ""; database = "trading"
    try:
        dsn = (cfg.get("dsn") or "").strip()
        # Parse --host --port --user --password --database from dsn if present
        toks = dsn.split()
        for i, t in enumerate(toks):
            if t == "--host" and i+1 < len(toks): host = toks[i+1]
            if t == "--port" and i+1 < len(toks): port = int(toks[i+1])
            if t == "--user" and i+1 < len(toks): user = toks[i+1]
            if t == "--password" and i+1 < len(toks): password = toks[i+1]
            if t == "--database" and i+1 < len(toks): database = toks[i+1]
    except Exception:
        pass
    return clickhouse_connect.get_client(host=host, port=port, username=user, password=password, database=database)


def _fetch_oof_with_labels(cli, features_table: str, symbols: List[str], start: str, end: str, horizon_min: int) -> pd.DataFrame:
    """
    Fetch OOF predictions joined with forward returns for evaluation.

    Uses inline JOIN (same as backtest_ml_engine) to bypass ClickHouse view issues.
    """
    syms = ",".join(f"'{s}'" for s in symbols)

    sql = f"""
        SELECT
          f.symbol AS symbol,
          f.ts AS ts,
          f.close AS close,
          l.pred AS lstm_pred,
          l.conf AS lstm_conf,
          x.pred AS xgb_pred,
          x.conf AS xgb_conf
        FROM {features_table} AS f
        LEFT JOIN (
          SELECT ts, symbol, pred, conf
          FROM (
            SELECT ts, symbol, pred, conf,
              row_number() OVER (PARTITION BY symbol, ts ORDER BY updated_at DESC) AS rn
            FROM trading.oof_lstm_h5
          )
          WHERE rn = 1
        ) AS l ON l.symbol = f.symbol AND l.ts = f.ts
        LEFT JOIN (
          SELECT ts, symbol, pred, conf
          FROM (
            SELECT ts, symbol, pred, conf,
              row_number() OVER (PARTITION BY symbol, ts ORDER BY updated_at DESC) AS rn
            FROM trading.oof_xgb_h5
          )
          WHERE rn = 1
        ) AS x ON x.symbol = f.symbol AND x.ts = f.ts
        WHERE f.symbol IN ({syms})
          AND f.ts >= toDateTime('{start}') AND f.ts < toDateTime('{end}')
          AND (
            (toHour(f.ts) = 9 AND toMinute(f.ts) >= 35) OR
            (toHour(f.ts) >= 10 AND toHour(f.ts) < 16)
          )
        ORDER BY f.ts, f.symbol
    """

    rows = cli.query(sql).result_rows
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["symbol", "ts", "close", "lstm_pred", "lstm_conf", "xgb_pred", "xgb_conf"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)

    # --- NEW: Fetch SPY data and merge ---
    spy_sql = f"""
        SELECT ts, close
        FROM {features_table}
        WHERE symbol = 'SPY'
          AND ts >= toDateTime('{start}') AND ts < toDateTime('{end}')
          AND (
            (toHour(ts) = 9 AND toMinute(ts) >= 35) OR
            (toHour(ts) >= 10 AND toHour(ts) < 16)
          )
        ORDER BY ts
    """
    spy_rows = cli.query(spy_sql).result_rows
    if spy_rows:
        spy_df = pd.DataFrame(spy_rows, columns=["ts", "spy_close"])
        spy_df["ts"] = pd.to_datetime(spy_df["ts"], utc=True)
        spy_df["spy_fwd_ret"] = spy_df["spy_close"].pct_change(periods=horizon_min).shift(-horizon_min)
        spy_df["spy_fwd_ret_bps"] = spy_df["spy_fwd_ret"] * 10000.0
        df = pd.merge(df, spy_df[["ts", "spy_fwd_ret_bps"]], on="ts", how="left")
    else:
        df["spy_fwd_ret_bps"] = np.nan # If no SPY data, fill with NaN
    # --- END NEW ---

    # Compute forward returns at horizon
    for sym in symbols:
        mask = df["symbol"] == sym
        df.loc[mask, "fwd_ret"] = df.loc[mask, "close"].pct_change(periods=horizon_min).shift(-horizon_min)

    df["fwd_ret_bps"] = df["fwd_ret"] * 10000.0

    # Create binary labels same as training (y=1 if fwd_ret > 0, for AUC)
    df["y_actual"] = (df["fwd_ret"] > 0).astype(float)

    # Drop rows with missing labels or predictions
    # Clean infinities and NaNs before metrics
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df = df.dropna(subset=["fwd_ret", "lstm_pred", "xgb_pred", "spy_fwd_ret_bps"])

    return df


def _evaluate_model(df: pd.DataFrame, pred_col: str, conf_col: str, model_name: str) -> Dict[str, Any]:
    """Compute quality metrics for one model."""
    if df.empty or pred_col not in df.columns or conf_col not in df.columns:
        return {"model": model_name, "n": 0, "error": "no_data"}

    # Filter to rows with valid predictions
    valid = df[[pred_col, conf_col, "y_actual", "fwd_ret_bps"]].dropna()
    if len(valid) < 100:
        return {"model": model_name, "n": len(valid), "error": "insufficient_data"}

    pred = valid[pred_col].values
    conf = valid[conf_col].values
    y_actual = valid["y_actual"].values
    fwd_ret_bps = valid["fwd_ret_bps"].values

    # AUC
    try:
        if roc_auc_score is not None:
            auc = float(roc_auc_score(y_actual, pred))
        else:
            auc = None
    except Exception:
        auc = None

    # Brier score (calibration error)
    try:
        if brier_score_loss is not None:
            brier = float(brier_score_loss(y_actual, pred))
        else:
            brier = None
    except Exception:
        brier = None

    # IC (Information Coefficient): correlation between prediction and forward return
    try:
        corr_matrix = np.corrcoef(pred, fwd_ret_bps)
        ic_raw = float(corr_matrix[0, 1])
        # Check for NaN/Inf (can occur when variance is zero or data is degenerate)
        if np.isnan(ic_raw) or np.isinf(ic_raw):
            ic = None
        else:
            ic = ic_raw
    except Exception:
        ic = None

    # Beta Correlation: correlation between forward returns and SPY forward returns
    beta_corr = None
    try:
        if "spy_fwd_ret_bps" in valid.columns: # Use valid.columns as df is already filtered
            spy_fwd_rets = valid["spy_fwd_ret_bps"].values
            # Check if there's enough variance for correlation calculation
            if len(np.unique(fwd_ret_bps)) > 1 and len(np.unique(spy_fwd_rets)) > 1:
                corr_matrix_beta = np.corrcoef(fwd_ret_bps, spy_fwd_rets)
                beta_corr_raw = float(corr_matrix_beta[0, 1])
                if np.isnan(beta_corr_raw) or np.isinf(beta_corr_raw):
                    beta_corr = None
                else:
                    beta_corr = beta_corr_raw
            else:
                # Not enough variance to calculate correlation
                beta_corr = None
    except Exception:
        beta_corr = None

    # Top-decile metrics (highest 10% confidence)
    try:
        top_decile_threshold = np.percentile(conf, 90)
        top_mask = conf >= top_decile_threshold
        top_preds = pred[top_mask]
        top_actuals = y_actual[top_mask]
        top_fwd_rets = fwd_ret_bps[top_mask]

        if len(top_actuals) > 0:
            # Accuracy: % of top-conf predictions that were correct direction
            top_accuracy = float(np.mean((top_preds > 0.5) == (top_actuals == 1)))
            # Mean forward return of top-conf predictions
            top_mean_ret = float(np.mean(top_fwd_rets))
            top_n = int(len(top_actuals))
        else:
            top_accuracy = None
            top_mean_ret = None
            top_n = 0
    except Exception:
        top_accuracy = None
        top_mean_ret = None
        top_n = 0

    # Simple EV estimates
    ev = None
    p_win_est = None
    rr_est = None
    try:
        p_win_est = float(np.clip(conf.mean(), 0, 1))
        wins = fwd_ret_bps[fwd_ret_bps > 0]
        losses = np.abs(fwd_ret_bps[fwd_ret_bps < 0])
        if len(wins) > 0 and len(losses) > 0 and losses.mean() > 0:
            rr_est = float(wins.mean() / losses.mean())
            ev = p_win_est * rr_est - (1 - p_win_est)
    except Exception:
        pass

    return {
        "model": model_name,
        "n": int(len(valid)),
        "auc": auc,
        "brier": brier,
        "ic": ic,
        "ev": ev,
        "beta_corr": beta_corr, # Add beta_corr here
        "p_win_est": p_win_est,
        "rr_est": rr_est,
        "top_decile": {
            "n": top_n,
            "accuracy": top_accuracy,
            "mean_fwd_ret_bps": top_mean_ret,
        },
        "mean_pred": float(np.mean(pred)),
        "mean_conf": float(np.mean(conf)),
    }


def _build_conf_calibration(df: pd.DataFrame, pred_col: str, conf_col: str) -> Dict[str, Any]:
    """Build a simple monotonic calibration mapping from confidence to correctness probability.

    Uses quantile binning over confidence and computes observed correctness (based on pred vs y_actual sign).
    Returns a JSON-serializable dict with bins [{low, high, p}, ...].
    """
    try:
        valid = df[[pred_col, conf_col, "y_actual"]].dropna()
        if len(valid) < 200:
            return {"method": "binning", "bins": []}
        pred = valid[pred_col].values
        conf = valid[conf_col].values
        y = valid["y_actual"].values
        # correctness: (pred>0.5) equals (y==1)
        correct = ((pred > 0.5) == (y == 1)).astype(float)
        # quantile bins
        q = np.linspace(0.0, 1.0, 11)  # 10 bins
        edges = np.quantile(conf, q)
        # Ensure edges are non-decreasing strictly
        edges = np.unique(edges)
        bins = []
        for i in range(len(edges) - 1):
            low, high = edges[i], edges[i + 1]
            mask = (conf >= low) & (conf <= high) if i == len(edges) - 2 else (conf >= low) & (conf < high)
            if mask.sum() == 0:
                continue
            p_hat = float(correct[mask].mean())
            bins.append({"low": float(low), "high": float(high), "p": p_hat})
        # Enforce monotonic non-decreasing p via cumulative max smoothing
        max_p = 0.0
        for b in bins:
            if b["p"] < max_p:
                b["p"] = max_p
            else:
                max_p = b["p"]
        return {"method": "binning", "bins": bins, "n": int(len(valid))}
    except Exception:
        return {"method": "binning", "bins": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--features-table", default="trading.features_1m")
    ap.add_argument("--horizon-min", type=int, default=5, help="Forward return horizon in minutes (must match OOF generation)")
    ap.add_argument("--out", default=None, help="Output directory for evaluation JSON")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    cli = _ch_client()

    print(f"Fetching OOF predictions for {symbols} from {args.start} to {args.end}...")
    df = _fetch_oof_with_labels(cli, args.features_table, symbols, args.start, args.end, args.horizon_min)

    if df.empty:
        print("ERROR: No data fetched. Check symbols, date range, and OOF table coverage.")
        return

    print(f"Loaded {len(df)} bars with OOF predictions.")

    # Evaluate XGB
    print("\nEvaluating XGB...")
    xgb_metrics = _evaluate_model(df, "xgb_pred", "xgb_conf", "xgb")

    # Evaluate LSTM
    print("Evaluating LSTM...")
    lstm_metrics = _evaluate_model(df, "lstm_pred", "lstm_conf", "lstm")

    # Evaluate Fusion (weighted average 0.6 XGB + 0.4 LSTM)
    print("Evaluating Fusion...")
    df["fusion_pred"] = 0.6 * df["xgb_pred"] + 0.4 * df["lstm_pred"]
    df["fusion_conf"] = 0.6 * df["xgb_conf"] + 0.4 * df["lstm_conf"]
    fusion_metrics = _evaluate_model(df, "fusion_pred", "fusion_conf", "fusion")

    # Aggregate results
    results = {
        "params": {
            "symbols": symbols,
            "start": args.start,
            "end": args.end,
            "horizon_min": args.horizon_min,
            "n_bars": len(df),
        },
        "xgb": xgb_metrics,
        "lstm": lstm_metrics,
        "fusion": fusion_metrics,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ev": fusion_metrics.get("ev"),
        "beta_corr": fusion_metrics.get("beta_corr"),
    }

    # Output aggregate file
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "oof_eval.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

        # Per-symbol output (required by nightly_oof_flow.py)
        print("\nGenerating per-symbol evaluation files...")
        for sym in symbols:
            sym_df = df[df["symbol"] == sym].copy()
            if sym_df.empty:
                print(f"  {sym}: No data, skipping")
                continue

            # Re-evaluate models for this symbol
            sym_xgb = _evaluate_model(sym_df, "xgb_pred", "xgb_conf", "xgb")
            sym_lstm = _evaluate_model(sym_df, "lstm_pred", "lstm_conf", "lstm")
            sym_df["fusion_pred"] = 0.6 * sym_df["xgb_pred"] + 0.4 * sym_df["lstm_pred"]
            sym_df["fusion_conf"] = 0.6 * sym_df["xgb_conf"] + 0.4 * sym_df["lstm_conf"]
            sym_fusion = _evaluate_model(sym_df, "fusion_pred", "fusion_conf", "fusion")

            # Calculate coverage (valid predictions / total bars for this symbol)
            n_bars_sym = len(sym_df)
            n_valid = sym_fusion.get("n", 0)
            coverage = n_valid / n_bars_sym if n_bars_sym > 0 else 0.0

            sym_results = {
                "symbol": sym,
                "params": {
                    "start": args.start,
                    "end": args.end,
                    "horizon_min": args.horizon_min,
                    "n_bars": n_bars_sym,
                },
                "xgb": sym_xgb,
                "lstm": sym_lstm,
                "fusion": sym_fusion,
                "coverage": coverage,  # Add coverage field for nightly_oof_flow.py
                "ev": sym_fusion.get("ev"),
                "p_win_est": sym_fusion.get("p_win_est"),
                "rr_est": sym_fusion.get("rr_est"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            sym_file = out_dir / f"{sym}.json"
            sym_file.write_text(json.dumps(sym_results, indent=2), encoding="utf-8")
            print(f"  {sym}: n={n_bars_sym}, coverage={coverage:.2%}, AUC={sym_fusion.get('auc', 0):.3f}")

            # Build and write confidence calibration for fusion model
            try:
                calib = _build_conf_calibration(sym_df, "fusion_pred", "fusion_conf")
                (out_dir / f"calibration_{sym}.json").write_text(json.dumps(calib, indent=2), encoding="utf-8")
            except Exception as e:
                print(f"  {sym}: calibration build failed: {e}")

    print("\n" + "="*60)
    print("OOF EVALUATION SUMMARY")
    print("="*60)
    print(json.dumps(results, indent=2))
    print("\n" + "="*60)
    print("SUCCESS CRITERIA:")
    print("  AUC ≥ 0.52 (better than random)")
    print("  IC > 0.02 (positive correlation)")
    print("  Top-decile mean return > 0 (high-conf profitable)")
    print("="*60)

    # Check success criteria
    for model in ["xgb", "lstm", "fusion"]:
        metrics = results[model]
        if "error" in metrics:
            print(f"\n{model.upper()}: {metrics['error']}")
            continue

        auc = metrics.get("auc")
        ic = metrics.get("ic")
        top_ret = metrics.get("top_decile", {}).get("mean_fwd_ret_bps")

        checks = []
        if auc is not None:
            checks.append(f"AUC={auc:.3f} {'✓' if auc >= 0.52 else '✗'}")
        if ic is not None:
            checks.append(f"IC={ic:.3f} {'✓' if ic > 0.02 else '✗'}")
        if top_ret is not None:
            checks.append(f"Top-decile={top_ret:.1f}bps {'✓' if top_ret > 0 else '✗'}")

        print(f"\n{model.upper()}: {', '.join(checks)}")


if __name__ == "__main__":
    main()
