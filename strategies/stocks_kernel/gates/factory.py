from __future__ import annotations

from typing import Any

from .registry import GateRegistry, default_gate_registry

_DEFAULT_GATE_REGISTRY = default_gate_registry()


def build_gate(
    gate_id: str,
    *,
    enabled: bool = True,
    config: dict[str, Any] | None = None,
    registry: GateRegistry | None = None,
):
    active_registry = registry or _DEFAULT_GATE_REGISTRY
    return active_registry.build(gate_id, enabled=enabled, config=config)
