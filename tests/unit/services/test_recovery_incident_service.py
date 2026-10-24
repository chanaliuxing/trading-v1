from __future__ import annotations

import json
from datetime import datetime

from services.production.recovery_incident_service import RecoveryIncidentService


def test_recovery_incident_upsert_is_one_active_lane(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")

    first = svc.upsert_active(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        qty=1.0,
        reason="broker_position_without_protection_flatten_quarantine_or_manual_lock",
        now_et=now,
        source="position_safety_supervisor",
    )
    second = svc.upsert_active(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        qty=1.0,
        reason="broker_position_without_protection_flatten_quarantine_or_manual_lock",
        now_et=datetime.fromisoformat("2026-05-05T21:11:00-04:00"),
        source="position_safety_supervisor",
    )
    index = svc.write_index(now_et=now)

    assert first["incident_id"] == second["incident_id"]
    assert len(list(tmp_path.glob("incident_*.json"))) == 1
    assert index["active_count"] == 1


def test_recovery_incident_artifacts_include_runtime_fingerprint_and_atomic_tmp_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_START_TIME", "2026-05-05T09:30:00-04:00")
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")

    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=1.0,
        reason="broker_position_without_protection_flatten_quarantine_or_manual_lock",
        now_et=now,
        source="position_safety_supervisor",
    )
    svc.write_index(now_et=now)

    incident = json.loads(next(tmp_path.glob("incident_*.json")).read_text(encoding="utf-8"))
    index = json.loads((tmp_path / "recovery_incidents_latest.json").read_text(encoding="utf-8"))

    for payload in (incident, index):
        assert payload["source_process_pid"] > 0
        assert payload["source_pid"] == payload["source_process_pid"]
        assert payload["process_start_time"] == "2026-05-05T09:30:00-04:00"
        assert "git_head" in payload
        assert "config_hash" in payload
    assert not list(tmp_path.glob("*.tmp"))


def test_recovery_incident_writes_use_artifact_locks(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")

    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=1.0,
        reason="broker_position_without_protection_flatten_quarantine_or_manual_lock",
        now_et=now,
        source="position_safety_supervisor",
    )
    svc.write_index(now_et=now)

    lock_names = {path.name for path in (tmp_path / ".locks").glob("*.lock")}

    assert "incident_QQQ_INTRADAY_001__TQQQ.json.lock" in lock_names
    assert "recovery_incidents_latest.json.lock" in lock_names


