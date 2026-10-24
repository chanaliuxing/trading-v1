from __future__ import annotations

import json
from pathlib import Path

from services.strategy_runtime_v2.parity_recorder import compare_event_streams
from tool.replay.build_replica_fixture_catalog import build_fixture_catalog, write_fixture_catalog


def test_build_fixture_catalog_contains_gate_and_exit_fixtures() -> None:
    payload = build_fixture_catalog()
    assert "gate_allowed_long" in payload["fixture_names"]
    assert "exit_force_eod_flatten" in payload["fixture_names"]
    assert "protection_restart_missing_stop_kill_switch" in payload["fixture_names"]
    assert "protection_audit_self_heal_after_threshold" in payload["fixture_names"]
    assert "protection_audit_recovered_stop_live" in payload["fixture_names"]
    assert "protection_audit_kill_switch_after_threshold" in payload["fixture_names"]
    assert payload["fixtures"]["gate_blocked_market_data_unavailable"]["category"] == "gate"
    assert payload["fixtures"]["exit_breakeven_stop"]["category"] == "exit"
    assert payload["fixtures"]["protection_restart_missing_stop_kill_switch"]["category"] == "protection"


def test_write_fixture_catalog_and_compare_fixture_to_itself(tmp_path: Path) -> None:
    out_path = write_fixture_catalog(tmp_path / "fixture_catalog.json")
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    fixture = payload["fixtures"]["exit_tp1_partial_fill"]["events"]
    report = compare_event_streams(fixture, fixture)
    assert report["ok"] is True
    assert report["matched_count"] == len(fixture)
