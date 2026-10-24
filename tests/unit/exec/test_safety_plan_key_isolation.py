from services.exec.safety_kernel import Plan, PlanOwnerKey, SafetyKernel


class _Broker:
    account = "DU123"


def test_plan_key_includes_account_strategy_instance_symbol_lot():
    kernel = SafetyKernel(broker_adapter=_Broker())

    key = kernel._plan_key(
        "mean_revert",
        "spy",
        account_id="DU123",
        strategy_instance_id="mean_revert_a",
        lot_id="lot-a",
    )

    assert key == "DU123:mean_revert_a:SPY:lot-a"
    assert key != "mean_revert:SPY"


def test_two_strategy_instances_same_symbol_get_distinct_plan_keys():
    kernel = SafetyKernel(broker_adapter=_Broker())

    key_a = kernel._plan_key(
        "mean_revert",
        "SPY",
        account_id="DU123",
        strategy_instance_id="mean_revert_a",
        lot_id="lot-a",
    )
    key_b = kernel._plan_key(
        "mean_revert",
        "SPY",
        account_id="DU123",
        strategy_instance_id="mean_revert_b",
        lot_id="lot-b",
    )

    assert key_a != key_b


def test_same_strategy_instance_same_symbol_two_lots_get_distinct_plan_keys():
    kernel = SafetyKernel(broker_adapter=_Broker())

    first_lot = kernel._plan_key(
        "mean_revert",
        "SPY",
        account_id="DU123",
        strategy_instance_id="mean_revert_a",
        lot_id="entry-1",
    )
    second_lot = kernel._plan_key(
        "mean_revert",
        "SPY",
        account_id="DU123",
        strategy_instance_id="mean_revert_a",
        lot_id="entry-2",
    )

    assert first_lot != second_lot


def test_default_lot_id_uses_strategy_instance_not_strategy_only():
    lot_id = SafetyKernel.default_lot_id(
        account_id="DU123",
        strategy_instance_id="mean_revert_a",
        symbol="spy",
    )

    assert lot_id == "lot:DU123:mean_revert_a:SPY"
    assert lot_id != "lot:mean_revert:SPY"


def test_legacy_strategy_id_symbol_key_not_used_for_new_plans():
    kernel = SafetyKernel(broker_adapter=_Broker())

    key_a = kernel._plan_key("mean_revert", "SPY")
    key_b = kernel._plan_key("mean_revert", "SPY")

    assert key_a != "mean_revert:SPY"
    assert key_a != key_b


def test_plan_owner_key_for_plan_is_owner_scoped_and_oco_compatible():
    kernel = SafetyKernel(broker_adapter=_Broker())
    plan = Plan(
        strategy_id="mean_revert",
        strategy_instance_id="mean_revert_a",
        account_id="DU123",
        symbol="spy",
        lot_id="lot-a",
        entry_order_id=101,
        entry_qty=10,
        oco_config={"sl_px": 99.0, "tp_px": 103.0},
    )

    key = kernel._plan_key_for_plan(plan)

    assert key == PlanOwnerKey("DU123", "mean_revert_a", "SPY", "lot-a").as_key()
    assert plan.lot_id == "lot-a"
    assert plan.strategy_id == "mean_revert"
