"""
Ledger Manager - Session-based ledger system for multi-LLM trading

This module provides:
- Independent session accounting for each LLM bot instance
- ClickHouse-based event logging (decisions, orders, fills, snapshots)
- Unified NY timezone for all timestamps
- Idempotent event recording

Usage:
    # Enable ledger
    ledger = LedgerManager(host="localhost", database="llm")
    ledger.ensure_tables()

    # Create session
    session_id = ledger.create_session(
        bot_id="DEEPSEEK_NVIDIA_STRATEGY_A",
        model="deepseek-ai/deepseek-r1",
        symbols=["TSLA", "AAPL"],
        mode="PAPER",
        initial_cash=50000.0,
        prompt_version_hash="v1",
        provider_config_hash="default",
    )

    # Log events
    ledger.log_decision(session_id=session_id, symbol="TSLA", decision_json={...})
    ledger.log_fill(session_id=session_id, order_id_internal="...", fill_qty=10, fill_price=400.0, ...)

    # Snapshots
    ledger.snapshot_position(session_id=session_id, positions=[...])
    ledger.snapshot_equity(session_id=session_id, equity=51000, cash=45000, ...)

    # Close session
    ledger.close_session(session_id=session_id, status="CLOSED", reason="EOD")
"""
from __future__ import annotations
import json
import uuid
from typing import Any, Dict, Iterable, Optional
from clickhouse_driver import Client as CHClient
from llm_trader.ny_time import now_ny, ny_trading_day, fmt_ny

DEFAULT_DB = "llm"  # Change to your preferred database name


def _new_id() -> str:
    """Generate a unique ID for events/fills/decisions"""
    return uuid.uuid4().hex


class NoopLedger:
    """
    No-op ledger for when ledger is disabled.
    Provides same interface with zero overhead.
    """
    def __init__(self, *a, **kw):
        pass

    def ensure_tables(self):
        pass

    def create_session(self, **kw) -> str:
        return kw.get("session_id") or _new_id()

    def log_decision(self, *a, **kw):
        pass

    def log_order_event(self, *a, **kw):
        pass

    def log_fill(self, *a, **kw):
        pass

    def snapshot_position(self, *a, **kw):
        pass

    def snapshot_equity(self, *a, **kw):
        pass

    def close_session(self, *a, **kw):
        pass


