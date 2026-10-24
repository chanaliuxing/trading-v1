#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ v1 preflight audit: detect stale "orders/state" in DB/Redis/local files.

This script is intentionally READ-ONLY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.config import config
from libs.db.postgres import PostgresDB
from libs.logger import get_logger

logger = get_logger("qqqv1_audit")

ET = ZoneInfo("America/New_York")


@dataclass
class AuditFinding:
    level: str  # OK/WARN/FAIL
    message: str
    details: Dict[str, Any]


def _now_et() -> datetime:
    return datetime.now(ET)


def _today_str_et() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _print(f: AuditFinding) -> None:
    details = f.details or {}
    suffix = ""
    if details:
        suffix = " " + json.dumps(details, ensure_ascii=False, separators=(",", ":"), default=str)
    print(f"[{f.level}] {f.message}{suffix}")


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _iter_symbols(raw: str) -> list[str]:
    syms = []
    for s in (raw or "").split(","):
        s2 = str(s).strip().upper()
        if s2:
            syms.append(s2)
    return syms


def audit_local_state(*, strategy_id: str, state_dir: Path) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    state_path = state_dir / f"qqq_intraday_{strategy_id}.json"
    tmp_path = state_path.with_suffix(".tmp")
    obs_path = state_dir / f"qqq_intraday_observability_{strategy_id}.json"
    diag_path = state_dir / f"qqq_intraday_diag_{strategy_id}.jsonl"

    if tmp_path.exists():
        findings.append(AuditFinding("WARN", "State tmp file exists (previous crash?)", {"path": str(tmp_path)}))

    if not state_path.exists():
        findings.append(AuditFinding("OK", "No local strategy state file", {"path": str(state_path)}))
        return findings

    raw = _load_json(state_path)
    if not isinstance(raw, dict):
        findings.append(AuditFinding("WARN", "State file is unreadable JSON", {"path": str(state_path)}))
        return findings

    day = str(raw.get("day") or "")
    updated_at = str(raw.get("updated_at") or "")
    reason = str(raw.get("reason") or "")
    pending_decision_id = raw.get("pending_decision_id")
    exec_symbol = raw.get("exec_symbol")
    side = raw.get("side")
    stop_px = raw.get("stop_px")
    is_traded = bool(raw.get("is_traded", False))

    findings.append(
        AuditFinding(
            "OK",
            "Loaded local strategy state",
            {
                "path": str(state_path),
                "day": day,
                "updated_at": updated_at,
                "reason": reason,
                "is_traded": is_traded,
            },
        )
    )

    if day == _today_str_et():
        if pending_decision_id:
            findings.append(
                AuditFinding(
                    "WARN",
                    "State indicates a pending decision today (may block new entry)",
                    {"pending_decision_id": pending_decision_id, "exec_symbol": exec_symbol, "side": side},
                )
            )
        if exec_symbol or side or stop_px:
            findings.append(
                AuditFinding(
                    "WARN",
                    "State indicates an active trade today (ensure broker + DB are clean before restart)",
                    {"exec_symbol": exec_symbol, "side": side, "stop_px": stop_px},
                )
            )
    else:
        findings.append(AuditFinding("OK", "State file is from a previous day (will not be restored today)", {"day": day}))

    if obs_path.exists():
        findings.append(AuditFinding("OK", "Observability snapshot exists", {"path": str(obs_path)}))
    if diag_path.exists():
        findings.append(AuditFinding("OK", "Diag log exists", {"path": str(diag_path)}))

    return findings


async def audit_postgres(*, strategy_id: str, symbols: list[str], max_rows: int, stale_days: int) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    pg = PostgresDB()

    try:
        await pg.connect()
    except Exception as exc:
        findings.append(AuditFinding("WARN", "Postgres unavailable (skip DB audit)", {"error": str(exc)}))
        return findings

    try:
        pending = await pg.fetchall(
            """
            SELECT
              order_id, broker_order_id, ib_perm_id, order_ref,
              symbol, side, order_type, qty, state, created_at, updated_at, note
            FROM core.core_order
            WHERE strategy_id = $1
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
              AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
            ORDER BY created_at DESC
            LIMIT $3
            """,
            strategy_id,
            symbols if symbols else None,
            int(max_rows),
        )
        if pending:
            findings.append(AuditFinding("FAIL", "DB has non-terminal orders (NEW/SUBMITTED/PARTIAL)", {"count": len(pending)}))
            for r in pending[: min(len(pending), 20)]:
                details = {
                    "symbol": r.get("symbol"),
                    "state": r.get("state"),
                    "side": r.get("side"),
                    "order_type": r.get("order_type"),
                    "qty": r.get("qty"),
                    "created_at": str(r.get("created_at") or ""),
                    "broker_order_id": str(r.get("broker_order_id") or ""),
                    "order_ref": str(r.get("order_ref") or ""),
                }
                findings.append(AuditFinding("FAIL", "Pending order row", details))
        else:
            findings.append(AuditFinding("OK", "DB has no non-terminal orders for strategy", {"strategy_id": strategy_id}))

        stale_cutoff = _now_et() - timedelta(days=int(max(1, stale_days)))
        stale = await pg.fetchall(
            """
            SELECT
              order_id, broker_order_id, order_ref, symbol, side, order_type, qty, state, created_at
            FROM core.core_order
            WHERE strategy_id = $1
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
              AND state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')
              AND created_at < $3
            ORDER BY created_at ASC
            LIMIT $4
            """,
            strategy_id,
            symbols if symbols else None,
            stale_cutoff,
            int(max_rows),
        )
        if stale:
            findings.append(
                AuditFinding(
                    "WARN",
                    "DB has very old non-terminal orders (may be stale records)",
                    {"count": len(stale), "cutoff": stale_cutoff.isoformat()},
                )
            )

        pos = await pg.fetchall(
            """
            SELECT symbol, pos_qty, avg_px, updated_at
            FROM core.core_position
            WHERE strategy_id = $1
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
              AND pos_qty != 0
            ORDER BY symbol
            """,
            strategy_id,
            symbols if symbols else None,
        )
        if pos:
            findings.append(AuditFinding("FAIL", "DB indicates non-zero positions for strategy", {"count": len(pos)}))
            for r in pos[: min(len(pos), 20)]:
                findings.append(
                    AuditFinding(
                        "FAIL",
                        "Position row",
                        {"symbol": r.get("symbol"), "pos_qty": r.get("pos_qty"), "avg_px": r.get("avg_px"), "updated_at": str(r.get("updated_at") or "")},
                    )
                )
        else:
            findings.append(AuditFinding("OK", "DB positions flat for strategy", {"strategy_id": strategy_id}))

    except Exception as exc:
        findings.append(AuditFinding("WARN", "DB audit error", {"error": str(exc)}))
    finally:
        try:
            await pg.close()
        except Exception:
            pass

    return findings


