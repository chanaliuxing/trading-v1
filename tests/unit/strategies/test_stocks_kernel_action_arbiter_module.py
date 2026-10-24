from strategies.stocks_kernel.modules.action_arbiter import ActionArbiter
from strategies.stocks_kernel.types import ActionPlan, ActionType


def test_action_arbiter_flatten_wins_over_entry_and_add():
    arbiter = ActionArbiter()
    plans = (
        ActionPlan(action_type=ActionType.ENTRY, symbol="SOXL", side="LONG", qty=10, priority=20),
        ActionPlan(action_type=ActionType.FLATTEN, symbol="SOXL", qty=10, priority=50),
        ActionPlan(action_type=ActionType.ADD, symbol="SOXL", side="LONG", qty=5, priority=10),
    )

    decision = arbiter.arbitrate(action_plans=plans, freeze_entry=False)

    assert [p.action_type for p in decision.winning_actions] == [ActionType.FLATTEN]
    assert sorted([p.action_type for p in decision.blocked_actions]) == [ActionType.ADD, ActionType.ENTRY]
    assert decision.reason == "suppressed_lower_priority"


def test_action_arbiter_freeze_blocks_entry_family():
    arbiter = ActionArbiter()
    plans = (
        ActionPlan(action_type=ActionType.ENTRY, symbol="SOXS", side="SHORT", qty=8, priority=10),
        ActionPlan(action_type=ActionType.ADD, symbol="SOXS", side="SHORT", qty=2, priority=20),
    )

    decision = arbiter.arbitrate(action_plans=plans, freeze_entry=True)

    assert decision.winning_actions == ()
    assert sorted([p.action_type for p in decision.blocked_actions]) == [ActionType.ADD, ActionType.ENTRY]
    assert decision.reason == "entry_frozen"


def test_action_arbiter_allows_cancel_working_plus_flatten():
    arbiter = ActionArbiter()
    plans = (
        ActionPlan(action_type=ActionType.CANCEL_WORKING, symbol="SOXL", priority=5),
        ActionPlan(action_type=ActionType.FLATTEN, symbol="SOXL", qty=10, priority=20),
        ActionPlan(action_type=ActionType.ENTRY, symbol="SOXL", side="LONG", qty=5, priority=30),
    )

    decision = arbiter.arbitrate(action_plans=plans, freeze_entry=False)

    assert [p.action_type for p in decision.winning_actions] == [ActionType.CANCEL_WORKING, ActionType.FLATTEN]
    assert [p.action_type for p in decision.blocked_actions] == [ActionType.ENTRY]


def test_action_arbiter_allows_take_profit_with_protection_rearm():
    arbiter = ActionArbiter()
    plans = (
        ActionPlan(action_type=ActionType.TAKE_PROFIT, symbol="TQQQ", qty=2, position_lot_id="lot-1", priority=10),
        ActionPlan(action_type=ActionType.ENSURE_PROTECTION, symbol="TQQQ", position_lot_id="lot-1", priority=20),
        ActionPlan(action_type=ActionType.REDUCE, symbol="TQQQ", qty=1, position_lot_id="lot-1", priority=30),
    )

    decision = arbiter.arbitrate(action_plans=plans, freeze_entry=False)

    assert set(p.action_type for p in decision.winning_actions) == {ActionType.TAKE_PROFIT, ActionType.ENSURE_PROTECTION}
    assert [p.action_type for p in decision.blocked_actions] == [ActionType.REDUCE]


def test_action_arbiter_allows_multi_symbol_entry_family_actions():
    arbiter = ActionArbiter()
    plans = (
        ActionPlan(action_type=ActionType.ENTRY, symbol="SOXL", side="LONG", qty=10, priority=20),
        ActionPlan(action_type=ActionType.ADD, symbol="SOXS", side="SHORT", qty=5, priority=30),
    )

    decision = arbiter.arbitrate(action_plans=plans, freeze_entry=False)

    assert set(p.action_type for p in decision.winning_actions) == {ActionType.ENTRY, ActionType.ADD}
    assert decision.blocked_actions == ()


def test_action_arbiter_allows_multi_lot_exit_actions_on_same_symbol():
    arbiter = ActionArbiter()
    plans = (
        ActionPlan(
            action_type=ActionType.TAKE_PROFIT,
            symbol="TQQQ",
            qty=2,
            position_lot_id="lot-a",
            priority=10,
        ),
        ActionPlan(
            action_type=ActionType.REDUCE,
            symbol="TQQQ",
            qty=1,
            position_lot_id="lot-b",
            priority=20,
        ),
    )

    decision = arbiter.arbitrate(action_plans=plans, freeze_entry=False)

    assert set((p.action_type, p.position_lot_id) for p in decision.winning_actions) == {
        (ActionType.TAKE_PROFIT, "lot-a"),
        (ActionType.REDUCE, "lot-b"),
    }
    assert decision.blocked_actions == ()


def test_action_arbiter_enforces_max_entry_actions_per_tick_budget():
    arbiter = ActionArbiter()
    plans = (
        ActionPlan(action_type=ActionType.ENTRY, symbol="SOXL", side="LONG", qty=1, priority=10),
        ActionPlan(action_type=ActionType.ADD, symbol="SOXL", side="LONG", qty=1, priority=20),
        ActionPlan(action_type=ActionType.ADD, symbol="SOXS", side="SHORT", qty=1, priority=30),
    )

    decision = arbiter.arbitrate(
        action_plans=plans,
        freeze_entry=False,
        max_entry_actions_per_tick=2,
    )

    assert len(decision.winning_actions) == 2
    assert len(decision.blocked_actions) == 1
    assert decision.reason == "entry_budget_suppressed"


def test_action_arbiter_enforces_max_entry_total_qty_per_tick_budget():
    arbiter = ActionArbiter()
    plans = (
        ActionPlan(action_type=ActionType.ENTRY, symbol="SOXL", side="LONG", qty=2, priority=10),
        ActionPlan(action_type=ActionType.ADD, symbol="SOXL", side="LONG", qty=2, priority=20),
        ActionPlan(action_type=ActionType.ADD, symbol="SOXL", side="LONG", qty=1, priority=30),
    )

    decision = arbiter.arbitrate(
        action_plans=plans,
        freeze_entry=False,
        max_entry_total_qty_per_tick=4.0,
    )

    assert [p.qty for p in decision.winning_actions] == [2, 2]
    assert [p.qty for p in decision.blocked_actions] == [1]
    assert decision.reason == "entry_budget_suppressed"
