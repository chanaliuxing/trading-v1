from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence

from libs.strategy import DecisionEnvelope, Freshness


@dataclass(frozen=True)
class RecoveryPolicyContext:
    strategy_id: str
    symbol: str
    broker_qty: float = 0.0
    ledger_qty: float = 0.0
    reason: str = "runtime_recovery"
    metadata: Dict[str, Any] = field(default_factory=dict)
    account_id: str = ""
    strategy_instance_id: str = ""
    lot_id: str = ""
    position_owner_key: str = ""
    owner_qty: float = 0.0

    @property
    def diff(self) -> float:
        return float(self.broker_qty) - float(self.ledger_qty)

    @property
    def owner_identity_complete(self) -> bool:
        return bool(
            str(self.account_id or "").strip()
            and str(self.strategy_id or "").strip()
            and str(self.strategy_instance_id or "").strip()
            and str(self.symbol or "").strip()
            and str(self.lot_id or "").strip()
            and str(self.position_owner_key or "").strip()
        )


@dataclass(frozen=True)
class RecoveryAction:
    action_type: str
    strategy_id: str
    symbol: str
    target_qty: float
    reason: str
    extras: Dict[str, Any] = field(default_factory=dict)

    def stable_decision_id(self) -> str:
        extras = dict(self.extras or {})
        broker_qty = float(extras.get("broker_position_qty_hint") or extras.get("position_qty_hint") or 0.0)
        sign = "long" if broker_qty > 0 else "short" if broker_qty < 0 else "flat"
        return (
            "runtime_recovery:"
            f"{self.strategy_id}:{str(self.symbol or '').strip().upper()}:"
            f"{sign}:{self.action_type}"
        )

    def to_decision_envelope(self, *, decision_id: Optional[str] = None) -> DecisionEnvelope:
        now_us = int(time.time() * 1_000_000)
        stable_id = decision_id or self.stable_decision_id()
        extras = {
            "runtime_recovery_action": True,
            "close_only": True,
            "broker_position_qty_hint": float(self.extras.get("broker_position_qty_hint", 0.0) or 0.0),
            "broker_position_qty_hint_source": "runtime_reconciler",
            "intent_id": stable_id,
            "recovery_intent_id": stable_id,
            **dict(self.extras or {}),
        }
        return DecisionEnvelope(
            decision_id=stable_id,
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            ts_bar_end_us=now_us,
            target_qty=float(self.target_qty),
            freshness=Freshness(dlb_ms=30_000, ttl_ms=60_000),
            tags=["runtime_recovery", self.action_type],
            reason_codes=[self.reason],
            extras=extras,
        )


class StrategyRecoveryPolicy(Protocol):
    policy_id: str

    def build_actions(self, context: RecoveryPolicyContext) -> Sequence[RecoveryAction]:
        ...


class DefaultCloseOnlyRecoveryPolicy:
    policy_id = "default_close_only"

    def build_actions(self, context: RecoveryPolicyContext) -> Sequence[RecoveryAction]:
        broker_qty = float(context.broker_qty or 0.0)
        if abs(broker_qty) <= 1e-9:
            return []
        owner_qty = float(context.owner_qty or 0.0)
        if context.owner_identity_complete and abs(owner_qty) > 1e-9 and (broker_qty * owner_qty) <= 0:
            return []
        return [
            RecoveryAction(
                action_type="flatten",
                strategy_id=context.strategy_id,
                symbol=context.symbol,
                target_qty=-(owner_qty if context.owner_identity_complete and abs(owner_qty) > 1e-9 else broker_qty),
                reason=context.reason or "broker_position_recovery_flatten",
                extras={
                    "flatten_action": True,
                    "close_only": True,
                    "broker_position_qty_hint": broker_qty,
                    "position_qty_hint": broker_qty,
                    "recovery_policy_id": self.policy_id,
                    "recovery_requires_uom_safety_lane": True,
                    "account_id": context.account_id or None,
                    "strategy_instance_id": context.strategy_instance_id or None,
                    "position_lot_id": context.lot_id or None,
                    "lot_id": context.lot_id or None,
                    "position_owner_key": context.position_owner_key or None,
                    "owner_lane_qty": context.owner_qty or None,
                },
            )
        ]