class LedgerManager:
    """
    Session-based ledger system with unified NY timezone.

    All timestamps use DateTime64(3, 'America/New_York').
    All trading_day values use toDate(ts_ny).

    This ensures consistency across:
    - Python code (ny_time.now_ny())
    - ClickHouse connection (SET timezone = 'America/New_York')
    - Table schemas (DateTime64(..., 'America/New_York'))
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9000,
        user: str = "default",
        password: str = "",
        database: str = DEFAULT_DB,
        settings: Optional[Dict[str, Any]] = None,
    ):
        self.db = database

        # First connect without database to create it if needed
        temp_client = CHClient(
            host=host,
            port=port,
            user=user,
            password=password,
            settings=(settings or {"allow_experimental_object_type": 1}),
        )
        temp_client.execute(f"CREATE DATABASE IF NOT EXISTS {database}")
        temp_client.disconnect()

        # Now connect with database
        self.client = CHClient(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            settings=(settings or {"allow_experimental_object_type": 1}),
        )
        # Note: timezone is already specified in DateTime64(..., 'America/New_York') column definitions

    # ========================== Table Creation ==========================

    def ensure_tables(self):
        """Create all ledger tables if they don't exist"""
        ddl = _DDL_TEMPLATE.replace("{DB}", self.db)
        for stmt in ddl.split(";"):
            s = stmt.strip()
            if s:
                self.client.execute(s)

    # ========================== Session Lifecycle ==========================

    def create_session(
        self,
        *,
        session_id: Optional[str] = None,
        bot_id: str,
        model: str,
        symbols: Iterable[str],
        mode: str,  # "PAPER" | "LIVE"
        initial_cash: float,
        prompt_version_hash: str,
        provider_config_hash: str,
        notes: str = "",
    ) -> str:
        """
        Create a new trading session.

        Returns:
            session_id: Unique identifier for this session
        """
        sid = session_id or _new_id()
        ts_ny = now_ny()  # Use datetime object for DateTime64 columns
        tday = ny_trading_day()
        sym_list = ",".join(symbols)

        # Insert session record
        self.client.execute(
            f"""
            INSERT INTO {self.db}.llm_sessions
            (session_id, bot_id, model, symbols, mode, initial_cash, status,
             start_ts_ny, trading_day, prompt_version_hash, provider_config_hash, notes)
            VALUES
            """,
            [
                {
                    "session_id": sid,
                    "bot_id": bot_id,
                    "model": model,
                    "symbols": sym_list,
                    "mode": mode,
                    "initial_cash": float(initial_cash),
                    "status": "RUNNING",
                    "start_ts_ny": ts_ny,
                    "trading_day": tday,
                    "prompt_version_hash": prompt_version_hash,
                    "provider_config_hash": provider_config_hash,
                    "notes": notes,
                }
            ],
        )

        # Log initial cash allocation
        self.client.execute(
            f"""
            INSERT INTO {self.db}.llm_cash_ledger
            (event_id, session_id, ts_ny, trading_day, type, amount, currency, note)
            VALUES
            """,
            [
                {
                    "event_id": _new_id(),
                    "session_id": sid,
                    "ts_ny": ts_ny,
                    "trading_day": tday,
                    "type": "ALLOCATE",
                    "amount": float(initial_cash),
                    "currency": "USD",
                    "note": "Initial allocation",
                }
            ],
        )

        return sid

    def close_session(self, *, session_id: str, status: str = "CLOSED", reason: str = ""):
        """Close a trading session"""
        ts_ny = now_ny()
        self.client.execute(
            f"""
            ALTER TABLE {self.db}.llm_sessions
            UPDATE status = %(status)s,
                   end_ts_ny = toDateTime64(%(ts)s, 3, 'America/New_York'),
                   close_reason = %(reason)s
            WHERE session_id = %(sid)s
            """,
            {"status": status, "ts": ts_ny, "reason": reason, "sid": session_id},
        )

    # ========================== Decision & Order Events ==========================

    def log_decision(
        self,
        *,
        session_id: str,
        symbol: str,
        decision_json: Dict[str, Any],
        prompt_digest: str,
        raw_text: str = "",
    ):
        """Log an LLM decision"""
        ts_ny = now_ny()
        tday = ny_trading_day()
        self.client.execute(
            f"""
            INSERT INTO {self.db}.llm_decisions
            (decision_id, session_id, ts_ny, trading_day, symbol,
             decision_json, prompt_digest, raw_text)
            VALUES
            """,
            [
                {
                    "decision_id": _new_id(),
                    "session_id": session_id,
                    "ts_ny": ts_ny,
                    "trading_day": tday,
                    "symbol": symbol,
                    "decision_json": json.dumps(decision_json),  # Serialize to JSON string
                    "prompt_digest": prompt_digest[:256],
                    "raw_text": raw_text[:4000],
                }
            ],
        )

    def log_order_event(
        self,
        *,
        session_id: str,
        order_id_internal: str,
        stage: str,  # created/submitted/filled/cancelled/rejected/...
        symbol: str,
        side: str,  # BUY/SELL
        qty: float,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
        tif: str = "DAY",
        meta: Optional[Dict[str, Any]] = None,
    ):
        """Log an order lifecycle event"""
        ts_ny = now_ny()
        tday = ny_trading_day()
        self.client.execute(
            f"""
            INSERT INTO {self.db}.llm_order_events
            (event_id, session_id, ts_ny, trading_day, order_id_internal, stage,
             symbol, side, qty, order_type, limit_price, tif, meta)
            VALUES
            """,
            [
                {
                    "event_id": _new_id(),
                    "session_id": session_id,
                    "ts_ny": ts_ny,
                    "trading_day": tday,
                    "order_id_internal": order_id_internal,
                    "stage": stage,
                    "symbol": symbol,
                    "side": side,
                    "qty": float(qty),
                    "order_type": order_type,
                    "limit_price": limit_price,
                    "tif": tif,
                    "meta": json.dumps(meta or {}),  # Serialize to JSON string
                }
            ],
        )

    def log_fill(
        self,
        *,
        session_id: str,
        order_id_internal: str,
        fill_qty: float,
        fill_price: float,
        symbol: str,
        fee: float = 0.0,
        source: str = "SIM",  # IBKR or SIM
    ):
        """Log a fill event and record commission"""
        ts_ny = now_ny()
        tday = ny_trading_day()

        # Log fill
        self.client.execute(
            f"""
            INSERT INTO {self.db}.llm_fills
            (fill_id, session_id, ts_ny, trading_day, order_id_internal,
             symbol, qty, price, fee, source)
            VALUES
            """,
            [
                {
                    "fill_id": _new_id(),
                    "session_id": session_id,
                    "ts_ny": ts_ny,
                    "trading_day": tday,
                    "order_id_internal": order_id_internal,
                    "symbol": symbol,
                    "qty": float(fill_qty),
                    "price": float(fill_price),
                    "fee": float(fee),
                    "source": source,
                }
            ],
        )

        # Log commission to cash ledger
        if fee and fee != 0.0:
            self.client.execute(
                f"""
                INSERT INTO {self.db}.llm_cash_ledger
                (event_id, session_id, ts_ny, trading_day, type, amount, currency, note)
                VALUES
                """,
                [
                    {
                        "event_id": _new_id(),
                        "session_id": session_id,
                        "ts_ny": ts_ny,
                        "trading_day": tday,
                        "type": "FEE",
                        "amount": -abs(float(fee)),
                        "currency": "USD",
                        "note": f"Commission for {order_id_internal}",
                    }
                ],
            )

    # ========================== Snapshots ==========================

    def snapshot_position(
        self,
        *,
        session_id: str,
        positions: Iterable[Dict[str, Any]],
    ):
        """
        Take a snapshot of current positions.

        Expected position format:
        {
            "symbol": str,
            "position": float,  # positive for long, negative for short
            "avg_cost": float,
            "market_price": float,
            "unrealized_pnl": float,
            "realized_pnl_to_date": float,
        }
        """
        ts_ny = now_ny()
        tday = ny_trading_day()
        rows = []

        for p in positions or []:
            rows.append(
                {
                    "session_id": session_id,
                    "ts_ny": ts_ny,
                    "trading_day": tday,
                    "symbol": p.get("symbol"),
                    "position": float(p.get("position", 0.0)),
                    "avg_cost": float(p.get("avg_cost", 0.0) or 0.0),
                    "market_price": float(p.get("market_price", 0.0) or 0.0),
                    "unrealized_pnl": float(p.get("unrealized_pnl", 0.0) or 0.0),
                    "realized_pnl_to_date": float(p.get("realized_pnl_to_date", 0.0) or 0.0),
                }
            )

        if rows:
            self.client.execute(
                f"""
                INSERT INTO {self.db}.llm_positions_snapshot
                (session_id, ts_ny, trading_day, symbol, position, avg_cost,
                 market_price, unrealized_pnl, realized_pnl_to_date)
                VALUES
                """,
                rows,
            )

    def snapshot_equity(
        self,
        *,
        session_id: str,
        equity: float,
        cash: float,
        gross_exposure: float,
        net_exposure: float,
        drawdown_to_date: float = 0.0,
    ):
        """Take a snapshot of portfolio equity"""
        ts_ny = now_ny()
        tday = ny_trading_day()
        self.client.execute(
            f"""
            INSERT INTO {self.db}.llm_equity_timeseries
            (session_id, ts_ny, trading_day, equity, cash,
             gross_exposure, net_exposure, drawdown_to_date)
            VALUES
            """,
            [
                {
                    "session_id": session_id,
                    "ts_ny": ts_ny,
                    "trading_day": tday,
                    "equity": float(equity),
                    "cash": float(cash),
                    "gross_exposure": float(gross_exposure),
                    "net_exposure": float(net_exposure),
                    "drawdown_to_date": float(drawdown_to_date),
                }
            ],
        )


