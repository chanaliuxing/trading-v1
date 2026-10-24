#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy Coach (Premarket + Intraday suggestions)

Generates per-symbol adaptive rule suggestions based on simple, auditable
heuristics and optionally applies them (hot-reload via JSON files).

Principles:
- Dry-run by default: prints and writes suggestions to runs/.../suggestions
- Safe apply only if autosettings.enabled and (not require_approval or --force)
- Append rules; do not remove existing ones; keep backups

Heuristics v1:
- High VIX (> 22): raise conf_floor to 0.7, sizer_scale = 0.75
- First 20 minutes: sizer_scale = 0.5 (time_lt 09:50)
- Optional: explicit halt (via --halt SYMBOL --sec N)
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from libs.db.clickhouse import ClickHouseDB


def _load_cfg() -> Dict[str, Any]:
    for fname in ("config.yml", "config.yaml", "config.example.yml"):
        p = Path(fname)
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}
    return {}


def _enabled_symbols(cfg: Dict[str, Any]) -> List[str]:
    syms = []
    for s in (cfg.get("strategies") or []):
        if s and s.get("enabled"):
            extras = ((s.get("params") or {}).get("extras")) or {}
            vals = extras.get("symbols")
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str):
                        syms.append(v.upper())
            val = extras.get("symbol")
            if isinstance(val, str):
                syms.append(val.upper())
    return sorted(list(set(syms)))


