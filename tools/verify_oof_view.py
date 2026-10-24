#!/usr/bin/env python3
from __future__ import annotations
"""
Verify (and optionally create) ClickHouse OOF view for LSTM and report coverage.

Usage:
  # Check existence and coverage for a period
  python tools/verify_oof_view.py --symbols TSLA,AAPL --start '2025-07-01 09:30:00' --end '2025-08-31 16:00:00'

  # Create/refresh DDL and then check
  python tools/verify_oof_view.py --create --symbols TSLA --start '2025-07-01 09:30:00' --end '2025-08-31 16:00:00'

Reads clickhouse client command from scripts/ai_trader/config/ai_trader.yaml:
  clickhouse.client (e.g., 'docker exec trading_clickhouse clickhouse-client')
  clickhouse.dsn     (extra args like '--host ... --port ...')
"""
import argparse
import json
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Any, List

import yaml


def _cfg() -> Dict[str, Any]:
    p = Path("scripts/ai_trader/config/ai_trader.yaml")
    return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}


def _client_cmd() -> List[str]:
    cfg = _cfg()
    click = (cfg.get("clickhouse") or {})
    client = str(click.get("client", "clickhouse-client")).strip()
    dsn = str(click.get("dsn", "")).strip()
    base = []
    base.extend(shlex.split(client))
    if dsn:
        base.extend(shlex.split(dsn))
    return base


def _run_sql(sql: str) -> str:
    cmd = _client_cmd() + ["--query", sql]
    try:
        out = subprocess.check_output(cmd, text=True)
        return out
    except subprocess.CalledProcessError as e:
        return e.output or ""


def _apply_ddl() -> None:
    ddl = Path("db/clickhouse/oof_lstm_schema.sql").read_text("utf-8")
    # Split on semicolons while preserving structure (simple approach)
    for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
        _run_sql(stmt)


def _exists_view(view: str) -> bool:
    out = _run_sql(f"EXISTS VIEW {view}")
    try:
        return out.strip().endswith("1")
    except Exception:
        return False


def _coverage(symbols: List[str], start: str, end: str) -> Dict[str, Any]:
    """Compute OOF coverage more robustly.

    - Some environments coalesce NULL to 0 in views/clients; treat non-zero as present.
    - Report both pred and conf non-zero coverage, plus classic NOT NULL for reference.
    """
    syms = ",".join([f"'{s}'" for s in symbols])
    sql = f"""
    SELECT symbol,
           count() AS n,
           sum(lstm_oof_h5 IS NOT NULL) AS n_oof_notnull,
           sum(CASE WHEN lstm_oof_h5 != 0 THEN 1 ELSE 0 END) AS n_pred_nonzero,
           sum(CASE WHEN lstm_oof_conf_h5 != 0 THEN 1 ELSE 0 END) AS n_conf_nonzero,
           round(n_pred_nonzero / nullIf(n,0) * 100, 2) AS pred_cov_pct,
           round(n_conf_nonzero / nullIf(n,0) * 100, 2) AS conf_cov_pct
    FROM trading.features_1m_with_oof
    WHERE symbol IN ({syms})
      AND ts >= toDateTime('{start}') AND ts <= toDateTime('{end}')
    GROUP BY symbol
    ORDER BY symbol
    """
    out = _run_sql(sql)
    rows = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split('\t')]
        if len(parts) == 7:
            rows.append({
                "symbol": parts[0],
                "n": int(float(parts[1])),
                "n_oof_notnull": int(float(parts[2])),
                "n_pred_nonzero": int(float(parts[3])),
                "n_conf_nonzero": int(float(parts[4])),
                "pred_cov_pct": float(parts[5]),
                "conf_cov_pct": float(parts[6]),
            })
    return {"rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true", help="Apply DDL to create/refresh OOF tables/views")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    if args.create:
        _apply_ddl()

    ok = _exists_view("trading.features_1m_with_oof")
    res: Dict[str, Any] = {"view_exists": ok}
    if args.symbols and args.start and args.end and ok:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
        cov = _coverage(symbols, args.start, args.end)
        res.update(cov)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
