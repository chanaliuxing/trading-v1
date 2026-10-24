"""Structured JSON logging with trace correlation for observability."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Dict, Optional

try:
    from opentelemetry import trace
except Exception:  # pragma: no cover - optional dependency
    trace = None  # type: ignore

_RUN_ID = os.getenv("RUN_ID", f"run-{int(time.time())}")
_HOSTNAME = socket.gethostname()
_loggers: Dict[str, logging.Logger] = {}
_file_handler_lock = threading.Lock()
_file_handler: Optional[logging.Handler] = None


def _trace_context() -> Dict[str, str]:
    if trace is None:
        return {}
    span = trace.get_current_span()
    if span is None:
        return {}
    ctx = span.get_span_context()
    if not ctx or not ctx.is_valid:
        return {}
    return {
        "trace_id": f"{ctx.trace_id:032x}",
        "span_id": f"{ctx.span_id:016x}",
    }


class JSONFormatter(logging.Formatter):
    """Render log records as JSON with consistent envelope."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload = {
            "ts": int(time.time() * 1000),
            "level": record.levelname,
            "host": getattr(record, "host", _HOSTNAME),
            "run_id": getattr(record, "run_id", _RUN_ID),
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", "log"),
            "msg": record.getMessage(),
        }

        trace_meta = _trace_context()
        if trace_meta:
            payload.update(trace_meta)

        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def get_logger(name: str, *, level: str = "INFO") -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger.handlers = [handler]
    logger.propagate = False
    _loggers[name] = logger
    return logger


def configure_audit_file_logging(path: str = "./logs/app.log") -> None:
    """Attach a rotating file handler for audit logs (idempotent)."""
    global _file_handler
    with _file_handler_lock:
        if _file_handler is not None:
            return
        log_path = Path(path).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(JSONFormatter())
        _file_handler = fh
        audit_logger = get_logger("audit")
        audit_logger.addHandler(fh)

        # Also attach to any other existing JSON loggers for consistency
        for logger in _loggers.values():
            logger.addHandler(fh)
