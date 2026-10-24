import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.capture_index_super_win_shadow_sample import capture_sample
from tool.run_index_super_win_shadow_session import run_session
from tool.build_index_super_win_shadow_session_report import build_report
from tool.compare_index_super_win_shadow import build_payload


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_payload_reports_fail_when_input_snapshot_missing(tmp_path):
    qqq_path = tmp_path / "missing_qqq.json"
    index_path = tmp_path / "missing_index.json"

    payload = build_payload(
        qqq_obs_path=qqq_path,
        index_diag_snapshot_path=index_path,
        symbol="AAPL",
    )

    assert int(payload["exit_code"]) == 2
    assert len(payload["findings"]) == 2


def test_build_report_aggregates_samples_by_symbol(tmp_path):
    sample_dir = tmp_path / "samples"
    _write_json(
        sample_dir / "AAPL_1.json",
        {
            "symbol": "AAPL",
            "captured_at_et": "2026-03-14T10:00:00-04:00",
            "exit_code": 0,
            "findings": [
                {"level": "PASS", "message": "Same-symbol isolation held", "details": {}},
                {"level": "OK", "message": "Loaded qqq and index snapshots", "details": {}},
            ],
        },
    )
    _write_json(
        sample_dir / "AAPL_2.json",
        {
            "symbol": "AAPL",
            "captured_at_et": "2026-03-14T10:05:00-04:00",
            "exit_code": 1,
            "findings": [
                {"level": "WARN", "message": "qqq is ready but index_super_win entry is disabled", "details": {}},
            ],
        },
    )
    _write_json(
        sample_dir / "MSFT_1.json",
        {
            "symbol": "MSFT",
            "captured_at_et": "2026-03-14T10:06:00-04:00",
            "exit_code": 0,
            "findings": [
                {"level": "OK", "message": "No same-symbol live conflict detected from qqq snapshot", "details": {}},
            ],
        },
    )

    report = build_report(sample_dir)

    assert report["sample_count"] == 3
    assert report["ready_for_cutin"] is False
    assert report["symbols"]["AAPL"]["sample_count"] == 2
    assert report["symbols"]["AAPL"]["exit_code_counter"]["0"] == 1
    assert report["symbols"]["AAPL"]["exit_code_counter"]["1"] == 1
    assert report["symbols"]["AAPL"]["ready_for_cutin"] is False
    assert report["symbols"]["MSFT"]["ready_for_cutin"] is True


def test_capture_sample_writes_timestamped_payload(tmp_path):
    qqq_path = tmp_path / "qqq.json"
    index_path = tmp_path / "index.json"
    _write_json(
        qqq_path,
        {
            "ready": True,
            "strategy_state": {"is_traded": False, "side": None, "exec_symbol": None},
            "missing_required": [],
        },
    )
    _write_json(
        index_path,
        {
            "entry_enabled_by_symbol": {"AAPL": True},
            "trader_view_by_symbol": {"AAPL": {"final_state": "no_attempt"}},
            "conflict_report_by_symbol": {"AAPL": {}},
            "shadow_report_by_symbol": {"AAPL": {"entry_enabled": True}},
            "chain_report_by_symbol": {"AAPL": {}},
        },
    )

    payload, output_path = capture_sample(
        symbol="AAPL",
        qqq_obs_path=qqq_path,
        index_diag_snapshot_path=index_path,
        output_dir=tmp_path / "samples",
    )

    assert output_path.exists()
    assert payload["symbol"] == "AAPL"
    assert "captured_at_et" in payload
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["symbol"] == "AAPL"


def test_run_session_captures_multiple_iterations_and_builds_report(tmp_path):
    qqq_path = tmp_path / "qqq.json"
    index_path = tmp_path / "index.json"
    _write_json(
        qqq_path,
        {
            "ready": True,
            "strategy_state": {"is_traded": True, "side": "LONG", "exec_symbol": "AAPL"},
            "missing_required": [],
        },
    )
    _write_json(
        index_path,
        {
            "entry_enabled_by_symbol": {"AAPL": True, "MSFT": True},
            "trader_view_by_symbol": {
                "AAPL": {
                    "final_state": "blocked",
                    "which_gate_blocked": "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed",
                    "broker_open_order_conflict": False,
                    "broker_position_conflict": False,
                    "foreign_owner_strategy_id": "qqq_intraday_001",
                    "last_preview": {"status": "OWNERSHIP_CONFLICT"},
                },
                "MSFT": {"final_state": "no_attempt"},
            },
            "conflict_report_by_symbol": {
                "AAPL": {
                    "foreign_owner_strategy_id": "qqq_intraday_001",
                    "foreign_symbol_ownership_conflicts": 1,
                    "foreign_open_order_conflicts": 0,
                    "foreign_position_conflict_detected": False,
                },
                "MSFT": {},
            },
            "shadow_report_by_symbol": {
                "AAPL": {"entry_enabled": True, "last_gate_reason": "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed"},
                "MSFT": {"entry_enabled": True},
            },
            "chain_report_by_symbol": {
                "AAPL": {"entry_gate": {"status": "blocked", "reason": "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed"}},
                "MSFT": {},
            },
        },
    )

    times = iter(
        [
            datetime.fromisoformat("2026-03-14T10:00:00-04:00"),
            datetime.fromisoformat("2026-03-14T10:00:00-04:00"),
            datetime.fromisoformat("2026-03-14T10:05:00-04:00"),
            datetime.fromisoformat("2026-03-14T10:05:00-04:00"),
        ]
    )
    sleep_calls: list[float] = []
    result = run_session(
        symbols=["AAPL", "MSFT"],
        qqq_obs_path=qqq_path,
        index_diag_snapshot_path=index_path,
        output_dir=tmp_path / "samples",
        iterations=2,
        sleep_interval_sec=0.0,
        clock=lambda: next(times),
        sleeper=lambda sec: sleep_calls.append(float(sec)),
    )

    assert Path(result["manifest_path"]).exists()
    assert Path(result["report_path"]).exists()
    assert result["sample_count"] == 4
    report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert report["symbols"]["AAPL"]["sample_count"] == 2
    assert report["symbols"]["MSFT"]["sample_count"] == 2
