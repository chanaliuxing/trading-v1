"""Early logging initialisation for observability stack."""
from __future__ import annotations

import logging
import os

from libs.logger import configure_logging, get_logger, install_error_hooks

try:
    from obs.tracing import setup_tracing
except Exception:  # pragma: no cover
    setup_tracing = None  # type: ignore

LOG_PATH = os.getenv("AUDIT_LOG_PATH", os.path.expanduser("~/grafana-stack/logs/trader.log"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

configure_logging(json_file_path=LOG_PATH, console_human_readable=True, level=LOG_LEVEL)

# ============================================================================
# Per-component log level controls
# ============================================================================
# QUIET: Data pipeline components (backfill/catchup/insert) - only WARN/ERROR
_QUIET_LOGGERS = [
    "services.market.backfill_service",
    "services.market.catchup_service",
    "services.market.writer.ch_writer",
    "services.market.writer.lake_writer",
    "services.market.features.feature_calculator",
    "services.market.features.feature_publisher",
    "services.market.features.main",
    "services.market.aggregator.five_sec_agg",
    # "libs.db.clickhouse",  # Temporarily moved to INFO to debug connection pool
    "libs.marketdata.vix_ingestor",
    "libs.event_loop_monitor",  # Suppress loop lag warnings (already monitored)
    "aiokafka",
    "aiokafka.consumer.subscription_state",
    "aiokafka.consumer.fetcher",  # Suppress noisy fetch request logs
    "aiokafka.consumer.group_coordinator",
    "aiokafka.conn",
    "kafka",
    "urllib3",
    "ib_insync",
    "ib_insync.wrapper",
    "ib_insync.client",
    "py.warnings",
]

for logger_name in _QUIET_LOGGERS:
    # Set py.warnings to ERROR to suppress ResourceWarning from ib_insync+Python3.13
    if logger_name == "py.warnings":
        logging.getLogger(logger_name).setLevel(logging.ERROR)
    else:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

# READABLE: Service lifecycle and IB connection - keep at INFO
_INFO_LOGGERS = [
    "__main__",
    "services.market.market_data_service",
    "services.market.ib_market_data",
    "libs.db.clickhouse",  # Temporarily added to debug connection pool
]

for logger_name in _INFO_LOGGERS:
    logging.getLogger(logger_name).setLevel(logging.INFO)

# DETAILED: Strategy execution - keep at INFO (or DEBUG if needed)
_STRATEGY_LOGGERS = [
    "strategies",
    "services.strategy",
    "libs.strategy",
]

for logger_name in _STRATEGY_LOGGERS:
    logging.getLogger(logger_name).setLevel(logging.INFO)

# Optional: quieter Ctrl+C / shutdown noise
# When QUIET_CTRL_C is set (e.g. "1", "true"), suppress noisy shutdown logs
quiet_ctrl_c = os.getenv("QUIET_CTRL_C", "").lower()
if quiet_ctrl_c in ("1", "true", "yes", "on"):
    # Suppress asyncio shutdown diagnostics like "Unclosed client session"
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    # Suppress aiokafka group coordinator errors on LeaveGroup during shutdown
    logging.getLogger("aiokafka.consumer.group_coordinator").setLevel(logging.CRITICAL)
    # Suppress ib_insync event-loop-closed noise in __del__ during interpreter exit
    logging.getLogger("ib_insync").setLevel(logging.CRITICAL)

install_error_hooks()
get_logger("boot").info("logging_initialized", log_path=LOG_PATH)

if setup_tracing is not None:
    try:
        setup_tracing()
    except Exception:
        pass
