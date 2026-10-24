from pathlib import Path

import pytest

from strategies.stocks_kernel.policies.default_pack import default_policy_registry


def test_policy_registry_build_supports_factory_mode():
    registry = default_policy_registry()
    policy = registry.build(
        policy_id="qqq_intraday_policy_v1",
        policy_params={"entry_model": "baseline", "rule_engine_parity": True},
        extras={"custom_flag": True},
        mode="factory",
    )

    assert getattr(policy, "policy_id", "") == "qqq_intraday_policy_v1"
    assert bool(getattr(policy, "static_extras", {}).get("custom_flag")) is True


def test_policy_registry_build_supports_legacy_mode_fallback():
    registry = default_policy_registry()
    policy = registry.build(
        policy_id="qqq_intraday_policy_v1",
        policy_params={"entry_model": "baseline"},
        extras={"legacy_mode": True},
        mode="legacy",
    )

    assert getattr(policy, "policy_id", "") == "qqq_intraday_policy_v1"
    assert bool(getattr(policy, "static_extras", {}).get("legacy_mode")) is True


def test_policy_registry_build_supports_spy_intraday_alias():
    registry = default_policy_registry()
    policy = registry.build(
        policy_id="spy_intraday_policy_v1",
        policy_params={"entry_model": "baseline", "rule_engine_parity": True},
        extras={"template_id": "spy_intraday_core"},
        mode="factory",
    )

    assert getattr(policy, "policy_id", "") == "spy_intraday_policy_v1"
    assert getattr(policy, "runtime_strategy_id", "") == "spy_intraday_policy_v1"
    assert bool(getattr(policy, "static_extras", {}).get("template_id")) is True


def test_policy_registry_register_from_specs_supports_config_driven_factory_loading():
    registry = default_policy_registry()
    registry.register_from_specs(
        [
            {
                "policy_id": "qqq_intraday_policy_v1_alias",
                "mode": "factory",
                "module": "strategies.stocks_kernel.policies.default_pack",
                "attr": "build_qqq_intraday_policy",
                "default_params": {"entry_model": "baseline"},
            }
        ]
    )

    policy = registry.build(
        policy_id="qqq_intraday_policy_v1_alias",
        policy_params={"rule_engine_parity": True},
        extras={"plugin_loaded": True},
        mode="factory",
    )
    assert getattr(policy, "policy_id", "") == "qqq_intraday_policy_v1"
    assert bool(getattr(policy, "static_extras", {}).get("plugin_loaded")) is True


def test_policy_registry_core_module_has_no_compile_time_qqq_import():
    registry_path = Path("./strategies/stocks_kernel/policies/registry.py")
    source = registry_path.read_text(encoding="utf-8")

    assert "qqq_intraday_policy_v1" not in source
    assert "QQQIntradayPolicyV1" not in source


def test_policy_registry_register_from_specs_is_fail_fast_for_malformed_specs():
    registry = default_policy_registry()

    with pytest.raises(ValueError, match="requires module/attr"):
        registry.register_from_specs(
            [
                {
                    "policy_id": "bad_policy",
                    "mode": "factory",
                }
            ]
        )
