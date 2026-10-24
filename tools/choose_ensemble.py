#!/usr/bin/env python3
from __future__ import annotations
"""
Choose ensemble weights from backtest compare results and update policy or apply directly.

Inputs: runs/.../compare.json produced by tools/backtest_ensemble_compare.py
Policy: config/ensemble_policies.json (updated if --update-policy)
Apply:  calls tools/apply_ensemble_policy.py if --apply (respects autosettings)
"""
import argparse
import json
from pathlib import Path
from typing import Dict, Any

from tools.ml_policy_store import load_policy


def _load_json(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text("utf-8"))


def _write_json(p: Path, obj: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _best_mode(res: Dict[str, Any]) -> str:
    # Compare by profit_factor then sharpe then total_return
    cand = []
    for k in ("xgb_only", "lstm_only", "fusion"):
        m = res.get(k) or {}
        cand.append((k, float(m.get("profit_factor", 0.0)), float(m.get("sharpe", 0.0)), float(m.get("total_return", 0.0))))
    cand.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)
    return cand[0][0]


def _weights_for(mode: str, res: Dict[str, Any]) -> Dict[str, float]:
    if mode == "xgb_only":
        return {"xgb": 1.0, "lstm": 0.0}
    if mode == "lstm_only":
        return {"xgb": 0.0, "lstm": 1.0}
    # fusion
    w = (res.get("weights") or {})
    wx = float(w.get("xgb", 0.5)); wl = float(w.get("lstm", 0.5))
    s = (wx + wl) or 1.0
    return {"xgb": wx / s, "lstm": wl / s}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", required=True, help="Path to runs/.../compare.json")
    ap.add_argument("--policy", default="config/ensemble_policies.json")
    ap.add_argument("--symbols", default=None, help="Comma-separated symbols to update (default: infer from compare.json)")
    ap.add_argument("--update-policy", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    res = _load_json(Path(args.compare))
    mode = _best_mode(res)
    weights = _weights_for(mode, res)

    # Symbols: use from compare params if not provided
    symbols = []
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    else:
        params = res.get("params") or {}
        symbols = [str(s).upper() for s in (params.get("symbols") or [])]

    print(json.dumps({"mode": mode, "weights": weights, "symbols": symbols}, indent=2))

    if args.update-policy:
        pol = load_policy(args.policy)
        pol.setdefault("defaults", {"mode": "weighted_avg", "weights": {}})
        pol.setdefault("symbols", {})
        for sym in symbols:
            pol["symbols"].setdefault(sym, {})
            pol["symbols"][sym]["weights"] = weights
        _write_json(Path(args.policy), pol)
        print(f"[choose-ensemble] updated {args.policy}")

    if args.apply:
        # Call apply_ensemble_policy.py
        import subprocess, sys
        cmd = [sys.executable, "tools/apply_ensemble_policy.py", "--policy", args.policy]
        if symbols:
            cmd += ["--symbols", ",".join(symbols)]
        if args.force:
            cmd += ["--force"]
        print("[choose-ensemble] applying via:", " ".join(cmd))
        subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()

