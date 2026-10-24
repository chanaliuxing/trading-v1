from tool.report.build_stocks_kernel_parity_report import build_stocks_kernel_parity_report


def test_build_stocks_kernel_replay_parity_report_counts_diffs():
    payload = build_stocks_kernel_parity_report(
        expected=[
            {"symbol": "QQQ", "allowed": True, "signal_action": "enter", "signal_side": "LONG", "entry_qty": 2.0},
            {"symbol": "QQQ", "allowed": False, "signal_action": "hold", "signal_side": None, "entry_qty": None},
        ],
        actual=[
            {"symbol": "QQQ", "allowed": True, "signal_action": "enter", "signal_side": "LONG", "entry_qty": 2.0},
            {"symbol": "QQQ", "allowed": False, "signal_action": "hold", "signal_side": "SHORT", "entry_qty": None},
        ],
    )

    assert payload["matched_count"] == 1
    assert payload["expected_count"] == 2
    assert payload["actual_count"] == 2
    assert payload["match_ratio"] == 0.5
    assert payload["comparisons"][1]["field_diffs"][0]["field"] == "signal_side"
