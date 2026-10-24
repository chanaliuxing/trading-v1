#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tool.query_audit import query_records
from tool.query_state_transition_audit import build_state_transition_chain

DEFAULT_QQQ_OPTION_STRATEGY_ID = "qqq_intraday_option_v1_001"


def _project_state_from_strategy_stages(stages: list[str]) -> dict[str, Any]:
    stage_set = {str(item or "").strip().lower() for item in stages if str(item or "").strip()}
    runtime: list[str] = []
    order: list[str] = []
    flatten: list[str] = []
    protection: list[str] = []

    if "entry_intent_submitted" in stage_set:
        runtime.append("intent_submitted")
        order.append("submitted")
    if any(item in stage_set for item in {"position_reconciled_live", "entered", "protective_stop_submitted", "stop_audit_warning"}):
        runtime.append("live")
    if any(item in stage_set for item in {"flatten_requested", "flatten_intent_requested", "flatten_submitted", "flatten_deduped"}):
        flatten.append("submitted")
        order.append("submitted")
    if "position_reconciled_flat" in stage_set:
        flatten.append("confirmed")
        runtime.append("flat")
    if "protective_stop_submitted" in stage_set:
        protection.append("protected")
        order.append("submitted")
    if "stop_audit_self_heal" in stage_set:
        protection.append("protection_recovery")
    if "stop_loss_missing_kill_switch" in stage_set:
        protection.append("protection_failed")

    def _uniq(values: list[str]) -> list[str]:
        out: list[str] = []
        seen = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    runtime = _uniq(runtime)
    order = _uniq(order)
    flatten = _uniq(flatten)
    protection = _uniq(protection)
    by_machine = {
        "runtime": len(runtime),
        "order": len(order),
        "flatten": len(flatten),
        "protection": len(protection),
    }
    return {
        "runtime_statuses": runtime,
        "order_statuses": order,
        "flatten_statuses": flatten,
        "protection_statuses": protection,
        "by_state_machine": by_machine,
        "used": any(int(v) > 0 for v in by_machine.values()),
    }


