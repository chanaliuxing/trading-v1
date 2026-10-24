#!/usr/bin/env python3
"""
Portfolio Hedging Overlay (plan-only, paper-first)

Goal:
- Compute a simple portfolio beta estimate from per-symbol betas (config hedging beta or default 1.0)
- Propose a SPY/QQQ hedge notional to bring beta towards 0 within a tolerance band
- Output a plan artifact for audit and downstream execution

Outputs:
- runs/ai_trader/YYYY-MM-DD/overlay_hedge.json
- runs/ai_trader/YYYY-MM-DD/overlay_events.jsonl (append)

Usage:
  python scripts/ai_trader/portfolio/overlay.py --target 0.0 --tolerance 0.15
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, List
from zoneinfo import ZoneInfo

import yaml


def _load_config(cfg_path: str = "config.yml") -> Dict[str, Any]:
    return yaml.safe_load(Path(cfg_path).read_text("utf-8"))


def _enhanced_extras(cfg: Dict[str, Any]) -> Dict[str, Any]:
    for s in (cfg.get("strategies") or []):
        if str(s.get("id")) == "enhanced_ml_001":
            return (((s.get("params") or {}).get("extras")) or {})
    return {}


def _weights_from_portfolio(runs_dir: str, dstr: str) -> Dict[str, float]:
    p = Path(runs_dir) / dstr / "portfolio_weights.json"
    if not p.exists():
        return {}
    try:
        js = json.loads(p.read_text("utf-8"))
        pm = js.get("params_multiplier") or {}
        base = float(js.get("base_multiplier") or 1.0)
        out = {}
        for s, v in pm.items():
            out[str(s)] = float(v) / base if base else float(v)
        return out
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--runs-dir", default="runs/ai_trader")
    ap.add_argument("--target", type=float, default=0.0)
    ap.add_argument("--tolerance", type=float, default=0.15)
    ap.add_argument("--prefer", default="SPY", help="Preferred hedge instrument")
    ap.add_argument("--capital", type=float, default=100000.0)
    args = ap.parse_args()

    cfg = _load_config(args.config)
    extras = _enhanced_extras(cfg)
    symbols: List[str] = list(extras.get("symbols") or [])
    # Approximate per-symbol weight from prior optimizer (if any), else equal weight
    dstr = date.today().isoformat()
    weights = _weights_from_portfolio(args.runs_dir, dstr)
    if not weights:
        weights = {s: 1.0 / len(symbols) for s in symbols} if symbols else {}

    # Approximate per-symbol beta from hedging cfg per_symbol or default=1.0
    betas: Dict[str, float] = {}
    hcfg = (extras.get("hedging") or {})
    h_per_symbol = hcfg.get("per_symbol") or {}
    for s in symbols:
        b = 1.0
        try:
            if s in h_per_symbol and isinstance(h_per_symbol[s], dict) and "beta" in h_per_symbol[s]:
                b = float(h_per_symbol[s].get("beta", 1.0))
            else:
                b = float(hcfg.get("beta", 1.0))
        except Exception:
            b = 1.0
        betas[s] = b

    # Portfolio beta estimate and hedge suggestion
    port_beta = sum(weights.get(s, 0.0) * betas.get(s, 1.0) for s in symbols)
    target = float(args.target)
    tol = float(args.tolerance)
    need_hedge = abs(port_beta - target) > tol
    hedge = {}
    if need_hedge:
        # Hedge notional relative to capital
        hedge_notional = -(port_beta - target) * float(args.capital)
        hedge = {
            "instrument": args.prefer,
            "target_beta": target,
            "current_beta": port_beta,
            "notional_usd": hedge_notional,
        }

    out_dir = Path(args.runs_dir) / dstr
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "beta": port_beta,
        "target": target,
        "tolerance": tol,
        "need_hedge": need_hedge,
        "hedge": hedge,
        "as_of": datetime.now(ZoneInfo("America/New_York")).isoformat(),
    }
    (out_dir / "overlay_hedge.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    # append event
    try:
        evt = {
            "ts": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "beta": port_beta,
            "need_hedge": need_hedge,
            **({"hedge": hedge} if hedge else {}),
        }
        with (out_dir / "overlay_events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(evt) + "\n")
    except Exception:
        pass
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()

