from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[2]


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_if_exists(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _load_json(path)
    except Exception:
        return {}


def build_option_launch_go_no_go_gate(
    *,
    platform_report: Dict[str, Any],
    rollout_report: Dict[str, Any],
    active_evidence_report: Dict[str, Any],
    qqq_cutover_report: Dict[str, Any],
) -> Dict[str, Any]:
    blockers: List[str] = []

    if bool(platform_report.get("platform_admission_blocked", True)):
        blockers.append("platform_admission_blocked")
    if str(rollout_report.get("recommendation") or "") != "ROLLOUT_CONTRACT_READY":
        blockers.append("rollout_contract_not_ready")
    if str(active_evidence_report.get("recommendation") or "") != "ACTIVE_STRATEGIES_EVIDENCE_READY":
        blockers.append("active_strategy_evidence_not_ready")

    if bool(qqq_cutover_report.get("cutover_still_blocked", True)):
        blockers.append("qqq_cutover_blocked")
    if int(qqq_cutover_report.get("post_instrumentation_day_count") or 0) < 3:
        blockers.append("qqq_insufficient_post_instrumentation_days")
    if int(qqq_cutover_report.get("post_instrumentation_pass_days") or 0) < 3:
        blockers.append("qqq_insufficient_post_instrumentation_pass_days")
    if int(qqq_cutover_report.get("post_instrumentation_fail_days") or 0) > 0:
        blockers.append("qqq_post_instrumentation_fail_days_present")

    strategy_rows = {
        str(row.get("strategy_id") or ""): row
        for row in list(active_evidence_report.get("strategies") or [])
        if isinstance(row, dict)
    }
    spy_row = strategy_rows.get("spy_1dte_ma10_reject_0324_001") or {}
    spy_reasons = {str(x) for x in list(spy_row.get("reasons") or [])}
    if "spy_replay_parity_no_real_matched_day" in spy_reasons:
        blockers.append("spy_replay_not_closed")
    if "spy_live_shadow_no_matched_entries" in spy_reasons:
        blockers.append("spy_live_shadow_not_closed")

    qqq_row = strategy_rows.get("qqq_intraday_option_v1_001") or {}
    if "qqq_state_shadow_cutover_blocked" in {str(x) for x in list(qqq_row.get("reasons") or [])}:
        blockers.append("qqq_state_shadow_not_closed")

    checklist = [
        {
            "item": "platform_admission_green",
            "pass": not bool(platform_report.get("platform_admission_blocked", True)),
        },
        {
            "item": "rollout_contract_green",
            "pass": str(rollout_report.get("recommendation") or "") == "ROLLOUT_CONTRACT_READY",
        },
        {
            "item": "active_strategy_evidence_green",
            "pass": str(active_evidence_report.get("recommendation") or "") == "ACTIVE_STRATEGIES_EVIDENCE_READY",
        },
        {
            "item": "qqq_post_instrumentation_days_ge_3",
            "pass": int(qqq_cutover_report.get("post_instrumentation_day_count") or 0) >= 3,
        },
        {
            "item": "qqq_post_instrumentation_pass_days_ge_3",
            "pass": int(qqq_cutover_report.get("post_instrumentation_pass_days") or 0) >= 3,
        },
        {
            "item": "qqq_post_instrumentation_fail_days_eq_0",
            "pass": int(qqq_cutover_report.get("post_instrumentation_fail_days") or 0) == 0,
        },
        {
            "item": "qqq_cutover_unblocked",
            "pass": not bool(qqq_cutover_report.get("cutover_still_blocked", True)),
        },
        {
            "item": "spy_replay_parity_closed",
            "pass": "spy_replay_parity_no_real_matched_day" not in spy_reasons,
        },
        {
            "item": "spy_live_shadow_closed",
            "pass": "spy_live_shadow_no_matched_entries" not in spy_reasons,
        },
    ]

    recommendation = "GO" if not blockers else "BLOCK"
    return {
        "generated_at": datetime.now().isoformat(),
        "recommendation": recommendation,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "checklist": checklist,
        "platform_recommendation": str(platform_report.get("recommendation") or ""),
        "rollout_recommendation": str(rollout_report.get("recommendation") or ""),
        "active_evidence_recommendation": str(active_evidence_report.get("recommendation") or ""),
        "qqq_cutover_recommendation": str(qqq_cutover_report.get("recommendation") or ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build option launch GO/BLOCK gate")
    parser.add_argument("--platform-report", default=str(ROOT / "runtime" / "state" / "option_platform_admission_gate.json"))
    parser.add_argument("--rollout-report", default=str(ROOT / "runtime" / "state" / "option_strategy_rollout_evidence_report.json"))
    parser.add_argument(
        "--active-evidence-report",
        default=str(ROOT / "runtime" / "state" / "option_active_strategy_evidence_status.json"),
    )
    parser.add_argument(
        "--qqq-cutover-report",
        default=str(ROOT / "runtime" / "state" / "qqq_state_shadow_acceptance" / "qqq_state_shadow_cutover_recommendation.json"),
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    report = build_option_launch_go_no_go_gate(
        platform_report=_load_json_if_exists(Path(args.platform_report).expanduser()),
        rollout_report=_load_json_if_exists(Path(args.rollout_report).expanduser()),
        active_evidence_report=_load_json_if_exists(Path(args.active_evidence_report).expanduser()),
        qqq_cutover_report=_load_json_if_exists(Path(args.qqq_cutover_report).expanduser()),
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        return
    print(payload)


if __name__ == "__main__":
    main()
