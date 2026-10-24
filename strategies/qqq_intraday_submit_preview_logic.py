from __future__ import annotations

from typing import Any, Dict, Mapping


def preview_row(
    *,
    status: str,
    reason: str | None = None,
    exec_symbol: str | None = None,
    order_ref: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {"status": status}
    if reason is not None:
        row["reason"] = reason
    if exec_symbol is not None:
        row["exec_symbol"] = exec_symbol
    if order_ref is not None:
        row["order_ref"] = order_ref
    if extra:
        row.update(dict(extra))
    return row


def preview_row_from_outcome(
    *,
    outcome: Any,
    exec_symbol: str | None,
    order_ref: str | None,
    extra: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    return preview_row(
        status=str(getattr(outcome, "preview_status", "") or ""),
        reason=str(getattr(outcome, "reason", "") or ""),
        exec_symbol=exec_symbol,
        order_ref=order_ref,
        extra=extra,
    )
