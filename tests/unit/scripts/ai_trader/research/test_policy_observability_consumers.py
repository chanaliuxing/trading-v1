from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.policy_compatibility_observability import build_policy_compatibility_observability
from scripts.ai_trader.research.policy_observability_consumers import (
    build_policy_observability_consumer_inventory,
    persist_policy_observability_consumer_inventory,
)


def test_consumer_inventory_detection_and_priority(tmp_path: Path) -> None:
    (tmp_path / "scripts/app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests/unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)

    (tmp_path / "scripts/app/runtime_legacy.py").write_text(
        "x = {'policy_compatibility': 1, 'recent_hits': []}\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts/app/runtime_new.py").write_text(
        "x = {'policy_compatibility_observability': 1, 'recent_examples': []}\n",
        encoding="utf-8",
    )
    (tmp_path / "tests/unit/test_only.py").write_text(
        "x = 'policy_compatibility'\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/readme.md").write_text(
        "policy_compatibility and recent_hits",
        encoding="utf-8",
    )

    inv = build_policy_observability_consumer_inventory(repo_root=tmp_path)
    summary = inv["summary"]
    assert summary["runtime_legacy_consumers"] == 1
    assert summary["runtime_new_only_consumers"] == 1
    assert summary["docs_or_tests_legacy_refs"] >= 2

    runtime_legacy = [x for x in inv["entries"] if x["file_path"].endswith("runtime_legacy.py")][0]
    assert runtime_legacy["is_legacy"] is True
    assert runtime_legacy["migration_priority"] == "high"


def test_no_false_positive_for_new_field_name(tmp_path: Path) -> None:
    (tmp_path / "scripts/app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts/app/new_only.py").write_text(
        "x = {'policy_compatibility_observability': {'recent_examples': []}}\n",
        encoding="utf-8",
    )
    inv = build_policy_observability_consumer_inventory(repo_root=tmp_path)
    entries = inv["entries"]
    assert len(entries) == 1
    assert entries[0]["consumer_mode"] == "new_only"
    assert entries[0]["is_legacy"] is False


def test_migration_notice_active_only_when_runtime_legacy_exists(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    summary_path = state_root / "policy_compatibility_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "summary": {
                    "known_count": 1,
                    "legacy_count": 0,
                    "unknown_count": 0,
                    "degraded_count": 0,
                    "digest_changed_without_version_bump_count": 0,
                },
                "current_cycle": {
                    "cycle_id": "c1",
                    "policy_state": "known",
                    "current_policy": {},
                    "history_summary": {},
                    "anomaly_count": 0,
                    "legacy_hint_count": 0,
                },
                "recent_hits": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (tmp_path / "scripts/app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts/app/runtime_legacy.py").write_text("x='policy_compatibility'\n", encoding="utf-8")
    inv_bundle = persist_policy_observability_consumer_inventory(state_root=state_root, repo_root=tmp_path)

    view = build_policy_compatibility_observability(
        summary_path,
        consumers_inventory_path=inv_bundle["policy_observability_consumers_json"],
    )
    assert view["migration_notice"]["status"] == "active"
    assert view["migration_notice"]["level"] == "info"
    assert view["migration_notice"]["legacy_runtime_consumer_count"] >= 1
    assert view["migration_notice"]["throttled"] is True
    progress = view["migration_progress"]
    assert progress["status"] == "ok"
    assert progress["runtime_legacy_consumer_count"] >= 1
    assert progress["total_runtime_consumers"] >= progress["runtime_legacy_consumer_count"]
    assert 0.0 <= float(progress["progress_ratio"]) <= 1.0

    (tmp_path / "scripts/app/runtime_legacy.py").write_text("x='policy_compatibility_observability'\n", encoding="utf-8")
    inv_bundle_2 = persist_policy_observability_consumer_inventory(state_root=state_root, repo_root=tmp_path)
    view_2 = build_policy_compatibility_observability(
        summary_path,
        consumers_inventory_path=inv_bundle_2["policy_observability_consumers_json"],
    )
    assert view_2["migration_notice"]["status"] == "none"
    assert view_2["migration_notice"]["level"] == "none"
    assert view_2["migration_progress"]["status"] == "ok"
    assert view_2["migration_progress"]["runtime_legacy_consumer_count"] == 0
    assert view_2["migration_progress"]["progress_ratio"] == 1.0


def test_migration_progress_fallback_for_missing_or_corrupt_inventory(tmp_path: Path) -> None:
    summary_path = tmp_path / "state" / "policy_compatibility_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "summary": {
                    "known_count": 1,
                    "legacy_count": 0,
                    "unknown_count": 0,
                    "degraded_count": 0,
                    "digest_changed_without_version_bump_count": 0,
                },
                "current_cycle": {"policy_state": "known", "current_policy": {}, "history_summary": {}},
                "recent_hits": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    missing_inventory = build_policy_compatibility_observability(
        summary_path,
        consumers_inventory_path=tmp_path / "state" / "missing_inventory.json",
    )
    assert missing_inventory["migration_progress"]["status"] == "unavailable"
    assert missing_inventory["migration_progress"]["reason"] == "missing_inventory_file"
    assert missing_inventory["migration_notice"]["status"] == "unavailable"
    assert missing_inventory["migration_notice"]["level"] == "warn"

    corrupt_inventory_path = tmp_path / "state" / "corrupt_inventory.json"
    corrupt_inventory_path.write_text("{bad-json", encoding="utf-8")
    corrupt_inventory = build_policy_compatibility_observability(
        summary_path,
        consumers_inventory_path=corrupt_inventory_path,
    )
    assert corrupt_inventory["migration_progress"]["status"] == "degraded"
    assert corrupt_inventory["migration_progress"]["reason"] == "corrupt_inventory_file"
    assert corrupt_inventory["migration_notice"]["status"] == "degraded"
    assert corrupt_inventory["migration_notice"]["level"] == "warn"


def test_migration_notice_severity_warn_and_high(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    summary_path = state_root / "policy_compatibility_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "summary": {
                    "known_count": 1,
                    "legacy_count": 0,
                    "unknown_count": 0,
                    "degraded_count": 0,
                    "digest_changed_without_version_bump_count": 0,
                },
                "current_cycle": {"policy_state": "known", "current_policy": {}, "history_summary": {}},
                "recent_hits": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (tmp_path / "scripts/app").mkdir(parents=True, exist_ok=True)
    for idx in range(3):
        (tmp_path / "scripts/app" / f"legacy_{idx}.py").write_text("x='policy_compatibility'\n", encoding="utf-8")
    inv_warn = persist_policy_observability_consumer_inventory(state_root=state_root, repo_root=tmp_path)
    view_warn = build_policy_compatibility_observability(summary_path, consumers_inventory_path=inv_warn["policy_observability_consumers_json"])
    assert view_warn["migration_notice"]["level"] == "warn"

    for idx in range(6):
        (tmp_path / "scripts/app" / f"legacy_{idx}.py").write_text("x='policy_compatibility'\n", encoding="utf-8")
    inv_high = persist_policy_observability_consumer_inventory(state_root=state_root, repo_root=tmp_path)
    view_high = build_policy_compatibility_observability(summary_path, consumers_inventory_path=inv_high["policy_observability_consumers_json"])
    assert view_high["migration_notice"]["level"] == "high"
