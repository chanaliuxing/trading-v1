#!/usr/bin/env python3
"""
Evaluate RL-enabled EnhancedModelSignalStrategy vs baseline on a given period.
Outputs runs/ai_trader/YYYY-MM-DD/rl_eval.json with metrics and gating decision.

Usage:
  python scripts/ai_trader/rl/eval_rl.py --start 2025-07-01 09:30:00 --end 2025-08-31 16:00:00 --symbols TSLA,AAPL,NVDA

Notes:
  - Requires pandas/numpy; RL deps are only required if rl.enabled=true and model loads
  - Uses BacktestEngine with ClickHouse features via adapters (StrategyRegistry)
"""

from __future__ import annotations
import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List
import os
import sys

# Ensure project root is in path for imports
script_dir = Path(__file__).parent
project_root = script_dir.parent.parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd

from backtest_ml_engine import BacktestConfig, BacktestEngine


def _bt_once(symbols: List[str], start: str, end: str, extras: Dict[str, Any]) -> Dict[str, Any]:
    # Build cfg; data will be loaded lazily by adapter via history_features; for now we pass None df
    bt = BacktestConfig(
        symbols=symbols,
        start=start,
        end=end,
        timeframe="1m",
        bars_table="trading.bars_1m_ibkr",
        features_table="trading.features_1m",
        initial_cash=100_000.0,
        commission_per_share=0.0035,
        slippage_bps=1.0,
        strategy_path="strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
        strategy_params=extras,  # Pass extras directly, not nested
    )
    eng = BacktestEngine(bt)
    res = eng.run(df=None)
    return res


def _metrics(res: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(res.get("metrics", {}))
    eq = res.get("equity_curve", [])
    # fallback: derive sortino/omega/calmar
    def rets(eq):
        if not eq:
            return []
        vals = [float(v) for _, v in eq]
        out = []
        for i in range(1, len(vals)):
            prev = vals[i-1] if vals[i-1] != 0 else 1e-9
            out.append((vals[i]-vals[i-1]) / prev)
        return out
    r = rets(eq)
    if r:
        import math
        neg = [x for x in r if x < 0]
        down = math.sqrt(sum(x*x for x in neg)/len(neg)) if neg else 1e-9
        mean_r = sum(r)/len(r)
        m.setdefault("sortino", mean_r/down)
        gains = sum(max(x, 0.0) for x in r)
        losses = sum(max(-x, 0.0) for x in r)
        m.setdefault("omega", gains/(losses if losses > 0 else 1e-9))
        tr = float(m.get("total_return", 0.0))
        mdd = float(m.get("max_drawdown", 0.0))
        m.setdefault("calmar", (tr/mdd) if mdd > 0 else float('inf'))
    return m


def evaluate_rl_model(
    *,
    start: str,
    end: str,
    symbols: List[str],
    model_path: str = "models/rl/sac_position_manager_latest.zip",
    ignore_hours: bool = False,
    min_sharpe: float = 1.0,
    max_mdd: float = 0.20,
    out_dir: str = "runs/ai_trader",
) -> Dict[str, Any]:
    """Import-safe evaluation entrypoint used by tests."""
    model_p = Path(model_path)
    if not model_p.exists():
        raise RuntimeError(f"RL model not found: {model_path}")

    base_extras: Dict[str, Any] = {
        "symbols": symbols,
        "bar_sec": 60,
        "rl": {"enabled": False},
    }
    if ignore_hours:
        base_extras["market_filter"] = {
            "check_after_hours": False,
            "min_volume": 0,
            "conf_floor": 0.0,
            "max_spread_bps": 999,
        }
    res_base = _bt_once(symbols, start, end, base_extras)
    m_base = _metrics(res_base)

    rl_extras: Dict[str, Any] = {
        "symbols": symbols,
        "bar_sec": 60,
        "rl": {
            "enabled": True,
            "model_path": model_path,
            "action_mode": "scale",
            "max_scale_amp": 0.5,
            "safety": {"max_position_pct": 0.25, "max_step_pct": 0.30},
        },
    }
    if ignore_hours:
        rl_extras["market_filter"] = {
            "check_after_hours": False,
            "min_volume": 0,
            "conf_floor": 0.0,
            "max_spread_bps": 999,
        }
    try:
        res_rl = _bt_once(symbols, start, end, rl_extras)
        m_rl = _metrics(res_rl)
    except Exception as e:
        m_rl = {"error": str(e)}

    ok = False
    reasons: List[str] = []
    try:
        sh = float(m_rl.get("sharpe", 0.0))
        mdd = float(m_rl.get("max_drawdown", 1.0))
        if sh >= min_sharpe and mdd <= max_mdd:
            ok = True
        else:
            if sh < min_sharpe:
                reasons.append(f"sharpe<{min_sharpe} (got {sh:.2f})")
            if mdd > max_mdd:
                reasons.append(f"mdd>{max_mdd} (got {mdd:.2f})")
    except Exception:
        reasons.append("metrics_error")

    out = {
        "baseline": m_base,
        "rl": m_rl,
        "gate": {"ok": ok, "reasons": reasons, "min_sharpe": min_sharpe, "max_mdd": max_mdd},
        "metrics": m_rl if isinstance(m_rl, dict) else {},
    }
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "rl_eval.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--symbols", default="TSLA")
    ap.add_argument("--model-path", default="models/rl/sac_position_manager_latest.zip")
    ap.add_argument("--out-dir", default="runs/ai_trader")
    ap.add_argument("--min_sharpe", type=float, default=1.0)
    ap.add_argument("--max_mdd", type=float, default=0.20)
    ap.add_argument("--ignore-hours", action="store_true", help="Disable market hours check for backtesting")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    out = evaluate_rl_model(
        start=args.start,
        end=args.end,
        symbols=symbols,
        model_path=args.model_path,
        ignore_hours=args.ignore_hours,
        min_sharpe=args.min_sharpe,
        max_mdd=args.max_mdd,
        out_dir=str(Path(args.out_dir) / date.today().isoformat()),
    )
    ok = bool(out.get("gate", {}).get("ok", False))
    m_base = out.get("baseline", {})
    m_rl = out.get("rl", {})
    d = Path(args.out_dir) / date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    (d / "rl_eval.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    # MLflow logging (optional)
    try:
        import mlflow
        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "file:./mlruns"))
        mlflow.set_experiment(os.getenv("MLFLOW_EXPERIMENT", "AI Trader RL Eval"))
        with mlflow.start_run(run_name=f"rl_eval_{'_'.join(symbols)}"):
            mlflow.log_params({"symbols": args.symbols, "start": args.start, "end": args.end, "min_sharpe": float(args.min_sharpe), "max_mdd": float(args.max_mdd)})
            mets = {}
            for k, v in (m_base or {}).items():
                if isinstance(v, (int, float)):
                    mets[f"base_{k}"] = float(v)
            for k, v in (m_rl or {}).items():
                if isinstance(v, (int, float)):
                    mets[f"rl_{k}"] = float(v)
            mets["gate_ok"] = 1.0 if ok else 0.0
            mlflow.log_metrics(mets)
            mlflow.log_artifact(str(d / "rl_eval.json"))
    except Exception:
        pass


if __name__ == "__main__":
    main()
