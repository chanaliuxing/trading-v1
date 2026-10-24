#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tool.report.build_qqq_state_shadow_readiness import build_qqq_state_shadow_readiness

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QQQ_OPTION_STRATEGY_ID = "qqq_intraday_option_v1_001"
DEFAULT_BASELINE_PATH = ROOT / "docs" / "baselines" / "qqq_intraday_option_v1_001_replica_baseline.json"
LEGACY_BASELINE_PATH = ROOT / "docs" / "baselines" / "qqq_intraday_001_replica_baseline.json"
DEFAULT_STATE_INSTRUMENTATION_START_DATE = "2026-04-03"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_baseline_path(path: Path) -> Path:
    if path.exists():
        return path
    if path == DEFAULT_BASELINE_PATH and LEGACY_BASELINE_PATH.exists():
        return LEGACY_BASELINE_PATH
    return path


def build_qqq_state_shadow_acceptance(
    *,
    audit_root: Path,
    date: str,
    strategy_id: str = DEFAULT_QQQ_OPTION_STRATEGY_ID,
    baseline_path: Path = DEFAULT_BASELINE_PATH,
    state_instrumentation_start_date: str | None = DEFAULT_STATE_INSTRUMENTATION_START_DATE,
    source: str = "local",
    clickhouse_table: str | None = None,
) -> dict[str, Any]:
    baseline_path = _resolve_baseline_path(baseline_path)
    readiness = build_qqq_state_shadow_readiness(
        audit_root=audit_root,
        date=date,
        strategy_id=strategy_id,
        state_instrumentation_start_date=state_instrumentation_start_date,
        source=source,
        clickhouse_table=clickhouse_table,
    )

    baseline_exists = baseline_path.exists()
    baseline = _load_json(baseline_path) if baseline_exists else {}
    behavior_freeze_contract = dict(baseline.get("behavior_freeze_contract") or {})
    upgrade_contract = dict(baseline.get("state_machine_upgrade_contract") or {})

    required_machines = tuple(str(item).strip() for item in list(upgrade_contract.get("independent_state_machines") or []) if str(item).strip())
    required_by_baseline = bool(behavior_freeze_contract.get("shadow_required_before_cutover", False))
    last_cutover_policy = str(behavior_freeze_contract.get("qqq_cutover_policy") or "").strip()

    reasons: list[str] = []
    readiness_reasons = list(readiness.get("reasons") or [])
    inactive_day = "inactive_strategy_day_no_state_transitions" in readiness_reasons
    pre_instrumentation_day = "pre_instrumentation_no_state_audit_records" in readiness_reasons
    projected_day = "state_projection_from_strategy_audit_used" in readiness_reasons
    if not baseline_exists:
        reasons.append("missing_baseline_contract")
    if required_by_baseline and not bool(readiness.get("ready_for_shadow_acceptance", False)):
        reasons.append("shadow_readiness_failed")
    if required_machines:
        observed = set(dict(readiness.get("by_state_machine") or {}).keys())
        missing = [item for item in required_machines if item not in observed]
        if missing:
            reasons.append(f"baseline_required_state_machines_missing:{','.join(missing)}")
    if last_cutover_policy and last_cutover_policy != "last_strategy_to_cut_over":
        reasons.append("unexpected_cutover_policy")

    if not baseline_exists:
        acceptance_status = "FAIL"
    elif inactive_day or pre_instrumentation_day or projected_day:
        acceptance_status = "WARN"
    elif bool(readiness.get("ready_for_shadow_acceptance", False)):
        acceptance_status = "PASS"
    elif int(readiness.get("record_count", 0) or 0) > 0:
        acceptance_status = "WARN"
    else:
        acceptance_status = "FAIL"

    next_actions: list[str] = []
    if acceptance_status != "PASS":
        next_actions.extend(list(readiness.get("reasons") or []))
    if not baseline_exists:
        next_actions.append("restore_or_regenerate_baseline_contract")
    if acceptance_status == "PASS":
        next_actions.append("continue_multi_day_shadow_collection")
        next_actions.append("run_replay_and_parity_before_any_cutover_decision")
    else:
        next_actions.append("fix_shadow_state_gaps_before_shadow_acceptance")

    report = {
        "date": date,
        "strategy_id": strategy_id,
        "acceptance_status": acceptance_status,
        "cutover_blocked": True,
        "baseline_path": str(baseline_path),
        "baseline_exists": baseline_exists,
        "baseline_shadow_required_before_cutover": required_by_baseline,
        "baseline_cutover_policy": last_cutover_policy or None,
        "baseline_required_state_machines": list(required_machines),
        "readiness": readiness,
        "state_instrumentation_start_date": state_instrumentation_start_date,
        "source": source,
        "clickhouse_table": clickhouse_table,
        "reasons": reasons,
        "next_actions": next_actions,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build QQQ state shadow acceptance report")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--date", required=True)
    parser.add_argument("--strategy-id", default=DEFAULT_QQQ_OPTION_STRATEGY_ID)
    parser.add_argument("--baseline-path", default=str(DEFAULT_BASELINE_PATH))
    parser.add_argument("--state-instrumentation-start-date", default=DEFAULT_STATE_INSTRUMENTATION_START_DATE)
    parser.add_argument("--source", choices=["local", "clickhouse", "auto"], default="local")
    parser.add_argument("--clickhouse-table")
    args = parser.parse_args()
    payload = build_qqq_state_shadow_acceptance(
        audit_root=Path(args.root).expanduser(),
        date=str(args.date),
        strategy_id=str(args.strategy_id),
        baseline_path=Path(args.baseline_path).expanduser(),
        state_instrumentation_start_date=str(args.state_instrumentation_start_date or "").strip() or None,
        source=str(args.source),
        clickhouse_table=str(args.clickhouse_table or "").strip() or None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if str(payload.get("acceptance_status")) == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
