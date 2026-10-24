from __future__ import annotations

import json
from pathlib import Path

from tool.runtime.run_bounded_paper_shadow_runtime import run_bounded_runtime


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_bounded_runtime_sample_count_emits_unique_shadow_chains(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "0")

    result = run_bounded_runtime(
        repo_root=tmp_path,
        mode="SHADOW_ONLY",
        day="20260512",
        duration_sec=30,
        tick_sec=2,
        sample_count=3,
        emit_paper_broker_events=False,
        append=False,
        no_sleep=True,
        no_live_guard_status="PASS",
    )

    rows = _read_jsonl(tmp_path / "runtime" / "audit" / "20260512" / "strategy-paper-shadow.jsonl")
    assert result["sample_count"] == 3
    assert len({row["intent_id"] for row in rows}) == 3
    assert sum(1 for row in rows if row["stage"] == "uom_skipped_shadow") == 3
    assert {row["uom_route_status"] for row in rows if row["stage"] == "uom_skipped_shadow"} == {"skipped"}


def test_bounded_runtime_paper_only_emits_fake_broker_events(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "0")

    result = run_bounded_runtime(
        repo_root=tmp_path,
        mode="PAPER_ONLY",
        day="20260512",
        duration_sec=30,
        tick_sec=2,
        sample_count=2,
        emit_paper_broker_events=True,
        append=False,
        no_sleep=True,
        no_live_guard_status="PASS",
    )

    rows = _read_jsonl(tmp_path / "runtime" / "audit" / "20260512" / "strategy-paper-shadow.jsonl")
    stages = [row["stage"] for row in rows]
    assert result["sample_count"] == 2
    assert stages.count("uom_delegated") == 2
    assert stages.count("paper_broker_submit") == 2
    assert stages.count("broker_ack") == 2
    assert stages.count("fill") == 2
    assert stages.count("pnl_replay") == 2
    broker_rows = [row for row in rows if row["stage"] in {"paper_broker_submit", "broker_ack", "fill", "pnl_replay"}]
    assert broker_rows
    assert {row["broker_source"] for row in broker_rows} == {"paper_broker"}
    assert all(row["paper_broker"] is True for row in broker_rows)