async def audit_redis(*, patterns: Iterable[str], max_keys: int) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    redis_url = config.redis_config
    try:
        import redis.asyncio as aioredis  # type: ignore
    except Exception as exc:
        findings.append(AuditFinding("WARN", "redis.asyncio missing (skip redis audit)", {"error": str(exc)}))
        return findings

    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
    except Exception as exc:
        findings.append(AuditFinding("WARN", "Redis unavailable (skip redis audit)", {"url": redis_url, "error": str(exc)}))
        return findings

    try:
        total_hits = 0
        for pat in patterns:
            hits: list[str] = []
            cursor = 0
            while True:
                cursor, keys = await r.scan(cursor=cursor, match=pat, count=200)
                for k in keys:
                    hits.append(k)
                    if len(hits) >= int(max_keys):
                        break
                if len(hits) >= int(max_keys) or cursor == 0:
                    break
            if hits:
                total_hits += len(hits)
                # Do not print values; only show key+type+ttl.
                sample = []
                for k in hits[: min(len(hits), 20)]:
                    try:
                        t = await r.type(k)
                        ttl = await r.ttl(k)
                        sample.append({"key": k, "type": t, "ttl": ttl})
                    except Exception:
                        sample.append({"key": k})
                findings.append(AuditFinding("WARN", "Redis keys matched suspicious pattern", {"pattern": pat, "count": len(hits), "sample": sample}))
            else:
                findings.append(AuditFinding("OK", "No redis keys matched pattern", {"pattern": pat}))

        if total_hits == 0:
            findings.append(AuditFinding("OK", "No suspicious redis keys found (limited scan)", {"url": redis_url}))

    except Exception as exc:
        findings.append(AuditFinding("WARN", "Redis audit error", {"error": str(exc)}))
    finally:
        try:
            if hasattr(r, "aclose"):
                await r.aclose()
            else:
                await r.close()
        except Exception:
            pass

    return findings


async def main_async() -> int:
    ap = argparse.ArgumentParser(description="QQQ v1 stale order/state audit (READ-ONLY)")
    ap.add_argument("--strategy-id", default="qqq_intraday_001")
    ap.add_argument("--symbols", default="QQQ,QQEW,TQQQ,SQQQ")
    ap.add_argument("--state-dir", default="runtime/state")
    ap.add_argument("--max-rows", type=int, default=200)
    ap.add_argument("--stale-days", type=int, default=3)
    ap.add_argument("--redis-max-keys", type=int, default=200)
    args = ap.parse_args()

    strategy_id = str(args.strategy_id).strip()
    symbols = _iter_symbols(args.symbols)
    state_dir = Path(str(args.state_dir)).expanduser()

    findings: list[AuditFinding] = []
    findings += audit_local_state(strategy_id=strategy_id, state_dir=state_dir)
    findings += await audit_postgres(strategy_id=strategy_id, symbols=symbols, max_rows=int(args.max_rows), stale_days=int(args.stale_days))
    findings += await audit_redis(
        patterns=[
            "*qqqv1*",
            "*qqq_intraday*",
            "*intent*",
            "*decision*",
            "*order*",
            "*orders*",
            "*execution*",
            "*exec*",
        ],
        max_keys=int(args.redis_max_keys),
    )

    worst = "OK"
    for f in findings:
        _print(f)
        if f.level == "FAIL":
            worst = "FAIL"
        elif f.level == "WARN" and worst != "FAIL":
            worst = "WARN"

    print(f"\nRESULT={worst}")
    return 0 if worst == "OK" else (2 if worst == "FAIL" else 1)


def main() -> None:
    try:
        code = asyncio.run(main_async())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
