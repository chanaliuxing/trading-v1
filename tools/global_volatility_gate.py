#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global Volatility Gate (MVP)

When VIX or index 1m absolute return exceeds thresholds, apply conservative
adaptive rules to ALL enabled strategy symbols (hot-reloadable).

Defaults:
- vix_gt: 24 -> conf_floor=0.7, sizer_scale=0.7
- idx_abs_ret_1m_gt: 1.0% -> exec_mode=TWAP, exec_horizon_s=180

Safety:
- Dry-run by default
- Requires per-symbol JSON autosettings.enabled=true; respects require_approval unless --force
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml
from libs.db.clickhouse import ClickHouseDB


def _load_cfg() -> Dict[str, Any]:
    for fname in ("config.yml", "config.yaml", "config.example.yml"):
        p = Path(fname)
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}
    return {}


def _enabled_symbols(cfg: Dict[str, Any]) -> List[str]:
    out = []
    for s in (cfg.get("strategies") or []):
        if s and s.get("enabled"):
            ex = ((s.get("params") or {}).get("extras")) or {}
            syms = ex.get("symbols") or ([] if not ex.get("symbol") else [ex.get("symbol")])
            out.extend([str(x).upper() for x in syms])
    return sorted(list(set(out)))


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def _merge_rules(config: Dict[str, Any], rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    c = dict(config or {})
    ad = dict(c.get("adaptive") or {})
    cur = list(ad.get("rules") or [])
    # dedup
    def _k(r):
        try:
            return json.dumps({"when": r.get("when", {}), "overrides": r.get("overrides", {})}, sort_keys=True)
        except Exception:
            return str(r)
    idx = {_k(r): r for r in cur}
    for r in rules:
        idx.setdefault(_k(r), r)
    ad["enabled"] = True if ad.get("enabled") is not False else False
    ad["rules"] = list(idx.values())
    c["adaptive"] = ad
    return c


def main():
    ap = argparse.ArgumentParser(description="Global volatility gate")
    ap.add_argument("--vix-th", type=float, default=24.0)
    ap.add_argument("--idx-th", type=float, default=1.0, help="abs 1m %")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--config-dir", default="strategies/configs")
    ap.add_argument("--prefix", default="enhanced_ml")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = _load_cfg()
    symbols = args.symbols or _enabled_symbols(cfg)
    if not symbols:
        print(json.dumps({"ok": False, "error": "no symbols"}))
        return

    ch = ClickHouseDB()
    vix_val = float(((cfg.get("vix") or {}).get("fallback") or 18.0))
    try:
        # If you have a VIX ticks table, query it; fallback to config
        pass
    except Exception:
        pass
    idx_abs = None
    try:
        row = ch.query("""
            SELECT abs(ret_1) AS a FROM trading.features_1m
            WHERE symbol IN ('SPY','QQQ')
            ORDER BY ts DESC LIMIT 1
        """)
        if row:
            idx_abs = float(row[0].get('a') or 0.0) * 100.0  # %
    except Exception:
        idx_abs = None

    triggered = []
    rules: List[Dict[str, Any]] = []
    if vix_val >= args.vix_th:
        rules.append({"when": {"vix_gt": args.vix_th}, "overrides": {"conf_floor": 0.7, "sizer_scale": 0.7}})
        triggered.append("vix")
    if idx_abs is not None and idx_abs >= args.idx_th:
        rules.append({"when": {"spy_abs_ret_1m_gt": args.idx_th}, "overrides": {"exec_mode": "TWAP", "exec_horizon_s": 180}})
        triggered.append("idx")

    out = {"ok": True, "vix": vix_val, "idx_abs_1m": idx_abs, "triggered": triggered, "applied": []}
    if rules and args.apply:
        day = datetime.now().date().isoformat()
        audit_dir = Path("runs/ai_trader") / day / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        for sym in symbols:
            path = Path(args.config_dir) / f"{args.prefix}_{sym}.json"
            base = _read_json(path)
            autos = (base.get("autosettings") or {})
            enabled = bool(autos.get("enabled", False))
            req_appr = bool(autos.get("require_approval", True))
            if not enabled:
                continue
            if req_appr and not args.force:
                continue
            if path.exists():
                backup = path.with_suffix(path.suffix + ".backup_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
                path.rename(backup)
            merged = _merge_rules(base, rules)
            _write_json(path, merged)
            _write_json(audit_dir / f"global_gate_{sym}.json", {"symbol": sym, "rules": rules})
            out["applied"].append(sym)

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

