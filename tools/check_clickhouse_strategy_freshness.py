#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check ClickHouse data freshness per strategy and per symbol.

Goals:
- Discover active strategies and their symbols from config.yml (enabled: true).
- For each symbol, verify latest timestamp per required table.

Defaults:
- Tables: trading.features_1m, trading.bars_1m_ibkr
- Staleness threshold: 180 seconds

You can override symbols or tables via CLI.
"""
import argparse
import json
from typing import List, Dict, Any, Set, Tuple
from pathlib import Path

import yaml
import clickhouse_connect
from datetime import datetime, timezone

from llm_trader.llm_trader.ny_time import now_ny
from libs.config import config as root_config


def _load_config_yaml() -> Dict[str, Any]:
    for fname in ("config.yml", "config.yaml", "config.example.yml"):
        p = Path(fname)
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}
    return {}


def _extract_active_symbols(cfg: Dict[str, Any]) -> Set[str]:
    syms: Set[str] = set()
    strategies = cfg.get("strategies") or []
    for strat in strategies:
        if not strat or not strat.get("enabled"):
            continue

        extras = ((strat.get("params") or {}).get("extras")) or {}

        # Common patterns
        # 1) Direct list: extras.symbols: ["TSLA", ...]
        vals = extras.get("symbols")
        if isinstance(vals, list):
            for s in vals:
                if isinstance(s, str):
                    syms.add(s.upper())

        # 2) Single symbol: extras.symbol: "TSLA"
        v = extras.get("symbol")
        if isinstance(v, str):
            syms.add(v.upper())

        # 3) Nested config (e.g., tsla_v7_2_config.symbol)
        for k, v in extras.items():
            if isinstance(v, dict):
                sym = v.get("symbol")
                if isinstance(sym, str):
                    syms.add(sym.upper())

        # 4) market_regime_symbols (if present)
        mrs = extras.get("market_regime_symbols")
        if isinstance(mrs, list):
            for s in mrs:
                if isinstance(s, str):
                    syms.add(s.upper())

    return syms


def _connect_ch() -> clickhouse_connect.driver.client.Client:
    ch_cfg = root_config.clickhouse_config
    client = clickhouse_connect.get_client(
        host=ch_cfg.get('host', 'localhost'),
        port=int(ch_cfg.get('port', 8123)),
        username=ch_cfg.get('user', 'default'),
        password=ch_cfg.get('password', ''),
        database=ch_cfg.get('database', 'trading'),
    )
    return client


def _latest_ts(client, table: str, symbol: str) -> Tuple[str, Any]:
    q = f"SELECT max(ts) AS ts_max FROM {table} WHERE symbol = %(sym)s"
    try:
        res = client.query(q, parameters={"sym": symbol})
        rows = res.result_rows
        if not rows:
            return ("no_rows", None)
        return ("ok", rows[0][0])
    except Exception as e:
        return ("error", str(e))


def main():
    ap = argparse.ArgumentParser(description="Check ClickHouse freshness per strategy/symbol")
    ap.add_argument("--symbols", nargs="*", default=None, help="Symbols to check (default: from active strategies)")
    ap.add_argument("--tables", nargs="*", default=["trading.features_1m", "trading.bars_1m_ibkr"], help="Tables to check")
    ap.add_argument("--stale-sec", type=int, default=180, help="Max staleness in seconds")
    ap.add_argument("--json", action="store_true", help="Print JSON output")
    args = ap.parse_args()

    cfg = _load_config_yaml()
    symbols = set(args.symbols) if args.symbols else _extract_active_symbols(cfg)
    if not symbols:
        # Fallback to universe if no active strategies found
        symbols = set(root_config.universe or [])

    client = _connect_ch()

    results: Dict[str, Dict[str, Any]] = {}
    now_local = now_ny()

    for sym in sorted(symbols):
        per_table: Dict[str, Any] = {}
        for table in args.tables:
            status, val = _latest_ts(client, table, sym)
            if status != "ok" or val is None:
                per_table[table] = {"ok": False, "status": status, "value": val}
                continue

            ts = val
            # Normalize to aware datetime (assume UTC if naive)
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            else:
                # Unexpected type
                per_table[table] = {"ok": False, "status": "invalid_type", "value": str(type(ts))}
                continue

            # Compare using NY-local now
            age_sec = (now_local.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
            ok = age_sec <= args.stale_sec
            per_table[table] = {"ok": ok, "age_sec": round(age_sec, 1), "latest_ts": ts.isoformat()}

        results[sym] = per_table

    client.close()

    # Overall status
    overall_ok = True
    for sym in results:
        for table in results[sym]:
            if not results[sym][table].get("ok"):
                overall_ok = False
                break

    if args.json:
        print(json.dumps({"ok": overall_ok, "results": results}, indent=2))
    else:
        print("=" * 80)
        print("ClickHouse Strategy Freshness Check")
        print("=" * 80)
        print(f"Symbols: {', '.join(sorted(symbols))}")
        print(f"Tables:  {', '.join(args.tables)}")
        print(f"Threshold: {args.stale_sec}s\n")
        for sym in sorted(results):
            print(f"[{sym}]")
            for table, r in results[sym].items():
                status = "OK" if r.get("ok") else r.get("status", "FAIL")
                age = r.get("age_sec", "-")
                latest = r.get("latest_ts", "-")
                print(f"  {table:<28} {status:<6} age={age!s:<6} latest={latest}")
            print()
        print("-" * 80)
        print(f"OVERALL: {'OK' if overall_ok else 'FAIL'}")


if __name__ == "__main__":
    main()

