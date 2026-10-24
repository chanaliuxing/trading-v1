#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ET = ZoneInfo("America/New_York")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _latest_json(repo_root: Path, path_glob: str) -> Path | None:
    paths = sorted(repo_root.glob(path_glob))
    return paths[-1] if paths else None


def _report_day(path: Path | None, payload: dict[str, Any]) -> str:
    day = str(payload.get("day") or "").strip()
    if day:
        return day
    if path is None:
        return ""
    prefix = path.name.split("_", 1)[0]
    return prefix if len(prefix) == 8 and prefix.isdigit() else ""


def _latest_json_for_day(repo_root: Path, path_glob: str, day: str) -> tuple[Path | None, dict[str, Any]]:
    if not day:
        path = _latest_json(repo_root, path_glob)
        return path, (_read_json(path) if path else {})
    for path in reversed(sorted(repo_root.glob(path_glob))):
        payload = _read_json(path)
        if _report_day(path, payload) == day:
            return path, payload
    return None, {}


def _opening_phase_summary(path: Path | None) -> dict[str, Any]:
    payload = _read_json(path) if path else {}
    initial = dict(payload.get("initial") or {})
    final = dict(payload.get("final") or {})
    actions = list(payload.get("actions") or [])
    if payload and final:
        final_recommendation = str(final.get("recommendation") or "UNKNOWN").strip()
        initial_recommendation = str(initial.get("recommendation") or final_recommendation).strip()
        if final_recommendation == "PASS" and actions and initial_recommendation != "PASS":
            status = "AUTO_FIXED"
        elif final_recommendation == "PASS":
            status = "PASS"
        else:
            status = "HOLD"
        return {
            "path": str(path),
            "phase": final.get("phase") or initial.get("phase"),
            "day": final.get("day") or initial.get("day"),
            "status": status,
            "initial_recommendation": initial_recommendation,
            "final_recommendation": final_recommendation,
            "blockers": list(final.get("blockers") or []),
            "actions": [str(dict(item).get("action") or "") for item in actions if str(dict(item).get("action") or "").strip()],
        }
    if payload:
        recommendation = str(payload.get("recommendation") or "UNKNOWN").strip()
        return {
            "path": str(path),
            "phase": payload.get("phase"),
            "day": payload.get("day"),
            "status": "PASS" if recommendation == "PASS" else "HOLD",
            "initial_recommendation": recommendation,
            "final_recommendation": recommendation,
            "blockers": list(payload.get("blockers") or []),
            "actions": [],
        }
    return {
        "path": str(path) if path else None,
        "phase": None,
        "day": None,
        "status": "MISSING",
        "initial_recommendation": "UNKNOWN",
        "final_recommendation": "UNKNOWN",
        "blockers": [],
        "actions": [],
    }


def _trace_coverage_from_opening(
    *,
    latest_day: str,
    opening_paths: list[Path | None],
) -> tuple[Path | None, dict[str, Any]]:
    if not latest_day:
        return None, {}
    for path in opening_paths:
        payload = _read_json(path) if path else {}
        for key in ("final", "initial"):
            phase = dict(payload.get(key) or {})
            trace_coverage = phase.get("trace_coverage")
            if not isinstance(trace_coverage, dict):
                continue
            if _report_day(path, trace_coverage) == latest_day:
                return path, dict(trace_coverage)
    return None, {}


def _derive_opening_status(*, opening: dict[str, dict[str, Any]], escalation_required: bool) -> dict[str, Any]:
    summaries = [dict(item) for item in opening.values() if dict(item).get("status") != "MISSING"]
    latest = summaries[-1] if summaries else {}
    if not summaries:
        severity = "UNKNOWN"
    elif escalation_required and latest and latest.get("final_recommendation") != "PASS":
        severity = "ESCALATE"
    elif any(item.get("status") == "AUTO_FIXED" for item in summaries):
        severity = "AUTO_FIXED"
    elif latest and latest.get("final_recommendation") == "PASS":
        severity = "PASS"
    else:
        severity = "HOLD"
    return {
        "severity": severity,
        "latest_phase": latest.get("phase"),
        "latest_day": latest.get("day"),
        "latest_blockers": list(latest.get("blockers") or []),
        "latest_actions": list(latest.get("actions") or []),
    }


