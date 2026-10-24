from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.policy_compatibility_observability import (
    apply_migration_notice_lifecycle,
    build_policy_compatibility_observability,
)
from scripts.ai_trader.research.policy_notice_lifecycle import apply_notice_lifecycle_engine


def _notice(level: str, key: str) -> dict:
    return {
        "status": "active" if level != "none" else "none",
        "level": level,
        "throttle_key": key,
        "summary": "x",
        "details": {},
    }


def _progress(legacy: int, total: int, migrated: int, ratio: float) -> dict:
    return {
        "status": "ok",
        "reason": "ready",
        "runtime_legacy_consumer_count": legacy,
        "total_runtime_consumers": total,
        "migrated_runtime_consumers": migrated,
        "docs_tests_only_references": 0,
        "progress_ratio": ratio,
    }


def test_notice_lifecycle_first_and_repeat_silenced(tmp_path: Path) -> None:
    state = tmp_path / "lifecycle.json"
    first = apply_migration_notice_lifecycle(
        notice=_notice("warn", "warn:2:8:0.75"),
        progress=_progress(2, 8, 6, 0.75),
        state_path=state,
        now_iso="2026-04-11T10:00:00+00:00",
        silence_seconds=3600,
        use_cache=False,
    )
    assert first["notice"]["silenced"] is False
    assert first["notice"]["active"] is True
    assert first["notice"]["lifecycle_status"] == "active"
    second = apply_migration_notice_lifecycle(
        notice=_notice("warn", "warn:2:8:0.75"),
        progress=_progress(2, 8, 6, 0.75),
        state_path=state,
        now_iso="2026-04-11T10:10:00+00:00",
        silence_seconds=3600,
        use_cache=False,
    )
    assert second["notice"]["silenced"] is True
    assert second["notice"]["active"] is False
    assert second["notice"]["lifecycle_status"] == "silenced"


def test_notice_lifecycle_reactivate_on_key_or_level_or_progress_change(tmp_path: Path) -> None:
    state = tmp_path / "lifecycle.json"
    apply_migration_notice_lifecycle(
        notice=_notice("warn", "warn:3:10:0.7"),
        progress=_progress(3, 10, 7, 0.7),
        state_path=state,
        now_iso="2026-04-11T10:00:00+00:00",
        silence_seconds=3600,
        use_cache=False,
    )
    repeated = apply_migration_notice_lifecycle(
        notice=_notice("warn", "warn:3:10:0.7"),
        progress=_progress(3, 10, 7, 0.7),
        state_path=state,
        now_iso="2026-04-11T10:05:00+00:00",
        silence_seconds=3600,
        use_cache=False,
    )
    assert repeated["notice"]["silenced"] is True

    changed_key = apply_migration_notice_lifecycle(
        notice=_notice("warn", "warn:4:10:0.6"),
        progress=_progress(4, 10, 6, 0.6),
        state_path=state,
        now_iso="2026-04-11T10:06:00+00:00",
        silence_seconds=3600,
        use_cache=False,
    )
    assert changed_key["notice"]["silenced"] is False

    changed_level = apply_migration_notice_lifecycle(
        notice=_notice("high", "high:4:10:0.6"),
        progress=_progress(4, 10, 6, 0.6),
        state_path=state,
        now_iso="2026-04-11T10:07:00+00:00",
        silence_seconds=3600,
        use_cache=False,
    )
    assert changed_level["notice"]["silenced"] is False

    changed_progress = apply_migration_notice_lifecycle(
        notice=_notice("high", "high:4:10:0.55"),
        progress=_progress(4, 10, 6, 0.55),
        state_path=state,
        now_iso="2026-04-11T10:08:00+00:00",
        silence_seconds=3600,
        use_cache=False,
    )
    assert changed_progress["notice"]["silenced"] is False


def test_notice_lifecycle_corrupt_state_fallback(tmp_path: Path) -> None:
    summary_path = tmp_path / "state" / "policy_compatibility_summary.json"
    inventory_path = tmp_path / "state" / "policy_observability_consumers.json"
    lifecycle_path = tmp_path / "state" / "policy_observability_notice_lifecycle.json"
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
    inventory_path.write_text(
        json.dumps(
            {
                "summary": {
                    "runtime_consumers": 2,
                    "runtime_legacy_consumers": 1,
                    "runtime_new_only_consumers": 1,
                    "docs_or_tests_legacy_refs": 0,
                },
                "entries": [
                    {
                        "file_path": "scripts/a.py",
                        "reference_type": "runtime",
                        "is_legacy": True,
                        "legacy_fields": ["policy_compatibility"],
                        "migration_priority": "high",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lifecycle_path.write_text("{bad-json", encoding="utf-8")

    view = build_policy_compatibility_observability(
        summary_path,
        consumers_inventory_path=inventory_path,
        notice_lifecycle_state_path=lifecycle_path,
    )
    assert view["migration_notice"]["status"] == "active"
    assert view["notice_lifecycle_state_status"] == "degraded"


def test_notice_lifecycle_engine_persists_minimal_state_fields(tmp_path: Path) -> None:
    state = tmp_path / "state" / "policy_observability_notice_lifecycle.json"
    result = apply_notice_lifecycle_engine(
        notice=_notice("warn", "warn:2:8:0.75"),
        state_path=state,
        now_iso="2026-04-11T10:00:00+00:00",
        silence_seconds=3600,
    )
    assert result["state_status"] == "ok"

    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert set(persisted.keys()) == {
        "first_seen",
        "last_seen",
        "last_level",
        "last_throttle_key",
        "silenced_until",
    }
