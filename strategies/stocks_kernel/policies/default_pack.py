from __future__ import annotations

from .base import Policy
from .qqq_intraday_policy_v1 import QQQIntradayPolicyV1, SPYIntradayPolicyV1
from .registry import PolicyFactorySpec, PolicyLegacySpec, PolicyRegistry


def build_qqq_intraday_policy(params: dict, extras: dict) -> Policy:
    return _build_intraday_parity_policy(policy_cls=QQQIntradayPolicyV1, params=params, extras=extras)


def build_spy_intraday_policy(params: dict, extras: dict) -> Policy:
    return _build_intraday_parity_policy(policy_cls=SPYIntradayPolicyV1, params=params, extras=extras)


def _build_intraday_parity_policy(*, policy_cls: type[Policy], params: dict, extras: dict) -> Policy:
    merged_params = dict(params or {})
    allowed = {
        "max_trades_per_day",
        "hard_stop_atr_mult",
        "logic_retreat_min_hold_sec",
        "third_trade_requires_positive_day_pnl",
        "block_when_pending_entry",
        "block_when_same_strategy_pending_orders",
        "block_when_order_lifecycle_pending",
        "rule_engine_parity",
        "entry_model",
        "qqew_decoupled",
        "entry_recipe",
        "v3m_units",
        "runtime_strategy_id",
    }
    static_extras = dict(extras or {})
    static_extras.update(merged_params)
    return policy_cls(
        **{k: v for k, v in merged_params.items() if k in allowed},
        static_extras=static_extras,
    )


def build_qqq_intraday_policy_legacy(params: dict, extras: dict) -> Policy:
    # Legacy constructor semantics kept for rollback compatibility.
    return build_qqq_intraday_policy(params=params, extras=extras)


def build_spy_intraday_policy_legacy(params: dict, extras: dict) -> Policy:
    return build_spy_intraday_policy(params=params, extras=extras)


def register_builtin_policy_pack(registry: PolicyRegistry) -> PolicyRegistry:
    registry.register_legacy_builder(
        PolicyLegacySpec(
            policy_id="qqq_intraday_policy_v1",
            builder=build_qqq_intraday_policy_legacy,
        )
    )
    registry.register_factory(
        PolicyFactorySpec(
            policy_id="qqq_intraday_policy_v1",
            factory=build_qqq_intraday_policy,
        )
    )
    registry.register_legacy_builder(
        PolicyLegacySpec(
            policy_id="spy_intraday_policy_v1",
            builder=build_spy_intraday_policy_legacy,
        )
    )
    registry.register_factory(
        PolicyFactorySpec(
            policy_id="spy_intraday_policy_v1",
            factory=build_spy_intraday_policy,
        )
    )
    return registry


def default_policy_registry() -> PolicyRegistry:
    return register_builtin_policy_pack(PolicyRegistry())
