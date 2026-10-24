from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from uuid import uuid4


ORDER_NAMESPACE = "iswv2"


class OrderKind(str, Enum):
    ENTRY = "ENTRY"
    STOP = "STOP"
    FLATTEN = "FLATTEN"
    TP1 = "TP1"
    EXIT = "EXIT"
    ADJUST = "ADJUST"
    GEN = "GEN"


@dataclass(frozen=True)
class ParsedOrderRef:
    namespace: str
    strategy_id: str
    symbol: str
    kind: str
    trigger_id: Optional[str] = None
    suffix: tuple[str, ...] = field(default_factory=tuple)


def normalize_trigger_id(trigger_id: Optional[str]) -> Optional[str]:
    raw = str(trigger_id or "").strip()
    if not raw:
        return None
    normalized = "".join(ch for ch in raw if ch.isalnum()).lower()
    if not normalized:
        return None
    return normalized[-16:]


def new_trigger_id() -> str:
    return normalize_trigger_id(uuid4().hex) or uuid4().hex[:16]


def build_order_ref(
    *,
    strategy_id: str,
    symbol: str,
    kind: str | OrderKind,
    trigger_id: Optional[str] = None,
    namespace: str = ORDER_NAMESPACE,
    suffix: Optional[list[str] | tuple[str, ...]] = None,
) -> str:
    namespace_u = str(namespace or ORDER_NAMESPACE).strip().lower() or ORDER_NAMESPACE
    strategy_u = str(strategy_id or "").strip()
    symbol_u = str(symbol or "").strip().upper() or "UNKNOWN"
    kind_u = str(kind.value if isinstance(kind, OrderKind) else kind or "").strip().upper() or OrderKind.GEN.value
    parts = [namespace_u, strategy_u, symbol_u, kind_u]
    trigger_u = normalize_trigger_id(trigger_id)
    if trigger_u:
        parts.extend(["TRG", trigger_u])
    for token in suffix or ():
        token_str = str(token or "").strip()
        if token_str:
            parts.append(token_str)
    return ":".join(parts)


def parse_order_ref(order_ref: str) -> Optional[ParsedOrderRef]:
    raw = str(order_ref or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) < 4:
        return None
    namespace, strategy_id, symbol, kind = parts[:4]
    remainder = parts[4:]
    trigger_id = None
    suffix: tuple[str, ...] = ()
    if len(remainder) >= 2 and str(remainder[0] or "").strip().upper() == "TRG":
        trigger_id = normalize_trigger_id(remainder[1])
        suffix = tuple(str(token or "").strip() for token in remainder[2:] if str(token or "").strip())
    else:
        suffix = tuple(str(token or "").strip() for token in remainder if str(token or "").strip())
    return ParsedOrderRef(
        namespace=str(namespace or "").strip().lower(),
        strategy_id=str(strategy_id or "").strip(),
        symbol=str(symbol or "").strip().upper(),
        kind=str(kind or "").strip().upper(),
        trigger_id=trigger_id,
        suffix=suffix,
    )


def order_ref_belongs_to(
    order_ref: str,
    *,
    strategy_id: str,
    symbol: str,
    namespace: str = ORDER_NAMESPACE,
) -> bool:
    parsed = parse_order_ref(order_ref)
    if parsed is None:
        return False
    return (
        parsed.namespace == str(namespace or ORDER_NAMESPACE).strip().lower()
        and parsed.strategy_id == str(strategy_id or "").strip()
        and parsed.symbol == str(symbol or "").strip().upper()
    )
