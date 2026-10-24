from tool.report.build_live_strategy_surface_report import evaluate_phase0_freeze_gate


def test_phase0_freeze_gate_blocks_shadow_named_live_config() -> None:
    gate = evaluate_phase0_freeze_gate(
        {
            "strategies": [
                {
                    "strategy_id": "stocks_kernel_shadow_spy_001",
                    "risk_flags": ["live_strategy", "shadow_named_but_live_config"],
                },
                {
                    "strategy_id": "qqq_intraday_001",
                    "risk_flags": ["live_strategy"],
                },
            ],
            "summary": {"active_strategy_count": 2},
        }
    )

    assert gate["ok"] is False
    assert gate["blocking_strategy_ids"] == ["stocks_kernel_shadow_spy_001"]
    assert gate["blocking_reasons"] == ["stocks_kernel_shadow_spy_001:shadow_named_but_live_config"]


def test_phase0_freeze_gate_blocks_new_live_strategy_vs_baseline() -> None:
    baseline = {
        "live_strategy_ids": ["qqq_intraday_001"],
        "shadow_strategy_ids": ["stocks_kernel_shadow_spy_001"],
        "summary": {"live_strategy_count": 1},
        "strategies": [
            {"strategy_id": "qqq_intraday_001", "mode": "live", "risk_flags": ["live_strategy"]},
            {"strategy_id": "stocks_kernel_shadow_spy_001", "mode": "shadow", "risk_flags": []},
        ],
    }
    report = {
        "live_strategy_ids": ["qqq_intraday_001", "spy_1dte_ma10_reject_0324_001"],
        "shadow_strategy_ids": ["stocks_kernel_shadow_spy_001"],
        "summary": {"live_strategy_count": 2},
        "strategies": [
            {"strategy_id": "qqq_intraday_001", "mode": "live", "risk_flags": ["live_strategy"]},
            {"strategy_id": "spy_1dte_ma10_reject_0324_001", "mode": "live", "risk_flags": ["live_strategy"]},
            {"strategy_id": "stocks_kernel_shadow_spy_001", "mode": "shadow", "risk_flags": []},
        ],
    }

    gate = evaluate_phase0_freeze_gate(report, baseline=baseline)

    assert gate["ok"] is False
    assert gate["blocking_strategy_ids"] == ["spy_1dte_ma10_reject_0324_001"]
    assert gate["blocking_reasons"] == [
        "spy_1dte_ma10_reject_0324_001:new_live_strategy_not_in_phase0_baseline"
    ]


def test_phase0_freeze_gate_blocks_shadow_promotion_vs_baseline() -> None:
    baseline = {
        "live_strategy_ids": ["qqq_intraday_001"],
        "shadow_strategy_ids": ["stocks_kernel_shadow_spy_001"],
        "strategies": [
            {"strategy_id": "qqq_intraday_001", "mode": "live", "risk_flags": ["live_strategy"]},
            {"strategy_id": "stocks_kernel_shadow_spy_001", "mode": "shadow", "risk_flags": []},
        ],
    }
    report = {
        "live_strategy_ids": ["qqq_intraday_001", "stocks_kernel_shadow_spy_001"],
        "shadow_strategy_ids": [],
        "strategies": [
            {"strategy_id": "qqq_intraday_001", "mode": "live", "risk_flags": ["live_strategy"]},
            {"strategy_id": "stocks_kernel_shadow_spy_001", "mode": "live", "risk_flags": ["live_strategy"]},
        ],
    }

    gate = evaluate_phase0_freeze_gate(report, baseline=baseline)

    assert gate["ok"] is False
    assert gate["blocking_strategy_ids"] == ["stocks_kernel_shadow_spy_001"]
    assert gate["blocking_reasons"] == ["stocks_kernel_shadow_spy_001:shadow_promoted_to_live"]


def test_phase0_freeze_gate_blocks_shadow_even_when_legacy_allow_flags_are_passed() -> None:
    baseline = {
        "live_strategy_ids": ["qqq_intraday_001"],
        "shadow_strategy_ids": ["stocks_kernel_shadow_spy_001"],
        "strategies": [
            {"strategy_id": "qqq_intraday_001", "mode": "live", "risk_flags": ["live_strategy"]},
            {"strategy_id": "stocks_kernel_shadow_spy_001", "mode": "shadow", "risk_flags": []},
        ],
    }
    report = {
        "live_strategy_ids": ["qqq_intraday_001", "stocks_kernel_shadow_spy_001"],
        "shadow_strategy_ids": [],
        "strategies": [
            {"strategy_id": "qqq_intraday_001", "mode": "live", "risk_flags": ["live_strategy"]},
            {
                "strategy_id": "stocks_kernel_shadow_spy_001",
                "mode": "live",
                "risk_flags": ["live_strategy", "shadow_named_but_live_config"],
            },
        ],
    }

    gate = evaluate_phase0_freeze_gate(
        report,
        baseline=baseline,
        allow_shadow_named_live=True,
        allow_shadow_promotion=True,
    )

    assert gate["ok"] is False
    assert gate["blocking_strategy_ids"] == ["stocks_kernel_shadow_spy_001", "stocks_kernel_shadow_spy_001"]
    assert gate["blocking_reasons"] == [
        "stocks_kernel_shadow_spy_001:shadow_named_but_live_config",
        "stocks_kernel_shadow_spy_001:shadow_promoted_to_live",
    ]


def test_phase0_freeze_gate_requires_baseline_when_requested() -> None:
    gate = evaluate_phase0_freeze_gate(
        {"live_strategy_ids": [], "shadow_strategy_ids": [], "strategies": []},
        require_baseline=True,
    )

    assert gate["ok"] is False
    assert gate["blocking_reasons"] == ["phase0_freeze_baseline_missing"]
