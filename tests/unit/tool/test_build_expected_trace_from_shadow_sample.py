from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_expected_trace_from_shadow_sample import build_expected_trace
from tool.compare_index_super_win_shadow import build_payload


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_payload_includes_summary_fields(tmp_path: Path) -> None:
    qqq_path = tmp_path / "qqq.json"
    index_path = tmp_path / "index.json"
    _write_json(
        qqq_path,
        {"ready": True, "strategy_state": {"is_traded": True, "side": "LONG", "exec_symbol": "AAPL"}},
    )
    _write_json(
        index_path,
        {
            "entry_enabled_by_symbol": {"AAPL": True},
            "trader_view_by_symbol": {"AAPL": {"final_state": "blocked", "which_gate_blocked": "ownership_conflict"}},
            "conflict_report_by_symbol": {"AAPL": {"foreign_owner_strategy_id": "qqq_intraday_001"}},
            "shadow_report_by_symbol": {"AAPL": {"last_gate_reason": "ownership_conflict"}},
        },
    )
    payload = build_payload(qqq_obs_path=qqq_path, index_diag_snapshot_path=index_path, symbol="AAPL")
    assert payload["summary"]["qqq_ready"] is True
    assert payload["summary"]["qqq_is_traded"] is True
    assert payload["summary"]["index_final_state"] == "blocked"
    assert payload["summary"]["foreign_owner_strategy_id"] == "qqq_intraday_001"


def test_build_expected_trace_from_same_symbol_blocked_sample(tmp_path: Path) -> None:
    sample_path = tmp_path / "AAPL_sample.json"
    sample_path.write_text(
        json.dumps(
            {
                "symbol": "AAPL",
                "summary": {
                    "qqq_is_traded": True,
                    "qqq_exec_symbol": "AAPL",
                    "index_final_state": "blocked",
                    "index_gate_reason": "ownership_conflict",
                },
            }
        ),
        encoding="utf-8",
    )

    trace = build_expected_trace(sample_path)
    assert trace["events"][0]["stage"] == "entry_gate_blocked"
    assert trace["events"][0]["reason"] == "ownership_conflict"


def test_build_expected_trace_from_entry_enabled_sample(tmp_path: Path) -> None:
    sample_path = tmp_path / "MSFT_sample.json"
    sample_path.write_text(
        json.dumps(
            {
                "symbol": "MSFT",
                "summary": {
                    "qqq_is_traded": False,
                    "qqq_exec_symbol": None,
                    "index_entry_enabled": True,
                    "index_final_state": "no_attempt",
                },
            }
        ),
        encoding="utf-8",
    )

    trace = build_expected_trace(sample_path)
    assert trace["events"][0]["stage"] == "entry_gate_allowed"
