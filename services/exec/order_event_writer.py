from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from prometheus_client import Counter

from libs.logger import get_logger

logger = get_logger(__name__)

_MISSING_TABLE_WARNED = False


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value

order_event_write_total = Counter(
    "order_event_write_total",
    "Order event write outcomes",
    ["event_type", "result"],
)


async def emit_order_event(
    *,
    pg: Any,
    enabled: bool,
    event_type: str,
    trace_id: str,
    strategy_id: str,
    strategy_run_id: str,
    decision_id: str,
    symbol: str,
    causation_id: Optional[str] = None,
    source_event_id: Optional[str] = None,
    trigger_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    order_id: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    parent_order_id: Optional[str] = None,
    ib_connection_epoch: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> bool:
    if not enabled:
        order_event_write_total.labels(event_type=str(event_type or ""), result="disabled").inc()
        return False

    payload_json = json.dumps(_json_safe(payload or {}), ensure_ascii=False)
    event_id = str(uuid.uuid4())
    try:
        await pg.execute(
            """
            INSERT INTO core.core_order_event (
                event_id, event_type, event_ts,
                trace_id, causation_id, source_event_id,
                strategy_id, strategy_run_id, trigger_id, decision_id, intent_id,
                order_id, broker_order_id, parent_order_id, ib_connection_epoch, symbol, payload
            ) VALUES (
                $1::uuid, $2, now(),
                $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11::uuid, $12, $13::uuid, $14, $15, $16::jsonb
            )
            """,
            event_id,
            str(event_type or ""),
            str(trace_id or ""),
            causation_id,
            source_event_id,
            str(strategy_id or ""),
            str(strategy_run_id or ""),
            trigger_id,
            str(decision_id or ""),
            intent_id,
            order_id,
            broker_order_id,
            parent_order_id,
            ib_connection_epoch,
            str(symbol or ""),
            payload_json,
        )
        order_event_write_total.labels(event_type=str(event_type or ""), result="success").inc()
        return True
    except Exception as exc:  # noqa: BLE001
        global _MISSING_TABLE_WARNED
        msg = str(exc)
        missing_table = "core_order_event" in msg and "does not exist" in msg.lower()
        result = "error"
        if missing_table and not _MISSING_TABLE_WARNED:
            _MISSING_TABLE_WARNED = True
            logger.warning("core_order_event_table_missing", error=msg)
            result = "missing_table"
        elif not missing_table:
            logger.warning("core_order_event_write_failed", event_type=event_type, error=msg)
        else:
            result = "missing_table"
        order_event_write_total.labels(event_type=str(event_type or ""), result=result).inc()
        return False
