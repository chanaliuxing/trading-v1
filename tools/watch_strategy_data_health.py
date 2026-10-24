#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Watch strategy config for symbol changes and dynamically check data health.

What it does:
- Parses config.yml to discover ENABLED strategies and their symbols.
- Periodically (or on change) checks:
  - ClickHouse freshness per symbol per table (default: features_1m, bars_1m_ibkr)
  - Optional: Redis realtime readiness for LLM Trader per symbol (1s list)

Outputs a concise summary to stdout and optionally writes a JSON health file.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import yaml
import clickhouse_connect
from datetime import datetime, timezone

try:
    import redis.asyncio as redis  # Optional for --check-redis
except Exception:
    redis = None  # type: ignore

from libs.config import config as root_config
from llm_trader.llm_trader.ny_time import now_ny


def load_cfg() -> Dict[str, Any]:
    for fname in ("config.yml", "config.yaml", "config.example.yml"):
        p = Path(fname)
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}
    return {}


def extract_symbols(cfg: Dict[str, Any]) -> Set[str]:
    syms: Set[str] = set()
    strategies = cfg.get("strategies") or []
    for strat in strategies:
        if not strat or not strat.get("enabled"):
            continue
        extras = ((strat.get("params") or {}).get("extras")) or {}
        vals = extras.get("symbols")
        if isinstance(vals, list):
            for s in vals:
                if isinstance(s, str):
                    syms.add(s.upper())
        v = extras.get("symbol")
        if isinstance(v, str):
            syms.add(v.upper())
        for k, v in extras.items():
            if isinstance(v, dict):
                sym = v.get("symbol")
                if isinstance(sym, str):
                    syms.add(sym.upper())
        mrs = extras.get("market_regime_symbols")
        if isinstance(mrs, list):
            for s in mrs:
                if isinstance(s, str):
                    syms.add(s.upper())
    return syms


def connect_ch():
    ch_cfg = root_config.clickhouse_config
    client = clickhouse_connect.get_client(
        host=ch_cfg.get('host', 'localhost'),
        port=int(ch_cfg.get('port', 8123)),
        username=ch_cfg.get('user', 'default'),
        password=ch_cfg.get('password', ''),
        database=ch_cfg.get('database', 'trading'),
    )
    return client


def latest_ts(client, table: str, symbol: str) -> Tuple[str, Any]:
    q = f"SELECT max(ts) AS ts_max FROM {table} WHERE symbol = %(sym)s"
    try:
        res = client.query(q, parameters={"sym": symbol})
        rows = res.result_rows
        if not rows:
            return ("no_rows", None)
        return ("ok", rows[0][0])
    except Exception as e:
        return ("error", str(e))


async def check_redis_symbols(symbols: List[str], redis_url: str, stale_sec: int, min_len: int) -> Dict[str, Any]:
    if redis is None:
        return {"ok": False, "error": "redis lib not available"}
    r = await redis.from_url(redis_url)
    out = {}
    try:
        import json as _J, time as _T
        now_ms = int(_T.time() * 1000)
        ok_all = True
        for sym in symbols:
            key = f"sfs:1s:{sym}"
            try:
                latest = await r.lindex(key, 0)
                llen = await r.llen(key)
                if not latest:
                    out[sym] = {"ok": False, "reason": "no_data", "llen": int(llen)}
                    ok_all = False
                    continue
                bar = _J.loads(latest)
                ts = int(bar.get("ts", 0))
                age_sec = (now_ms - ts) / 1000.0
                ok = (age_sec <= stale_sec) and (llen >= min_len)
                if not ok:
                    ok_all = False
                out[sym] = {
                    "ok": ok, "age_sec": round(age_sec, 2), "llen": int(llen),
                    "reason": None if ok else ("stale" if age_sec > stale_sec else "insufficient_context")
                }
            except Exception as e:
                ok_all = False
                out[sym] = {"ok": False, "error": str(e)}
        out["ok"] = ok_all
        return out
    finally:
        await r.close()


