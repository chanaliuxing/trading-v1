from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision


@dataclass(frozen=True)
class GateRuntimeContext:
    data_context_bundle: Any | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class Gate(ABC):
    gate_id: str

    @abstractmethod
    def evaluate(
        self,
        *,
        symbol: str,
        feature_context: FeatureContext,
        readiness: DataReadinessReport,
        runtime: GateRuntimeContext | None = None,
    ) -> GateDecision:
        raise NotImplementedError
