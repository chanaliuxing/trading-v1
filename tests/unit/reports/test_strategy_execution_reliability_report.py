from __future__ import annotations

import json
from pathlib import Path

from tool.report.build_strategy_execution_reliability_report import (
    StrategyExecutionReliabilityConfig,
    StrategyExecutionReliabilityDriver,
    build_chains_from_events,
    build_reliability_report,
    build_strategy_execution_reliability_report,
    classify_reliability_driver,
)


def _event(stage: str, ts: str, **kwargs):
    row = {
        "stage": stage,
        "ts": ts,
        "strategy_id": "nvda_option_churn_smoke_001",
        "strategy_instance_id": "nvda-1",
        "symbol": "OPT:NVDA:20260515:900:C",
        "underlying": "NVDA",
        "asset_type": "option",
        "trace_id": "trace-1",
        "decision_id": "decision-1",
        "intent_id": "intent-1",
        "client_order_id": "client-1",
        "lot_id": "lot-1",
        "readiness": {
            "system_health": "healthy",
            "broker_truth_ready": True,
            "position_safety_ready": True,
            "quarantine_clear": True,
            "runtime_fresh": True,
            "manual_recovery_clear": True,
        },
        "option_data": {
            "quote_missing": False,
            "quote_source": "paper_quote",
            "quote_age_ms": 500,
            "contract_policy": "dte_1_7",
        },
    }
    row.update(kwargs)
    return row


def _full_chain(**last_overrides):
    rows = [
        _event(
            "decision_approved",
            "2026-05-12T10:00:00-04:00",
            payload={"signal_ts": "2026-05-12T09:59:59-04:00"},
        ),
        _event("validator_start", "2026-05-12T10:00:01-04:00"),
        _event("validator_accepted", "2026-05-12T10:00:02-04:00"),
        _event("submit_intent", "2026-05-12T10:00:03-04:00"),
        _event("uom_delegate", "2026-05-12T10:00:04-04:00"),
        _event("execution_gate_accept", "2026-05-12T10:00:05-04:00"),
        _event("paper_broker_submit", "2026-05-12T10:00:06-04:00"),
        _event("broker_ack", "2026-05-12T10:00:07-04:00"),
        _event("fill", "2026-05-12T10:00:08-04:00", attribution_status="attributed"),
        _event("pnl_replay", "2026-05-12T10:00:09-04:00", pnl_replay_ready=True),
    ]
    if last_overrides:
        rows[-1].update(last_overrides)
    return rows


def test_reliability_report_builds_full_approved_to_pnl_chain():
    chain = build_chains_from_events(_full_chain())[0]

    assert chain["final_status"] == "pnl_replay_ready"
    assert chain["drop_stage"] == "none"
    assert chain["pnl_replay_ts"] == "2026-05-12T10:00:09-04:00"


def test_reliability_detects_validator_drop():
    rows = [
        _event("decision_approved", "2026-05-12T10:00:00-04:00"),
        _event("validator_rejected", "2026-05-12T10:00:01-04:00", reason="stale_decision:dlb_exceeded"),
    ]
    chain = build_chains_from_events(rows)[0]

    assert chain["final_status"] == "validator_rejected"
    assert chain["drop_stage"] == "validator"


def test_reliability_detects_submit_intent_loss():
    chain = build_chains_from_events([_event("decision_approved", "2026-05-12T10:00:00-04:00")])[0]

    assert chain["final_status"] == "approved_only"
    assert chain["drop_stage"] == "submit_intent"


def test_reliability_detects_uom_delegate_loss():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
        ]
    )[0]

    assert chain["drop_stage"] == "uom_delegate"
    assert chain["uom_route_status"] == "missing"


def test_reliability_detects_execution_gate_drop():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
            _event("uom_delegate", "2026-05-12T10:00:02-04:00"),
            _event("execution_reject", "2026-05-12T10:00:03-04:00", reason="system_health_gate_blocked"),
        ]
    )[0]

    assert chain["final_status"] == "execution_rejected"
    assert chain["drop_stage"] == "execution_gate"


def test_reliability_report_preserves_uom_reject_reason():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
            _event(
                "uom_rejected",
                "2026-05-12T10:00:02-04:00",
                uom_route_status="rejected",
                uom_reject_reason="missing_owner_identity",
                owner_identity_complete=False,
            ),
        ]
    )[0]

    assert chain["final_status"] == "uom_rejected"
    assert chain["drop_stage"] == "uom_delegate"
    assert chain["uom_reject_reason"] == "missing_owner_identity"
    assert chain["owner_identity_complete"] is False


