from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN_ORDER_STATUSES = {
    "PRESUBMITTED",
    "SUBMITTED",
    "PENDINGSUBMIT",
    "PENDING_CANCEL",
    "APIPENDING",
    "PENDING",
    "CREATED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "PARTIAL",
}
TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


@dataclass
class NormalizedOrder:
    source: str
    order_key: str
    broker_order_id: str
    perm_id: str
    order_ref: str
    strategy_id: str
    symbol: str
    side: str
    qty: float
    status: str
    raw: Dict[str, Any]


@dataclass
class SeverityIssue:
    severity: str
    category: str
    message: str
    order_key: str = ""
    strategy_id: str = ""
    symbol: str = ""
    details: Dict[str, Any] | None = None


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> Any:
    if yaml is None:
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
            if isinstance(obj, Mapping):
                rows.append(dict(obj))
        except Exception:
            continue
    return rows


def _load_rows(path: Optional[str], fallback_keys: Tuple[str, ...]) -> List[Dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    if p.suffix.lower() == ".jsonl":
        return _read_jsonl(p)
    obj = _read_json(p)
    if isinstance(obj, list):
        return [dict(x) for x in obj if isinstance(x, Mapping)]
    if isinstance(obj, Mapping):
        root = dict(obj)
        for key in fallback_keys:
            if isinstance(root.get(key), list):
                return [dict(x) for x in root.get(key, []) if isinstance(x, Mapping)]
            if isinstance(root.get(key), Mapping):
                return [dict(root.get(key) or {})]
        return [root]
    return []


def _load_structured(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {}
    suffix = p.suffix.lower()
    try:
        if suffix in {".yaml", ".yml"}:
            obj = _read_yaml(p)
        else:
            obj = _read_json(p)
    except Exception:
        return {}
    return _as_dict(obj)


def _dedup_keep_order(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in values:
        v = str(raw or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def load_enabled_strategy_ids_from_configs(config_paths: Iterable[str]) -> List[str]:
    enabled: List[str] = []
    for path in config_paths:
        cfg = _load_structured(path)
        if not cfg:
            continue
        strategies = cfg.get("strategies")
        if isinstance(strategies, list):
            for item in strategies:
                row = _as_dict(item)
                sid = str(row.get("id") or row.get("strategy_id") or "").strip()
                if not sid:
                    continue
                if bool(row.get("enabled", True)):
                    enabled.append(sid)
        if not enabled:
            ncfg = _as_dict(cfg.get("notifications"))
            tcfg = _as_dict(ncfg.get("telegram"))
            for sid in _as_list(tcfg.get("strategy_allowlist")):
                text = str(sid or "").strip()
                if text and text != "system_reconciler":
                    enabled.append(text)
    return _dedup_keep_order(enabled)


def _build_pg_dsn(explicit_dsn: str = "") -> str:
    direct = str(explicit_dsn or os.getenv("PG_URL") or "").strip()
    if direct:
        return direct
    user = os.getenv("PG_USER", "postgres")
    password = os.getenv("PG_PASSWORD", "postgres")
    host = os.getenv("PG_HOST", os.getenv("PGHOST", "localhost"))
    port = os.getenv("PG_PORT", os.getenv("PGPORT", "5432"))
    database = os.getenv("PG_DB", os.getenv("PGDATABASE", "test"))
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def _load_system_payload_from_db_async(
    *,
    trade_date: str,
    dsn: str,
    account_table: str,
) -> Dict[str, List[Dict[str, Any]]]:
    try:
        import asyncpg  # type: ignore
    except Exception as exc:
        raise RuntimeError("asyncpg is required for --load-system-from-db; run with the project venv") from exc

    start_et = datetime.fromisoformat(f"{trade_date}T00:00:00").replace(tzinfo=ET)
    end_et = start_et + timedelta(days=1)
    start_utc = start_et.astimezone(ZoneInfo("UTC"))
    end_utc = end_et.astimezone(ZoneInfo("UTC"))
    is_today_trade_date = trade_date == datetime.now(ET).date().isoformat()

    conn = await asyncpg.connect(dsn)
    try:
        try:
            orders = await conn.fetch(
                """
                SELECT
                    COALESCE(order_id::text, '') AS client_order_id,
                    COALESCE(strategy_id, '') AS strategy_id,
                    COALESCE(symbol, '') AS symbol,
                    COALESCE(side, '') AS side,
                    COALESCE(qty, 0) AS qty,
                    COALESCE(state, '') AS status,
                    COALESCE(broker_order_id, '') AS broker_order_id,
                    COALESCE(ib_perm_id::text, '') AS perm_id,
                    COALESCE(order_ref, '') AS order_ref
                FROM core.core_order
                WHERE
                    (created_at >= $1 AND created_at < $2)
                    OR (updated_at >= $1 AND updated_at < $2)
                ORDER BY COALESCE(updated_at, created_at) ASC
                """,
                start_utc,
                end_utc,
            )
        except Exception:
            orders = await conn.fetch(
                """
                SELECT
                    COALESCE(order_id::text, '') AS client_order_id,
                    COALESCE(strategy_id, '') AS strategy_id,
                    COALESCE(symbol, '') AS symbol,
                    COALESCE(side, '') AS side,
                    COALESCE(qty, 0) AS qty,
                    COALESCE(state, '') AS status,
                    COALESCE(broker_order_id, '') AS broker_order_id,
                    COALESCE(ib_perm_id::text, '') AS perm_id,
                    COALESCE(order_ref, '') AS order_ref
                FROM core.core_order
                WHERE created_at >= $1 AND created_at < $2
                ORDER BY created_at ASC
                """,
                start_utc,
                end_utc,
            )
        positions = await conn.fetch(
            """
            SELECT
                COALESCE(strategy_id, '') AS strategy_id,
                COALESCE(symbol, '') AS symbol,
                COALESCE(pos_qty, 0) AS qty,
                COALESCE(avg_px, 0) AS avg_px
            FROM core.core_position
            WHERE abs(COALESCE(pos_qty, 0)) > 0.000001
            ORDER BY strategy_id, symbol
            """
        )
        account_rows: List[Dict[str, Any]] = []
        account_table_name = str(account_table or "").strip()
        if account_table_name:
            try:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", account_table_name):
                    raise ValueError(f"invalid account table name: {account_table_name}")
                if "." in account_table_name:
                    schema_name, table_name = account_table_name.split(".", 1)
                else:
                    schema_name, table_name = "public", account_table_name
                exists = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = $1 AND table_name = $2
                    )
                    """,
                    schema_name,
                    table_name,
                )
                if not exists:
                    raise LookupError(f"account table does not exist: {account_table_name}")
                time_column = None
                for cand in ("ts", "updated_at", "created_at"):
                    has_col = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = $1 AND table_name = $2 AND column_name = $3
                        )
                        """,
                        schema_name,
                        table_name,
                        cand,
                    )
                    if has_col:
                        time_column = cand
                        break
                rec = None
                if time_column:
                    rec = await conn.fetchrow(
                        f"""
                        SELECT *, 'db_account_table' AS account_source
                        FROM {account_table_name}
                        WHERE {time_column} >= $1 AND {time_column} < $2
                        ORDER BY {time_column} DESC
                        LIMIT 1
                        """,
                        start_utc,
                        end_utc,
                    )
                if rec:
                    account_rows = [dict(rec)]
            except Exception:
                account_rows = []
        if not account_rows:
            try:
                account_from_positions = None
                try:
                    account_from_positions = await conn.fetchrow(
                        """
                        SELECT
                            SUM(COALESCE(realized_pnl_usd, 0)) AS realized_pnl,
                            SUM(COALESCE(unrealized_pnl_usd, 0)) AS unrealized_pnl,
                            MAX(updated_at) AS ts,
                            COUNT(*) AS row_count,
                            'core_position_aggregate_window' AS account_source
                        FROM core.core_position
                        WHERE updated_at >= $1 AND updated_at < $2
                        """,
                        start_utc,
                        end_utc,
                    )
                except Exception:
                    if is_today_trade_date:
                        account_from_positions = await conn.fetchrow(
                            """
                            SELECT
                                SUM(COALESCE(realized_pnl_usd, 0)) AS realized_pnl,
                                SUM(COALESCE(unrealized_pnl_usd, 0)) AS unrealized_pnl,
                                MAX(updated_at) AS ts,
                                COUNT(*) AS row_count,
                                'core_position_aggregate_unscoped_today_only' AS account_source
                            FROM core.core_position
                            """
                        )
                if account_from_positions:
                    row = dict(account_from_positions)
                    if int(row.get("row_count") or 0) <= 0:
                        row = {}
                else:
                    row = {}
                if row:
                    realized = float(row.get("realized_pnl") or 0.0)
                    unrealized = float(row.get("unrealized_pnl") or 0.0)
                    account_rows = [
                        {
                            "realized_pnl": realized,
                            "unrealized_pnl": unrealized,
                            "day_pnl": realized + unrealized,
                            "account_source": row.get("account_source"),
                            "ts": row.get("ts"),
                        }
                    ]
            except Exception:
                account_rows = []
        return {
            "orders": [dict(x) for x in orders],
            "positions": [dict(x) for x in positions],
            "account": account_rows,
        }
    except Exception as exc:
        raise RuntimeError(f"failed to load system payload from PostgreSQL: {exc}") from exc
    finally:
        await conn.close()


def load_system_payload_from_db(
    *,
    trade_date: str,
    dsn: str,
    account_table: str,
) -> Dict[str, List[Dict[str, Any]]]:
    return asyncio.run(
        _load_system_payload_from_db_async(
            trade_date=trade_date,
            dsn=dsn,
            account_table=account_table,
        )
    )


async def _load_ib_live_snapshot_async(
    *,
    host: str,
    port: int,
    client_id: int,
    connect_timeout: float = 30.0,
    trade_date: str = "",
    include_completed_orders: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    try:
        from ib_insync import IB, ExecutionFilter  # type: ignore
    except Exception:
        return {"orders": [], "positions": [], "account": []}

    ib = IB()
    try:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                await ib.connectAsync(
                    host,
                    int(port),
                    clientId=int(client_id),
                    timeout=float(connect_timeout),
                    readonly=True,
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt >= 1:
                    raise
                await asyncio.sleep(1.0)
        if last_exc is not None:
            raise last_exc
        target_trade_date = str(trade_date or "").strip()

        def _trade_day_matches(value: Any) -> bool:
            if not target_trade_date:
                return True
            ts = _parse_iso_ts(value)
            if ts is not None:
                return ts.astimezone(ET).date().isoformat() == target_trade_date
            text = str(value or "").strip()
            for fmt in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt).date().isoformat() == target_trade_date
                except Exception:
                    continue
            return False

        def _normalize_order_like_row(*, order: Any, contract: Any, status: Any = "", execution: Any = None) -> Dict[str, Any]:
            action = str(getattr(order, "action", "") or getattr(execution, "side", "") or "").strip().upper()
            qty = _safe_abs_qty(
                getattr(order, "totalQuantity", None)
                or getattr(execution, "shares", None)
                or getattr(order, "filledQuantity", None)
            )
            return {
                "broker_order_id": str(getattr(order, "orderId", "") or "").strip(),
                "perm_id": str(
                    getattr(order, "permId", None)
                    or getattr(execution, "permId", None)
                    or ""
                ).strip(),
                "order_ref": str(getattr(order, "orderRef", "") or "").strip(),
                "client_order_id": str(getattr(order, "clientId", "") or "").strip(),
                "symbol": str(getattr(contract, "symbol", "") or "").strip().upper(),
                "side": action,
                "qty": qty,
                "status": str(status or "").strip(),
                "order_type": str(getattr(order, "orderType", "") or "").strip(),
            }

        try:
            if hasattr(ib, "reqAllOpenOrdersAsync"):
                await ib.reqAllOpenOrdersAsync()
            else:
                ib.reqAllOpenOrders()
            await asyncio.sleep(0.2)
        except Exception:
            pass

        orders: List[Dict[str, Any]] = []
        for trade in list(ib.openTrades() or []):
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            order_status = getattr(trade, "orderStatus", None)
            if order is None or contract is None:
                continue
            orders.append(
                _normalize_order_like_row(
                    order=order,
                    contract=contract,
                    status=getattr(order_status, "status", "") or "",
                )
            )

        def _merge_order_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                item = dict(row)
                key = (
                    str(item.get("broker_order_id") or "").strip(),
                    str(item.get("perm_id") or "").strip(),
                    str(item.get("order_ref") or "").strip(),
                )
                if key == ("", "", ""):
                    key = (
                        str(item.get("symbol") or "").strip(),
                        str(item.get("side") or "").strip(),
                        f"{_safe_abs_qty(item.get('qty')):.6f}",
                    )
                current = merged.get(key)
                if current is None:
                    merged[key] = item
                    continue
                for field in ("broker_order_id", "perm_id", "order_ref", "client_order_id", "symbol", "side", "order_type"):
                    if not current.get(field) and item.get(field):
                        current[field] = item.get(field)
                current["qty"] = max(_safe_abs_qty(current.get("qty")), _safe_abs_qty(item.get("qty")))
                status_candidates = {
                    _norm_status(current.get("status")),
                    _norm_status(item.get("status")),
                }
                if any(x in TERMINAL_ORDER_STATUSES for x in status_candidates):
                    current["status"] = next(
                        x for x in (
                            current.get("status"),
                            item.get("status"),
                        )
                        if _norm_status(x) in TERMINAL_ORDER_STATUSES
                    )
                elif not current.get("status") and item.get("status"):
                    current["status"] = item.get("status")
            return list(merged.values())

        positions: List[Dict[str, Any]] = []
        try:
            for p in list(ib.positions() or []):
                contract = getattr(p, "contract", None)
                sym = str(getattr(contract, "symbol", "") or "").strip().upper()
                if not sym:
                    continue
                positions.append(
                    {
                        "symbol": sym,
                        "qty": float(getattr(p, "position", 0.0) or 0.0),
                        "avg_price": float(getattr(p, "avgCost", 0.0) or 0.0),
                    }
                )
        except Exception:
            positions = []

        history_rows: List[Dict[str, Any]] = []
        try:
            exec_filter = ExecutionFilter()
            exec_time = datetime.strptime(target_trade_date, "%Y-%m-%d").strftime("%Y%m%d 00:00:00") if target_trade_date else ""
            if exec_time:
                exec_filter.time = exec_time
            exec_details = await ib.reqExecutionsAsync(exec_filter)
            for fill in list(exec_details or []):
                execution = getattr(fill, "execution", None)
                contract = getattr(fill, "contract", None)
                if execution is None or contract is None:
                    continue
                if not _trade_day_matches(getattr(execution, "time", None)):
                    continue
                history_rows.append(
                    _normalize_order_like_row(
                        order=SimpleNamespace(
                            orderId=getattr(execution, "orderId", None),
                            permId=getattr(execution, "permId", None),
                            orderRef="",
                            clientId=getattr(execution, "clientId", ""),
                            action=getattr(execution, "side", None),
                            totalQuantity=getattr(execution, "shares", None),
                            orderType="",
                        ),
                        contract=contract,
                        status="Filled",
                        execution=execution,
                    )
                )
        except Exception:
            pass

        if include_completed_orders:
            try:
                # reqCompletedOrders can be extremely chatty on active accounts; keep opt-in.
                completed = await ib.reqCompletedOrdersAsync(apiOnly=True)
                for trade in list(completed or []):
                    order = getattr(trade, "order", None)
                    contract = getattr(trade, "contract", None)
                    order_status = getattr(trade, "orderStatus", None)
                    if order is None or contract is None:
                        continue
                    completed_ts = (
                        getattr(order_status, "completedTime", None)
                        or getattr(order, "goodTillDate", None)
                        or getattr(order, "goodAfterTime", None)
                    )
                    if target_trade_date and completed_ts and not _trade_day_matches(completed_ts):
                        continue
                    history_rows.append(
                        _normalize_order_like_row(
                            order=order,
                            contract=contract,
                            status=getattr(order_status, "status", "") or "Completed",
                        )
                    )
            except Exception:
                pass

        if history_rows:
            orders = _merge_order_rows(orders + history_rows)

        account_row: Dict[str, Any] = {}
        try:
            rows = await ib.accountSummaryAsync("")
            by_tag: Dict[str, float] = {}
            for r in rows or []:
                tag = str(getattr(r, "tag", "") or "")
                currency = str(getattr(r, "currency", "") or "")
                if currency and currency != "USD":
                    continue
                try:
                    by_tag[tag] = float(getattr(r, "value", 0.0) or 0.0)
                except Exception:
                    continue
            account_row = {
                "equity": float(by_tag.get("NetLiquidation", 0.0)),
                "cash": float(by_tag.get("TotalCashValue", 0.0)),
                "buying_power": float(by_tag.get("BuyingPower", 0.0)),
                "day_pnl": float(by_tag.get("RealizedPnL", 0.0) + by_tag.get("UnrealizedPnL", 0.0)),
                "realized_pnl": float(by_tag.get("RealizedPnL", 0.0)),
                "unrealized_pnl": float(by_tag.get("UnrealizedPnL", 0.0)),
            }
        except Exception:
            account_row = {}

        return {
            "orders": orders,
            "positions": positions,
            "account": [account_row] if account_row else [],
        }
    except Exception as exc:
        raise RuntimeError(f"failed to load IBKR live snapshot: {exc}") from exc
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass


def load_ib_live_snapshot(
    *,
    host: str,
    port: int,
    client_id: int,
    connect_timeout: float = 30.0,
    trade_date: str = "",
    include_completed_orders: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    payload = asyncio.run(
        _load_ib_live_snapshot_async(
            host=host,
            port=int(port),
            client_id=int(client_id),
            connect_timeout=float(connect_timeout),
            trade_date=str(trade_date or "").strip(),
            include_completed_orders=bool(include_completed_orders),
        )
    )
    if not payload.get("account"):
        raise RuntimeError("IBKR live snapshot did not include account summary; refusing to generate a partial report")
    return payload


def _flex_attr(obj: Any, *names: str) -> Any:
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value not in (None, ""):
                return value
    return None


def _flex_trade_day_matches(row: Any, trade_date: str) -> bool:
    target = str(trade_date or "").strip()
    if not target:
        return True
    for key in (
        "dateTime",
        "tradeDateTime",
        "date",
        "reportDate",
        "tradeDate",
        "orderTime",
    ):
        value = _flex_attr(row, key)
        if value in (None, ""):
            continue
        ts = _parse_iso_ts(value)
        if ts is not None and ts.astimezone(ET).date().isoformat() == target:
            return True
        text = str(value).strip()
        if text == target:
            return True
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y%m%d;%H%M%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).date().isoformat() == target
            except Exception:
                continue
    return False


def load_ib_flex_snapshot(
    *,
    trade_date: str,
    token: str = "",
    query_id: str = "",
    report_path: str = "",
    save_xml_path: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    try:
        from ib_insync import FlexReport  # type: ignore
    except Exception:
        return {"orders": [], "positions": [], "account": []}

    token = str(token or "").strip()
    query_id = str(query_id or "").strip()
    report_path = str(report_path or "").strip()
    save_xml_path = str(save_xml_path or "").strip()
    if not report_path and not (token and query_id):
        return {"orders": [], "positions": [], "account": []}

    try:
        report = FlexReport(token=token, queryId=query_id) if (token and query_id) else FlexReport(path=report_path)
        if save_xml_path:
            try:
                Path(save_xml_path).parent.mkdir(parents=True, exist_ok=True)
                report.save(save_xml_path)
            except Exception:
                pass
    except Exception:
        return {"orders": [], "positions": [], "account": []}

    topics = set(report.topics() or set())
    orders: List[Dict[str, Any]] = []
    positions: List[Dict[str, Any]] = []
    account_row: Dict[str, Any] = {}

    if "Trade" in topics:
        try:
            for trade in report.extract("Trade"):
                if not _flex_trade_day_matches(trade, trade_date):
                    continue
                side = str(_flex_attr(trade, "buySell", "side") or "").strip().upper()
                symbol = str(_flex_attr(trade, "symbol", "underlyingSymbol") or "").strip().upper()
                qty = _safe_abs_qty(_flex_attr(trade, "quantity", "qty"))
                if not symbol or not side or qty <= 0:
                    continue
                orders.append(
                    {
                        "broker_order_id": str(
                            _flex_attr(trade, "ibOrderID", "orderID", "brokerOrderID", "orderId") or ""
                        ).strip(),
                        "perm_id": str(_flex_attr(trade, "permID", "permId") or "").strip(),
                        "order_ref": str(_flex_attr(trade, "orderReference", "orderRef") or "").strip(),
                        "symbol": symbol,
                        "side": side,
                        "qty": qty,
                        "status": "Filled",
                        "order_type": str(_flex_attr(trade, "orderType") or "").strip(),
                        "exec_id": str(_flex_attr(trade, "ibExecID", "execID", "execId") or "").strip(),
                        "price": _safe_float(_flex_attr(trade, "tradePrice", "price")),
                    }
                )
        except Exception:
            orders = []

    if "OpenPosition" in topics:
        try:
            for pos in report.extract("OpenPosition"):
                symbol = str(_flex_attr(pos, "symbol") or "").strip().upper()
                if not symbol:
                    continue
                positions.append(
                    {
                        "symbol": symbol,
                        "qty": _safe_float(_flex_attr(pos, "position", "quantity", "qty")),
                        "avg_price": _safe_float(_flex_attr(pos, "costBasisPrice", "costBasis", "markPrice")),
                    }
                )
        except Exception:
            positions = []

    if "EquitySummaryByReportDateInBase" in topics:
        try:
            rows = report.extract("EquitySummaryByReportDateInBase")
            if rows:
                latest = rows[-1]
                account_row = {
                    "equity": _safe_float(_flex_attr(latest, "netLiquidationValue", "netAssetValue", "endingSettledValue")),
                    "cash": _safe_float(_flex_attr(latest, "cash", "endingSettledValue")),
                    "day_pnl": _safe_float(_flex_attr(latest, "changeInNAV", "mtmMTD", "mtmYTD")),
                }
        except Exception:
            account_row = {}

    return {
        "orders": orders,
        "positions": positions,
        "account": [account_row] if account_row else [],
    }


def _normalize_mode(mode: str) -> str:
    text = str(mode or "").strip().lower()
    if text in {"live", "research"}:
        return text
    return "auto"


def infer_report_mode(*paths: str) -> str:
    joined = " ".join(str(p or "") for p in paths).lower()
    if "/research/cycles/" in joined or "runs/ai_trader/research/" in joined:
        return "research"
    return "live"


def _safe_upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _parse_iso_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _safe_abs_qty(value: Any) -> float:
    return abs(_safe_float(value))


def _norm_status(status: Any) -> str:
    s = _safe_upper(status).replace(" ", "").replace("-", "_")
    alias = {
        "NEW": "CREATED",
        "PENDING_SUBMIT": "PENDINGSUBMIT",
        "PENDINGCANCEL": "PENDING_CANCEL",
        "PARTIALLYFILLED": "PARTIALLY_FILLED",
        "PARTIAL": "PARTIALLY_FILLED",
        "CANCELLED": "CANCELED",
        "CLOSED": "FILLED",
    }
    return alias.get(s, s or "UNKNOWN")


def _extract_strategy_id(row: Mapping[str, Any]) -> str:
    for key in ("strategy_id", "candidate_id", "experiment_id", "strategy", "algo_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    md = _as_dict(row.get("metadata"))
    value = str(md.get("strategy_id") or "").strip()
    if value:
        return value
    order_ref = str(row.get("order_ref") or "").strip()
    if order_ref and ":" in order_ref:
        return order_ref.split(":", 1)[0]
    return "UNKNOWN"


def _extract_order_key(row: Mapping[str, Any]) -> Tuple[str, str, str, str]:
    broker_order_id = str(row.get("broker_order_id") or row.get("order_id") or "").strip()
    perm_id = str(row.get("perm_id") or row.get("ib_perm_id") or "").strip()
    order_ref = str(row.get("order_ref") or "").strip()
    client_id = str(row.get("client_order_id") or row.get("idempotency_key") or "").strip()
    if broker_order_id:
        return f"broker:{broker_order_id}", broker_order_id, perm_id, order_ref
    if perm_id:
        return f"perm:{perm_id}", broker_order_id, perm_id, order_ref
    if order_ref:
        return f"ref:{order_ref}", broker_order_id, perm_id, order_ref
    if client_id:
        return f"client:{client_id}", broker_order_id, perm_id, order_ref
    symbol = _safe_upper(row.get("symbol"))
    side = _safe_upper(row.get("side"))
    qty = _safe_abs_qty(row.get("qty") or row.get("quantity") or row.get("totalQuantity"))
    return f"fallback:{symbol}:{side}:{qty:.6f}", broker_order_id, perm_id, order_ref


def _normalize_orders(rows: Iterable[Mapping[str, Any]], source: str) -> List[NormalizedOrder]:
    out: List[NormalizedOrder] = []
    for raw in rows:
        row = _as_dict(raw)
        order_key, broker_order_id, perm_id, order_ref = _extract_order_key(row)
        out.append(
            NormalizedOrder(
                source=source,
                order_key=order_key,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
                order_ref=order_ref,
                strategy_id=_extract_strategy_id(row),
                symbol=_safe_upper(row.get("symbol")),
                side=_safe_upper(row.get("side")),
                qty=_safe_abs_qty(row.get("qty") or row.get("quantity") or row.get("totalQuantity")),
                status=_norm_status(row.get("status") or row.get("state")),
                raw=row,
            )
        )
    return out


def _telegram_sent_index(rows: List[Mapping[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    sent = [
        _as_dict(x)
        for x in rows
        if _safe_upper(_as_dict(x).get("outcome")) in {"SENT", "ATTEMPT_FAILED", "FAILED", "SKIPPED"}
    ]
    sent.sort(key=lambda x: str(x.get("push_ts") or x.get("ts") or ""))
    latest_by_key: Dict[str, Dict[str, Any]] = {}
    submitted_qty_by_key: Dict[str, float] = {}
    fill_qty_sum_by_key: Dict[str, float] = {}
    by_broker: Dict[str, Dict[str, Any]] = {}
    by_perm: Dict[str, Dict[str, Any]] = {}
    by_ref: Dict[str, Dict[str, Any]] = {}
    for row in sent:
        key, _, _, _ = _extract_order_key(row)
        latest_by_key[key] = row
        event_type = str(row.get("event_type") or "").strip().lower()
        qty = _safe_abs_qty(row.get("qty"))
        if event_type == "order_submitted" and qty > 0:
            submitted_qty_by_key[key] = qty
        elif event_type == "order_fill" and qty > 0:
            fill_qty_sum_by_key[key] = fill_qty_sum_by_key.get(key, 0.0) + qty
        broker_order_id = str(row.get("broker_order_id") or row.get("order_id") or "").strip()
        perm_id = str(row.get("perm_id") or row.get("ib_perm_id") or "").strip()
        order_ref = str(row.get("order_ref") or "").strip()
        if broker_order_id:
            by_broker[broker_order_id] = row
        if perm_id:
            by_perm[perm_id] = row
        if order_ref:
            by_ref[order_ref] = row
    out: Dict[str, Dict[str, Dict[str, Any]]] = {"by_key": {}, "by_broker": by_broker, "by_perm": by_perm, "by_ref": by_ref}
    for key, row in latest_by_key.items():
        out["by_key"][key] = {
            "latest": row,
            "submitted_qty": submitted_qty_by_key.get(key, 0.0),
            "fill_qty_sum": fill_qty_sum_by_key.get(key, 0.0),
        }
    return out


def _resolve_telegram_match(
    tg_index: Mapping[str, Any],
    order: NormalizedOrder,
) -> Dict[str, Any]:
    by_key = _as_dict(tg_index.get("by_key"))
    direct = _as_dict(by_key.get(order.order_key))
    if direct:
        return direct
    broker_order_id = str(order.broker_order_id or "").strip()
    if broker_order_id:
        row = _as_dict(_as_dict(tg_index.get("by_broker")).get(broker_order_id))
        if row:
            return {"latest": row, "submitted_qty": 0.0, "fill_qty_sum": 0.0}
    perm_id = str(order.perm_id or "").strip()
    if perm_id:
        row = _as_dict(_as_dict(tg_index.get("by_perm")).get(perm_id))
        if row:
            return {"latest": row, "submitted_qty": 0.0, "fill_qty_sum": 0.0}
    order_ref = str(order.order_ref or "").strip()
    if order_ref:
        row = _as_dict(_as_dict(tg_index.get("by_ref")).get(order_ref))
        if row:
            return {"latest": row, "submitted_qty": 0.0, "fill_qty_sum": 0.0}
    return {}


def _status_close_enough(sys_status: str, ib_status: str) -> bool:
    if sys_status == ib_status:
        return True
    open_set = OPEN_ORDER_STATUSES
    if sys_status in open_set and ib_status in open_set:
        return True
    if sys_status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"} and ib_status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
        return sys_status == ib_status
    return False


def _likely_sent_to_broker(order: NormalizedOrder) -> bool:
    if str(order.broker_order_id or "").strip():
        return True
    if str(order.perm_id or "").strip():
        return True
    # Rejected/created orders without broker identity are usually blocked before broker submit.
    if order.status in {"REJECTED", "CREATED", "NEW", "UNKNOWN"}:
        return False
    return True


def _group_positions(rows: List[Mapping[str, Any]]) -> Dict[str, float]:
    acc: Dict[str, float] = {}
    for raw in rows:
        row = _as_dict(raw)
        sym = _safe_upper(row.get("symbol"))
        if not sym:
            continue
        qty = _safe_float(row.get("qty") or row.get("pos_qty") or row.get("position"))
        acc[sym] = acc.get(sym, 0.0) + qty
    return acc


def _account_metrics(row: Optional[Mapping[str, Any]]) -> Dict[str, float]:
    obj = _as_dict(row)
    nested = _as_dict(obj.get("account") or obj.get("account_snapshot"))
    if nested:
        obj = {**nested, **obj}

    aliases = {
        "day_pnl": ("day_pnl", "daily_pnl", "pnl", "total_pnl", "total_pnl_usd"),
        "realized_pnl": ("realized_pnl", "realized", "realized_pnl_usd", "RealizedPnL"),
        "unrealized_pnl": ("unrealized_pnl", "unrealized", "unrealized_pnl_usd", "UnrealizedPnL"),
        "equity": ("equity", "net_liq", "net_liquidation", "net_liquidation_value", "NetLiquidation"),
    }
    out: Dict[str, float] = {}
    for key, names in aliases.items():
        for name in names:
            if name in obj and obj.get(name) not in (None, ""):
                out[key] = _safe_float(obj.get(name))
                break
    if "day_pnl" not in out and ("realized_pnl" in out or "unrealized_pnl" in out):
        out["day_pnl"] = out.get("realized_pnl", 0.0) + out.get("unrealized_pnl", 0.0)
    return out


def _severity_rank(sev: str) -> int:
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(sev, 9)


def build_reconciliation_report(
    *,
    trade_date: str,
    system_orders: List[Mapping[str, Any]],
    ib_orders: List[Mapping[str, Any]],
    telegram_audit: List[Mapping[str, Any]],
    system_positions: List[Mapping[str, Any]],
    ib_positions: List[Mapping[str, Any]],
    system_account: Optional[Mapping[str, Any]],
    ib_account: Optional[Mapping[str, Any]],
    enabled_strategies: Optional[Iterable[str]] = None,
    report_mode: str = "live",
    enable_broker_order_consistency: bool = True,
    pnl_tolerance: float = 1.0,
    qty_tolerance: float = 1e-6,
) -> Dict[str, Any]:
    sys_orders = _normalize_orders(system_orders, source="system")
    ib_norm_orders = _normalize_orders(ib_orders, source="ibkr")
    tg_index = _telegram_sent_index(telegram_audit)

    sys_by_key = {x.order_key: x for x in sys_orders}
    ib_by_key = {x.order_key: x for x in ib_norm_orders}
    enabled_set: Set[str] = {str(x).strip() for x in (enabled_strategies or []) if str(x).strip()}
    mode = _normalize_mode(report_mode)
    check_strategy_config_warnings = mode == "live"
    trade_day = str(trade_date or "").strip()

    issues: List[SeverityIssue] = []

    for key, s in sys_by_key.items():
        if check_strategy_config_warnings and enabled_set and s.strategy_id not in enabled_set:
            issues.append(
                SeverityIssue(
                    severity="HIGH",
                    category="order_from_non_enabled_strategy",
                    message="配置中未启用的策略出现当日订单",
                    order_key=key,
                    strategy_id=s.strategy_id,
                    symbol=s.symbol,
                )
            )
        tg = _resolve_telegram_match(tg_index, s)
        tg_latest = _as_dict(tg.get("latest"))
        if not tg_latest:
            issues.append(
                SeverityIssue(
                    severity="HIGH",
                    category="missing_telegram_push",
                    message="订单在 Telegram 审计中缺失",
                    order_key=key,
                    strategy_id=s.strategy_id,
                    symbol=s.symbol,
                )
            )
        else:
            tg_symbol = _safe_upper(tg_latest.get("symbol"))
            tg_side = _safe_upper(tg_latest.get("side"))
            submitted_qty = _safe_abs_qty(tg.get("submitted_qty"))
            fill_qty_sum = _safe_abs_qty(tg.get("fill_qty_sum"))
            tg_qty = submitted_qty if submitted_qty > 0 else (fill_qty_sum if fill_qty_sum > 0 else _safe_abs_qty(tg_latest.get("qty")))
            if tg_symbol and s.symbol and tg_symbol != s.symbol:
                issues.append(
                    SeverityIssue(
                        severity="MEDIUM",
                        category="telegram_symbol_mismatch",
                        message="Telegram 推送 symbol 与系统订单不一致",
                        order_key=key,
                        strategy_id=s.strategy_id,
                        symbol=s.symbol,
                        details={"system": s.symbol, "telegram": tg_symbol},
                    )
                )
            if tg_side and s.side and tg_side != s.side:
                issues.append(
                    SeverityIssue(
                        severity="MEDIUM",
                        category="telegram_side_mismatch",
                        message="Telegram 推送 side 与系统订单不一致",
                        order_key=key,
                        strategy_id=s.strategy_id,
                        symbol=s.symbol,
                        details={"system": s.side, "telegram": tg_side},
                    )
                )
            if tg_qty and abs(tg_qty - s.qty) > qty_tolerance:
                issues.append(
                    SeverityIssue(
                        severity="MEDIUM",
                        category="telegram_qty_mismatch",
                        message="Telegram 推送 qty 与系统订单不一致",
                        order_key=key,
                        strategy_id=s.strategy_id,
                        symbol=s.symbol,
                        details={"system": s.qty, "telegram": tg_qty},
                    )
                )
        if not enable_broker_order_consistency:
            continue

        b = ib_by_key.get(key)
        if b is None:
            if not _likely_sent_to_broker(s):
                continue
            issues.append(
                SeverityIssue(
                    severity="CRITICAL",
                    category="missing_in_ibkr",
                    message="系统订单在 IBKR 未找到",
                    order_key=key,
                    strategy_id=s.strategy_id,
                    symbol=s.symbol,
                    details={"system_status": s.status},
                )
            )
            continue

        if s.symbol and b.symbol and s.symbol != b.symbol:
            issues.append(
                SeverityIssue(
                    severity="CRITICAL",
                    category="symbol_mismatch",
                    message="系统与 IBKR 的 symbol 不一致",
                    order_key=key,
                    strategy_id=s.strategy_id,
                    symbol=s.symbol,
                    details={"system": s.symbol, "ibkr": b.symbol},
                )
            )
        if s.side and b.side and s.side != b.side:
            issues.append(
                SeverityIssue(
                    severity="CRITICAL",
                    category="side_mismatch",
                    message="系统与 IBKR 的 side 不一致",
                    order_key=key,
                    strategy_id=s.strategy_id,
                    symbol=s.symbol,
                    details={"system": s.side, "ibkr": b.side},
                )
            )
        if abs(s.qty - b.qty) > qty_tolerance:
            issues.append(
                SeverityIssue(
                    severity="HIGH",
                    category="qty_mismatch",
                    message="系统与 IBKR 的下单数量不一致",
                    order_key=key,
                    strategy_id=s.strategy_id,
                    symbol=s.symbol,
                    details={"system": s.qty, "ibkr": b.qty},
                )
            )
        if not _status_close_enough(s.status, b.status):
            sev = "CRITICAL" if b.status == "PRESUBMITTED" else "HIGH"
            issues.append(
                SeverityIssue(
                    severity=sev,
                    category="status_mismatch",
                    message="系统与 IBKR 的订单状态不一致",
                    order_key=key,
                    strategy_id=s.strategy_id,
                    symbol=s.symbol,
                    details={"system": s.status, "ibkr": b.status},
                )
            )

    if enable_broker_order_consistency:
        for key, b in ib_by_key.items():
            if key not in sys_by_key:
                sev = "CRITICAL" if b.status in OPEN_ORDER_STATUSES else "HIGH"
                issues.append(
                    SeverityIssue(
                        severity=sev,
                        category="extra_in_ibkr",
                        message="IBKR 存在系统未记录订单",
                        order_key=key,
                        strategy_id=b.strategy_id,
                        symbol=b.symbol,
                        details={"ibkr_status": b.status},
                    )
                )
    elif mode == "live" and len(sys_orders) > 0:
        issues.append(
            SeverityIssue(
                severity="HIGH",
                category="ibkr_input_missing",
                message="未提供 IBKR 订单输入，无法完成系统-IB 逐单一致性核对",
                strategy_id="system_reconciler",
                details={"system_orders": len(sys_orders), "ibkr_orders": len(ib_norm_orders)},
            )
        )

    pre_submitted = [
        x for x in ib_norm_orders if x.status == "PRESUBMITTED"
    ]
    for x in pre_submitted:
        issues.append(
            SeverityIssue(
                severity="CRITICAL",
                category="ibkr_presubmitted_open",
                message="IBKR 存在 PreSubmitted 残留单（未完全同步清仓）",
                order_key=x.order_key,
                strategy_id=x.strategy_id,
                symbol=x.symbol,
                details={"status": x.status, "qty": x.qty},
            )
        )

    ib_open_non_terminal = [x for x in ib_norm_orders if x.status in OPEN_ORDER_STATUSES]
    for x in ib_open_non_terminal:
        issues.append(
            SeverityIssue(
                severity="HIGH",
                category="ibkr_open_order_after_close",
                message="收盘后仍有 IBKR 开放订单",
                order_key=x.order_key,
                strategy_id=x.strategy_id,
                symbol=x.symbol,
                details={"status": x.status},
            )
        )

    runtime_recon_rows = []
    for raw in telegram_audit:
        row = _as_dict(raw)
        etype = str(row.get("event_type") or "").strip().lower()
        if etype not in {"reconciliation_alert", "reconciliation_summary"}:
            continue
        ts = _parse_iso_ts(row.get("ts") or row.get("push_ts"))
        if ts is not None and trade_day:
            if ts.astimezone(ET).date().isoformat() != trade_day:
                continue
        runtime_recon_rows.append(row)

    recon_alert_count = len(runtime_recon_rows)
    max_ghosts = 0
    max_ib_presubmitted = 0
    max_mismatches = 0
    for row in runtime_recon_rows:
        md = _as_dict(row.get("metadata"))
        ghosts = int(md.get("ghosts_found") or 0)
        ib_pre = int(md.get("ib_pre_submitted_orders") or 0)
        mismatches = int(md.get("mismatches") or 0)
        max_ghosts = max(max_ghosts, ghosts)
        max_ib_presubmitted = max(max_ib_presubmitted, ib_pre)
        max_mismatches = max(max_mismatches, mismatches)
        evidence = _as_dict(md.get("evidence"))
        if ghosts > 0:
            ghost_samples = _as_list(evidence.get("ghost_samples"))
            details = {
                "ghosts_found": ghosts,
                "db_open_orders": int(md.get("db_open_orders") or 0),
                "ib_open_orders": int(md.get("ib_open_orders") or 0),
                "sample_count": len(ghost_samples),
            }
            ghost_severity = "CRITICAL"
            ghost_category = "runtime_ghost_orders"
            ghost_message = "Runtime 对账发现 ghost orders（DB有单但IB无单）"
            if ghost_samples:
                sample0 = _as_dict(ghost_samples[0])
                details["sample_order_id"] = str(sample0.get("order_id") or "")
                details["sample_strategy_id"] = str(sample0.get("strategy_id") or "")
                details["sample_symbol"] = str(sample0.get("symbol") or "")
                sample_order_id = str(sample0.get("order_id") or "").strip()
                if sample_order_id:
                    current = next(
                        (x for x in sys_orders if str(x.raw.get("client_order_id") or "").strip() == sample_order_id),
                        None,
                    )
                    if current is not None:
                        details["sample_current_system_status"] = current.status
                        if current.status in TERMINAL_ORDER_STATUSES:
                            ghost_severity = "HIGH"
                            ghost_category = "runtime_ghost_orders_resolved"
                            ghost_message = "Runtime 曾发现 ghost orders，但样本订单当前已终态"
            issues.append(
                SeverityIssue(
                    severity=ghost_severity,
                    category=ghost_category,
                    message=ghost_message,
                    strategy_id="system_reconciler",
                    details=details,
                )
            )
        if ib_pre > 0:
            issues.append(
                SeverityIssue(
                    severity="CRITICAL",
                    category="runtime_ib_presubmitted",
                    message="Runtime 对账发现 IB PreSubmitted 残留单",
                    strategy_id="system_reconciler",
                    details={
                        "ib_pre_submitted_orders": ib_pre,
                        "db_open_orders": int(md.get("db_open_orders") or 0),
                        "ib_open_orders": int(md.get("ib_open_orders") or 0),
                    },
                )
            )
        if mismatches > 0:
            issues.append(
                SeverityIssue(
                    severity="HIGH",
                    category="runtime_reconcile_mismatch",
                    message="Runtime 对账存在 mismatch",
                    strategy_id="system_reconciler",
                    details={"mismatches": mismatches},
                )
            )

    sys_pos = _group_positions(system_positions)
    ib_pos = _group_positions(ib_positions)
    all_symbols = sorted(set(sys_pos) | set(ib_pos))
    position_mismatches = []
    for sym in all_symbols:
        sq = sys_pos.get(sym, 0.0)
        bq = ib_pos.get(sym, 0.0)
        if abs(sq - bq) > qty_tolerance:
            position_mismatches.append({"symbol": sym, "system_qty": sq, "ibkr_qty": bq})
            issues.append(
                SeverityIssue(
                    severity="CRITICAL",
                    category="position_mismatch",
                    message="系统与 IBKR 持仓不一致",
                    symbol=sym,
                    details={"system_qty": sq, "ibkr_qty": bq},
                )
            )

    for sym, qty in ib_pos.items():
        if abs(qty) > qty_tolerance:
            issues.append(
                SeverityIssue(
                    severity="CRITICAL",
                    category="ibkr_not_flattened",
                    message="IBKR 侧收盘未清仓",
                    symbol=sym,
                    details={"ibkr_qty": qty},
                )
            )

    sys_acc = _account_metrics(system_account)
    ib_acc = _account_metrics(ib_account)
    pnl_diffs: Dict[str, float] = {}
    if ib_acc and not sys_acc:
        issues.append(
            SeverityIssue(
                severity="HIGH",
                category="system_account_snapshot_missing",
                message="系统账户快照缺失，跳过账户字段差异计算以避免把缺失误判为0",
                details={"ibkr_account_fields": sorted(ib_acc.keys())},
            )
        )
    for key in ("day_pnl", "realized_pnl", "unrealized_pnl", "equity"):
        if key in sys_acc and key in ib_acc:
            pnl_diffs[key] = sys_acc[key] - ib_acc[key]
            if abs(pnl_diffs[key]) > pnl_tolerance:
                issues.append(
                    SeverityIssue(
                        severity="HIGH",
                        category="account_pnl_mismatch",
                        message=f"账户字段 {key} 系统与 IBKR 不一致",
                        details={"system": sys_acc[key], "ibkr": ib_acc[key], "diff": pnl_diffs[key]},
                    )
                )

    strategy_summary: Dict[str, Dict[str, Any]] = {}
    for sid in sorted(enabled_set):
        strategy_summary.setdefault(
            sid,
            {
                "strategy_id": sid,
                "enabled_in_config": True,
                "system_orders": 0,
                "ibkr_matched_orders": 0,
                "telegram_matched_orders": 0,
                "open_or_presubmitted_in_ibkr": 0,
                "issues": 0,
            },
        )

    for order in sys_orders:
        st = strategy_summary.setdefault(
            order.strategy_id,
            {
                "strategy_id": order.strategy_id,
                "enabled_in_config": (order.strategy_id in enabled_set) if enabled_set else None,
                "system_orders": 0,
                "ibkr_matched_orders": 0,
                "telegram_matched_orders": 0,
                "open_or_presubmitted_in_ibkr": 0,
                "issues": 0,
            },
        )
        st["system_orders"] += 1
        if order.order_key in ib_by_key:
            st["ibkr_matched_orders"] += 1
            if ib_by_key[order.order_key].status in OPEN_ORDER_STATUSES:
                st["open_or_presubmitted_in_ibkr"] += 1
        if _resolve_telegram_match(tg_index, order):
            st["telegram_matched_orders"] += 1

    for issue in issues:
        sid = issue.strategy_id or "UNKNOWN"
        strategy_summary.setdefault(
            sid,
            {
                "strategy_id": sid,
                "enabled_in_config": (sid in enabled_set) if enabled_set else None,
                "system_orders": 0,
                "ibkr_matched_orders": 0,
                "telegram_matched_orders": 0,
                "open_or_presubmitted_in_ibkr": 0,
                "issues": 0,
            },
        )
        strategy_summary[sid]["issues"] += 1

    enabled_but_no_orders = []
    if check_strategy_config_warnings and len(sys_orders) > 0:
        for sid in sorted(enabled_set):
            st = strategy_summary.get(sid)
            if st and int(st.get("system_orders", 0)) == 0:
                enabled_but_no_orders.append(sid)
                issues.append(
                    SeverityIssue(
                        severity="LOW",
                        category="enabled_strategy_no_orders",
                        message="配置启用策略当日无订单",
                        strategy_id=sid,
                    )
                )

    issues_sorted = sorted(issues, key=lambda x: (_severity_rank(x.severity), x.category, x.order_key, x.symbol))
    highlights = [
        {
            "severity": x.severity,
            "category": x.category,
            "message": x.message,
            "order_key": x.order_key,
            "strategy_id": x.strategy_id,
            "symbol": x.symbol,
            "details": x.details or {},
        }
        for x in issues_sorted[:12]
    ]

    return {
        "trade_date": trade_date,
        "report_mode": mode,
        "summary": {
            "system_orders": len(sys_orders),
            "ibkr_orders": len(ib_norm_orders),
            "telegram_records": len(telegram_audit),
            "critical_issues": sum(1 for x in issues if x.severity == "CRITICAL"),
            "high_issues": sum(1 for x in issues if x.severity == "HIGH"),
            "medium_issues": sum(1 for x in issues if x.severity == "MEDIUM"),
            "low_issues": sum(1 for x in issues if x.severity == "LOW"),
            "ibkr_presubmitted_count": len(pre_submitted),
            "ibkr_open_order_count": len(ib_open_non_terminal),
            "position_mismatch_count": len(position_mismatches),
            "ibkr_non_flat_positions": sum(1 for _, qty in ib_pos.items() if abs(qty) > qty_tolerance),
            "enabled_strategy_count": len(enabled_set),
            "enabled_strategy_no_orders_count": len(enabled_but_no_orders),
            "runtime_recon_alert_count": recon_alert_count,
            "runtime_max_ghosts_found": max_ghosts,
            "runtime_max_ib_presubmitted": max_ib_presubmitted,
            "runtime_max_mismatches": max_mismatches,
            "broker_order_consistency_enabled": bool(enable_broker_order_consistency),
        },
        "enabled_strategies": sorted(enabled_set),
        "enabled_strategy_no_orders": enabled_but_no_orders,
        "highlights": highlights,
        "strategy_summary": sorted(strategy_summary.values(), key=lambda x: (-int(x.get("issues", 0)), str(x.get("strategy_id", "")))),
        "position_reconcile": {
            "system": sys_pos,
            "ibkr": ib_pos,
            "mismatches": position_mismatches,
        },
        "account_reconcile": {
            "system": sys_acc,
            "ibkr": ib_acc,
            "diff": pnl_diffs,
            "pnl_tolerance": pnl_tolerance,
        },
        "issues": [
            {
                "severity": x.severity,
                "category": x.category,
                "message": x.message,
                "order_key": x.order_key,
                "strategy_id": x.strategy_id,
                "symbol": x.symbol,
                "details": x.details or {},
            }
            for x in issues_sorted
        ],
        "generated_at": datetime.now(tz=ET).isoformat(),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    s = _as_dict(report.get("summary"))
    lines: List[str] = []
    lines.append(f"# 收盘复盘对账报告 ({report.get('trade_date', '')})")
    lines.append(f"- 模式: {report.get('report_mode', 'live')}")
    lines.append("")
    lines.append("## 高光问题")
    highlights = _as_list(report.get("highlights"))
    if not highlights:
        lines.append("- 无高优先级问题，三端基本一致。")
    else:
        for idx, row in enumerate(highlights, start=1):
            item = _as_dict(row)
            lines.append(
                f"{idx}. [{item.get('severity','')}] {item.get('message','')} | "
                f"category={item.get('category','')} | strategy={item.get('strategy_id','')} | "
                f"symbol={item.get('symbol','')} | order={item.get('order_key','')}"
            )
            details = _as_dict(item.get("details"))
            if details:
                lines.append(f"   details={json.dumps(details, ensure_ascii=False)}")

    lines.append("")
    lines.append("## 总览")
    lines.append(f"- 系统订单: {int(s.get('system_orders', 0))}")
    lines.append(f"- IBKR订单: {int(s.get('ibkr_orders', 0))}")
    lines.append(f"- Telegram记录: {int(s.get('telegram_records', 0))}")
    lines.append(f"- CRITICAL/HIGH/MEDIUM/LOW: {int(s.get('critical_issues', 0))}/{int(s.get('high_issues', 0))}/{int(s.get('medium_issues', 0))}/{int(s.get('low_issues', 0))}")
    lines.append(f"- IBKR PreSubmitted: {int(s.get('ibkr_presubmitted_count', 0))}")
    lines.append(f"- IBKR 开放订单(收盘后): {int(s.get('ibkr_open_order_count', 0))}")
    lines.append(f"- 持仓不一致symbol数: {int(s.get('position_mismatch_count', 0))}")
    lines.append(f"- IBKR 未清仓symbol数: {int(s.get('ibkr_non_flat_positions', 0))}")
    lines.append(f"- 配置启用策略数: {int(s.get('enabled_strategy_count', 0))}")
    lines.append(f"- 启用但无交易策略数: {int(s.get('enabled_strategy_no_orders_count', 0))}")
    lines.append(f"- Runtime对账告警数: {int(s.get('runtime_recon_alert_count', 0))}")
    lines.append(f"- Runtime最大Ghosts: {int(s.get('runtime_max_ghosts_found', 0))}")
    lines.append(f"- Runtime最大IB PreSubmitted: {int(s.get('runtime_max_ib_presubmitted', 0))}")
    lines.append(f"- Runtime最大Mismatches: {int(s.get('runtime_max_mismatches', 0))}")
    lines.append(f"- IB逐单一致性检查: {'开启' if bool(s.get('broker_order_consistency_enabled', True)) else '关闭(未提供IB订单输入)'}")

    enabled_sids = _as_list(report.get("enabled_strategies"))
    if enabled_sids:
        lines.append(f"- 启用策略: {', '.join(str(x) for x in enabled_sids)}")

    lines.append("")
    lines.append("## 策略维度")
    for row in _as_list(report.get("strategy_summary")):
        x = _as_dict(row)
        lines.append(
            f"- {x.get('strategy_id','UNKNOWN')}: system={int(x.get('system_orders',0))}, "
            f"ib_match={int(x.get('ibkr_matched_orders',0))}, tg_match={int(x.get('telegram_matched_orders',0))}, "
            f"ib_open={int(x.get('open_or_presubmitted_in_ibkr',0))}, issues={int(x.get('issues',0))}"
        )

    lines.append("")
    lines.append("## 账户盈亏")
    acc = _as_dict(report.get("account_reconcile"))
    lines.append(f"- system={json.dumps(_as_dict(acc.get('system')), ensure_ascii=False)}")
    lines.append(f"- ibkr={json.dumps(_as_dict(acc.get('ibkr')), ensure_ascii=False)}")
    lines.append(f"- diff={json.dumps(_as_dict(acc.get('diff')), ensure_ascii=False)}")

    lines.append("")
    lines.append("## 逐单异常")
    issues = _as_list(report.get("issues"))
    if not issues:
        lines.append("- 无异常")
    else:
        for row in issues[:200]:
            x = _as_dict(row)
            lines.append(
                f"- [{x.get('severity','')}] {x.get('category','')} | {x.get('message','')} | "
                f"strategy={x.get('strategy_id','')} | symbol={x.get('symbol','')} | order={x.get('order_key','')}"
            )

    return "\n".join(lines).strip() + "\n"


def _default_trade_date() -> str:
    return datetime.now(ET).date().isoformat()


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y"}


def _adhoc_ib_connect_allowed() -> bool:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="收盘复盘对账脚本：系统/IBKR/Telegram 三端一致性检查")
    ap.add_argument("--trade-date", default=_default_trade_date(), help="交易日期，格式 YYYY-MM-DD")
    ap.add_argument(
        "--strategy-config",
        action="append",
        default=[],
        help="策略配置文件路径，可重复传入；默认自动尝试 config.yml",
    )
    ap.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "live", "research"],
        help="对账模式：live=生产口径，research=实验口径(关闭策略启用告警)，auto=按输入路径自动判断",
    )
    ap.add_argument("--system-orders", default="", help="系统订单文件(json/jsonl)")
    ap.add_argument("--ib-orders", default="", help="IBKR订单文件(json/jsonl)")
    ap.add_argument("--telegram-audit", default="runtime/audit/telegram_push_audit.jsonl", help="Telegram推送审计jsonl")
    ap.add_argument("--system-positions", default="", help="系统持仓文件(json/jsonl)")
    ap.add_argument("--ib-positions", default="", help="IBKR持仓文件(json/jsonl)")
    ap.add_argument("--system-account", default="", help="系统账户快照(json)")
    ap.add_argument("--ib-account", default="", help="IBKR账户快照(json)")
    ap.add_argument("--output-dir", default="", help="输出目录，默认 runs/ai_trader/<trade-date>/reconcile")
    ap.add_argument("--pnl-tolerance", type=float, default=1.0, help="盈亏容差，默认1.0")
    ap.add_argument("--qty-tolerance", type=float, default=1e-6, help="数量容差")
    ap.add_argument(
        "--load-system-from-db",
        action="store_true",
        help="若系统订单/持仓/账户文件未提供，则自动从 PostgreSQL 拉取当日系统数据",
    )
    ap.add_argument(
        "--load-ib-live-snapshot",
        action="store_true",
        help="若 IB 订单/持仓/账户文件未提供，则实时连接 IB 并抓取快照",
    )
    ap.add_argument("--pg-dsn", default="", help="PostgreSQL DSN；不传则走 PG_URL 或默认本地参数")
    ap.add_argument(
        "--db-account-table",
        default="core.core_account_snapshot",
        help="账户快照表（best-effort，可为空字符串关闭）",
    )
    ap.add_argument("--ib-host", default=os.getenv("IBKR_HOST", "127.0.0.1"), help="IB host")
    ap.add_argument("--ib-port", type=int, default=int(os.getenv("IBKR_PORT", "4002")), help="IB port")
    ap.add_argument("--ib-client-id", type=int, default=int(os.getenv("IBKR_ADHOC_CLIENT_ID", "9902")), help="IB client id")
    ap.add_argument(
        "--ib-connect-timeout",
        type=float,
        default=float(os.getenv("IBKR_ADHOC_CONNECT_TIMEOUT", "30")),
        help="IBKR live snapshot API handshake timeout seconds.",
    )
    ap.add_argument(
        "--ib-include-completed-orders",
        action="store_true",
        help="IB live snapshot also pulls reqCompletedOrders (higher API payload; default off).",
    )
    ap.add_argument(
        "--connect-cooldown-sec",
        type=float,
        default=float(os.getenv("IBKR_ADHOC_CONNECT_COOLDOWN_SEC", "120")),
        help="临时 IBKR 连接冷却秒数，避免连续短连接冲击 Gateway/TWS",
    )
    ap.add_argument(
        "--load-ib-flex-report",
        action="store_true",
        help="若 IB live snapshot 无法提供当日历史，则使用 Flex Query / XML 补拉当日 IB 数据",
    )
    ap.add_argument("--ib-flex-token", default=os.getenv("IB_FLEX_TOKEN", ""), help="IB Flex token")
    ap.add_argument("--ib-flex-query-id", default=os.getenv("IB_FLEX_QUERY_ID", ""), help="IB Flex query id")
    ap.add_argument("--ib-flex-report-path", default="", help="本地 Flex XML 路径；有值时优先于在线下载")
    args = ap.parse_args()

    out_dir = Path(args.output_dir) if args.output_dir else Path("runs") / "ai_trader" / args.trade_date / "reconcile"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg_paths = _dedup_keep_order(
        [
            *(args.strategy_config or []),
            "config.yml",
        ]
    )
    enabled_strategies = load_enabled_strategy_ids_from_configs(cfg_paths)
    inferred_mode = infer_report_mode(
        args.system_orders,
        args.ib_orders,
        args.system_positions,
        args.ib_positions,
        args.system_account,
        args.ib_account,
    )
    final_mode = inferred_mode if args.mode == "auto" else args.mode

    sys_orders = _load_rows(args.system_orders, fallback_keys=("items", "orders", "rows", "data"))
    ib_orders = _load_rows(args.ib_orders, fallback_keys=("orders", "items", "rows", "data"))
    tg_rows = _load_rows(args.telegram_audit, fallback_keys=("items", "rows", "data"))
    sys_pos = _load_rows(args.system_positions, fallback_keys=("positions", "items", "rows", "data"))
    ib_pos = _load_rows(args.ib_positions, fallback_keys=("positions", "items", "rows", "data"))

    sys_acc_rows = _load_rows(args.system_account, fallback_keys=("account", "items", "rows", "data"))
    ib_acc_rows = _load_rows(args.ib_account, fallback_keys=("account", "items", "rows", "data"))

    if args.load_system_from_db and (not sys_orders or not sys_pos or not sys_acc_rows):
        dsn = _build_pg_dsn(args.pg_dsn)
        db_payload = load_system_payload_from_db(
            trade_date=args.trade_date,
            dsn=dsn,
            account_table=str(args.db_account_table or "").strip(),
        )
        if not sys_orders:
            sys_orders = db_payload.get("orders", [])
        if not sys_pos:
            sys_pos = db_payload.get("positions", [])
        if not sys_acc_rows:
            sys_acc_rows = db_payload.get("account", [])

    if args.load_ib_live_snapshot and not _adhoc_ib_connect_allowed():
        print(
            "[BLOCKED] --load-ib-live-snapshot blocked by IBKR_DISABLE_ADHOC_CONNECT=true."
        )
        return 2

    if args.load_ib_live_snapshot and (not ib_orders or not ib_pos or not ib_acc_rows):
        connect_guard = None
        try:
            connect_guard = _acquire_adhoc_ib_connect_guard(float(args.connect_cooldown_sec))
            ib_payload = load_ib_live_snapshot(
                host=str(args.ib_host or "127.0.0.1"),
                port=int(args.ib_port),
                client_id=int(args.ib_client_id),
                connect_timeout=float(args.ib_connect_timeout),
                trade_date=args.trade_date,
                include_completed_orders=bool(args.ib_include_completed_orders),
            )
        finally:
            _release_adhoc_ib_connect_guard(connect_guard)
        if not ib_orders:
            ib_orders = ib_payload.get("orders", [])
        if not ib_pos:
            ib_pos = ib_payload.get("positions", [])
        if not ib_acc_rows:
            ib_acc_rows = ib_payload.get("account", [])

    should_try_flex = bool(args.load_ib_flex_report or ((not ib_orders) and (args.ib_flex_report_path or (args.ib_flex_token and args.ib_flex_query_id))))
    if should_try_flex and (not ib_orders or not ib_pos or not ib_acc_rows):
        flex_xml_out = out_dir / "ib_flex_statement.xml" if not args.ib_flex_report_path else ""
        flex_payload = load_ib_flex_snapshot(
            trade_date=args.trade_date,
            token=str(args.ib_flex_token or ""),
            query_id=str(args.ib_flex_query_id or ""),
            report_path=str(args.ib_flex_report_path or ""),
            save_xml_path=str(flex_xml_out),
        )
        if not ib_orders:
            ib_orders = flex_payload.get("orders", [])
        if not ib_pos:
            ib_pos = flex_payload.get("positions", [])
        if not ib_acc_rows:
            ib_acc_rows = flex_payload.get("account", [])

    report = build_reconciliation_report(
        trade_date=args.trade_date,
        system_orders=sys_orders,
        ib_orders=ib_orders,
        telegram_audit=tg_rows,
        system_positions=sys_pos,
        ib_positions=ib_pos,
        system_account=sys_acc_rows[0] if sys_acc_rows else {},
        ib_account=ib_acc_rows[0] if ib_acc_rows else {},
        enabled_strategies=enabled_strategies,
        report_mode=final_mode,
        enable_broker_order_consistency=bool(ib_orders),
        pnl_tolerance=float(args.pnl_tolerance),
        qty_tolerance=float(args.qty_tolerance),
    )

    json_path = out_dir / "eod_reconcile_report.json"
    md_path = out_dir / "eod_reconcile_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    print(f"[OK] report_json={json_path}")
    print(f"[OK] report_md={md_path}")
    print(
        "[SUMMARY] "
        f"critical={report['summary']['critical_issues']} "
        f"high={report['summary']['high_issues']} "
        f"presubmitted={report['summary']['ibkr_presubmitted_count']} "
        f"not_flat={report['summary']['ibkr_non_flat_positions']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
