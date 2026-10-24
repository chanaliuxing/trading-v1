from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, Any, List, Tuple

try:  # best-effort ClickHouse lookup for sector names
    from libs.db.clickhouse import ClickHouseDB  # type: ignore
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore


def _resolve_sectors(symbols: List[str]) -> Dict[str, str]:
    """Best-effort map from symbol to sector via ClickHouse.

    Falls back to empty mapping if ClickHouse is unavailable or errors.
    """
    out: Dict[str, str] = {}
    if not symbols or ClickHouseDB is None:
        return out
    try:
        ch = ClickHouseDB()
    except Exception:
        return out
    for s in symbols:
        try:
            rows = ch.query("SELECT sector FROM trading.dim_symbol WHERE symbol={s:String} LIMIT 1", {"s": s})
            if rows:
                sec = rows[0].get("sector") or ""
                if sec:
                    out[s] = sec
        except Exception:
            continue
    return out


def _hedging_summary(out_dir: Path) -> Dict[str, Any]:
    """Aggregate hedging events into totals, per-symbol and per-sector distributions."""
    hev = out_dir / "hedging_events.jsonl"
    cnt = 0
    notional = 0.0
    by_symbol: Dict[str, Dict[str, float]] = {}
    symbols_seen: Dict[str, bool] = {}
    if not hev.exists():
        return {"count": 0, "notional_usd": 0.0, "per_symbol": [], "per_sector": []}
    try:
        with hev.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # event fields: symbol (primary), hedge_symbol, hedge_qty, ref_px, beta, conf, qty
                sym = str(obj.get("symbol") or "")
                try:
                    ev_notional = abs(float(obj.get("hedge_qty", 0.0)) * float(obj.get("ref_px", 0.0)))
                except Exception:
                    ev_notional = 0.0
                cnt += 1
                notional += ev_notional
                if sym:
                    symbols_seen[sym] = True
                    st = by_symbol.setdefault(sym, {"events": 0.0, "notional": 0.0})
                    st["events"] += 1.0
                    st["notional"] += ev_notional
    except Exception:
        # Return minimal aggregation on failure
        return {"count": cnt, "notional_usd": notional, "per_symbol": [], "per_sector": []}

    # Per-symbol list (sorted by notional desc)
    per_symbol: List[Dict[str, Any]] = []
    for s, agg in by_symbol.items():
        per_symbol.append({
            "symbol": s,
            "events": int(agg.get("events", 0.0)),
            "notional_usd": float(agg.get("notional", 0.0)),
        })
    per_symbol.sort(key=lambda x: x.get("notional_usd", 0.0), reverse=True)

    # Per-sector aggregation (best-effort via ClickHouse mapping)
    sector_of = _resolve_sectors(list(symbols_seen.keys())) if symbols_seen else {}
    by_sector: Dict[str, Dict[str, float]] = {}
    for s, agg in by_symbol.items():
        sec = sector_of.get(s, "Unknown") or "Unknown"
        st = by_sector.setdefault(sec, {"events": 0.0, "notional": 0.0})
        st["events"] += float(agg.get("events", 0.0))
        st["notional"] += float(agg.get("notional", 0.0))
    per_sector: List[Dict[str, Any]] = []
    for sec, agg in by_sector.items():
        per_sector.append({
            "sector": sec,
            "events": int(agg.get("events", 0.0)),
            "notional_usd": float(agg.get("notional", 0.0)),
        })
    per_sector.sort(key=lambda x: x.get("notional_usd", 0.0), reverse=True)

    return {
        "count": cnt,
        "notional_usd": notional,
        "per_symbol": per_symbol,
        "per_sector": per_sector,
    }


