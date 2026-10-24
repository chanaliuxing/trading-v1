from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from typing import Any, Callable

from .base import Policy


@dataclass(frozen=True)
class PolicyFactorySpec:
    policy_id: str
    factory: Callable[[dict[str, Any], dict[str, Any]], Policy]
    default_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyLegacySpec:
    policy_id: str
    builder: Callable[[dict[str, Any], dict[str, Any]], Policy]


class PolicyRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, PolicyFactorySpec] = {}
        self._legacy_builders: dict[str, PolicyLegacySpec] = {}
        self._legacy_instances: dict[str, Policy] = {}

    def register(self, policy: Policy) -> None:
        policy_id = str(getattr(policy, "policy_id", "")).strip()
        if not policy_id:
            raise ValueError("policy_id is required")
        self._legacy_instances[policy_id] = policy

    def register_factory(self, spec: PolicyFactorySpec) -> None:
        policy_id = str(spec.policy_id or "").strip()
        if not policy_id:
            raise ValueError("policy_id is required")
        self._factories[policy_id] = spec

    def register_legacy_builder(self, spec: PolicyLegacySpec) -> None:
        policy_id = str(spec.policy_id or "").strip()
        if not policy_id:
            raise ValueError("policy_id is required")
        self._legacy_builders[policy_id] = spec

    def register_from_specs(self, specs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> None:
        for idx, raw in enumerate(list(specs or ()), start=1):
            if not isinstance(raw, dict):
                raise TypeError(f"policy_registry_specs[{idx}] must be a dict")
            policy_id = str(raw.get("policy_id") or "").strip()
            if not policy_id:
                raise ValueError(f"policy_registry_specs[{idx}] requires policy_id")
            mode = str(raw.get("mode") or "factory").strip().lower()
            if mode not in {"factory", "legacy"}:
                raise ValueError(f"policy_registry_specs[{idx}] has invalid mode '{mode}'")
            module_name = str(raw.get("module") or "").strip()
            attr_name = str(raw.get("attr") or "").strip()
            if not module_name or not attr_name:
                raise ValueError(f"policy_registry_specs[{idx}] requires module/attr")
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                raise ImportError(f"policy_registry_specs[{idx}] failed to import module '{module_name}': {exc}") from exc
            try:
                target = getattr(module, attr_name)
            except AttributeError as exc:
                raise AttributeError(f"policy_registry_specs[{idx}] missing attr '{attr_name}' in '{module_name}'") from exc
            if not callable(target):
                raise TypeError(f"policy_registry_specs[{idx}] target '{module_name}.{attr_name}' must be callable")
            if mode == "legacy":
                self.register_legacy_builder(
                    PolicyLegacySpec(
                        policy_id=policy_id,
                        builder=target,
                    )
                )
                continue
            default_params = raw.get("default_params") or {}
            if not isinstance(default_params, dict):
                raise TypeError(f"policy_registry_specs[{idx}] default_params must be a dict")
            self.register_factory(
                PolicyFactorySpec(
                    policy_id=policy_id,
                    factory=target,
                    default_params=dict(default_params),
                )
            )

    def build(
        self,
        *,
        policy_id: str,
        policy_params: dict[str, Any] | None = None,
        extras: dict[str, Any] | None = None,
        mode: str = "factory",
    ) -> Policy:
        key = str(policy_id or "").strip()
        mode_v = str(mode or "factory").strip().lower()
        if mode_v == "legacy" and key in self._legacy_builders:
            return self._legacy_builders[key].builder(
                dict(policy_params or {}),
                dict(extras or {}),
            )
        if key in self._factories:
            spec = self._factories[key]
            merged_params = {**dict(spec.default_params), **dict(policy_params or {})}
            return spec.factory(merged_params, dict(extras or {}))
        if key in self._legacy_instances:
            return self._legacy_instances[key]
        raise KeyError(f"unknown policy: {key}")

    def get(self, policy_id: str) -> Policy:
        key = str(policy_id or "").strip()
        if key in self._legacy_instances:
            return self._legacy_instances[key]
        if key in self._factories:
            return self.build(policy_id=key)
        raise KeyError(f"unknown policy: {key}")


def default_policy_registry() -> PolicyRegistry:
    # Compatibility alias: default policy assembly lives outside pure registry core.
    from .default_pack import default_policy_registry as _default_policy_registry

    return _default_policy_registry()
