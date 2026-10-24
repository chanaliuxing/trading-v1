from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from scripts.ai_trader.research.ops_hardening_acceptance import write_json_crash_safe


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rate(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def build_reliability_metrics(
    *,
    day_id: str,
    run_id: str | None = None,
    multi_day_run_report: Mapping[str, Any],
    incident_index: Mapping[str, Any],
    broker_readonly_probe: Mapping[str, Any],
    broker_reconcile_report: Mapping[str, Any],
    recovery_coordinator_report: Mapping[str, Any],
    live_ops_summary: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "reliability_metrics.json"

    multi = _as_dict(multi_day_run_report)
    incidents = _as_dict(incident_index)
    reconcile = _as_dict(broker_reconcile_report)
    recovery = _as_dict(recovery_coordinator_report)
    ops = _as_dict(live_ops_summary)

    run_count = int(_as_dict(multi.get("summary")).get("run_count") or len(_as_list(multi.get("cycles_run"))))
    failed_count = int(_as_dict(multi.get("summary")).get("failed_count") or len(_as_list(multi.get("days_failed"))))
    recovered_count = int(_as_dict(multi.get("summary")).get("recovered_count") or len(_as_list(multi.get("days_recovered"))))
    critical_count = int(incidents.get("critical_count") or 0)
    total_incidents = int(incidents.get("total_count") or 0)
    probe_success = 1.0 if str(_as_dict(broker_readonly_probe).get("status") or "") == "success" else 0.0
    reconcile_clean = 1.0 if bool(reconcile.get("clean_reconcile", False)) else 0.0
    halt_count = len(_as_list(ops.get("halted_candidates")))
    flatten_count = len(_as_list(ops.get("flattened_candidates")))
    recovery_actions = _as_list(recovery.get("actions"))
    recovery_success_count = sum(1 for x in recovery_actions if str(_as_dict(x).get("action") or "") in {"safe_resume_next_day", "retry_same_day_safe_stages"} and bool(_as_dict(x).get("applied")))

    payload = {
        "day_id": day_id,
        "run_id": str(run_id or day_id),
        "cycle_success_rate": _rate(max(run_count - failed_count, 0), max(run_count, 1)),
        "broker_probe_success_rate": probe_success,
        "reconcile_clean_rate": reconcile_clean,
        "incident_rate": _rate(total_incidents, max(run_count, 1)),
        "recovery_success_rate": _rate(recovery_success_count, max(critical_count, 1)),
        "halt_rate": _rate(halt_count, max(run_count, 1)),
        "flatten_rate": _rate(flatten_count, max(run_count, 1)),
        "status": "degraded"
        if (
            probe_success < 1.0
            or reconcile_clean < 1.0
            or _rate(max(run_count - failed_count, 0), max(run_count, 1)) < 0.9
        )
        else "healthy",
        "samples": {
            "fixture_sample": {
                "cycle_success_rate": 1.0,
                "broker_probe_success_rate": 1.0,
            },
            "real_sample": {
                "cycle_success_rate": _rate(max(run_count - failed_count, 0), max(run_count, 1)),
                "incident_rate": _rate(total_incidents, max(run_count, 1)),
            },
            "degraded_example": {
                "broker_probe_success_rate": 0.0,
                "reconcile_clean_rate": 0.0,
                "status": "degraded",
            },
        },
        "generated_at": _iso_now(),
    }
    write_json_crash_safe(path, payload)
    return {"reliability_metrics_json": str(path), "reliability_metrics": payload}
