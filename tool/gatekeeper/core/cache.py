from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

from core.types import ResolvedInput


Scope = Literal["GLOBAL", "ANCHOR", "TICKER"]


def make_field_id(scope: Scope, key: str, symbol: str | None = None) -> str:
    if scope == "GLOBAL":
        return f"GLOBAL|{key}"
    if not symbol:
        raise ValueError(f"symbol required for scope={scope}")
    return f"{scope}|{symbol}|{key}"


def parse_field_id(field_id: str) -> tuple[Scope, str | None, str]:
    parts = field_id.split("|")
    if len(parts) == 2 and parts[0] == "GLOBAL":
        return "GLOBAL", None, parts[1]
    if len(parts) == 3 and parts[0] in ("ANCHOR", "TICKER"):
        scope: Scope = parts[0]  # type: ignore[assignment]
        return scope, parts[1], parts[2]
    raise ValueError(f"Invalid field_id: {field_id}")


@dataclass(slots=True)
class GatekeeperCache:
    store: dict[str, ResolvedInput] = field(default_factory=dict)

    def get(self, field_id: str) -> ResolvedInput | None:
        return self.store.get(field_id)

    def set(self, field_id: str, value: ResolvedInput) -> None:
        self.store[field_id] = value

    def delete(self, field_id: str) -> None:
        self.store.pop(field_id, None)

    def keys(self) -> Iterable[str]:
        return self.store.keys()

