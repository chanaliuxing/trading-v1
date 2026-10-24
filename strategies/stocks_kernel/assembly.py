from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .gates.default_pack import default_gate_registry
from .policies.default_pack import default_policy_registry
from .runtime.default_stage_pack import default_runtime_stage_registry


@dataclass(frozen=True)
class KernelAssembly:
    gate_registry: Any
    policy_registry: Any
    runtime_stage_registry: Any


KernelAssemblyProvider = Callable[..., KernelAssembly]


def build_default_kernel_assembly(*, extras: dict[str, Any] | None = None) -> KernelAssembly:
    _ = dict(extras or {})
    return KernelAssembly(
        gate_registry=default_gate_registry(),
        policy_registry=default_policy_registry(),
        runtime_stage_registry=default_runtime_stage_registry(),
    )


def legacy_kernel_assembly_provider(*, extras: dict[str, Any] | None = None) -> KernelAssembly:
    return build_default_kernel_assembly(extras=extras)
