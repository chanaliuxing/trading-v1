from tool.report.build_stocks_kernel_launch_go_no_go_gate import build_stocks_kernel_launch_go_no_go_gate


def test_build_stocks_kernel_launch_go_no_go_gate_blocks_when_readiness_or_matrix_fail() -> None:
    report = build_stocks_kernel_launch_go_no_go_gate(
        readiness_report={
            "ready": False,
            "reason": "not_ready",
            "blocking_checks": [
                {"check_id": "symbol_kill_switches"},
                {"check_id": "protection_invariants"},
            ],
        },
        matrix_report={"passed": False, "reason": "matrix_failed", "scenario_count": 1},
        audit_summary={"rows": 12, "blocked_events": 2, "kill_switch_events": 1, "protection_events": 1},
    )

    assert report["recommendation"] == "BLOCK"
    assert "runtime_readiness_not_ready" in report["blockers"]
    assert "active_symbol_kill_switches" in report["blockers"]
    assert "unsafe_live_positions_present" in report["blockers"]
    assert "regression_matrix_failed" in report["blockers"]
    assert "regression_matrix_insufficient" in report["blockers"]
    assert "recent_kill_switch_events_present" in report["warnings"]


def test_build_stocks_kernel_launch_go_no_go_gate_go_when_all_green() -> None:
    report = build_stocks_kernel_launch_go_no_go_gate(
        readiness_report={
            "ready": True,
            "reason": "ready",
            "blocking_checks": [],
        },
        matrix_report={"passed": True, "reason": "ready", "scenario_count": 3},
        audit_summary={"rows": 18, "blocked_events": 0, "kill_switch_events": 0, "protection_events": 0},
    )

    assert report["recommendation"] == "GO"
    assert report["blocker_count"] == 0
    assert report["warning_count"] == 0
