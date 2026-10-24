from __future__ import annotations

import json
from pathlib import Path

from tool.replay.run_qqq_state_shadow_acceptance import run_qqq_state_shadow_acceptance


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_run_qqq_state_shadow_acceptance_writes_report(tmp_path: Path) -> None:
    audit_day = tmp_path / "audit" / "2026-04-03"
    audit_day.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-04-03T10:00:00-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "intent_submitted", "reason": "submit_target", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-03T10:00:01-04:00", "domain": "state", "stage": "order_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "submitted", "reason": "entry", "trace_id": "t1", "payload": {"state_machine": "order"}},
        {"ts": "2026-04-03T10:00:02-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "live", "reason": "fill_confirmed", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-03T10:00:03-04:00", "domain": "state", "stage": "protection_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "protected", "reason": "entry_protective_stop", "trace_id": "t1", "payload": {"state_machine": "protection"}},
        {"ts": "2026-04-03T10:30:00-04:00", "domain": "state", "stage": "flatten_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "submitted", "reason": "target_hit", "trace_id": "t2", "payload": {"state_machine": "flatten"}},
        {"ts": "2026-04-03T10:30:01-04:00", "domain": "state", "stage": "flatten_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "confirmed", "reason": "position_reconciled_flat", "trace_id": "t2", "payload": {"state_machine": "flatten"}},
    ]
    (audit_day / "state-10.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    _write_json(
        baseline_path,
        {
            "behavior_freeze_contract": {
                "shadow_required_before_cutover": True,
                "qqq_cutover_policy": "last_strategy_to_cut_over",
            },
            "state_machine_upgrade_contract": {
                "independent_state_machines": ["runtime", "order", "flatten", "protection"],
            },
        },
    )

    report = run_qqq_state_shadow_acceptance(
        audit_root=tmp_path / "audit",
        output_root=tmp_path / "out",
        date="2026-04-03",
        baseline_path=baseline_path,
    )

    assert report["acceptance_status"] == "PASS"
    assert Path(str(report["report_path"])).exists()