def _normalize_blockers(source: str, values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text:
            out.append(f"{source}:{text}")
    return out


def _pick_priority_task(
    *,
    evidence: dict[str, Any],
    rollout: dict[str, Any],
    trace_coverage: dict[str, Any],
    postclose: dict[str, Any],
) -> dict[str, Any]:
    missing_trace = list(trace_coverage.get("missing_strategy_ids") or [])
    if missing_trace:
        return {
            "id": "qqq_trace_coverage",
            "title": "Restore QQQ live+shadow trace coverage",
            "reason": f"missing trace writers: {', '.join(missing_trace)}",
            "source": "trace_coverage",
            "status": "blocked_until_next_market_session" if "qqq_intraday_001" in missing_trace else "ready",
        }
    evidence_blockers = list(evidence.get("blockers") or [])
    if evidence_blockers:
        return {
            "id": "daily_evidence_blocker",
            "title": "Resolve highest-priority daily evidence blocker",
            "reason": evidence_blockers[0],
            "source": "daily_evidence",
            "status": "ready",
        }
    postclose_blockers = list(postclose.get("blockers") or [])
    if postclose_blockers:
        return {
            "id": "qqq_postclose_gap",
            "title": "Close QQQ postclose evidence gap",
            "reason": postclose_blockers[0],
            "source": "postclose_report",
            "status": "observe_next_session" if "no_entry_candidate_today" in postclose_blockers else "ready",
        }
    if int(rollout.get("paper_clean_days") or 0) < int(rollout.get("min_paper_clean_days") or 3):
        return {
            "id": "paper_clean_days",
            "title": "Accumulate clean paper sessions",
            "reason": f"paper clean days={rollout.get('paper_clean_days')} of {rollout.get('min_paper_clean_days')}",
            "source": "rollout_state",
            "status": "observe_next_session",
        }
    if int(rollout.get("qqq_shadow_observed_day_count") or 0) < 3:
        return {
            "id": "qqq_d3_shadow",
            "title": "Accumulate QQQ D+3 shadow evidence",
            "reason": f"qqq observed days={rollout.get('qqq_shadow_observed_day_count')} of 3",
            "source": "rollout_state",
            "status": "observe_next_session",
        }
    return {
        "id": "maintain_rollout_gates",
        "title": "Maintain rollout gates and wait for explicit live authorization",
        "reason": "no current blockers and cutover remains manually controlled",
        "source": "rollout_state",
        "status": "ready",
    }


def update_rollout_orchestrator_state(
    *,
    repo_root: str | Path | None = None,
    rollout_state_path: str | Path | None = None,
    output_path: str | Path | None = None,
    min_escalation_streak: int = 2,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    rollout_path = Path(rollout_state_path) if rollout_state_path is not None else root / "runtime/state/trading_rollout_state.json"
    if not rollout_path.is_absolute():
        rollout_path = root / rollout_path
    output = Path(output_path) if output_path is not None else root / "runtime/state/trading_rollout_orchestrator_state.json"
    if not output.is_absolute():
        output = root / output

    prior = _read_json(output)
    rollout = _read_json(rollout_path)
    evidence_path = _latest_json(root, "reports/daily_trading_evidence/*.json")
    opening_preopen_path = _latest_json(root, "runtime/state/opening_day/*_preopen_supervisor.json")
    opening_open_path = _latest_json(root, "runtime/state/opening_day/*_open_supervisor.json")
    opening_acceptance_path = _latest_json(root, "runtime/state/opening_day/*_acceptance_supervisor.json")

    evidence = _read_json(evidence_path) if evidence_path else {}
    latest_day = str(rollout.get("last_day") or evidence.get("day") or "").strip()
    preopen_path, preopen = _latest_json_for_day(root, "reports/qqq_kernel_shadow_parity/*_preopen_smoke.json", latest_day)
    trace_path, trace_coverage = _latest_json_for_day(root, "reports/qqq_kernel_shadow_parity/*_trace_coverage.json", latest_day)
    postclose_path, postclose = _latest_json_for_day(root, "reports/qqq_kernel_shadow_parity/*_postclose_report.json", latest_day)
    trace_source = "standalone" if trace_path else "missing"
    if not trace_coverage:
        embedded_trace_path, embedded_trace_coverage = _trace_coverage_from_opening(
            latest_day=latest_day,
            opening_paths=[opening_acceptance_path, opening_open_path, opening_preopen_path],
        )
        if embedded_trace_coverage:
            trace_path = embedded_trace_path
            trace_coverage = embedded_trace_coverage
            trace_source = "opening_supervisor"
    opening = {
        "preopen": _opening_phase_summary(opening_preopen_path),
        "open": _opening_phase_summary(opening_open_path),
        "acceptance": _opening_phase_summary(opening_acceptance_path),
    }

    blockers: list[str] = []
    blockers.extend(_normalize_blockers("daily_evidence", list(evidence.get("blockers") or [])))
    blockers.extend(_normalize_blockers("preopen_smoke", list(preopen.get("blockers") or [])))
    blockers.extend(_normalize_blockers("trace_coverage", list(trace_coverage.get("missing_strategy_ids") or [])))
    blockers.extend(_normalize_blockers("postclose", list(postclose.get("blockers") or [])))
    blockers = sorted(set(blockers))

    priority_task = _pick_priority_task(
        evidence=evidence,
        rollout=rollout,
        trace_coverage=trace_coverage,
        postclose=postclose,
    )

    history = list(prior.get("history") or [])
    history.append(
        {
            "evaluated_at": datetime.now(ET).isoformat(timespec="seconds"),
            "blockers": blockers,
            "priority_task_id": priority_task["id"],
            "priority_task_status": priority_task["status"],
        }
    )
    history = history[-30:]

    streaks: dict[str, int] = {}
    for blocker in blockers:
        streak = 0
        for row in reversed(history):
            if blocker in list(dict(row).get("blockers") or []):
                streak += 1
            else:
                break
        streaks[blocker] = streak
    escalation_blockers = sorted([blocker for blocker, streak in streaks.items() if streak >= int(min_escalation_streak)])
    opening_status = _derive_opening_status(opening=opening, escalation_required=bool(escalation_blockers))
    if opening_status["severity"] == "ESCALATE":
        escalation_blockers = sorted(set(escalation_blockers + _normalize_blockers("opening", opening_status["latest_blockers"])))

    payload = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "latest_day": latest_day or None,
        "last_recommendation": rollout.get("last_recommendation") or evidence.get("recommendation"),
        "current_priority_task": priority_task,
        "blockers": blockers,
        "blocker_streaks": streaks,
        "escalation_required": bool(escalation_blockers),
        "escalation_blockers": escalation_blockers,
        "opening_supervisor": opening,
        "opening_status": opening_status,
        "recent_reports": {
            "daily_evidence": str(evidence_path) if evidence_path else None,
            "preopen_smoke": str(preopen_path) if preopen_path else None,
            "trace_coverage": str(trace_path) if trace_path else None,
            "trace_coverage_source": trace_source,
            "postclose_report": str(postclose_path) if postclose_path else None,
            "opening_preopen": str(opening_preopen_path) if opening_preopen_path else None,
            "opening_open": str(opening_open_path) if opening_open_path else None,
            "opening_acceptance": str(opening_acceptance_path) if opening_acceptance_path else None,
            "rollout_state": str(rollout_path),
        },
        "paper_clean_days": int(rollout.get("paper_clean_days") or 0),
        "qqq_shadow_observed_day_count": int(rollout.get("qqq_shadow_observed_day_count") or 0),
        "live_cutover_allowed": bool(rollout.get("live_cutover_allowed")),
        "history": history,
    }
    for key in ("last_execution", "execution_history", "task_attempts", "last_completed_task", "updated_at"):
        if key in prior:
            payload[key] = prior[key]
    _write_json(output, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update rollout orchestrator state from latest reports")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--rollout-state", default="runtime/state/trading_rollout_state.json")
    parser.add_argument("--output", default="runtime/state/trading_rollout_orchestrator_state.json")
    parser.add_argument("--min-escalation-streak", type=int, default=2)
    args = parser.parse_args(argv)
    payload = update_rollout_orchestrator_state(
        repo_root=args.repo_root or None,
        rollout_state_path=args.rollout_state,
        output_path=args.output,
        min_escalation_streak=args.min_escalation_streak,
    )
    print(
        json.dumps(
            {
                "latest_day": payload["latest_day"],
                "current_priority_task": payload["current_priority_task"]["id"],
                "blockers": payload["blockers"],
                "opening_severity": payload["opening_status"]["severity"],
                "escalation_required": payload["escalation_required"],
                "escalation_blockers": payload["escalation_blockers"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
