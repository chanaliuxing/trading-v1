from __future__ import annotations

import json
from pathlib import Path

from tool.ops.check_qqq_shadow_trace_coverage import build_trace_coverage_report


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_trace_coverage_passes_when_live_and_paper_traces_present(tmp_path: Path) -> None:
    _write(
        tmp_path / "qqq_signal_snapshots_20260422.jsonl",
        [{"strategy_id": "qqq_intraday_001", "snapshot_kind": "evaluation"}],
    )
    _write(
        tmp_path / "qqq_signal_snapshots_20260422_qqq_kernel_legacy_on_001.jsonl",
        [{"strategy_id": "qqq_kernel_legacy_on_001", "snapshot_kind": "evaluation"}],
    )
    _write(
        tmp_path / "qqq_signal_snapshots_20260422_qqq_kernel_legacy_off_001.jsonl",
        [{"strategy_id": "qqq_kernel_legacy_off_001", "snapshot_kind": "entry"}],
    )

    payload = build_trace_coverage_report(day="20260422", trace_root=tmp_path)

    assert payload["recommendation"] == "PASS"
    assert payload["missing_strategy_ids"] == []
    assert payload["legacy_writer_present"] is True


def test_trace_coverage_holds_when_legacy_writer_missing(tmp_path: Path) -> None:
    _write(
        tmp_path / "qqq_signal_snapshots_20260422_qqq_kernel_legacy_on_001.jsonl",
        [{"strategy_id": "qqq_kernel_legacy_on_001", "snapshot_kind": "evaluation"}],
    )
    _write(
        tmp_path / "qqq_signal_snapshots_20260422_qqq_kernel_legacy_off_001.jsonl",
        [{"strategy_id": "qqq_kernel_legacy_off_001", "snapshot_kind": "evaluation"}],
    )

    payload = build_trace_coverage_report(day="20260422", trace_root=tmp_path)

    assert payload["recommendation"] == "HOLD"
    assert payload["missing_strategy_ids"] == ["qqq_intraday_001"]


def test_trace_coverage_accepts_option_live_writer_alias(tmp_path: Path) -> None:
    _write(
        tmp_path / "qqq_signal_snapshots_20260422.jsonl",
        [{"strategy_id": "qqq_intraday_option_v1_001", "snapshot_kind": "evaluation"}],
    )
    _write(
        tmp_path / "qqq_signal_snapshots_20260422_qqq_kernel_legacy_on_001.jsonl",
        [{"strategy_id": "qqq_kernel_legacy_on_001", "snapshot_kind": "evaluation"}],
    )
    _write(
        tmp_path / "qqq_signal_snapshots_20260422_qqq_kernel_legacy_off_001.jsonl",
        [{"strategy_id": "qqq_kernel_legacy_off_001", "snapshot_kind": "entry"}],
    )

    payload = build_trace_coverage_report(day="20260422", trace_root=tmp_path)

    assert payload["recommendation"] == "PASS"
    assert payload["missing_strategy_ids"] == []
    assert payload["live_writer_present"] is True
    assert payload["matched_live_strategy_ids"] == ["qqq_intraday_option_v1_001"]
    assert payload["legacy_writer_present"] is False