class QQQIntradayRecoveryPolicy(DefaultCloseOnlyRecoveryPolicy):
    policy_id = "qqq_intraday_close_only"

    def build_actions(self, context: RecoveryPolicyContext) -> Sequence[RecoveryAction]:
        actions = list(super().build_actions(context))
        for action in actions:
            action.extras.setdefault("strategy_family", "qqq_intraday")
        return actions


class StocksKernelRecoveryPolicy(DefaultCloseOnlyRecoveryPolicy):
    policy_id = "stocks_kernel_close_only"

    def build_actions(self, context: RecoveryPolicyContext) -> Sequence[RecoveryAction]:
        actions = list(super().build_actions(context))
        for action in actions:
            action.extras.setdefault("strategy_family", "stocks_kernel")
        return actions


class OptionCloseOnlyRecoveryPolicy(DefaultCloseOnlyRecoveryPolicy):
    policy_id = "option_close_only"

    def build_actions(self, context: RecoveryPolicyContext) -> Sequence[RecoveryAction]:
        actions = list(super().build_actions(context))
        for action in actions:
            action.extras.setdefault("strategy_family", "options")
            action.extras.setdefault("option_recovery", True)
        return actions


class QQQKernelRecoveryPolicy(StocksKernelRecoveryPolicy):
    policy_id = "qqq_kernel_close_only"

    def build_actions(self, context: RecoveryPolicyContext) -> Sequence[RecoveryAction]:
        actions = list(super().build_actions(context))
        for action in actions:
            action.extras["strategy_family"] = "qqq_kernel"
        return actions


RECOVERY_POLICY_FACTORIES: Dict[str, type[StrategyRecoveryPolicy]] = {
    DefaultCloseOnlyRecoveryPolicy.policy_id: DefaultCloseOnlyRecoveryPolicy,
    QQQIntradayRecoveryPolicy.policy_id: QQQIntradayRecoveryPolicy,
    StocksKernelRecoveryPolicy.policy_id: StocksKernelRecoveryPolicy,
    OptionCloseOnlyRecoveryPolicy.policy_id: OptionCloseOnlyRecoveryPolicy,
    QQQKernelRecoveryPolicy.policy_id: QQQKernelRecoveryPolicy,
}


class StrategyRecoveryPolicyRegistry:
    def __init__(
        self,
        *,
        default_policy: Optional[StrategyRecoveryPolicy] = None,
        policies: Optional[Mapping[str, StrategyRecoveryPolicy]] = None,
    ) -> None:
        self.default_policy = default_policy or DefaultCloseOnlyRecoveryPolicy()
        self._policies: Dict[str, StrategyRecoveryPolicy] = {}
        for strategy_id, policy in dict(policies or {}).items():
            self.register(strategy_id, policy)

    def register(self, strategy_id: str, policy: StrategyRecoveryPolicy) -> None:
        sid = str(strategy_id or "").strip()
        if not sid:
            raise ValueError("strategy_id is required")
        self._policies[sid] = policy

    def get(self, strategy_id: str) -> StrategyRecoveryPolicy:
        sid = str(strategy_id or "").strip()
        return self._policies.get(sid) or self.default_policy

    def registered_strategy_ids(self) -> List[str]:
        return sorted(self._policies.keys())

    def build_actions(self, context: RecoveryPolicyContext) -> List[RecoveryAction]:
        return list(self.get(context.strategy_id).build_actions(context))


def default_strategy_recovery_policy_registry(
    strategy_ids: Optional[Iterable[str]] = None,
) -> StrategyRecoveryPolicyRegistry:
    registry = StrategyRecoveryPolicyRegistry()
    for strategy_id in strategy_ids or []:
        sid = str(strategy_id or "").strip()
        if not sid:
            continue
        if sid.startswith("qqq_intraday"):
            registry.register(sid, QQQIntradayRecoveryPolicy())
    return registry


def known_recovery_policy_ids() -> set[str]:
    return set(RECOVERY_POLICY_FACTORIES.keys())


def strategy_recovery_policy_registry_from_manifests(
    manifests: Iterable[Any],
) -> StrategyRecoveryPolicyRegistry:
    registry = StrategyRecoveryPolicyRegistry()
    for manifest in manifests or []:
        strategy_id = str(getattr(manifest, "strategy_id", "") or "").strip()
        policy_id = str(getattr(manifest, "recovery_policy_id", "") or "").strip()
        if not strategy_id or not policy_id:
            continue
        factory = RECOVERY_POLICY_FACTORIES.get(policy_id)
        if factory is None:
            continue
        registry.register(strategy_id, factory())
    return registry
