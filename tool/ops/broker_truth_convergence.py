#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ib_insync import IB

from libs.config import config
from libs.db.postgres import PostgresDB
from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract
from services.reconciliation.broker_truth_convergence import BrokerTruthConvergenceService

SNAPSHOT_SCHEMA_VERSION = 1


async def _connect_pg() -> PostgresDB:
    pg = PostgresDB()
    await pg.connect()
    return pg


async def inspect_active() -> dict[str, Any]:
    pg = await _connect_pg()
    try:
        quarantines = await pg.fetchall(
            """
            SELECT symbol, reason, COUNT(*) AS active_count, MAX(observed_at) AS last_observed_at
            FROM core.core_order_quarantine
            WHERE status = 'active'
            GROUP BY symbol, reason
            ORDER BY symbol ASC, reason ASC
            """
        )
        ledger = await pg.fetchall(
            """
            SELECT symbol, COALESCE(SUM(qty), 0) AS ledger_qty
            FROM core.position_ledger_current
            GROUP BY symbol
            HAVING ABS(COALESCE(SUM(qty), 0)) > 0.000001
            ORDER BY symbol ASC
            """
        )
        return {
            "active_quarantines": [dict(row) for row in (quarantines or [])],
            "nonzero_ledger_positions": [dict(row) for row in (ledger or [])],
        }
    finally:
        await pg.close()


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


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


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _broker_position_rows(positions: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pos in positions or []:
        contract = getattr(pos, "contract", None)
        symbol = internal_opt_symbol_from_ib_contract(contract) or str(getattr(contract, "symbol", "") or "").strip().upper()
        qty = float(getattr(pos, "position", 0.0) or 0.0)
        rows.append(
            {
                "symbol": symbol,
                "qty": qty,
                "secType": str(getattr(contract, "secType", "") or ""),
                "localSymbol": str(getattr(contract, "localSymbol", "") or ""),
                "conId": int(getattr(contract, "conId", 0) or 0),
            }
        )
    rows.sort(key=lambda item: (str(item.get("symbol") or ""), int(item.get("conId") or 0)))
    return rows


def _target_view_from_rows(rows: list[dict[str, Any]], symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    by_symbol = {str(row.get("symbol") or "").upper(): dict(row) for row in rows}
    result: dict[str, dict[str, Any]] = {}
    for symbol in sorted({_normalize_symbol(item) for item in symbols if _normalize_symbol(item)}):
        result[symbol] = by_symbol.get(symbol, {"symbol": symbol, "qty": 0.0, "missing_from_broker_positions": True})
    return result


def _parse_snapshot_ts(snapshot: dict[str, Any]) -> datetime:
    raw = str(snapshot.get("generated_at") or "")
    if not raw:
        raise ValueError("snapshot_missing_generated_at")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _snapshot_age_sec(snapshot: dict[str, Any], *, now: Optional[datetime] = None) -> float:
    ts = _parse_snapshot_ts(snapshot)
    now_dt = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    return (now_dt - ts).total_seconds()


def _broker_qty_from_snapshot(snapshot: dict[str, Any], *, symbol: str) -> float:
    sym = _normalize_symbol(symbol)
    targets = snapshot.get("target_positions") or {}
    if sym in targets:
        return float((targets.get(sym) or {}).get("qty") or 0.0)
    for row in snapshot.get("all_nonzero_positions") or []:
        if _normalize_symbol(row.get("symbol")) == sym:
            return float(row.get("qty") or 0.0)
    return 0.0


async def live_snapshot(
    *,
    symbols: list[str],
    host: str,
    port: int,
    client_id: int,
    timeout: float,
    connect_cooldown_sec: float,
    output: Optional[Path] = None,
) -> dict[str, Any]:
    if _env_truthy("IBKR_DISABLE_ADHOC_CONNECT"):
        raise RuntimeError("IBKR ad-hoc connection blocked by IBKR_DISABLE_ADHOC_CONNECT=true")
    ib = IB()
    guard = None
    try:
        guard = _acquire_adhoc_ib_connect_guard(connect_cooldown_sec)
        await ib.connectAsync(host, int(port), clientId=int(client_id), timeout=float(timeout), readonly=True)
        rows = _broker_position_rows(ib.positions() or [])
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "ib_insync.positions",
            "host": host,
            "port": int(port),
            "client_id": int(client_id),
            "readonly": True,
            "target_positions": _target_view_from_rows(rows, symbols),
            "all_nonzero_positions": [row for row in rows if abs(float(row.get("qty") or 0.0)) > 1e-12],
        }
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        return payload
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        finally:
            _release_adhoc_ib_connect_guard(guard)


async def _current_ledger_qty(pg: PostgresDB, *, symbol: str) -> float:
    row = await pg.fetchone(
        """
        SELECT COALESCE(SUM(qty), 0) AS ledger_qty
        FROM core.position_ledger_current
        WHERE symbol = $1
        """,
        _normalize_symbol(symbol),
    )
    if not row:
        return 0.0
    return float(row.get("ledger_qty") if hasattr(row, "get") else row["ledger_qty"] or 0.0)


async def apply_broker_flat(*, symbol: str, ledger_qty: float, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise SystemExit("--confirm is required for apply-broker-flat")
    pg = await _connect_pg()
    try:
        svc = BrokerTruthConvergenceService(pg=pg)
        result = await svc.converge_position_mismatch(
            symbol=symbol,
            ib_qty=0.0,
            ledger_qty=float(ledger_qty),
            reason="operator_apply_broker_flat",
        )
        return {"ok": result.ok, "reason": result.reason, "metadata": result.metadata}
    finally:
        await pg.close()


async def apply_snapshot_broker_flat(
    *,
    symbol: str,
    snapshot_path: Path,
    max_age_sec: float,
    confirm: bool,
) -> dict[str, Any]:
    if not confirm:
        raise SystemExit("--confirm is required for apply-snapshot-broker-flat")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    age_sec = _snapshot_age_sec(snapshot)
    if age_sec < -5:
        raise SystemExit(f"snapshot generated_at is in the future: age_sec={age_sec:.1f}")
    if age_sec > float(max_age_sec):
        raise SystemExit(f"snapshot too old: age_sec={age_sec:.1f} max_age_sec={float(max_age_sec):.1f}")
    ib_qty = _broker_qty_from_snapshot(snapshot, symbol=symbol)
    if abs(float(ib_qty)) > 0.001:
        raise SystemExit(f"broker is not flat for {symbol}: ib_qty={ib_qty}")
    pg = await _connect_pg()
    try:
        ledger_qty = await _current_ledger_qty(pg, symbol=symbol)
        svc = BrokerTruthConvergenceService(pg=pg)
        result = await svc.converge_position_mismatch(
            symbol=symbol,
            ib_qty=0.0,
            ledger_qty=float(ledger_qty),
            reason="operator_apply_snapshot_broker_flat",
        )
        return {
            "ok": result.ok,
            "reason": result.reason,
            "metadata": {
                **result.metadata,
                "snapshot_path": str(snapshot_path),
                "snapshot_age_sec": age_sec,
                "ledger_qty_read_from_db": ledger_qty,
            },
        }
    finally:
        await pg.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Broker truth convergence operations")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inspect-active", help="Show active broker-truth quarantine and nonzero ledger positions")
    snap = sub.add_parser("live-snapshot", help="Read current IB positions in readonly mode and save broker truth snapshot")
    snap.add_argument("--symbol", action="append", default=[], help="Symbol to include in target_positions; repeatable")
    snap.add_argument("--host", default=str(config.ib_config["host"]))
    snap.add_argument("--port", type=int, default=int(config.ib_config["port"]))
    snap.add_argument("--client-id", type=int, default=int(os.getenv("IBKR_ADHOC_CLIENT_ID", "9917")))
    snap.add_argument("--connect-timeout", type=float, default=float(os.getenv("IBKR_ADHOC_CONNECT_TIMEOUT", "12")))
    snap.add_argument(
        "--connect-cooldown-sec",
        type=float,
        default=float(os.getenv("IBKR_ADHOC_CONNECT_COOLDOWN_SEC", "120")),
    )
    snap.add_argument("--output", type=Path)
    apply = sub.add_parser("apply-broker-flat", help="Append an audited adjustment to make a symbol broker-flat")
    apply.add_argument("--symbol", required=True)
    apply.add_argument("--ledger-qty", required=True, type=float)
    apply.add_argument("--confirm", action="store_true")
    apply_snapshot = sub.add_parser(
        "apply-snapshot-broker-flat",
        help="Append an audited adjustment only when a fresh broker snapshot proves the symbol is flat",
    )
    apply_snapshot.add_argument("--symbol", required=True)
    apply_snapshot.add_argument("--snapshot", required=True, type=Path)
    apply_snapshot.add_argument("--max-age-sec", type=float, default=300.0)
    apply_snapshot.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.cmd == "inspect-active":
        payload = await inspect_active()
    elif args.cmd == "live-snapshot":
        payload = await live_snapshot(
            symbols=args.symbol,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            timeout=args.connect_timeout,
            connect_cooldown_sec=args.connect_cooldown_sec,
            output=args.output,
        )
    elif args.cmd == "apply-broker-flat":
        payload = await apply_broker_flat(symbol=args.symbol, ledger_qty=args.ledger_qty, confirm=args.confirm)
    elif args.cmd == "apply-snapshot-broker-flat":
        payload = await apply_snapshot_broker_flat(
            symbol=args.symbol,
            snapshot_path=args.snapshot,
            max_age_sec=args.max_age_sec,
            confirm=args.confirm,
        )
    else:
        raise SystemExit(f"unknown command: {args.cmd}")
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