def build_qqq_state_shadow_readiness(
    *,
    audit_root: Path,
    date: str,
    strategy_id: str = DEFAULT_QQQ_OPTION_STRATEGY_ID,
    state_instrumentation_start_date: str | None = None,
    source: str = "local",
    clickhouse_table: str | None = None,
) -> dict[str, Any]:
    strategy_rows = query_records(
        audit_root,
        date=date,
        domain="strategy",
        source=source,
        clickhouse_table=clickhouse_table,
    )
    strategy_rows = [row for row in strategy_rows if str(row.get("strategy_id") or "").strip() == str(strategy_id).strip()]
    strategy_record_count = len(strategy_rows)
    strategy_stages = [str(row.get("stage") or "").strip() for row in strategy_rows]
    likely_trade_activity = any(
        any(token in stage.lower() for token in ("entry", "submit", "flatten", "order", "protect", "stop", "exit", "target_hit"))
        for stage in strategy_stages
    )

    all_strategy_payload = build_state_transition_chain(
        audit_root=audit_root,
        strategy_id=None,
        date=date,
        include_inconsistency=True,
        source=source,
        clickhouse_table=clickhouse_table,
    )
    payload = build_state_transition_chain(
        audit_root=audit_root,
        strategy_id=strategy_id,
        date=date,
        include_inconsistency=True,
        source=source,
        clickhouse_table=clickhouse_table,
    )
    rows = list(payload.get("rows") or [])
    summary = dict(payload.get("summary") or {})
    all_strategy_summary = dict(all_strategy_payload.get("summary") or {})
    by_stage = dict(summary.get("by_stage") or {})
    by_state_machine = dict(summary.get("by_state_machine") or {})
    by_strategy_all = dict(all_strategy_summary.get("by_strategy") or {})
    state_records_all_strategies = int(all_strategy_summary.get("record_count", 0) or 0)
    cross_state_inconsistency_count = int(summary.get("cross_state_inconsistency_count", 0) or 0)

    runtime_statuses: list[str] = []
    flatten_statuses: list[str] = []
    protection_statuses: list[str] = []
    order_statuses: list[str] = []
    for row in rows:
        stage = str(row.get("stage") or "").strip()
        status = str(row.get("status") or "").strip().lower()
        if stage == "runtime_transition" and status:
            runtime_statuses.append(status)
        elif stage == "flatten_transition" and status:
            flatten_statuses.append(status)
        elif stage == "protection_transition" and status:
            protection_statuses.append(status)
        elif stage == "order_transition" and status:
            order_statuses.append(status)

    projected = _project_state_from_strategy_stages(strategy_stages) if len(rows) <= 0 and strategy_record_count > 0 else {
        "runtime_statuses": [],
        "order_statuses": [],
        "flatten_statuses": [],
        "protection_statuses": [],
        "by_state_machine": {},
        "used": False,
    }
    projection_used = bool(projected.get("used", False))
    if projection_used:
        runtime_statuses = list(projected.get("runtime_statuses") or runtime_statuses)
        order_statuses = list(projected.get("order_statuses") or order_statuses)
        flatten_statuses = list(projected.get("flatten_statuses") or flatten_statuses)
        protection_statuses = list(projected.get("protection_statuses") or protection_statuses)
        if not by_state_machine:
            by_state_machine = dict(projected.get("by_state_machine") or {})

    reasons: list[str] = []
    is_pre_instrumentation_day = bool(state_instrumentation_start_date) and str(date).strip() < str(state_instrumentation_start_date).strip()
    if state_records_all_strategies <= 0:
        if is_pre_instrumentation_day:
            reasons.append("pre_instrumentation_no_state_audit_records")
        else:
            reasons.append("no_state_audit_records_for_day")
    elif len(rows) <= 0:
        reasons.append("no_state_records_for_target_strategy")
    inactive_strategy_day = (
        len(rows) <= 0
        and strategy_record_count > 0
        and not likely_trade_activity
    )
    if inactive_strategy_day:
        reasons = ["inactive_strategy_day_no_state_transitions"]
    if projection_used and not inactive_strategy_day:
        reasons = [item for item in reasons if item not in {"no_state_audit_records_for_day", "no_state_records_for_target_strategy"}]
        reasons.append("state_projection_from_strategy_audit_used")
    pre_instrumentation_no_state = "pre_instrumentation_no_state_audit_records" in reasons
    required_state_machines = ("runtime", "order", "flatten", "protection")
    if not inactive_strategy_day and not pre_instrumentation_no_state:
        missing_state_machines = [name for name in required_state_machines if int(by_state_machine.get(name, 0) or 0) <= 0]
        if missing_state_machines:
            reasons.append(f"missing_state_machines:{','.join(missing_state_machines)}")
        if cross_state_inconsistency_count > 0:
            reasons.append("cross_state_inconsistency_detected")
        if "intent_submitted" not in runtime_statuses:
            reasons.append("missing_runtime_intent_submitted")
        if "live" not in runtime_statuses:
            reasons.append("missing_runtime_live")
        if flatten_statuses and not any(state in {"confirmed", "rejected", "cancelled", "failed", "quarantined"} for state in flatten_statuses):
            reasons.append("flatten_missing_terminal_state")
        if "protection_failed" in protection_statuses:
            reasons.append("protection_failed_present")
        if "protected" not in protection_statuses:
            reasons.append("missing_protected_state")
        if not any(state in {"submitted", "accepted", "filled"} for state in order_statuses):
            reasons.append("missing_order_progression")

    readiness = {
        "date": date,
        "strategy_id": strategy_id,
        "record_count": len(rows),
        "state_records_all_strategies": state_records_all_strategies,
        "observed_state_strategies": list(sorted(by_strategy_all.keys())),
        "strategy_record_count": strategy_record_count,
        "strategy_stages": strategy_stages,
        "likely_trade_activity": likely_trade_activity,
        "inactive_strategy_day": inactive_strategy_day,
        "state_instrumentation_start_date": state_instrumentation_start_date,
        "is_pre_instrumentation_day": is_pre_instrumentation_day,
        "source": source,
        "clickhouse_table": clickhouse_table,
        "state_projection_used": projection_used,
        "by_state_machine": by_state_machine,
        "by_stage": by_stage,
        "cross_state_inconsistency_count": cross_state_inconsistency_count,
        "runtime_statuses": runtime_statuses,
        "order_statuses": order_statuses,
        "flatten_statuses": flatten_statuses,
        "protection_statuses": protection_statuses,
        "ready_for_shadow_acceptance": len(reasons) == 0,
        "reasons": reasons,
    }
    return readiness


def main() -> int:
    parser = argparse.ArgumentParser(description="Build QQQ state shadow readiness report")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--date", required=True)
    parser.add_argument("--strategy-id", default=DEFAULT_QQQ_OPTION_STRATEGY_ID)
    parser.add_argument("--source", choices=["local", "clickhouse", "auto"], default="local")
    parser.add_argument("--clickhouse-table")
    args = parser.parse_args()
    payload = build_qqq_state_shadow_readiness(
        audit_root=Path(args.root).expanduser(),
        date=str(args.date),
        strategy_id=str(args.strategy_id),
        source=str(args.source),
        clickhouse_table=str(args.clickhouse_table or "").strip() or None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(payload.get("ready_for_shadow_acceptance")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
