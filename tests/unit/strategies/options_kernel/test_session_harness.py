from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from strategies.options_kernel.harness import run_minute_session_parity_scenario_sync
from strategies.qqq_1dte_ma10_reject_live_strategy import QQQ1DTEMA10RejectLiveStrategy


ET = ZoneInfo("America/New_York")


def _assert_core_env_equivalence(old_env, new_env) -> None:
    assert old_env.symbol == new_env.symbol
    assert old_env.target_qty == new_env.target_qty
    assert old_env.order_style.kind == new_env.order_style.kind
    for key in (
        "instrument_type",
        "underlying",
        "bars_symbol",
        "risk_symbol",
        "contracts_mult",
        "ref_px",
        "ref_px_source",
        "opt_bid_px",
        "opt_ask_px",
        "opt_mid_px",
        "opt_last_px",
        "opt_quote_age_sec",
    ):
        assert old_env.extras.get(key) == new_env.extras.get(key)


def test_minute_session_parity_harness_matches_single_entry_flow(tmp_path: Path):
    start = datetime(2026, 3, 25, 9, 30, 0, tzinfo=ET)
    seed_bars = []
    for i in range(220):
        close = 500.0 + (i * 0.2)
        seed_bars.append(
            {
                "ts": (start + timedelta(minutes=i)).isoformat(),
                "open": close - 0.05,
                "high": close + 0.20,
                "low": close - 0.20,
                "close": close,
                "vwap": close - 0.10,
                "volume": 1000.0,
            }
        )

    setup_close = 500.0 + (219 * 0.2)
    setup_ts = start + timedelta(minutes=220)
    confirm_close = setup_close + 0.30
    bars = [
        {
            "ts": setup_ts.isoformat(),
            "open": setup_close - 0.20,
            "high": setup_close + 0.15,
            "low": setup_close - 1.10,
            "close": setup_close,
            "vwap": setup_close - 0.10,
            "volume": 5000.0,
        },
        {
            "ts": (setup_ts + timedelta(minutes=1)).isoformat(),
            "open": confirm_close - 0.10,
            "high": (setup_close + 0.15) + 0.20,
            "low": (setup_close - 1.10) + 0.10,
            "close": confirm_close,
            "vwap": confirm_close - 0.10,
            "volume": 2200.0,
        },
    ]

    result = run_minute_session_parity_scenario_sync(
        {
            "snapshot_root": str(tmp_path / "snapshots"),
            "strategy_extras": {
                "preload_history_bars": 0,
                "enable_fresh_momentum_branch": False,
                "enable_daily_best_additive_branch": False,
            },
            "quote": {
                "opt_symbol": "OPT:QQQ:20260326:C:544",
                "strike": 544.0,
                "bid": 2.00,
                "ask": 2.10,
                "mid": 2.05,
                "last": 2.06,
                "quote_age_sec": 15.0,
            },
            "seed_bars": seed_bars,
            "bars": bars,
        }
    )

    _assert_core_env_equivalence(result["old_envs"][0], result["new_envs"][0])
    assert result["old_count"] == 1
    assert result["new_count"] == 1


def test_minute_session_parity_harness_uses_real_selector_when_quote_not_supplied(monkeypatch, tmp_path: Path):
    start = datetime(2026, 3, 25, 9, 30, 0, tzinfo=ET)
    seed_bars = []
    for i in range(220):
        close = 500.0 + (i * 0.2)
        seed_bars.append(
            {
                "ts": (start + timedelta(minutes=i)).isoformat(),
                "open": close - 0.05,
                "high": close + 0.20,
                "low": close - 0.20,
                "close": close,
                "vwap": close - 0.10,
                "volume": 1000.0,
            }
        )

    setup_close = 500.0 + (219 * 0.2)
    setup_ts = start + timedelta(minutes=220)
    confirm_close = setup_close + 0.30
    bars = [
        {
            "ts": setup_ts.isoformat(),
            "open": setup_close - 0.20,
            "high": setup_close + 0.15,
            "low": setup_close - 1.10,
            "close": setup_close,
            "vwap": setup_close - 0.10,
            "volume": 5000.0,
        },
        {
            "ts": (setup_ts + timedelta(minutes=1)).isoformat(),
            "open": confirm_close - 0.10,
            "high": (setup_close + 0.15) + 0.20,
            "low": (setup_close - 1.10) + 0.10,
            "close": confirm_close,
            "vwap": confirm_close - 0.10,
            "volume": 2200.0,
        },
    ]

    def _fake_select(self, *args, **kwargs):
        return (
            {
                "opt_symbol": "OPT:QQQ:20260326:C:544",
                "strike": 544.0,
                "bid": 2.00,
                "ask": 2.10,
                "mid": 2.05,
                "last": 2.06,
                "quote_age_sec": 15.0,
            },
            None,
        )

    monkeypatch.setattr(QQQ1DTEMA10RejectLiveStrategy, "_select_0dte_quote", _fake_select)

    result = run_minute_session_parity_scenario_sync(
        {
            "snapshot_root": str(tmp_path / "snapshots"),
            "strategy_extras": {
                "preload_history_bars": 0,
                "enable_fresh_momentum_branch": False,
                "enable_daily_best_additive_branch": False,
            },
            "seed_bars": seed_bars,
            "bars": bars,
        }
    )

    _assert_core_env_equivalence(result["old_envs"][0], result["new_envs"][0])
    assert result["old_count"] == 1
    assert result["new_count"] == 1
