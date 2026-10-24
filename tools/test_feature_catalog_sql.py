#!/usr/bin/env python3
from __future__ import annotations
"""
Test helper: scan config/features_catalog.json for invalid ClickHouse column references.

Usage (from repo root, with venv):
  venv/bin/python tools/test_feature_catalog_sql.py

What it does:
  - Loads trading.features_1m, trading.features_5m, trading.features_1d_prev schemas
  - Parses each candidate feature expression for f./m5./d. column references
  - Reports any columns that do not exist in the corresponding table
"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple


def _cfg_ai_trader() -> Dict:
    p = Path("scripts/ai_trader/config/ai_trader.yaml")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _ch_client():
    try:
        import clickhouse_connect  # type: ignore
    except Exception as e:
        raise RuntimeError(f"clickhouse_connect not installed: {e}")
    cfg = _cfg_ai_trader().get("clickhouse", {}) or {}
    host = "localhost"
    port = 8123
    user = "default"
    password = ""
    database = "trading"
    try:
        dsn = (cfg.get("dsn") or "").strip()
        toks = dsn.split()
        for i, t in enumerate(toks):
            if t == "--host" and i + 1 < len(toks):
                host = toks[i + 1]
            if t == "--port" and i + 1 < len(toks):
                port = int(toks[i + 1])
            if t == "--user" and i + 1 < len(toks):
                user = toks[i + 1]
            if t == "--password" and i + 1 < len(toks):
                password = toks[i + 1]
            if t == "--database" and i + 1 < len(toks):
                database = toks[i + 1]
    except Exception:
        pass
    return clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password, database=database
    )


def _describe_columns(cli, table: str) -> Set[str]:
    try:
        rows = cli.query(f"DESCRIBE TABLE {table}").named_results()
        return {r.get("name") or r.get("column") for r in rows}
    except Exception as exc:
        print(f"[WARN] DESCRIBE TABLE {table} failed: {exc}", file=sys.stderr)
        return set()


def _load_catalog(path: Path) -> List[Dict]:
    if not path.exists():
        raise SystemExit(f"features catalog not found: {path}")
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception as exc:
        raise SystemExit(f"failed to parse {path}: {exc}")
    candidates = data.get("candidate") or []
    specs = [c for c in candidates if isinstance(c, dict) and c.get("name")]
    return specs


def _extract_refs(expr: str) -> List[Tuple[str, str]]:
    """
    Return list of (alias, column) pairs for f./m5./d. references.
    Example: 'f.ret_10 / nullIf(m5.vwap, 0)' -> [('f','ret_10'), ('m5','vwap')]
    """
    out: List[Tuple[str, str]] = []
    pattern = re.compile(r"\b(f|m5|d)\.([A-Za-z0-9_]+)\b")
    for m in pattern.finditer(expr):
        alias, col = m.group(1), m.group(2)
        out.append((alias, col))
    return out


def main() -> None:
    catalog_path = Path("config/features_catalog.json")
    specs = _load_catalog(catalog_path)

    try:
        cli = _ch_client()
    except Exception as exc:
        print(f"[ERROR] Failed to connect to ClickHouse: {exc}", file=sys.stderr)
        raise SystemExit(1)

    # Load column sets
    cols_f = _describe_columns(cli, "trading.features_1m")
    cols_m5 = _describe_columns(cli, "trading.features_5m")
    cols_d = _describe_columns(cli, "trading.features_1d_prev")

    if not cols_f:
        print("[ERROR] trading.features_1m has no columns (DESCRIBE failed?)", file=sys.stderr)
        raise SystemExit(1)

    print("=== Base table columns ===")
    print(f"features_1m: {len(cols_f)} columns")
    print(f"features_5m: {len(cols_m5)} columns")
    print(f"features_1d_prev: {len(cols_d)} columns")
    print()

    any_missing = False
    # Keep banned fragments in sync with runtime filters so that specs which
    # are never built (because they rely on unsupported SQL or unavailable
    # order-book aliases) do not block catalog validation.
    banned_fragments = (
        "lag(",
        "mean(",
        "stddev(",
        "stddevpop(",
        "stddevsamp(",
        "movingaverage(",
        "runningdifference(",
        "linearregressionslope(",
        "neighbor(",
        "rows between",
        # Order-book / microstructure columns that are not present
        # in the current ClickHouse schema (sandbox-only).
        "bidask_midpoint",
        "bid_price",
        "ask_price",
        "ask_size",
        "bid_size",
        "buy_volume",
        "sell_volume",
        "total_volume",
        "vwap_ma_5",
        "volatility_30",
        "avg_spread",
        "bid_ask_imbalance",
        "avg_daily_spread",
    )
    for spec in specs:
        name = str(spec.get("name"))
        expr = str(spec.get("expr") or spec.get("sql") or spec.get("expr_sql") or "")
        expr_lower = expr.lower()
        # Skip specs that contain banned fragments; these are filtered out at
        # runtime by feature_build and are treated as sandbox-only.
        if any(bad in expr_lower for bad in banned_fragments):
            continue
        # Hard fail if expr still contains a bare 'op' identifier, which is
        # almost always a sign of a malformed DSL→SQL translation (e.g.
        # placeholders like '(op) * (col)'). This prevents legacy bad specs
        # from silently remaining in the catalog.
        if re.search(r"\bop\b", expr_lower):
            any_missing = True
            print(f"[BAD_EXPR] spec '{name}': expr contains bare identifier 'op'", file=sys.stderr)
            print(f"  expr: {expr}")
            print()
            continue

        refs = _extract_refs(expr)
        missing_for_spec: Dict[str, Set[str]] = {"f": set(), "m5": set(), "d": set()}
        for alias, col in refs:
            if alias == "f" and col not in cols_f:
                missing_for_spec["f"].add(col)
            elif alias == "m5" and cols_m5 and col not in cols_m5:
                missing_for_spec["m5"].add(col)
            elif alias == "d" and cols_d and col not in cols_d:
                missing_for_spec["d"].add(col)
        # Remove empty alias sets
        missing_for_spec = {k: v for k, v in missing_for_spec.items() if v}
        if missing_for_spec:
            any_missing = True
            print(f"[MISSING] spec '{name}':")
            for alias, cols in missing_for_spec.items():
                print(f"  - {alias}.: unknown columns: {', '.join(sorted(cols))}")
            print(f"  expr: {expr}")
            print()

    if not any_missing:
        print("✅ No unknown column references detected in features_catalog.json")
    else:
        print("⚠️ Unknown column references detected. See [MISSING] entries above.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
