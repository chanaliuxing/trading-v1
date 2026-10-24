from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OwnershipDecision:
    allowed: bool
    reason: str
    owner_strategy_id: str | None = None
    owner_lane_id: str | None = None
    payload: dict[str, Any] | None = None


class OwnershipPolicy:
    """
    Runtime-safe ownership checks for same-symbol multi-strategy coexistence.

    Default behavior is strategy-scoped and permissive for backward compatibility.
    """

    def __init__(self, *, default_scope: str = "strategy") -> None:
        self._default_scope = str(default_scope or "strategy").strip() or "strategy"

    def allow_entry(
        self,
        *,
        strategy_id: str,
        lane_id: str | None,
        symbol: str,
        active_lots: tuple[Any, ...],
    ) -> OwnershipDecision:
        strategy_id_v = str(strategy_id or "").strip()
        lane_id_v = str(lane_id or "").strip() or None
        symbol_v = str(symbol or "").strip().upper()
        for lot in active_lots:
            if str(getattr(lot, "symbol", "") or "").strip().upper() != symbol_v:
                continue
            lot_strategy_id = str(getattr(lot, "strategy_id", "") or "").strip()
            lot_lane_id = str(getattr(lot, "lane_id", "") or "").strip() or None
            ownership_scope = str(getattr(lot, "ownership_scope", "") or self._default_scope).strip() or self._default_scope
            if ownership_scope == "strategy" and lot_strategy_id != strategy_id_v:
                return OwnershipDecision(
                    allowed=False,
                    reason="foreign_strategy_owns_symbol",
                    owner_strategy_id=lot_strategy_id,
                    owner_lane_id=lot_lane_id,
                    payload={"symbol": symbol_v, "ownership_scope": ownership_scope},
                )
            if ownership_scope == "lane" and lot_strategy_id == strategy_id_v and lane_id_v and lot_lane_id and lot_lane_id != lane_id_v:
                return OwnershipDecision(
                    allowed=False,
                    reason="foreign_lane_owns_symbol",
                    owner_strategy_id=lot_strategy_id,
                    owner_lane_id=lot_lane_id,
                    payload={"symbol": symbol_v, "ownership_scope": ownership_scope},
                )
        return OwnershipDecision(allowed=True, reason="ready", payload={"symbol": symbol_v})

    def can_manage_protection(
        self,
        *,
        strategy_id: str,
        lot: Any,
        requested_lane_id: str | None = None,
    ) -> OwnershipDecision:
        return self._authority_decision(
            strategy_id=strategy_id,
            lot=lot,
            authority_field="protection_authority",
            default_reason="ready",
            requested_lane_id=requested_lane_id,
        )

    def can_flatten(
        self,
        *,
        strategy_id: str,
        lot: Any,
        requested_lane_id: str | None = None,
    ) -> OwnershipDecision:
        return self._authority_decision(
            strategy_id=strategy_id,
            lot=lot,
            authority_field="flatten_authority",
            default_reason="ready",
            requested_lane_id=requested_lane_id,
        )

    def can_adopt_reconcile(
        self,
        *,
        strategy_id: str,
        lot: Any,
        requested_lane_id: str | None = None,
    ) -> OwnershipDecision:
        return self._authority_decision(
            strategy_id=strategy_id,
            lot=lot,
            authority_field="adopt_authority",
            default_reason="ready",
            requested_lane_id=requested_lane_id,
        )

    def can_build_protection_for_lane(
        self,
        *,
        strategy_id: str,
        instrument: Any,
        exec_symbol: str,
        lane_id: str | None = None,
    ) -> OwnershipDecision:
        lane = None
        if instrument is not None:
            lane_id_v = str(lane_id or "").strip() or None
            if lane_id_v and hasattr(instrument, "lane_by_id"):
                lane = instrument.lane_by_id(lane_id_v)
            if lane is None and hasattr(instrument, "lanes"):
                exec_symbol_v = str(exec_symbol or "").strip().upper()
                for candidate in tuple(instrument.lanes("execution") or ()):
                    if str(getattr(candidate, "symbol", "") or "").strip().upper() == exec_symbol_v:
                        lane = candidate
                        break
        authority = str(getattr(lane, "protection_authority", "") or "strategy").strip() or "strategy"
        if authority not in {"strategy", "lane"}:
            return OwnershipDecision(
                allowed=False,
                reason="protection_authority_reserved",
                owner_strategy_id=str(strategy_id or "").strip() or None,
                owner_lane_id=str(getattr(lane, "lane_id", "") or "").strip() or None,
                payload={
                    "authority": authority,
                    "authority_field": "protection_authority",
                    "exec_symbol": str(exec_symbol or "").strip().upper(),
                    "requested_strategy_id": str(strategy_id or "").strip() or None,
                    "lane_id": str(getattr(lane, "lane_id", "") or "").strip() or None,
                },
            )
        return OwnershipDecision(
            allowed=True,
            reason="ready",
            owner_strategy_id=str(strategy_id or "").strip() or None,
            owner_lane_id=str(getattr(lane, "lane_id", "") or "").strip() or None,
            payload={
                "authority": authority,
                "authority_field": "protection_authority",
                "exec_symbol": str(exec_symbol or "").strip().upper(),
                "requested_strategy_id": str(strategy_id or "").strip() or None,
                "lane_id": str(getattr(lane, "lane_id", "") or "").strip() or None,
            },
        )

    @staticmethod
    def _authority_decision(
        *,
        strategy_id: str,
        lot: Any,
        authority_field: str,
        default_reason: str,
        requested_lane_id: str | None = None,
    ) -> OwnershipDecision:
        strategy_id_v = str(strategy_id or "").strip()
        lot_strategy_id = str(getattr(lot, "strategy_id", "") or "").strip()
        lot_lane_id = str(getattr(lot, "lane_id", "") or "").strip() or None
        requested_lane_id_v = str(requested_lane_id or "").strip() or None
        lot_symbol = str(getattr(lot, "symbol", "") or "").strip().upper() or None
        authority = str(getattr(lot, authority_field, "") or "strategy").strip() or "strategy"
        payload = {
            "authority": authority,
            "authority_field": authority_field,
            "requested_strategy_id": strategy_id_v or None,
            "requested_lane_id": requested_lane_id_v,
            "lot_strategy_id": lot_strategy_id or None,
            "lot_lane_id": lot_lane_id,
            "symbol": lot_symbol,
        }
        if authority not in {"strategy", "lane"}:
            return OwnershipDecision(
                allowed=False,
                reason=f"{authority_field}_reserved",
                owner_strategy_id=lot_strategy_id or None,
                owner_lane_id=lot_lane_id,
                payload=payload,
            )
        if authority == "strategy" and lot_strategy_id != str(strategy_id or "").strip():
            return OwnershipDecision(
                allowed=False,
                reason=f"{authority_field}_denied",
                owner_strategy_id=lot_strategy_id,
                owner_lane_id=lot_lane_id,
                payload=payload,
            )
        if authority == "lane":
            if lot_strategy_id != strategy_id_v:
                return OwnershipDecision(
                    allowed=False,
                    reason=f"{authority_field}_denied",
                    owner_strategy_id=lot_strategy_id,
                    owner_lane_id=lot_lane_id,
                    payload=payload,
                )
            if requested_lane_id_v and lot_lane_id and requested_lane_id_v != lot_lane_id:
                return OwnershipDecision(
                    allowed=False,
                    reason=f"{authority_field}_lane_denied",
                    owner_strategy_id=lot_strategy_id or None,
                    owner_lane_id=lot_lane_id,
                    payload=payload,
                )
        return OwnershipDecision(
            allowed=True,
            reason=default_reason,
            owner_strategy_id=lot_strategy_id or None,
            owner_lane_id=lot_lane_id,
            payload=payload,
        )
