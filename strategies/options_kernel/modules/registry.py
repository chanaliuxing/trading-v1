from __future__ import annotations

from copy import deepcopy
from typing import Dict

from .definition import StrategyDefinition


class StrategyDefinitionRegistry:
    def __init__(self) -> None:
        self._definitions: Dict[str, StrategyDefinition] = {}
        self._defaults_loaded = False

    def _ensure_defaults_loaded(self) -> None:
        if self._defaults_loaded:
            return
        from . import strategy_definitions as _strategy_definitions  # noqa: F401

        self._defaults_loaded = True

    def register(self, definition: StrategyDefinition) -> None:
        definition_id = str(definition.definition_id)
        if definition_id in self._definitions:
            raise ValueError(f"duplicate option strategy definition_id={definition_id}")
        self._definitions[definition_id] = definition

    def get(self, definition_id: str) -> StrategyDefinition:
        self._ensure_defaults_loaded()
        return deepcopy(self._definitions[str(definition_id)])

    def has(self, definition_id: str) -> bool:
        self._ensure_defaults_loaded()
        return str(definition_id) in self._definitions

    def items(self) -> Dict[str, StrategyDefinition]:
        self._ensure_defaults_loaded()
        return {key: deepcopy(value) for key, value in self._definitions.items()}


definition_registry = StrategyDefinitionRegistry()
