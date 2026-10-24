"""
RFE ClickHouse Sink with Micro-Batching + ReplacingMergeTree

Persists 1m RFE emissions to ClickHouse for nightly training with:
- ReplacingMergeTree(ver) to allow idempotent replays (dedupe on (symbol, ts) by ver)
- In-process micro-batching to reduce insert overhead (flush on size/time)

Notes:
- Table: trading.features_1m_active (minimal schema for OOF/HPO)
- Mapping: ema_20_close_1m → ema_20, rsi_14_1m → rsi_14, atr_14_1m → atr_14, vwap_1m → vwap
"""
from __future__ import annotations

from typing import Dict, Any
import threading
import time
from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


_ENSURED = False

# Protects table DDL / ALTER so we never run those concurrently, even if
# multiple threads start emitting RFE rows at the same time.
_DDL_LOCK = threading.Lock()

# Micro-batch buffer (process-local)
_BUF_LOCK = threading.Lock()
_BUF = []  # list[list[Any]]
_LAST_FLUSH_TS = 0.0
_MAX_ROWS = 200
_MAX_SEC = 5.0


def _ensure_table(table: str = "trading.features_1m_active") -> None:
    global _ENSURED
    if _ENSURED:
        return
    # Guard DDL with a lock to avoid concurrent CREATE/ALTER attempts and
    # to ensure only one thread ever talks to ClickHouse for this step.
    with _DDL_LOCK:
        if _ENSURED:
            return
        try:
            ch = get_client()
            # 1) Ensure baseline features_1m exists and collect its schema
            baseline_cols: Dict[str, str] = {}
            try:
                rows = ch.query("DESCRIBE TABLE trading.features_1m")
                for r in rows:
                    name = r.get("name") or r.get("column")
                    typ = r.get("type")
                    if name and typ:
                        baseline_cols[str(name)] = str(typ)
            except Exception as exc:
                logger.warning("rfe_sink_describe_baseline_failed", error=str(exc))

            # 2) Create active table as a copy of baseline schema if it does not exist.
            # We deliberately avoid specifying an explicit column list here so that
            # the active table tracks the full feature set, not just a minimal subset.
            try:
                ddl = (
                    f"CREATE TABLE IF NOT EXISTS {table} "
                    "AS trading.features_1m "
                    "ENGINE = ReplacingMergeTree() "
                    "ORDER BY (symbol, ts)"
                )
                ch.command(ddl)
            except Exception as exc:
                logger.warning("rfe_sink_table_create_failed", table=table, error=str(exc))

            # 3) Reconcile active schema with baseline: add any baseline columns
            # that are missing from the active table so gateway/model features
            # can be read consistently.
            try:
                active_rows = ch.query(f"DESCRIBE TABLE {table}")
                active_cols = {r.get("name") or r.get("column") for r in active_rows}
                for name, typ in baseline_cols.items():
                    if name not in active_cols:
                        try:
                            ch.command(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {typ}")
                        except Exception as exc2:
                            logger.warning(
                                "rfe_sink_add_baseline_column_failed",
                                table=table,
                                column=name,
                                error=str(exc2),
                            )
            except Exception as exc:
                logger.warning("rfe_sink_describe_active_failed", table=table, error=str(exc))

            # 4) Ensure RFE-specific columns exist on top of the baseline schema.
            extra_cols = [
                ("orb5_hi", "Nullable(Float64)"),
                ("orb5_lo", "Nullable(Float64)"),
                ("orb5_frozen", "Nullable(UInt8)"),
                ("ver", "UInt64"),
                ("ingested_at", "DateTime DEFAULT now()"),
            ]
            for col_name, col_type in extra_cols:
                try:
                    ch.command(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
                except Exception as exc:
                    logger.warning(
                        "rfe_sink_add_extra_column_failed",
                        table=table,
                        column=col_name,
                        error=str(exc),
                    )

            _ENSURED = True
            logger.info("rfe_sink_table_ready", table=table)
        except Exception as exc:
            logger.error("rfe_sink_table_ensure_failed", table=table, error=str(exc))


def _map_features(symbol: str, ts_us: int, feats: Dict[str, Any]) -> Dict[str, Any]:
    # Baseline row with required OHLCV (assumed present in feats via engine attach)
    row = {
        "ts": int(ts_us // 1_000_000),
        "symbol": symbol,
        "open": float(feats.get("open", float("nan"))),
        "high": float(feats.get("high", float("nan"))),
        "low": float(feats.get("low", float("nan"))),
        "close": float(feats.get("close", float("nan"))),
        "volume": float(feats.get("volume", 0.0)),
        "trades": int(feats.get("trades", 0) or 0),
        # Optional mapped indicators
        "atr_14": None,
        "rsi_14": None,
        "ema_20": None,
        "vwap": None,
        "orb5_hi": None,
        "orb5_lo": None,
        "orb5_frozen": None,
        "ver": int(ts_us),
    }

    # Map indicator keys from RFE naming to canonical columns
    for k, v in feats.items():
        if v is None:
            continue
        # ORB5 keys already canonical
        if k in ("orb5_hi", "orb5_lo", "orb5_frozen"):
            row[k] = float(v) if k != "orb5_frozen" else int(bool(v))
            continue
        ks = str(k)
        if ks.startswith("atr_14") and ks.endswith("_1m"):
            row["atr_14"] = float(v)
        elif ks.startswith("rsi_14") and ks.endswith("_1m"):
            row["rsi_14"] = float(v)
        elif ks.startswith("ema_20_") and ks.endswith("_1m"):
            row["ema_20"] = float(v)
        elif ks == "vwap_1m":
            row["vwap"] = float(v)

    return row


def _flush(table: str) -> None:
    """Flush buffered rows to ClickHouse."""
    global _LAST_FLUSH_TS
    buf_copy = []
    with _BUF_LOCK:
        if not _BUF:
            return
        buf_copy, _BUF[:] = _BUF[:], []
        _LAST_FLUSH_TS = time.time()
    try:
        # Serialize ClickHouse writes for this sink. Each call obtains a fresh
        # client instance (backed by the global HTTP pool), but we still guard
        # the critical section to avoid any chance of per-session concurrency
        # issues in the driver.
        _ensure_table(table)
        with _DDL_LOCK:
            ch = get_client()
            cols = [
                "ts",
                "symbol",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trades",
                "atr_14",
                "rsi_14",
                "ema_20",
                "vwap",
                "orb5_hi",
                "orb5_lo",
                "orb5_frozen",
                "ver",
            ]
            ch.insert(table, buf_copy, column_names=cols)
        logger.debug("rfe_sink_flushed", rows=len(buf_copy))
    except Exception as e:
        logger.warning("rfe_sink_flush_failed", table=table, rows=len(buf_copy), error=str(e))


def enqueue_row(symbol: str, ts_us: int, feats: Dict[str, Any], table: str = "trading.features_1m_active") -> None:
    """Enqueue a row for micro-batched insert; flush by size/time thresholds."""
    try:
        row = _map_features(symbol, ts_us, feats)
        values = [
            row["ts"], row["symbol"], row["open"], row["high"], row["low"], row["close"], row["volume"], row["trades"],
            row["atr_14"], row["rsi_14"], row["ema_20"], row["vwap"], row["orb5_hi"], row["orb5_lo"], row["orb5_frozen"], row["ver"]
        ]
        do_flush = False
        now = time.time()
        with _BUF_LOCK:
            _BUF.append(values)
            if len(_BUF) >= _MAX_ROWS or (now - _LAST_FLUSH_TS) >= _MAX_SEC:
                do_flush = True
        if do_flush:
            _flush(table)
    except Exception as e:
        logger.warning("rfe_sink_enqueue_failed", table=table, symbol=symbol, error=str(e))


# Backward-compatible API: write_row_sync now enqueues to batch
def write_row_sync(symbol: str, ts_us: int, feats: Dict[str, Any], table: str = "trading.features_1m_active") -> None:
    enqueue_row(symbol, ts_us, feats, table)