def test_reliability_report_delegated_uom_proceeds_to_next_stage():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
            _event(
                "uom_delegated",
                "2026-05-12T10:00:02-04:00",
                uom_route_status="delegated",
                uom_delegate_target="submit_entry",
            ),
        ]
    )[0]

    assert chain["final_status"] == "uom_delegated"
    assert chain["drop_stage"] == "execution_gate"
    assert chain["uom_delegate_target"] == "submit_entry"


def test_reliability_report_counts_uom_route_status():
    chains = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00", intent_id="intent-1", client_order_id="client-1"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00", intent_id="intent-1", client_order_id="client-1"),
            _event(
                "uom_delegated",
                "2026-05-12T10:00:02-04:00",
                intent_id="intent-1",
                client_order_id="client-1",
                uom_route_status="delegated",
                uom_delegate_target="submit_entry",
            ),
            _event("decision_approved", "2026-05-12T10:01:00-04:00", intent_id="intent-2", client_order_id="client-2"),
            _event("submit_intent", "2026-05-12T10:01:01-04:00", intent_id="intent-2", client_order_id="client-2"),
            _event(
                "uom_rejected",
                "2026-05-12T10:01:02-04:00",
                intent_id="intent-2",
                client_order_id="client-2",
                uom_route_status="rejected",
                uom_reject_reason="no_delegate_registered",
            ),
        ]
    )
    report = build_reliability_report(chains, config=StrategyExecutionReliabilityConfig(min_sample_count=1))

    assert report["uom_route_status_counts"]["delegated"] == 1
    assert report["uom_route_status_counts"]["rejected"] == 1
    assert report["uom_reject_reason_counts"]["no_delegate_registered"] == 1
    assert report["uom_delegate_target_counts"]["submit_entry"] == 1


def test_reliability_report_classifies_missing_uom_as_uom_routing():
    rows = [
        _event(
            "decision_approved",
            f"2026-05-12T10:00:0{i}-04:00",
            trace_id=f"trace-uom-{i}",
            decision_id=f"decision-uom-{i}",
            intent_id=f"intent-uom-{i}",
            client_order_id=f"client-uom-{i}",
        )
        for i in range(5)
    ]
    rows.extend(
        _event(
            "submit_intent",
            f"2026-05-12T10:00:1{i}-04:00",
            trace_id=f"trace-uom-{i}",
            decision_id=f"decision-uom-{i}",
            intent_id=f"intent-uom-{i}",
            client_order_id=f"client-uom-{i}",
        )
        for i in range(5)
    )

    chains = build_chains_from_events(rows)

    assert classify_reliability_driver(chains, min_sample_count=5) == StrategyExecutionReliabilityDriver.UOM_ROUTING.value


def test_reliability_shadow_mode_records_skipped_not_unknown_loss():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
            _event(
                "uom_skipped_shadow",
                "2026-05-12T10:00:02-04:00",
                uom_route_status="skipped",
                uom_delegate_target="shadow_no_broker_submit",
                uom_reject_reason="execution_mode_shadow_no_delegate",
                uom_shadow_mode=True,
            ),
        ]
    )[0]

    assert chain["final_status"] == "shadow_no_broker_submit"
    assert chain["drop_stage"] == "none"
    assert chain["uom_route_status"] == "skipped"


def test_reliability_detects_paper_broker_submit_loss():
    chain = build_chains_from_events(_full_chain()[:6])[0]

    assert chain["final_status"] == "execution_accepted"
    assert chain["drop_stage"] == "paper_broker_submit"


def test_reliability_detects_broker_ack_loss():
    chain = build_chains_from_events(_full_chain()[:7])[0]

    assert chain["final_status"] == "paper_broker_submitted"
    assert chain["drop_stage"] == "broker_ack"


def test_reliability_detects_fill_to_attribution_loss():
    rows = _full_chain()[:9]
    rows[-1]["attribution_status"] = "missing_owner"
    chain = build_chains_from_events(rows)[0]

    assert chain["final_status"] == "filled"
    assert chain["drop_stage"] == "attribution"


def test_reliability_detects_pnl_replay_loss():
    rows = _full_chain()[:9]
    rows[-1]["attribution_status"] = "attributed"
    chain = build_chains_from_events(rows)[0]

    assert chain["final_status"] == "attributed"
    assert chain["drop_stage"] == "pnl_replay"


