from __future__ import annotations

import json
from pathlib import Path

from tool.report.build_qqq_state_shadow_rollup import build_qqq_state_shadow_rollup


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_qqq_state_shadow_rollup_summarizes_batch_and_cutover(tmp_path: Path) -> None:
    out_root = tmp_path / "out"
    _write_json(
        out_root / "qqq_state_shadow_batch_report.json",
        {
            "strategy_id": "qqq_intraday_option_v1_001",
            "day_count": 3,
            "pass_days": 2,
            "warn_days": 1,
            "fail_days": 0,
            "ready_days": 2,
            "projection_days": 1,
            "cross_state_inconsistency_day_count": 1,
            "protection_failed_day_count": 0,
            "reason_counts": {"shadow_readiness_failed": 1, "cross_state_inconsistency_detected": 1},
            "rows": [
                {"day": "2026-04-01", "acceptance_status": "PASS"},
                {"day": "2026-04-02", "acceptance_status": "WARN"},
                {"day": "2026-04-03", "acceptance_status": "PASS"},
            ],
        },
    )
    _write_json(
        out_root / "qqq_state_shadow_cutover_recommendation.json",
        {
            "state_instrumentation_start_date": "2026-04-03",
            "post_instrumentation_day_count": 1,
            "post_instrumentation_pass_days": 1,
            "post_instrumentation_warn_days": 0,
            "post_instrumentation_fail_days": 0,
            "post_instrumentation_ready_days": 1,
            "post_instrumentation_projection_days": 1,
            "recommendation": "BLOCK",
            "cutover_still_blocked": True,
            "next_actions": ["collect_at_least_three_clean_pass_days"],
        },
    )

    report = build_qqq_state_shadow_rollup(output_root=out_root)

    assert report["latest_day"] == "2026-04-03"
    assert report["latest_acceptance_status"] == "PASS"
    assert report["cutover_still_blocked"] is True
    assert report["post_instrumentation_day_count"] == 1
    assert report["projection_days"] == 1
    assert report["post_instrumentation_projection_days"] == 1
    assert report["top_reason_counts"]["shadow_readiness_failed"] == 1
