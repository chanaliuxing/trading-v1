"""Unified logging helpers: console + JSON file for Promtail."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import warnings
from pathlib import Path
from typing import Optional

import structlog
from structlog.stdlib import BoundLogger, ProcessorFormatter
from structlog.typing import Processor

DEFAULT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", os.path.expanduser("~/grafana-stack/logs/trader.log"))
_CONFIGURED = False


class _DemoteNoiseFilter(logging.Filter):
    """Demote known noisy third‑party logs to DEBUG level.

    Currently handles IBKR historical data noise like:
    - "Error 162, reqId X: Historical Market Data Service error message: API historical data query cancelled"
    - "Error 162, reqId X: Historical Market Data Service error message: HMDS query returned no data"
    """

    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - trivial
        try:
            msg = record.getMessage() or ""
        except Exception:
            msg = str(getattr(record, "msg", ""))

        # Completely suppress HMDS "no data" noise (very common and not useful)
        hmds_no_data = (
            "Error 162" in msg
            and "Historical Market Data Service" in msg
            and "HMDS query returned no data" in msg
        )
        if hmds_no_data:
            return False  # Suppress completely

        # Demote cancel messages to DEBUG
        cancel_noise = (
            "Error 162" in msg
            and "Historical Market Data Service" in msg
            and "cancel" in msg.lower()
        )
        if cancel_noise:
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"

        return True


def _shared_pre_chain() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),  # Use local timezone (America/New_York)
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(
    *,
    json_file_path: Optional[str] = None,
    console_human_readable: bool = True,
    level: str = "INFO",
) -> None:
    """Initialise logging once, wiring console + JSON file handlers."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved_path = json_file_path or DEFAULT_LOG_PATH
    if resolved_path:
        Path(resolved_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Console handler (human readable or JSON)
    renderer = structlog.dev.ConsoleRenderer() if console_human_readable else structlog.processors.JSONRenderer()
    console_handler = logging.StreamHandler(sys.stdout)
    if level.upper() == "DEBUG":
        console_handler.setLevel(logging.DEBUG)
    else:
        # Concise console mode: only show WARNING/ERROR on console when LOG_CONCISE=1
        concise = os.getenv("LOG_CONCISE", "").strip().lower() in {"1", "true", "yes", "on"}
        console_handler.setLevel(logging.WARNING if concise else root.level)
    console_handler.setFormatter(
        ProcessorFormatter(processor=renderer, foreign_pre_chain=_shared_pre_chain())
    )
    console_handler.addFilter(_DemoteNoiseFilter())
    root.addHandler(console_handler)

    # File handler for Promtail ingestion
    if resolved_path:
        # Detect multiprocessing worker mode - use simple FileHandler to avoid rotation race conditions
        # In multiprocessing, all workers share the same log file, and RotatingFileHandler causes
        # FileNotFoundError when multiple processes try to rotate simultaneously
        import multiprocessing
        current_process = multiprocessing.current_process()
        is_worker = current_process.name != 'MainProcess'

        if is_worker:
            # Worker process: Use simple FileHandler (append-only, no rotation)
            # Parent process will handle rotation
            file_handler = logging.FileHandler(
                os.path.expanduser(resolved_path),
                mode='a',
                encoding="utf-8",
            )
        else:
            # Main process: Use RotatingFileHandler as before
            file_handler = logging.handlers.RotatingFileHandler(
                os.path.expanduser(resolved_path),
                maxBytes=50 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )

        file_handler.setLevel(root.level)
        file_handler.setFormatter(
            ProcessorFormatter(processor=structlog.processors.JSONRenderer(), foreign_pre_chain=_shared_pre_chain())
        )
        file_handler.addFilter(_DemoteNoiseFilter())
        root.addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *_shared_pre_chain(),
            ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def _ensure_configured() -> None:
    if not _CONFIGURED:
        configure_logging()


def get_logger(name: Optional[str] = None) -> BoundLogger:
    """Return a structlog BoundLogger instance."""
    _ensure_configured()
    return structlog.get_logger(name)


def install_error_hooks(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """
    Capture uncaught exceptions, asyncio task errors, and warnings so they flow through logging.
    Calling this repeatedly is safe; the last installed hooks win.
    """
    log = get_logger("runtime").bind(component="error_hooks")

    def _excepthook(exc_type, exc, tb):
        log.error(
            "unhandled_exception",
            exc_type=exc_type.__name__,
            err=str(exc),
            exc_info=(exc_type, exc, tb),
        )

    sys.excepthook = _excepthook

    target_loop = loop
    if target_loop is None:
        try:
            target_loop = asyncio.get_event_loop()
        except RuntimeError:
            target_loop = None

    if target_loop is not None:

        def _async_err_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
            message = context.get("message")
            exc = context.get("exception")
            if exc is not None:
                log.error("asyncio_task_exception", message=message, err=str(exc), exc_info=exc)
            else:
                log.error("asyncio_task_error", message=message)

        target_loop.set_exception_handler(_async_err_handler)

    warnings.simplefilter("default")
    logging.captureWarnings(True)

    log.info("error_hooks_installed")
