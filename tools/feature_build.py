#!/usr/bin/env python3
from __future__ import annotations

"""
Feature Build (P1 minimal, ClickHouse view)

Reads config/features_catalog.json and prepares candidate features by creating
or replacing a ClickHouse VIEW that projects candidate expressions on top of
the baseline features table (trading.features_1m).

Outputs:
  - Creates/updates view: trading.features_1m_candidate_v{version}
  - runs/ai_trader/YYYY-MM-DD/features_build/status.json
"""

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Set


def _cfg_ai_trader() -> Dict[str, Any]:
    p = Path("scripts/ai_trader/config/ai_trader.yaml")
    try:
        import yaml
        return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _ch_client():
    try:
        import clickhouse_connect
    except Exception as e:
        raise RuntimeError(f"clickhouse_connect not installed: {e}")
    cfg = _cfg_ai_trader().get("clickhouse", {}) or {}
    # Defaults (port 8123 for clickhouse-connect HTTP driver)
    host = "localhost"; port = 8123; user = "default"; password = ""; database = "trading"
    try:
        dsn = (cfg.get("dsn") or "").strip()
        toks = dsn.split()
        for i, t in enumerate(toks):
            if t == "--host" and i+1 < len(toks): host = toks[i+1]
            if t == "--port" and i+1 < len(toks): port = int(toks[i+1])
            if t == "--user" and i+1 < len(toks): user = toks[i+1]
            if t == "--password" and i+1 < len(toks): password = toks[i+1]
            if t == "--database" and i+1 < len(toks): database = toks[i+1]
    except Exception:
        pass
    return clickhouse_connect.get_client(host=host, port=port, username=user, password=password, database=database)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default="config/features_catalog.json")
    ap.add_argument("--out", default=None, help="Output dir (defaults to runs/ai_trader/<today>/features_build)")
    args = ap.parse_args()

    dstr = date.today().isoformat()
    out_dir = Path(args.out) if args.out else (Path("runs/ai_trader") / dstr / "features_build")
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        (out_dir / "status.json").write_text(json.dumps({"ok": False, "error": "catalog_missing"}, indent=2), encoding="utf-8")
        print(json.dumps({"ok": False, "error": "catalog_missing"}))
        return

    try:
        catalog = json.loads(catalog_path.read_text("utf-8"))
    except Exception as e:
        (out_dir / "status.json").write_text(json.dumps({"ok": False, "error": f"catalog_parse:{e}"}, indent=2), encoding="utf-8")
        print(json.dumps({"ok": False, "error": f"catalog_parse:{e}"}))
        return

    version = str(catalog.get("version") or date.today().isoformat())
    candidates = catalog.get("candidate") or []
    specs = [c for c in candidates if isinstance(c, dict) and c.get("name")]

    # Filter out specs that use unsupported or dangerous SQL constructs for ClickHouse.
    # This prevents Brain-generated standard SQL (e.g. LAG, MEAN, STDDEV) or
    # unavailable order-book aliases from breaking the entire feature_build pipeline
    # while we migrate toward DSL/CH-compatible forms.
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
        "vwap_ma_20",
        "volatility_30",
        "avg_spread",
        "bid_ask_imbalance",
        "avg_daily_spread",
    )
    filtered_specs: List[Dict[str, Any]] = []
    skipped_unsupported: List[str] = []
    for s in specs:
        expr_raw = str(s.get("expr") or s.get("sql") or s.get("expr_sql") or "")
        expr_lower = expr_raw.lower()
        if any(bad in expr_lower for bad in banned_fragments):
            skipped_unsupported.append(str(s.get("name")))
            continue
        filtered_specs.append(s)
    specs = filtered_specs

    # Extra safety: drop specs that reference columns not present in the
    # current ClickHouse schemas for trading.features_1m / features_5m /
    # features_1d_prev. This prevents a single bad spec from breaking the
    # entire feature_build pipeline due to UNKNOWN_IDENTIFIER.
    def _extract_refs(expr: str):
        pattern = re.compile(r"\b(f|m5|d)\.([A-Za-z0-9_]+)\b")
        return [(m.group(1), m.group(2)) for m in pattern.finditer(expr)]

    try:
        cli = _ch_client()
        cols_f = {r.get("name") or r.get("column") for r in cli.query("DESCRIBE TABLE trading.features_1m").named_results()}
    except Exception:
        cols_f = set()
    try:
        cols_m5 = {r.get("name") or r.get("column") for r in cli.query("DESCRIBE TABLE trading.features_5m").named_results()}
    except Exception:
        cols_m5 = set()
    try:
        cols_d = {r.get("name") or r.get("column") for r in cli.query("DESCRIBE TABLE trading.features_1d_prev").named_results()}
    except Exception:
        cols_d = set()

    valid_specs: List[Dict[str, Any]] = []
    for s in specs:
        expr_raw = str(s.get("expr") or s.get("sql") or s.get("expr_sql") or "")
        expr_lower = expr_raw.lower()
        # Drop specs that still contain a bare 'op' identifier in the final
        # expression. This is almost always a sign of a malformed DSL→SQL
        # translation (e.g. placeholders like '(op) * (col)') and would cause
        # ClickHouse to raise UNKNOWN_IDENTIFIER. We treat such specs as
        # unsupported until they are regenerated by the DSL compiler.
        if re.search(r"\bop\b", expr_lower):
            skipped_unsupported.append(str(s.get("name")))
            continue

        refs = _extract_refs(expr_raw)
        missing_f: Set[str] = set()
        missing_m5: Set[str] = set()
        missing_d: Set[str] = set()
        for alias, col in refs:
            if alias == "f" and cols_f and col not in cols_f:
                missing_f.add(col)
            elif alias == "m5" and cols_m5 and col not in cols_m5:
                missing_m5.add(col)
            elif alias == "d" and cols_d and col not in cols_d:
                missing_d.add(col)
        if missing_f or missing_m5 or missing_d:
            skipped_unsupported.append(str(s.get("name")))
            continue
        valid_specs.append(s)
    specs = valid_specs

    extra_source_defs = {
        "m5": {
            "table": "trading.features_5m",
            "join": "{alias}.symbol = f.symbol AND {alias}.ts = toStartOfInterval(f.ts, INTERVAL 5 MINUTE)",
        },
        "d": {
            "table": "trading.features_1d_prev",
            "join": "{alias}.symbol = f.symbol AND {alias}.ts = toStartOfDay(f.ts)",
        },
    }

    required_aliases: Set[str] = set()
    for s in specs:
        expr_str = str(s.get("expr") or s.get("sql") or s.get("expr_sql") or "")
        declared = {alias for alias in s.get("required_sources", []) if alias in extra_source_defs}
        autodetected = {alias for alias in extra_source_defs if f"{alias}." in expr_str}
        resolved = declared | autodetected
        if resolved:
            s["_resolved_sources"] = sorted(resolved)
            required_aliases.update(resolved)

    # Build a CREATE OR REPLACE VIEW that selects baseline plus candidate expressions
    view_name = f"trading.features_1m_candidate_v{version.replace('-', '')}"
    select_cols = ["f.*"]
    added = []
    for s in specs:
        name = str(s.get("name")).strip()
        expr = s.get("expr") or s.get("sql") or s.get("expr_sql")
        if not name or not expr:
            continue
        added.append(name)
        select_cols.append(f"({expr}) AS `{name}`")

    ok = True
    err = ""
    try:
        cli = _ch_client()
        # Ensure database exists
        try:
            cli.command("CREATE DATABASE IF NOT EXISTS trading")
        except Exception:
            pass
        # Proactively drop any existing object with the same name (view/table/materialized view)
        for drop_stmt in (
            f"DROP VIEW IF EXISTS {view_name}",
            f"DROP TABLE IF EXISTS {view_name}",
            f"DROP MATERIALIZED VIEW IF EXISTS {view_name}",
        ):
            try:
                cli.command(drop_stmt)
            except Exception:
                # Ignore drop errors to maximize resilience in tests
                pass

        # Create view projecting candidate expressions on top of baseline table
        select_sql = ",\n            ".join(select_cols)
        joins_sql = ""
        for alias in sorted(required_aliases):
            cfg = extra_source_defs.get(alias)
            if not cfg:
                continue
            join_clause = cfg["join"].format(alias=alias)
            joins_sql += f"\n        LEFT JOIN {cfg['table']} AS {alias}\n            ON {join_clause}"

        ddl = f"""
        CREATE VIEW {view_name} AS
        SELECT
            {select_sql}
        FROM trading.features_1m AS f
        {joins_sql}
        """
        cli.command(ddl)
    except Exception as e:
        ok = False
        err = str(e)

    status: Dict[str, Any] = {
        "ok": ok,
        "version": version,
        "built": len(added),
        "view": view_name,
        # Local wall-clock time for operator readability
        "ts": datetime.now().isoformat(),
        "error": err,
        "skipped_unsupported": skipped_unsupported,
        "added": added,
    }
    (out_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status))


if __name__ == "__main__":
    main()
