from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.ai_trader.research.policy_compatibility_observability import (
    build_legacy_policy_compatibility,
    build_policy_compatibility_observability,
)


def test_policy_observability_contract_and_aliases(tmp_path: Path) -> None:
    summary_path = tmp_path / "state" / "policy_compatibility_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "summary": {
                    "known_count": 2,
                    "legacy_count": 1,
                    "unknown_count": 1,
                    "degraded_count": 2,
                    "digest_changed_without_version_bump_count": 1,
                },
                "recent_hits": {
                    "legacy": [{"cycle_id": "c-001"}],
                    "unknown": [{"cycle_id": "c-002"}],
                    "degraded": [{"cycle_id": "c-003"}],
                    "digest_changed_without_version_bump": [{"cycle_id": "c-004"}],
                },
                "current_cycle": {
                    "cycle_id": "c-004",
                    "policy_state": "unknown",
                    "current_policy": {"policy_id": "preflight-policy", "version": "v9", "digest": "digest-v9"},
                    "history_summary": {"history_count": 4},
                    "anomaly_count": 1,
                    "legacy_hint_count": 1,
                    "advisory_count": 1,
                    "anomalies": ["digest_changed_without_version_bump"],
                    "legacy_hints": ["legacy_history_reports_missing_fields:1"],
                    "advisories": ["current_policy_not_found_in_history"],
                    "degraded": True,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    view = build_policy_compatibility_observability(summary_path)
    assert view["status"] == "ok"
    assert set(view.keys()) >= {
        "status",
        "reason",
        "source_path",
        "counts",
        "current_cycle",
        "recent_examples",
        "recent_hits",
        "migration_progress",
    }
    assert view["recent_hits"] == view["recent_examples"]

    legacy = build_legacy_policy_compatibility(view)
    assert legacy["policy_state"] == "unknown"
    assert legacy["history_count"] == 4


def test_policy_observability_status_enum_and_fallback_semantics(tmp_path: Path) -> None:
    missing = build_policy_compatibility_observability(tmp_path / "missing.json")
    assert missing["status"] == "unavailable"
    assert missing["reason"] == "missing_summary_file"

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{bad-json", encoding="utf-8")
    corrupt = build_policy_compatibility_observability(corrupt_path)
    assert corrupt["status"] == "degraded"
    assert corrupt["reason"] == "corrupt_summary_file"


def test_policy_observability_contract_doc_matches_impl() -> None:
    doc_path = Path("docs/README.md")
    text = doc_path.read_text(encoding="utf-8")
    assert "Policy Compatibility Observability 只读契约" in text
    for field_name in ("`status`", "`reason`", "`source_path`", "`counts`", "`current_cycle`", "`recent_examples`", "`migration_progress`"):
        assert field_name in text
    assert "`recent_hits`" in text
    assert "`policy_compatibility`" in text

    match = re.search(
        r"Policy Compatibility Observability 只读契约[\s\S]*?```json\n([\s\S]*?)\n```",
        text,
    )
    assert match is not None
    example = json.loads(match.group(1))
    assert set(example.keys()) >= {
        "status",
        "reason",
        "source_path",
        "counts",
        "current_cycle",
        "recent_examples",
        "recent_hits",
    }
    assert set(example["counts"].keys()) == {
        "known",
        "legacy",
        "unknown",
        "degraded",
        "digest_changed_without_version_bump",
    }
