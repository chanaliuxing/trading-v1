#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ib_insync import IB, LimitOrder, MarketOrder, Stock

from libs.config import config
from libs.db.postgres import PostgresDB
from libs.options.internal_symbol import (
    ib_option_contract_from_internal_symbol,
    internal_opt_symbol_from_ib_contract,
    is_internal_opt_symbol,
)
from services.risk.reservation_service import ReservationService
from services.orders.ledger import PositionLedger
from strategies.stocks_kernel.runtime.audit import LocalAuditWriter
from strategies.stocks_kernel.types import KernelExecutionEvent
from tool.reset_qqqv1_daylock import reset_daylock

ET = ZoneInfo("America/New_York")
TERMINAL_ORDER_STATES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y"}


def _adhoc_ib_connect_allowed(args: argparse.Namespace) -> bool:
    return not _env_truthy("IBKR_DISABLE_ADHOC_CONNECT")


def _acquire_adhoc_ib_connect_guard(cooldown_sec: float) -> Any:
    lock_path = Path(os.getenv("IBKR_ADHOC_LOCK_PATH", "/tmp/trading_ibkr_adhoc_connect.lock"))
    state_path = Path(os.getenv("IBKR_ADHOC_STATE_PATH", "/tmp/trading_ibkr_adhoc_connect.last"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path.open("a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            last_ts = float(state_path.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            last_ts = 0.0
        remaining = float(cooldown_sec or 0.0) - (time.time() - last_ts)
        if remaining > 0:
            raise RuntimeError(f"IBKR ad-hoc connection cooldown active; wait {remaining:.0f}s.")
        return {"lock_fh": lock_fh, "state_path": state_path}
    except Exception:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()
        raise


def _release_adhoc_ib_connect_guard(guard: Any) -> None:
    if not guard:
        return
    try:
        Path(guard["state_path"]).write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
    lock_fh = guard.get("lock_fh")
    try:
        if lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        if lock_fh:
            lock_fh.close()
    except Exception:
        pass


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class BrokerResidualPosition:
    symbol: str
    qty: float


@dataclass(frozen=True)
class DbResidualPosition:
    strategy_id: str
    symbol: str
    pos_qty: float


@dataclass(frozen=True)
class DbResidualOrder:
    order_id: str
    strategy_id: str
    symbol: str
    side: str
    qty: float
    state: str
    broker_order_id: str | None = None
    order_ref: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class BrokerCleanupOrder:
    symbol: str
    action: str
    qty: float
    session: str
    exchange: str
    tif: str
    outside_rth: bool = False


def build_broker_cleanup_orders(
    *,
    broker_positions: Sequence[BrokerResidualPosition],
    session: str,
) -> list[BrokerCleanupOrder]:
    session_u = str(session or "rth").strip().lower()
    exchange = "OVERNIGHT" if session_u == "overnight" else "SMART"
    tif = "DAY"
    outside_rth = session_u in {"afterhours", "overnight"}
    orders: list[BrokerCleanupOrder] = []
    for row in broker_positions:
        qty = float(row.qty or 0.0)
        if abs(qty) <= 1e-12:
            continue
        action = "SELL" if qty > 0 else "BUY"
        orders.append(
            BrokerCleanupOrder(
                symbol=str(row.symbol).strip().upper(),
                action=action,
                qty=abs(qty),
                session=session_u,
                exchange=exchange,
                tif=tif,
                outside_rth=outside_rth,
            )
        )
    return orders


def _extract_reference_price(ticker: Any) -> float | None:
    candidates = [
        getattr(ticker, "marketPrice", lambda: None)(),
        getattr(ticker, "last", None),
        getattr(ticker, "close", None),
    ]
    bid = getattr(ticker, "bid", None)
    ask = getattr(ticker, "ask", None)
    if bid not in (None, -1) and ask not in (None, -1) and bid > 0 and ask > 0:
        candidates.append((float(bid) + float(ask)) / 2.0)
    for value in candidates:
        try:
            px = float(value)
        except Exception:
            continue
        if px > 0:
            return px
    return None


def _aggressive_limit_price(*, action: str, reference_px: float, session: str) -> float:
    session_u = str(session or "rth").strip().lower()
    bps = 1000 if session_u == "overnight" else 100
    mult = 1.0 + (bps / 10000.0) if str(action or "").strip().upper() == "BUY" else 1.0 - (bps / 10000.0)
    px = float(reference_px) * mult
    return round(max(px, 0.01), 2)


def build_db_position_cleanup_plan(
    *,
    db_positions: Sequence[DbResidualPosition],
    symbols: Sequence[str] | None = None,
    strategy_ids: Sequence[str] | None = None,
) -> list[DbResidualPosition]:
    wanted_symbols = {str(item).strip().upper() for item in (symbols or []) if str(item).strip()}
    allowed = {str(item).strip() for item in (strategy_ids or []) if str(item).strip()}
    plan = []
    for row in db_positions:
        symbol_u = str(row.symbol or "").strip().upper()
        if wanted_symbols and symbol_u in wanted_symbols:
            pass
        elif allowed and row.strategy_id in allowed:
            pass
        elif wanted_symbols or allowed:
            continue
        if abs(float(row.pos_qty or 0.0)) <= 1e-12:
            continue
        plan.append(row)
    return plan


def build_explicit_position_cleanup_plan(
    *,
    symbols: Sequence[str] | None = None,
    strategy_ids: Sequence[str] | None = None,
    existing_rows: Sequence[DbResidualPosition] | None = None,
) -> list[DbResidualPosition]:
    """Return target strategy/symbol rows even when core_position is already flat."""
    wanted_symbols = sorted({str(item).strip().upper() for item in (symbols or []) if str(item).strip()})
    wanted_strategies = sorted({str(item).strip() for item in (strategy_ids or []) if str(item).strip()})
    rows: list[DbResidualPosition] = []
    seen: set[tuple[str, str]] = set()
    for row in existing_rows or []:
        key = (str(row.strategy_id or "").strip(), str(row.symbol or "").strip().upper())
        if key[0] and key[1]:
            rows.append(row)
            seen.add(key)
    if wanted_symbols and wanted_strategies:
        for strategy_id in wanted_strategies:
            for symbol in wanted_symbols:
                key = (strategy_id, symbol)
                if key not in seen:
                    rows.append(DbResidualPosition(strategy_id=strategy_id, symbol=symbol, pos_qty=0.0))
                    seen.add(key)
    return rows


def build_db_order_cleanup_plan(
    *,
    db_orders: Sequence[DbResidualOrder],
    symbols: Sequence[str] | None = None,
    strategy_ids: Sequence[str] | None = None,
) -> list[DbResidualOrder]:
    wanted_symbols = {str(item).strip().upper() for item in (symbols or []) if str(item).strip()}
    allowed = {str(item).strip() for item in (strategy_ids or []) if str(item).strip()}
    plan = []
    for row in db_orders:
        symbol_u = str(row.symbol or "").strip().upper()
        if wanted_symbols and symbol_u in wanted_symbols:
            pass
        elif allowed and row.strategy_id in allowed:
            pass
        elif wanted_symbols or allowed:
            continue
        if str(row.state or "").strip().upper() in TERMINAL_ORDER_STATES:
            continue
        plan.append(row)
    return plan


def summarize_cleanup_state(
    *,
    broker_positions: Sequence[BrokerResidualPosition],
    broker_open_order_count: int,
    db_positions: Sequence[DbResidualPosition],
    db_orders: Sequence[DbResidualOrder],
) -> dict[str, Any]:
    return {
        "broker_positions": [asdict(item) for item in broker_positions if abs(float(item.qty or 0.0)) > 1e-12],
        "broker_open_order_count": int(broker_open_order_count),
        "db_positions": [asdict(item) for item in db_positions if abs(float(item.pos_qty or 0.0)) > 1e-12],
        "db_orders": [asdict(item) for item in db_orders if str(item.state or "").strip().upper() not in TERMINAL_ORDER_STATES],
        "clean": (
            broker_open_order_count == 0
            and not any(abs(float(item.qty or 0.0)) > 1e-12 for item in broker_positions)
            and not any(abs(float(item.pos_qty or 0.0)) > 1e-12 for item in db_positions)
            and not any(str(item.state or "").strip().upper() not in TERMINAL_ORDER_STATES for item in db_orders)
        ),
    }


def write_cleanup_audit(
    *,
    audit_root: str | Path,
    strategy_id: str | None,
    stage: str,
    reason: str,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> None:
    safe_payload = _json_safe(dict(payload or {}))
    writer = LocalAuditWriter(root_dir=audit_root)
    writer.write_event(
        domain="recon",
        now=now or datetime.now(ET),
        event=KernelExecutionEvent(
            stage=stage,
            strategy_id=strategy_id,
            strategy_run_id=None,
            symbol="SYSTEM",
            domain="recon",
            component="cleanup_tool",
            status="observed",
            reason=reason,
            payload=safe_payload,
        ),
    )


async def fetch_db_residuals(*, pg: PostgresDB, symbols: Sequence[str], strategy_ids: Sequence[str]) -> tuple[list[DbResidualPosition], list[DbResidualOrder]]:
    position_rows = await pg.fetchall(
        """
        SELECT strategy_id, symbol, pos_qty
        FROM core.core_position
        WHERE (symbol = ANY($1::text[]) OR strategy_id = ANY($2::text[]))
          AND ABS(COALESCE(pos_qty, 0)) > 0
        ORDER BY strategy_id, symbol
        """,
        list(symbols),
        list(strategy_ids),
    )
    order_rows = await pg.fetchall(
        """
        SELECT order_id::text AS order_id, strategy_id, symbol, side, qty, state,
               broker_order_id::text AS broker_order_id,
               order_ref::text AS order_ref
             , idempotency_key::text AS idempotency_key
        FROM core.core_order
        WHERE (symbol = ANY($1::text[]) OR strategy_id = ANY($2::text[]))
          AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
        ORDER BY strategy_id, symbol, order_id
        """,
        list(symbols),
        list(strategy_ids),
    )
    return (
        [DbResidualPosition(**row) for row in position_rows],
        [DbResidualOrder(**row) for row in order_rows],
    )


async def terminalize_db_orders(*, pg: PostgresDB, rows: Sequence[DbResidualOrder], reason: str) -> int:
    count = 0
    note = json.dumps({"cleanup_reason": reason, "ts": datetime.now(ET).isoformat()})
    for row in rows:
        await pg.execute(
            """
            UPDATE core.core_order
            SET state = 'CANCELLED', note = $2, updated_at = now()
            WHERE order_id = $1
            """,
            row.order_id,
            note,
        )
        count += 1
    return count


async def release_db_order_reservations(
    *,
    pg: PostgresDB,
    rows: Sequence[DbResidualOrder],
    terminal_state: str,
    reason: str,
) -> dict[str, Any]:
    service = ReservationService(db=pg)
    released_count = 0
    released_usd = 0.0
    attempted = 0
    for row in rows:
        idem = str(row.idempotency_key or "").strip()
        if not idem:
            continue
        attempted += 1
        released, amount = await service.release_on_terminal(
            idempotency_key=idem,
            order_id=str(row.order_id),
            terminal_state=str(terminal_state or "CANCELLED"),
            reason=reason,
        )
        if released:
            released_count += 1
            released_usd += float(amount or 0.0)
    return {
        "attempted": attempted,
        "released_count": released_count,
        "released_usd": round(released_usd, 2),
    }


async def zero_db_positions(*, pg: PostgresDB, rows: Sequence[DbResidualPosition]) -> int:
    count = 0
    for row in rows:
        await pg.execute(
            """
            UPDATE core.core_position
            SET pos_qty = 0, avg_px = 0, updated_at = now()
            WHERE strategy_id = $1 AND symbol = $2
            """,
            row.strategy_id,
            row.symbol,
        )
        count += 1
    return count


async def converge_position_ledgers_flat(*, pg: PostgresDB, rows: Sequence[DbResidualPosition]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    targets = sorted({(str(row.strategy_id or "").strip(), str(row.symbol or "").strip().upper()) for row in rows})
    for strategy_id, symbol in targets:
        if not strategy_id or not symbol:
            continue
        ledger_rows = await pg.fetchall(
            """
            SELECT account_id, COALESCE(SUM(qty_delta), 0) AS qty
            FROM core.position_ledger_event
            WHERE strategy_id = $1 AND symbol = $2
            GROUP BY account_id
            HAVING ABS(COALESCE(SUM(qty_delta), 0)) > 1e-9
            ORDER BY account_id
            """,
            strategy_id,
            symbol,
        )
        for ledger_row in ledger_rows:
            account_id = str(ledger_row.get("account_id") or "").strip() or "UNKNOWN_ACCOUNT"
            qty = float(ledger_row.get("qty") or 0.0)
            if abs(qty) <= 1e-9:
                continue
            idempotency_key = (
                f"residual_cleanup_flat:{account_id}:{strategy_id}:{symbol}:{round(qty, 8)}"
            )
            ledger = PositionLedger(pg=pg, account_id=account_id)
            inserted = await ledger.append_adjustment_event(
                strategy_id=strategy_id,
                symbol=symbol,
                qty_delta=-qty,
                event_ts=datetime.now(ET),
                idempotency_key=idempotency_key,
                metadata={
                    "reason": "residual_cleanup_broker_verified_flat",
                    "previous_net_qty": qty,
                },
                source="residual_cleanup_tool",
            )
            results.append(
                {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "account_id": account_id,
                    "previous_net_qty": qty,
                    "adjustment_qty": -qty,
                    "inserted": bool(inserted),
                    "idempotency_key": idempotency_key,
                }
            )
    return results


async def converge_invalid_position_ledger_groups_flat(
    *,
    pg: PostgresDB,
    rows: Sequence[DbResidualPosition],
) -> list[dict[str, Any]]:
    """Flatten invalid account/lot ledger groups after broker truth proves flat.

    Account-level net can be zero while UNKNOWN_ACCOUNT/null-lot groups remain nonzero.
    Production readiness treats those groups as unsafe because they can later corrupt
    ownership and average-price derivation, so cleanup must converge them explicitly.
    """

    results: list[dict[str, Any]] = []
    targets = sorted({(str(row.strategy_id or "").strip(), str(row.symbol or "").strip().upper()) for row in rows})
    for strategy_id, symbol in targets:
        if not strategy_id or not symbol:
            continue
        ledger_rows = await pg.fetchall(
            """
            SELECT COALESCE(account_id, '') AS account_id,
                   COALESCE(lot_id, '') AS lot_id,
                   COALESCE(SUM(qty_delta), 0) AS qty
            FROM core.position_ledger_event
            WHERE strategy_id = $1 AND symbol = $2
            GROUP BY COALESCE(account_id, ''), COALESCE(lot_id, '')
            HAVING ABS(COALESCE(SUM(qty_delta), 0)) > 1e-9
               AND (COALESCE(account_id, '') = '' OR COALESCE(account_id, '') = 'UNKNOWN_ACCOUNT' OR COALESCE(lot_id, '') = '')
            ORDER BY account_id, lot_id
            """,
            strategy_id,
            symbol,
        )
        for ledger_row in ledger_rows:
            account_id = str(ledger_row.get("account_id") or "").strip() or "UNKNOWN_ACCOUNT"
            lot_id = str(ledger_row.get("lot_id") or "").strip()
            qty = float(ledger_row.get("qty") or 0.0)
            if abs(qty) <= 1e-9:
                continue
            lot_key = lot_id or "NULL_LOT"
            idempotency_key = (
                f"residual_cleanup_invalid_ledger_group:{account_id}:{strategy_id}:{symbol}:{lot_key}:{round(qty, 8)}"
            )
            ledger = PositionLedger(pg=pg, account_id=account_id)
            inserted = await ledger.append_adjustment_event(
                strategy_id=strategy_id,
                symbol=symbol,
                lot_id=lot_id or None,
                qty_delta=-qty,
                event_ts=datetime.now(ET),
                idempotency_key=idempotency_key,
                metadata={
                    "reason": "residual_cleanup_invalid_ledger_group_broker_verified_flat",
                    "previous_net_qty": qty,
                    "previous_account_id": account_id,
                    "previous_lot_id": lot_id or None,
                },
                source="residual_cleanup_tool",
            )
            results.append(
                {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "account_id": account_id,
                    "lot_id": lot_id or None,
                    "previous_net_qty": qty,
                    "adjustment_qty": -qty,
                    "inserted": bool(inserted),
                    "idempotency_key": idempotency_key,
                }
            )
    return results


async def converge_position_ledger_lot_groups_flat(
    *,
    pg: PostgresDB,
    rows: Sequence[DbResidualPosition],
) -> list[dict[str, Any]]:
    """Flatten every nonzero ledger lot group after broker truth proves flat."""

    results: list[dict[str, Any]] = []
    targets = sorted({(str(row.strategy_id or "").strip(), str(row.symbol or "").strip().upper()) for row in rows})
    for strategy_id, symbol in targets:
        if not strategy_id or not symbol:
            continue
        ledger_rows = await pg.fetchall(
            """
            SELECT COALESCE(account_id, '') AS account_id,
                   COALESCE(lot_id, '') AS lot_id,
                   COALESCE(SUM(qty_delta), 0) AS qty
            FROM core.position_ledger_event
            WHERE strategy_id = $1 AND symbol = $2
            GROUP BY COALESCE(account_id, ''), COALESCE(lot_id, '')
            HAVING ABS(COALESCE(SUM(qty_delta), 0)) > 1e-9
            ORDER BY account_id, lot_id
            """,
            strategy_id,
            symbol,
        )
        for ledger_row in ledger_rows:
            account_id = str(ledger_row.get("account_id") or "").strip() or "UNKNOWN_ACCOUNT"
            lot_id = str(ledger_row.get("lot_id") or "").strip()
            qty = float(ledger_row.get("qty") or 0.0)
            if abs(qty) <= 1e-9:
                continue
            lot_key = lot_id or "NULL_LOT"
            idempotency_key = (
                f"residual_cleanup_lot_flat:{account_id}:{strategy_id}:{symbol}:{lot_key}:{round(qty, 8)}"
            )
            ledger = PositionLedger(pg=pg, account_id=account_id)
            inserted = await ledger.append_adjustment_event(
                strategy_id=strategy_id,
                symbol=symbol,
                lot_id=lot_id or None,
                qty_delta=-qty,
                event_ts=datetime.now(ET),
                idempotency_key=idempotency_key,
                metadata={
                    "reason": "residual_cleanup_lot_group_broker_verified_flat",
                    "previous_net_qty": qty,
                    "previous_account_id": account_id,
                    "previous_lot_id": lot_id or None,
                },
                source="residual_cleanup_tool",
            )
            results.append(
                {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "account_id": account_id,
                    "lot_id": lot_id or None,
                    "previous_net_qty": qty,
                    "adjustment_qty": -qty,
                    "inserted": bool(inserted),
                    "idempotency_key": idempotency_key,
                }
            )
    return results


async def clear_manual_locks_for_targets(*, pg: PostgresDB, rows: Sequence[DbResidualPosition]) -> int:
    count = 0
    targets = sorted({(str(row.strategy_id or "").strip(), str(row.symbol or "").strip().upper()) for row in rows})
    for strategy_id, symbol in targets:
        if not strategy_id or not symbol:
            continue
        existing = await pg.fetchall(
            """
            SELECT 1
            FROM core.manual_intervention_lock
            WHERE strategy_id = $1 AND symbol = $2
            """,
            strategy_id,
            symbol,
        )
        if not existing:
            continue
        await pg.execute(
            """
            DELETE FROM core.manual_intervention_lock
            WHERE strategy_id = $1 AND symbol = $2
            """,
            strategy_id,
            symbol,
        )
        count += len(existing)
    return count


async def terminalize_protection_registry_for_targets(
    *,
    pg: PostgresDB,
    rows: Sequence[DbResidualPosition],
) -> int:
    count = 0
    targets = sorted({(str(row.strategy_id or "").strip(), str(row.symbol or "").strip().upper()) for row in rows})
    for strategy_id, symbol in targets:
        if not strategy_id or not symbol:
            continue
        existing = await pg.fetchall(
            """
            SELECT 1
            FROM core.core_protection_order_registry
            WHERE strategy_id = $1
              AND symbol = $2
              AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
            """,
            strategy_id,
            symbol,
        )
        if not existing:
            continue
        await pg.execute(
            """
            UPDATE core.core_protection_order_registry
            SET state = 'CANCELLED',
                degraded_reason = 'residual_cleanup_broker_verified_flat',
                last_broker_sync_at = now(),
                updated_at = now()
            WHERE strategy_id = $1
              AND symbol = $2
              AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
            """,
            strategy_id,
            symbol,
        )
        count += len(existing)
    return count


async def terminalize_protection_registry_for_orders(
    *,
    pg: PostgresDB,
    rows: Sequence[DbResidualOrder],
) -> int:
    count = 0
    for row in rows:
        broker_order_id = str(row.broker_order_id or "").strip()
        order_ref = str(row.order_ref or "").strip()
        strategy_id = str(row.strategy_id or "").strip()
        symbol = str(row.symbol or "").strip().upper()
        if not broker_order_id and not order_ref:
            continue
        existing = await pg.fetchall(
            """
            SELECT 1
            FROM core.core_protection_order_registry
            WHERE strategy_id = $1
              AND symbol = $2
              AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
              AND (
                    ($3::text <> '' AND broker_order_id::text = $3::text)
                 OR ($4::text <> '' AND order_ref::text = $4::text)
              )
            """,
            strategy_id,
            symbol,
            broker_order_id,
            order_ref,
        )
        if not existing:
            continue
        await pg.execute(
            """
            UPDATE core.core_protection_order_registry
            SET state = 'CANCELLED',
                degraded_reason = 'residual_cleanup_broker_verified_absent_order',
                last_broker_sync_at = now(),
                updated_at = now()
            WHERE strategy_id = $1
              AND symbol = $2
              AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
              AND (
                    ($3::text <> '' AND broker_order_id::text = $3::text)
                 OR ($4::text <> '' AND order_ref::text = $4::text)
              )
            """,
            strategy_id,
            symbol,
            broker_order_id,
            order_ref,
        )
        count += len(existing)
    return count


async def fetch_broker_positions(*, ib: IB, symbols: Sequence[str]) -> list[BrokerResidualPosition]:
    wanted = {str(item).strip().upper() for item in symbols if str(item).strip()}
    rows: list[BrokerResidualPosition] = []
    for pos in ib.positions():
        contract = getattr(pos, "contract", None)
        symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
        mapped = internal_opt_symbol_from_ib_contract(contract)
        if mapped:
            symbol = mapped
        if wanted and symbol not in wanted:
            continue
        qty = float(getattr(pos, "position", 0.0) or 0.0)
        rows.append(BrokerResidualPosition(symbol=symbol, qty=qty))
    rows.sort(key=lambda item: item.symbol)
    return rows


async def count_broker_open_orders(*, ib: IB, symbols: Sequence[str]) -> int:
    return len(await list_broker_open_orders(ib=ib, symbols=symbols))


async def list_broker_open_orders(*, ib: IB, symbols: Sequence[str]) -> list[dict[str, Any]]:
    wanted = {str(item).strip().upper() for item in symbols if str(item).strip()}
    if hasattr(ib, "reqAllOpenOrdersAsync"):
        await ib.reqAllOpenOrdersAsync()
    elif hasattr(ib, "reqOpenOrdersAsync"):
        await ib.reqOpenOrdersAsync()
    else:
        ib.reqAllOpenOrders()
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.2)
    rows: list[dict[str, Any]] = []
    for trade in ib.openTrades():
        contract = getattr(trade, "contract", None)
        symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
        mapped = internal_opt_symbol_from_ib_contract(contract)
        if mapped:
            symbol = mapped
        if wanted and symbol not in wanted:
            continue
        order = getattr(trade, "order", None)
        status = getattr(trade, "orderStatus", None)
        rows.append(
            {
                "symbol": symbol,
                "broker_order_id": str(getattr(order, "orderId", "") or getattr(status, "orderId", "") or "").strip(),
                "perm_id": str(getattr(order, "permId", "") or getattr(status, "permId", "") or "").strip(),
                "order_ref": str(getattr(order, "orderRef", "") or "").strip(),
                "side": str(getattr(order, "action", "") or "").strip().upper(),
                "qty": float(getattr(order, "totalQuantity", 0.0) or 0.0),
                "status": str(getattr(status, "status", "") or "").strip(),
            }
        )
    return rows


def _broker_order_matches_db_order(broker_order: dict[str, Any], db_order: DbResidualOrder) -> bool:
    broker_symbol = str(broker_order.get("symbol") or "").strip().upper()
    db_symbol = str(db_order.symbol or "").strip().upper()
    if broker_symbol and db_symbol and broker_symbol != db_symbol:
        return False
    broker_id = str(broker_order.get("broker_order_id") or "").strip()
    db_broker_id = str(db_order.broker_order_id or "").strip()
    if db_broker_id and broker_id == db_broker_id:
        return True
    broker_ref = str(broker_order.get("order_ref") or "").strip()
    db_ref = str(db_order.order_ref or "").strip()
    if db_ref and broker_ref == db_ref:
        return True
    return False


async def verify_broker_absent_for_db_orders(
    *,
    ib: IB,
    symbols: Sequence[str],
    rows: Sequence[DbResidualOrder],
) -> dict[str, Any]:
    if not rows:
        return {"clean": True, "open_matches": [], "broker_open_order_count": 0}
    broker_orders = await list_broker_open_orders(ib=ib, symbols=symbols)
    open_matches: list[dict[str, Any]] = []
    for row in rows:
        for broker_order in broker_orders:
            if _broker_order_matches_db_order(broker_order, row):
                open_matches.append({"db_order": asdict(row), "broker_order": broker_order})
    return {
        "clean": len(open_matches) == 0,
        "open_matches": open_matches,
        "broker_open_order_count": len(broker_orders),
    }


async def cancel_broker_orders(
    *,
    ib: IB,
    symbols: Sequence[str],
    global_cancel: bool,
) -> dict[str, Any]:
    """
    Cancel broker open orders before flattening.

    Safety: by default we only cancel orders for the specified symbols.
    `reqGlobalCancel()` is a last resort and must be explicitly enabled.
    """
    if global_cancel:
        ib.reqGlobalCancel()
        await asyncio.sleep(1.0)
        return {"mode": "global", "requested": True}

    if hasattr(ib, "reqAllOpenOrdersAsync"):
        await ib.reqAllOpenOrdersAsync()
    elif hasattr(ib, "reqOpenOrdersAsync"):
        await ib.reqOpenOrdersAsync()
    else:
        ib.reqAllOpenOrders()
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.2)

    wanted = {str(item).strip().upper() for item in symbols if str(item).strip()}
    cancelled = 0
    for trade in list(ib.openTrades() or []):
        contract = getattr(trade, "contract", None)
        symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
        mapped = internal_opt_symbol_from_ib_contract(contract)
        if mapped:
            symbol = mapped
        if wanted and symbol not in wanted:
            continue
        order = getattr(trade, "order", None)
        if order is None:
            continue
        try:
            ib.cancelOrder(order)
            cancelled += 1
        except Exception:
            # Best-effort: flatten will still bring account back to flat.
            pass
    await asyncio.sleep(0.5)
    return {"mode": "symbol_filtered", "requested": True, "cancelled": cancelled, "symbols": sorted(wanted)}


async def flatten_broker_residuals(*, ib: IB, orders: Sequence[BrokerCleanupOrder], account: str | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in orders:
        if is_internal_opt_symbol(item.symbol):
            contract = ib_option_contract_from_internal_symbol(item.symbol)
            if hasattr(contract, "exchange"):
                contract.exchange = "SMART"
        else:
            contract = Stock(item.symbol, item.exchange, "USD")
        if item.session == "rth":
            order = MarketOrder(item.action, float(item.qty))
        else:
            ticker = None
            if hasattr(ib, "reqTickersAsync"):
                tickers = await ib.reqTickersAsync(contract)
                ticker = tickers[0] if tickers else None
            ref_px = _extract_reference_price(ticker) if ticker is not None else None
            if ref_px is None:
                raise RuntimeError(f"missing_cleanup_reference_price:{item.symbol}:{item.session}")
            order = LimitOrder(
                item.action,
                float(item.qty),
                _aggressive_limit_price(action=item.action, reference_px=ref_px, session=item.session),
            )
        order.tif = item.tif
        if hasattr(order, "outsideRth"):
            order.outsideRth = bool(item.outside_rth)
        if account:
            order.account = account
        trade = ib.placeOrder(contract, order)
        await asyncio.sleep(0.5)
        results.append(
            {
                "symbol": item.symbol,
                "action": item.action,
                "qty": item.qty,
                "exchange": item.exchange,
                "tif": item.tif,
                "outside_rth": item.outside_rth,
                "order_type": str(getattr(order, "orderType", "") or ""),
                "limit_px": float(getattr(order, "lmtPrice", 0.0) or 0.0) or None,
                "order_status": str(getattr(getattr(trade, "orderStatus", None), "status", "") or ""),
            }
        )
    return results


async def wait_for_broker_clean(
    *,
    ib: IB,
    symbols: Sequence[str],
    timeout_sec: float,
    poll_sec: float,
) -> dict[str, Any]:
    deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
    latest_positions: list[BrokerResidualPosition] = []
    latest_open_order_count = 0
    attempts = 0
    while True:
        attempts += 1
        latest_positions = await fetch_broker_positions(ib=ib, symbols=symbols)
        latest_open_order_count = await count_broker_open_orders(ib=ib, symbols=symbols)
        has_position = any(abs(float(item.qty or 0.0)) > 1e-12 for item in latest_positions)
        clean = latest_open_order_count == 0 and not has_position
        if clean:
            return {
                "clean": True,
                "attempts": attempts,
                "broker_positions": [asdict(item) for item in latest_positions if abs(float(item.qty or 0.0)) > 1e-12],
                "broker_open_order_count": int(latest_open_order_count),
            }
        if time.time() >= deadline:
            return {
                "clean": False,
                "attempts": attempts,
                "broker_positions": [asdict(item) for item in latest_positions if abs(float(item.qty or 0.0)) > 1e-12],
                "broker_open_order_count": int(latest_open_order_count),
            }
        await asyncio.sleep(max(0.2, float(poll_sec or 1.0)))


async def wait_for_broker_open_orders_absent(
    *,
    ib: IB,
    symbols: Sequence[str],
    timeout_sec: float,
    poll_sec: float,
) -> dict[str, Any]:
    deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
    latest_open_order_count = 0
    attempts = 0
    while True:
        attempts += 1
        latest_open_order_count = await count_broker_open_orders(ib=ib, symbols=symbols)
        if latest_open_order_count == 0:
            return {"clean": True, "attempts": attempts, "broker_open_order_count": 0}
        if time.time() >= deadline:
            return {
                "clean": False,
                "attempts": attempts,
                "broker_open_order_count": int(latest_open_order_count),
            }
        await asyncio.sleep(max(0.2, float(poll_sec or 1.0)))


def reset_strategy_runtime(*, strategy_id: str, state_dir: Path, reason: str) -> dict[str, Any]:
    state_file = state_dir / f"qqq_intraday_{strategy_id}.json"
    return reset_daylock(state_file=state_file, day=datetime.now(ET).strftime("%Y-%m-%d"), reason=reason)


async def main_async(args: argparse.Namespace) -> int:
    if not _adhoc_ib_connect_allowed(args):
        print(
            "BLOCKED: cleanup_strategy_residuals is disabled by IBKR_DISABLE_ADHOC_CONNECT=true.",
            file=sys.stderr,
        )
        return 2

    symbols = [str(item).strip().upper() for item in args.symbols if str(item).strip()]
    strategy_ids = [str(item).strip() for item in args.strategy_ids if str(item).strip()]
    payload: dict[str, Any] = {"mode": "preview" if args.dry_run else "execute", "symbols": symbols, "strategy_ids": strategy_ids}

    pg = PostgresDB()
    await pg.connect()
    ib = IB()
    connect_guard = None
    try:
        connect_guard = _acquire_adhoc_ib_connect_guard(float(args.connect_cooldown_sec))
        await ib.connectAsync(
            config.ib_config["host"],
            int(config.ib_config["port"]),
            clientId=int(args.client_id),
            timeout=float(args.connect_timeout),
            readonly=bool(args.dry_run),
        )
        broker_positions = await fetch_broker_positions(ib=ib, symbols=symbols)
        broker_open_order_count = await count_broker_open_orders(ib=ib, symbols=symbols)
        db_positions, db_orders = await fetch_db_residuals(pg=pg, symbols=symbols, strategy_ids=strategy_ids)
        payload["before"] = summarize_cleanup_state(
            broker_positions=broker_positions,
            broker_open_order_count=broker_open_order_count,
            db_positions=db_positions,
            db_orders=db_orders,
        )
        payload["broker_cleanup_orders"] = [asdict(item) for item in build_broker_cleanup_orders(broker_positions=broker_positions, session=args.session)]
        payload["db_position_cleanup"] = [
            asdict(item)
            for item in build_db_position_cleanup_plan(db_positions=db_positions, symbols=symbols, strategy_ids=strategy_ids)
        ]
        payload["db_order_cleanup"] = [
            asdict(item) for item in build_db_order_cleanup_plan(db_orders=db_orders, symbols=symbols, strategy_ids=strategy_ids)
        ]
        write_cleanup_audit(
            audit_root=args.audit_root_dir,
            strategy_id=args.primary_strategy_id or None,
            stage="residual_cleanup_started",
            reason="preview" if args.dry_run else "execute",
            payload={
                "symbols": symbols,
                "strategy_ids": strategy_ids,
                "before": payload["before"],
                "broker_cleanup_orders": payload["broker_cleanup_orders"],
                "db_position_cleanup": payload["db_position_cleanup"],
                "db_order_cleanup": payload["db_order_cleanup"],
                "session": args.session,
            },
        )
        if args.dry_run:
            write_cleanup_audit(
                audit_root=args.audit_root_dir,
                strategy_id=args.primary_strategy_id or None,
                stage="residual_cleanup_preview",
                reason="preview_only",
                payload=payload,
            )
            print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2))
            return 0

        position_cleanup_rows = build_explicit_position_cleanup_plan(
            symbols=symbols,
            strategy_ids=strategy_ids,
            existing_rows=build_db_position_cleanup_plan(
                db_positions=db_positions,
                symbols=symbols,
                strategy_ids=strategy_ids,
            ),
        )

        payload["broker_cancel"] = await cancel_broker_orders(
            ib=ib,
            symbols=symbols,
            global_cancel=bool(args.global_cancel),
        )
        payload["broker_cancel_verify"] = await wait_for_broker_open_orders_absent(
            ib=ib,
            symbols=symbols,
            timeout_sec=float(args.verify_timeout_sec),
            poll_sec=float(args.verify_poll_sec),
        )
        if not bool(payload["broker_cancel_verify"].get("clean")):
            after_broker_positions = await fetch_broker_positions(ib=ib, symbols=symbols)
            after_broker_open_order_count = await count_broker_open_orders(ib=ib, symbols=symbols)
            after_db_positions, after_db_orders = await fetch_db_residuals(pg=pg, symbols=symbols, strategy_ids=strategy_ids)
            payload["after"] = summarize_cleanup_state(
                broker_positions=after_broker_positions,
                broker_open_order_count=after_broker_open_order_count,
                db_positions=after_db_positions,
                db_orders=after_db_orders,
            )
            write_cleanup_audit(
                audit_root=args.audit_root_dir,
                strategy_id=args.primary_strategy_id or None,
                stage="residual_cleanup_blocked",
                reason="broker_cancel_not_confirmed",
                payload=payload,
            )
            print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2))
            return 2
        payload["broker_flatten_results"] = await flatten_broker_residuals(
            ib=ib,
            orders=build_broker_cleanup_orders(broker_positions=broker_positions, session=args.session),
            account=args.account or None,
        )
        payload["broker_verify"] = await wait_for_broker_clean(
            ib=ib,
            symbols=symbols,
            timeout_sec=float(args.verify_timeout_sec),
            poll_sec=float(args.verify_poll_sec),
        )
        if not bool(payload["broker_verify"].get("clean")):
            after_broker_positions = await fetch_broker_positions(ib=ib, symbols=symbols)
            after_broker_open_order_count = await count_broker_open_orders(ib=ib, symbols=symbols)
            after_db_positions, after_db_orders = await fetch_db_residuals(pg=pg, symbols=symbols, strategy_ids=strategy_ids)
            payload["after"] = summarize_cleanup_state(
                broker_positions=after_broker_positions,
                broker_open_order_count=after_broker_open_order_count,
                db_positions=after_db_positions,
                db_orders=after_db_orders,
            )
            write_cleanup_audit(
                audit_root=args.audit_root_dir,
                strategy_id=args.primary_strategy_id or None,
                stage="residual_cleanup_blocked",
                reason="broker_not_verified_flat",
                payload=payload,
            )
            print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2))
            return 2
        order_cleanup_rows = build_db_order_cleanup_plan(db_orders=db_orders, symbols=symbols, strategy_ids=strategy_ids)
        payload["db_order_broker_absence"] = await verify_broker_absent_for_db_orders(
            ib=ib,
            symbols=symbols,
            rows=order_cleanup_rows,
        )
        if not bool(payload["db_order_broker_absence"].get("clean")):
            after_broker_positions = await fetch_broker_positions(ib=ib, symbols=symbols)
            after_broker_open_order_count = await count_broker_open_orders(ib=ib, symbols=symbols)
            after_db_positions, after_db_orders = await fetch_db_residuals(pg=pg, symbols=symbols, strategy_ids=strategy_ids)
            payload["after"] = summarize_cleanup_state(
                broker_positions=after_broker_positions,
                broker_open_order_count=after_broker_open_order_count,
                db_positions=after_db_positions,
                db_orders=after_db_orders,
            )
            write_cleanup_audit(
                audit_root=args.audit_root_dir,
                strategy_id=args.primary_strategy_id or None,
                stage="residual_cleanup_blocked",
                reason="broker_order_still_live_for_db_order",
                payload=payload,
            )
            print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2))
            return 2
        payload["db_orders_cleaned"] = await terminalize_db_orders(
            pg=pg,
            rows=order_cleanup_rows,
            reason="residual_cleanup_tool",
        )
        payload["reservation_release"] = await release_db_order_reservations(
            pg=pg,
            rows=order_cleanup_rows,
            terminal_state="CANCELLED",
            reason="residual_cleanup_tool",
        )
        payload["ledger_adjustments"] = await converge_position_ledgers_flat(
            pg=pg,
            rows=position_cleanup_rows,
        )
        payload["ledger_lot_adjustments"] = await converge_position_ledger_lot_groups_flat(
            pg=pg,
            rows=position_cleanup_rows,
        )
        payload["invalid_ledger_group_adjustments"] = await converge_invalid_position_ledger_groups_flat(
            pg=pg,
            rows=position_cleanup_rows,
        )
        payload["manual_locks_cleared"] = await clear_manual_locks_for_targets(
            pg=pg,
            rows=position_cleanup_rows,
        )
        payload["protection_registry_cleaned_by_target"] = await terminalize_protection_registry_for_targets(
            pg=pg,
            rows=position_cleanup_rows,
        )
        payload["protection_registry_cleaned_by_order"] = await terminalize_protection_registry_for_orders(
            pg=pg,
            rows=order_cleanup_rows,
        )
        payload["protection_registry_cleaned"] = int(payload["protection_registry_cleaned_by_target"]) + int(
            payload["protection_registry_cleaned_by_order"]
        )
        payload["db_positions_cleaned"] = await zero_db_positions(
            pg=pg,
            rows=position_cleanup_rows,
        )
        if args.reset_runtime and args.primary_strategy_id:
            payload["runtime_reset"] = reset_strategy_runtime(
                strategy_id=args.primary_strategy_id,
                state_dir=Path(args.state_dir),
                reason="residual_cleanup_tool",
            )

        after_broker_positions = await fetch_broker_positions(ib=ib, symbols=symbols)
        after_broker_open_order_count = await count_broker_open_orders(ib=ib, symbols=symbols)
        after_db_positions, after_db_orders = await fetch_db_residuals(pg=pg, symbols=symbols, strategy_ids=strategy_ids)
        payload["after"] = summarize_cleanup_state(
            broker_positions=after_broker_positions,
            broker_open_order_count=after_broker_open_order_count,
            db_positions=after_db_positions,
            db_orders=after_db_orders,
        )
        write_cleanup_audit(
            audit_root=args.audit_root_dir,
            strategy_id=args.primary_strategy_id or None,
            stage="residual_cleanup_completed",
            reason="clean" if bool(payload["after"]["clean"]) else "residuals_remaining",
            payload=payload,
        )
        print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2))
        return 0 if bool(payload["after"]["clean"]) else 2
    finally:
        if ib.isConnected():
            ib.disconnect()
        _release_adhoc_ib_connect_guard(connect_guard)
        await pg.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-shot residual cleanup for broker/DB/runtime state.")
    parser.add_argument("--symbols", nargs="+", default=["QQQ", "TQQQ", "SQQQ"])
    parser.add_argument("--strategy-ids", nargs="+", default=["qqq_intraday_001"])
    parser.add_argument("--primary-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--session", choices=["rth", "afterhours", "overnight"], default="overnight")
    parser.add_argument("--state-dir", default="runtime/state")
    parser.add_argument("--client-id", type=int, default=int(os.getenv("IBKR_ADHOC_CLIENT_ID", "9903")))
    parser.add_argument("--connect-timeout", type=float, default=float(os.getenv("IBKR_ADHOC_CONNECT_TIMEOUT", "30")))
    parser.add_argument(
        "--connect-cooldown-sec",
        type=float,
        default=float(os.getenv("IBKR_ADHOC_CONNECT_COOLDOWN_SEC", "120")),
        help="Minimum seconds between ad-hoc IBKR sessions.",
    )
    parser.add_argument("--account", default="")
    parser.add_argument(
        "--global-cancel",
        action="store_true",
        help="DANGEROUS: cancel ALL account open orders via IB global cancel before flattening.",
    )
    parser.add_argument("--reset-runtime", action="store_true")
    parser.add_argument("--audit-root-dir", default="runtime/audit")
    parser.add_argument("--verify-timeout-sec", type=float, default=60.0)
    parser.add_argument("--verify-poll-sec", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
