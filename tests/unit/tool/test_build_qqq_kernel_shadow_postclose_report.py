from pathlib import Path

import yaml

from tool.replay.build_qqq_kernel_shadow_postclose_report import build_postclose_report


def _write_config(path: Path, *, shadow_enabled: bool = True):
    payload = {
        "strategies": [
            {"id": "qqq_intraday_001", "enabled": True},
            {
                "id": "qqq_kernel_shadow_legacy_on_001",
                "enabled": shadow_enabled,
                "params": {
                    "extras": {
                        "dry_run": True,
                        "shadow_only": True,
                        "policy_id": "qqq_intraday_policy_v1",
                        "shadow_legacy_mode": "legacy_on",
                        "shadow_origin_strategy_id": "qqq_intraday_001",
                        "shared_underlying_read_mode": "plane_only",
                    }
                },
            },
            {
                "id": "qqq_kernel_shadow_legacy_off_001",
                "enabled": shadow_enabled,
                "params": {
                    "extras": {
                        "dry_run": True,
                        "shadow_only": True,
                        "policy_id": "qqq_intraday_policy_v1",
                        "shadow_legacy_mode": "legacy_off",
                        "shadow_origin_strategy_id": "qqq_intraday_001",
                        "shared_underlying_read_mode": "plane_only",
                    }
                },
            },
        ]
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_build_postclose_report_blocks_when_parity_missing_on_trading_day(tmp_path: Path):
    config = tmp_path / "config.yml"
    report_root = tmp_path / "reports"
    trace_root = tmp_path / "traces"
    report_root.mkdir()
    trace_root.mkdir()
    _write_config(config)
    (report_root / "20260421_enabled_readiness.json").write_text(
        '{"ok": true, "recommendation": "GO_FOR_READINESS_ONLY", "failed_checks": []}',
        encoding="utf-8",
    )

    payload = build_postclose_report(day="20260421", config_path=config, report_root=report_root, trace_root=trace_root)

    assert payload["recommendation"] == "HOLD"
    assert "parity_runner_did_not_execute" in payload["blockers"]
    assert payload["summary"]["day_is_trading_day"] is True
    assert payload["summary"]["parity_report_count"] == 0


def test_build_postclose_report_continues_when_parity_exists(tmp_path: Path):
    config = tmp_path / "config.yml"
    report_root = tmp_path / "reports"
    trace_root = tmp_path / "traces"
    report_root.mkdir()
    trace_root.mkdir()
    _write_config(config)
    (report_root / "20260421_enabled_readiness.json").write_text(
        '{"ok": true, "recommendation": "GO_FOR_READINESS_ONLY", "failed_checks": []}',
        encoding="utf-8",
    )
    (report_root / "20260421_1130_parity.json").write_text(
        '{"recommendation":"PASS","diff_total":0,"match_ratio":1.0,"legacy_mode":"legacy_on","legacy_count":3,"kernel_count":3,"diff_count_by_field":{"signal_authority":0}}',
        encoding="utf-8",
    )

    payload = build_postclose_report(day="20260421", config_path=config, report_root=report_root, trace_root=trace_root)

    assert payload["recommendation"] == "CONTINUE_SHADOW"
    assert payload["blockers"] == []
    assert payload["summary"]["parity_report_count"] == 1


def test_build_postclose_report_ignores_non_parity_artifacts(tmp_path: Path):
    config = tmp_path / "config.yml"
    report_root = tmp_path / "reports"
    trace_root = tmp_path / "traces"
    report_root.mkdir()
    trace_root.mkdir()
    _write_config(config)
    (report_root / "20260421_enabled_readiness.json").write_text(
        '{"ok": true, "recommendation": "GO_FOR_READINESS_ONLY", "failed_checks": []}',
        encoding="utf-8",
    )
    (report_root / "20260421_postclose_report_preview.json").write_text(
        '{"recommendation":"CONTINUE_SHADOW"}',
        encoding="utf-8",
    )
    (report_root / "20260421_preopen_gate_enabled.json").write_text(
        '{"recommendation":"GO_FOR_READINESS_ONLY"}',
        encoding="utf-8",
    )

    payload = build_postclose_report(day="20260421", config_path=config, report_root=report_root, trace_root=trace_root)

    assert payload["recommendation"] == "HOLD"
    assert payload["summary"]["parity_report_count"] == 0
    assert "parity_runner_did_not_execute" in payload["blockers"]


def test_build_postclose_report_marks_no_entry_candidate_day(tmp_path: Path):
    config = tmp_path / "config.yml"
    report_root = tmp_path / "reports"
    trace_root = tmp_path / "traces"
    report_root.mkdir()
    trace_root.mkdir()
    _write_config(config)
    (report_root / "20260421_enabled_readiness.json").write_text(
        '{"ok": true, "recommendation": "GO_FOR_READINESS_ONLY", "failed_checks": []}',
        encoding="utf-8",
    )
    (trace_root / "qqq_signal_snapshots_20260421.jsonl").write_text(
        '{"strategy_id":"qqq_intraday_001","snapshot_kind":"evaluation","decision_id":"d1"}\n',
        encoding="utf-8",
    )

    payload = build_postclose_report(day="20260421", config_path=config, report_root=report_root, trace_root=trace_root)

    assert payload["recommendation"] == "HOLD"
    assert payload["trace_summary"]["evaluation_rows"] == 1
    assert payload["trace_summary"]["entry_rows"] == 0
    assert "no_entry_candidate_today" in payload["blockers"]