def test_recovery_incident_archives_terminal_history_before_new_lane_incident(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=10.0,
        reason="first_incident",
        now_et=now,
        source="position_safety_supervisor",
    )
    svc.resolve_verified_flat(
        nonflat_symbols=set(),
        active_quarantine_symbols=set(),
        now_et=datetime.fromisoformat("2026-05-05T21:11:00-04:00"),
    )
    active = svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=5.0,
        reason="second_incident",
        now_et=datetime.fromisoformat("2026-05-05T21:12:00-04:00"),
        source="position_safety_supervisor",
    )

    history = [
        json.loads(line)
        for line in (tmp_path / "recovery_incident_history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert active["terminal"] is False
    assert active["reason"] == "second_incident"
    assert history[-1]["state"] == "verified_flat"
    assert history[-1]["reason"] == "first_incident"


def test_recovery_incident_tracks_submit_and_verified_flat(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=10.0,
        reason="active_quarantine_nonzero_position_requires_flatten_or_policy_repair",
        now_et=now,
        source="position_safety_supervisor",
        active_quarantine=True,
    )

    submitted = svc.mark_recovery_submitted(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        now_et=now,
        decision_id="position_safety_recovery:2026-05-05:qqq_intraday_001:TQQQ:long",
        side="SELL",
        qty=10.0,
    )
    assert submitted["state"] == "recovery_submitted"
    assert submitted["terminal"] is False
    assert submitted["active_recovery_intent"]["next_action"] == "verify_flat_or_protected"
    assert submitted["active_recovery_intent"]["attempt_count"] == 1

    resolved = svc.resolve_verified_flat(
        nonflat_symbols=set(),
        active_quarantine_symbols=set(),
        now_et=datetime.fromisoformat("2026-05-05T21:12:00-04:00"),
    )
    index = svc.write_index(now_et=now)

    assert resolved[0]["state"] == "verified_flat"
    assert resolved[0]["active_recovery_intent"] is None
    assert resolved[0]["active_quarantine"] is False
    assert resolved[0]["protected_or_flattening"] is False
    assert index["active_count"] == 0
    assert index["terminal_count"] == 1


def test_recovery_incident_resolves_nonflat_when_owner_lane_is_protected(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        qty=1.0,
        reason="active_quarantine_nonzero_position_pending_verified_flatten",
        now_et=now,
        source="position_safety_supervisor",
        active_quarantine=True,
        protected_or_flattening=True,
    )

    unresolved = svc.resolve_verified_flat(
        nonflat_symbols={"NVDA"},
        active_quarantine_symbols=set(),
        protected_symbols=set(),
        now_et=datetime.fromisoformat("2026-05-05T21:11:00-04:00"),
    )
    assert unresolved == []

    resolved = svc.resolve_verified_flat(
        nonflat_symbols={"NVDA"},
        active_quarantine_symbols=set(),
        protected_symbols={"NVDA"},
        now_et=datetime.fromisoformat("2026-05-05T21:12:00-04:00"),
    )
    index = svc.write_index(now_et=now)

    assert resolved[0]["state"] == "verified_protected"
    assert resolved[0]["terminal"] is True
    assert resolved[0]["active_quarantine"] is False
    assert resolved[0]["protected_or_flattening"] is True
    assert resolved[0]["verified_protected_at"] == "2026-05-05T21:12:00-04:00"
    assert index["active_count"] == 0


def test_manual_required_is_terminal(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=10.0,
        reason="active_quarantine_nonzero_position_requires_flatten_or_policy_repair",
        now_et=now,
        source="position_safety_supervisor",
        active_quarantine=True,
    )

    incident = svc.mark_manual_required(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        now_et=now,
        manual_lock_ids=["position_safety_manual_lock:2026-05-05:qqq_intraday_001:TQQQ"],
        reason="broker_position_truth_unavailable",
    )
    index = svc.write_index(now_et=now)

    assert incident["state"] == "manual_required_acknowledged"
    assert incident["terminal"] is True
    assert incident["active_recovery_intent"] is None
    assert incident["active_quarantine"] is False
    assert incident["protected_or_flattening"] is False
    assert index["active_count"] == 0


def test_resolve_source_verified_flat_only_resolves_matching_source(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=10.0,
        reason="platform_eod_flatten_not_verified",
        now_et=now,
        source="platform_eod_supervisor",
    )
    svc.upsert_active(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        qty=1.0,
        reason="broker_position_without_protection_flatten_quarantine_or_manual_lock",
        now_et=now,
        source="position_safety_supervisor",
    )

    resolved = svc.resolve_source_verified_flat(source="platform_eod_supervisor", now_et=now)
    index = svc.write_index(now_et=now)

    assert [item["incident_id"] for item in resolved] == ["recovery_incident:qqq_intraday_001:TQQQ"]
    assert index["active_count"] == 1
    assert any(item["state"] == "verified_flat" for item in index["incidents"])


def test_resolve_symbol_verified_flat_can_scope_by_source(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="runtime_reconciler",
        symbol="QQQ",
        qty=1.0,
        reason="unknown_broker_order_detected",
        now_et=now,
        source="runtime_reconciler_orphan_detected",
        active_quarantine=True,
    )
    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="QQQ",
        qty=1.0,
        reason="platform_eod_flatten_not_verified",
        now_et=now,
        source="platform_eod_supervisor",
        active_quarantine=True,
    )

    resolved = svc.resolve_symbol_verified_flat(
        symbol="QQQ",
        source="runtime_reconciler_orphan_detected",
        now_et=now,
    )
    index = svc.write_index(now_et=now)

    assert [item["incident_id"] for item in resolved] == ["recovery_incident:runtime_reconciler:QQQ"]
    assert index["active_count"] == 1
    assert any(item["source"] == "platform_eod_supervisor" and item["terminal"] is False for item in index["incidents"])


def test_write_index_normalizes_legacy_terminal_quarantine_flags(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    path = tmp_path / "incident_RUNTIME_RECONCILER__TQQQ.json"
    path.write_text(
        """{
  "artifact_type": "recovery_incident",
  "incident_id": "recovery_incident:runtime_reconciler:TQQQ",
  "strategy_id": "runtime_reconciler",
  "symbol": "TQQQ",
  "state": "verified_flat",
  "terminal": true,
  "active_quarantine": true,
  "protected_or_flattening": true,
  "verified_flat_at": "2026-05-05T22:45:30-04:00"
}""",
        encoding="utf-8",
    )

    index = svc.write_index(now_et=datetime.fromisoformat("2026-05-05T22:50:00-04:00"))
    rewritten = svc.get_incident(strategy_id="runtime_reconciler", symbol="TQQQ")

    assert index["active_count"] == 0
    assert index["incidents"][0]["active_quarantine"] is False
    assert index["incidents"][0]["protected_or_flattening"] is False
    assert rewritten["active_quarantine"] is False


def test_recovery_incident_marks_awaiting_market_reopen_intent(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="runtime_reconciler",
        symbol="OPT:NVDA:20260506:207.5:P",
        qty=1.0,
        reason="unknown_broker_order_detected",
        now_et=now,
        source="runtime_reconciler_orphan_detected",
        active_quarantine=True,
    )

    updated = svc.mark_symbol_awaiting_market_reopen(
        symbol="OPT:NVDA:20260506:207.5:P",
        now_et=now,
        qty=1.0,
        reason="manual_flatten_awaiting_option_market_reopen",
        not_before=datetime.fromisoformat("2026-05-06T09:30:00-04:00"),
        trigger_id="platform_eod_flatten:2026-05-05",
        side="SELL",
    )
    index = svc.write_index(now_et=now)

    assert len(updated) == 1
    assert updated[0]["state"] == "awaiting_market_reopen"
    assert updated[0]["protected_or_flattening"] is True
    assert updated[0]["active_recovery_intent"]["next_action"] == "wait_until_market_reopen"
    assert index["active_intent_count"] == 1
    assert index["awaiting_market_reopen_count"] == 1
    assert index["action_queue"][0]["next_action"] == "wait_until_market_reopen"


def test_upsert_active_does_not_downgrade_awaiting_market_reopen(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.mark_awaiting_market_reopen(
        strategy_id="runtime_reconciler",
        symbol="OPT:NVDA:20260506:207.5:P",
        now_et=now,
        qty=1.0,
        reason="manual_flatten_awaiting_option_market_reopen",
        not_before=datetime.fromisoformat("2026-05-06T09:30:00-04:00"),
        trigger_id="platform_eod_flatten:2026-05-05",
        side="SELL",
    )

    updated = svc.upsert_active(
        strategy_id="runtime_reconciler",
        symbol="OPT:NVDA:20260506:207.5:P",
        qty=1.0,
        reason="unknown_broker_order_detected",
        now_et=datetime.fromisoformat("2026-05-05T21:11:00-04:00"),
        source="runtime_reconciler_orphan_detected",
        active_quarantine=True,
        protected_or_flattening=False,
    )

    assert updated["state"] == "awaiting_market_reopen"
    assert updated["protected_or_flattening"] is True
    assert updated["active_quarantine"] is True
    assert updated["active_recovery_intent"]["next_action"] == "wait_until_market_reopen"


def test_recovery_incident_exposes_awaiting_market_reopen_context(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.mark_awaiting_market_reopen(
        strategy_id="runtime_reconciler",
        symbol="OPT:NVDA:20260506:207.5:P",
        now_et=now,
        qty=1.0,
        reason="manual_flatten_awaiting_option_market_reopen",
        not_before=datetime.fromisoformat("2026-05-06T09:30:00-04:00"),
        trigger_id="platform_eod_flatten:2026-05-05",
        side="SELL",
    )

    context = svc.awaiting_market_reopen_context(symbol="OPT:NVDA:20260506:207.5:P")

    assert context["awaiting_market_reopen"] is True
    assert context["awaiting_market_reopen_not_before"] == "2026-05-06T09:30:00-04:00"
    assert context["awaiting_market_reopen_incident_ids"] == [
        "recovery_incident:runtime_reconciler:OPT:NVDA:20260506:207.5:P"
    ]


def test_recovery_incident_retry_backoff_and_cancel_replace_are_artifacted(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=10.0,
        reason="active_quarantine_nonzero_position_requires_flatten_or_policy_repair",
        now_et=now,
        source="position_safety_supervisor",
        active_quarantine=True,
    )
    svc.mark_recovery_submitted(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        now_et=now,
        decision_id="recovery:1",
        side="SELL",
        qty=10.0,
    )
    retry_wait = svc.mark_recovery_attempt_result(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        now_et=now,
        success=False,
        error="broker_order_rejected",
        retry_after_sec=30,
    )

    assert retry_wait["state"] == "recovery_retry_wait"
    assert retry_wait["active_recovery_intent"]["next_action"] == "retry_recovery"
    assert retry_wait["active_recovery_intent"]["last_error"] == "broker_order_rejected"

    due_index = svc.write_index(now_et=datetime.fromisoformat("2026-05-05T21:10:31-04:00"))
    assert due_index["active_intent_count"] == 1
    assert due_index["retry_due_count"] == 1
    assert due_index["action_required_count"] == 1
    assert due_index["action_queue"][0]["next_action"] == "retry_recovery"
    assert due_index["action_queue"][0]["retry_due"] is True
    assert due_index["action_queue"][0]["intent_id"] == "recovery:1"

    cancel_replace = svc.mark_cancel_replace_requested(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        now_et=datetime.fromisoformat("2026-05-05T21:10:32-04:00"),
        reason="stale_recovery_order",
        replacement_decision_id="recovery:2",
    )
    index = svc.write_index(now_et=datetime.fromisoformat("2026-05-05T21:10:33-04:00"))

    assert cancel_replace["state"] == "recovery_cancel_replace_requested"
    assert cancel_replace["cancel_replace_count"] == 1
    assert index["cancel_replace_pending_count"] == 1
    assert index["action_queue"][0]["next_action"] == "cancel_replace_recovery_order"


def test_recovery_incident_attempt_exhaustion_moves_to_manual_required_pending(tmp_path):
    svc = RecoveryIncidentService(artifact_dir=tmp_path)
    now = datetime.fromisoformat("2026-05-05T21:10:00-04:00")
    svc.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=10.0,
        reason="active_quarantine_nonzero_position_requires_flatten_or_policy_repair",
        now_et=now,
        source="position_safety_supervisor",
        active_quarantine=True,
    )
    for attempt in range(3):
        svc.mark_recovery_submitted(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            now_et=datetime.fromisoformat(f"2026-05-05T21:10:0{attempt}-04:00"),
            decision_id=f"recovery:{attempt}",
            side="SELL",
            qty=10.0,
        )
    incident = svc.mark_recovery_attempt_result(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        now_et=datetime.fromisoformat("2026-05-05T21:10:04-04:00"),
        success=False,
        error="broker_reject_exhausted",
        retry_after_sec=30,
    )

    assert incident["state"] == "manual_required_pending"
    assert incident["active_recovery_intent"]["status"] == "retry_exhausted"
    assert incident["active_recovery_intent"]["next_action"] == "manual_required"

    index = svc.write_index(now_et=datetime.fromisoformat("2026-05-05T21:10:05-04:00"))
    assert index["manual_required_pending_count"] == 1
    assert index["action_queue"][0]["next_action"] == "manual_required"
