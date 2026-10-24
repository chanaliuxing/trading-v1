#!/usr/bin/env python3
from __future__ import annotations
"""
Generate XGBoost OOF predictions (walk-forward) and write into ClickHouse trading.oof_xgb_h5.

Best-effort pipeline:
 - Tries to use XGBoost classifier. If xgboost unavailable, falls back to sklearn LogisticRegression.
 - Walk-forward: rolling IS window (is_days) → predict next oos_days into OOF table.
 - Labels (clf): y = 1 if forward_return >= label_bps; y = 0 if <= -label_bps; drop flat if enabled.

Usage:
  python tools/generate_xgb_oof.py \
    --symbols TSLA,AAPL \
    --start 2025-10-01 09:30:00 \
    --end   2025-10-29 16:00:00 \
    --is-days 60 --oos-days 1 --step-days 1 \
    --horizon-min 5 --label-bps 10 --drop-flat --rth-only --exclude-open-min 5 \
    --features-table trading.features_1m

Notes:
  - Uses clickhouse_connect; ClickHouse DSN from scripts/ai_trader/config/ai_trader.yaml if available.
  - Writes into trading.oof_xgb_h5 (schema in db/clickhouse/oof_xgb_schema.sql).
"""
import argparse
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

try:
    # Optional feature-family filter; if unavailable, pipeline behaves as before.
    from tools.feature_family_filter import filter_feature_columns  # type: ignore[import]
except Exception:  # pragma: no cover - best-effort import
    def filter_feature_columns(cols):
        return cols


try:
    import clickhouse_connect
except Exception as e:
    clickhouse_connect = None


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


def _fetch_features(cli, table: str, symbol: str, start: str, end: str,
                    horizon_min: int, label_bps: float, drop_flat: bool,
                    rth_only: bool, exclude_open_min: int, use_enhanced_features: bool = True) -> pd.DataFrame:
    # Phase 1: Fetch base features + compute 50 enhanced features on-the-fly
    cols = [
        "ts", "symbol", "open", "high", "low", "close", "volume", "atr_14",
        "z_price_20", "slope_20", "rsi_14", "vwap",
    ]
    q = f"SELECT ts, symbol, open, high, low, close, volume, atr_14, z_price_20, slope_20, rsi_14, vwap FROM {table} " \
        f"WHERE symbol=%(s)s AND ts >= toDateTime(%(st)s) AND ts <= toDateTime(%(et)s) ORDER BY ts"
    rows = cli.query(q, parameters={"s": symbol, "st": start, "et": end}).result_rows
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)

    # Data cleaning: filter price and volume anomalies
    before_clean = len(df)
    df = df[(df["close"] > 0) & (df["volume"] >= 0)].copy()
    if df.empty:
        print(f"  [WARN] {symbol}: All prices/volumes invalid, skipping")
        return df

    # Filter extreme intra-bar returns
    df["ret_1m"] = df["close"].pct_change()
    df = df[df["ret_1m"].abs() <= float(args.max_abs_ret)].copy()

    # Clean infinities in all numeric columns
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    df[numeric_cols] = df[numeric_cols].replace([float('inf'), float('-inf')], np.nan)

    # Drop rows with NaN in critical columns
    df = df.dropna(subset=["close", "volume"]).copy()

    after_clean = len(df)
    if before_clean > after_clean:
        print(f"  [INFO] {symbol}: Dropped {before_clean - after_clean} rows (data cleaning)")

    if df.empty:
        print(f"  [WARN] {symbol}: No rows left after cleaning")
        return df

    # Phase 1: Compute enhanced features if enabled
    if use_enhanced_features:
        try:
            from libs.features.feature_engineer_v2 import FeatureEngineerV2

            # Fetch SPY data for relative features (if not SPY itself)
            spy_df = None
            if symbol != 'SPY':
                q_spy = f"SELECT ts, symbol, open, high, low, close, volume, atr_14, z_price_20, slope_20, rsi_14, vwap FROM {table} " \
                        f"WHERE symbol='SPY' AND ts >= toDateTime(%(st)s) AND ts <= toDateTime(%(et)s) ORDER BY ts"
                rows_spy = cli.query(q_spy, parameters={"st": start, "et": end}).result_rows
                if rows_spy:
                    spy_df = pd.DataFrame(rows_spy, columns=cols)
                    spy_df["ts"] = pd.to_datetime(spy_df["ts"], utc=True)
                    spy_df = spy_df.set_index("ts")

            # Compute all 50 enhanced features
            df_with_ts = df.set_index("ts")
            eng = FeatureEngineerV2()
            df_enhanced = eng.compute_all_features(df_with_ts, spy_df)
            df = df_enhanced.reset_index()
            print(f"  [Phase 1] Computed {len(df.columns)} total features (50 enhanced)")
        except Exception as e:
            print(f"  [Phase 1] Enhanced features failed: {e}, using base features only")
            pass

    # Compute forward returns at specified horizon
    df["fwd_ret"] = df["close"].pct_change(periods=horizon_min).shift(-horizon_min)
    df["fwd_ret_bps"] = df["fwd_ret"] * 10000.0  # Convert to bps

    # Relabeling logic: y=1 if fwd_ret >= label_bps, y=0 if <= -label_bps
    df["y"] = np.where(
        df["fwd_ret_bps"] >= label_bps, 1.0,
        np.where(df["fwd_ret_bps"] <= -label_bps, 0.0, np.nan)
    )

    # RTH-only filter (9:30-16:00 ET)
    if rth_only:
        df["hour"] = df["ts"].dt.tz_convert("America/New_York").dt.hour
        df["minute"] = df["ts"].dt.tz_convert("America/New_York").dt.minute
        # Market hours: 9:30-16:00 = hour in [9,10,...,15] or (hour==9 and minute>=30)
        rth_mask = ((df["hour"] >= 10) & (df["hour"] < 16)) | ((df["hour"] == 9) & (df["minute"] >= 30))
        df = df[rth_mask].copy()

    # Exclude first N minutes after open (9:30)
    if exclude_open_min > 0:
        df["time_et"] = df["ts"].dt.tz_convert("America/New_York")
        df["minutes_from_open"] = (df["time_et"].dt.hour - 9) * 60 + df["time_et"].dt.minute - 30
        df = df[df["minutes_from_open"] >= exclude_open_min].copy()

    # Drop flat bars (abs(fwd_ret_bps) < label_bps)
    if drop_flat:
        df = df[df["y"].notna()].copy()

    # Fill missing features (forward-fill then backfill)
    for c in ["atr_14","z_price_20","slope_20","rsi_14","vwap","volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").ffill().fillna(0.0)

    # Drop rows with missing labels for training
    df = df.dropna(subset=["y"])

    return df


