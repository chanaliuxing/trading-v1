from __future__ import annotations

import json

from tool.report.build_qqq_state_shadow_acceptance import build_qqq_state_shadow_acceptance


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_qqq_state_shadow_acceptance_passes_when_readiness_and_baseline_are_green(tmp_path):
    day_dir = tmp_path / "audit" / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-04-03T10:00:00-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "intent_submitted", "reason": "submit_target", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-03T10:00:01-04:00", "domain": "state", "stage": "order_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "submitted", "reason": "entry", "trace_id": "t1", "payload": {"state_machine": "order"}},
        {"ts": "2026-04-03T10:00:02-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "live", "reason": "fill_confirmed", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-03T10:00:03-04:00", "domain": "state", "stage": "protection_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "protected", "reason": "entry_protective_stop", "trace_id": "t1", "payload": {"state_machine": "protection"}},
        {"ts": "2026-04-03T10:30:00-04:00", "domain": "state", "stage": "flatten_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "submitted", "reason": "target_hit", "trace_id": "t2", "payload": {"state_machine": "flatten"}},
        {"ts": "2026-04-03T10:30:01-04:00", "domain": "state", "stage": "flatten_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "confirmed", "reason": "position_reconciled_flat", "trace_id": "t2", "payload": {"state_machine": "flatten"}},
    ]
    (day_dir / "state-10.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
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

    report = build_qqq_state_shadow_acceptance(
        audit_root=tmp_path / "audit",
        date="2026-04-03",
        baseline_path=baseline_path,
    )

    assert report["acceptance_status"] == "PASS"
    assert report["cutover_blocked"] is True
    assert report["reasons"] == []


def test_build_qqq_state_shadow_acceptance_warns_when_readiness_fails_but_baseline_exists(tmp_path):
    day_dir = tmp_path / "audit" / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-04-03T10:00:00-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "intent_submitted", "reason": "submit_target", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-03T10:00:01-04:00", "domain": "state", "stage": "protection_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "protection_failed", "reason": "audit_self_heal", "trace_id": "t1", "payload": {"state_machine": "protection"}},
    ]
    (day_dir / "state-10.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
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

    report = build_qqq_state_shadow_acceptance(
        audit_root=tmp_path / "audit",
        date="2026-04-03",
        baseline_path=baseline_path,
    )

    assert report["acceptance_status"] == "WARN"
    assert "shadow_readiness_failed" in report["reasons"]


def test_build_qqq_state_shadow_acceptance_fails_without_baseline(tmp_path):
    day_dir = tmp_path / "audit" / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "state-10.jsonl").write_text("", encoding="utf-8")

    report = build_qqq_state_shadow_acceptance(
        audit_root=tmp_path / "audit",
        date="2026-04-03",
        baseline_path=tmp_path / "missing.json",
    )

    assert report["acceptance_status"] == "FAIL"
    assert "missing_baseline_contract" in report["reasons"]


def test_build_qqq_state_shadow_acceptance_warns_on_inactive_strategy_day(tmp_path):
    day_dir = tmp_path / "audit" / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    strategy_row = {
        "ts": "2026-04-03T18:00:00-04:00",
        "domain": "strategy",
        "component": "qqq_intraday_strategy",
        "stage": "position_sync_qty_resolution",
        "severity": "info",
        "strategy_id": "qqq_intraday_option_v1_001",
        "strategy_run_id": "run-1",
        "status": "resolved",
        "payload": {"is_traded_before": False},
    }
    (day_dir / "strategy-18.jsonl").write_text(json.dumps(strategy_row) + "\n", encoding="utf-8")
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

    report = build_qqq_state_shadow_acceptance(
        audit_root=tmp_path / "audit",
        date="2026-04-03",
        baseline_path=baseline_path,
    )

    assert report["acceptance_status"] == "WARN"
    assert "shadow_readiness_failed" in report["reasons"]
    assert "inactive_strategy_day_no_state_transitions" in list(report["readiness"]["reasons"] or [])


def test_build_qqq_state_shadow_acceptance_warns_on_pre_instrumentation_no_state_day(tmp_path):
    day_dir = tmp_path / "audit" / "2026-04-01"
    day_dir.mkdir(parents=True, exist_ok=True)
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

    report = build_qqq_state_shadow_acceptance(
        audit_root=tmp_path / "audit",
        date="2026-04-01",
        baseline_path=baseline_path,
        state_instrumentation_start_date="2026-04-03",
    )

    assert report["acceptance_status"] == "WARN"
    assert "pre_instrumentation_no_state_audit_records" in list(report["readiness"]["reasons"] or [])


def test_build_qqq_state_shadow_acceptance_warns_on_strategy_projection_day(tmp_path):
    day_dir = tmp_path / "audit" / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    strategy_rows = [
        {"ts": "2026-04-03T10:00:00-04:00", "domain": "strategy", "component": "qqq_intraday_strategy", "stage": "entry_intent_submitted", "severity": "info", "strategy_id": "qqq_intraday_option_v1_001"},
        {"ts": "2026-04-03T10:00:01-04:00", "domain": "strategy", "component": "qqq_intraday_strategy", "stage": "protective_stop_submitted", "severity": "info", "strategy_id": "qqq_intraday_option_v1_001"},
        {"ts": "2026-04-03T10:10:00-04:00", "domain": "strategy", "component": "qqq_intraday_strategy", "stage": "flatten_submitted", "severity": "info", "strategy_id": "qqq_intraday_option_v1_001"},
    ]
    (day_dir / "strategy-10.jsonl").write_text("\n".join(json.dumps(row) for row in strategy_rows) + "\n", encoding="utf-8")
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

    report = build_qqq_state_shadow_acceptance(
        audit_root=tmp_path / "audit",
        date="2026-04-03",
        baseline_path=baseline_path,
    )

    assert report["acceptance_status"] == "WARN"
    assert "state_projection_from_strategy_audit_used" in list(report["readiness"]["reasons"] or [])
