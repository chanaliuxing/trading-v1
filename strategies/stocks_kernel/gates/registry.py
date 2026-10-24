from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from typing import Any, Callable

from .base import Gate


@dataclass(frozen=True)
class GateSpec:
    gate_id: str
    factory: Callable[[dict[str, Any]], Gate]
    default_config: dict[str, Any] = field(default_factory=dict)


class DisabledGate:
    def __init__(self, *, gate_id: str) -> None:
        self.gate_id = gate_id

    def evaluate(self, *, symbol, feature_context, readiness, runtime=None):
        from strategies.stocks_kernel.types import GateDecision

        return GateDecision(
            gate_id=self.gate_id,
            allowed=True,
            reason="gate_disabled",
            trader_reason_code="gate_disabled",
            payload={},
        )


class GateRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, GateSpec] = {}

    def register(self, spec: GateSpec) -> None:
        gate_id = str(spec.gate_id or "").strip()
        if not gate_id:
            raise ValueError("gate_id is required")
        self._specs[gate_id] = spec

    def register_from_specs(self, specs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> None:
        for idx, raw in enumerate(list(specs or ()), start=1):
            if not isinstance(raw, dict):
                raise TypeError(f"gate_registry_specs[{idx}] must be a dict")
            gate_id = str(raw.get("gate_id") or "").strip()
            module_name = str(raw.get("module") or "").strip()
            attr_name = str(raw.get("attr") or "").strip()
            if not gate_id or not module_name or not attr_name:
                raise ValueError(f"gate_registry_specs[{idx}] requires gate_id/module/attr")
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                raise ImportError(f"gate_registry_specs[{idx}] failed to import module '{module_name}': {exc}") from exc
            try:
                target = getattr(module, attr_name)
            except AttributeError as exc:
                raise AttributeError(f"gate_registry_specs[{idx}] missing attr '{attr_name}' in '{module_name}'") from exc
            default_config = dict(raw.get("default_config") or {})
            if isinstance(target, GateSpec):
                spec = GateSpec(
                    gate_id=gate_id,
                    factory=target.factory,
                    default_config={**dict(target.default_config), **default_config},
                )
            elif isinstance(target, type):
                if not issubclass(target, Gate):
                    raise TypeError(f"gate_registry_specs[{idx}] class '{module_name}.{attr_name}' must subclass Gate")
                spec = GateSpec(
                    gate_id=gate_id,
                    factory=lambda cfg, gate_cls=target: gate_cls(**dict(cfg)),
                    default_config=default_config,
                )
            else:
                if not callable(target):
                    raise TypeError(f"gate_registry_specs[{idx}] target '{module_name}.{attr_name}' must be callable")
                spec = GateSpec(
                    gate_id=gate_id,
                    factory=target,
                    default_config=default_config,
                )
            self.register(spec)

    def build(self, gate_id: str, *, enabled: bool = True, config: dict[str, Any] | None = None) -> Gate:
        key = str(gate_id or "").strip()
        if not enabled:
            return DisabledGate(gate_id=key)
        if key not in self._specs:
            raise KeyError(f"unknown gate: {key}")
        spec = self._specs[key]
        merged = dict(spec.default_config)
        merged.update(dict(config or {}))
        return spec.factory(merged)

    def known_gate_ids(self) -> list[str]:
        return sorted(self._specs.keys())


def default_gate_registry() -> GateRegistry:
    from .default_pack import default_gate_registry as _default_gate_registry

    return _default_gate_registry()
