from __future__ import annotations

"""
Minimal anomaly detector:
- Scans recent ml_events.jsonl for high error_rate / latency / token cost
- Emits immune_case records (dict) for ResearchState consumption
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any

try:
    # Optional memory fabric for semantic recall
    from scripts.ai_trader.memory.memory_fabric import write_memory  # type: ignore
except Exception:
    write_memory = None  # type: ignore


def _recommend_action(case: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map anomaly to a default immune action.
    - Hard errors -> pause
    - Long latency -> reduce sizing
    """
    reasons = " ".join(case.get("reasons") or [])
    action = "reduce_sizing_multiplier"
    params: Dict[str, Any] = {"factor": 0.8}
    if "error_code" in reasons or "ok=false" in reasons:
        action = "pause_strategy"
        params = {"reason": "anomaly_detected"}
    return {
        "action": action,
        "params": params,
        "reason": reasons,
        "source": "anomaly_detector",
        "ts": case.get("ts") or datetime.now(timezone.utc).isoformat(),
    }


def load_recent_ml_events(days: int = 1) -> List[Dict[str, Any]]:
    runs_dir = Path("runs/ai_trader")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items: List[Dict[str, Any]] = []
    for day_dir in sorted(runs_dir.glob("*")):
        try:
            day_date = datetime.fromisoformat(day_dir.name)
        except Exception:
            continue
        if day_date < cutoff:
            continue
        ml_events = day_dir / "ml_events.jsonl"
        if not ml_events.exists():
            continue
        with ml_events.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
    return items


def detect_anomalies(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Heuristic rules:
    - error_code present -> anomaly
    - duration_sec > 300s -> anomaly
    - ok=false -> anomaly
    """
    cases: List[Dict[str, Any]] = []
    for evt in events:
        if evt.get("event") != "ml.job":
            continue
        if evt.get("status") != "completed":
            continue
        metrics = evt.get("metrics", {}) or {}
        err = metrics.get("error_code")
        duration = metrics.get("duration_sec") or evt.get("duration_sec")
        ok = evt.get("ok")
        reasons = []
        if err:
            reasons.append(f"error_code={err}")
        if duration and float(duration) > 300:
            reasons.append(f"duration>{duration}s")
        if ok is False:
            reasons.append("ok=false")
        if reasons:
            cases.append({
                "type": "ml_job_anomaly",
                "job_id": evt.get("job_id"),
                "job_type": evt.get("job_type"),
                "reasons": reasons,
                "ts": evt.get("ts"),
            })
    return cases


def main() -> None:
    events = load_recent_ml_events(days=1)
    cases = detect_anomalies(events)
    if cases:
        immune_out = Path("runs/memory/immune_cases.jsonl")
        immune_out.parent.mkdir(parents=True, exist_ok=True)
        action_out = Path("runs/memory/immune_actions.jsonl")
        with immune_out.open("a", encoding="utf-8") as f_cases, action_out.open("a", encoding="utf-8") as f_act:
            for c in cases:
                f_cases.write(json.dumps(c) + "\n")
                action = _recommend_action(c)
                f_act.write(json.dumps(action) + "\n")
                if write_memory:
                    try:
                        write_memory(
                            "STS",
                            {
                                "type": "immune_case",
                                "summary": c.get("reasons"),
                                "details": c,
                                "recommended_action": action,
                            },
                        )
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
