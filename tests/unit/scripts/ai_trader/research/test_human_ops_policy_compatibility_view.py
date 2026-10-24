from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.human_ops_summary import build_human_ops_summary


def _base_inputs() -> dict:
    return {
        "day_id": "2026-04-11",
        "trading_day_status": {"current_stage": "archived"},
        "ops_control_center": {"live_enabled": False, "system_mode": "paper", "active_batches": [], "active_candidates": []},
        "end_of_day_review": {"orders_summary": {"total_orders": 0}, "fills_summary": {"total_fills": 0}, "candidate_outcomes": []},
        "weekly_candidate_review": {"promote": [], "demote": [], "retire": [], "watchlist": []},
        "review_plan_patch": {"actions": []},
    }


def test_human_ops_summary_reads_policy_compatibility_view(tmp_path: Path) -> None:
    summary_path = tmp_path / "state" / "policy_compatibility_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    source_payload = {
        "summary": {
            "known_count": 3,
            "legacy_count": 1,
            "unknown_count": 2,
            "degraded_count": 2,
            "digest_changed_without_version_bump_count": 1,
        },
        "recent_hits": {
            "unknown": [{"cycle_id": "c-001"}, {"cycle_id": "c-003"}, {"cycle_id": "c-002"}],
            "legacy": [{"cycle_id": "c-001"}],
            "degraded": [{"cycle_id": "c-002"}],
            "digest_changed_without_version_bump": [{"cycle_id": "c-002"}],
        },
        "current_cycle": {
            "cycle_id": "c-003",
            "policy_state": "known",
            "current_policy": {"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"},
            "history_summary": {"history_count": 3},
            "anomaly_count": 0,
            "legacy_hint_count": 0,
            "degraded": False,
        },
    }
    summary_path.write_text(json.dumps(source_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    before = summary_path.read_text(encoding="utf-8")

    payload = build_human_ops_summary(
        **_base_inputs(),
        output_dir=tmp_path,
        policy_compatibility_summary_path=summary_path,
    )["human_ops_summary"]

    view = payload["policy_compatibility_observability"]
    assert view["status"] == "ok"
    assert view["counts"] == {
        "known": 3,
        "legacy": 1,
        "unknown": 2,
        "degraded": 2,
        "digest_changed_without_version_bump": 1,
    }
    assert [x["cycle_id"] for x in view["recent_hits"]["unknown"]] == ["c-003", "c-002", "c-001"]
    assert view["current_cycle"]["cycle_id"] == "c-003"
    assert summary_path.read_text(encoding="utf-8") == before


def test_human_ops_summary_policy_compatibility_missing_file_fallback(tmp_path: Path) -> None:
    missing_path = tmp_path / "state" / "policy_compatibility_summary.json"
    payload = build_human_ops_summary(
        **_base_inputs(),
        output_dir=tmp_path,
        policy_compatibility_summary_path=missing_path,
    )["human_ops_summary"]
    view = payload["policy_compatibility_observability"]
    assert view["status"] == "unavailable"
    assert view["reason"] == "missing_summary_file"
    assert view["counts"]["known"] == 0


def test_human_ops_summary_policy_compatibility_corrupt_file_fallback(tmp_path: Path) -> None:
    summary_path = tmp_path / "state" / "policy_compatibility_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("{not-json", encoding="utf-8")

    payload = build_human_ops_summary(
        **_base_inputs(),
        output_dir=tmp_path,
        policy_compatibility_summary_path=summary_path,
    )["human_ops_summary"]
    view = payload["policy_compatibility_observability"]
    assert view["status"] == "degraded"
    assert view["reason"] == "corrupt_summary_file"
    assert view["counts"]["degraded"] == 0


def test_human_ops_summary_policy_compatibility_missing_fields_and_stable_render(tmp_path: Path) -> None:
    summary_path = tmp_path / "state" / "policy_compatibility_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"recent_hits": {"unknown": [{"cycle_id": "c-10"}, {"cycle_id": "c-02"}]}}), encoding="utf-8")

    first = build_human_ops_summary(
        **_base_inputs(),
        output_dir=tmp_path,
        policy_compatibility_summary_path=summary_path,
    )["human_ops_summary"]["policy_compatibility_observability"]
    second = build_human_ops_summary(
        **_base_inputs(),
        output_dir=tmp_path,
        policy_compatibility_summary_path=summary_path,
    )["human_ops_summary"]["policy_compatibility_observability"]

    assert first["status"] == "ok"
    assert first["counts"] == {
        "known": 0,
        "legacy": 0,
        "unknown": 0,
        "degraded": 0,
        "digest_changed_without_version_bump": 0,
    }
    assert first["recent_hits"]["unknown"][0]["cycle_id"] == "c-10"
    assert first == second

