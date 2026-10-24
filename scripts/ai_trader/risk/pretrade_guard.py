from __future__ import annotations
import json
import hashlib
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple
from datetime import date

from scripts.ai_trader.risk.metrics import (
    collect_symbol_metrics,
    portfolio_exposure,
    collect_trade_stats,
    collect_risk_analytics,
)


def _per_symbol_sig(symbol: str, metrics: Dict, perf: Dict, analytics: Dict, flags: List[str]) -> str:
    """Generate per-symbol signature based on quantized states (not continuous values).

    Only uses boolean/categorical features to avoid jitter from small numerical changes.
    """
    # EV state: positive or non-positive
    ev = perf.get("ev", 0.0)
    ev_state = "pos" if (ev and float(ev) > 0) else "non_pos"

    # PF state: greater than 1 or not
    pf = perf.get("pf", 1.0)
    pf_state = "gt1" if (pf and float(pf) > 1.0) else "le1"

    # VaR backtest pass/fail
    kp = analytics.get("kupiec_p", 1.0) or 1.0
    cp = analytics.get("christoffersen_p", 1.0) or 1.0
    var_pass = "pass" if (float(kp) >= 0.05 and float(cp) >= 0.05) else "fail"

    # Liquidity flags (spread, adv)
    spread_bps = metrics.get("spread_bps") or 0.0
    spread_state = "wide" if float(spread_bps) > 50.0 else "ok"

    adv_usd = metrics.get("adv_usd") or 0.0
    adv_state = "low" if (adv_usd and float(adv_usd) < 10_000_000) else "ok"

    # Combine states
    state = {
        "symbol": symbol,
        "ev": ev_state,
        "pf": pf_state,
        "var": var_pass,
        "spread": spread_state,
        "adv": adv_state,
        "flags": sorted(flags),  # Include computed flags
    }

    sig_json = json.dumps(state, sort_keys=True)
    return hashlib.sha256(sig_json.encode()).hexdigest()[:16]  # Short hash


def _global_sig(exposure: Dict, dq_ok: bool = True) -> str:
    """Generate global signature for portfolio-wide changes.

    Uses bucketed values to avoid jitter from small changes.
    """
    gross = exposure.get("gross", 0.0) or 0.0
    net = exposure.get("net", 0.0) or 0.0

    # Bucket gross exposure in 10% increments
    gross_bucket = int(abs(float(gross)) / 0.10)
    net_bucket = int(abs(float(net)) / 0.10)
    net_sign = "pos" if float(net) >= 0 else "neg"

    state = {
        "gross_bucket": gross_bucket,
        "net_bucket": net_bucket,
        "net_sign": net_sign,
        "dq_ok": dq_ok,
    }

    sig_json = json.dumps(state, sort_keys=True)
    return hashlib.sha256(sig_json.encode()).hexdigest()[:16]


def _load_cache(runs_dir: str) -> Dict[str, Any]:
    """Load per-symbol cache from disk."""
    dstr = date.today().isoformat()
    cache_file = Path(runs_dir) / dstr / "risk_pretrade.cache.json"

    if not cache_file.exists():
        return {"global": {}, "per_symbol": {}, "stats": {"hits": 0, "misses": 0}}

    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return {"global": {}, "per_symbol": {}, "stats": {"hits": 0, "misses": 0}}


def _save_cache(runs_dir: str, cache: Dict[str, Any]) -> None:
    """Save per-symbol cache to disk."""
    dstr = date.today().isoformat()
    cache_file = Path(runs_dir) / dstr / "risk_pretrade.cache.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _check_global_invalidation(cache: Dict, cur_global_sig: str, gross_delta_pct: float = 0.10) -> bool:
    """Check if global state changed significantly enough to invalidate all cached symbols."""
    cached_global = cache.get("global", {})
    cached_sig = cached_global.get("sig")

    if not cached_sig:
        return True  # No previous global state

    # Simple check: if signature changed, invalidate
    # More sophisticated: parse and compare buckets, but signature already does bucketing
    return cached_sig != cur_global_sig


