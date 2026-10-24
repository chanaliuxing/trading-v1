from __future__ import annotations

import json
from pathlib import Path

from tool.report.build_qqq_state_shadow_batch import build_qqq_state_shadow_batch


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_day(root: Path, day: str, rows: list[dict]) -> None:
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "state-10.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _baseline(path: Path) -> None:
    _write_json(
        path,
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


def test_build_qqq_state_shadow_batch_counts_pass_warn_fail(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    baseline_path = tmp_path / "baseline.json"
    _baseline(baseline_path)
    green_rows = [
        {"ts": "2026-04-01T10:00:00-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "intent_submitted", "reason": "submit_target", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-01T10:00:01-04:00", "domain": "state", "stage": "order_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "submitted", "reason": "entry", "trace_id": "t1", "payload": {"state_machine": "order"}},
        {"ts": "2026-04-01T10:00:02-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "live", "reason": "fill_confirmed", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-01T10:00:03-04:00", "domain": "state", "stage": "protection_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "protected", "reason": "stop", "trace_id": "t1", "payload": {"state_machine": "protection"}},
        {"ts": "2026-04-01T10:30:00-04:00", "domain": "state", "stage": "flatten_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "submitted", "reason": "exit", "trace_id": "t2", "payload": {"state_machine": "flatten"}},
        {"ts": "2026-04-01T10:30:01-04:00", "domain": "state", "stage": "flatten_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "confirmed", "reason": "flat", "trace_id": "t2", "payload": {"state_machine": "flatten"}},
    ]
    warn_rows = [
        {"ts": "2026-04-02T10:00:00-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "intent_submitted", "reason": "submit_target", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
    ]
    _write_day(audit_root, "2026-04-01", green_rows)
    _write_day(audit_root, "2026-04-02", warn_rows)

    report = build_qqq_state_shadow_batch(
        audit_root=audit_root,
        output_root=tmp_path / "out",
        days=["2026-04-01", "2026-04-02"],
        baseline_path=baseline_path,
    )

    assert report["pass_days"] == 1
    assert report["warn_days"] == 1
    assert report["fail_days"] == 0
    assert report["ready_days"] == 1
    assert report["reason_counts"]["shadow_readiness_failed"] == 1
    assert report["projection_days"] == 0
