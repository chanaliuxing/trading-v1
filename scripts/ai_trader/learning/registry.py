from __future__ import annotations

from typing import Dict, Type

from .base import GenericLearningAdapter


class LearningAdapterRegistry:
    """Simple profile -> adapter class registry."""

    def __init__(self) -> None:
        self._registry: Dict[str, Type[GenericLearningAdapter]] = {}
        self.register("generic", GenericLearningAdapter)
        # Backward-compatible aliases for current strategy families.
        self.register("enhanced_ml", GenericLearningAdapter)
        self.register("qqq_intraday", GenericLearningAdapter)
        self.register("options_spread", GenericLearningAdapter)

    def register(self, profile: str, adapter_cls: Type[GenericLearningAdapter]) -> None:
        key = str(profile or "").strip()
        if not key:
            return
        self._registry[key] = adapter_cls

    def create(self, profile: str) -> GenericLearningAdapter:
        key = str(profile or "").strip() or "generic"
        cls = self._registry.get(key) or self._registry["generic"]
        return cls()
