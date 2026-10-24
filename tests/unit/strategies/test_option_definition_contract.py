import pytest

from strategies.options_kernel.modules.definition import StrategyDefinition
from strategies.options_kernel.modules.evidence_contract import OptionEvidenceContract


class _DummySignalModule:
    module_id = "sig1"


class _DummyContractPolicy:
    policy_id = "contract1"


class _DummyExecutionPolicy:
    policy_id = "exec1"


class _DummyPositionPolicy:
    policy_id = "pos1"


class _DummyDirectionMappingPolicy:
    policy_id = "map1"


class _DummyRiskPolicy:
    policy_id = "risk1"


class _DummyProfitManagementPolicy:
    policy_id = "profit1"


class _DummyFlattenPolicy:
    policy_id = "flatten1"


_DUMMY_EVIDENCE = OptionEvidenceContract(
    contract_id="evidence1",
    replay_harness="options_kernel_harness",
    cutover_policy="replay_and_shadow_before_live",
)


def _build_definition(**overrides):
    payload = {
        "definition_id": "DEF1",
        "strategy_family": "qqq_option",
        "strategy_archetype": "test_archetype",
        "underlying": "QQQ",
        "risk_symbol": "OPT:QQQ:1DTE",
        "signal_module": _DummySignalModule(),
        "direction_mapping": _DummyDirectionMappingPolicy(),
        "contract_policy": _DummyContractPolicy(),
        "execution_policy": _DummyExecutionPolicy(),
        "position_policy": _DummyPositionPolicy(),
        "risk_policy": _DummyRiskPolicy(),
        "profit_management_policy": _DummyProfitManagementPolicy(),
        "flatten_policy": _DummyFlattenPolicy(),
        "evidence_contract": _DUMMY_EVIDENCE,
        "audit_namespace": "qqq_option",
        "extras": {"replay_strategy_id": "replay_1"},
    }
    payload.update(overrides)
    return StrategyDefinition(**payload)


def test_strategy_definition_requires_identity_fields() -> None:
    with pytest.raises(ValueError):
        _build_definition(strategy_family="")


def test_strategy_definition_exposes_module_identity() -> None:
    definition = _build_definition()

    assert definition.module_identity()["signal_module_id"] == "sig1"
    assert definition.module_identity()["strategy_archetype"] == "test_archetype"
    assert definition.module_identity()["direction_mapping_policy"] == "map1"
    assert definition.module_identity()["contract_policy_id"] == "contract1"
    assert definition.module_identity()["execution_policy_id"] == "exec1"
    assert definition.module_identity()["position_policy_id"] == "pos1"
    assert definition.module_identity()["risk_policy_id"] == "risk1"
    assert definition.module_identity()["profit_management_policy_id"] == "profit1"
    assert definition.module_identity()["flatten_policy_id"] == "flatten1"
    assert definition.module_identity()["evidence_contract_id"] == "evidence1"


def test_strategy_definition_blocks_protected_config_override() -> None:
    definition = _build_definition()

    with pytest.raises(ValueError, match="forbids overriding protected extra 'underlying'"):
        definition.enforce_extra_contract(
            {"underlying": "SPY"},
            {
                "definition_id": "DEF1",
                "underlying": "QQQ",
                "risk_symbol": "OPT:QQQ:1DTE",
                "audit_namespace": "qqq_option",
                "replay_strategy_id": "replay_1",
            },
        )


def test_strategy_definition_allows_only_whitelisted_forced_plugin_overrides() -> None:
    definition = _build_definition(allowed_plugin_override_keys=("min_open_interest",))

    enriched = definition.enforce_extra_contract(
        {"min_open_interest": 50},
        {
            "definition_id": "DEF1",
            "underlying": "QQQ",
            "risk_symbol": "OPT:QQQ:1DTE",
            "audit_namespace": "qqq_option",
            "replay_strategy_id": "replay_1",
            "min_open_interest": 0,
        },
    )
    assert enriched["min_open_interest"] == 0

    with pytest.raises(ValueError, match="forced overrides not allowed"):
        definition.enforce_extra_contract(
            {"contracts": 5},
            {
                "definition_id": "DEF1",
                "underlying": "QQQ",
                "risk_symbol": "OPT:QQQ:1DTE",
                "audit_namespace": "qqq_option",
                "replay_strategy_id": "replay_1",
                "contracts": 2,
            },
        )
