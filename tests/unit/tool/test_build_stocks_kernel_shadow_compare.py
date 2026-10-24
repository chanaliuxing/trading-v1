from tool.report.build_stocks_kernel_shadow_compare import build_shadow_compare


def test_build_shadow_compare_counts_mismatches():
    payload = build_shadow_compare(
        left=[{"symbol": "QQQ", "signal_action": "enter"}, {"symbol": "QQQ", "signal_action": "hold"}],
        right=[{"symbol": "QQQ", "signal_action": "enter"}, {"symbol": "QQQ", "signal_action": "enter"}],
    )

    assert payload["left_count"] == 2
    assert payload["right_count"] == 2
    assert payload["mismatch_count"] == 1
    assert payload["match_ratio"] == 0.5
