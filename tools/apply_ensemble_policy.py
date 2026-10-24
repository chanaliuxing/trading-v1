#!/usr/bin/env python3
from __future__ import annotations
"""
Apply ensemble weights from config/ensemble_policies.json to per-symbol strategy configs.

Respects policies.autosettings (config.yml). Use --force to bypass require_approval.
"""
import argparse
import json
from pathlib import Path
from typing import Any, Dict
import yaml

from tools.ml_policy_store import load_policy, get_ensemble


def _load_yaml(path: str) -> Dict[str, Any]:
    p = Path(path)
    return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="config/ensemble_policies.json")
    ap.add_argument("--config-dir", default="strategies/configs")
    ap.add_argument("--prefix", default="enhanced_ml")
    ap.add_argument("--symbols", default=None, help="Comma-separated symbols; default: infer from files")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = _load_yaml("config.yml")
    autos = ((cfg.get("policies") or {}).get("autosettings") or {})
    if not bool(autos.get("enabled", False)):
        print("[apply-ensemble] autosettings disabled; abort")
        return
    if bool(autos.get("require_approval", True)) and not args.force:
        print("[apply-ensemble] require_approval=true; pass --force to apply")
        return

    pol = load_policy(args.policy)
    symbols = []
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    else:
        for f in Path(args.config_dir).glob(f"{args.prefix}_*.json"):
            sym = f.stem.split("_")[-1].upper()
            symbols.append(sym)
        symbols = sorted(list(set(symbols)))

    for sym in symbols:
        cfg_path = Path(args.config_dir) / f"{args.prefix}_{sym}.json"
        if not cfg_path.exists():
            print(f"[apply-ensemble] skip {sym}: {cfg_path} not found")
            continue
        try:
            cur = json.loads(cfg_path.read_text("utf-8"))
        except Exception as e:
            print(f"[apply-ensemble] cannot read {cfg_path}: {e}")
            continue
        ens = get_ensemble(pol, sym)
        weights = ens.get("weights", {})
        mode = ens.get("mode", "weighted_avg")
        if not weights:
            print(f"[apply-ensemble] no weights for {sym}; skip")
            continue
        models = [{"name": k, "weight": float(v)} for k, v in weights.items()]
        cur.setdefault("ensemble", {})
        cur["ensemble"]["mode"] = mode
        cur["ensemble"]["models"] = models
        # backup and write
        try:
            bdir = Path(args.config_dir) / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / f"{cfg_path.name}.bak").write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        _write_json(cfg_path, cur)
        print(f"[apply-ensemble] applied weights to {cfg_path}")


if __name__ == "__main__":
    main()

