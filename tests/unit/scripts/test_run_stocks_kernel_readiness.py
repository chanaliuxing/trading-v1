import json
from pathlib import Path

from tool.replay.run_stocks_kernel_readiness import _run


def test_run_stocks_kernel_readiness_reports_blocked_runtime(tmp_path):
    config = {
        "strategy_id": "stocks_kernel_ready_script_001",
        "ttl_ms": 2000,
        "extras": {
            "dry_run": True,
            "startup_preflight_enabled": False,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    }
    runtime_state = {
        "kill_switches": {"TQQQ": "broker_truth_mismatch"},
        "lots": [
            {
                "position_lot_id": "lot-1",
                "strategy_id": "stocks_kernel_ready_script_001",
                "symbol": "TQQQ",
                "side": "LONG",
                "opened_by_decision_id": "d1",
                "entry_order_ref": "oref-1",
                "live_qty": 1.0,
                "filled_qty": 1.0,
                "avg_entry_price": 100.0,
                "state": "live",
                "extras": {"protection_state": {"protection_lifecycle": "flatten_only", "freeze_required": True}},
            }
        ],
    }

    import asyncio

    payload = asyncio.run(_run(config=config, runtime_state=runtime_state))

    assert payload["ready"] is False
    assert any(check["check_id"] == "symbol_kill_switches" and check["status"] == "block" for check in payload["checks"])
    assert any(check["check_id"] == "protection_invariants" and check["status"] == "block" for check in payload["checks"])


def test_run_stocks_kernel_readiness_uses_runtime_broker_snapshots_for_preflight():
    config = {
        "strategy_id": "stocks_kernel_ready_script_002",
        "ttl_ms": 2000,
        "extras": {
            "dry_run": False,
            "startup_preflight_enabled": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    }
    runtime_state = {
        "broker_positions": [],
        "broker_open_orders": [],
    }

    import asyncio

    payload = asyncio.run(_run(config=config, runtime_state=runtime_state))

    assert payload["ready"] is True
    preflight = next(check for check in payload["checks"] if check["check_id"] == "startup_preflight")
    assert preflight["status"] == "pass"
    assert preflight["reason"] == "ready"
