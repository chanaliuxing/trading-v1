#!/usr/bin/env python3
from __future__ import annotations
"""
Apply HPO suggestions to per-symbol strategy configs with audit/backup and governance.

Usage:
  python tools/apply_hpo_suggestions.py --symbols TSLA,AAPL --runs-dir runs/ai_trader --force

Notes:
  - Reads runs/{day}/hpo_suggestions/{SYM}.json (structure: flat or nested dict)
  - Merges into strategies/configs/enhanced_ml_{SYM}.json (backup copies to strategies/configs/backups/)
  - Respects policies.autosettings (config.yml): enabled + (not require_approval or --force)
"""
import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict

import yaml


def _load_yaml(path: str) -> Dict[str, Any]:
    p = Path(path)
    return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}


def _set_dotted(d: Dict[str, Any], dotted: str, value: Any) -> None:
    cur = d
    parts = dotted.split('.')
    for k in parts[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[parts[-1]] = value


def _merge_suggestion(cfg: Dict[str, Any], sug: Dict[str, Any]) -> Dict[str, Any]:
    # Accept both nested dict and dotted-key dict
    out = dict(cfg or {})
    # Dotted keys
    for k, v in list(sug.items()):
        if isinstance(k, str) and ('.' in k) and not isinstance(v, dict):
            _set_dotted(out, k, v)
    # Nested keys
    def _rec(dst: Dict[str, Any], src: Dict[str, Any]):
        for k, v in src.items():
            if isinstance(v, dict):
                if k not in dst or not isinstance(dst[k], dict):
                    dst[k] = {}
                _rec(dst[k], v)
            else:
                dst[k] = v
    nested = {k: v for k, v in sug.items() if not (isinstance(k, str) and '.' in k)}
    _rec(out, nested)
    # Special handling: ensemble weights suggestions
    try:
        # Accept either flat keys or nested mapping
        wx = sug.get("ensemble_weight_xgb")
        wl = sug.get("ensemble_weight_lstm")
        wmap = sug.get("ensemble", {}).get("weights") if isinstance(sug.get("ensemble"), dict) else None
        if wmap and isinstance(wmap, dict):
            wx = wmap.get("xgb", wx)
            wl = wmap.get("lstm", wl)
        if wx is not None or wl is not None:
            try:
                wxf = float(wx if wx is not None else 0.5)
                wlf = float(wl if wl is not None else 0.5)
                s = (wxf + wlf) if (wxf + wlf) > 0 else 1.0
                wxn, wln = wxf / s, wlf / s
                ens = dict(out.get("ensemble") or {})
                ens["mode"] = ens.get("mode", "weighted_avg")
                ens["models"] = [{"name": "xgb", "weight": wxn}, {"name": "lstm", "weight": wln}]
                out["ensemble"] = ens
            except Exception:
                pass
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="Comma-separated symbols; default: all suggestions in runs dir")
    ap.add_argument("--runs-dir", default="runs/ai_trader")
    ap.add_argument("--force", action="store_true", help="Bypass require_approval guard")
    args = ap.parse_args()

    cfg = _load_yaml("config.yml")
    pol = ((cfg.get("policies") or {}).get("autosettings") or {})
    enabled = bool(pol.get("enabled", False))
    require_approval = bool(pol.get("require_approval", True))
    if not enabled:
        print("[HPO] autosettings disabled; abort")
        return
    if require_approval and not args.force:
        print("[HPO] require_approval=true; pass --force to apply")
        return

    dstr = date.today().isoformat()
    runs = Path(args.runs_dir) / dstr / "hpo_suggestions"
    if not runs.exists():
        print(f"[HPO] no suggestions dir: {runs}")
        return

    symbols = []
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    else:
        for f in runs.glob("*.json"):
            symbols.append(f.stem.upper())
    symbols = sorted(list(set(symbols)))

    backups = Path("strategies/configs/backups")
    backups.mkdir(parents=True, exist_ok=True)

    for sym in symbols:
        sug_path = runs / f"{sym}.json"
        if not sug_path.exists():
            print(f"[HPO] skip {sym}: no suggestion file")
            continue
        try:
            sug = json.loads(sug_path.read_text("utf-8"))
        except Exception as e:
            print(f"[HPO] bad suggestion {sym}: {e}")
            continue
        cfg_path = Path("strategies/configs") / f"enhanced_ml_{sym}.json"
        if not cfg_path.exists():
            print(f"[HPO] skip {sym}: no config {cfg_path}")
            continue
        try:
            cur = json.loads(cfg_path.read_text("utf-8"))
        except Exception as e:
            print(f"[HPO] cannot read {cfg_path}: {e}")
            continue
        merged = _merge_suggestion(cur, sug)
        # backup
        try:
            b = backups / f"{cfg_path.name}.{dstr}"
            b.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        # write
        cfg_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[HPO] applied to {cfg_path}")


if __name__ == "__main__":
    main()
