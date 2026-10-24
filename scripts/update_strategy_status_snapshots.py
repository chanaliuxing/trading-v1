#!/usr/bin/env python3
"""
Populate the strategy_status_snapshots table without touching runtime services.

Usage:
    python scripts/update_strategy_status_snapshots.py
    python scripts/update_strategy_status_snapshots.py --dry-run

The script reads strategy definitions from config.yml (or config.example.yml),
derives basic metadata (id, name, symbol, enabled flag), and writes the snapshot
into PostgreSQL via asyncpg.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import asyncpg
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH_CANDIDATES = [
    ROOT / "config.yml",
    ROOT / "config.example.yml",
]


def load_config() -> Dict[str, Any]:
    for path in CONFIG_PATH_CANDIDATES:
        if path.exists():
            with path.open("r") as handle:
                return yaml.safe_load(handle) or {}
    raise FileNotFoundError("config.yml or config.example.yml not found")


def build_dsn() -> str:
    direct = os.getenv("PG_URL")
    if direct:
        return direct

    user = os.getenv("PG_USER", "postgres")
    password = os.getenv("PG_PASSWORD", "postgres")
    host = os.getenv("PG_HOST", os.getenv("PGHOST", "localhost"))
    port = os.getenv("PG_PORT", os.getenv("PGPORT", "5432"))
    database = os.getenv("PG_DB", os.getenv("PGDATABASE", "test"))
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def find_symbol(payload: Any) -> Optional[str]:
    """Depth-first search for a 'symbol' key with a string value."""
    if isinstance(payload, dict):
        if "symbol" in payload and isinstance(payload["symbol"], str):
            return payload["symbol"]
        for value in payload.values():
            result = find_symbol(value)
            if result:
                return result
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            result = find_symbol(item)
            if result:
                return result
    return None


def collect_rows(cfg: Dict[str, Any]) -> List[Tuple[str, str, Optional[str], bool, str, Optional[str]]]:
    rows: List[Tuple[str, str, Optional[str], bool, str, Optional[str]]] = []
    strategies = cfg.get("strategies") or []

    if isinstance(strategies, list) and strategies:
        for entry in strategies:
            if not isinstance(entry, dict):
                continue
            strategy_id = entry.get("id")
            if not strategy_id:
                continue
            name = entry.get("name") or strategy_id
            symbol = find_symbol(entry)
            is_active = bool(entry.get("enabled", True))
            module = entry.get("module")
            clazz = entry.get("class")
            notes_parts = [p for p in (module, clazz) if p]
            notes = ", ".join(notes_parts) if notes_parts else None
            rows.append((strategy_id, name, symbol, is_active, "config", notes))
    else:
        registry = cfg.get("strategy_registry") or {}
        if isinstance(registry, dict):
            for strategy_id, target in registry.items():
                name = str(strategy_id).replace("_", " ").title()
                notes = str(target)
                rows.append((str(strategy_id), name, None, True, "registry", notes))

    # Deduplicate by strategy_id (last one wins)
    dedup: Dict[str, Tuple[str, str, Optional[str], bool, str, Optional[str]]] = {}
    for row in rows:
        dedup[row[0]] = row
    return list(dedup.values())


async def upsert_rows(rows: Iterable[Tuple[str, str, Optional[str], bool, str, Optional[str]]], dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        query = """
        INSERT INTO public.strategy_status_snapshots(strategy_id, name, symbol, is_active, source, notes, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (strategy_id)
        DO UPDATE
        SET name = EXCLUDED.name,
            symbol = EXCLUDED.symbol,
            is_active = EXCLUDED.is_active,
            source = EXCLUDED.source,
            notes = EXCLUDED.notes,
            updated_at = now();
        """
        await conn.executemany(query, rows)
    finally:
        await conn.close()


def format_preview(rows: Iterable[Tuple[str, str, Optional[str], bool, str, Optional[str]]]) -> str:
    lines = ["strategy_id,name,symbol,is_active,source,notes"]
    for strategy_id, name, symbol, is_active, source, notes in rows:
        safe_notes = (notes or "").replace("\n", " ").replace(",", ";")
        lines.append(f"{strategy_id},{name},{symbol or ''},{int(is_active)},{source},{safe_notes}")
    return "\n".join(lines)


async def main(dry_run: bool) -> None:
    cfg = load_config()
    rows = collect_rows(cfg)
    if not rows:
        print("No strategies discovered in configuration; nothing to upsert.")
        return

    if dry_run:
        print(format_preview(rows))
        return

    dsn = build_dsn()
    await upsert_rows(rows, dsn)
    print(f"Upserted {len(rows)} strategy status rows into PostgreSQL.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update strategy_status_snapshots table.")
    parser.add_argument("--dry-run", action="store_true", help="Print rows without writing to the database.")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
