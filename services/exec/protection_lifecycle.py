from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class ProtectionLifecycleState(str, Enum):
    ENTRY_INTENT_ACCEPTED = "ENTRY_INTENT_ACCEPTED"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    ENTRY_FILLED = "ENTRY_FILLED"
    PROTECTION_REQUIRED = "PROTECTION_REQUIRED"
    PROTECTION_SUBMITTING = "PROTECTION_SUBMITTING"
    PROTECTION_BROKER_ACKED = "PROTECTION_BROKER_ACKED"
    PROTECTION_CORE_PERSISTED = "PROTECTION_CORE_PERSISTED"
    PROTECTION_BROKER_TRUTH_SEEN = "PROTECTION_BROKER_TRUTH_SEEN"
    PROTECTION_VERIFIED = "PROTECTION_VERIFIED"
    ACTIVE_PROTECTED = "ACTIVE_PROTECTED"
    EXITING = "EXITING"
    FLAT_PENDING_PROTECTION_CANCEL = "FLAT_PENDING_PROTECTION_CANCEL"
    CLOSED_VERIFIED = "CLOSED_VERIFIED"
    QUARANTINED_MANUAL_REVIEW = "QUARANTINED_MANUAL_REVIEW"


_ORDER: dict[ProtectionLifecycleState, int] = {
    state: idx for idx, state in enumerate(ProtectionLifecycleState)
}


@dataclass(frozen=True)
class ProtectionLifecycleRecord:
    protection_group_id: str
    strategy_id: str
    strategy_instance_id: str
    symbol: str
    qty: float
    side: str
    state: str
    generation: int
    updated_at: str
    owner_lane: dict[str, Any] = field(default_factory=dict)
    broker_order_ids: tuple[str, ...] = ()
    core_order_ids: tuple[str, ...] = ()
    perm_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.state in {
            ProtectionLifecycleState.PROTECTION_VERIFIED.value,
            ProtectionLifecycleState.ACTIVE_PROTECTED.value,
        }

    @property
    def converging(self) -> bool:
        return self.state in {
            ProtectionLifecycleState.PROTECTION_REQUIRED.value,
            ProtectionLifecycleState.PROTECTION_SUBMITTING.value,
            ProtectionLifecycleState.PROTECTION_BROKER_ACKED.value,
            ProtectionLifecycleState.PROTECTION_CORE_PERSISTED.value,
            ProtectionLifecycleState.PROTECTION_BROKER_TRUTH_SEEN.value,
        }

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["broker_order_ids"] = list(self.broker_order_ids)
        data["core_order_ids"] = list(self.core_order_ids)
        data["perm_ids"] = list(self.perm_ids)
        return data


def _now_iso() -> str:
    return datetime.now(ET).isoformat()


