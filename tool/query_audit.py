#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from libs.config import config

ET_TZ = "America/New_York"


def _iter_files(root: Path, *, date: Optional[str], domain: Optional[str]) -> Iterable[Path]:
    if date:
        base = root / date
        if not base.exists():
            return []
        pattern = f"{domain}-*.jsonl" if domain else "*.jsonl"
        return sorted(base.glob(pattern))
    pattern = f"*/{domain}-*.jsonl" if domain else "*/*.jsonl"
    return sorted(root.glob(pattern))


def _match_hour(path: Path, hour: Optional[str]) -> bool:
    if not hour:
        return True
    name = path.name
    stem = name.rsplit(".", 1)[0]
    hour_part = stem.rsplit("-", 1)[-1]
    return hour_part == str(hour).zfill(2)


def _match_record(
    record: Dict[str, Any],
    *,
    trace_id: Optional[str],
    order_id: Optional[str],
    broker_order_id: Optional[str],
    symbol: Optional[str],
    stage: Optional[str],
) -> bool:
    if trace_id and str(record.get("trace_id") or "") != trace_id:
        return False
    if order_id and str(record.get("order_id") or "") != order_id:
        return False
    if broker_order_id and str(record.get("broker_order_id") or "") != broker_order_id:
        return False
    if symbol and str(record.get("symbol") or "").upper() != symbol.upper():
        return False
    if stage and str(record.get("stage") or "") != stage:
        return False
    return True


def query_records(
    root: Path,
    *,
    date: Optional[str] = None,
    domain: Optional[str] = None,
    hour: Optional[str] = None,
    trace_id: Optional[str] = None,
    order_id: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    symbol: Optional[str] = None,
    stage: Optional[str] = None,
    source: str = "local",
    clickhouse_table: Optional[str] = None,
) -> List[Dict[str, Any]]:
    source_norm = str(source or "local").strip().lower()
    if source_norm in {"clickhouse", "ch"}:
        return _query_records_clickhouse(
            date=date,
            domain=domain,
            hour=hour,
            trace_id=trace_id,
            order_id=order_id,
            broker_order_id=broker_order_id,
            symbol=symbol,
            stage=stage,
            clickhouse_table=clickhouse_table,
        )
    if source_norm == "auto":
        try:
            rows = _query_records_clickhouse(
                date=date,
                domain=domain,
                hour=hour,
                trace_id=trace_id,
                order_id=order_id,
                broker_order_id=broker_order_id,
                symbol=symbol,
                stage=stage,
                clickhouse_table=clickhouse_table,
            )
            if rows:
                return rows
        except Exception:
            pass
    rows: List[Dict[str, Any]] = []
    for path in _iter_files(root, date=date, domain=domain):
        if not _match_hour(path, hour):
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if _match_record(
                    record,
                    trace_id=trace_id,
                    order_id=order_id,
                    broker_order_id=broker_order_id,
                    symbol=symbol,
                    stage=stage,
                ):
                    rows.append(record)
        except Exception:
            continue
    rows.sort(key=lambda r: str(r.get("ts") or ""))
    return rows


def _normalize_ch_record(row: Dict[str, Any]) -> Dict[str, Any]:
    record = dict(row)
    payload = record.get("payload")
    if isinstance(payload, str):
        payload_s = payload.strip()
        if payload_s.startswith("{") and payload_s.endswith("}"):
            try:
                record["payload"] = json.loads(payload_s)
            except Exception:
                pass
    return record


def _discover_clickhouse_audit_tables(client: Any, *, explicit_table: Optional[str] = None) -> List[str]:
    db = str(config.clickhouse_config.get("database") or "trading").strip()
    if explicit_table:
        table = str(explicit_table).strip()
        if not table:
            return []
        table_ref = table if "." in table else f"{db}.{table}"
        if _table_exists(client, table_ref):
            return [table_ref]
        return []
    rows = client.query(
        """
        SELECT database, name
        FROM system.tables
        WHERE database = {db:String}
          AND (
            positionCaseInsensitive(name, 'audit') > 0
            OR positionCaseInsensitive(name, 'event') > 0
          )
        ORDER BY name
        """,
        parameters={"db": db},
    )
    out = []
    for row in rows:
        db_name = str(row.get("database") or "").strip()
        table_name = str(row.get("name") or "").strip()
        if not db_name or not table_name:
            continue
        table_ref = f"{db_name}.{table_name}"
        if _table_has_columns(client, table_ref, ["ts", "domain", "stage", "strategy_id"]):
            out.append(table_ref)
    if out:
        return out
    return []


def _table_has_columns(client: Any, table: str, required: List[str]) -> bool:
    try:
        rows = client.query(f"DESCRIBE TABLE {table}")
    except Exception:
        return False
    cols = {str(row.get("name") or "").strip().lower() for row in rows}
    return all(str(col).strip().lower() in cols for col in required)


