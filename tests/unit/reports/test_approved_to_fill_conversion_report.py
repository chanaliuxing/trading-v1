from __future__ import annotations

import json
from pathlib import Path

from tool.report.build_approved_to_fill_conversion_report import (
    ApprovedToFillConfig,
    ApprovedToFillLossDriver,
    build_approved_to_fill_conversion_report,
    build_chains_from_events,
    build_conversion_report,
    classify_conversion_loss,
)


def _event(stage: str, ts: str, **kwargs):
    row = {
        "stage": stage,
        "ts": ts,
        "strategy_id": "vegas",
        "strategy_instance_id": "vegas-1",
        "symbol": "SPY",
        "account_id": "DU123",
        "trace_id": "trace-1",
        "decision_id": "decision-1",
        "intent_id": "intent-1",
        "client_order_id": "client-1",
        "lot_id": "lot-1",
        "order_role": "ENTRY",
        "requested_qty": 10,
    }
    row.update(kwargs)
    return row


def _full_chain(**last_overrides):
    rows = [
        _event("decision_approved", "2026-05-12T10:00:00-04:00", payload={"signal_ts": "2026-05-12T09:59:59-04:00"}),
        _event("submit_intent", "2026-05-12T10:00:01-04:00"),
        _event("reservation_accepted", "2026-05-12T10:00:02-04:00"),
        _event("execution_accept", "2026-05-12T10:00:03-04:00"),
        _event("broker_submit", "2026-05-12T10:00:04-04:00", broker_order_id="101", ib_perm_id="9001"),
        _event("broker_ack", "2026-05-12T10:00:05-04:00", broker_order_id="101", ib_perm_id="9001"),
        _event("fill", "2026-05-12T10:00:06-04:00", fill_qty=10, broker_order_id="101", ib_perm_id="9001", attribution_status="attributed", pnl_replay_ready=True),
    ]
    if last_overrides:
        rows[-1].update(last_overrides)
    return rows


def test_approved_to_fill_builds_chain_from_approved_to_fill():
    chain = build_chains_from_events(_full_chain())[0]

    assert chain["final_status"] == "filled"
    assert chain["drop_stage"] == "none"
    assert chain["owner_identity_complete"] is True
    assert chain["attribution_status"] == "attributed"
    assert chain["pnl_replay_ready"] is True


def test_approved_to_fill_detects_post_approval_drop():
    chain = build_chains_from_events([_event("decision_approved", "2026-05-12T10:00:00-04:00")])[0]

    assert chain["final_status"] == "approved_only"
    assert chain["drop_stage"] == "post_approval"


def test_approved_to_fill_detects_reservation_bottleneck():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
            _event("reservation_rejected", "2026-05-12T10:00:02-04:00", reason="capital_reserved"),
        ]
    )[0]

    assert chain["final_status"] == "reservation_rejected"
    assert chain["drop_stage"] == "reservation"


def test_approved_to_fill_detects_execution_gate_bottleneck():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
            _event("reservation_accepted", "2026-05-12T10:00:02-04:00"),
            _event("execution_reject", "2026-05-12T10:00:03-04:00", reason="system_health_gate_blocked"),
        ]
    )[0]

    assert chain["final_status"] == "execution_rejected"
    assert chain["drop_stage"] == "execution_gate"


def test_approved_to_fill_detects_broker_submit_bottleneck():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
            _event("reservation_accepted", "2026-05-12T10:00:02-04:00"),
            _event("execution_accept", "2026-05-12T10:00:03-04:00"),
        ]
    )[0]

    assert chain["final_status"] == "execution_accepted"
    assert chain["drop_stage"] == "broker_submit"


def test_approved_to_fill_shadow_uom_skip_is_not_broker_submit_bottleneck():
    chain = build_chains_from_events(
        [
            _event("decision_approved", "2026-05-12T10:00:00-04:00"),
            _event("submit_intent", "2026-05-12T10:00:01-04:00"),
            _event(
                "uom_skipped_shadow",
                "2026-05-12T10:00:02-04:00",
                uom_route_status="skipped",
                uom_reject_reason="execution_mode_shadow_no_delegate",
                uom_shadow_mode=True,
            ),
            _event("reservation_accepted", "2026-05-12T10:00:03-04:00"),
            _event("execution_accept", "2026-05-12T10:00:04-04:00"),
        ]
    )[0]
    report = build_conversion_report([chain], config=ApprovedToFillConfig(min_sample_count=1))

    assert chain["final_status"] == "shadow_no_broker_submit"
    assert chain["drop_stage"] == "none"
    assert report["drop_stage_counts"]["broker_submit"] == 0
    assert report["classification"]["is_broker_submission_bottleneck"] is False


