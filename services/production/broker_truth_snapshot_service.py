from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract


async def build_live_broker_truth_snapshot(
    *,
    ib_adapter: Any,
    generated_at: datetime,
    account: str | None = None,
    max_open_orders: int = 500,
    max_positions: int = 500,
) -> dict[str, Any]:
    """Build a fresh broker-backed snapshot from the live IB adapter surface.

    This is intentionally adapter-shape tolerant because production code uses
    both direct ib_insync objects and small wrapper DTOs.  Failure to read live
    broker truth is represented as no-go data, not as a clean DB projection.
    """

    account_s = str(account or getattr(ib_adapter, "account", "") or os.getenv("IB_ACCOUNT", "") or "").strip()
    account_invalid = not account_s or account_s.upper() == "UNKNOWN_ACCOUNT"
    try:
        positions_raw = await _call_adapter(ib_adapter, "positions")
        open_orders_raw = await _call_adapter(ib_adapter, "open_orders", force_refresh=True)
    except Exception as exc:
        return {
            "snapshot_id": f"live_broker_truth:{generated_at.isoformat()}:{os.getpid()}",
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "generated_at": generated_at.isoformat(),
            "source": "unavailable",
            "broker_truth_backed": False,
            "source_process_pid": os.getpid(),
            "account": account_s,
            "account_invalid": account_invalid,
            "lookup_failed": True,
            "stale": False,
            "go_no_go": "no_go",
            "error": str(exc),
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "positions": [],
            "open_orders": [],
        }

    positions = []
    for item in list(positions_raw or [])[: max(0, int(max_positions or 500))]:
        row = _normalize_broker_position(item)
        if not row:
            continue
        if account_s and row.get("account") and row.get("account") != account_s:
            continue
        if abs(float(row.get("qty") or 0.0)) <= 1e-9:
            continue
        positions.append(row)

    open_orders = []
    for item in list(open_orders_raw or [])[: max(0, int(max_open_orders or 500))]:
        row = _normalize_broker_open_order(item)
        if not row:
            continue
        if account_s and row.get("account") and row.get("account") != account_s:
            continue
        status = str(row.get("status") or "").strip().upper()
        if status in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "INACTIVE"}:
            continue
        open_orders.append(row)

    return {
        "snapshot_id": f"live_broker_truth:{generated_at.isoformat()}:{os.getpid()}",
        "artifact_type": "live_broker_truth_snapshot",
        "artifact_present": True,
        "generated_at": generated_at.isoformat(),
        "source": "live_broker",
        "broker_truth_backed": not account_invalid,
        "source_process_pid": os.getpid(),
        "account": account_s,
        "account_invalid": account_invalid,
        "lookup_failed": False,
        "stale": False,
        "go_no_go": "no_go" if account_invalid else "go",
        "error": "broker_account_scope_invalid" if account_invalid else "",
        "broker_position_count": len(positions),
        "broker_open_order_count": len(open_orders),
        "positions": positions,
        "open_orders": open_orders,
    }


