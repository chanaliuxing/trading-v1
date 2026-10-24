from tool.replay.run_stocks_kernel_dual_live_lot_simulation import run_dual_live_lot_simulation


def test_dual_live_lot_simulation_keeps_strategies_isolated():
    result = run_dual_live_lot_simulation()

    assert result["alpha_state"] == "closed"
    assert result["beta_state"] == "pending_exit"
    assert result["alpha_live_qty"] == 0.0
    assert result["beta_live_qty"] == 50.0
    assert result["event_count"] >= 7
