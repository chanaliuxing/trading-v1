#!/usr/bin/env python3
from __future__ import annotations
"""
Lightweight ML policy store utilities.

Allows AI brain (or operators) to govern model generation and gating via JSON, without code edits.

Policy file schema examples (kept simple and forward-compatible):

config/oof_policies.json
{
  "defaults": {
    "is_days": 60, "oos_days": 1, "step_days": 1,
    "horizon_min": 5, "label_bps": 10,
    "drop_flat": true, "rth_only": true, "exclude_open_min": 5,
    "min_is_rows": 100,
    "algo": "baseline",
    "xgb": {"n_estimators": 200, "max_depth": 3, "learning_rate": 0.05,
             "min_child_weight": 5, "subsample": 0.7, "colsample_bytree": 0.7,
             "reg_lambda": 1.0, "scale_pos_weight": "auto"}
  },
  "models": {
    "xgb": {"horizon_min": 10},
    "lstm": {"algo": "baseline", "min_is_rows": 50}
  },
  "symbols": {
    "TSLA": {"xgb": {"label_bps": 5}},
    "AAPL": {"lstm": {"exclude_open_min": 10}}
  }
}

config/ensemble_policies.json
{
  "defaults": {"mode": "weighted_avg", "weights": {"xgb": 0.6, "lstm": 0.4}},
  "symbols": {"TSLA": {"weights": {"xgb": 1.0, "lstm": 0.0}}}
}

config/gating_policies.json
{
  "defaults": {"conf_floor": 0.0},
  "symbols": {"TSLA": {"conf_floor": 0.85}, "AAPL": {"conf_floor": 0.7}}
}
"""
from pathlib import Path
from typing import Any, Dict, Optional
import json


def _load_json(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def load_policy(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    return _load_json(path)


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get_oof_params(policy: Dict[str, Any], model: str, symbol: str) -> Dict[str, Any]:
    """Resolve OOF generation parameters from policy JSON.

    Priority: defaults → models[model] → symbols[symbol] → symbols[symbol][model]
    """
    defaults = dict(policy.get("defaults") or {})
    by_model = (policy.get("models") or {}).get(model) or {}
    by_symbol = (policy.get("symbols") or {}).get(symbol.upper()) or {}
    # If symbol-level has per-model sub-map, merge it as well
    by_symbol_model = (by_symbol.get(model) if isinstance(by_symbol, dict) else None) or {}
    # Merge in order
    out = _merge(defaults, by_model)
    out = _merge(out, by_symbol)
    out = _merge(out, by_symbol_model)
    return out


def get_gating(policy: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    d = dict(policy.get("defaults") or {})
    s = (policy.get("symbols") or {}).get(symbol.upper()) or {}
    return _merge(d, s)


def get_ensemble(policy: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    d = dict(policy.get("defaults") or {})
    s = (policy.get("symbols") or {}).get(symbol.upper()) or {}
    # Normalize weights to sum to 1 if present
    w = (s.get("weights") or d.get("weights") or {})
    if isinstance(w, dict) and w:
        total = sum(float(v or 0.0) for v in w.values())
        if total > 0:
            wn = {k: float(v) / total for k, v in w.items()}
            if s.get("weights") is not None:
                s["weights"] = wn
            else:
                d["weights"] = wn
    return _merge(d, s)