def new_protection_lifecycle(
    *,
    protection_group_id: str,
    strategy_id: str,
    strategy_instance_id: str,
    symbol: str,
    qty: float,
    side: str,
    owner_lane: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ProtectionLifecycleRecord:
    return ProtectionLifecycleRecord(
        protection_group_id=str(protection_group_id),
        strategy_id=str(strategy_id),
        strategy_instance_id=strategy_instance_id,
        symbol=str(symbol),
        qty=float(qty),
        side=str(side),
        state=ProtectionLifecycleState.ENTRY_INTENT_ACCEPTED.value,
        generation=0,
        updated_at=_now_iso(),
        owner_lane=dict(owner_lane or {}),
        metadata=dict(metadata or {}),
    )


def transition_protection_lifecycle(
    record: ProtectionLifecycleRecord,
    state: ProtectionLifecycleState | str,
    *,
    metadata: dict[str, Any] | None = None,
    broker_order_ids: tuple[str, ...] | list[str] | None = None,
    core_order_ids: tuple[str, ...] | list[str] | None = None,
    perm_ids: tuple[str, ...] | list[str] | None = None,
) -> ProtectionLifecycleRecord:
    target = state if isinstance(state, ProtectionLifecycleState) else ProtectionLifecycleState(str(state))
    current = ProtectionLifecycleState(record.state)
    if _ORDER[target] < _ORDER[current] and target != ProtectionLifecycleState.QUARANTINED_MANUAL_REVIEW:
        raise ValueError(f"protection_lifecycle_regression:{record.state}->{target.value}")
    merged_meta = dict(record.metadata)
    merged_meta.update(dict(metadata or {}))
    merged_broker_order_ids = tuple(broker_order_ids) if broker_order_ids is not None else record.broker_order_ids
    merged_core_order_ids = tuple(core_order_ids) if core_order_ids is not None else record.core_order_ids
    merged_perm_ids = tuple(perm_ids) if perm_ids is not None else record.perm_ids
    if target == current and merged_meta == record.metadata:
        return record
    return replace(
        record,
        state=target.value,
        generation=record.generation + 1,
        updated_at=_now_iso(),
        metadata=merged_meta,
        broker_order_ids=merged_broker_order_ids,
        core_order_ids=merged_core_order_ids,
        perm_ids=merged_perm_ids,
    )


def protection_verification_ready(record: ProtectionLifecycleRecord) -> bool:
    return bool(record.broker_order_ids and record.core_order_ids and record.owner_lane)


def lifecycle_state_from_registry_state(state: Any) -> ProtectionLifecycleState:
    normalized = str(state or "").strip().upper()
    if normalized in {"NEW"}:
        return ProtectionLifecycleState.PROTECTION_REQUIRED
    if normalized in {"SUBMITTED", "PARTIAL"}:
        return ProtectionLifecycleState.PROTECTION_SUBMITTING
    if normalized in {"ACTIVE"}:
        return ProtectionLifecycleState.PROTECTION_VERIFIED
    if normalized in {"CANCELLED", "CANCELED", "FILLED", "EXPIRED", "REJECTED", "FAILED"}:
        return ProtectionLifecycleState.CLOSED_VERIFIED
    return ProtectionLifecycleState.QUARANTINED_MANUAL_REVIEW


def protection_group_id_from_registry_row(row: dict[str, Any]) -> str:
    bracket = str(row.get("bracket_id") or "").strip()
    if bracket:
        return bracket
    owner_key = str(row.get("position_owner_key") or row.get("position_owner") or "").strip()
    if owner_key:
        return owner_key
    key = str(row.get("protection_key") or "").strip()
    parts = key.split("|")
    if len(parts) >= 4:
        return "|".join(parts[:4])
    return key


def registry_row_to_lifecycle_record(row: dict[str, Any]) -> ProtectionLifecycleRecord:
    protection_key = str(row.get("protection_key") or "").strip()
    parts = protection_key.split("|")
    strategy_id = str(row.get("strategy_id") or (parts[1] if len(parts) > 1 else "") or "").strip()
    strategy_instance_id = str(row.get("strategy_instance_id") or strategy_id).strip()
    symbol = str(row.get("symbol") or (parts[2] if len(parts) > 2 else "") or "").strip().upper()
    lot_id = str(row.get("lot_id") or (parts[3] if len(parts) > 3 else "") or "").strip()
    broker_order_id = str(row.get("broker_order_id") or "").strip()
    ib_perm_id = str(row.get("ib_perm_id") or "").strip()
    try:
        qty = abs(float(row.get("desired_qty") or 0.0))
    except Exception:
        qty = 0.0
    return ProtectionLifecycleRecord(
        protection_group_id=protection_group_id_from_registry_row(row),
        strategy_id=strategy_id,
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        qty=qty,
        side=str(row.get("side") or "").strip().upper(),
        state=lifecycle_state_from_registry_state(row.get("state")).value,
        generation=0,
        updated_at=str(row.get("updated_at") or row.get("first_seen_at") or _now_iso()),
        owner_lane={
            "strategy_id": strategy_id,
            "strategy_instance_id": strategy_instance_id,
            "symbol": symbol,
            "lot_id": lot_id,
            "position_owner_key": str(row.get("position_owner_key") or row.get("position_owner") or "").strip(),
        },
        broker_order_ids=(broker_order_id,) if broker_order_id else (),
        perm_ids=(ib_perm_id,) if ib_perm_id else (),
        metadata={
            "protection_key": protection_key,
            "registry_state": str(row.get("state") or "").strip().upper(),
            "order_role": str(row.get("order_role") or "").strip().upper(),
            "protection_kind": str(row.get("protection_kind") or "").strip().lower(),
        },
    )