def test_approved_to_fill_detects_ack_to_fill_bottleneck():
    chain = build_chains_from_events(_full_chain()[:-1])[0]

    assert chain["final_status"] == "broker_acknowledged"
    assert chain["drop_stage"] == "fill"


def test_approved_to_fill_detects_attribution_bottleneck():
    chain = build_chains_from_events(_full_chain(attribution_status="missing_owner", pnl_replay_ready=False))[0]

    assert chain["final_status"] == "filled"
    assert chain["drop_stage"] == "attribution"
    assert chain["pnl_replay_ready"] is False


def test_approved_to_fill_does_not_correlate_by_symbol_only():
    approved = _event("decision_approved", "2026-05-12T10:00:00-04:00")
    fill = {
        "stage": "fill",
        "ts": "2026-05-12T10:00:05-04:00",
        "symbol": "SPY",
        "fill_qty": 10,
    }
    for key in ("trace_id", "decision_id", "intent_id", "client_order_id"):
        approved.pop(key, None)
    chains = build_chains_from_events([approved, fill])

    assert len(chains) == 2
    approved_chain = next(row for row in chains if row["approved_ts"])
    assert approved_chain["final_status"] == "approved_only"


def test_approved_to_fill_computes_conversion_rates():
    chains = build_chains_from_events(_full_chain())
    report = build_conversion_report(chains, config=ApprovedToFillConfig(min_sample_count=1))

    assert report["conversion_rates"]["approved_to_intent"] == 1.0
    assert report["conversion_rates"]["broker_submit_to_ack"] == 1.0
    assert report["conversion_rates"]["approved_to_fill"] == 1.0


def test_approved_to_fill_computes_latency_percentiles():
    chains = build_chains_from_events(_full_chain())
    report = build_conversion_report(chains, config=ApprovedToFillConfig(min_sample_count=1))

    assert report["latency_ms"]["approved_to_intent"]["p50"] == 1000.0
    assert report["latency_ms"]["approved_to_first_fill"]["max"] == 6000.0


def test_approved_to_fill_handles_partial_fill():
    chain = build_chains_from_events(_full_chain(fill_qty=4, pnl_replay_ready=False))[0]

    assert chain["final_status"] == "partially_filled"
    assert chain["fill_ratio"] == 0.4


def test_approved_to_fill_report_writes_latest_json(tmp_path: Path):
    audit_dir = tmp_path / "runtime" / "audit" / "20260512"
    audit_dir.mkdir(parents=True)
    (audit_dir / "strategy-001.jsonl").write_text("\n".join(json.dumps(row) for row in _full_chain()) + "\n", encoding="utf-8")

    report = build_approved_to_fill_conversion_report(repo_root=tmp_path, day="20260512", min_sample_count=1)
    latest = tmp_path / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json"

    assert latest.exists()
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["sample_count"] == 1
    assert report["sample_count"] == 1


def test_approved_to_fill_does_not_change_strategy_or_execution_parameters():
    chains = build_chains_from_events(_full_chain())
    report = build_conversion_report(chains, config=ApprovedToFillConfig(min_sample_count=20))

    assert report["classification"]["primary_conversion_loss"] == ApprovedToFillLossDriver.INSUFFICIENT_SAMPLE.value
    assert report["recommendations"] == ["collect_more_approved_to_fill_samples"]


def test_approved_to_fill_classifies_bottlenecks():
    reservation = [
        build_chains_from_events(
            [
                _event("decision_approved", f"2026-05-12T10:00:0{i}-04:00", intent_id=f"intent-r-{i}", client_order_id=f"client-r-{i}"),
                _event("submit_intent", f"2026-05-12T10:00:1{i}-04:00", intent_id=f"intent-r-{i}", client_order_id=f"client-r-{i}"),
                _event("reservation_rejected", f"2026-05-12T10:00:2{i}-04:00", intent_id=f"intent-r-{i}", client_order_id=f"client-r-{i}"),
            ]
        )[0]
        for i in range(5)
    ]
    assert classify_conversion_loss(reservation, min_sample_count=5) == ApprovedToFillLossDriver.RESERVATION_BOTTLENECK.value
