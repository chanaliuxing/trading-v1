"""Structured audit event helper."""
from __future__ import annotations

import logging
from typing import Any, Dict

from libs.logger import get_logger
from libs.obs.loki import push as loki_push
import os

AUDIT_LOGGER = get_logger("audit")

REASONS: Dict[str, str] = {
    "low_conf": "confidence below threshold",
    "zero_signal": "signal equals zero",
    "overlay_limit": "portfolio overlay blocked/trimmed",
    "kill_switch": "overlay halted",
    "participation_cap": "volume participation cap",
    "limit_reject": "limit price not marketable",
    "market_closed": "market closed or price unavailable",
    "budget": "insufficient cash or margin",
    "exposure": "exceeds exposure/position limits",
    "fallback_zero": "model fallback to zero",
}


def log_event(
    event: str,
    *,
    level: str = "INFO",
    msg: str = "",
    component: str | None = None,
    **fields: Any,
) -> None:
    levelno = getattr(logging, level.upper(), logging.INFO)
    logger = AUDIT_LOGGER
    payload: Dict[str, Any] = {"event_name": event}
    if component:
        payload["component"] = component
    if fields:
        payload.update(fields)
    logger = logger.bind(**payload)
    logger.log(levelno, msg or event)
    # Optional Loki shipping
    try:
        if os.getenv("LOKI_PUSH_ENABLED", "").strip() in {"1", "true", "TRUE", "yes"}:
            labels = {"job": "ai_trader", "component": component or "audit", "event": event}
            loki_push(labels, payload)
    except Exception:
        pass
