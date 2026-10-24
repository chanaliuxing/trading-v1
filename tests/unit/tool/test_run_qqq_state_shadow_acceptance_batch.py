from __future__ import annotations

import json
from pathlib import Path

import tool.replay.run_qqq_state_shadow_acceptance_batch as batch_mod
from tool.replay.run_qqq_state_shadow_acceptance_batch import discover_available_days, run_qqq_state_shadow_acceptance_batch


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_day(root: Path, day: str, rows: list[dict]) -> None:
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "state-10.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_run_qqq_state_shadow_acceptance_batch_writes_daily_and_rollup_reports(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
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
    rows = [
        {"ts": "2026-04-01T10:00:00-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "intent_submitted", "reason": "submit_target", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-01T10:00:01-04:00", "domain": "state", "stage": "order_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "submitted", "reason": "entry", "trace_id": "t1", "payload": {"state_machine": "order"}},
        {"ts": "2026-04-01T10:00:02-04:00", "domain": "state", "stage": "runtime_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "live", "reason": "fill_confirmed", "trace_id": "t1", "payload": {"state_machine": "runtime"}},
        {"ts": "2026-04-01T10:00:03-04:00", "domain": "state", "stage": "protection_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "protected", "reason": "stop", "trace_id": "t1", "payload": {"state_machine": "protection"}},
        {"ts": "2026-04-01T10:30:00-04:00", "domain": "state", "stage": "flatten_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "submitted", "reason": "exit", "trace_id": "t2", "payload": {"state_machine": "flatten"}},
        {"ts": "2026-04-01T10:30:01-04:00", "domain": "state", "stage": "flatten_transition", "strategy_id": "qqq_intraday_option_v1_001", "symbol": "TQQQ", "status": "confirmed", "reason": "flat", "trace_id": "t2", "payload": {"state_machine": "flatten"}},
    ]
    _write_day(audit_root, "2026-04-01", rows)
    _write_day(audit_root, "2026-04-02", rows)
    _write_day(audit_root, "2026-04-03", rows)

    report = run_qqq_state_shadow_acceptance_batch(
        audit_root=audit_root,
        output_root=tmp_path / "out",
        days=["2026-04-01", "2026-04-02", "2026-04-03"],
        baseline_path=baseline_path,
        state_instrumentation_start_date="2026-04-01",
    )

    assert len(report["daily_report_paths"]) == 3
    assert Path(str(report["batch_report_path"])).exists()
    assert Path(str(report["cutover_recommendation_path"])).exists()
    assert Path(str(report["rollup_report_path"])).exists()
    assert report["recommendation"] == "SHADOW_PASS_CUTOVER_REVIEW_ALLOWED"


def test_run_qqq_state_shadow_acceptance_batch_with_empty_days_still_writes_reports(tmp_path: Path) -> None:
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

    report = run_qqq_state_shadow_acceptance_batch(
        audit_root=tmp_path / "audit",
        output_root=tmp_path / "out",
        days=[],
        baseline_path=baseline_path,
        state_instrumentation_start_date="2026-04-03",
    )

    assert report["days"] == []
    assert report["cutover_still_blocked"] is True
    assert Path(str(report["batch_report_path"])).exists()
    assert Path(str(report["cutover_recommendation_path"])).exists()
    assert Path(str(report["rollup_report_path"])).exists()


def test_discover_available_days_filters_by_start_date_and_max_days(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    for day in ("2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04"):
        (audit_root / day).mkdir(parents=True, exist_ok=True)
    (audit_root / "2026-04-03" / "strategy-10.jsonl").write_text(
        json.dumps({"strategy_id": "qqq_intraday_option_v1_001", "domain": "strategy"}) + "\n",
        encoding="utf-8",
    )
    (audit_root / "2026-04-04" / "state-10.jsonl").write_text(
        json.dumps({"strategy_id": "qqq_intraday_option_v1_001", "domain": "state"}) + "\n",
        encoding="utf-8",
    )

    days = discover_available_days(
        audit_root=audit_root,
        state_instrumentation_start_date="2026-04-03",
        max_days=1,
    )

    assert days == ["2026-04-04"]


def test_discover_available_days_skips_days_without_target_strategy_records(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    for day in ("2026-04-03", "2026-04-04"):
        (audit_root / day).mkdir(parents=True, exist_ok=True)
        (audit_root / day / "strategy-10.jsonl").write_text(
            json.dumps({"strategy_id": "some_other_strategy", "domain": "strategy"}) + "\n",
            encoding="utf-8",
        )

    days = discover_available_days(
        audit_root=audit_root,
        state_instrumentation_start_date="2026-04-03",
        max_days=10,
    )

    assert days == []


def test_discover_available_days_clickhouse_source_uses_clickhouse_discovery(monkeypatch, tmp_path: Path) -> None:
    def _fake_discover_clickhouse_days(**kwargs):
        assert kwargs["strategy_id"] == "qqq_intraday_option_v1_001"
        return ["2026-04-03", "2026-04-04"]

    monkeypatch.setattr(batch_mod, "_discover_clickhouse_days", _fake_discover_clickhouse_days)

    days = discover_available_days(
        audit_root=tmp_path / "audit",
        source="clickhouse",
        strategy_id="qqq_intraday_option_v1_001",
        state_instrumentation_start_date="2026-04-03",
        max_days=10,
    )
    assert days == ["2026-04-03", "2026-04-04"]