def run_pretrade_guard(
    runs_dir: str,
    symbols: List[str],
    extras: Dict[str, Any] | None = None,
    brain=None,
    min_interval_sec: int = 3600,
    per_symbol_ttl_sec: int = 1800,
) -> Dict[str, Any]:
    """Compute risk metrics with per-symbol caching + global invalidation.

    Strategy:
    1. Load cache with per-symbol decisions
    2. Check global state (portfolio exposure, DQ status) - if changed significantly, invalidate all
    3. For each symbol:
       - Compute per-symbol signature
       - If cached + not expired + signature matches → reuse
       - Else add to to_eval list
    4. If to_eval not empty, call brain with only those symbols
    5. Merge results and save cache

    Args:
        runs_dir: Base runs directory
        symbols: List of symbols to evaluate
        extras: Extra context (optional)
        brain: Brain instance for LLM calls (optional)
        min_interval_sec: Global cache TTL (default 1 hour)
        per_symbol_ttl_sec: Per-symbol cache TTL (default 30 min)

    Returns:
        Dict with decisions, cache stats, etc.
    """
    dstr = date.today().isoformat()
    runs = Path(runs_dir) / dstr
    runs.mkdir(parents=True, exist_ok=True)

    # Collect metrics for all symbols
    sym_metrics = collect_symbol_metrics(symbols)
    perf = collect_trade_stats(symbols, lookback_days=60)
    analytics = collect_risk_analytics(symbols, lookback_days=60, alpha=0.99)
    exposure = portfolio_exposure()

    # Threshold overrides from extras
    thr = ((extras or {}).get("thresholds") or {})
    try:
        max_spread_bps = float(thr.get("max_spread_bps", 50.0) or 50.0)
    except Exception:
        max_spread_bps = 50.0
    try:
        min_adv_usd = float(thr.get("min_adv_usd", 10_000_000.0) or 10_000_000.0)
    except Exception:
        min_adv_usd = 10_000_000.0
    try:
        var_k_min = float(thr.get("var_kupiec_p_min", 0.05) or 0.05)
    except Exception:
        var_k_min = 0.05
    try:
        var_c_min = float(thr.get("var_christoffersen_p_min", 0.05) or 0.05)
    except Exception:
        var_c_min = 0.05

    # Compute flags (market microstructure only - NOT historical performance)
    # Historical performance (ev/pf) is checked via OOF quality gate in orchestrator
    flags_by_symbol: Dict[str, list] = {}
    for s in symbols:
        f: list[str] = []
        m = (sym_metrics or {}).get(s) or {}
        p = (perf or {}).get(s) or {}
        rk = (analytics or {}).get(s) or {}

        # ❌ REMOVED: ev_le_0 and pf_le_1 checks
        # Reason: Historical performance should not prevent future trades
        # Model quality is checked via OOF gate (auc/ic/coverage)

        # ✅ Market microstructure checks (liquidity, volatility)
        try:
            sp = float(m.get("spread_bps", 0.0) or 0.0)
            if sp > max_spread_bps:
                f.append("spread_gt_thresh")  # High transaction cost
        except Exception:
            pass
        try:
            adv = float(m.get("adv_usd", 0.0) or 0.0)
            if adv and adv < min_adv_usd:
                f.append("adv_lt_thresh")  # Low liquidity
        except Exception:
            pass
        try:
            beta = rk.get("beta_spy")
            if beta is not None and abs(float(beta)) > 1.5:
                f.append("beta_spy_gt_1.5")  # High systematic risk
        except Exception:
            pass

        # VAR backtest (Kupiec/Christoffersen) thresholds
        try:
            kp = float(rk.get("kupiec_p", 1.0) or 1.0)
            cp = float(rk.get("christoffersen_p", 1.0) or 1.0)
            if (kp < var_k_min) or (cp < var_c_min):
                f.append("var_backtest_fail")
        except Exception:
            pass

        # ✅ Extreme volatility check (new - using atr14)
        try:
            atr = float(m.get("atr14", 0.0) or 0.0)
            # For stocks, ATR > 10% of recent close is considered extreme
            # This is a simplified check - ideally compare to historical percentiles
            if atr > 0.0:  # Only check if we have ATR data
                # Flag if volatility seems extreme (placeholder threshold)
                # TODO: Make this adaptive based on symbol's historical volatility
                pass  # Commented out for now - needs proper calibration
        except Exception:
            pass

        flags_by_symbol[s] = f

    # Load cache
    cache = _load_cache(runs_dir)

    # Check global invalidation
    cur_global_sig = _global_sig(exposure, dq_ok=True)  # TODO: get actual DQ status from extras
    global_invalidated = _check_global_invalidation(cache, cur_global_sig)

    # Determine which symbols need evaluation
    to_eval_symbols = []
    cached_decisions = {}
    now = time.time()

    if global_invalidated:
        # Global state changed - re-evaluate all
        to_eval_symbols = symbols.copy()
        cache["stats"]["global_invalidations"] = cache["stats"].get("global_invalidations", 0) + 1
    else:
        # Check per-symbol cache
        for s in symbols:
            m = (sym_metrics or {}).get(s) or {}
            p = (perf or {}).get(s) or {}
            rk = (analytics or {}).get(s) or {}
            flags = flags_by_symbol.get(s, [])

            per_sym_sig = _per_symbol_sig(s, m, p, rk, flags)

            cached_sym = cache.get("per_symbol", {}).get(s)

            if cached_sym:
                cached_sig = cached_sym.get("sig")
                cached_ts = cached_sym.get("ts", 0)
                age = now - cached_ts

                if cached_sig == per_sym_sig and age < per_symbol_ttl_sec:
                    # Cache hit
                    cached_decisions[s] = cached_sym.get("decision", {})
                    cache["stats"]["hits"] = cache["stats"].get("hits", 0) + 1
                else:
                    # Cache miss (signature changed or expired)
                    to_eval_symbols.append(s)
                    cache["stats"]["misses"] = cache["stats"].get("misses", 0) + 1
            else:
                # No cache entry
                to_eval_symbols.append(s)
                cache["stats"]["misses"] = cache["stats"].get("misses", 0) + 1

    # Evaluate uncached symbols
    decisions: List[Dict[str, Any]] = []

    if to_eval_symbols:
        # Build context for only symbols that need evaluation
        ctx_sub = {
            "portfolio": exposure,
            "symbols": to_eval_symbols,
            "metrics": {s: (sym_metrics or {}).get(s, {}) for s in to_eval_symbols},
            "performance": {s: (perf or {}).get(s, {}) for s in to_eval_symbols},
            "risk": {s: (analytics or {}).get(s, {}) for s in to_eval_symbols},
            "flags": {s: flags_by_symbol.get(s, []) for s in to_eval_symbols},
        }
        if extras:
            ctx_sub.update({"extras": extras})

        # Detect data completeness and pre-mark symbols needing backfill
        need_backfill_syms: List[str] = []
        missing_by_symbol: Dict[str, List[str]] = {}
        for s in to_eval_symbols:
            m = (sym_metrics or {}).get(s) or {}
            missing: List[str] = []
            if m.get("atr14") is None:
                missing.append("atr14")
            if m.get("rvol20") is None:
                missing.append("rvol20")
            # adv/spread 作为次要参考，不强制要求
            if missing:
                need_backfill_syms.append(s)
                missing_by_symbol[s] = missing

        # Call brain only for symbols needing evaluation and not missing required metrics
        eval_decisions = []
        if brain is not None:
            try:
                ctx_call = dict(ctx_sub)
                if need_backfill_syms:
                    # 排除缺关键指标的符号，先返回 backfill 指示；其余符号走大脑裁决
                    ctx_call["symbols"] = [s for s in to_eval_symbols if s not in need_backfill_syms]
                resp = brain.decide("risk_pretrade", ctx_call)
                eval_decisions = (resp or {}).get("decisions") or []
            except Exception:
                eval_decisions = []

        # Post-process Brain decisions to avoid over-rejection on weak historical stats
        # and to convert insufficent volatility data into backfill requests.
        if eval_decisions:
            sanitized: List[Dict[str, Any]] = []
            for d in eval_decisions:
                try:
                    s = d.get("symbol")
                    if not s:
                        continue
                    m = (sym_metrics or {}).get(s) or {}
                    p = (perf or {}).get(s) or {}
                    rk = (analytics or {}).get(s) or {}
                    flags = list(d.get("risk_flags") or [])
                    dec = str(d.get("decision") or "").upper()
                    rationale = str(d.get("rationale") or "")

                    # If volatility metrics missing or Brain rationale implies missing vol → convert to backfill
                    missing_metrics = []
                    if m.get("atr14") is None:
                        missing_metrics.append("atr14")
                    if m.get("rvol20") is None:
                        missing_metrics.append("rvol20")
                    if missing_metrics or ("volatility" in rationale.lower() and "insufficient" in rationale.lower()):
                        sanitized.append({
                            "symbol": s,
                            "decision": "PENDING",
                            "risk_flags": ["data_missing"],
                            "need_backfill": True,
                            "missing_metrics": missing_metrics or ["atr14","rvol20"],
                            "rationale": "required_metrics_missing"
                        })
                        continue

                    # If REJECT solely because EV<=0/PF<=1 with few trades → ALLOW (ignore weak historical priors)
                    if dec == "REJECT":
                        # Normalize flags to lowercase
                        low = {str(f).lower() for f in flags}
                        ev_pf_only = low.issubset({"ev_le_0", "pf_le_1"}) and len(low) > 0
                        n_trades = int((p or {}).get("n") or 0)
                        if ev_pf_only and n_trades < 30:
                            sanitized.append({
                                "symbol": s,
                                "decision": "ALLOW",
                                "risk_flags": [],
                                "rationale": "ev_pf_ignored_due_to_strategy_changes"
                            })
                            continue

                    # Strip ev/pf weak priors from flags (keep microstructure/var flags)
                    flags = [f for f in flags if str(f).lower() not in ("ev_le_0", "pf_le_1")]
                    d["risk_flags"] = flags
                    sanitized.append(d)
                except Exception:
                    sanitized.append(d)
            eval_decisions = sanitized

        # Fallback: use heuristics (but exclude symbols needing backfill)
        if not eval_decisions:
            for s in to_eval_symbols:
                if s in need_backfill_syms:
                    continue  # Skip - will be handled separately as PENDING
                fs = flags_by_symbol.get(s) or []
                # If flags are ONLY due to no trading history (ev_le_0 + pf_le_1 only), allow SIZE_DOWN
                # This prevents rejecting all symbols when starting fresh with no history
                if fs == ["ev_le_0", "pf_le_1"]:
                    dec = "ALLOW"
                    rationale = "ev_pf_ignored_due_to_strategy_changes"
                elif ("var_backtest_fail" in fs) or ("ev_le_0" in fs and "pf_le_1" in fs and len(fs) > 2):
                    # Real risk flags present beyond just no history
                    dec = "REJECT"
                    rationale = "risk_flags_triggered"
                elif fs:
                    dec = "SIZE_DOWN"
                    rationale = "minor_risk_flags"
                else:
                    dec = "ALLOW"
                    rationale = "no_risk_flags"
                eval_decisions.append({
                    "symbol": s,
                    "decision": dec,
                    "risk_flags": fs,
                    "rationale": rationale
                })

        # Add backfill-needed decisions (data_missing)
        for s in need_backfill_syms:
            eval_decisions.append({
                "symbol": s,
                "decision": "PENDING",
                "risk_flags": ["data_missing"],
                "need_backfill": True,
                "missing_metrics": missing_by_symbol.get(s, []),
                "rationale": "required_metrics_missing"
            })

        # Update cache with new evaluations
        if "per_symbol" not in cache:
            cache["per_symbol"] = {}

        for dec in eval_decisions:
            s = dec.get("symbol")
            if s:
                m = (sym_metrics or {}).get(s) or {}
                p = (perf or {}).get(s) or {}
                rk = (analytics or {}).get(s) or {}
                flags = flags_by_symbol.get(s, [])

                per_sym_sig = _per_symbol_sig(s, m, p, rk, flags)

                cache["per_symbol"][s] = {
                    "sig": per_sym_sig,
                    "ts": now,
                    "decision": dec
                }

                decisions.append(dec)

    # Add cached decisions
    for s in symbols:
        if s in cached_decisions:
            decisions.append(cached_decisions[s])

    # Update global signature in cache
    cache["global"] = {
        "sig": cur_global_sig,
        "ts": now
    }

    # Save cache
    _save_cache(runs_dir, cache)

    # Persist risk_limits.json
    limits = {d.get("symbol"): {
        "decision": d.get("decision"),
        "size": d.get("size"),
        "stops": d.get("stops"),
        "risk_flags": d.get("risk_flags") or flags_by_symbol.get(str(d.get("symbol")), []),
    } for d in decisions if d.get("symbol")}
    (runs / "risk_limits.json").write_text(json.dumps(limits, indent=2), encoding="utf-8")

    return {
        "decisions": decisions,
        "limits_file": str(runs / "risk_limits.json"),
        "cache_stats": {
            "hits": cache["stats"].get("hits", 0),
            "misses": cache["stats"].get("misses", 0),
            "to_eval_count": len(to_eval_symbols),
            "cached_count": len(cached_decisions),
            "global_invalidated": global_invalidated,
        },
        "global_sig": cur_global_sig,
    }