def _guardian_summary(out_dir: Path) -> Dict[str, Any]:
    ge = out_dir / "guardian_events.jsonl"
    cnt = 0
    max_dd = 0.0
    last = None
    if not ge.exists():
        return {"count": 0, "max_dd_pct": 0.0, "last": None}
    try:
        with ge.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                cnt += 1
                try:
                    dd = float(obj.get("dd_pct", 0.0) or 0.0)
                    if dd > max_dd:
                        max_dd = dd
                except Exception:
                    pass
                last = obj
    except Exception:
        pass
    last_brief = None
    if isinstance(last, dict):
        try:
            last_brief = {"action": last.get("action"), "reason": last.get("reason"), "dd_pct": float(last.get("dd_pct", 0.0) or 0.0)}
        except Exception:
            last_brief = {"action": last.get("action"), "reason": last.get("reason")}
    return {"count": cnt, "max_dd_pct": max_dd, "last": last_brief}


def _dq_summary(out_dir: Path) -> Dict[str, Any]:
    dq = out_dir / "data_quality.json"
    if not dq.exists():
        return {"ok": True, "failed": {}}
    try:
        data = json.loads(dq.read_text("utf-8"))
    except Exception:
        return {"ok": True, "failed": {}}
    ok = bool(data.get("ok", True))
    failed = data.get("failed") or {}
    return {"ok": ok, "failed": failed}


def _safe_mode_summary(out_dir: Path) -> Dict[str, Any]:
    sm = out_dir / "safe_mode.json"
    if not sm.exists():
        return {}
    try:
        return json.loads(sm.read_text("utf-8"))
    except Exception:
        return {}


def _features_freshness_summary(out_dir: Path) -> Dict[str, Any]:
    ff = out_dir / "features_freshness.json"
    if not ff.exists():
        return {}
    try:
        data = json.loads(ff.read_text("utf-8"))
        return {
            "ok": bool(data.get("ok", True)),
            "ok_count": len(data.get("ok_symbols") or []),
            "stale_count": len((data.get("stale") or {}).keys()),
            "dead_count": len((data.get("dead") or {}).keys()),
        }
    except Exception:
        return {}


def _plan_vs_execution(out_dir: Path) -> Dict[str, Any]:
    """Compare planned providers/symbols/intervals to actual running processes/heartbeats.

    Best-effort: if files are missing, returns empty structures.
    """
    plan_files = [
        out_dir / "brain_plan_morning.json",
        out_dir / "brain_plan_intraday.json",
    ]
    plan: Dict[str, Any] = {}
    for pf in plan_files:
        if pf.exists():
            try:
                plan = json.loads(pf.read_text("utf-8"))
                break
            except Exception:
                continue
    planned = []
    if isinstance(plan, dict):
        for p in (plan.get("providers") or []):
            try:
                planned.append({
                    "provider": p.get("name"),
                    "symbols": list(p.get("symbols") or []),
                    "interval": int(p.get("interval_sec") or 0),
                    "cycles": p.get("cycles"),
                })
            except Exception:
                continue
    # Running procs
    procs = []
    procs_dir = out_dir / "procs"
    if procs_dir.exists():
        for pmeta in procs_dir.glob("llm_*.json"):
            try:
                meta = json.loads(pmeta.read_text("utf-8"))
                procs.append({
                    "provider": meta.get("provider"),
                    "symbols": list(meta.get("symbols") or []),
                    "interval": int(meta.get("interval") or 0),
                    "status": meta.get("status"),
                })
            except Exception:
                continue
    return {"planned": planned, "running": procs}


