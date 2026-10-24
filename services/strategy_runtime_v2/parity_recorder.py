from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class ParityEvent:
    stage: str
    symbol: str
    side: Optional[str] = None
    qty: Optional[float] = None
    reason: Optional[str] = None
    order_ref_kind: Optional[str] = None
    trace_id: Optional[str] = None
    ts: Optional[str] = None
    runtime_transition: Optional[str] = None


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _order_ref_kind(order_ref: Any) -> Optional[str]:
    ref = str(order_ref or "").strip().upper()
    if not ref:
        return None
    for token in ("ENTRY", "STOP", "TP1", "FLATTEN"):
        if f":{token}" in ref:
            return token
    return "OTHER"


def normalize_event(row: Dict[str, Any]) -> ParityEvent:
    payload = dict(row.get("payload") or {})
    side = row.get("side") or payload.get("side") or payload.get("intent_side")
    qty = (
        row.get("qty")
        if row.get("qty") is not None
        else payload.get("qty", payload.get("intent_target_qty", payload.get("target_qty")))
    )
    order_ref = row.get("order_ref") or payload.get("order_ref")
    runtime_before = payload.get("runtime_lifecycle_before")
    runtime_after = payload.get("runtime_lifecycle_after")
    runtime_transition = None
    if runtime_before or runtime_after:
        runtime_transition = f"{runtime_before or '?'}->{runtime_after or '?'}"
    return ParityEvent(
        stage=str(row.get("stage") or ""),
        symbol=str(row.get("symbol") or "").strip().upper(),
        side=str(side).upper() if side is not None else None,
        qty=_as_float(qty),
        reason=str(row.get("reason") or payload.get("reason") or "").strip() or None,
        order_ref_kind=_order_ref_kind(order_ref),
        trace_id=str(row.get("trace_id") or payload.get("trace_id") or "").strip() or None,
        ts=str(row.get("ts") or "").strip() or None,
        runtime_transition=runtime_transition,
    )


def _event_key(event: ParityEvent) -> tuple[str, str, Optional[str]]:
    return (event.stage, event.symbol, event.trace_id)


def _qty_equal(left: Optional[float], right: Optional[float], tolerance: float) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) <= float(tolerance)


def compare_event_streams(
    expected_rows: Iterable[Dict[str, Any]],
    actual_rows: Iterable[Dict[str, Any]],
    *,
    qty_tolerance: float = 1.0,
) -> Dict[str, Any]:
    expected = [normalize_event(row) for row in expected_rows]
    actual = [normalize_event(row) for row in actual_rows]
    actual_map = {_event_key(event): event for event in actual}

    mismatches: List[Dict[str, Any]] = []
    matched = 0
    for event in expected:
        key = _event_key(event)
        peer = actual_map.get(key)
        if peer is None:
            mismatches.append(
                {
                    "category": "missing_event",
                    "key": {"stage": event.stage, "symbol": event.symbol, "trace_id": event.trace_id},
                    "expected": asdict(event),
                    "actual": None,
                }
            )
            continue

        categories: List[str] = []
        if (event.side or None) != (peer.side or None):
            categories.append("side_mismatch")
        if not _qty_equal(event.qty, peer.qty, qty_tolerance):
            categories.append("qty_mismatch")
        if (event.reason or None) != (peer.reason or None):
            categories.append("reason_mismatch")
        if (event.order_ref_kind or None) != (peer.order_ref_kind or None):
            categories.append("order_ref_kind_mismatch")
        if (event.runtime_transition or None) != (peer.runtime_transition or None):
            categories.append("runtime_transition_mismatch")

        if categories:
            mismatches.append(
                {
                    "category": ",".join(categories),
                    "key": {"stage": event.stage, "symbol": event.symbol, "trace_id": event.trace_id},
                    "expected": asdict(event),
                    "actual": asdict(peer),
                }
            )
            continue
        matched += 1

    expected_keys = {_event_key(event) for event in expected}
    extra_events = [asdict(event) for event in actual if _event_key(event) not in expected_keys]
    for item in extra_events:
        mismatches.append({"category": "extra_event", "key": item, "expected": None, "actual": item})

    return {
        "expected_count": len(expected),
        "actual_count": len(actual),
        "matched_count": matched,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "ok": len(mismatches) == 0,
    }