def _table_exists(client: Any, table: str) -> bool:
    table_ref = str(table or "").strip()
    if not table_ref:
        return False
    if "." in table_ref:
        db_name, table_name = table_ref.split(".", 1)
    else:
        db_name = str(config.clickhouse_config.get("database") or "trading").strip()
        table_name = table_ref
    rows = client.query(
        """
        SELECT 1
        FROM system.tables
        WHERE database = {database:String}
          AND name = {name:String}
        LIMIT 1
        """,
        parameters={"database": db_name, "name": table_name},
    )
    return len(rows) > 0


def _query_records_clickhouse(
    *,
    date: Optional[str],
    domain: Optional[str],
    hour: Optional[str],
    trace_id: Optional[str],
    order_id: Optional[str],
    broker_order_id: Optional[str],
    symbol: Optional[str],
    stage: Optional[str],
    clickhouse_table: Optional[str] = None,
) -> List[Dict[str, Any]]:
    from libs.db.ch_pool import get_client

    client = get_client()
    tables = _discover_clickhouse_audit_tables(client, explicit_table=clickhouse_table)
    if not tables:
        return []
    rows: List[Dict[str, Any]] = []
    for table in tables:
        try:
            if not _table_has_columns(client, table, ["ts", "domain", "stage"]):
                continue
            where = ["1 = 1"]
            params: Dict[str, Any] = {}
            if domain:
                where.append("domain = {domain:String}")
                params["domain"] = str(domain)
            if date:
                where.append(f"toDate(toTimeZone(ts, '{ET_TZ}')) = toDate({{date:String}})")
                params["date"] = str(date)
            if hour:
                where.append(f"toHour(toTimeZone(ts, '{ET_TZ}')) = {{hour:UInt8}}")
                params["hour"] = int(str(hour).zfill(2))
            if trace_id:
                where.append("trace_id = {trace_id:String}")
                params["trace_id"] = str(trace_id)
            if order_id:
                where.append("order_id = {order_id:String}")
                params["order_id"] = str(order_id)
            if broker_order_id:
                where.append("broker_order_id = {broker_order_id:String}")
                params["broker_order_id"] = str(broker_order_id)
            if symbol:
                where.append("upperUTF8(symbol) = upperUTF8({symbol:String})")
                params["symbol"] = str(symbol)
            if stage:
                where.append("stage = {stage:String}")
                params["stage"] = str(stage)
            sql = f"""
            SELECT
                ts,
                domain,
                component,
                stage,
                severity,
                strategy_id,
                strategy_run_id,
                trigger_id,
                decision_id,
                trace_id,
                causation_id,
                intent_id,
                order_id,
                broker_order_id,
                perm_id,
                symbol,
                status,
                reason,
                payload
            FROM {table}
            WHERE {' AND '.join(where)}
            ORDER BY ts
            """
            part = client.query(sql, parameters=params)
            rows.extend(_normalize_ch_record(row) for row in part)
        except Exception:
            continue
    rows.sort(key=lambda r: str(r.get("ts") or ""))
    return rows


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Query local runtime audit jsonl files")
    p.add_argument("--root", default="runtime/audit", help="audit root directory")
    p.add_argument("--date", help="ET date directory, e.g. 2026-03-14")
    p.add_argument("--hour", help="ET hour file suffix, e.g. 10")
    p.add_argument("--domain", help="domain file prefix, e.g. exec/market/recon/system/strategy")
    p.add_argument("--trace-id")
    p.add_argument("--order-id")
    p.add_argument("--broker-order-id")
    p.add_argument("--symbol")
    p.add_argument("--stage")
    p.add_argument("--source", choices=["local", "clickhouse", "auto"], default="local")
    p.add_argument("--clickhouse-table")
    p.add_argument("--json", action="store_true", help="emit JSON array instead of line summary")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    rows = query_records(
        Path(args.root).expanduser(),
        date=args.date,
        domain=args.domain,
        hour=args.hour,
        trace_id=args.trace_id,
        order_id=args.order_id,
        broker_order_id=args.broker_order_id,
        symbol=args.symbol,
        stage=args.stage,
        source=args.source,
        clickhouse_table=args.clickhouse_table,
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print(
            f"{row.get('ts')} "
            f"{row.get('domain')}/{row.get('component')} "
            f"{row.get('stage')} "
            f"symbol={row.get('symbol') or '-'} "
            f"status={row.get('status') or '-'} "
            f"reason={row.get('reason') or '-'} "
            f"trace_id={row.get('trace_id') or '-'} "
            f"broker_order_id={row.get('broker_order_id') or '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
