from __future__ import annotations

import json
from pathlib import Path

from tool.report.build_qqq_state_shadow_cutover_recommendation import build_qqq_state_shadow_cutover_recommendation


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_qqq_state_shadow_cutover_recommendation_blocks_when_not_enough_clean_days(tmp_path: Path) -> None:
    out_root = tmp_path / "out"
    _write_json(
        out_root / "qqq_state_shadow_batch_report.json",
        {"day_count": 2, "pass_days": 2, "warn_days": 0, "fail_days": 0, "ready_days": 2, "projection_days": 0, "protection_failed_day_count": 0, "cross_state_inconsistency_day_count": 0, "rows": [{"day": "2026-04-01"}, {"day": "2026-04-02"}]},
    )

    report = build_qqq_state_shadow_cutover_recommendation(output_root=out_root, state_instrumentation_start_date="2026-04-01")

    assert report["recommendation"] == "BLOCK"
    assert "insufficient_pass_days_for_cutover_review" in report["reasons"]


def test_build_qqq_state_shadow_cutover_recommendation_allows_review_after_three_clean_days(tmp_path: Path) -> None:
    out_root = tmp_path / "out"
    _write_json(
        out_root / "qqq_state_shadow_batch_report.json",
        {
            "day_count": 3,
            "pass_days": 3,
            "warn_days": 0,
            "fail_days": 0,
            "ready_days": 3,
            "projection_days": 0,
            "protection_failed_day_count": 0,
            "cross_state_inconsistency_day_count": 0,
            "rows": [
                {"day": "2026-04-01", "acceptance_status": "PASS", "ready_for_shadow_acceptance": True, "readiness_reasons": [], "cross_state_inconsistency_count": 0},
                {"day": "2026-04-02", "acceptance_status": "PASS", "ready_for_shadow_acceptance": True, "readiness_reasons": [], "cross_state_inconsistency_count": 0},
                {"day": "2026-04-03", "acceptance_status": "PASS", "ready_for_shadow_acceptance": True, "readiness_reasons": [], "cross_state_inconsistency_count": 0},
            ],
        },
    )

    report = build_qqq_state_shadow_cutover_recommendation(output_root=out_root, state_instrumentation_start_date="2026-04-01")

    assert report["recommendation"] == "SHADOW_PASS_CUTOVER_REVIEW_ALLOWED"
    assert report["cutover_still_blocked"] is False


def test_build_qqq_state_shadow_cutover_recommendation_blocks_without_post_instrumentation_days(tmp_path: Path) -> None:
    out_root = tmp_path / "out"
    _write_json(
        out_root / "qqq_state_shadow_batch_report.json",
        {
            "day_count": 3,
            "pass_days": 0,
            "warn_days": 3,
            "fail_days": 0,
            "ready_days": 0,
            "projection_days": 3,
            "protection_failed_day_count": 0,
            "cross_state_inconsistency_day_count": 0,
            "rows": [
                {"day": "2026-03-31", "acceptance_status": "WARN", "ready_for_shadow_acceptance": False, "readiness_reasons": ["pre_instrumentation_no_state_audit_records"], "cross_state_inconsistency_count": 0},
                {"day": "2026-04-01", "acceptance_status": "WARN", "ready_for_shadow_acceptance": False, "readiness_reasons": ["pre_instrumentation_no_state_audit_records"], "cross_state_inconsistency_count": 0},
                {"day": "2026-04-02", "acceptance_status": "WARN", "ready_for_shadow_acceptance": False, "readiness_reasons": ["pre_instrumentation_no_state_audit_records"], "cross_state_inconsistency_count": 0},
            ],
        },
    )

    report = build_qqq_state_shadow_cutover_recommendation(output_root=out_root, state_instrumentation_start_date="2026-04-03")

    assert report["recommendation"] == "BLOCK"
    assert report["post_instrumentation_day_count"] == 0
    assert "insufficient_post_instrumentation_days" in report["reasons"]
    assert "projection_days_present" in report["reasons"]
