#!/usr/bin/env python3
from __future__ import annotations

"""
Collect observability/health signals for AI Brain components.

Outputs a structured snapshot that can be rendered by monitoring dashboards or
used as a lightweight CLI report.
"""

import argparse
import json
from collections import Counter, defaultdict
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Ensure repo root is on sys.path when invoked via `python scripts/...` or `make`.
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs import metrics
from libs.logger import get_logger
from libs.skills.registry import SkillRegistry

LOG = get_logger("health_snapshot")


def _iter_recent_run_dirs(base: Path, days: int) -> Iterable[Path]:
    cutoff = date.today() - timedelta(days=days - 1)
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        try:
            d = date.fromisoformat(child.name)
        except Exception:
            continue
        if d >= cutoff:
            yield child


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _sleep_loop_stats(days: int) -> Dict[str, Any]:
    summaries = _read_jsonl(Path("runs/memory/daily_summaries.jsonl"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = [
        s for s in summaries
        if "date" in s and _parse_dt(s.get("date"), cutoff) >= cutoff
    ]
    unique_days = {str(s.get("date"))[:10] for s in recent}
    success_rate = len(unique_days) / max(days, 1)
    notes: List[str] = []
    if not recent:
        notes.append("no_recent_daily_summaries")
    elif success_rate < 1.0:
        notes.append("missing_days_in_window")
    return {
        "entries": len(recent),
        "unique_days": len(unique_days),
        "success_rate": round(success_rate, 3),
        "last_date": max(unique_days) if unique_days else None,
        "notes": notes,
    }


def _vector_store_stats() -> Dict[str, Any]:
    try:
        from libs.memory.vector_store import VectorStore  # type: ignore
    except Exception as exc:
        LOG.warning("vector_store_import_failed", error=str(exc))
        return {"available": False, "error": str(exc)}

    try:
        store = VectorStore()
        stats = store.get_stats()
        backend = stats.get("backend")
        count = int(stats.get("count", 0))
        persist_dir = Path(stats.get("persist_dir") or "runs/memory/vector_store")
        size_bytes = 0
        file_count = 0
        if persist_dir.exists():
            for fp in persist_dir.rglob("*"):
                if fp.is_file():
                    file_count += 1
                    size_bytes += fp.stat().st_size
        fragmentation_est = round(file_count / max(count, 1), 3) if count else 0.0
        metrics.vector_store_health.labels(backend=str(backend)).set(1)
        metrics.vector_store_size.labels(backend=str(backend)).set(count)
        return {
            "available": True,
            "backend": backend,
            "count": count,
            "persist_dir": str(persist_dir),
            "size_bytes": size_bytes,
            "file_count": file_count,
            "fragmentation_est": fragmentation_est,
        }
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("vector_store_stats_failed", error=str(exc))
        return {"available": False, "error": str(exc)}


def _memory_usage_stats() -> Dict[str, Any]:
    tiers = {
        "STS": Path("runs/memory/sts.jsonl"),
        "MTS": Path("runs/memory/mts.jsonl"),
        "LTS": Path("runs/memory/lts.jsonl"),
    }
    total_entries = 0
    total_use_count = 0
    for fp in tiers.values():
        for rec in _read_jsonl(fp):
            total_entries += 1
            try:
                total_use_count += int(rec.get("use_count", 0) or 0)
            except Exception:
                continue
    hit_ratio_est = total_use_count / max(total_entries, 1) if total_entries else 0.0
    return {
        "entries": total_entries,
        "use_count": total_use_count,
        "hit_ratio_est": round(hit_ratio_est, 3),
    }


def _tool_call_stats(run_dirs: Iterable[Path]) -> Dict[str, Any]:
    stats = Counter()
    for run_dir in run_dirs:
        for rec in _read_jsonl(run_dir / "brain_calls.jsonl"):
            stats["total"] += 1
            success = rec.get("success")
            if success in (1, True, "1", "true", "True"):
                stats["success"] += 1
            else:
                stats["fail"] += 1
        for rec in _read_jsonl(run_dir / "ml_events.jsonl"):
            if rec.get("event") != "ml.job":
                continue
            stats["total"] += 1
            if rec.get("status") == "completed":
                stats["success"] += 1
            else:
                stats["fail"] += 1
    success_rate = stats["success"] / max(stats["total"], 1)
    return {
        "total": stats["total"],
        "success": stats["success"],
        "fail": stats["fail"],
        "success_rate": round(success_rate, 3),
    }


def _auto_evolution_stats(days: int) -> Dict[str, Any]:
    records = _read_jsonl(Path("runs/ai_trader/evolution_experiments.jsonl"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = [r for r in records if _parse_dt(r.get("ts"), cutoff) >= cutoff]
    status_counter = Counter()
    for rec in recent:
        reason = (rec.get("reason") or "").lower()
        status = "success"
        if "fail" in reason:
            status = "fail"
        status_counter[status] += 1
    pnl_delta = _plan_outcome_delta(days)
    if pnl_delta is not None:
        metrics.auto_evolution_profit_delta.set(pnl_delta)
    return {
        "total": sum(status_counter.values()),
        "by_status": dict(status_counter),
        "pnl_delta": pnl_delta,
    }


def _skill_flow_stats(run_dirs: Iterable[Path]) -> Dict[str, Any]:
    events = Counter()
    approvals = 0
    rejections = 0
    rejection_reasons = Counter()
    evaluations_started = 0
    coverage: List[float] = []
    for run_dir in run_dirs:
        flow = run_dir / "skill_flow.jsonl"
        for rec in _read_jsonl(flow):
            evt = str(rec.get("event") or "").lower()
            if evt:
                events[evt] += 1
            if evt == "evaluation_started":
                evaluations_started += 1
            status = str(rec.get("status") or "").lower()
            if status == "approved":
                approvals += 1
            if status == "rejected":
                rejections += 1
                reasons = rec.get("reasons", [])
                if isinstance(reasons, list):
                    for reason in reasons:
                        # Normalize reason strings like "coverage_below_threshold:80.0%<90.0%"
                        reason_code = reason.split(":")[0]
                        rejection_reasons[reason_code] += 1
            cov = rec.get("coverage_pct")
            try:
                if cov is not None:
                    coverage.append(float(cov))
            except Exception:
                continue
    mean_cov = sum(coverage) / len(coverage) if coverage else None
    enabled = len(SkillRegistry().list_enabled())
    total_evaluations = approvals + rejections
    approval_rate = approvals / max(total_evaluations, 1)
    return {
        "events": sum(events.values()),
        "by_event": dict(events),
        "approvals": approvals,
        "rejections": rejections,
        "evaluations_started": evaluations_started,
        "approval_rate": round(approval_rate, 3),
        "rejection_reasons": dict(rejection_reasons.most_common(5)),
        "mean_coverage": round(mean_cov, 2) if mean_cov is not None else None,
        "enabled_skills": enabled,
    }


def _plan_outcome_delta(days: int) -> float | None:
    path = Path("runs/memory/plan_vs_outcome.jsonl")
    if not path.exists():
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deltas: List[float] = []
    for rec in _read_jsonl(path):
        ts = _parse_dt(rec.get("timestamp"), cutoff)
        if ts < cutoff:
            continue
        try:
            ev_plan = float(rec.get("ev_plan", 0.0))
            ev_realized = float(rec.get("ev_realized", 0.0))
            deltas.append(ev_realized - ev_plan)
        except Exception:
            continue
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 4)


def _performance_stats(run_dirs: Iterable[Path]) -> Dict[str, Any]:
    """Compute simple performance stats (returns/Sharpe) vs optional benchmark from pnl.jsonl if present."""
    returns: List[float] = []
    bench: List[float] = []
    for run_dir in run_dirs:
        pnl_path = run_dir / "pnl.jsonl"
        if not pnl_path.exists():
            continue
        for rec in _read_jsonl(pnl_path):
            try:
                r = float(rec.get("return", rec.get("pnl", 0.0)))
                returns.append(r)
            except Exception:
                continue
            try:
                br = rec.get("bench_return", rec.get("benchmark_return"))
                if br is not None:
                    bench.append(float(br))
            except Exception:
                continue
    days = max(len(returns), len(bench))
    if not returns:
        return {"status": "missing_data", "days": days}
    import math
    mean_r = sum(returns) / len(returns)
    std_r = math.sqrt(sum((x - mean_r) ** 2 for x in returns) / len(returns)) if len(returns) > 1 else 0.0
    sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else None
    result = {
        "status": "ok",
        "days": days,
        "mean_return": mean_r,
        "sharpe": sharpe,
    }
    if bench:
        mean_b = sum(bench) / len(bench)
        std_b = math.sqrt(sum((x - mean_b) ** 2 for x in bench) / len(bench)) if len(bench) > 1 else 0.0
        sharpe_b = (mean_b / std_b * math.sqrt(252)) if std_b > 0 else None
        result["bench_sharpe"] = sharpe_b
        result["excess_return"] = mean_r - mean_b
        if sharpe is not None and sharpe_b is not None:
            result["excess_sharpe"] = sharpe - sharpe_b
    return result


def _strategy_council_stats(run_dirs: Iterable[Path]) -> Dict[str, Any]:
    adoption_ratios: List[float] = []
    hedge_effects: List[float] = []
    for run_dir in run_dirs:
        for rec in _read_jsonl(run_dir / "strategy_council.jsonl"):
            proposals = rec.get("proposals") or []
            final_plan = rec.get("final_plan") or {}
            if proposals:
                adoption_ratios.append(1.0 / len(proposals))
            paper = rec.get("paper_result") or {}
            hedge_var = paper.get("hedge_var_reduction")
            try:
                if hedge_var is not None:
                    hedge_effects.append(float(hedge_var))
            except Exception:
                continue
            base_id = final_plan.get("base_proposal_id")
            if base_id and proposals:
                adoption_ratios.append(1.0 / len(proposals))
    avg_adoption = round(sum(adoption_ratios) / len(adoption_ratios), 3) if adoption_ratios else 0.0
    avg_hedge = round(sum(hedge_effects) / len(hedge_effects), 3) if hedge_effects else 0.0
    if adoption_ratios:
        metrics.strategy_council_adoption_rate.set(avg_adoption)
    if hedge_effects:
        metrics.strategy_council_hedge_effect.set(avg_hedge)
    return {
        "records": len(adoption_ratios),
        "adoption_rate": avg_adoption,
        "hedge_var_reduction": avg_hedge,
    }


def _anomaly_stats(run_dirs: Iterable[Path]) -> Dict[str, Any]:
    count = 0
    for run_dir in run_dirs:
        for rec in _read_jsonl(run_dir / "ml_events.jsonl"):
            evt = (rec.get("event") or "").lower()
            status = (rec.get("status") or "").lower()
            if "anomaly" in evt or "immune" in evt or "immunity" in status:
                count += 1
    return {"recent": count}


def _hedge_execution_stats(run_dirs: Iterable[Path]) -> Dict[str, Any]:
    """Collect hedge execution statistics from audit logs."""
    total_triggers = 0
    successful = 0
    failed = 0
    total_cost = 0.0
    effectiveness_vals: List[float] = []
    strategies = Counter()
    failures_by_reason = Counter()

    for run_dir in run_dirs:
        hedge_audit = run_dir / "hedge_audit.jsonl"
        for rec in _read_jsonl(hedge_audit):
            total_triggers += 1

            result = rec.get("result") or {}
            intent = rec.get("intent") or {}

            if result.get("success"):
                successful += 1
            else:
                failed += 1
                error = result.get("error", "unknown")
                # Extract error category
                if "cost_too_high" in error:
                    failures_by_reason["cost_exceeded"] += 1
                elif "no_positions" in error:
                    failures_by_reason["no_positions"] += 1
                elif "timeout" in error:
                    failures_by_reason["timeout"] += 1
                else:
                    failures_by_reason["other"] += 1

            # Accumulate cost
            cost = result.get("total_cost", 0.0)
            if isinstance(cost, (int, float)):
                total_cost += cost

            # Track effectiveness
            eff = result.get("hedge_effectiveness")
            if eff is not None and isinstance(eff, (int, float)):
                effectiveness_vals.append(float(eff))

            # Track strategy usage
            strategy = intent.get("strategy", "unknown")
            strategies[strategy] += 1

    success_rate = successful / max(total_triggers, 1) if total_triggers > 0 else 0.0
    avg_effectiveness = sum(effectiveness_vals) / len(effectiveness_vals) if effectiveness_vals else None

    return {
        "total_triggers": total_triggers,
        "successful": successful,
        "failed": failed,
        "success_rate": round(success_rate, 3),
        "total_cost_usd": round(total_cost, 2),
        "avg_effectiveness": round(avg_effectiveness, 3) if avg_effectiveness is not None else None,
        "by_strategy": dict(strategies),
        "failures_by_reason": dict(failures_by_reason.most_common(5)),
    }


def _parse_dt(val: Any, default: datetime) -> datetime:
    if isinstance(val, datetime):
        return val
    if not val:
        return default
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return default


def _research_cycle_stats(days: int) -> Dict[str, Any]:
    # Track research cycles via evolution_experiments.jsonl (primary log)
    # or scan runs/ai_trader for research_*.json artifacts
    records = _read_jsonl(Path("runs/ai_trader/evolution_experiments.jsonl"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = [r for r in records if _parse_dt(r.get("ts"), cutoff) >= cutoff]
    
    total = len(recent)
    success = sum(1 for r in recent if "fail" not in (r.get("status") or "").lower())
    rate = success / max(total, 1)
    
    return {
        "total": total,
        "success": success,
        "success_rate": round(rate, 3),
        "last_ts": recent[-1].get("ts") if recent else None
    }


def collect_health_snapshot(days: int = 7) -> Dict[str, Any]:
    base_dir = Path("runs/ai_trader")
    recent_dirs = list(_iter_recent_run_dirs(base_dir, days)) if base_dir.exists() else []
    snapshot = {
        "window_days": days,
        "sleep_loop": _sleep_loop_stats(days),
        "research_cycle": _research_cycle_stats(days),
        "vector_store": _vector_store_stats(),
        "memory": _memory_usage_stats(),
        "tool_calls": _tool_call_stats(recent_dirs),
        "auto_evolution": _auto_evolution_stats(days),
        "skill_flow": _skill_flow_stats(recent_dirs),
        "strategy_council": _strategy_council_stats(recent_dirs),
        "hedge_execution": _hedge_execution_stats(recent_dirs),
        "performance": _performance_stats(recent_dirs),
        "anomalies": _anomaly_stats(recent_dirs),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return snapshot


def persist_health_snapshot(out_path: Optional[Path] = None, days: int = 7) -> Path:
    snapshot = collect_health_snapshot(days=days)
    if out_path is None:
        out_dir = Path("runs/ai_trader") / date.today().isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "health_snapshot.json"
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect AI Trader health snapshot")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    parser.add_argument("--out", type=Path, help="Optional output path; defaults to runs/ai_trader/<today>/health_snapshot.json")
    args = parser.parse_args()

    path = persist_health_snapshot(out_path=args.out, days=args.days)
    print(f"✅ health snapshot written to {path}")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
