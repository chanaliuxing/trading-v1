from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from strategies.stocks_kernel.types import FeatureResult, HistoryBundle, MarketSnapshot

FeatureCalculator = Callable[[MarketSnapshot, HistoryBundle, dict[str, Any]], FeatureResult]


@dataclass(frozen=True)
class FeaturePlugin:
    feature_id: str
    calculator: FeatureCalculator
    dependencies: tuple[str, ...] = ()
    default_params: dict[str, Any] = field(default_factory=dict)