def test_reliability_computes_signal_age_queue_delay_validator_delay():
    chain = build_chains_from_events(_full_chain())[0]

    assert chain["latency_ms"]["signal_age_at_validator"] == 2000.0
    assert chain["latency_ms"]["queue_delay"] == 1000.0
    assert chain["latency_ms"]["validator_delay"] == 1000.0


def test_reliability_computes_quote_age():
    chain = build_chains_from_events(_full_chain())[0]

    assert chain["latency_ms"]["quote_age_at_validator"] == 500.0
    assert chain["option_data"]["quote_source"] == "paper_quote"


def test_reliability_classifies_quote_missing_as_option_data_policy():
    rows = [
        _event(
            "decision_approved",
            f"2026-05-12T10:00:0{i}-04:00",
            trace_id=f"trace-{i}",
            decision_id=f"decision-{i}",
            intent_id=f"intent-{i}",
            client_order_id=f"client-{i}",
            option_data={"quote_missing": True, "quote_source": None, "quote_age_ms": None},
        )
        for i in range(5)
    ]
    chains = build_chains_from_events(rows)

    assert classify_reliability_driver(chains, min_sample_count=5) == StrategyExecutionReliabilityDriver.OPTION_DATA_POLICY.value


def test_reliability_classifies_system_readiness_hard_blockers():
    rows = [
        _event(
            "decision_approved",
            f"2026-05-12T10:00:0{i}-04:00",
            trace_id=f"trace-{i}",
            decision_id=f"decision-{i}",
            intent_id=f"intent-{i}",
            client_order_id=f"client-{i}",
            readiness={"system_health": "healthy", "broker_truth_ready": False},
        )
        for i in range(5)
    ]
    chains = build_chains_from_events(rows)

    assert classify_reliability_driver(chains, min_sample_count=5) == StrategyExecutionReliabilityDriver.SYSTEM_READINESS.value


def test_reliability_system_health_recovering_is_not_hard_block_alone():
    rows = [
        _event(
            "decision_approved",
            f"2026-05-12T10:00:0{i}-04:00",
            trace_id=f"trace-{i}",
            decision_id=f"decision-{i}",
            intent_id=f"intent-{i}",
            client_order_id=f"client-{i}",
            readiness={
                "system_health": "recovering",
                "broker_truth_ready": True,
                "position_safety_ready": True,
                "quarantine_clear": True,
                "runtime_fresh": True,
                "manual_recovery_clear": True,
            },
        )
        for i in range(5)
    ]
    chains = build_chains_from_events(rows)

    assert classify_reliability_driver(chains, min_sample_count=5) == StrategyExecutionReliabilityDriver.SUBMIT_INTENT_LOSS.value


def test_reliability_report_does_not_change_strategy_parameters():
    report = build_reliability_report([], config=StrategyExecutionReliabilityConfig(min_sample_count=20))

    assert report["classification"]["primary_bottleneck"] == StrategyExecutionReliabilityDriver.INSUFFICIENT_SAMPLE.value
    assert report["recommendations"] == ["collect_more_paper_shadow_execution_samples"]


def test_reliability_report_no_drop_driver_is_not_insufficient_sample():
    chains = build_chains_from_events(_full_chain())
    report = build_reliability_report(chains, config=StrategyExecutionReliabilityConfig(min_sample_count=1, latency_ms_threshold=999999.0))

    assert report["classification"]["primary_bottleneck"] == StrategyExecutionReliabilityDriver.NO_BOTTLENECK.value
    assert report["classification"]["needs_uom_routing_fix"] is False
    assert report["classification"]["needs_paper_broker_fix"] is False


def test_reliability_report_writes_latest_json_and_markdown(tmp_path: Path):
    audit_dir = tmp_path / "runtime" / "audit" / "20260512"
    audit_dir.mkdir(parents=True)
    (audit_dir / "strategy.jsonl").write_text("\n".join(json.dumps(row) for row in _full_chain()) + "\n", encoding="utf-8")
    output_json = tmp_path / "reports" / "strategy_execution_reliability" / "strategy_execution_reliability_latest.json"
    output_md = tmp_path / "docs" / "reports" / "strategy_execution_reliability_20260512.md"

    report = build_strategy_execution_reliability_report(
        repo_root=tmp_path,
        output_json=output_json,
        output_md=output_md,
        day="20260512",
        min_sample_count=1,
    )

    assert output_json.exists()
    assert output_md.exists()
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["sample_count"] == 1
    assert report["sample_count"] == 1
