from __future__ import annotations

from typing import Dict, Callable, Any


class CodeToolBase:
    """
    Base class for code tools used by the self-programming agents.
    """

    name: str = "base"

    def run(self, **kwargs: Any) -> Any:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self) -> None:
        self._registry: Dict[str, CodeToolBase] = {}

    def register(self, tool: CodeToolBase) -> None:
        self._registry[tool.name] = tool

    def get(self, name: str) -> CodeToolBase:
        return self._registry[name]

    def run(self, name: str, **kwargs: Any) -> Any:
        tool = self.get(name)
        return tool.run(**kwargs)


__all__ = ["CodeToolBase", "ToolRegistry"]
