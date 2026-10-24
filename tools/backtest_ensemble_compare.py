#!/usr/bin/env python3
from __future__ import annotations
"""
Backtest three ensemble modes for EnhancedModelSignalStrategy:
  1) XGB-only (xgb_signal/xgb_conf)
  2) LSTM-only (lstm_signal/lstm_conf)
  3) Fusion (weighted_avg, default 0.6/0.4)

Requirements:
  - trading.features_1m_with_oof view with lstm_oof_h5/lstm_oof_conf_h5
  - Adapter will expose lstm_signal/lstm_conf and xgb_signal/xgb_conf

Usage:
  python tools/backtest_ensemble_compare.py \
    --symbols TSLA,AAPL \
    --start '2025-07-01 09:30:00' \
    --end   '2025-08-31 16:00:00' \
    --features-table trading.features_1m_with_oof \
    --out runs/ai_trader/compare
"""
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import sys
from pathlib import Path as _P
# Ensure project root on sys.path for local module imports
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
from backtest_ml_engine import BacktestConfig, BacktestEngine


def _bt(symbols: List[str], start: str, end: str, extras: Dict[str, Any], features_table: str) -> Dict[str, Any]:
    cfg = BacktestConfig(
        symbols=symbols,
        start=start,
        end=end,
        timeframe="1m",
        features_table=features_table,
        initial_cash=100_000.0,
        commission_per_share=0.0035,
        slippage_bps=1.0,
        strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
        strategy_params=extras,
    )
    eng = BacktestEngine(cfg)
    res = eng.run(df=None)
    return res


def _metrics(res: Dict[str, Any]) -> Dict[str, Any]:
    return dict(res.get("metrics", {}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--features-table", default="trading.features_1m_with_oof")
    ap.add_argument("--out", default=None)
    ap.add_argument("--w_xgb", type=float, default=0.6)
    ap.add_argument("--w_lstm", type=float, default=0.4)
    ap.add_argument("--policy-file", default=None, help="Optional policy JSON for gating/ensemble (config/gating_policies.json, config/ensemble_policies.json)")
    ap.add_argument("--conf-floor", type=float, default=0.0, help="Minimum confidence threshold")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]

    # Base extras with per-symbol overrides to ensure backtest settings truly take effect
    base = {
        "symbols": symbols,
        "bar_sec": 60,
        # Backtest-friendly defaults to avoid gating/fill issues
        "market_filter": {
            "conf_floor": args.conf_floor,
            "check_after_hours": False,
            "min_volume": 0,  # Disable volume filter for backtest
            "max_spread_bps": 999  # Disable spread filter
        },
        # Ensure file JSON cannot override conf gate during compare
        "force_conf_floor": args.conf_floor,
        "order_type": "MKT",
        "limit_offset_bps": 0.0,
        "sizing": {"mode": "fixed", "base_qty": 100, "max_qty": 1000, "lot": 100},
        # Per-symbol overrides to ensure backtest settings override production configs
        "per_symbol": {
            sym: {
                "market_filter": {
                    "conf_floor": args.conf_floor,
                    "check_after_hours": False,
                    "min_volume": 0,
                    "max_spread_bps": 999
                }
            } for sym in symbols
        }
    }

    # Optional policy-driven gating and ensemble weights
    if args.policy_file:
        try:
            from tools.ml_policy_store import load_policy, get_gating, get_ensemble
            pol = load_policy(args.policy_file)
            # Apply per-symbol gating (conf_floor) if present
            for sym in symbols:
                g = get_gating(pol, sym)
                cf = g.get("conf_floor", None)
                if cf is not None:
                    base["per_symbol"][sym]["market_filter"]["conf_floor"] = float(cf)
                    base["force_conf_floor"] = float(cf)
            # Derive weights (normalize inside helper)
            # If policy provides symbol weights, use first symbol's weights as global for simplicity
            ens = get_ensemble(pol, symbols[0])
            wmap = ens.get("weights", {})
            if wmap:
                wx = float(wmap.get("xgb", 0.0)); wl = float(wmap.get("lstm", 0.0))
                args.w_xgb, args.w_lstm = wx, wl
        except Exception:
            pass

    # Modes
    xgb_extras = dict(base)
    xgb_extras["ensemble"] = {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": 1.0}]}

    lstm_extras = dict(base)
    lstm_extras["ensemble"] = {"mode": "weighted_avg", "models": [{"name": "lstm", "weight": 1.0}]}

    w1, w2 = float(args.w_xgb), float(args.w_lstm)
    s = w1 + w2
    if s <= 0:
        w1, w2 = 0.5, 0.5
    else:
        w1, w2 = w1 / s, w2 / s
    fusion_extras = dict(base)
    fusion_extras["ensemble"] = {"mode": "weighted_avg", "models": [{"name": "xgb", "weight": w1}, {"name": "lstm", "weight": w2}]}

    results = {
        "xgb_only": _metrics(_bt(symbols, args.start, args.end, xgb_extras, args.features_table)),
        "lstm_only": _metrics(_bt(symbols, args.start, args.end, lstm_extras, args.features_table)),
        "fusion": _metrics(_bt(symbols, args.start, args.end, fusion_extras, args.features_table)),
        "weights": {"xgb": w1, "lstm": w2},
        "params": {"start": args.start, "end": args.end, "symbols": symbols},
    }

    out_dir = Path(args.out or (Path("runs/ai_trader") / "ensemble_compare"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compare.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