def main():
    ap = argparse.ArgumentParser(description="Watch strategy data health and refresh on config changes")
    ap.add_argument("--interval", type=int, default=60, help="Seconds between checks")
    ap.add_argument("--tables", nargs="*", default=["trading.features_1m", "trading.bars_1m_ibkr"], help="ClickHouse tables")
    ap.add_argument("--stale-sec", type=int, default=180, help="Max CH staleness in seconds")
    ap.add_argument("--check-redis", action="store_true", help="Also check Redis 1s readiness for discovered symbols")
    ap.add_argument("--redis-url", default=None, help="Redis URL (defaults to STATEBUS_REDIS)")
    ap.add_argument("--out", default=None, help="Optional path to write JSON health snapshot")
    args = ap.parse_args()

    cfg_path = Path("config.yml")
    last_mtime = cfg_path.stat().st_mtime if cfg_path.exists() else 0
    prev_symbols: Set[str] = set()

    while True:
        try:
            # Re-parse config on every loop (cheap) and detect symbol set
            cfg = load_cfg()
            symbols = extract_symbols(cfg)
            if not symbols:
                # Fallback to universe
                universe = root_config.universe or []
                symbols = set(universe)

            # Log symbol delta
            if symbols != prev_symbols:
                added = symbols - prev_symbols
                removed = prev_symbols - symbols
                print("[watch] symbol set changed:")
                print(f"  + {sorted(list(added))}")
                print(f"  - {sorted(list(removed))}")
                prev_symbols = set(symbols)

            # ClickHouse checks
            client = connect_ch()
            now_local = now_ny()
            ch_results: Dict[str, Dict[str, Any]] = {}
            overall_ch_ok = True
            for sym in sorted(symbols):
                per_table: Dict[str, Any] = {}
                for table in args.tables:
                    status, val = latest_ts(client, table, sym)
                    if status != "ok" or val is None:
                        per_table[table] = {"ok": False, "status": status, "value": val}
                        overall_ch_ok = False
                        continue
                    ts = val
                    if isinstance(ts, datetime):
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                    else:
                        per_table[table] = {"ok": False, "status": "invalid_type", "value": str(type(ts))}
                        overall_ch_ok = False
                        continue
                    age_sec = (now_local.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
                    ok = age_sec <= args.stale_sec
                    if not ok:
                        overall_ch_ok = False
                    per_table[table] = {"ok": ok, "age_sec": round(age_sec, 1), "latest_ts": ts.isoformat()}
                ch_results[sym] = per_table
            client.close()

            # Optional Redis checks
            redis_results = None
            if args.check_redis:
                url = args.redis_url or root_config.redis_config
                # Avoid blocking main loop for too long; Redis check runs quickly
                import asyncio
                redis_results = asyncio.run(check_redis_symbols(sorted(symbols), url, stale_sec=10, min_len=30))

            # Print summary
            print("=" * 80)
            print(f"Strategy Data Health @ {now_local.isoformat()}")
            print("=" * 80)
            print(f"Symbols: {', '.join(sorted(symbols))}")
            print(f"CH Tables: {', '.join(args.tables)} | Threshold: {args.stale_sec}s")
            print(f"ClickHouse: {'OK' if overall_ch_ok else 'FAIL'}")
            for sym in sorted(ch_results):
                for table, r in ch_results[sym].items():
                    status = "OK" if r.get("ok") else r.get("status", "FAIL")
                    age = r.get("age_sec", "-")
                    latest = r.get("latest_ts", "-")
                    print(f"  {sym:<6} {table:<28} {status:<6} age={age!s:<6} latest={latest}")
            if redis_results is not None:
                print("-" * 80)
                ok = redis_results.get("ok")
                print(f"Redis 1s: {'OK' if ok else 'FAIL'}")
                for sym in sorted(symbols):
                    r = redis_results.get(sym, {})
                    status = "OK" if r.get("ok") else r.get("reason", r.get("error", "FAIL"))
                    print(f"  {sym:<6} {status:<8} age={str(r.get('age_sec', '-')):<6} llen={str(r.get('llen', '-')):<6}")

            # Optional JSON output
            if args.out:
                payload = {
                    "ts": now_local.isoformat(),
                    "symbols": sorted(list(symbols)),
                    "clickhouse": ch_results,
                    "clickhouse_ok": overall_ch_ok,
                    "redis": redis_results,
                }
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(json.dumps(payload, indent=2))

        except Exception as e:
            print(f"[watch] error: {e}")

        # Sleep until next loop
        time.sleep(max(5, int(args.interval)))


if __name__ == "__main__":
    main()

