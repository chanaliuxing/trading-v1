from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


_BASELINE_PATH = Path("docs/research_safety_contract_freeze_baseline.json")


def _load_baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def _load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _extract_three_entry_payload(doc: dict) -> dict:
    observability = dict(doc.get("policy_compatibility_observability") or {})
    migration_notice = dict(observability.get("migration_notice") or {})
    notice_lifecycle = dict(observability.get("notice_lifecycle") or {})
    return {
        "status": migration_notice.get("status"),
        "level": migration_notice.get("level"),
        "active": migration_notice.get("active"),
        "silenced": migration_notice.get("silenced"),
        "throttle_key": migration_notice.get("throttle_key"),
        "first_seen": notice_lifecycle.get("first_seen"),
        "last_seen": notice_lifecycle.get("last_seen"),
        "last_level": notice_lifecycle.get("last_level"),
        "last_throttle_key": notice_lifecycle.get("last_throttle_key"),
        "silenced_until": notice_lifecycle.get("silenced_until"),
    }


def test_freeze_manifest_existence_and_minimum_sections() -> None:
    assert _BASELINE_PATH.exists()
    baseline = _load_baseline()
    assert baseline["scope"] == "ai_trader_research_safety_observability_readonly_contract"
    for section in (
        "core_state_files",
        "readonly_view_files",
        "contract_fields",
        "compatibility_aliases",
        "execution_chain_unchanged_guards",
        "freeze_constraints",
    ):
        assert section in baseline


def test_critical_artifact_and_contract_field_presence_matches_baseline() -> None:
    run_fixture_cycle_main()
    baseline = _load_baseline()
    for rel_path in baseline["core_state_files"]:
        assert Path(rel_path).exists(), rel_path
    for rel_path in baseline["readonly_view_files"]:
        assert Path(rel_path).exists(), rel_path

    readiness = _load_json(baseline["readonly_view_files"][0])
    verdict = _load_json(baseline["readonly_view_files"][1])
    human = _load_json(baseline["readonly_view_files"][2])
    views = (readiness, verdict, human)

    for view in views:
        observability = dict(view.get("policy_compatibility_observability") or {})
        for field_name in baseline["contract_fields"]["policy_compatibility_observability"]:
            assert field_name in observability
        for field_name in baseline["contract_fields"]["migration_notice"]:
            assert field_name in dict(observability.get("migration_notice") or {})
        for field_name in baseline["contract_fields"]["notice_lifecycle"]:
            assert field_name in dict(observability.get("notice_lifecycle") or {})
        for field_name in baseline["contract_fields"]["migration_progress"]:
            assert field_name in dict(observability.get("migration_progress") or {})

    lifecycle_state = _load_json(baseline["core_state_files"][2])
    assert set(lifecycle_state.keys()) == set(baseline["contract_fields"]["notice_lifecycle_state_file"])

    entry_payloads = [_extract_three_entry_payload(view) for view in views]
    for key_name in baseline["contract_fields"]["three_entry_consistency"]:
        values = [payload.get(key_name) for payload in entry_payloads]
        assert values[0] == values[1] == values[2]


def test_compatibility_alias_lock_matches_baseline() -> None:
    run_fixture_cycle_main()
    baseline = _load_baseline()
    alias_rows = {row["alias"]: row for row in baseline["compatibility_aliases"]}
    assert alias_rows["recent_hits"]["canonical"] == "recent_examples"
    assert alias_rows["policy_compatibility"]["canonical"] == "policy_compatibility_observability.current_cycle"

    readiness = _load_json(baseline["readonly_view_files"][0])
    observability = dict(readiness.get("policy_compatibility_observability") or {})
    assert observability.get("recent_hits") == observability.get("recent_examples")
    assert "policy_compatibility" in readiness


def test_execution_chain_unchanged_guard_presence() -> None:
    baseline = _load_baseline()
    for row in baseline["execution_chain_unchanged_guards"]:
        text = Path(row["file"]).read_text(encoding="utf-8")
        assert str(row["must_contain"]) in text