async def build_live_broker_truth_snapshot_from_manager(
    *,
    generated_at: datetime,
    account: str | None = None,
    manager: Any | None = None,
    host: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    """Build broker truth through the IB connection manager owner.

    This path is read-only and intentionally goes through the manager-owned
    exec IB handle.  It must not call the legacy direct adapter reconnect path or use the
    legacy direct adapter reconnect path.
    """

    try:
        if manager is None:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            manager = get_ib_connection_manager()
        handle = await manager.get_exec_ib_handle(host=host, port=port, account=account)
        ib = getattr(handle, "ib", None)
        if ib is None:
            raise RuntimeError("manager_exec_ib_handle_unavailable")

        async def _open_orders(_self: Any, *, force_refresh: bool = False) -> list[Any]:
            if force_refresh:
                if hasattr(ib, "reqOpenOrdersAsync"):
                    await ib.reqOpenOrdersAsync()
                elif hasattr(ib, "reqAllOpenOrdersAsync"):
                    await ib.reqAllOpenOrdersAsync()
            if hasattr(ib, "openTrades"):
                return list(ib.openTrades() or [])
            if hasattr(ib, "openOrders"):
                return list(ib.openOrders() or [])
            return []

        adapter = type(
            "ManagerOwnedBrokerTruthReader",
            (),
            {
                "account": str(account or "").strip(),
                "positions": lambda _self: list(ib.positions() or []) if hasattr(ib, "positions") else [],
                "open_orders": _open_orders,
            },
        )()
        snapshot = await build_live_broker_truth_snapshot(
            ib_adapter=adapter,
            generated_at=generated_at,
            account=account,
        )
        snapshot.update(
            {
                "source": "live_broker",
                "connection_role": getattr(handle, "role", "exec"),
                "ib_client_id": int(getattr(handle, "client_id", 0) or 0),
                "ib_connection_epoch": int(getattr(handle, "epoch", 0) or 0),
            }
        )
        return snapshot
    except Exception as exc:
        account_s = str(account or os.getenv("IB_ACCOUNT", "") or "").strip()
        return {
            "snapshot_id": f"live_broker_truth:{generated_at.isoformat()}:{os.getpid()}",
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "generated_at": generated_at.isoformat(),
            "source": "unavailable",
            "broker_truth_backed": False,
            "source_process_pid": os.getpid(),
            "account": account_s,
            "account_invalid": not account_s or account_s.upper() == "UNKNOWN_ACCOUNT",
            "lookup_failed": True,
            "stale": False,
            "go_no_go": "no_go",
            "error": str(exc) or "manager_handle_unavailable",
            "errors": [str(exc) or "manager_handle_unavailable"],
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "positions": [],
            "open_orders": [],
        }


async def query_broker_truth_snapshot_from_broker(
    *,
    pg: Any,
    ib_adapter: Any,
    generated_at: datetime,
    strategy_ids: list[str],
    symbols: list[str],
    account: str | None = None,
    require_live_broker_truth: bool = True,
) -> dict[str, Any]:
    live_snapshot = await build_live_broker_truth_snapshot(
        ib_adapter=ib_adapter,
        generated_at=generated_at,
        account=account,
    )
    return await query_broker_truth_snapshot(
        pg=pg,
        generated_at=generated_at,
        strategy_ids=strategy_ids,
        symbols=symbols,
        live_broker_snapshot=live_snapshot,
        require_live_broker_truth=require_live_broker_truth,
    )


async def query_broker_truth_snapshot(
    *,
    pg: Any,
    generated_at: datetime,
    strategy_ids: list[str],
    symbols: list[str],
    live_broker_snapshot: dict[str, Any] | None = None,
    require_live_broker_truth: bool = True,
) -> dict[str, Any]:
    open_orders = await pg.fetchall(
        """
        SELECT strategy_id, symbol, state, COUNT(*)::int AS count
        FROM core.core_order
        WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL')
          AND ($1::text[] IS NULL OR strategy_id = ANY($1::text[]))
          AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
        GROUP BY strategy_id, symbol, state
        ORDER BY strategy_id, symbol, state
        LIMIT 100
        """,
        strategy_ids or None,
        symbols or None,
    )
    active_quarantines = await pg.fetchall(
        """
        SELECT symbol, reason, COUNT(*)::int AS count
        FROM core.core_order_quarantine
        WHERE status = 'active'
          AND ($1::text[] IS NULL OR symbol = ANY($1::text[]))
        GROUP BY symbol, reason
        ORDER BY symbol, reason
        LIMIT 100
        """,
        symbols or None,
    )
    nonzero_ledger = await pg.fetchall(
        """
        SELECT account_id, strategy_id, symbol, lot_id, qty::float AS qty, notional_sum::float AS notional_sum
        FROM core.position_ledger_current
        WHERE abs(qty) > 1e-9
          AND ($1::text[] IS NULL OR strategy_id = ANY($1::text[]))
          AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
        ORDER BY strategy_id, symbol, account_id, lot_id
        LIMIT 100
        """,
        strategy_ids or None,
        symbols or None,
    )
    nonzero_core_positions = await pg.fetchall(
        """
        SELECT strategy_id, symbol, pos_qty::float AS qty, avg_px::float AS avg_px, updated_at
        FROM core.core_position
        WHERE abs(pos_qty) > 1e-9
          AND ($1::text[] IS NULL OR strategy_id = ANY($1::text[]))
          AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
        ORDER BY strategy_id, symbol
        LIMIT 100
        """,
        strategy_ids or None,
        symbols or None,
    )
    projection_divergences = await pg.fetchall(
        """
        WITH ledger_net AS (
            SELECT strategy_id, symbol, SUM(qty)::float AS ledger_qty
            FROM core.position_ledger_current
            WHERE ($1::text[] IS NULL OR strategy_id = ANY($1::text[]))
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
            GROUP BY strategy_id, symbol
        ),
        core_net AS (
            SELECT strategy_id, symbol, pos_qty::float AS core_qty
            FROM core.core_position
            WHERE ($1::text[] IS NULL OR strategy_id = ANY($1::text[]))
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
        )
        SELECT COALESCE(ledger_net.strategy_id, core_net.strategy_id) AS strategy_id,
               COALESCE(ledger_net.symbol, core_net.symbol) AS symbol,
               COALESCE(ledger_net.ledger_qty, 0)::float AS ledger_qty,
               COALESCE(core_net.core_qty, 0)::float AS core_qty,
               (COALESCE(ledger_net.ledger_qty, 0) - COALESCE(core_net.core_qty, 0))::float AS qty_delta
        FROM ledger_net
        FULL OUTER JOIN core_net
          ON core_net.strategy_id = ledger_net.strategy_id
         AND core_net.symbol = ledger_net.symbol
        WHERE abs(COALESCE(ledger_net.ledger_qty, 0) - COALESCE(core_net.core_qty, 0)) > 1e-9
        ORDER BY strategy_id, symbol
        LIMIT 100
        """,
        strategy_ids or None,
        symbols or None,
    )

    open_order_rows = [_json_safe_row(row) for row in open_orders or []]
    quarantine_rows = [_json_safe_row(row) for row in active_quarantines or []]
    ledger_rows = [_json_safe_row(row) for row in nonzero_ledger or []]
    core_rows = [_json_safe_row(row) for row in nonzero_core_positions or []]
    divergence_rows = [_json_safe_row(row) for row in projection_divergences or []]
    dirty_lot_rows = [
        row
        for row in ledger_rows
        if not str(row.get("lot_id") or "").strip()
        or str(row.get("account_id") or "").strip().upper() in {"", "UNKNOWN_ACCOUNT"}
    ]
    live = dict(live_broker_snapshot or {})
    live_validation = _validate_live_broker_snapshot(live)
    live_available = bool(live_validation["available"])
    live_lookup_failed = bool(live_validation["lookup_failed"])
    broker_truth_backed = bool(live_validation["valid"])
    dirty = bool(quarantine_rows or divergence_rows or dirty_lot_rows)
    blockers = _blockers(
        quarantine_count=len(quarantine_rows),
        projection_divergence_count=len(divergence_rows),
        dirty_lot_row_count=len(dirty_lot_rows),
        require_live_broker_truth=require_live_broker_truth,
        broker_truth_backed=broker_truth_backed,
        live_lookup_failed=live_lookup_failed,
    )
    go = not dirty and not blockers
    return {
        "schema_version": 2,
        "artifact_type": "broker_truth_snapshot",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "enabled": True,
        "lookup_failed": False,
        "go_no_go": "go" if go else "no_go",
        "truth_source": "live_broker_snapshot_plus_db_projection" if broker_truth_backed else "db_projection_only",
        "require_live_broker_truth": bool(require_live_broker_truth),
        "live_broker_truth_available": bool(live_available),
        "broker_truth_backed": bool(broker_truth_backed),
        "live_broker_lookup_failed": bool(live_lookup_failed),
        "live_broker_position_count": int(live.get("broker_position_count") or 0) if live else None,
        "live_broker_snapshot_generated_at": live.get("generated_at"),
        "live_broker_snapshot_age_sec": live.get("age_sec"),
        "live_broker_snapshot_path": live.get("artifact_path"),
        "live_broker_snapshot_validation_errors": live_validation["errors"],
        "live_broker_account": live.get("account"),
        "live_broker_open_order_count": int(live.get("broker_open_order_count") or 0) if live else None,
        "broker_truth_projection_clean": not dirty,
        "order_truth_active_quarantine_count": sum(int(row.get("count") or 0) for row in quarantine_rows),
        "open_core_order_count": sum(int(row.get("count") or 0) for row in open_order_rows),
        "nonzero_ledger_position_count": len(ledger_rows),
        "nonzero_core_position_count": len(core_rows),
        "projection_divergence_count": len(divergence_rows),
        "dirty_lot_position_count": len(dirty_lot_rows),
        "open_core_orders": open_order_rows,
        "active_quarantines": quarantine_rows,
        "nonzero_ledger_positions": ledger_rows,
        "nonzero_core_positions": core_rows,
        "projection_divergences": divergence_rows,
        "dirty_lot_positions": dirty_lot_rows,
        "blockers": blockers,
        "clear_conditions": [] if go else _clear_conditions(blockers),
    }


def disabled_broker_truth_snapshot_status(*, generated_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "artifact_type": "broker_truth_snapshot",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "enabled": False,
        "lookup_failed": False,
        "go_no_go": "go",
        "truth_source": "disabled",
        "require_live_broker_truth": False,
        "live_broker_truth_available": False,
        "broker_truth_backed": False,
        "live_broker_lookup_failed": False,
        "live_broker_position_count": None,
        "live_broker_snapshot_generated_at": None,
        "live_broker_snapshot_age_sec": None,
        "live_broker_snapshot_path": None,
        "broker_truth_projection_clean": True,
        "order_truth_active_quarantine_count": 0,
        "open_core_order_count": 0,
        "nonzero_ledger_position_count": 0,
        "nonzero_core_position_count": 0,
        "projection_divergence_count": 0,
        "open_core_orders": [],
        "active_quarantines": [],
        "nonzero_ledger_positions": [],
        "nonzero_core_positions": [],
        "projection_divergences": [],
        "blockers": [],
        "clear_conditions": [],
    }


def failed_broker_truth_snapshot_status(*, generated_at: datetime, error: str) -> dict[str, Any]:
    status = disabled_broker_truth_snapshot_status(generated_at=generated_at)
    status.update(
        {
            "enabled": True,
            "lookup_failed": True,
            "go_no_go": "no_go",
            "broker_truth_projection_clean": False,
            "blockers": ["broker_truth_snapshot_lookup_failed"],
            "clear_conditions": ["restore_broker_truth_snapshot_lookup"],
            "error": error,
        }
    )
    return status


def write_broker_truth_snapshot_artifact(*, repo_root: str | Path, status: dict[str, Any]) -> Path:
    path = Path(repo_root) / "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_live_broker_truth_snapshot_artifact(*, repo_root: str | Path, status: dict[str, Any]) -> Path:
    path = Path(repo_root) / "runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


async def _call_adapter(adapter: Any, name: str, **kwargs: Any) -> Any:
    fn = getattr(adapter, name, None)
    if callable(fn):
        try:
            value = fn(**kwargs)
        except TypeError:
            value = fn()
        if hasattr(value, "__await__"):
            return await value
        return value
    ib = getattr(adapter, "ib", None)
    fn = getattr(ib, name, None)
    if callable(fn):
        value = fn()
        if hasattr(value, "__await__"):
            return await value
        return value
    raise RuntimeError(f"broker_adapter_{name}_unavailable")


def _normalize_broker_position(item: Any) -> dict[str, Any]:
    try:
        contract = getattr(item, "contract", None)
        if isinstance(item, dict):
            symbol = str(item.get("symbol") or "").strip().upper()
            qty = float(item.get("qty", item.get("position", 0.0)) or 0.0)
            account = str(item.get("account") or "").strip()
            market_ref = item.get("market_ref_px", item.get("marketPrice", item.get("avgCost")))
        else:
            symbol = _contract_symbol(contract)
            qty = float(getattr(item, "position", getattr(item, "qty", 0.0)) or 0.0)
            account = str(getattr(item, "account", "") or "").strip()
            market_ref = (
                getattr(item, "marketPrice", None)
                or getattr(item, "market_price", None)
                or getattr(item, "last", None)
                or getattr(item, "avgCost", None)
                or getattr(item, "averageCost", None)
            )
        row = {"account": account, "symbol": symbol, "qty": qty}
        try:
            if market_ref is not None and float(market_ref) > 0:
                row["market_ref_px"] = float(market_ref)
        except Exception:
            pass
        return row if symbol else {}
    except Exception:
        return {}


def _normalize_broker_open_order(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        row = dict(item)
    else:
        order = getattr(item, "order", None) or item
        status = getattr(item, "orderStatus", None)
        contract = getattr(item, "contract", None)
        row = {
            "account": getattr(order, "account", getattr(item, "account", "")),
            "symbol": getattr(item, "symbol", None) or _contract_symbol(contract),
            "side": getattr(order, "action", getattr(item, "side", "")),
            "qty": getattr(order, "totalQuantity", getattr(item, "qty", None)),
            "broker_order_id": getattr(order, "orderId", getattr(item, "broker_order_id", None)),
            "perm_id": getattr(order, "permId", getattr(item, "perm_id", None)),
            "order_ref": getattr(order, "orderRef", getattr(item, "order_ref", "")),
            "status": getattr(status, "status", getattr(item, "status", "")),
            "order_type": getattr(order, "orderType", getattr(item, "order_type", "")),
        }
    row["symbol"] = str(row.get("symbol") or "").strip().upper()
    row["account"] = str(row.get("account") or "").strip()
    row["side"] = str(row.get("side") or row.get("action") or "").strip().upper()
    row["order_ref"] = str(row.get("order_ref") or row.get("orderRef") or "").strip()
    return row if row["symbol"] else {}


def _contract_symbol(contract: Any) -> str:
    if contract is None:
        return ""
    sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
    if sec_type in {"OPT", "FOP"}:
        return str(internal_opt_symbol_from_ib_contract(contract) or "").strip().upper()
    return str(getattr(contract, "symbol", "") or "").strip().upper()


def _blockers(
    *,
    quarantine_count: int,
    projection_divergence_count: int,
    dirty_lot_row_count: int = 0,
    require_live_broker_truth: bool = False,
    broker_truth_backed: bool = True,
    live_lookup_failed: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if require_live_broker_truth and not broker_truth_backed:
        blockers.append("broker_truth_live_snapshot_missing")
    if live_lookup_failed:
        blockers.append("broker_truth_live_lookup_failed")
    if quarantine_count > 0:
        blockers.append("broker_truth_active_order_quarantine")
    if projection_divergence_count > 0:
        blockers.append("broker_truth_position_projection_divergence")
    if dirty_lot_row_count > 0:
        blockers.append("broker_truth_dirty_ledger_lot_identity")
    return blockers


def _validate_live_broker_snapshot(live: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if not live:
        return {"available": False, "valid": False, "lookup_failed": False, "errors": ["missing"]}
    if bool(live.get("artifact_present", True)) is not True:
        errors.append("artifact_not_present")
    if bool(live.get("stale")):
        errors.append("stale")
    if str(live.get("artifact_type") or "") != "live_broker_truth_snapshot":
        errors.append("invalid_artifact_type")
    if bool(live.get("lookup_failed")):
        errors.append("lookup_failed")
    if str(live.get("go_no_go") or "").strip().lower() not in {"go", ""}:
        errors.append("snapshot_no_go")
    try:
        int(live.get("source_process_pid"))
    except Exception:
        errors.append("missing_source_process_pid")
    if not str(live.get("generated_at") or "").strip():
        errors.append("missing_generated_at")
    account = str(live.get("account") or "").strip()
    if "account" not in live:
        errors.append("missing_account")
    elif not account or account.upper() == "UNKNOWN_ACCOUNT":
        errors.append("invalid_account")
    positions = live.get("positions")
    open_orders = live.get("open_orders")
    if not isinstance(positions, list):
        errors.append("positions_not_list")
    if not isinstance(open_orders, list):
        errors.append("open_orders_not_list")
    if isinstance(positions, list) and int(live.get("broker_position_count") or 0) != len(positions):
        errors.append("broker_position_count_mismatch")
    if isinstance(open_orders, list) and int(live.get("broker_open_order_count") or 0) != len(open_orders):
        errors.append("broker_open_order_count_mismatch")
    lookup_failed = bool(live.get("lookup_failed"))
    available = bool(live) and bool(live.get("artifact_present", True)) and not bool(live.get("stale"))
    return {
        "available": available,
        "valid": available and not errors,
        "lookup_failed": lookup_failed,
        "errors": errors,
    }


def _json_safe_row(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            out[str(key)] = value.isoformat()
        else:
            out[str(key)] = value
    return out


def _clear_conditions(blockers: list[str]) -> list[str]:
    conditions: list[str] = []
    if "broker_truth_live_snapshot_missing" in blockers or "broker_truth_live_lookup_failed" in blockers:
        conditions.append("produce_fresh_live_broker_truth_snapshot_from_current_process")
    if (
        "broker_truth_active_order_quarantine" in blockers
        or "broker_truth_position_projection_divergence" in blockers
        or "broker_truth_dirty_ledger_lot_identity" in blockers
    ):
        conditions.append("resolve_order_quarantine_and_position_projection_divergence")
    return conditions
