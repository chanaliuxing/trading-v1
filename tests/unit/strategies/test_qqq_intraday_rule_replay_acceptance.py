from datetime import datetime

from strategies.qqq_intraday_rules import ReplaySample, load_entry_recipe, run_replay_samples


def test_branch_replay_samples_cover_all_entry_models() -> None:
    samples = [
        ReplaySample(
            name="baseline_long_allowed",
            now=datetime(2026, 4, 3, 10, 5, 0),
            spot=500.0,
            features={"v3m": 0.9, "vwap": 499.1, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            extras={"entry_model": "baseline", "qqew_decoupled": True},
            expected_allowed=True,
            expected_stock_side="LONG",
            expected_reason="direction_allowed",
        ),
        ReplaySample(
            name="v47_fast_drift_blocked_pre0945",
            now=datetime(2026, 4, 3, 9, 35, 0),
            spot=500.0,
            features={"v3m": 0.9, "vwap": 499.0, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            # atr_rank_prev_day 显式置 None，模拟没有 daily ATR 文件的环境
            extras={"entry_model": "v47_fast_drift", "qqew_decoupled": True, "qqq_daily_atr_path": "/dev/null"},
            expected_allowed=False,
            expected_stock_side="NONE",
            expected_reason="atr_rank_not_ready",
        ),
        ReplaySample(
            name="v46_ab_opening_allowed",
            now=datetime(2026, 4, 3, 9, 35, 0),
            spot=500.0,
            features={"v3m": 1.0, "vwap": 499.0, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True, "volume": 2000.0},
            extras={"entry_model": "v46_ab", "qqew_decoupled": True, "opening_breakout_vol_min": 0.0},
            expected_allowed=True,
            expected_stock_side="LONG",
            expected_reason="direction_allowed",
        ),
        ReplaySample(
            name="v44_elite_blocked_overextended",
            now=datetime(2026, 4, 3, 10, 15, 0),
            spot=505.0,
            features={"v3m": 0.8, "vwap": 499.0, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            extras={"entry_model": "v44_elite", "qqew_decoupled": True},
            expected_allowed=False,
            expected_stock_side="NONE",
            expected_reason="elite_overextended",
        ),
    ]
    results = run_replay_samples(samples)
    assert len(results) == 4
    for row in results:
        assert row.allowed == row.expected_allowed
        assert row.stock_side == row.expected_stock_side
        assert row.reason == row.expected_reason


def test_block_reasons_are_trader_readable() -> None:
    samples = [
        ReplaySample(
            name="reason_data_not_ready",
            now=datetime(2026, 4, 3, 10, 5, 0),
            spot=500.0,
            features={"v3m": 0.9, "vwap": 499.1, "atr_eff": 0.0, "atr_seed": 0.0, "qqew_decoupled": True},
            extras={"entry_model": "baseline", "qqew_decoupled": True},
            expected_allowed=False,
            expected_stock_side="NONE",
            expected_reason="data_not_ready",
        ),
        ReplaySample(
            name="reason_ownership_conflict",
            now=datetime(2026, 4, 3, 10, 5, 0),
            spot=500.0,
            features={"v3m": 0.9, "vwap": 499.1, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            extras={"entry_model": "baseline", "qqew_decoupled": True, "ownership_conflict": True},
            expected_allowed=False,
            expected_stock_side="NONE",
            expected_reason="ownership_conflict",
        ),
        ReplaySample(
            name="reason_first_entry_seed_not_ready",
            now=datetime(2026, 4, 3, 10, 5, 0),
            spot=500.0,
            features={"v3m": 0.9, "vwap": 499.1, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            extras={
                "entry_model": "baseline",
                "qqew_decoupled": True,
                "first_entry_require_seed_ready": True,
                "first_entry_seed_min_samples": 600,
                "seed_n": 120,
            },
            expected_allowed=False,
            expected_stock_side="NONE",
            expected_reason="first_entry_seed_not_ready",
        ),
        ReplaySample(
            name="reason_data_integrity_fail_closed",
            now=datetime(2026, 4, 3, 10, 5, 0),
            spot=500.0,
            features={"v3m": 0.9, "vwap": 499.1, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            extras={
                "entry_model": "baseline",
                "qqew_decoupled": True,
                "data_integrity_fail_closed": True,
            },
            expected_allowed=False,
            expected_stock_side="NONE",
            expected_reason="data_integrity_fail_closed",
        ),
    ]
    results = run_replay_samples(samples)
    assert [r.reason for r in results] == [
        "data_not_ready",
        "ownership_conflict",
        "first_entry_seed_not_ready",
        "data_integrity_fail_closed",
    ]
    assert [r.trader_reason_code for r in results] == [
        "SIGNAL_GATE_FAIL_DATA_NOT_READY",
        "OWNERSHIP_CONFLICT",
        "SIGNAL_GATE_FAIL_FIRST_ENTRY_SEED_NOT_READY",
        "SIGNAL_GATE_FAIL_DATA_INTEGRITY_FAIL_CLOSED",
    ]


def test_recipe_can_combine_modules_without_strategy_fork() -> None:
    recipe = load_entry_recipe(
        {
            "entry_model": "baseline",
            "entry_recipe": {
                "session_module": "baseline_rth_window",
                "direction_module": "ab_direction",
                "guards": [
                    "amp_lock_guard",
                    "stable_entry_guard",
                    "restart_degrade_guard",
                    "startup_anchor_guard",
                    "open_orders_guard",
                    "live_position_guard",
                    "pending_decision_sync_guard",
                    "ownership_guard",
                    "qqq_freshness_guard",
                    "qqew_freshness_guard",
                    "continuity_180s_guard",
                    "atr_ready_guard",
                    "vwap_ready_guard",
                    "volume_ready_guard",
                ],
                "overlays": ["day_shape_overlay", "high_vol_confirm_veto"],
                "translator": "qqq_option_translation",
            },
        }
    )
    assert recipe.direction_module == "ab_direction"
    assert recipe.overlays == ("day_shape_overlay", "high_vol_confirm_veto")
