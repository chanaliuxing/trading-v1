#!/usr/bin/env python3
"""
Load benchmark index bars (e.g. SPY, QQQ) into ClickHouse.

Requires:
    pip install clickhouse-driver yfinance pandas

Example:
    python scripts/load_market_index_snapshots.py --symbols SPY QQQ --period 1y --interval 1d
"""
from __future__ import annotations

import argparse
import os
from typing import Iterable, List, Sequence, Tuple

try:
    from clickhouse_driver import Client
except ImportError as exc:  # pragma: no cover - optional dependency
    raise SystemExit("Missing dependency 'clickhouse-driver'. Install via `pip install clickhouse-driver`.") from exc

try:
    import yfinance as yf
except ImportError as exc:  # pragma: no cover - optional dependency
    raise SystemExit("Missing dependency 'yfinance'. Install via `pip install yfinance pandas`.") from exc


def fetch_symbol_rows(symbol: str, period: str, interval: str) -> List[Tuple]:
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if df.empty:
        return []
    df = df.dropna()
    rows: List[Tuple] = []
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime()
        rows.append(
            (
                ts,
                symbol,
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                int(row["Volume"]),
            )
        )
    return rows


def load_indices(symbols: Sequence[str], period: str, interval: str, dry_run: bool = False) -> None:
    rows: List[Tuple] = []
    for symbol in symbols:
        symbol_rows = fetch_symbol_rows(symbol, period, interval)
        if not symbol_rows:
            print(f"[warn] No data retrieved for {symbol} ({period}, {interval}).")
        rows.extend(symbol_rows)

    if not rows:
        print("[warn] Nothing to load; aborting.")
        return

    if dry_run:
        print(f"[dry-run] Prepared {len(rows)} rows for {', '.join(symbols)}.")
        return

    client = Client(
        host=os.getenv("CH_HOST", "localhost"),
        port=int(os.getenv("CH_PORT", 9000)),
        user=os.getenv("CH_USER", "default"),
        password=os.getenv("CH_PASSWORD", ""),
        database=os.getenv("CH_DB", "trading"),
    )
    client.execute("INSERT INTO trading.market_index_snapshots VALUES", rows)
    print(f"[ok] Inserted {len(rows)} rows into trading.market_index_snapshots.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load market index bars into ClickHouse.")
    parser.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"], help="Symbols to load (default: SPY QQQ).")
    parser.add_argument("--period", default="1y", help="yfinance period (default: 1y).")
    parser.add_argument("--interval", default="1d", help="yfinance interval (default: 1d).")
    parser.add_argument("--dry-run", action="store_true", help="Only show counts, do not load.")
    args = parser.parse_args()

    load_indices(args.symbols, args.period, args.interval, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
