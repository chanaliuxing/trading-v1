from __future__ import annotations

"""
Dash Exporter: write AI Trader metrics to ClickHouse for Grafana dashboards.

Tables:
  ai_trader_dash_metrics(
    date Date,
    ts DateTime,
    run_name String,
    session LowCardinality(String),
    name LowCardinality(String),
    value Float64,
    tags String
  ) ENGINE=MergeTree ORDER BY (date, run_name, name, ts)

Best-effort: if ClickHouse unavailable, calls will no-op.
"""

import json
from datetime import datetime, date
from typing import Dict, Any, Optional

from libs.logger import get_logger

try:
    from libs.db.clickhouse import ClickHouseDB
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore

log = get_logger("ai_trader.dash_exporter")


def _ch() -> Optional[ClickHouseDB]:
    if ClickHouseDB is None:
        return None
    try:
        return ClickHouseDB()
    except Exception as e:
        log.warning("dash_exporter_ch_unavailable", err=str(e))
        return None


def ensure_tables() -> None:
    ch = _ch()
    if ch is None:
        return
    try:
        ch.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_trader_dash_metrics (
              date Date,
              ts DateTime,
              run_name String,
              session LowCardinality(String),
              name LowCardinality(String),
              value Float64,
              tags String
            ) ENGINE = MergeTree
            ORDER BY (date, run_name, name, ts)
            """
        )
    except Exception as e:
        log.warning("dash_exporter_create_failed", err=str(e))


def export_metrics(run_name: str, session: str, metrics: Dict[str, float], tags: Optional[Dict[str, Any]] = None, when: Optional[datetime] = None) -> None:
    """Export a metrics dict to ClickHouse.

    Args:
      run_name: e.g. learn_2025-10-29 / pending_exec_2025-10-29
      session: 'learn' | 'morning' | 'intraday' | 'afterhours' | 'overnight'
      metrics: {name: value}
      tags: optional metadata (dict) stored as JSON
      when: optional timestamp (defaults to now())
    """
    ch = _ch()
    if ch is None:
        return
    if not metrics:
        return
    ensure_tables()
    ts = when or datetime.now()
    d = date.fromtimestamp(ts.timestamp())
    tags_json = json.dumps(tags or {}, ensure_ascii=False)
    rows = []
    for k, v in metrics.items():
        try:
            fv = float(v)
        except Exception:
            continue
        rows.append([d, ts, run_name, session, str(k), fv, tags_json])
    try:
        ch.insert(
            "ai_trader_dash_metrics",
            rows,
            column_names=["date", "ts", "run_name", "session", "name", "value", "tags"],
        )
        log.info("dash_exporter_metrics", run=run_name, session=session, count=len(rows))
    except Exception as e:
        log.warning("dash_exporter_insert_failed", err=str(e), run=run_name, session=session)

