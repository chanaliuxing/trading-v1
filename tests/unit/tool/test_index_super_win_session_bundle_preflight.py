from __future__ import annotations

from pathlib import Path

import yaml

from tool.index_super_win_session_bundle_preflight import build_session_bundle_report


def _write_config(path: Path, *, strategy_id: str, runtime_scope: str, runtime_snapshot_path: str, session_name: str, start: str, end: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "strategy_id": strategy_id,
                "runtime_scope": runtime_scope,
                "runtime_snapshot_path": runtime_snapshot_path,
                "session_name": session_name,
                "session_start_et": start,
                "session_end_et": end,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_session_bundle_preflight_passes_for_non_overlapping_configs(tmp_path: Path) -> None:
    pre = tmp_path / "pre.yml"
    over = tmp_path / "over.yml"
    after = tmp_path / "after.yml"
    _write_config(pre, strategy_id="qqq_premarket_001", runtime_scope="a", runtime_snapshot_path="a.json", session_name="premarket", start="04:00:00", end="09:29:59")
    _write_config(over, strategy_id="qqq_overnight_001", runtime_scope="b", runtime_snapshot_path="b.json", session_name="overnight", start="20:00:00", end="03:59:59")
    _write_config(after, strategy_id="qqq_afterhours_001", runtime_scope="c", runtime_snapshot_path="c.json", session_name="afterhours", start="16:00:00", end="19:59:59")
    report = build_session_bundle_report(config_paths=[pre, over, after])
    assert report["ready"] is True
    assert report["ready_for_enablement"] is True
    assert report["errors"] == []


def test_session_bundle_preflight_fails_on_overlap_and_duplicate_ids(tmp_path: Path) -> None:
    a = tmp_path / "a.yml"
    b = tmp_path / "b.yml"
    _write_config(a, strategy_id="dup", runtime_scope="a", runtime_snapshot_path="a.json", session_name="premarket", start="04:00:00", end="09:29:59")
    _write_config(b, strategy_id="dup", runtime_scope="b", runtime_snapshot_path="a.json", session_name="overnight", start="09:00:00", end="12:00:00")
    report = build_session_bundle_report(config_paths=[a, b])
    assert report["ready"] is False
    assert report["ready_for_enablement"] is False
    messages = [item["message"] for item in report["findings"] if item["level"] == "FAIL"]
    assert "duplicate strategy_id" in messages
    assert "duplicate runtime_snapshot_path" in messages
    assert "session windows overlap" in messages
    assert len(report["errors"]) == 3
