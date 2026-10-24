from __future__ import annotations

import json
from pathlib import Path

from tool.report.build_vegas_dlb_edge_measurement_report import (
    VegasMeasurementConfig,
    VegasRejectionDriver,
    build_measurement_report,
    build_vegas_dlb_edge_measurement_report,
    classify_rejection_driver,
    measurement_from_audit_row,
)


SID = "vegas_hma_setup_trigger_risk_v12_spy_001"


def _row(**overrides):
    base = {
        "strategy_id": SID,
        "symbol": "SPY",
        "stage": "validator",
        "status": "rejected",
        "reason": "edge_insufficient:50.00<=cost:55.00",
        "ts": "2026-05-12T10:00:02-04:00",
        "payload": {
            "signal_ts": "2026-05-12T10:00:00-04:00",
            "decision_ts": "2026-05-12T10:00:01-04:00",
            "validation_ts": "2026-05-12T10:00:02-04:00",
            "submit_intent_ts": None,
            "dlb_ms": 2000,
            "ttl_ms": 2000,
        },
    }
    base.update(overrides)
    return base


def test_vegas_measurement_computes_latency_buckets():
    rec = measurement_from_audit_row(_row(reason="approved", status="approved"), VegasMeasurementConfig())
    report = build_measurement_report([rec], config=VegasMeasurementConfig(min_sample_count=1))

    assert rec["signal_to_decision_ms"] == 1000.0
    assert rec["decision_to_validation_ms"] == 1000.0
    assert rec["signal_to_validation_ms"] == 2000.0
    assert report["latency_ms"]["signal_to_validation"]["p50"] == 2000.0


def test_vegas_measurement_handles_missing_timestamps():
    rec = measurement_from_audit_row(
        {
            "strategy_id": SID,
            "symbol": "SPY",
            "stage": "validator",
            "status": "rejected",
            "reason": "validator_rejected",
            "payload": {},
        },
        VegasMeasurementConfig(),
    )

    assert rec["signal_ts"] is None
    assert rec["signal_to_validation_ms"] is None
    assert rec["submit_intent_ts"] is None


def test_vegas_measurement_counts_dlb_breaches():
    rec = measurement_from_audit_row(
        _row(
            reason="stale_decision:dlb_exceeded",
            payload={
                "signal_ts": "2026-05-12T10:00:00-04:00",
                "validation_ts": "2026-05-12T10:00:03-04:00",
                "dlb_ms": 2000,
            },
        ),
        VegasMeasurementConfig(),
    )
    report = build_measurement_report([rec], config=VegasMeasurementConfig(min_sample_count=1))

    assert rec["dlb_exceeded"] is True
    assert report["dlb"]["breach_count"] == 1
    assert report["status_counts"]["stale_decision"] == 1


def test_vegas_measurement_counts_edge_insufficient():
    rec = measurement_from_audit_row(_row(), VegasMeasurementConfig())
    report = build_measurement_report([rec], config=VegasMeasurementConfig(min_sample_count=1))

    assert rec["final_status"] == "edge_insufficient"
    assert rec["edge_value"] == 50.0
    assert rec["cost_value"] == 55.0
    assert rec["edge_minus_cost"] == -5.0
    assert report["edge_cost"]["edge_insufficient_count"] == 1


def test_vegas_measurement_classifies_latency_systemic():
    records = []
    for idx in range(5):
        records.append(
            measurement_from_audit_row(
                _row(
                    event_id=f"lat-{idx}",
                    reason="stale_decision:dlb_exceeded",
                    payload={
                        "signal_ts": "2026-05-12T10:00:00-04:00",
                        "validation_ts": "2026-05-12T10:00:03-04:00",
                        "dlb_ms": 2000,
                    },
                ),
                VegasMeasurementConfig(),
            )
        )

    assert classify_rejection_driver(records, min_sample_count=5) == VegasRejectionDriver.LATENCY_DLB_SYSTEMIC.value


def test_vegas_measurement_classifies_edge_cost_systemic():
    records = [measurement_from_audit_row(_row(event_id=f"edge-{idx}"), VegasMeasurementConfig()) for idx in range(5)]

    assert classify_rejection_driver(records, min_sample_count=5) == VegasRejectionDriver.EDGE_COST_SYSTEMIC.value


def test_vegas_measurement_no_rejections_is_not_systemic_or_tuning_signal():
    records = [
        measurement_from_audit_row(
            _row(
                event_id=f"approved-{idx}",
                status="approved",
                reason="approved",
                payload={
                    "signal_ts": "2026-05-12T10:00:00-04:00",
                    "decision_ts": "2026-05-12T10:00:00.050000-04:00",
                    "validation_ts": "2026-05-12T10:00:00.100000-04:00",
                    "dlb_ms": 2000,
                    "ttl_ms": 2000,
                },
            ),
            VegasMeasurementConfig(),
        )
        for idx in range(5)
    ]
    report = build_measurement_report(records, config=VegasMeasurementConfig(min_sample_count=5))

    assert report["classification"]["primary_rejection_driver"] == VegasRejectionDriver.NO_BOTTLENECK.value
    assert report["classification"]["is_latency_systemic"] is False
    assert report["classification"]["is_edge_cost_systemic"] is False
    assert report["classification"]["needs_parameter_calibration"] is False


def test_vegas_measurement_insufficient_sample_does_not_recommend_tuning():
    rec = measurement_from_audit_row(_row(), VegasMeasurementConfig())
    report = build_measurement_report([rec], config=VegasMeasurementConfig(min_sample_count=20))

    assert report["classification"]["primary_rejection_driver"] == VegasRejectionDriver.INSUFFICIENT_SAMPLE.value
    assert report["classification"]["needs_parameter_calibration"] is False
    assert report["recommendations"] == ["collect_more_samples_before_tuning"]


def test_vegas_report_builder_writes_latest_json(tmp_path: Path):
    audit_dir = tmp_path / "runtime" / "audit" / "20260512"
    audit_dir.mkdir(parents=True)
    (audit_dir / "strategy-001.jsonl").write_text(json.dumps(_row()) + "\n", encoding="utf-8")

    report = build_vegas_dlb_edge_measurement_report(repo_root=tmp_path, day="20260512", min_sample_count=1)
    latest = tmp_path / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json"

    assert latest.exists()
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["sample_count"] == 1
    assert report["sample_count"] == 1


def test_vegas_measurement_does_not_change_strategy_parameters():
    cfg = VegasMeasurementConfig(dlb_limit_ms=2000, ttl_limit_ms=2000)
    rec = measurement_from_audit_row(_row(), cfg)
    report = build_measurement_report([rec], config=cfg)

    assert rec["dlb_limit_ms"] == 2000
    assert rec["ttl_limit_ms"] == 2000
    assert report["dlb"]["limit_ms"] == 2000
