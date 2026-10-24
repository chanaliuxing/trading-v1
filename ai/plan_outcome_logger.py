from __future__ import annotations

"""
Helper to log plan_vs_outcome records into memory_fabric + local file.

Intended to be called after plan execution (paper or live) to persist:
- planned EV vs realized EV
- drawdown / hedge effectiveness
- lessons learned
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, List


def log_plan_vs_outcome(
    plan_id: str,
    summary: str,
    ev_plan: float,
    ev_realized: float,
    mdd: float,
    hedge_effect: Optional[Dict[str, Any]] = None,
    issues: Optional[list] = None,
    lessons: Optional[list] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "type": "plan_vs_outcome",
        "plan_id": plan_id,
        "summary": summary,
        "ev_plan": ev_plan,
        "ev_realized": ev_realized,
        "mdd": mdd,
        "hedge_effect": hedge_effect or {},
        "issues": issues or [],
        "lessons": lessons or [],
        "extra": extra or {},
        "timestamp": now,
    }
    doc_id: Optional[str] = None

    # Write to memory_fabric if available
    try:
        from scripts.ai_trader.memory.memory_fabric import write_memory  # type: ignore

        doc_id = write_memory("STS", record, doc_id=f"plan_outcome_{plan_id}_{now}")
        record["doc_id"] = doc_id
    except Exception:
        pass

    # Also append to runs/memory/plan_vs_outcome.jsonl for auditing
    try:
        out_path = Path("runs/memory/plan_vs_outcome.jsonl")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return record


def load_recent_plan_outcomes(
    limit: int = 5,
    days: int = 7,
    mark_accessed: bool = True,
) -> List[Dict[str, Any]]:
    """
    Read recent plan_vs_outcome records (best-effort) with light schema checks.
    """
    path = Path("runs/memory/plan_vs_outcome.jsonl")
    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "plan_vs_outcome":
                    continue
                if any(key not in rec for key in ("plan_id", "ev_plan", "ev_realized", "mdd", "timestamp")):
                    continue
                try:
                    ts = datetime.fromisoformat(str(rec.get("timestamp")).replace("Z", "+00:00"))
                except Exception:
                    ts = datetime.now(timezone.utc)
                if ts < cutoff:
                    continue
                rec["_parsed_ts"] = ts
                rows.append(rec)
    except Exception:
        return []

    rows.sort(key=lambda r: r.get("_parsed_ts") or datetime.now(timezone.utc), reverse=True)
    selected = rows[:limit]

    if mark_accessed:
        try:
            from scripts.ai_trader.memory.memory_fabric import mark_accessed_by_doc_id  # type: ignore

            for rec in selected:
                doc_id = rec.get("doc_id")
                if doc_id:
                    try:
                        mark_accessed_by_doc_id(doc_id)
                    except Exception:
                        continue
        except Exception:
            pass

    for rec in selected:
        rec.pop("_parsed_ts", None)
    return selected


__all__ = ["log_plan_vs_outcome", "load_recent_plan_outcomes"]
