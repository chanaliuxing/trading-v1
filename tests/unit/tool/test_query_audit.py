import json
from pathlib import Path

from tool.query_audit import _discover_clickhouse_audit_tables, query_records


def test_query_audit_filters_by_trace_and_domain(tmp_path: Path):
    day_dir = tmp_path / "2026-03-14"
    day_dir.mkdir(parents=True)
    exec_file = day_dir / "exec-10.jsonl"
    market_file = day_dir / "market-10.jsonl"
    exec_file.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-14T10:00:00-04:00", "domain": "exec", "stage": "intent_requested", "trace_id": "t1", "symbol": "TQQQ"}),
                json.dumps({"ts": "2026-03-14T10:00:01-04:00", "domain": "exec", "stage": "fill_received", "trace_id": "t1", "symbol": "TQQQ"}),
                json.dumps({"ts": "2026-03-14T10:00:02-04:00", "domain": "exec", "stage": "fill_received", "trace_id": "t2", "symbol": "SQQQ"}),
            ])
        + "\n",
        encoding="utf-8",
    )
    market_file.write_text(
        json.dumps({"ts": "2026-03-14T10:00:00-04:00", "domain": "market", "stage": "tick_minute_summary", "trace_id": "t1", "symbol": "QQQ"}) + "\n",
        encoding="utf-8",
    )

    rows = query_records(tmp_path, date="2026-03-14", domain="exec", trace_id="t1")

    assert [row["stage"] for row in rows] == ["intent_requested", "fill_received"]
    assert all(row["domain"] == "exec" for row in rows)


def test_query_audit_filters_by_broker_order_id(tmp_path: Path):
    day_dir = tmp_path / "2026-03-14"
    day_dir.mkdir(parents=True)
    path = day_dir / "strategy-10.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-14T10:00:00-04:00", "domain": "strategy", "stage": "flatten_requested", "broker_order_id": "22", "symbol": "SQQQ"}),
                json.dumps({"ts": "2026-03-14T10:00:01-04:00", "domain": "strategy", "stage": "flatten_submitted", "broker_order_id": "22", "symbol": "SQQQ"}),
                json.dumps({"ts": "2026-03-14T10:00:02-04:00", "domain": "strategy", "stage": "flatten_submitted", "broker_order_id": "23", "symbol": "SQQQ"}),
            ])
        + "\n",
        encoding="utf-8",
    )

    rows = query_records(tmp_path, broker_order_id="22")

    assert len(rows) == 2
    assert [row["stage"] for row in rows] == ["flatten_requested", "flatten_submitted"]


def test_query_audit_filters_by_hour(tmp_path: Path):
    day_dir = tmp_path / "2026-03-14"
    day_dir.mkdir(parents=True)
    (day_dir / "strategy-09.jsonl").write_text(
        json.dumps({"ts": "2026-03-14T09:59:00-04:00", "domain": "strategy", "stage": "entry_gate_blocked", "symbol": "AAPL"}) + "\n",
        encoding="utf-8",
    )
    (day_dir / "strategy-10.jsonl").write_text(
        json.dumps({"ts": "2026-03-14T10:00:00-04:00", "domain": "strategy", "stage": "entry_gate_blocked", "symbol": "AAPL"}) + "\n",
        encoding="utf-8",
    )

    rows = query_records(tmp_path, date="2026-03-14", domain="strategy", hour="10")

    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-03-14T10:00:00-04:00"


class _FakeClient:
    def __init__(self) -> None:
        self._tables = {
            ("trading", "audit_events"): {"columns": ["ts", "domain", "stage", "strategy_id"]},
            ("trading", "order_events"): {"columns": ["ts", "stage", "strategy_id"]},
        }

    def query(self, sql: str, parameters: dict | None = None):
        q = " ".join(str(sql).split()).lower()
        params = parameters or {}
        if "from system.tables" in q and "positioncaseinsensitive" in q:
            db = str(params.get("db") or "").strip()
            out = []
            for (database, name), _ in self._tables.items():
                if database != db:
                    continue
                if "audit" in name or "event" in name:
                    out.append({"database": database, "name": name})
            return out
        if "from system.tables" in q and "name = {name:string}" in q:
            database = str(params.get("database") or "").strip()
            name = str(params.get("name") or "").strip()
            return [{"1": 1}] if (database, name) in self._tables else []
        if q.startswith("describe table"):
            table = str(sql).split()[-1]
            if "." in table:
                database, name = table.split(".", 1)
            else:
                database, name = "trading", table
            cols = self._tables.get((database, name), {}).get("columns", [])
            return [{"name": item} for item in cols]
        return []


def test_discover_clickhouse_audit_tables_filters_to_compatible_schema() -> None:
    client = _FakeClient()
    tables = _discover_clickhouse_audit_tables(client)
    assert tables == ["trading.audit_events"]


def test_discover_clickhouse_audit_tables_ignores_missing_explicit_table() -> None:
    client = _FakeClient()
    tables = _discover_clickhouse_audit_tables(client, explicit_table="trading.not_exists")
    assert tables == []
