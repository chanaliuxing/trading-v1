#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_qqq_state_shadow_cutover_recommendation(
    *,
    output_root: Path,
    state_instrumentation_start_date: str | None = "2026-04-03",
) -> dict[str, Any]:
    batch = _load_json(output_root / "qqq_state_shadow_batch_report.json")
    rows = list(batch.get("rows") or [])
    pass_days = int(batch.get("pass_days", 0) or 0)
    warn_days = int(batch.get("warn_days", 0) or 0)
    fail_days = int(batch.get("fail_days", 0) or 0)
    ready_days = int(batch.get("ready_days", 0) or 0)
    projection_days = int(batch.get("projection_days", 0) or 0)
    day_count = int(batch.get("day_count", 0) or 0)
    protection_failed_day_count = int(batch.get("protection_failed_day_count", 0) or 0)
    cross_state_inconsistency_day_count = int(batch.get("cross_state_inconsistency_day_count", 0) or 0)
    start_date = str(state_instrumentation_start_date or "").strip() or None
    post_rows = [row for row in rows if (start_date is None) or (str(row.get("day") or "").strip() >= start_date)]
    post_day_count = len(post_rows)
    post_pass_days = sum(1 for row in post_rows if str(row.get("acceptance_status") or "").upper() == "PASS")
    post_warn_days = sum(1 for row in post_rows if str(row.get("acceptance_status") or "").upper() == "WARN")
    post_fail_days = sum(1 for row in post_rows if str(row.get("acceptance_status") or "").upper() not in {"PASS", "WARN"})
    post_ready_days = sum(1 for row in post_rows if bool(row.get("ready_for_shadow_acceptance", False)))
    post_protection_failed_day_count = sum(
        1 for row in post_rows if "protection_failed_present" in set(str(item) for item in list(row.get("readiness_reasons") or []))
    )
    post_cross_state_inconsistency_day_count = sum(1 for row in post_rows if int(row.get("cross_state_inconsistency_count", 0) or 0) > 0)
    post_projection_days = sum(
        1 for row in post_rows if "state_projection_from_strategy_audit_used" in set(str(item) for item in list(row.get("readiness_reasons") or []))
    )

    recommendation = "BLOCK"
    reasons: list[str] = []
    next_actions: list[str] = []
    if day_count <= 0:
        reasons.append("no_batch_days")
        next_actions.append("run_multi_day_shadow_acceptance_batch")
    if fail_days > 0:
        reasons.append("fail_days_present")
        next_actions.append("fix_failed_days_before_any_cutover_review")
    if warn_days > 0:
        reasons.append("warn_days_present")
        next_actions.append("explain_warn_days_and_collect_more_shadow_evidence")
    if pass_days < 3:
        reasons.append("insufficient_pass_days_for_cutover_review")
        next_actions.append("collect_at_least_three_clean_pass_days")
    if ready_days < day_count:
        reasons.append("readiness_not_green_for_all_days")
        next_actions.append("stabilize_shadow_coverage_before_cutover_review")
    if protection_failed_day_count > 0:
        reasons.append("protection_failures_present")
        next_actions.append("investigate_and_remove_protection_failures")
    if cross_state_inconsistency_day_count > 0:
        reasons.append("cross_state_inconsistencies_present")
        next_actions.append("resolve_cross_state_inconsistencies_before_review")
    if projection_days > 0:
        reasons.append("projection_days_present")
        next_actions.append("replace_projected_days_with_true_state_transition_days")
    if post_day_count < 3:
        reasons.append("insufficient_post_instrumentation_days")
        next_actions.append("collect_at_least_three_post_instrumentation_days")
    if post_pass_days < 3:
        reasons.append("insufficient_post_instrumentation_pass_days")
        next_actions.append("collect_three_clean_pass_days_after_state_instrumentation")
    if post_fail_days > 0:
        reasons.append("post_instrumentation_fail_days_present")
        next_actions.append("fix_post_instrumentation_failures_before_review")
    if post_warn_days > 0:
        reasons.append("post_instrumentation_warn_days_present")
        next_actions.append("resolve_post_instrumentation_warn_days_before_review")
    if post_ready_days < post_day_count:
        reasons.append("post_instrumentation_readiness_not_green")
        next_actions.append("stabilize_post_instrumentation_readiness")
    if post_protection_failed_day_count > 0:
        reasons.append("post_instrumentation_protection_failures_present")
        next_actions.append("remove_post_instrumentation_protection_failures")
    if post_cross_state_inconsistency_day_count > 0:
        reasons.append("post_instrumentation_cross_state_inconsistencies_present")
        next_actions.append("resolve_post_instrumentation_cross_state_inconsistencies")
    if post_projection_days > 0:
        reasons.append("post_instrumentation_projection_days_present")
        next_actions.append("disable_projection_dependency_and_collect_true_state_days")

    if (
        day_count >= 3
        and fail_days == 0
        and warn_days == 0
        and pass_days >= 3
        and ready_days == day_count
        and protection_failed_day_count == 0
        and cross_state_inconsistency_day_count == 0
        and post_day_count >= 3
        and post_fail_days == 0
        and post_warn_days == 0
        and post_pass_days >= 3
        and post_ready_days == post_day_count
        and post_protection_failed_day_count == 0
        and post_cross_state_inconsistency_day_count == 0
        and post_projection_days == 0
    ):
        recommendation = "SHADOW_PASS_CUTOVER_REVIEW_ALLOWED"
        next_actions.append("run_replay_parity_and_failure_injection_before_cutover")

    report = {
        "day_count": day_count,
        "pass_days": pass_days,
        "warn_days": warn_days,
        "fail_days": fail_days,
        "ready_days": ready_days,
        "projection_days": projection_days,
        "protection_failed_day_count": protection_failed_day_count,
        "cross_state_inconsistency_day_count": cross_state_inconsistency_day_count,
        "state_instrumentation_start_date": start_date,
        "post_instrumentation_day_count": post_day_count,
        "post_instrumentation_pass_days": post_pass_days,
        "post_instrumentation_warn_days": post_warn_days,
        "post_instrumentation_fail_days": post_fail_days,
        "post_instrumentation_ready_days": post_ready_days,
        "post_instrumentation_projection_days": post_projection_days,
        "post_instrumentation_protection_failed_day_count": post_protection_failed_day_count,
        "post_instrumentation_cross_state_inconsistency_day_count": post_cross_state_inconsistency_day_count,
        "recommendation": recommendation,
        "cutover_still_blocked": recommendation != "SHADOW_PASS_CUTOVER_REVIEW_ALLOWED",
        "reasons": reasons,
        "next_actions": next_actions,
        "days": [str(row.get("day")) for row in rows],
    }
    report_path = output_root / "qqq_state_shadow_cutover_recommendation.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build QQQ state shadow cutover recommendation")
    parser.add_argument("--output-root", default="runtime/state/qqq_state_shadow_acceptance")
    parser.add_argument("--state-instrumentation-start-date", default="2026-04-03")
    args = parser.parse_args()
    payload = build_qqq_state_shadow_cutover_recommendation(
        output_root=Path(args.output_root).expanduser(),
        state_instrumentation_start_date=str(args.state_instrumentation_start_date or "").strip() or None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if str(payload.get("recommendation")) == "SHADOW_PASS_CUTOVER_REVIEW_ALLOWED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
