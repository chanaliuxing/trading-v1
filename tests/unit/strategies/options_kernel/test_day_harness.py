from strategies.options_kernel.harness import run_clickhouse_day_parity_scenario_sync


def test_clickhouse_day_parity_harness_uses_loaded_bars(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "strategies.options_kernel.harness._load_day_bars_from_clickhouse",
        lambda **kwargs: [
            {
                "ts": "2026-03-25T13:10:00-04:00",
                "open": 543.6,
                "high": 543.7,
                "low": 542.6,
                "close": 542.8,
                "vwap": 543.1,
                "volume": 5000.0,
            },
            {
                "ts": "2026-03-25T13:11:00-04:00",
                "open": 542.7,
                "high": 542.9,
                "low": 542.4,
                "close": 542.5,
                "vwap": 542.9,
                "volume": 2200.0,
            },
        ],
    )

    result = run_clickhouse_day_parity_scenario_sync(
        {
            "day": "2026-03-25",
            "symbol": "QQQ",
            "session_mode": "extended",
            "snapshot_root": str(tmp_path / "snapshots"),
            "strategy_extras": {
                "preload_history_bars": 0,
                "enable_fresh_momentum_branch": False,
                "enable_daily_best_additive_branch": False,
            },
            "quote": {
                "opt_symbol": "OPT:QQQ:20260326:P:542",
                "strike": 542.0,
                "bid": 3.90,
                "ask": 4.02,
                "mid": 3.96,
                "last": 3.98,
                "quote_age_sec": 12.0,
            },
        }
    )

    assert result["old_count"] == result["new_count"]
    assert "diff" in result
