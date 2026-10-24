from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.policy_compatibility_summary import (
    build_policy_compatibility_summary,
    persist_policy_compatibility_summary,
)


def _write_preflight(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _preflight(policy_state: str, *, anomalies: list[str] | None = None, legacy_hints: list[str] | None = None) -> dict:
    return {
        "cycle_id": "sample",
        "generated_at": "2026-04-10T00:00:00+00:00",
        "overall_status": "ready",
        "policy_compatibility": {
            "policy_state": policy_state,
            "current_policy": {"policy_id": "preflight-policy", "version": "v1", "digest": "digest-v1"},
            "history_summary": {
                "history_count": 3,
                "known_versions": ["v1"],
                "known_digests_count": 1,
                "match_by_version_count": 1,
                "match_by_digest_count": 1,
            },
            "anomalies": anomalies or [],
            "legacy_hints": legacy_hints or [],
            "advisories": [],
        },
    }


def test_policy_compatibility_summary_single_cycle(tmp_path: Path) -> None:
    cycles_root = tmp_path / "cycles"
    state_root = tmp_path / "state"
    summary_bundle = persist_policy_compatibility_summary(
        cycle_id="c-001",
        preflight_check=_preflight("known"),
        cycles_root=cycles_root,
        state_root=state_root,
    )
    summary_path = Path(summary_bundle["policy_compatibility_summary_json"])
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary_path.exists()
    assert payload["current_cycle"]["policy_state"] == "known"
    assert payload["summary"]["total_cycles"] == 1
    assert payload["summary"]["known_count"] == 1
    assert payload["summary"]["unknown_count"] == 0


def test_policy_compatibility_summary_multi_cycle_aggregate(tmp_path: Path) -> None:
    cycles_root = tmp_path / "cycles"
    _write_preflight(cycles_root / "c-001" / "preflight_check.json", _preflight("known"))
    _write_preflight(
        cycles_root / "c-002" / "preflight_check.json",
        _preflight("known", anomalies=["digest_changed_without_version_bump"]),
    )
    _write_preflight(
        cycles_root / "c-003" / "preflight_check.json",
        _preflight("unknown", legacy_hints=["legacy_history_reports_missing_fields:2"]),
    )
    payload = build_policy_compatibility_summary(
        cycle_id="c-004",
        preflight_check=_preflight("known"),
        cycles_root=cycles_root,
    )
    summary = payload["summary"]
    assert summary["total_cycles"] == 4
    assert summary["known_count"] == 3
    assert summary["unknown_count"] == 1
    assert summary["legacy_count"] == 1
    assert summary["digest_changed_without_version_bump_count"] == 1
    assert summary["degraded_count"] == 2
    assert payload["recent_hits"]["digest_changed_without_version_bump"]


def test_policy_compatibility_summary_missing_and_corrupt_cycle_files(tmp_path: Path) -> None:
    cycles_root = tmp_path / "cycles"
    (cycles_root / "c-missing").mkdir(parents=True, exist_ok=True)
    (cycles_root / "c-corrupt").mkdir(parents=True, exist_ok=True)
    (cycles_root / "c-corrupt" / "preflight_check.json").write_text("{not-json", encoding="utf-8")

    payload = build_policy_compatibility_summary(
        cycle_id="c-current",
        preflight_check={
            "cycle_id": "c-current",
            "generated_at": "2026-04-10T00:00:00+00:00",
            "overall_status": "ready",
        },
        cycles_root=cycles_root,
    )
    source = payload["source"]
    current = payload["current_cycle"]
    assert source["scanned_cycle_dirs"] == 2
    assert source["missing_preflight_checks"] == 1
    assert source["corrupt_preflight_checks"] == 1
    assert current["has_policy_compatibility"] is False
    assert current["policy_state"] == "unknown"
    assert "missing_policy_compatibility_section" in current["advisories"]

