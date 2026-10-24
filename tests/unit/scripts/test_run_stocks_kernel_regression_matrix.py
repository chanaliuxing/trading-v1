import asyncio

from tool.replay.run_stocks_kernel_regression_matrix import _run


def test_run_stocks_kernel_regression_matrix_passes_two_scenarios():
    config = {
        "strategy_id": "stocks_kernel_matrix_script_001",
        "ttl_ms": 2000,
        "extras": {
            "dry_run": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    }
    scenarios = [
        {
            "name": "trend_long",
            "required_symbols": ["QQQ"],
            "frames": [
                {
                    "symbol": "QQQ",
                    "features": {
                        "ts_epoch_s": 100.0,
                        "now_epoch_s": 101.0,
                        "last_price": 500.0,
                        "session": "RTH",
                        "vwap": 499.0,
                        "planned_qty": 2,
                        "trigger_id": "m1",
                        "bars_1m": [
                            {"ts_epoch_s": float(i), "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 10 * i}
                            for i in range(1, 16)
                        ],
                    },
                }
            ],
        },
        {
            "name": "trend_long_2",
            "required_symbols": ["QQQ"],
            "frames": [
                {
                    "symbol": "QQQ",
                    "features": {
                        "ts_epoch_s": 200.0,
                        "now_epoch_s": 201.0,
                        "last_price": 400.0,
                        "session": "RTH",
                        "vwap": 399.0,
                        "planned_qty": 1,
                        "trigger_id": "m2",
                        "bars_1m": [
                            {"ts_epoch_s": float(i), "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 10 * i}
                            for i in range(1, 16)
                        ],
                    },
                }
            ],
        },
    ]

    payload = asyncio.run(_run(config=config, scenarios_payload=scenarios))

    assert payload["passed"] is True
    assert payload["scenario_count"] == 2
    assert payload["passed_count"] == 2
    assert "coverage" in payload
    assert payload["coverage"]["coverage_ok"] is True


def test_run_stocks_kernel_regression_matrix_uses_default_scenarios_when_missing():
    config = {
        "strategy_id": "stocks_kernel_matrix_script_default_001",
        "ttl_ms": 2000,
        "extras": {
            "dry_run": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    }

    payload = asyncio.run(_run(config=config, scenarios_payload=[]))

    assert payload["passed"] is True
    assert payload["scenario_count"] == 3
    assert payload["coverage"]["minimum_required_scenarios"] == 3
    assert payload["coverage"]["coverage_ok"] is True
