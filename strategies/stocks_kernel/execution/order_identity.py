from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _norm_token(value: object) -> str:
    return str(value or "").strip()


def _sanitize_token(value: object) -> str:
    # Order refs are colon-delimited. Any colon inside a token would corrupt parsing.
    return _norm_token(value).replace(":", "_")


@dataclass(frozen=True)
class ParsedOrderRef:
    namespace: str
    strategy_id: str
    symbol: str
    kind: str
    lane_id: Optional[str] = None
    trigger_id: Optional[str] = None
    lot_id: Optional[str] = None
    seq: Optional[int] = None


@dataclass(frozen=True)
class ParsedProtectionOrderRef:
    role: str
    account_id: str
    strategy_id: str
    symbol: str
    suffix: Optional[str] = None


def build_order_ref(
    *,
    namespace: str,
    strategy_id: str,
    symbol: str,
    kind: str,
    lane_id: Optional[str] = None,
    trigger_id: Optional[str] = None,
    lot_id: Optional[str] = None,
    seq: Optional[int] = None,
) -> str:
    namespace_v = _sanitize_token(namespace)
    strategy_id_v = _sanitize_token(strategy_id)
    symbol_v = _sanitize_token(symbol).upper()
    kind_v = _sanitize_token(kind).upper()
    if not namespace_v or not strategy_id_v or not symbol_v or not kind_v:
        raise ValueError("namespace/strategy_id/symbol/kind are required")
    parts = [namespace_v, strategy_id_v, symbol_v, kind_v]
    if lane_id:
        parts.extend(["LANE", _sanitize_token(lane_id)])
    if trigger_id:
        parts.extend(["TRG", _sanitize_token(trigger_id)])
    if lot_id:
        parts.extend(["LOT", _sanitize_token(lot_id)])
    if seq is not None:
        parts.extend(["SEQ", str(int(seq))])
    return ":".join(parts)


def build_protection_order_ref(
    *,
    role: str,
    account_id: str,
    strategy_id: str,
    symbol: str,
    suffix: Optional[str] = None,
) -> str:
    role_v = _sanitize_token(role).lower()
    account_id_v = _sanitize_token(account_id)
    strategy_id_v = _sanitize_token(strategy_id)
    symbol_v = _sanitize_token(symbol).upper()
    suffix_v = _sanitize_token(suffix)
    if role_v not in {"sl", "tp"}:
        raise ValueError("protection role must be sl or tp")
    if not account_id_v or not strategy_id_v or not symbol_v:
        raise ValueError("account_id/strategy_id/symbol are required")
    return f"{role_v}:{account_id_v}|{strategy_id_v}|{symbol_v}|{suffix_v}"


def parse_order_ref(order_ref: object) -> Optional[ParsedOrderRef]:
    text = _norm_token(order_ref)
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 4:
        return None
    namespace, strategy_id, symbol, kind = parts[:4]
    lane_id: Optional[str] = None
    trigger_id: Optional[str] = None
    lot_id: Optional[str] = None
    seq: Optional[int] = None
    idx = 4
    while idx + 1 < len(parts):
        key = parts[idx].upper()
        value = parts[idx + 1]
        if key == "LANE":
            lane_id = value or None
        elif key == "TRG":
            trigger_id = value or None
        elif key == "LOT":
            lot_id = value or None
        elif key == "SEQ":
            try:
                seq = int(value)
            except Exception:
                return None
        idx += 2
    if idx != len(parts):
        return None
    return ParsedOrderRef(
        namespace=namespace,
        strategy_id=strategy_id,
        symbol=symbol.upper(),
        kind=kind.upper(),
        lane_id=lane_id,
        trigger_id=trigger_id,
        lot_id=lot_id,
        seq=seq,
    )


def parse_protection_order_ref(order_ref: object) -> Optional[ParsedProtectionOrderRef]:
    text = _norm_token(order_ref)
    if not text:
        return None
    role, sep, payload = text.partition(":")
    role_v = role.lower()
    if sep != ":" or role_v not in {"sl", "tp"}:
        return None
    parts = payload.split("|")
    if len(parts) < 4:
        return None
    account_id = parts[0].strip()
    strategy_id = parts[1].strip()
    symbol = parts[2].strip().upper()
    suffix = "|".join(parts[3:]).strip() or None
    if not account_id or not strategy_id or not symbol:
        return None
    return ParsedProtectionOrderRef(
        role=role_v.upper(),
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        suffix=suffix,
    )


def order_ref_belongs_to_strategy(order_ref: object, *, namespace: str, strategy_id: str) -> bool:
    parsed = parse_order_ref(order_ref)
    strategy_id_v = _sanitize_token(strategy_id)
    if parsed is not None:
        return parsed.namespace == _sanitize_token(namespace) and parsed.strategy_id == strategy_id_v
    protection = parse_protection_order_ref(order_ref)
    if protection is None:
        return False
    return protection.strategy_id == strategy_id_v
