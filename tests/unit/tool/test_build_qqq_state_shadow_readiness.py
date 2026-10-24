from __future__ import annotations

import json

from tool.report.build_qqq_state_shadow_readiness import build_qqq_state_shadow_readiness


def test_build_qqq_state_shadow_readiness_accepts_complete_day(tmp_path):
    day_dir = tmp_path / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "state-10.jsonl"
    rows = [
        {
            "ts": "2026-04-03T10:00:00-04:00",
            "domain": "state",
            "stage": "runtime_transition",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "intent_submitted",
            "reason": "submit_target",
            "trace_id": "trace-1",
            "payload": {"state_machine": "runtime"},
        },
        {
            "ts": "2026-04-03T10:00:01-04:00",
            "domain": "state",
            "stage": "order_transition",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "submitted",
            "reason": "qqq_intraday_entry",
            "trace_id": "trace-1",
            "payload": {"state_machine": "order"},
        },
        {
            "ts": "2026-04-03T10:00:02-04:00",
            "domain": "state",
            "stage": "runtime_transition",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "live",
            "reason": "fill_confirmed",
            "trace_id": "trace-1",
            "payload": {"state_machine": "runtime"},
        },
        {
            "ts": "2026-04-03T10:00:03-04:00",
            "domain": "state",
            "stage": "protection_transition",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "protected",
            "reason": "entry_protective_stop",
            "trace_id": "trace-1",
            "payload": {"state_machine": "protection"},
        },
        {
            "ts": "2026-04-03T10:30:00-04:00",
            "domain": "state",
            "stage": "flatten_transition",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "submitted",
            "reason": "target_hit",
            "trace_id": "trace-2",
            "payload": {"state_machine": "flatten"},
        },
        {
            "ts": "2026-04-03T10:30:01-04:00",
            "domain": "state",
            "stage": "flatten_transition",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "confirmed",
            "reason": "position_reconciled_flat",
            "trace_id": "trace-2",
            "payload": {"state_machine": "flatten"},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    payload = build_qqq_state_shadow_readiness(audit_root=tmp_path, date="2026-04-03")

    assert payload["ready_for_shadow_acceptance"] is True
    assert payload["cross_state_inconsistency_count"] == 0
    assert payload["by_state_machine"]["runtime"] == 2
    assert payload["by_state_machine"]["protection"] == 1


def test_build_qqq_state_shadow_readiness_flags_missing_machine_and_failed_protection(tmp_path):
    day_dir = tmp_path / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "state-10.jsonl"
    rows = [
        {
            "ts": "2026-04-03T10:00:00-04:00",
            "domain": "state",
            "stage": "runtime_transition",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "intent_submitted",
            "reason": "submit_target",
            "trace_id": "trace-1",
            "payload": {"state_machine": "runtime"},
        },
        {
            "ts": "2026-04-03T10:00:01-04:00",
            "domain": "state",
            "stage": "protection_transition",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "protection_failed",
            "reason": "audit_self_heal",
            "trace_id": "trace-1",
            "payload": {"state_machine": "protection"},
        },
        {
            "ts": "2026-04-03T10:00:02-04:00",
            "domain": "state",
            "stage": "cross_state_inconsistency_detected",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "TQQQ",
            "status": "warning",
            "reason": "runtime_flat_conflicts_with_owned_live_order",
            "trace_id": "trace-x",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    payload = build_qqq_state_shadow_readiness(audit_root=tmp_path, date="2026-04-03")

    assert payload["ready_for_shadow_acceptance"] is False
    assert "missing_state_machines:order,flatten" in payload["reasons"]
    assert "cross_state_inconsistency_detected" in payload["reasons"]
    assert "protection_failed_present" in payload["reasons"]


def test_build_qqq_state_shadow_readiness_flags_no_state_audit_records(tmp_path):
    day_dir = tmp_path / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)

    payload = build_qqq_state_shadow_readiness(audit_root=tmp_path, date="2026-04-03")

    assert payload["ready_for_shadow_acceptance"] is False
    assert payload["state_records_all_strategies"] == 0
    assert "no_state_audit_records_for_day" in payload["reasons"]


def test_build_qqq_state_shadow_readiness_detects_inactive_strategy_day(tmp_path):
    day_dir = tmp_path / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    strategy_path = day_dir / "strategy-18.jsonl"
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
    strategy_path.write_text(json.dumps(strategy_row) + "\n", encoding="utf-8")

    payload = build_qqq_state_shadow_readiness(audit_root=tmp_path, date="2026-04-03")

    assert payload["ready_for_shadow_acceptance"] is False
    assert payload["inactive_strategy_day"] is True
    assert payload["likely_trade_activity"] is False
    assert payload["reasons"] == ["inactive_strategy_day_no_state_transitions"]


def test_build_qqq_state_shadow_readiness_uses_strategy_projection_when_state_missing(tmp_path):
    day_dir = tmp_path / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    strategy_rows = [
        {
            "ts": "2026-04-03T10:00:00-04:00",
            "domain": "strategy",
            "component": "qqq_intraday_strategy",
            "stage": "entry_intent_submitted",
            "severity": "info",
            "strategy_id": "qqq_intraday_option_v1_001",
        },
        {
            "ts": "2026-04-03T10:00:01-04:00",
            "domain": "strategy",
            "component": "qqq_intraday_strategy",
            "stage": "protective_stop_submitted",
            "severity": "info",
            "strategy_id": "qqq_intraday_option_v1_001",
        },
        {
            "ts": "2026-04-03T10:10:00-04:00",
            "domain": "strategy",
            "component": "qqq_intraday_strategy",
            "stage": "flatten_submitted",
            "severity": "info",
            "strategy_id": "qqq_intraday_option_v1_001",
        },
    ]
    (day_dir / "strategy-10.jsonl").write_text("\n".join(json.dumps(row) for row in strategy_rows) + "\n", encoding="utf-8")

    payload = build_qqq_state_shadow_readiness(audit_root=tmp_path, date="2026-04-03")

    assert payload["state_projection_used"] is True
    assert "state_projection_from_strategy_audit_used" in payload["reasons"]
    assert "runtime" in payload["by_state_machine"]
    assert "order" in payload["by_state_machine"]
