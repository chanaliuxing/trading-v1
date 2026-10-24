from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def _clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean_value(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_clean_value(v) for v in value if v is not None]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ET)
        else:
            value = value.astimezone(ET)
        return value.isoformat()
    return value


def build_audit_record(
    *,
    domain: str,
    stage: str,
    severity: str = "info",
    ts: Optional[datetime] = None,
    component: Optional[str] = None,
    strategy_id: Optional[str] = None,
    strategy_run_id: Optional[str] = None,
    trigger_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    causation_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    order_id: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    perm_id: Optional[Any] = None,
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = ts or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    record = {
        "ts": now.isoformat(),
        "domain": str(domain or "").strip().lower() or "system",
        "component": str(component or "").strip() or "unknown",
        "stage": str(stage or "").strip() or "unknown",
        "severity": str(severity or "info").strip().lower(),
        "strategy_id": strategy_id,
        "strategy_run_id": strategy_run_id,
        "trigger_id": trigger_id,
        "decision_id": decision_id,
        "trace_id": trace_id,
        "causation_id": causation_id,
        "intent_id": intent_id,
        "order_id": order_id,
        "broker_order_id": broker_order_id,
        "perm_id": perm_id,
        "symbol": symbol,
        "status": status,
        "reason": reason,
        "payload": payload or {},
    }
    return _clean_value(record)
