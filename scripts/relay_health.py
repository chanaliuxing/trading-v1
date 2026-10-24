"""Relay upgrade health checks: coverage and freshness derived from ClickHouse."""
from __future__ import annotations

import argparse
import re
from typing import Any, Dict, Optional

import pandas as pd
from clickhouse_driver import Client


def query_df(cli: Client, query: str, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    data, meta = cli.execute(query, params or {}, with_column_types=True)
    cols = [name for name, _ in meta]
    return pd.DataFrame(data, columns=cols)


def _normalize_interval(interval: str) -> str:
    pattern = r"^(\d{1,4})\s+(MINUTE|MINUTES|HOUR|HOURS|DAY|DAYS)$"
    match = re.fullmatch(pattern, interval.strip().upper())
    if not match:
        raise ValueError("coverage-window must look like '1 HOUR' or '15 MINUTES'")
    value, unit = match.groups()
    if unit.endswith("S") and int(value) == 1:
        unit = unit[:-1]
    return f"{value} {unit}"


def coverage_report(cli: Client, args: argparse.Namespace) -> pd.DataFrame:
    lookback = _normalize_interval(args.coverage_window)
    base_query = """
        SELECT symbol,
               avg(lstm_oof_h5 IS NOT NULL) AS coverage
        FROM trading.features_1m_with_oof
        WHERE ts >= now() - INTERVAL {window}
    """
    if args.symbol:
        base_query += " AND symbol = %(symbol)s"
    base_query += " GROUP BY symbol ORDER BY coverage ASC"
    query = base_query.format(window=lookback)
    if args.symbol:
        params = {"symbol": args.symbol}
    else:
        params = None
    return query_df(cli, query, params)


def freshness_report(cli: Client, args: argparse.Namespace) -> pd.DataFrame:
    base_query = """
        SELECT symbol,
               max(updated_at) AS last_update,
               toUInt32((toUnixTimestamp(now()) - toUnixTimestamp(max(updated_at))) / 60) AS lag_minutes
        FROM trading.oof_lstm_h5
    """
    if args.run_id:
        base_query += " WHERE run_id = %(run_id)s"
    base_query += " GROUP BY symbol ORDER BY lag_minutes DESC"
    params: Dict[str, Any] = {}
    if args.run_id:
        params["run_id"] = args.run_id
    return query_df(cli, base_query, params)


def rowcount_report(cli: Client, args: argparse.Namespace) -> pd.DataFrame:
    if not args.run_id:
        return pd.DataFrame()
    query = """
        SELECT run_id, count() AS rows
        FROM trading.oof_lstm_h5
        WHERE run_id = %(run_id)s
        GROUP BY run_id
    """
    return query_df(cli, query, {"run_id": args.run_id})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check relay OOF coverage and freshness")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--user", default="default")
    parser.add_argument("--password", default="")
    parser.add_argument("--database", default="trading")
    parser.add_argument("--run-id", "--run_id", default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--coverage-window", "--coverage_window", default="1 HOUR",
                        help="ClickHouse interval literal, e.g. '1 HOUR' or '1 DAY'")
    parser.add_argument("--no-freshness", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cli = Client(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        settings={"use_numpy": True},
    )
    cover_df = coverage_report(cli, args)
    if not cover_df.empty:
        print("\nCoverage (lstm_oof_h5):")
        print(cover_df.to_string(index=False))
    else:
        print("No coverage rows returned.")

    if not args.no_freshness:
        fresh_df = freshness_report(cli, args)
        if not fresh_df.empty:
            print("\nFreshness (lag_minutes):")
            print(fresh_df.to_string(index=False))
        else:
            print("\nNo freshness rows returned.")

    rc_df = rowcount_report(cli, args)
    if not rc_df.empty:
        print("\nRow count for run_id:")
        print(rc_df.to_string(index=False))


if __name__ == "__main__":
    main()