def _wf_splits(start_dt: datetime, end_dt: datetime, is_days: int, oos_days: int, step_days: int) -> List[Tuple[datetime, datetime, datetime, datetime]]:
    """
    Generate walk-forward splits where:
    - OOS period: start_dt to end_dt (the prediction window)
    - IS period: For each OOS window, use prior is_days as training data

    For example, if start_dt=Oct 1, end_dt=Oct 29, is_days=60, oos_days=1:
    - Window 1: IS=Aug 2 to Oct 1, OOS=Oct 1 to Oct 2
    - Window 2: IS=Aug 3 to Oct 2, OOS=Oct 2 to Oct 3
    - etc.
    """
    out = []
    oos_cur = start_dt
    while oos_cur < end_dt:
        oos_start = oos_cur
        oos_end = min(oos_cur + timedelta(days=oos_days), end_dt)
        # IS window: prior is_days before oos_start
        is_end = oos_start
        is_start = is_end - timedelta(days=is_days)
        out.append((is_start, is_end, oos_start, oos_end))
        oos_cur = oos_cur + timedelta(days=step_days)
    return out


def _train_xgb_predict(df: pd.DataFrame, is_mask: np.ndarray, oos_mask: np.ndarray) -> Tuple[pd.Series, pd.Series]:
    # Phase 1: Use ALL available features (base + enhanced)
    # Exclude non-feature columns
    exclude_cols = {"ts", "symbol", "y", "fwd_ret", "fwd_ret_bps", "hour", "minute", "time_et", "minutes_from_open"}
    feats = [c for c in df.columns if c not in exclude_cols and pd.api.types.is_numeric_dtype(df[c])]

    if not feats:
        # Fallback to base features only
        feats = [c for c in df.columns if c in ("close","volume","atr_14","z_price_20","slope_20","rsi_14","vwap")]
    if not feats:
        feats = ["close","volume","atr_14"]

    # Apply high-level feature family filter (price/volume/volatility/time, etc.)
    # This respects config/feature_families.json and feature_registry.json.
    feats = filter_feature_columns(feats)

    print(f"    Training with {len(feats)} features")

    X_is = df.loc[is_mask, feats].astype(float).fillna(0.0)
    y_is = df.loc[is_mask, "y"].astype(float).values
    X_oos = df.loc[oos_mask, feats].astype(float).fillna(0.0)

    if len(X_is) < 50 or len(X_oos) == 0:
        # Insufficient data - return neutral predictions
        prob = pd.Series(0.5, index=df.index[oos_mask])
        conf = pd.Series(0.0, index=df.index[oos_mask])
        return prob, conf

    # Guard against degenerate label distributions (all 0 or all 1), which
    # tend to produce meaningless IC and unstable models.
    try:
        y_non_nan = y_is[~np.isnan(y_is)]
        unique_labels = np.unique(y_non_nan)
        if unique_labels.size < 2:
            print(f\"    [WARN] Degenerate labels in IS window: labels={unique_labels.tolist()}\")
            prob = pd.Series(0.5, index=df.index[oos_mask])
            conf = pd.Series(0.0, index=df.index[oos_mask])
            return prob, conf
    except Exception:
        pass

    try:
        # Try XGBoost
        import xgboost as xgb
        model = xgb.XGBClassifier(
            n_estimators=50,
            max_depth=3,
            learning_rate=0.1,
            random_state=42,
            eval_metric='logloss'
        )
        model.fit(X_is, y_is)
        # predict_proba returns [P(0), P(1)]
        prob_arr = model.predict_proba(X_oos)[:, 1]  # P(y=1)
        prob = pd.Series(prob_arr, index=X_oos.index)
        conf = (np.abs(prob - 0.5) * 2.0).clip(0.0, 1.0)
        return prob, conf
    except Exception:
        # Fallback to LogisticRegression
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler

            scaler = StandardScaler()
            X_is_scaled = scaler.fit_transform(X_is)
            X_oos_scaled = scaler.transform(X_oos)

            model = LogisticRegression(random_state=42, max_iter=200)
            model.fit(X_is_scaled, y_is)
            prob_arr = model.predict_proba(X_oos_scaled)[:, 1]
            prob = pd.Series(prob_arr, index=X_oos.index)
            conf = (np.abs(prob - 0.5) * 2.0).clip(0.0, 1.0)
            return prob, conf
        except Exception:
            # Ultimate fallback - random baseline
            prob = pd.Series(0.5, index=df.index[oos_mask])
            conf = pd.Series(0.0, index=df.index[oos_mask])
            return prob, conf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--is-days", type=int, default=60, help="In-sample training window (days)")
    ap.add_argument("--oos-days", type=int, default=1)
    ap.add_argument("--step-days", type=int, default=1)
    ap.add_argument("--features-table", default="trading.features_1m")
    ap.add_argument("--max-abs-ret", type=float, default=0.2, help="Filter extreme 1m returns by absolute value (e.g., 0.2 = 20%)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--policy-file", default=None, help="Optional JSON policy file (config/oof_policies.json)")
    # Relabeling flags
    ap.add_argument("--horizon-min", type=int, default=5, help="Forward return horizon in minutes")
    ap.add_argument("--label-bps", type=float, default=10.0, help="Label threshold in basis points")
    ap.add_argument("--drop-flat", action="store_true", help="Drop bars with abs(forward_ret) < label_bps")
    ap.add_argument("--rth-only", action="store_true", help="Regular trading hours only (9:30-16:00 ET)")
    ap.add_argument("--exclude-open-min", type=int, default=5, help="Exclude first N minutes after open")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    cli = _ch_client()
    run_id = args.run_id or f"xgb_oof_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    model_ver = "xgb_v1"

    start_dt = datetime.fromisoformat(args.start.replace('T',' ')).astimezone(timezone.utc)
    end_dt = datetime.fromisoformat(args.end.replace('T',' ')).astimezone(timezone.utc)

    # Expand data fetch window to include IS lookback period
    fetch_start_dt = start_dt - timedelta(days=args.is_days)
    fetch_start_str = fetch_start_dt.strftime('%Y-%m-%d %H:%M:%S')

    # Load optional policy
    policy = {}
    if args.policy_file:
        try:
            from tools.ml_policy_store import load_policy, get_oof_params
            policy = load_policy(args.policy_file)
        except Exception:
            policy = {}

    for sym in symbols:
        print(f"Processing {sym}...")
        # Resolve per-symbol params from policy (defaults to CLI args)
        p = {}
        try:
            if policy:
                from tools.ml_policy_store import get_oof_params
                p = get_oof_params(policy, "xgb", sym)
        except Exception:
            p = {}
        is_days = int(p.get("is_days", args.is_days))
        oos_days = int(p.get("oos_days", args.oos_days))
        step_days = int(p.get("step_days", args.step_days))
        horizon_min = int(p.get("horizon_min", args.horizon_min))
        label_bps = float(p.get("label_bps", args.label_bps))
        drop_flat = bool(p.get("drop_flat", args.drop_flat))
        rth_only = bool(p.get("rth_only", args.rth_only))
        exclude_open_min = int(p.get("exclude_open_min", args.exclude_open_min))
        xgb_params = dict(p.get("xgb") or {})

        print(f"  Fetch window: {fetch_start_str} to {args.end} (includes {is_days}-day IS lookback)")
        df = _fetch_features(
            cli, args.features_table, sym, fetch_start_str, args.end,
            horizon_min=horizon_min,
            label_bps=label_bps,
            drop_flat=drop_flat,
            rth_only=rth_only,
            exclude_open_min=exclude_open_min
        )
        if df.empty:
            print(f"  No data for {sym}")
            continue

        # Quick label diagnostics to detect degenerate tasks (helps explain IC/no_signal issues)
        try:
            y_all = df["y"].dropna()
            pos = int((y_all == 1.0).sum())
            neg = int((y_all == 0.0).sum())
            total = pos + neg
            pos_rate = (pos / total) if total > 0 else 0.0
            print(f"  [LABEL] {sym}: total={total}, pos={pos}, neg={neg}, pos_rate={pos_rate:.3f}")
        except Exception:
            pass

        splits = _wf_splits(start_dt, end_dt, is_days, oos_days, step_days)
        print(f"  Generated {len(splits)} walk-forward windows")
        out_rows: List[Tuple] = []

        for i, (is_s, is_e, oos_s, oos_e) in enumerate(splits):
            is_mask = (df["ts"] >= is_s) & (df["ts"] < is_e)
            oos_mask = (df["ts"] >= oos_s) & (df["ts"] < oos_e)

            if i == 0 or i == len(splits) - 1 or i % 5 == 0:
                print(f"    Window {i+1}/{len(splits)}: IS={is_s.date()} to {is_e.date()} ({is_mask.sum()} bars), OOS={oos_s.date()} to {oos_e.date()} ({oos_mask.sum()} bars)")

            if is_mask.sum() < 50 or oos_mask.sum() == 0:
                if i == 0:
                    print(f"      Skipped (IS={is_mask.sum()} < 50 or OOS={oos_mask.sum()} == 0)")
                continue

            # Train with optional tuned XGB hyperparams (from policy)
            prob, conf = _train_xgb_predict(df, is_mask.values, oos_mask.values)
            # If xgboost is available, the helper used static defaults. We can optionally re-train quickly with policy params here.
            try:
                import xgboost as xgb
                # Phase 1: Use all features
                exclude_cols = {"ts", "symbol", "y", "fwd_ret", "fwd_ret_bps", "hour", "minute", "time_et", "minutes_from_open"}
                X_cols = [c for c in df.columns if c not in exclude_cols and pd.api.types.is_numeric_dtype(df[c])]
                if not X_cols:
                    X_cols = [c for c in df.columns if c in ("close","volume","atr_14","z_price_20","slope_20","rsi_14","vwap")]
                X_is = df.loc[is_mask.values, X_cols].astype(float).fillna(0.0)
                y_is = df.loc[is_mask.values, "y"].astype(float)
                X_oos = df.loc[oos_mask.values, X_cols].astype(float).fillna(0.0)
                pos = float((y_is == 1.0).sum()); neg = float((y_is == 0.0).sum())
                spw = xgb_params.get("scale_pos_weight", None)
                if str(spw).lower() == "auto" and pos > 0:
                    spw = min(20.0, max(1.0, neg / max(pos, 1.0)))
                params = {
                    "n_estimators": int(xgb_params.get("n_estimators", 200)),
                    "max_depth": int(xgb_params.get("max_depth", 3)),
                    "learning_rate": float(xgb_params.get("learning_rate", 0.05)),
                    "min_child_weight": int(xgb_params.get("min_child_weight", 5)),
                    "subsample": float(xgb_params.get("subsample", 0.7)),
                    "colsample_bytree": float(xgb_params.get("colsample_bytree", 0.7)),
                    "reg_lambda": float(xgb_params.get("reg_lambda", 1.0)),
                    "random_state": 42,
                    "objective": "binary:logistic",
                    "eval_metric": "logloss",
                }
                model = xgb.XGBClassifier(**params)
                if pos > 0 and neg > 0:
                    model.set_params(scale_pos_weight=spw if spw is not None else 1.0)
                model.fit(X_is, y_is)
                prob_arr = model.predict_proba(X_oos)[:, 1]
                prob = pd.Series(prob_arr, index=X_oos.index)
                conf = (np.abs(prob - 0.5) * 2.0).clip(0.0, 1.0)
            except Exception:
                pass

            for idx, p in prob.items():
                c = float(conf.loc[idx]) if idx in conf.index else 0.0
                ts_val = df.loc[idx, "ts"]
                ts_dt = pd.to_datetime(ts_val, utc=True).to_pydatetime()
                out_rows.append((ts_dt, sym, float(p), c, run_id, model_ver, datetime.now(timezone.utc)))

        if out_rows:
            cli.insert(
                table='trading.oof_xgb_h5',
                column_names=["ts","symbol","pred","conf","run_id","model_ver","updated_at"],
                data=out_rows,
            )
            print(f"  Wrote {len(out_rows)} predictions for {sym}")
        else:
            print(f"  No predictions generated for {sym}")


if __name__ == "__main__":
    main()