# ========================== ClickHouse DDL ==========================

_DDL_TEMPLATE = """
CREATE DATABASE IF NOT EXISTS {DB};

CREATE TABLE IF NOT EXISTS {DB}.llm_sessions
(
    session_id String,
    bot_id String,
    model String,
    symbols String,
    mode LowCardinality(String),           -- PAPER/LIVE
    initial_cash Float64,
    status LowCardinality(String),         -- RUNNING/CLOSED/ABORTED
    start_ts_ny DateTime64(3, 'America/New_York'),
    end_ts_ny   Nullable(DateTime64(3, 'America/New_York')),
    trading_day Date,
    prompt_version_hash String,
    provider_config_hash String,
    close_reason String,
    notes String
)
ENGINE = ReplacingMergeTree
PARTITION BY trading_day
ORDER BY (session_id, start_ts_ny)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS {DB}.llm_cash_ledger
(
    event_id String,
    session_id String,
    ts_ny DateTime64(3, 'America/New_York'),
    trading_day Date,
    type LowCardinality(String),          -- ALLOCATE / FEE / REALIZED_PNL / ADJUSTMENT
    amount Float64,
    currency LowCardinality(String),
    note String
)
ENGINE = ReplacingMergeTree
PARTITION BY trading_day
ORDER BY (session_id, ts_ny, event_id);

CREATE TABLE IF NOT EXISTS {DB}.llm_order_events
(
    event_id String,
    session_id String,
    ts_ny DateTime64(3, 'America/New_York'),
    trading_day Date,
    order_id_internal String,
    stage LowCardinality(String),         -- created/submitted/filled/cancelled/rejected
    symbol String,
    side LowCardinality(String),          -- BUY/SELL
    qty Float64,
    order_type LowCardinality(String),
    limit_price Nullable(Float64),
    tif LowCardinality(String),
    meta String  -- Changed from JSON to String for compatibility
)
ENGINE = ReplacingMergeTree
PARTITION BY trading_day
ORDER BY (session_id, ts_ny, order_id_internal, event_id);

CREATE TABLE IF NOT EXISTS {DB}.llm_fills
(
    fill_id String,
    session_id String,
    ts_ny DateTime64(3, 'America/New_York'),
    trading_day Date,
    order_id_internal String,
    symbol String,
    qty Float64,
    price Float64,
    fee Float64,
    source LowCardinality(String)         -- SIM / IBKR
)
ENGINE = ReplacingMergeTree
PARTITION BY trading_day
ORDER BY (session_id, ts_ny, fill_id);

CREATE TABLE IF NOT EXISTS {DB}.llm_positions_snapshot
(
    session_id String,
    ts_ny DateTime64(3, 'America/New_York'),
    trading_day Date,
    symbol String,
    position Float64,
    avg_cost Float64,
    market_price Float64,
    unrealized_pnl Float64,
    realized_pnl_to_date Float64
)
ENGINE = ReplacingMergeTree
PARTITION BY trading_day
ORDER BY (session_id, ts_ny, symbol);

CREATE TABLE IF NOT EXISTS {DB}.llm_equity_timeseries
(
    session_id String,
    ts_ny DateTime64(3, 'America/New_York'),
    trading_day Date,
    equity Float64,
    cash Float64,
    gross_exposure Float64,
    net_exposure Float64,
    drawdown_to_date Float64
)
ENGINE = ReplacingMergeTree
PARTITION BY trading_day
ORDER BY (session_id, ts_ny);

CREATE TABLE IF NOT EXISTS {DB}.llm_decisions
(
    decision_id String,
    session_id String,
    ts_ny DateTime64(3, 'America/New_York'),
    trading_day Date,
    symbol String,
    decision_json String,  -- Changed from JSON to String for compatibility
    prompt_digest String,
    raw_text String
)
ENGINE = ReplacingMergeTree
PARTITION BY trading_day
ORDER BY (session_id, ts_ny, decision_id);

CREATE VIEW IF NOT EXISTS {DB}.llm_daily_pnl_view AS
SELECT
    trading_day,
    session_id,
    sumIf(qty * price, qty < 0) AS gross_sell,
    sumIf(qty * price, qty > 0) AS gross_buy,
    sum(fee) AS fees,
    count() AS fills
FROM {DB}.llm_fills
GROUP BY trading_day, session_id;
"""
