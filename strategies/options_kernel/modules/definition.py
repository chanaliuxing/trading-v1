from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple

from .contract_policy import BaseContractPolicy
from .direction_mapping import BaseDirectionMappingPolicy
from .execution_policy import BaseExecutionPolicy
from .evidence_contract import OptionEvidenceContract
from .flatten_policy import BaseFlattenPolicy
from .position_policy import BasePositionPolicy
from .profit_management_policy import BaseProfitManagementPolicy
from .risk_policy import BaseRiskPolicy
from .signal import BaseSignalModule


PROTECTED_DEFINITION_EXTRA_KEYS: Tuple[str, ...] = (
    "definition_id",
    "underlying",
    "risk_symbol",
    "audit_namespace",
    "replay_strategy_id",
)

ALWAYS_ALLOWED_PLUGIN_OVERRIDE_KEYS: Tuple[str, ...] = (
    "position_namespace",
)


@dataclass(frozen=True)
class StrategyDefinition:
    definition_id: str
    strategy_family: str
    strategy_archetype: str
    underlying: str
    risk_symbol: str
    signal_module: BaseSignalModule
    direction_mapping: BaseDirectionMappingPolicy
    contract_policy: BaseContractPolicy
    execution_policy: BaseExecutionPolicy
    position_policy: BasePositionPolicy
    risk_policy: BaseRiskPolicy
    profit_management_policy: BaseProfitManagementPolicy
    flatten_policy: BaseFlattenPolicy
    evidence_contract: OptionEvidenceContract
    audit_namespace: str
    ownership_scope: str = "strategy_instance"
    allowed_plugin_override_keys: Tuple[str, ...] = field(default_factory=tuple)
    extras: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        definition_id = str(self.definition_id or "").strip()
        strategy_family = str(self.strategy_family or "").strip()
        strategy_archetype = str(self.strategy_archetype or "").strip()
        underlying = str(self.underlying or "").strip().upper()
        risk_symbol = str(self.risk_symbol or "").strip()
        audit_namespace = str(self.audit_namespace or "").strip()
        ownership_scope = str(self.ownership_scope or "").strip()
        if not definition_id:
            raise ValueError("definition_id is required")
        if not strategy_family:
            raise ValueError(f"definition_id={definition_id} missing strategy_family")
        if not strategy_archetype:
            raise ValueError(f"definition_id={definition_id} missing strategy_archetype")
        if not underlying:
            raise ValueError(f"definition_id={definition_id} missing underlying")
        if not risk_symbol:
            raise ValueError(f"definition_id={definition_id} missing risk_symbol")
        if not audit_namespace:
            raise ValueError(f"definition_id={definition_id} missing audit_namespace")
        if not ownership_scope:
            raise ValueError(f"definition_id={definition_id} missing ownership_scope")
        for attr_name, value in (
            ("signal_module.module_id", getattr(self.signal_module, "module_id", "")),
            ("direction_mapping.policy_id", getattr(self.direction_mapping, "policy_id", "")),
            ("contract_policy.policy_id", getattr(self.contract_policy, "policy_id", "")),
            ("execution_policy.policy_id", getattr(self.execution_policy, "policy_id", "")),
            ("position_policy.policy_id", getattr(self.position_policy, "policy_id", "")),
            ("risk_policy.policy_id", getattr(self.risk_policy, "policy_id", "")),
            ("profit_management_policy.policy_id", getattr(self.profit_management_policy, "policy_id", "")),
            ("flatten_policy.policy_id", getattr(self.flatten_policy, "policy_id", "")),
            ("evidence_contract.contract_id", getattr(self.evidence_contract, "contract_id", "")),
        ):
            if not str(value or "").strip():
                raise ValueError(f"definition_id={definition_id} missing {attr_name}")

    def protected_extras(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "definition_id": str(self.definition_id),
            "underlying": str(self.underlying),
            "risk_symbol": str(self.risk_symbol),
            "audit_namespace": str(self.audit_namespace),
        }
        replay_strategy_id = str((self.extras or {}).get("replay_strategy_id") or "").strip()
        if replay_strategy_id:
            payload["replay_strategy_id"] = replay_strategy_id
        return payload

    def module_identity(self) -> Dict[str, str]:
        return {
            "strategy_family": str(self.strategy_family or ""),
            "strategy_archetype": str(self.strategy_archetype or ""),
            "signal_module_id": str(getattr(self.signal_module, "module_id", "") or ""),
            "direction_mapping_policy": str(getattr(self.direction_mapping, "policy_id", "") or ""),
            "contract_policy_id": str(getattr(self.contract_policy, "policy_id", "") or ""),
            "execution_policy_id": str(getattr(self.execution_policy, "policy_id", "") or ""),
            "position_policy_id": str(getattr(self.position_policy, "policy_id", "") or ""),
            "risk_policy_id": str(getattr(self.risk_policy, "policy_id", "") or ""),
            "profit_management_policy_id": str(getattr(self.profit_management_policy, "policy_id", "") or ""),
            "flatten_policy_id": str(getattr(self.flatten_policy, "policy_id", "") or ""),
            "ownership_scope": str(self.ownership_scope or ""),
            "evidence_contract_id": str(getattr(self.evidence_contract, "contract_id", "") or ""),
        }

    def enforce_extra_contract(self, original_extras: Mapping[str, Any], merged_extras: Mapping[str, Any]) -> Dict[str, Any]:
        original = dict(original_extras or {})
        merged = dict(merged_extras or {})
        protected = self.protected_extras()
        enriched = dict(merged)
        enriched.update(protected)
        for key, expected in protected.items():
            if key in original and original.get(key) not in (None, "", expected):
                raise ValueError(
                    f"definition_id={self.definition_id} forbids overriding protected extra '{key}'"
                )
            if enriched.get(key) != expected:
                raise ValueError(
                    f"definition_id={self.definition_id} requires merged extra '{key}'={expected!r}"
                )

        forced_override_keys = {
            key
            for key in original.keys()
            if key in enriched and original.get(key) != enriched.get(key)
        }
        non_protected_forced = forced_override_keys.difference(protected.keys())
        allowed_override_keys = set(self.allowed_plugin_override_keys or ()) | set(ALWAYS_ALLOWED_PLUGIN_OVERRIDE_KEYS)
        disallowed = sorted(non_protected_forced.difference(allowed_override_keys))
        if disallowed:
            raise ValueError(
                f"definition_id={self.definition_id} plugin forced overrides not allowed for keys={disallowed}"
            )

        enriched.update(self.module_identity())
        return enriched
