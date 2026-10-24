import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.netting import NetChildIntent, NettingCoordinator


def test_netting_coordinator_builds_single_parent_plan_for_same_side():
    c = NettingCoordinator()
    intents = [
        NetChildIntent(strategy_id="s1", decision_id="d1", symbol="tqqq", side="buy", qty=10, priority=40),
        NetChildIntent(strategy_id="s2", decision_id="d2", symbol="TQQQ", side="BUY", qty=5, priority=60),
    ]

    out = c.build_group(intents)

    assert out.status == "NETTED"
    assert out.parent_plan is not None
    assert out.parent_plan.symbol == "TQQQ"
    assert out.parent_plan.side == "BUY"
    assert out.parent_plan.qty == 15.0
    assert [i.strategy_id for i in out.accepted_children] == ["s2", "s1"]


def test_netting_coordinator_rejects_mixed_side_in_same_batch():
    c = NettingCoordinator()
    intents = [
        NetChildIntent(strategy_id="s1", decision_id="d1", symbol="TQQQ", side="BUY", qty=10),
        NetChildIntent(strategy_id="s2", decision_id="d2", symbol="TQQQ", side="SELL", qty=2),
    ]

    out = c.build_group(intents)

    assert out.status == "NETTED"
    assert out.parent_plan is not None
    assert out.parent_plan.side == "BUY"
    assert out.parent_plan.qty == 10.0
    assert len(out.rejected_children) == 1
    assert out.rejected_children[0].strategy_id == "s2"


def test_netting_coordinator_rejects_when_no_compatible_intents():
    c = NettingCoordinator()
    intents = [
        NetChildIntent(strategy_id="s1", decision_id="d1", symbol="TQQQ", side="BUY", qty=0),
    ]

    out = c.build_group(intents)

    assert out.status == "REJECTED"
    assert out.parent_plan is None
