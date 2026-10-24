#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_QQQ_OPTION_STRATEGY_ID = "qqq_intraday_option_v1_001"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_qqq_state_shadow_rollup(*, output_root: Path) -> dict[str, Any]:
    batch = _load_json(output_root / "qqq_state_shadow_batch_report.json")
    recommendation = _load_json(output_root / "qqq_state_shadow_cutover_recommendation.json")

    rows = list(batch.get("rows") or [])
    latest_day = str(rows[-1].get("day")) if rows else None
    latest_status = str(rows[-1].get("acceptance_status")) if rows else None
    reason_counts = dict(batch.get("reason_counts") or {})

    rollup = {
        "strategy_id": str(batch.get("strategy_id") or DEFAULT_QQQ_OPTION_STRATEGY_ID),
        "day_count": int(batch.get("day_count", 0) or 0),
        "latest_day": latest_day,
        "latest_acceptance_status": latest_status,
        "pass_days": int(batch.get("pass_days", 0) or 0),
        "warn_days": int(batch.get("warn_days", 0) or 0),
        "fail_days": int(batch.get("fail_days", 0) or 0),
        "ready_days": int(batch.get("ready_days", 0) or 0),
        "projection_days": int(batch.get("projection_days", 0) or 0),
        "cross_state_inconsistency_day_count": int(batch.get("cross_state_inconsistency_day_count", 0) or 0),
        "protection_failed_day_count": int(batch.get("protection_failed_day_count", 0) or 0),
        "state_instrumentation_start_date": recommendation.get("state_instrumentation_start_date"),
        "post_instrumentation_day_count": int(recommendation.get("post_instrumentation_day_count", 0) or 0),
        "post_instrumentation_pass_days": int(recommendation.get("post_instrumentation_pass_days", 0) or 0),
        "post_instrumentation_warn_days": int(recommendation.get("post_instrumentation_warn_days", 0) or 0),
        "post_instrumentation_fail_days": int(recommendation.get("post_instrumentation_fail_days", 0) or 0),
        "post_instrumentation_ready_days": int(recommendation.get("post_instrumentation_ready_days", 0) or 0),
        "post_instrumentation_projection_days": int(recommendation.get("post_instrumentation_projection_days", 0) or 0),
        "cutover_recommendation": str(recommendation.get("recommendation") or "BLOCK"),
        "cutover_still_blocked": bool(recommendation.get("cutover_still_blocked", True)),
        "top_reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-int(item[1]), str(item[0])))[:10]),
        "next_actions": list(recommendation.get("next_actions") or []),
    }
    report_path = output_root / "qqq_state_shadow_rollup.json"
    report_path.write_text(json.dumps(rollup, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rollup["report_path"] = str(report_path)
    return rollup


def main() -> int:
    parser = argparse.ArgumentParser(description="Build QQQ state shadow rollup summary")
    parser.add_argument("--output-root", default="runtime/state/qqq_state_shadow_acceptance")
    args = parser.parse_args()
    payload = build_qqq_state_shadow_rollup(output_root=Path(args.output_root).expanduser())
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not bool(payload.get("cutover_still_blocked", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
