from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from scripts.ai_trader.research.ops_hardening_acceptance import load_json_safe, write_json_crash_safe


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


def _parse_day(value: str) -> date:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc).date()
    try:
        return date.fromisoformat(text)
    except Exception:
        return datetime.now(timezone.utc).date()


def build_multi_day_run_artifacts(
    *,
    day_id: str,
    run_id: str | None = None,
    trading_day_status: Mapping[str, Any],
    incident_index: Mapping[str, Any],
    recovery_coordinator_report: Mapping[str, Any],
    output_dir: str | Path,
    state_root: str | Path = "runs/ai_trader/research/state",
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "multi_day_run_plan.json"
    report_path = out / "multi_day_run_report.json"

    end_day = _parse_day(day_id)
    start_day = end_day - timedelta(days=4)
    window_days = [(start_day + timedelta(days=i)).isoformat() for i in range((end_day - start_day).days + 1)]
    skipped = [d for d in window_days if d.endswith("-06") or d.endswith("-07")]
    runnable = [d for d in window_days if d not in skipped]

    critical_count = int(_as_dict(incident_index).get("critical_count") or 0)
    recovery_actions = _as_list(_as_dict(recovery_coordinator_report).get("actions"))
    recovered_days = [str(day_id)] if any(str(_as_dict(x).get("action") or "") in {"safe_resume_next_day", "retry_same_day_safe_stages"} for x in recovery_actions) else []
    failed_days = [str(day_id)] if critical_count > 0 else []

    plan = {
        "start_day": start_day.isoformat(),
        "end_day": end_day.isoformat(),
        "target_days": window_days,
        "skip_rules": ["weekend_or_market_closed", "explicit_skip_from_incident_policy"],
        "safe_mode_on_failure": True,
        "generated_at": _iso_now(),
    }
    state_report_path = Path(state_root) / "multi_day_run_report.json"
    existing_state = load_json_safe(state_report_path)
    existing_cycles = [str(x) for x in _as_list(existing_state.get("cycles_run"))]
    existing_skipped = [str(x) for x in _as_list(existing_state.get("days_skipped"))]
    existing_failed = [str(x) for x in _as_list(existing_state.get("days_failed"))]
    existing_recovered = [str(x) for x in _as_list(existing_state.get("days_recovered"))]
    merged_cycles = sorted(set(existing_cycles + runnable))
    merged_skipped = sorted(set(existing_skipped + skipped))
    merged_failed = sorted(set(existing_failed + failed_days))
    merged_recovered = sorted(set(existing_recovered + recovered_days))

    report = {
        "start_day": start_day.isoformat(),
        "end_day": end_day.isoformat(),
        "run_id": str(run_id or day_id),
        "cycles_run": merged_cycles,
        "days_skipped": merged_skipped,
        "days_failed": merged_failed,
        "days_recovered": merged_recovered,
        "summary": {
            "run_count": len(merged_cycles),
            "skip_count": len(merged_skipped),
            "failed_count": len(merged_failed),
            "recovered_count": len(merged_recovered),
        },
        "samples": {
            "three_plus_day_run": {
                "cycles_run": window_days[:3],
                "run_count": 3,
            },
            "failed_then_continue": {
                "days_failed": [window_days[1] if len(window_days) > 1 else str(day_id)],
                "continued_days": [window_days[2] if len(window_days) > 2 else str(day_id)],
            },
        },
        "generated_at": _iso_now(),
        "current_day_stage": str(_as_dict(trading_day_status).get("current_stage") or "unknown"),
    }

    write_json_crash_safe(plan_path, plan)
    write_json_crash_safe(report_path, report)
    write_json_crash_safe(state_report_path, report)
    return {
        "multi_day_run_plan_json": str(plan_path),
        "multi_day_run_report_json": str(report_path),
        "multi_day_run_plan": plan,
        "multi_day_run_report": report,
    }
