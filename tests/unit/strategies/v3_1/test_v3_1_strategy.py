import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.v3_1.exit_manager import PositionState, evaluate_exits
from strategies.v3_1.lanes import lane2_pullback
from strategies.v3_1.market_state import evaluate_state
from strategies.v3_1.params_store import ParamSet
from strategies.v3_1.scoring import structure_space
from strategies.v3_1.hedge import hedge_action
from strategies.v3_1.scarcity import ScarcityController
from strategies.v3_1.execution_guard import ExecutionGuard
from strategies.v3_1.exit_manager import build_exit_plan
from strategies.v3_1.indicators import wall_dist_pct


def test_lane2_pullback_trigger():
    features = {"close": 101.0}
    signal = lane2_pullback(features, anchor=100.0, pullback_low=99.5, pullback_high=100.5, bars_since_touch=1)
    assert signal is not None
    assert signal.lane == "LANE_2_PULLBACK"


def test_protect_moves_stop():
    params = ParamSet()
    position = PositionState(entry_px=100.0, stop0=95.0, r_per_share=5.0, lane="LANE_2_PULLBACK", entry_ts=0)
    actions = evaluate_exits(params, position, close_px=103.6, ema10=102.0, ema20=101.0, atr=2.0, bars_held=2, mfe_r=1.0, range_5d=5.0)
    assert any(a.action == "MOVE_STOP" for a in actions)


def test_gap_trim_cumulative_targets():
    plan = build_exit_plan(entry_px=100.0, r_per_share=2.0, lane="LANE_1_TREND", targets={"T_W1": 106.0})
    prices = [t.price for t in plan.targets]
    assert prices == sorted(prices)


def test_structure_space_scoring():
    params = ParamSet()
    assert structure_space(0.08, params) == 25.0


def test_no_progress_exit():
    params = ParamSet()
    position = PositionState(entry_px=100.0, stop0=95.0, r_per_share=5.0, lane="LANE_1_TREND", entry_ts=0)
    actions = evaluate_exits(params, position, close_px=100.5, ema10=100.0, ema20=100.0, atr=2.0, bars_held=20, mfe_r=1.0, range_5d=1.0)
    assert any(a.action == "EXIT_ALL" for a in actions)


def test_state_off_gate():
    params = ParamSet()
    feats = {
        "SPY": {"close": 400.0, "ema50": 410.0, "ema20": 405.0, "ema20_slope": -0.5},
        "AAPL": {"close": 100.0, "ema20": 101.0},
    }
    state = evaluate_state(feats, params)
    assert state.state == "OFF"


def test_scarcity_expands_watchlist(tmp_path):
    params = ParamSet(watchlist_base=["A"], watchlist_extended=["A", "B"])
    controller = ScarcityController(str(tmp_path / "signals.jsonl"))
    decision = controller.decide(params, "TREND_OK")
    assert "B" in decision.watchlist


def test_hedge_cost_ratio_reduces_size():
    action = hedge_action("B", hedge_cost_ratio=0.5, threshold=0.35)
    assert action.method == "SIZE_REDUCTION"
    assert action.size_reduction_pct > 0


def test_open_order_conflict_detection():
    guard = ExecutionGuard(enable_lock=False)
    asyncio.run(guard._update_open_index(1, [(123, "BUY")], "Submitted"))
    conflicts = asyncio.run(guard._check_conflicts(123, "BUY"))
    assert 1 in conflicts


def test_cancel_waits_for_cancelled():
    class DummyStatus:
        def __init__(self, status):
            self.status = status

    class DummyMonitor:
        async def wait_for_fill(self, order_id: int, timeout_sec: int = 15):
            return DummyStatus("Cancelled")

    guard = ExecutionGuard(enable_lock=False)
    guard._order_monitor = DummyMonitor()
    asyncio.run(guard._wait_cancelled(99, timeout_sec=1))