def build_daily_report(out_dir: str, context: Dict[str, Any], dxp: Dict[str, Any], hpo: Dict[str, Any], rl_eval: Dict[str, Any]) -> str:
    p = Path(out_dir) / "daily_report.md"
    def j(obj: Any) -> str:
        try:
            return json.dumps(obj, indent=2)
        except Exception:
            return str(obj)
    lines = []
    lines.append(f"# Daily Trading Reflection - {dxp.get('market',{}).get('date','')}\n")
    lines.append("## Market Context")
    lines.append(f"- Regime: {context.get('regime')}  |  VIX: {context.get('vix')}  |  TrendScore: {context.get('trend_score')}")
    lines.append("")
    lines.append("## Facts")
    facts = dxp.get('facts',{})
    ca = facts.get('claim_consistency')
    ma = facts.get('market_alignment', facts.get('consistency'))
    lines.append(f"- Decisions: {facts.get('n_decisions')}  |  MarketAlignment: {ma}  |  ClaimConsistency: {ca}")
    lines.append("")
    # Risk audit (best-effort)
    try:
        limits_fp = Path(out_dir)/"risk_limits.json"
        status_fp = Path(out_dir)/"risk_status.json"
        limits = json.loads(limits_fp.read_text("utf-8")) if limits_fp.exists() else {}
        ks = json.loads(status_fp.read_text("utf-8")) if status_fp.exists() else {}
        if limits or ks:
            lines.append("## Risk Audit")
            if ks:
                lines.append(f"- Kill-Switch: status={ks.get('risk_status')} action={ks.get('action')} explanation={ks.get('explanation')}")
            if limits:
                # summarize decisions
                from collections import Counter
                decs = [str(v.get('decision') or '').upper() for v in (limits or {}).values()]
                cnt = Counter(decs)
                lines.append(f"- Pre-Trade Decisions: ALLOW={cnt.get('ALLOW',0)} SIZE_DOWN={cnt.get('SIZE_DOWN',0)} REJECT={cnt.get('REJECT',0)} EXIT={cnt.get('EXIT',0)}")
                # show first few with flags
                shown = 0
                for sym, v in limits.items():
                    if shown >= 8: break
                    flags = ",".join([str(x) for x in (v.get('risk_flags') or [])])
                    lines.append(f"  - {sym}: {str(v.get('decision')).upper()} flags=[{flags}]")
                    shown += 1
            lines.append("")
    except Exception:
        pass
    
    # Hedging summary
    hs = _hedging_summary(Path(out_dir))
    lines.append("## Hedging")
    lines.append(f"- Events: {hs.get('count',0)}  |  Notional: ${hs.get('notional_usd',0.0):,.0f}")
    # Per-symbol distribution (top 10)
    ps = hs.get("per_symbol") or []
    if ps:
        lines.append("- Per-symbol (top 10 by notional):")
        top_ps = ps[:10]
        for it in top_ps:
            lines.append(f"  - {it.get('symbol')}: {it.get('events')} events | ${float(it.get('notional_usd',0.0)):,.0f}")
    # Per-sector distribution (top 10)
    psec = hs.get("per_sector") or []
    if psec:
        lines.append("- Per-sector (top 10 by notional):")
        top_psec = psec[:10]
        for it in top_psec:
            lines.append(f"  - {it.get('sector')}: {it.get('events')} events | ${float(it.get('notional_usd',0.0)):,.0f}")
    lines.append("")
    # Guardian summary
    gs = _guardian_summary(Path(out_dir))
    lines.append("## Guardian")
    lines.append(f"- Events: {gs.get('count',0)}  |  Max DD: {gs.get('max_dd_pct',0.0)*100:.2f}%  |  Last: { (gs.get('last') or {}).get('action') }({ (gs.get('last') or {}).get('reason') })")
    lines.append("")
    # Data Quality
    dq = _dq_summary(Path(out_dir))
    lines.append("## Data Quality")
    if dq.get("ok", True):
        lines.append("- All checks passed")
    else:
        failed = dq.get("failed") or {}
        syms = list(failed.keys())
        lines.append(f"- Blacklist ({len(syms)}): {', '.join(syms[:10])}{'...' if len(syms)>10 else ''}")
        # Show first few reasons per symbol
        show = syms[:5]
        for s in show:
            errs = failed.get(s) or []
            if errs:
                lines.append(f"  - {s}: {errs[0]}{'; +more' if len(errs)>1 else ''}")
    lines.append("")
    # Safe Mode & Features
    sm = _safe_mode_summary(Path(out_dir))
    ff = _features_freshness_summary(Path(out_dir))
    if sm or ff:
        lines.append("## Safe Mode / Features")
        if ff:
            lines.append(f"- Features: ok={ff.get('ok_count',0)} stale={ff.get('stale_count',0)} dead={ff.get('dead_count',0)}")
        if sm:
            lines.append(f"- Safe Mode: {'ON' if sm.get('enabled') else 'OFF'} | restrictions={','.join(sm.get('restrictions') or [])}")
            pso = sm.get('per_symbol_override') or {}
            # show top few overrides
            keys = list(pso.keys())[:5]
            for k in keys:
                lines.append(f"  - {k}: {pso.get(k)}")
        lines.append("")
    # Plan vs Execution (best-effort)
    pve = _plan_vs_execution(Path(out_dir))
    if pve.get("planned") or pve.get("running"):
        lines.append("## Plan vs Execution")
        planned = pve.get("planned") or []
        running = pve.get("running") or []
        lines.append(f"- Planned: {len(planned)}  |  Running: {len(running)}")
        # Show up to 5 planned entries
        if planned:
            lines.append("- Planned entries (up to 5):")
            for it in planned[:5]:
                prov = it.get("provider")
                syms = ",".join(it.get("symbols") or [])
                iv = it.get("interval") or 0
                lines.append(f"  - {prov} @ {int(iv)}s → [{syms}]")
        # Show up to 5 running entries
        if running:
            lines.append("- Running entries (up to 5):")
            for it in running[:5]:
                prov = it.get("provider")
                syms = ",".join(it.get("symbols") or [])
                iv = it.get("interval") or 0
                st = it.get("status")
                lines.append(f"  - {prov} @ {int(iv)}s [{st}] → [{syms}]")
        lines.append("")
    # Portfolio summary (best-effort)
    try:
        pw = json.loads((Path(out_dir)/"portfolio_weights.json").read_text("utf-8")) if (Path(out_dir)/"portfolio_weights.json").exists() else {}
        lines.append("## Portfolio Weights")
        sc = (pw.get("scales") or {})
        if sc:
            lines.append("- Scales (top 10):")
            for i, (k, v) in enumerate(sorted(sc.items(), key=lambda kv: kv[1], reverse=True)):
                if i >= 10: break
                lines.append(f"  - {k}: x{float(v):.2f}")
            lines.append("")
    except Exception:
        pass
    try:
        ov = json.loads((Path(out_dir)/"overlay_hedge.json").read_text("utf-8")) if (Path(out_dir)/"overlay_hedge.json").exists() else {}
        opt = json.loads((Path(out_dir)/"options_hedge.json").read_text("utf-8")) if (Path(out_dir)/"options_hedge.json").exists() else {}
        if ov or opt:
            lines.append("## Overlay Hedge Plan")
            if ov:
                lines.append(f"- Beta: {float(ov.get('beta',0.0)):.2f}  | Target: {float((ov.get('target') or 0.0)):.2f}  | Need Hedge: {ov.get('need_hedge')}  | Instr: {((ov.get('hedge') or {}).get('instrument'))}  | Notional: ${float(((ov.get('hedge') or {}).get('notional_usd',0.0))):,.0f}")
            if opt:
                lines.append(f"- Options Hedge: need={opt.get('need_hedge')}  | Instr: {opt.get('instrument')} {opt.get('option_type')}  | Target Notional: ${float(opt.get('target_notional_usd',0.0)):,.0f}  | Intent: {opt.get('intent')}")
            lines.append("")
    except Exception:
        pass
    lines.append("## Diagnosis")
    lines.append(f"{dxp.get('diagnosis') or 'N/A'}\n")
    lines.append("## Prescription (Proposed)")
    pres = dxp.get('prescription') or []
    if not pres:
        lines.append("- N/A\n")
    else:
        for step in pres:
            lines.append(f"- {step.get('action')}  params={step.get('params')}  conf={step.get('confidence')}")
        lines.append("")
    lines.append("## HPO Decision")
    lines.append(f"```json\n{j(hpo)}\n```\n")
    lines.append("## RL Evaluation")
    lines.append(f"```json\n{j(rl_eval)}\n```\n")
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)
