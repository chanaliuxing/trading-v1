#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from tool.report.build_qqq_state_shadow_acceptance import build_qqq_state_shadow_acceptance

DEFAULT_QQQ_OPTION_STRATEGY_ID = "qqq_intraday_option_v1_001"


def build_qqq_state_shadow_batch(
    *,
    audit_root: Path,
    output_root: Path,
    days: list[str],
    strategy_id: str = DEFAULT_QQQ_OPTION_STRATEGY_ID,
    baseline_path: Path,
    state_instrumentation_start_date: str | None = "2026-04-03",
    source: str = "local",
    clickhouse_table: str | None = None,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    reason_counts = Counter()
    pass_days = 0
    warn_days = 0
    fail_days = 0
    ready_days = 0
    cross_state_inconsistency_day_count = 0
    protection_failed_day_count = 0
    projection_days = 0

    for day in list(days or []):
        acceptance = build_qqq_state_shadow_acceptance(
            audit_root=audit_root,
            date=str(day),
            strategy_id=strategy_id,
            baseline_path=baseline_path,
            state_instrumentation_start_date=state_instrumentation_start_date,
            source=source,
            clickhouse_table=clickhouse_table,
        )
        status = str(acceptance.get("acceptance_status") or "FAIL").upper()
        if status == "PASS":
            pass_days += 1
        elif status == "WARN":
            warn_days += 1
        else:
            fail_days += 1
        readiness = dict(acceptance.get("readiness") or {})
        readiness_reasons = [str(item) for item in list(readiness.get("reasons") or []) if str(item).strip()]
        acceptance_reasons = [str(item) for item in list(acceptance.get("reasons") or []) if str(item).strip()]
        all_reasons = acceptance_reasons + readiness_reasons
        reason_counts.update(all_reasons)
        if "state_projection_from_strategy_audit_used" in readiness_reasons:
            projection_days += 1
        if bool(readiness.get("ready_for_shadow_acceptance", False)):
            ready_days += 1
        if int(readiness.get("cross_state_inconsistency_count", 0) or 0) > 0:
            cross_state_inconsistency_day_count += 1
        if "protection_failed_present" in readiness_reasons:
            protection_failed_day_count += 1
        rows.append(
            {
                "day": str(day),
                "acceptance_status": status,
                "cutover_blocked": bool(acceptance.get("cutover_blocked", True)),
                "record_count": int((readiness.get("record_count", 0)) or 0),
                "ready_for_shadow_acceptance": bool(readiness.get("ready_for_shadow_acceptance", False)),
                "cross_state_inconsistency_count": int((readiness.get("cross_state_inconsistency_count", 0)) or 0),
                "readiness_reasons": readiness_reasons,
                "reasons": acceptance_reasons,
                "next_actions": list(acceptance.get("next_actions") or []),
            }
        )

    report = {
        "strategy_id": strategy_id,
        "day_count": len(rows),
        "pass_days": pass_days,
        "warn_days": warn_days,
        "fail_days": fail_days,
        "ready_days": ready_days,
        "projection_days": projection_days,
        "cross_state_inconsistency_day_count": cross_state_inconsistency_day_count,
        "protection_failed_day_count": protection_failed_day_count,
        "reason_counts": dict(sorted(reason_counts.items())),
        "rows": rows,
        "all_days_pass": fail_days == 0 and warn_days == 0 and len(rows) > 0,
    }
    report_path = output_root / "qqq_state_shadow_batch_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build multi-day QQQ state shadow batch acceptance report")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--output-root", default="runtime/state/qqq_state_shadow_acceptance")
    parser.add_argument("--strategy-id", default=DEFAULT_QQQ_OPTION_STRATEGY_ID)
    parser.add_argument("--baseline-path", required=True)
    parser.add_argument("--state-instrumentation-start-date", default="2026-04-03")
    parser.add_argument("--source", choices=["local", "clickhouse", "auto"], default="local")
    parser.add_argument("--clickhouse-table")
    parser.add_argument("--days", nargs="+", required=True)
    args = parser.parse_args()
    payload = build_qqq_state_shadow_batch(
        audit_root=Path(args.root).expanduser(),
        output_root=Path(args.output_root).expanduser(),
        days=[str(item) for item in list(args.days or [])],
        strategy_id=str(args.strategy_id),
        baseline_path=Path(args.baseline_path).expanduser(),
        state_instrumentation_start_date=str(args.state_instrumentation_start_date or "").strip() or None,
        source=str(args.source),
        clickhouse_table=str(args.clickhouse_table or "").strip() or None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(payload.get("all_days_pass")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
