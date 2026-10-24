import json
import subprocess
import sys
from pathlib import Path


def test_run_stocks_kernel_trader_parity_gate_go(tmp_path: Path) -> None:
    identity_path = tmp_path / "identity.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "gate.json"

    identity_path.write_text(
        json.dumps(
            {
                "ok": True,
                "parity": {
                    "allow_match_ratio": 0.995,
                    "side_match_ratio": 0.991,
                    "reason_match_ratio": 0.992,
                    "exec_symbol_match_ratio": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(
            {
                "strategy_sections": [
                    {
                        "strategy_id": "qqq_intraday_001",
                        "fills_count": 6,
                        "open_trigger_count": 0,
                        "position_notional_abs_usd": 0.0,
                    },
                    {
                        "strategy_id": "stocks_kernel_shadow_smh_001",
                        "fills_count": 8,
                        "open_trigger_count": 0,
                        "position_notional_abs_usd": 0.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(Path("./tool/replay/run_stocks_kernel_trader_parity_gate.py")),
            "--identity-report",
            str(identity_path),
            "--daily-summary-report",
            str(summary_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "GO"
    assert payload["failed_checks"] == []


def test_run_stocks_kernel_trader_parity_gate_hold(tmp_path: Path) -> None:
    identity_path = tmp_path / "identity.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "gate.json"

    identity_path.write_text(
        json.dumps(
            {
                "ok": True,
                "parity": {
                    "allow_match_ratio": 0.91,
                    "side_match_ratio": 0.89,
                    "reason_match_ratio": 0.87,
                    "exec_symbol_match_ratio": 0.6,
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(
            {
                "strategy_sections": [
                    {
                        "strategy_id": "qqq_intraday_001",
                        "fills_count": 5,
                        "open_trigger_count": 0,
                        "position_notional_abs_usd": 0.0,
                    },
                    {
                        "strategy_id": "stocks_kernel_shadow_smh_001",
                        "fills_count": 19,
                        "open_trigger_count": 4,
                        "position_notional_abs_usd": 1234.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(Path("./tool/replay/run_stocks_kernel_trader_parity_gate.py")),
            "--identity-report",
            str(identity_path),
            "--daily-summary-report",
            str(summary_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "HOLD"
    assert "signal_allow_parity" in payload["failed_checks"]
    assert "signal_side_parity" in payload["failed_checks"]
    assert "signal_reason_parity" in payload["failed_checks"]
    assert "exec_symbol_parity" in payload["failed_checks"]
    assert "cadence_fill_count_guard" in payload["failed_checks"]
    assert "same_day_flatten_guard" in payload["failed_checks"]


def test_run_stocks_kernel_trader_parity_gate_hold_when_summary_sections_missing(tmp_path: Path) -> None:
    identity_path = tmp_path / "identity.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "gate.json"

    identity_path.write_text(
        json.dumps(
            {
                "ok": True,
                "parity": {
                    "allow_match_ratio": 0.995,
                    "side_match_ratio": 0.995,
                    "reason_match_ratio": 0.995,
                    "exec_symbol_match_ratio": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text(json.dumps({"strategy_sections": []}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(Path("./tool/replay/run_stocks_kernel_trader_parity_gate.py")),
            "--identity-report",
            str(identity_path),
            "--daily-summary-report",
            str(summary_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "HOLD"
    assert "legacy_summary_present" in payload["failed_checks"]
    assert "kernel_summary_present" in payload["failed_checks"]


def test_run_stocks_kernel_trader_parity_gate_hold_when_reason_or_exec_parity_missing(tmp_path: Path) -> None:
    identity_path = tmp_path / "identity.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "gate.json"

    identity_path.write_text(
        json.dumps(
            {
                "ok": True,
                "parity": {
                    "allow_match_ratio": 0.995,
                    "side_match_ratio": 0.995,
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(
            {
                "strategy_sections": [
                    {
                        "strategy_id": "qqq_intraday_001",
                        "fills_count": 3,
                        "open_trigger_count": 0,
                        "position_notional_abs_usd": 0.0,
                    },
                    {
                        "strategy_id": "stocks_kernel_shadow_smh_001",
                        "fills_count": 3,
                        "open_trigger_count": 0,
                        "position_notional_abs_usd": 0.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(Path("./tool/replay/run_stocks_kernel_trader_parity_gate.py")),
            "--identity-report",
            str(identity_path),
            "--daily-summary-report",
            str(summary_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "HOLD"
    assert "signal_reason_parity" in payload["failed_checks"]
    assert "exec_symbol_parity" in payload["failed_checks"]
