from __future__ import annotations

"""
Events Exporter: ingest JSONL event files into ClickHouse for Grafana use.

Reads from runs/ai_trader/YYYY-MM-DD/*_events.jsonl and inserts into ai_trader_events.

Files handled (best-effort):
  - hedging_events.jsonl   → kind='hedging'
  - guardian_events.jsonl  → kind='guardian'
  - tools_ledger.jsonl     → kind='tool'
  - overlay_events.jsonl   → kind='overlay'
  - data_quality.json      → kind='dq' (per-symbol errors expanded)

Table:
  ai_trader_events(
    date Date,
    ts DateTime,
    run_name String,
    session LowCardinality(String),
    kind LowCardinality(String),
    symbol String,
    payload String
  ) ENGINE=MergeTree ORDER BY (date, kind, ts)

Best-effort: silently skip on ClickHouse unavailability or invalid lines.
"""

import json
from datetime import datetime, date
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, Any, List, Optional

from libs.logger import get_logger

NY_TZ = ZoneInfo("America/New_York")

try:
    from libs.db.clickhouse import ClickHouseDB
except Exception:  # pragma: no cover
    ClickHouseDB = None  # type: ignore

log = get_logger("ai_trader.events_exporter")


def _ch() -> Optional[ClickHouseDB]:
    if ClickHouseDB is None:
        return None
    try:
        return ClickHouseDB()
    except Exception as e:
        log.warning("events_exporter_ch_unavailable", err=str(e))
        return None


def ensure_tables() -> None:
    ch = _ch()
    if ch is None:
        return
    try:
        ch.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_trader_events (
              date Date,
              ts DateTime,
              run_name String,
              session LowCardinality(String),
              kind LowCardinality(String),
              symbol String,
              payload String
            ) ENGINE = MergeTree
            ORDER BY (date, kind, ts)
            """
        )
    except Exception as e:
        log.warning("events_exporter_create_failed", err=str(e))


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def export_events(runs_dir: str, run_date: str, session: str = "learn") -> None:
    """Export known JSONL events for the given date directory.

    Args:
      runs_dir: base runs directory (e.g., runs/ai_trader)
      run_date: folder date (YYYY-MM-DD)
      session: logical session ('learn' by default)
    """
    ch = _ch()
    if ch is None:
        return
    ensure_tables()
    out_dir = Path(runs_dir) / run_date
    if not out_dir.exists():
        return
    files = [
        (out_dir / "hedging_events.jsonl", "hedging"),
        (out_dir / "guardian_events.jsonl", "guardian"),
        (out_dir / "tools_ledger.jsonl", "tool"),
        (out_dir / "overlay_events.jsonl", "overlay"),
        (out_dir / "brain_calls.jsonl", "brain"),
    ]
    d = date.fromisoformat(run_date)
    rows: List[List[Any]] = []
    for fp, kind in files:
        if not fp.exists():
            continue
        events = _read_jsonl(fp)
        for ev in events:
            try:
                ts_str = str(ev.get("ts") or ev.get("time") or ev.get("timestamp") or "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        ts = datetime.now(NY_TZ)
                else:
                    ts = datetime.now(NY_TZ)
                symbol = str(ev.get("symbol") or ev.get("hedge_symbol") or ev.get("ticker") or "")
                # Determine logical session per row (brain calls carry session/action)
                row_session = session
                if kind == "brain":
                    try:
                        rs = ev.get("session") or ev.get("action")
                        if rs:
                            row_session = str(rs)
                    except Exception:
                        pass
                run_name = f"{row_session}_{run_date}"
                payload = json.dumps(ev, ensure_ascii=False)
                rows.append([d, ts, run_name, row_session, kind, symbol, payload])
            except Exception:
                continue
    if not rows:
        # Also try to ingest data_quality.json into per-symbol events (kind='dq')
        try:
            dq = out_dir / "data_quality.json"
            if dq.exists():
                dqj = json.loads(dq.read_text("utf-8"))
                failed = (dqj.get("failed") or {}) if isinstance(dqj, dict) else {}
                for sym, errs in failed.items():
                    if not isinstance(errs, list):
                        continue
                    for e in errs:
                        try:
                            ts = datetime.now(NY_TZ)
                            payload = json.dumps({"symbol": sym, "error": str(e)}, ensure_ascii=False)
                            run_name = f"{session}_{run_date}"
                            rows.append([d, ts, run_name, session, "dq", sym, payload])
                        except Exception:
                            continue
        except Exception:
            pass
        if not rows:
            return
    try:
        ch.insert(
            "ai_trader_events",
            rows,
            column_names=["date", "ts", "run_name", "session", "kind", "symbol", "payload"],
        )
        log.info("events_exported", date=run_date, session=session, count=len(rows))
    except Exception as e:
        log.warning("events_exporter_insert_failed", err=str(e), date=run_date, session=session)
