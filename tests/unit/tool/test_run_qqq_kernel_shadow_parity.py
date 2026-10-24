from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tool" / "replay" / "run_qqq_kernel_shadow_parity.py"


def test_run_qqq_kernel_shadow_parity_pass(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    kernel = tmp_path / "kernel.json"
    output = tmp_path / "report.json"
    rows = [
        {
            "decision_id": "d1",
            "decision_ts_epoch_s": 1710000000.0,
            "allow": True,
            "side": "LONG",
            "reason": "ready",
            "symbol": "QQQ",
            "target_qty": 10,
            "exec_symbol": "TQQQ",
            "qty": 10,
            "order_type": "MKT",
        }
    ]
    legacy.write_text(json.dumps({"records": rows}), encoding="utf-8")
    kernel.write_text(json.dumps({"records": rows}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--legacy",
            str(legacy),
            "--kernel",
            str(kernel),
            "--output",
            str(output),
            "--day",
            "20260420",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "PASS"
    assert payload["diff_total"] == 0
    assert payload["metrics"]["qqq_kernel_shadow_parity_diff_total"] == 0


def test_run_qqq_kernel_shadow_parity_accepts_jsonl_signal_snapshots(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.jsonl"
    kernel = tmp_path / "kernel.jsonl"
    output = tmp_path / "report.json"
    row = {
        "decision_id": "d1",
        "ts_epoch_s": 1710000000.0,
        "direction": "LONG",
        "exec_symbol": "TQQQ",
        "signal_state": {"mode": "v47_fast_drift"},
    }
    legacy.write_text(json.dumps(row) + "\n", encoding="utf-8")
    kernel.write_text(json.dumps(row) + "\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--legacy",
            str(legacy),
            "--kernel",
            str(kernel),
            "--output",
            str(output),
            "--day",
            "20260422",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["legacy_count"] == 1
    assert payload["kernel_count"] == 1
    assert payload["diff_total"] == 0


def test_run_qqq_kernel_shadow_parity_hold_and_fail_on_diff(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    kernel = tmp_path / "kernel.json"
    output = tmp_path / "report.json"
    legacy.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "decision_id": "d1",
                        "decision_ts_epoch_s": 1710000000.0,
                        "allow": True,
                        "side": "LONG",
                        "reason": "ready",
                        "symbol": "QQQ",
                        "target_qty": 10,
                        "exec_symbol": "TQQQ",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    kernel.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "decision_id": "d1",
                        "decision_ts_epoch_s": 1710000031.0,
                        "allow": False,
                        "side": "SHORT",
                        "reason": "blocked",
                        "symbol": "QQQ",
                        "target_qty": -10,
                        "exec_symbol": "SQQQ",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--legacy",
            str(legacy),
            "--kernel",
            str(kernel),
            "--output",
            str(output),
            "--day",
            "20260420",
            "--fail-on-diff",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "HOLD"
    assert payload["diff_total"] == 1
    assert payload["diff_count_by_classification"]["true_decision_diff"] == 1
    assert payload["diff_count_by_field"]["signal_authority"] == 1
    assert payload["diff_count_by_field"]["intent"] == 1
    assert payload["diff_count_by_field"]["exec_symbol"] == 1
    assert payload["diff_count_by_field"]["decision_ts_delta_sec"] == 1


def test_run_qqq_kernel_shadow_parity_collapses_flat_no_trade_semantics(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    kernel = tmp_path / "kernel.json"
    output = tmp_path / "report.json"
    legacy.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "decision_id": "d1",
                        "decision_ts_epoch_s": 1710000000.0,
                        "allow": False,
                        "side": "NA",
                        "reason": "amp_not_locked",
                        "symbol": "",
                        "target_qty": None,
                        "exec_symbol": None,
                        "qty": None,
                        "order_type": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    kernel.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "decision_id": "d1",
                        "decision_ts_epoch_s": 1710000005.0,
                        "allow": False,
                        "side": "FLAT",
                        "reason": "sign_mismatch",
                        "symbol": "",
                        "target_qty": 0.0,
                        "exec_symbol": None,
                        "qty": 0.0,
                        "order_type": "DRY_RUN",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--legacy",
            str(legacy),
            "--kernel",
            str(kernel),
            "--output",
            str(output),
            "--day",
            "20260420",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "PASS"
    assert payload["diff_total"] == 0
    assert payload["diff_count_by_classification"]["semantic_equivalent"] == 1
    assert payload["rows"][0]["blocking"] is False
    assert payload["rows"][0]["diff_classification"] == "semantic_equivalent"


def test_run_qqq_kernel_shadow_parity_tolerates_small_timing_delta(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    kernel = tmp_path / "kernel.json"
    output = tmp_path / "report.json"
    base = {
        "decision_id": "d1",
        "allow": True,
        "side": "LONG",
        "reason": "ready",
        "symbol": "QQQ",
        "target_qty": 10,
        "exec_symbol": "TQQQ",
        "qty": 10,
        "order_type": "MKT",
    }
    legacy.write_text(json.dumps({"records": [dict(base, decision_ts_epoch_s=1710000000.0)]}), encoding="utf-8")
    kernel.write_text(json.dumps({"records": [dict(base, decision_ts_epoch_s=1710000020.0)]}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--legacy",
            str(legacy),
            "--kernel",
            str(kernel),
            "--output",
            str(output),
            "--day",
            "20260420",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "PASS"
    assert payload["diff_total"] == 0


def test_run_qqq_kernel_shadow_parity_ignores_out_of_window_kernel_minutes(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    kernel = tmp_path / "kernel.json"
    output = tmp_path / "report.json"
    legacy.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "decision_id": "legacy-open",
                        "minute": "2026-04-22T09:30",
                        "ts_epoch_s": 1776864600.0,
                        "direction": "FLAT",
                        "exec_symbol": "",
                    },
                    {
                        "decision_id": "legacy-close",
                        "minute": "2026-04-22T16:00",
                        "ts_epoch_s": 1776888000.0,
                        "direction": "FLAT",
                        "exec_symbol": "",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    kernel.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "decision_id": "kernel-midnight",
                        "minute": "2026-04-22T00:00",
                        "ts_epoch_s": 1776830400.0,
                        "direction": "FLAT",
                        "reason": "session_window",
                    },
                    {
                        "decision_id": "kernel-open",
                        "minute": "2026-04-22T09:30",
                        "ts_epoch_s": 1776864600.0,
                        "direction": "FLAT",
                        "exec_symbol": "",
                    },
                    {
                        "decision_id": "kernel-close",
                        "minute": "2026-04-22T16:00",
                        "ts_epoch_s": 1776888000.0,
                        "direction": "FLAT",
                        "exec_symbol": "",
                    },
                    {
                        "decision_id": "kernel-after-hours",
                        "minute": "2026-04-22T20:00",
                        "ts_epoch_s": 1776902400.0,
                        "direction": "FLAT",
                        "reason": "session_window",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--legacy",
            str(legacy),
            "--kernel",
            str(kernel),
            "--output",
            str(output),
            "--day",
            "20260422",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "PASS"
    assert payload["diff_total"] == 0
    assert payload["ignored_out_of_window_key_count"] == 2
    assert payload["comparison_window"] == {
        "minute_floor": "202604220930",
        "minute_ceiling": "202604221600",
    }
