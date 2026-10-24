from strategies.options_kernel.modules.registry import definition_registry
from strategies.options_kernel.modules.strategy_definitions import (
    QQQ_MA10_REJECT_0324,
    SPY_MA_CROSS_STACK_CONT_V2_DUAL,
)


def test_definition_registry_contains_spy_and_qqq() -> None:
    assert definition_registry.has("QQQ_MA10_REJECT_0324")
    assert definition_registry.has("SPY_MA_CROSS_STACK_CONT_V2_DUAL")


def test_spy_and_qqq_definitions_are_isolated() -> None:
    qqq = definition_registry.get("QQQ_MA10_REJECT_0324")
    spy = definition_registry.get("SPY_MA_CROSS_STACK_CONT_V2_DUAL")

    assert qqq is not QQQ_MA10_REJECT_0324
    assert spy is not SPY_MA_CROSS_STACK_CONT_V2_DUAL
    assert qqq.underlying == "QQQ"
    assert spy.underlying == "SPY"
    assert qqq.risk_symbol != spy.risk_symbol
    assert qqq.audit_namespace != spy.audit_namespace
    assert qqq.signal_module.module_id != spy.signal_module.module_id
    assert qqq.signal_module is not definition_registry.get("QQQ_MA10_REJECT_0324").signal_module


def test_registry_returns_independent_policy_instances() -> None:
    qqq_a = definition_registry.get("QQQ_MA10_REJECT_0324")
    qqq_b = definition_registry.get("QQQ_MA10_REJECT_0324")

    assert qqq_a.contract_policy is not qqq_b.contract_policy
    assert qqq_a.execution_policy is not qqq_b.execution_policy
    assert qqq_a.position_policy is not qqq_b.position_policy
