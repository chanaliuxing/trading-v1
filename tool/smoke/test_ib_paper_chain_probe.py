#!/usr/bin/env python3
"""
IB Paper chain probe for multi-strategy isolation.

Default mode is non-invasive:
- No order placement
- Reads IB completed orders/fills
- Reads Postgres core_order_event/core_order
- Detects suspicious cross-strategy close patterns

Usage:
  ./venv/bin/python tool/smoke/test_ib_paper_chain_probe.py
  ./venv/bin/python tool/smoke/test_ib_paper_chain_probe.py --hours 8 --symbols TQQQ,SQQQ
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import asyncpg
from ib_insync import IB


ET = timezone(timedelta(hours=-5))  # sufficient for current checks (EST window)


@dataclass
class Finding:
    level: str
    code: str
    detail: str


def _to_et(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET)


def _parse_symbols(raw: str) -> list[str]:
    out = []
    for s in str(raw or "").split(","):
        x = s.strip().upper()
        if x:
            out.append(x)
    return out or ["TQQQ", "SQQQ", "QQQ"]


def _strategy_from_ref(order_ref: str) -> str:
    ref = str(order_ref or "")
    if ref.startswith("qqqv1:"):
        parts = ref.split(":")
        if len(parts) >= 2:
            return parts[1]
    if ref.startswith("indexsw:"):
        parts = ref.split(":")
        if len(parts) >= 2:
            return parts[1]
    if ref.startswith("manual_flatten:"):
        parts = ref.split(":")
        if len(parts) >= 3:
            return parts[2]
    return "unknown"


def _kind_from_ref(order_ref: str, order_type: str) -> str:
    ref = str(order_ref or "").upper()
    ot = str(order_type or "").upper()
    if ":ENTRY" in ref:
        return "ENTRY"
    if ":FLATTEN" in ref:
        return "FLATTEN"
    if ":STOP" in ref or ot in {"STP", "STP_LMT", "STOP", "STOP_LIMIT"}:
        return "STOP"
    if ":TP" in ref or "TAKE_PROFIT" in ref:
        return "TP"
    return "OTHER"


async def _load_db_metrics(dsn: str, start_utc: datetime) -> tuple[int, int]:
    conn = await asyncpg.connect(dsn)
    try:
        event_count = await conn.fetchval(
            """
            SELECT count(*)::int
            FROM core.core_order_event
            WHERE event_ts >= $1
            """,
            start_utc,
        )
        order_count = await conn.fetchval(
            """
            SELECT count(*)::int
            FROM core.core_order
            WHERE created_at >= $1
            """,
            start_utc,
        )
        return int(event_count or 0), int(order_count or 0)
    finally:
        await conn.close()


def _iter_tqqq_fills(ib: IB, symbols: Iterable[str], start_utc: datetime):
    symset = {s.upper() for s in symbols}
    for f in ib.fills() or []:
        ex = getattr(f, "execution", None)
        c = getattr(f, "contract", None)
        if ex is None or c is None:
            continue
        sym = str(getattr(c, "symbol", "") or "").upper()
        if sym not in symset:
            continue
        t = getattr(ex, "time", None)
        if t is None:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t < start_utc:
            continue
        yield {
            "utc": t,
            "symbol": sym,
            "side": str(getattr(ex, "side", "") or ""),
            "shares": float(getattr(ex, "shares", 0.0) or 0.0),
            "price": float(getattr(ex, "price", 0.0) or 0.0),
            "orderId": int(getattr(ex, "orderId", 0) or 0),
            "permId": int(getattr(ex, "permId", 0) or 0),
            "clientId": int(getattr(ex, "clientId", 0) or 0),
            "execId": str(getattr(ex, "execId", "") or ""),
        }


def _detect_cross_strategy_fast_close(completed_rows: list[dict], window_sec: int = 20) -> list[Finding]:
    findings: list[Finding] = []
    entries = [r for r in completed_rows if r["kind"] == "ENTRY" and r["status"] == "Filled"]
    flattens = [r for r in completed_rows if r["kind"] == "FLATTEN" and r["status"] == "Filled"]
    for e in entries:
        for x in flattens:
            if e["symbol"] != x["symbol"]:
                continue
            if e["strategy_id"] == x["strategy_id"]:
                continue
            te = e["completed_utc"]
            tx = x["completed_utc"]
            if te is None or tx is None:
                continue
            dt = (tx - te).total_seconds()
            if 0 <= dt <= window_sec:
                findings.append(
                    Finding(
                        level="WARN",
                        code="cross_strategy_fast_close",
                        detail=(
                            f"{e['symbol']} entry[{e['strategy_id']}] -> flatten[{x['strategy_id']}] "
                            f"in {dt:.1f}s (entry_perm={e['perm_id']}, flatten_perm={x['perm_id']})"
                        ),
                    )
                )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="IB paper chain probe (non-invasive)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=0)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--symbols", default="TQQQ,SQQQ,QQQ")
    ap.add_argument("--dsn", default="postgresql://postgres:postgres@localhost:5432/trading")
    args = ap.parse_args()

    start_utc = datetime.now(timezone.utc) - timedelta(hours=max(1, int(args.hours)))
    symbols = _parse_symbols(args.symbols)
    findings: list[Finding] = []

    ib = IB()
    try:
        ib.connect(args.host, args.port, clientId=int(args.client_id), readonly=True, timeout=8)
    except Exception as exc:
        print(f"FAIL | ib_connect | {exc}")
        return 2

    try:
        completed = ib.reqCompletedOrders(False) or []
        completed_rows: list[dict] = []
        for c in completed:
            o = getattr(c, "order", None)
            s = getattr(c, "orderStatus", None)
            ct = getattr(c, "contract", None)
            if o is None or s is None or ct is None:
                continue
            sym = str(getattr(ct, "symbol", "") or "").upper()
            if sym not in {x.upper() for x in symbols}:
                continue
            status = str(getattr(s, "status", "") or "")
            order_ref = str(getattr(o, "orderRef", "") or "")
            kind = _kind_from_ref(order_ref, str(getattr(o, "orderType", "") or ""))
            perm_id = int(getattr(o, "permId", 0) or 0)
            strategy_id = _strategy_from_ref(order_ref)
            # completedTime not always available in paper; keep nullable
            completed_rows.append(
                {
                    "symbol": sym,
                    "status": status,
                    "order_ref": order_ref,
                    "kind": kind,
                    "perm_id": perm_id,
                    "strategy_id": strategy_id,
                    "completed_utc": None,
                }
            )

        fill_rows = list(_iter_tqqq_fills(ib, symbols, start_utc))
        fills_by_perm: dict[int, datetime] = {}
        for f in fill_rows:
            p = int(f["permId"])
            t = f["utc"]
            if p <= 0:
                continue
            if p not in fills_by_perm or t < fills_by_perm[p]:
                fills_by_perm[p] = t
        for r in completed_rows:
            p = int(r["perm_id"])
            if p in fills_by_perm:
                r["completed_utc"] = fills_by_perm[p]

        findings.extend(_detect_cross_strategy_fast_close(completed_rows, window_sec=20))
    finally:
        ib.disconnect()

    try:
        event_count, order_count = asyncio.run(_load_db_metrics(args.dsn, start_utc))
    except Exception as exc:
        findings.append(Finding("WARN", "db_metrics_unavailable", str(exc)))
        event_count, order_count = -1, -1

    if event_count == 0:
        findings.append(
            Finding(
                "WARN",
                "no_order_events",
                "core.core_order_event has 0 rows in selected window; traceability is degraded",
            )
        )
    if order_count == 0:
        findings.append(
            Finding(
                "WARN",
                "no_core_orders",
                "core.core_order has 0 rows in selected window; persistence pipeline may be disconnected",
            )
        )

    print("=== IB Paper Chain Probe ===")
    print(f"window_start_utc={start_utc.isoformat()}")
    print(f"symbols={','.join(symbols)}")
    print(f"db_core_order_event_count={event_count}")
    print(f"db_core_order_count={order_count}")
    print(f"finding_count={len(findings)}")
    for f in findings:
        print(f"{f.level} | {f.code} | {f.detail}")

    has_error = any(f.level.upper() == "ERROR" for f in findings)
    print(f"SUMMARY | errors={int(has_error)} warnings={sum(1 for f in findings if f.level.upper() == 'WARN')}")
    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())

