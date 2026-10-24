#!/usr/bin/env python3
"""
Portfolio Optimizer (paper-first)

Goal:
- Compute per-symbol relative sizing scales for Enhanced strategy based on recent performance (Sharpe rank)
- Apply via apply_extras_params (dot-notation): per_symbol.<sym>.sizing.multiplier

Inputs:
- config.yml → strategies.enhanced_ml_001.params.extras.symbols & sizing.multiplier (baseline)
- ClickHouse (optional): ai_trader_memory_episodes via aggregator.top_symbols('intraday')

Outputs:
- runs/ai_trader/YYYY-MM-DD/portfolio_weights.json (weights & scales)
- runs/ai_trader/YYYY-MM-DD/portfolio_apply.json (best_params for apply)

Usage:
  python scripts/ai_trader/portfolio/optimizer.py --apply
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Tuple

import yaml

try:
    from scripts.ai_trader.memory.aggregator import top_symbols as _top_symbols
except Exception:  # pragma: no cover
    _top_symbols = None  # type: ignore


def _load_config(cfg_path: str = "config.yml") -> Dict[str, Any]:
    return yaml.safe_load(Path(cfg_path).read_text("utf-8"))


def _enhanced_extras(cfg: Dict[str, Any]) -> Tuple[List[str], float]:
    syms: List[str] = []
    base_mult = 2.0
    for s in (cfg.get("strategies") or []):
        if str(s.get("id")) == "enhanced_ml_001":
            ex = (((s.get("params") or {}).get("extras")) or {})
            syms = list(ex.get("symbols") or [])
            base_mult = float(((ex.get("sizing") or {}).get("multiplier", 2.0)) or 2.0)
            break
    return syms, base_mult


def _fetch_scores(session: str = "intraday", lookback_days: int = 14) -> Dict[str, float]:
    if _top_symbols is None:
        return {}
    rows = _top_symbols(session, lookback_days=lookback_days, min_trades=5, top_k=100)
    out: Dict[str, float] = {}
    for r in rows or []:
        try:
            sym = str(r.get("symbol"))
            sh = float(r.get("avg_sharpe") or 0.0)
            out[sym] = sh
        except Exception:
            continue
    return out


def _rank_scales(symbols: List[str], scores: Dict[str, float], *, min_scale: float = 0.7, max_scale: float = 1.3) -> Dict[str, float]:
    if not symbols:
        return {}
    # Build list with default score 0 if missing
    pairs = [(s, float(scores.get(s, 0.0))) for s in symbols]
    # Rank by score desc
    pairs.sort(key=lambda x: x[1], reverse=True)
    n = len(pairs)
    if n <= 1:
        return {pairs[0][0]: 1.0} if pairs else {}
    out: Dict[str, float] = {}
    for idx, (sym, _) in enumerate(pairs):
        frac = idx / (n - 1)
        scale = min_scale + (1.0 - frac) * (max_scale - min_scale)  # top gets max_scale
        out[sym] = round(scale, 4)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--session", default="intraday")
    ap.add_argument("--lookback", type=int, default=14)
    ap.add_argument("--min_scale", type=float, default=0.7)
    ap.add_argument("--max_scale", type=float, default=1.3)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = _load_config(args.config)
    symbols, base_mult = _enhanced_extras(cfg)
    scores = _fetch_scores(args.session, args.lookback)
    scales = _rank_scales(symbols, scores, min_scale=args.min_scale, max_scale=args.max_scale)

    dstr = date.today().isoformat()
    runs = Path("runs/ai_trader") / dstr
    runs.mkdir(parents=True, exist_ok=True)

    weights = {
        "symbols": symbols,
        "scores": scores,
        "scales": scales,
        "base_multiplier": base_mult,
        "params_multiplier": {s: round(base_mult * scales.get(s, 1.0), 4) for s in symbols},
    }
    (runs / "portfolio_weights.json").write_text(json.dumps(weights, indent=2), encoding="utf-8")

    best_params: Dict[str, Any] = {}
    for s in symbols:
        best_params[f"per_symbol.{s}.sizing.multiplier"] = weights["params_multiplier"][s]
    (runs / "portfolio_apply.json").write_text(json.dumps(best_params, indent=2), encoding="utf-8")

    if args.apply:
        # Reuse existing writer via Orchestrator commands; here we directly call script to keep this module standalone
        try:
            import subprocess, shlex
            cmd = f"python apply_extras_params.py --config-yaml {args.config} --strategy-id enhanced_ml_001 --hpo-params {str(runs / 'portfolio_apply.json')}"
            subprocess.check_call(shlex.split(cmd))
            print(json.dumps({"status": "ok", "applied": str(runs / 'portfolio_apply.json')}))
        except Exception as e:
            print(json.dumps({"status": "error", "error": str(e)}))
    else:
        print(json.dumps({"status": "ok", "weights_file": str(runs / 'portfolio_weights.json')}))


if __name__ == "__main__":
    main()

