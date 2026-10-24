#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

from libs.db.clickhouse import ClickHouseDB

ET = ZoneInfo("America/New_York")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    return max(0.0, (now - ts.astimezone(ET)).total_seconds())


def _iter_recon_rows(audit_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(audit_dir.glob("recon-*.jsonl")):
        try:
            for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                text = raw.strip()
                if not text:
                    continue
                payload = json.loads(text)
                if isinstance(payload, dict):
                    rows.append(payload)
        except Exception:
            continue
    return rows


def _latest_stage(rows: list[dict[str, Any]], stage: str) -> dict[str, Any]:
    matched = [row for row in rows if str(row.get("stage") or "").strip() == stage]
    return matched[-1] if matched else {}


def _query_latest_account_snapshot(now_et: datetime) -> dict[str, Any]:
    candidate_tables = (
        "ibkr_account_realtime",
        "trading.ibkr_account_realtime",
        "default.ibkr_account_realtime",
    )
    for table in candidate_tables:
        try:
            ch = ClickHouseDB()
            rows = ch.query(
                f"""
                SELECT
                    ts_ny,
                    net_liquidation,
                    total_cash,
                    gross_position_value,
                    unrealized_pnl,
                    realized_pnl,
                    buying_power,
                    available_funds
                FROM {table}
                ORDER BY ts_ny DESC
                LIMIT 1
                """
            )
        except Exception:
            continue
        if not rows:
            continue
        row = dict(rows[0])
        raw_ts = row.get("ts_ny")
        ts = raw_ts if isinstance(raw_ts, datetime) else _parse_iso(raw_ts)
        age_sec = _age_seconds(ts, now_et)
        net_liq = _safe_float(row.get("net_liquidation"))
        available_funds = _safe_float(row.get("available_funds"))
        buying_power = _safe_float(row.get("buying_power"))
        blockers: list[str] = []
        if age_sec is None or age_sec > 900.0:
            blockers.append("financial_snapshot_stale")
        if net_liq is None:
            blockers.append("net_liquidation_missing")
        elif net_liq <= 0.0:
            blockers.append("net_liquidation_nonpositive")
        if available_funds is None:
            blockers.append("available_funds_missing")
        elif available_funds < 0.0:
            blockers.append("available_funds_negative")
        if buying_power is None:
            blockers.append("buying_power_missing")
        elif buying_power < 0.0:
            blockers.append("buying_power_negative")
        return {
            "available": True,
            "table": table,
            "ts_ny": ts.isoformat() if ts else str(raw_ts or ""),
            "age_sec": age_sec,
            "net_liquidation": net_liq,
            "total_cash": _safe_float(row.get("total_cash")),
            "gross_position_value": _safe_float(row.get("gross_position_value")),
            "unrealized_pnl": _safe_float(row.get("unrealized_pnl")),
            "realized_pnl": _safe_float(row.get("realized_pnl")),
            "buying_power": buying_power,
            "available_funds": available_funds,
            "ok": not blockers,
            "blockers": blockers,
        }
    return {
        "available": False,
        "ok": False,
        "blockers": ["financial_snapshot_unavailable"],
        "reason": "clickhouse_account_snapshot_table_missing_or_unreadable",
    }


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def build_runtime_reconciliation_health(
    *,
    audit_root: Path,
    day: str,
    now_et: datetime | None = None,
    stale_sec: float = 600.0,
) -> dict[str, Any]:
    current_et = now_et.astimezone(ET) if now_et is not None else datetime.now(ET)
    in_market_session = time(9, 25) <= current_et.timetz().replace(tzinfo=None) <= time(16, 5)
    effective_stale_sec = stale_sec if in_market_session else max(stale_sec, 1800.0)
    audit_dir = audit_root / f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    rows = _iter_recon_rows(audit_dir)
    latest_position_audit = _latest_stage(rows, "position_audit_snapshot")
    latest_alert = _latest_stage(rows, "reconciliation_alert_pushed")

    position_ts = _parse_iso(latest_position_audit.get("ts"))
    position_age_sec = _age_seconds(position_ts, current_et)
    payload = dict(latest_position_audit.get("payload") or {})
    position_meta = dict(payload.get("position_reconciler") or {})

    financial_snapshot = _query_latest_account_snapshot(current_et)

    blockers: list[str] = []
    if not latest_position_audit:
        blockers.append("runtime_reconciliation_audit_missing")
    elif position_age_sec is None or position_age_sec > effective_stale_sec:
        blockers.append("runtime_reconciliation_audit_stale")

    if int(payload.get("ghosts") or 0) > 0:
        blockers.append("runtime_ghost_orders_present")
    if int(payload.get("orphans_unresolved") or 0) > 0:
        blockers.append("runtime_unresolved_orphans_present")
    if int(payload.get("mismatches") or 0) > 0:
        blockers.append("runtime_order_mismatches_present")
    if int(payload.get("position_mismatches") or 0) > 0:
        blockers.append("runtime_position_mismatches_present")
    if int(payload.get("active_quarantine_count") or 0) > 0:
        blockers.append("runtime_active_quarantine_present")

    if not financial_snapshot.get("ok"):
        blockers.extend(str(item) for item in list(financial_snapshot.get("blockers") or []))

    return {
        "generated_at": current_et.isoformat(timespec="seconds"),
        "day": day,
        "audit_dir": str(audit_dir),
        "latest_position_audit_ts": latest_position_audit.get("ts"),
        "latest_position_audit_age_sec": position_age_sec,
        "latest_position_audit_payload": {
            "db_open_count": int(payload.get("db_open_count") or 0),
            "ib_open_count": int(payload.get("ib_open_count") or 0),
            "ib_pre_submitted": int(payload.get("ib_pre_submitted") or 0),
            "ghosts": int(payload.get("ghosts") or 0),
            "orphans_detected": int(payload.get("orphans_detected") or 0),
            "orphans_unresolved": int(payload.get("orphans_unresolved") or 0),
            "mismatches": int(payload.get("mismatches") or 0),
            "position_mismatches": int(payload.get("position_mismatches") or 0),
            "active_quarantine_count": int(payload.get("active_quarantine_count") or 0),
            "ib_positions": dict(position_meta.get("ib_positions") or {}),
            "ledger_positions": dict(position_meta.get("ledger_positions") or {}),
            "mismatch_samples": list(position_meta.get("mismatches") or []),
        },
        "latest_reconciliation_alert_ts": latest_alert.get("ts"),
        "latest_reconciliation_alert_payload": dict(latest_alert.get("payload") or {}),
        "financial_snapshot": financial_snapshot,
        "blockers": sorted(set(blockers)),
        "ok": not blockers,
    }