def _sector_map(cfg: Dict[str, Any]) -> Dict[str, str]:
    sm: Dict[str, str] = {}
    for s in (cfg.get("strategies") or []):
        if s and s.get("enabled"):
            ex = ((s.get("params") or {}).get("extras")) or {}
            sector_map = ex.get("sector_map") or {}
            if isinstance(sector_map, dict):
                for k, v in sector_map.items():
                    sm[str(k).upper()] = str(v)
    return sm


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def _suggest_rules(symbol: str, vix: float, halt_for: int | None,
                   idx_abs_ret_1m: Optional[float] = None,
                   sym_metrics: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    # First 20 minutes dampen
    rules.append({
        "when": {"time_lt": "09:50"},
        "overrides": {"sizer_scale": 0.5}
    })
    # High VIX gate
    if vix is not None and vix > 22.0:
        rules.append({
            "when": {"vix_gt": 22},
            "overrides": {"conf_floor": 0.7, "sizer_scale": 0.75}
        })
    # Index shock: switch to TWAP/POV for smoother fills
    try:
        if idx_abs_ret_1m is not None and idx_abs_ret_1m >= 0.008:  # 0.8%
            # Prefer TWAP with short horizon for fast drift
            rules.append({
                "when": {"spy_abs_ret_1m_gt": 0.8},
                "overrides": {"exec_mode": "TWAP", "exec_horizon_s": 120}
            })
    except Exception:
        pass
    # Symbol spread/ATR: tighten filter or scale size
    try:
        if sym_metrics:
            spread_bps = float(sym_metrics.get("spread_bps", 0.0) or 0.0)
            atr14 = float(sym_metrics.get("atr_14", 0.0) or 0.0)
            ret1 = float(sym_metrics.get("ret_1", 0.0) or 0.0)
            # If spread wide, raise conf_floor a bit
            if spread_bps >= 12.0:
                rules.append({
                    "when": {"time_ge": "09:30"},
                    "overrides": {"conf_floor": 0.65, "max_spread_bps": 12}
                })
            # If 1m surge, require higher conf for entries
            if abs(ret1) >= 0.01:
                rules.append({
                    "when": {"ret_1m_gt": abs(ret1) * 100.0},
                    "overrides": {"conf_floor": 0.65}
                })
    except Exception:
        pass
    # Optional explicit halt
    if halt_for and halt_for > 0:
        rules.append({
            "when": {"halt": True},
            "overrides": {"halt_for_sec": int(halt_for)}
        })
    return rules


def _merge_adaptive(config: Dict[str, Any], rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    def _key(r: Dict[str, Any]) -> str:
        try:
            return json.dumps({"when": r.get("when", {}), "overrides": r.get("overrides", {})}, sort_keys=True)
        except Exception:
            return str(r)

    c = dict(config or {})
    ad = dict(c.get("adaptive") or {})
    existing = list(ad.get("rules") or [])

    # Deduplicate by normalized when+overrides
    seen = {_key(r): r for r in existing}
    for r in rules:
        k = _key(r)
        if k not in seen:
            seen[k] = r

    merged_rules = list(seen.values())
    ad["enabled"] = True if ad.get("enabled") is not False else False
    ad["rules"] = merged_rules
    c["adaptive"] = ad
    return c


def _diff_rules(before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    def _key(r: Dict[str, Any]) -> str:
        try:
            return json.dumps({"when": r.get("when", {}), "overrides": r.get("overrides", {})}, sort_keys=True)
        except Exception:
            return str(r)
    bset = {_key(r) for r in before or []}
    aset = {_key(r) for r in after or []}
    added_keys = list(aset - bset)
    removed_keys = list(bset - aset)
    # materialize items for report
    def _find_by_key(keys: List[str], all_rules: List[Dict[str, Any]]):
        idx = {_key(r): r for r in all_rules or []}
        return [idx[k] for k in keys if k in idx]
    return _find_by_key(added_keys, after or []), _find_by_key(removed_keys, before or [])


def main():
    ap = argparse.ArgumentParser(description="Strategy Coach - suggest/adapt per-symbol rules")
    ap.add_argument("--symbols", nargs="*", default=None, help="Symbols to process (default: enabled strategies)")
    ap.add_argument("--config-dir", default="strategies/configs", help="Per-symbol config directory")
    ap.add_argument("--prefix", default="enhanced_ml", help="Per-symbol config prefix")
    ap.add_argument("--vix", type=float, default=None, help="Override VIX value (else read from config fallback)")
    ap.add_argument("--halt", nargs="*", default=None, help="Symbols to halt (space-separated)")
    ap.add_argument("--halt-sec", type=int, default=0, help="Halt seconds for --halt symbols")
    ap.add_argument("--apply", action="store_true", help="Apply changes to per-symbol JSON")
    ap.add_argument("--force", action="store_true", help="Ignore require_approval guard")
    ap.add_argument("--report-md", default=None, help="Write consolidated report Markdown")
    ap.add_argument("--stale-sec", type=float, default=180, help="CH freshness threshold (sec)")
    ap.add_argument("--sector-thr-spread", type=float, default=12.0, help="Sector median spread_bps gate")
    ap.add_argument("--sector-thr-ret1", type=float, default=1.0, help="Sector median |ret_1| gate (%)")
    ap.add_argument("--sector-trigger-ratio", type=float, default=0.6, help="Ratio of symbols to trigger sector rule")
    args = ap.parse_args()

    cfg = _load_cfg()
    symbols = args.symbols or _enabled_symbols(cfg)
    sectors = _sector_map(cfg)
    # Fetch index metrics (SPY/QQQ)
    ch = ClickHouseDB()
    idx_abs_ret_1m = None
    idx_abs_ret_5m = None
    idx_abs_ret_15m = None
    try:
        # Pull last 15 1m returns across SPY/QQQ; compute average abs for 1m/5m/15m windows
        rows = ch.query("""
            SELECT symbol, ts, ret_1
            FROM trading.features_1m FINAL
            WHERE symbol IN ('SPY','QQQ')
            ORDER BY ts DESC
            LIMIT 30
        """)
        if rows:
            # Flatten latest ret_1 list
            rets = [abs(float(r.get('ret_1') or 0.0)) for r in rows[:15]]
            if len(rets) > 0:
                idx_abs_ret_1m = rets[0]
                idx_abs_ret_5m = sum(rets[:5]) / min(5, len(rets))
                idx_abs_ret_15m = sum(rets[:15]) / min(15, len(rets))
    except Exception:
        idx_abs_ret_1m = None
        idx_abs_ret_5m = None
        idx_abs_ret_15m = None

    today = datetime.now().date().isoformat()
    suggest_dir = Path("runs/ai_trader") / today / "suggestions"

    vix_val = args.vix
    if vix_val is None:
        # fallback to config default
        vix_val = float(((cfg.get("vix") or {}).get("fallback") or 18.0))

    halted = set([s.upper() for s in (args.halt or [])]) if args.halt else set()

    applied = []
    report_lines = ["# Strategy Coach Suggestions", ""]
    # Preload CH freshness and per-symbol metrics
    sym_last_ok: Dict[str, bool] = {}
    sym_metrics_map: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        try:
            row = ch.query(
                """
                SELECT ts, ret_1, atr_14, spread_bps
                FROM trading.features_1m FINAL
                WHERE symbol = {sym:String}
                ORDER BY ts DESC LIMIT 1
                """,
                {"sym": sym},
            )
            if row:
                sym_metrics_map[sym] = row[0]
                # Freshness check against now (UTC assumed in CH; approximate)
                sym_last_ok[sym] = True
            else:
                sym_metrics_map[sym] = {}
                sym_last_ok[sym] = False
        except Exception:
            sym_metrics_map[sym] = {}
            sym_last_ok[sym] = False

    # Sector-level triggers
    sector_groups: Dict[str, List[str]] = {}
    for sym in symbols:
        sec = sectors.get(sym)
        if sec:
            sector_groups.setdefault(sec, []).append(sym)

    sector_overrides: Dict[str, Dict[str, Any]] = {}
    for sec, syms in sector_groups.items():
        vals_spread = []
        vals_ret1 = []
        ok_count = 0
        for s in syms:
            m = sym_metrics_map.get(s) or {}
            if m:
                ok_count += 1
                try:
                    if m.get("spread_bps") is not None:
                        vals_spread.append(float(m.get("spread_bps")))
                except Exception:
                    pass
                try:
                    if m.get("ret_1") is not None:
                        vals_ret1.append(abs(float(m.get("ret_1"))) * 100.0)  # %
                except Exception:
                    pass
        ratio = (ok_count / max(1, len(syms)))
        med_spread = sorted(vals_spread)[len(vals_spread)//2] if vals_spread else 0.0
        med_ret1 = sorted(vals_ret1)[len(vals_ret1)//2] if vals_ret1 else 0.0
        if ratio >= args.sector_trigger_ratio and (med_spread >= args.sector_thr_spread or med_ret1 >= args.sector_thr_ret1):
            # Apply conservative overrides sector-wide
            sector_overrides[sec] = {"conf_floor": 0.65, "sizer_scale": 0.7}

    for sym in symbols:
        # Per-symbol metrics
        sym_metrics = sym_metrics_map.get(sym)
        # Freshness gate
        if not sym_last_ok.get(sym, False):
            # Report stale symbol and skip applying suggestions for safety
            report_stale = f"(stale CH data)"
        else:
            report_stale = ""

        rules = _suggest_rules(sym, vix_val, args.halt_sec if sym in halted else None,
                               idx_abs_ret_1m=idx_abs_ret_1m, sym_metrics=sym_metrics)
        # Sector overrides
        sec = sectors.get(sym)
        if sec and sec in sector_overrides:
            rules.append({"when": {"time_ge": "09:30"}, "overrides": sector_overrides[sec]})
        out_obj = {"symbol": sym, "vix": vix_val, "rules": rules}
        _write_json(suggest_dir / f"enhanced_ml_{sym}.suggest.json", out_obj)

        # Apply if allowed
        if args.apply and sym_last_ok.get(sym, False):
            path = Path(args.config_dir) / f"{args.prefix}_{sym}.json"
            base = _read_json(path)
            autos = (base.get("autosettings") or {})
            enabled = bool(autos.get("enabled", False))
            req_appr = bool(autos.get("require_approval", True))
            if not enabled:
                continue
            if req_appr and not args.force:
                continue
            # backup
            if path.exists():
                backup = path.with_suffix(path.suffix + ".backup_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
                path.rename(backup)
            before_rules = list(((base.get("adaptive") or {}).get("rules") or []))
            merged = _merge_adaptive(base, rules)
            _write_json(path, merged)
            # Audit diff
            after_rules = list(((merged.get("adaptive") or {}).get("rules") or []))
            added, removed = _diff_rules(before_rules, after_rules)
            audit_dir = Path("runs/ai_trader") / today / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            _write_json(audit_dir / f"apply_{sym}.json", {
                "symbol": sym,
                "before_count": len(before_rules),
                "after_count": len(after_rules),
                "added": added,
                "removed": removed,
            })
            applied.append(sym)

        # Append to report
        report_lines.append(f"## {sym}")
        report_lines.append(f"- VIX: {vix_val}")
        if idx_abs_ret_1m is not None:
            report_lines.append(f"- Index abs ret: 1m={idx_abs_ret_1m:.4f}  5m={idx_abs_ret_5m if idx_abs_ret_5m is not None else '-'}  15m={idx_abs_ret_15m if idx_abs_ret_15m is not None else '-'}")
        if sym_metrics:
            report_lines.append(f"- ret_1: {sym_metrics.get('ret_1')}  atr_14: {sym_metrics.get('atr_14')}  spread_bps: {sym_metrics.get('spread_bps')} {report_stale}")
        report_lines.append("- Suggested rules:")
        for r in rules:
            report_lines.append(f"  - when={json.dumps(r.get('when'))} overrides={json.dumps(r.get('overrides'))}")
        # Snapshot current key config values for context
        base_cfg_path = Path(args.config_dir) / f"{args.prefix}_{sym}.json"
        base_cfg = _read_json(base_cfg_path)
        mf = (base_cfg.get("market_filter") or {})
        sz = (base_cfg.get("sizing") or {})
        rk = (base_cfg.get("risk") or {})
        report_lines.append(f"- Current config: conf_floor={mf.get('conf_floor','-')}  max_spread_bps={mf.get('max_spread_bps','-')}  sizing.mode={sz.get('mode','-')}  base_qty={sz.get('base_qty', sz.get('fixed',{}).get('base_qty','-'))}  risk(tp/sl/ts)={rk.get('take_profit_pct','-')}/{rk.get('stop_loss_pct','-')}/{rk.get('time_stop_min','-')}")
        report_lines.append("")

    if args.report_md:
        Path(args.report_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_md).write_text("\n".join(report_lines))

    print(json.dumps({
        "ok": True,
        "symbols": symbols,
        "applied": applied,
        "suggest_dir": str(suggest_dir)
    }, indent=2))


if __name__ == "__main__":
    main()
