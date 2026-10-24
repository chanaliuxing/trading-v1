from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional

from libs.logger import get_logger

from .dispatcher import NotificationDispatcher, NotificationManager
from .telegram_notifier import TelegramNotifier

logger = get_logger(__name__)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _enabled_strategy_ids(cfg: Dict[str, Any]) -> list[str]:
    strategies = cfg.get("strategies") if isinstance(cfg, dict) else None
    if not isinstance(strategies, list):
        return []
    out: list[str] = []
    for item in strategies:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "").strip()
        if not sid:
            continue
        if bool(item.get("enabled", False)):
            out.append(sid)
    return out


def _dedup_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        v = str(raw).strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def build_notification_manager(config: Optional[Dict[str, Any]] = None) -> Optional[NotificationManager]:
    """Build notification manager from config + env (env has higher priority)."""
    cfg = config or {}
    ncfg = (cfg.get("notifications") or {}) if isinstance(cfg, dict) else {}
    tcfg = (ncfg.get("telegram") or {}) if isinstance(ncfg, dict) else {}

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", str(tcfg.get("bot_token") or "")).strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", str(tcfg.get("chat_id") or "")).strip()

    if not bot_token or not chat_id:
        logger.info("Telegram notifier disabled: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return None

    enabled_env = os.getenv("TELEGRAM_NOTIFY_ENABLED", "").strip().lower()
    enabled_cfg = bool(ncfg.get("enabled", True)) and bool(tcfg.get("enabled", True))
    if enabled_env in {"0", "false", "no", "off"}:
        enabled_cfg = False
    if not enabled_cfg:
        logger.info("Telegram notifier disabled by config")
        return None

    allowlist_cfg = tcfg.get("strategy_allowlist") or []
    if isinstance(allowlist_cfg, str):
        allowlist = [s.strip() for s in allowlist_cfg.split(",") if s.strip()]
    else:
        allowlist = [str(x).strip() for x in allowlist_cfg if str(x).strip()]

    allowlist_env = os.getenv("TELEGRAM_STRATEGY_ALLOWLIST", "").strip()
    if allowlist_env:
        allowlist = [s.strip() for s in allowlist_env.split(",") if s.strip()]

    # Keep Telegram strategy allowlist in sync with enabled strategies by default,
    # so newly enabled strategies automatically get notifications.
    sync_enabled = str(os.getenv("TELEGRAM_ALLOWLIST_SYNC_ENABLED", "1")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if sync_enabled:
        enabled_ids = _enabled_strategy_ids(cfg)
        allowlist = _dedup_keep_order([*allowlist, *enabled_ids, "system_reconciler"])
    else:
        allowlist = _dedup_keep_order(allowlist)

    notifier = TelegramNotifier(
        bot_token=bot_token,
        chat_id=chat_id,
        timeout_sec=_to_float(os.getenv("TELEGRAM_TIMEOUT_SEC", tcfg.get("timeout_sec", 3.0)), 3.0),
        max_retries=_to_int(os.getenv("TELEGRAM_MAX_RETRIES", tcfg.get("max_retries", 3)), 3),
        retry_base_delay_sec=_to_float(os.getenv("TELEGRAM_RETRY_BASE_DELAY_SEC", tcfg.get("retry_base_delay_sec", 0.5)), 0.5),
        retry_max_delay_sec=_to_float(os.getenv("TELEGRAM_RETRY_MAX_DELAY_SEC", tcfg.get("retry_max_delay_sec", 5.0)), 5.0),
        retry_jitter_sec=_to_float(os.getenv("TELEGRAM_RETRY_JITTER_SEC", tcfg.get("retry_jitter_sec", 0.25)), 0.25),
        failure_threshold=_to_int(os.getenv("TELEGRAM_CB_FAILURE_THRESHOLD", tcfg.get("failure_threshold", 5)), 5),
        breaker_open_timeout_sec=_to_float(os.getenv("TELEGRAM_CB_OPEN_TIMEOUT_SEC", tcfg.get("open_timeout_sec", 60.0)), 60.0),
    )

    queue_size = _to_int(os.getenv("TELEGRAM_NOTIFY_QUEUE_SIZE", ncfg.get("queue_size", 1000)), 1000)
    worker_count = _to_int(os.getenv("TELEGRAM_NOTIFY_WORKERS", ncfg.get("worker_count", 1)), 1)

    dispatcher = NotificationDispatcher([notifier], queue_size=queue_size, worker_count=worker_count)
    manager = NotificationManager(dispatcher=dispatcher, strategy_allowlist=allowlist)

    logger.info(
        "Telegram notifier enabled",
        strategy_allowlist=allowlist or "ALL",
        queue_size=queue_size,
        worker_count=worker_count,
    )
    return manager
