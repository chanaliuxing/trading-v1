#!/usr/bin/env python3
"""
Update symbol correlations from ClickHouse historical data.

This script calculates rolling 30-day correlations between stocks and hedge instruments
and updates the correlation matrix used by auto_hedge.py.

Usage:
    python scripts/options/update_correlations.py [--days 30] [--output auto_hedge.py]
"""

import argparse
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

LOG = get_logger(__name__)


async def calculate_correlations(
    symbols: List[str],
    hedge_instruments: List[str],
    lookback_days: int = 30,
) -> Dict[str, Dict[str, float]]:
    """Calculate rolling correlations from ClickHouse historical data.

    Args:
        symbols: List of stock symbols to calculate correlations for
        hedge_instruments: List of hedge instruments (SPY, QQQ, IWM, DIA)
        lookback_days: Number of days for rolling correlation

    Returns:
        Dict mapping symbol -> {hedge_instrument -> correlation}
    """
    ch_client = ClickHouseDB()

    all_symbols = list(set(symbols + hedge_instruments))

    # Query to calculate pairwise correlations using intraday data
    # Use bars_1m table and aggregate to daily close prices
    query = """
    WITH prices AS (
        SELECT
            symbol,
            toDate(ts_end) AS date,
            anyLast(close) AS close
        FROM trading.bars_1m
        WHERE symbol IN {symbols:Array(String)}
          AND ts_end >= today() - INTERVAL {days:UInt32} DAY
          AND toHour(ts_end) >= 9 AND toHour(ts_end) <= 16  -- RTH only
        GROUP BY symbol, date
        HAVING count(*) >= 200  -- At least 200 bars per day (reduced threshold)
    )
    SELECT
        a.symbol AS symbol1,
        b.symbol AS symbol2,
        corr(a.close, b.close) AS correlation,
        count(*) AS data_points
    FROM prices a
    JOIN prices b ON a.date = b.date
    WHERE a.symbol != b.symbol
    GROUP BY symbol1, symbol2
    HAVING data_points >= {min_days:UInt32}
    ORDER BY symbol1, symbol2
    """

    result = ch_client.query(
        query,
        {
            "symbols": all_symbols,
            "days": lookback_days + 5,  # Extra days buffer
            "min_days": max(10, lookback_days // 3),  # At least 1/3 of lookback
        }
    )

    # Parse results into correlation matrix
    correlations = {}

    for row in result:
        sym1 = row['symbol1']
        sym2 = row['symbol2']
        corr = row['correlation']
        data_points = row['data_points']

        if sym1 not in correlations:
            correlations[sym1] = {}

        correlations[sym1][sym2] = round(float(corr), 4)

    # Filter to only include stock -> hedge correlations
    filtered = {}
    for symbol in symbols:
        if symbol in correlations:
            filtered[symbol] = {}
            for hedge in hedge_instruments:
                if hedge in correlations[symbol]:
                    filtered[symbol][hedge] = correlations[symbol][hedge]

    return filtered


async def main():
    parser = argparse.ArgumentParser(description="Update correlation matrix from ClickHouse")
    parser.add_argument("--days", type=int, default=30, help="Lookback period in days")
    parser.add_argument("--symbols", type=str, help="Comma-separated symbols (default: TSLA,NVDA,AAPL,AMZN,MSFT,GOOGL)")
    parser.add_argument("--output", type=Path, help="Output file (default: stdout)")
    args = parser.parse_args()

    # Default symbols
    symbols = ["TSLA", "NVDA", "AAPL", "AMZN", "MSFT", "GOOGL"]
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]

    hedge_instruments = ["SPY", "QQQ", "IWM", "DIA"]

    print(f"Calculating {args.days}-day rolling correlations...")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Hedge instruments: {', '.join(hedge_instruments)}")

    try:
        correlations = await calculate_correlations(symbols, hedge_instruments, args.days)

        if not correlations:
            print("\n⚠️  No correlation data found in ClickHouse")
            print("   Ensure trading.bars_1m table has recent data for these symbols")
            return

        # Print correlation matrix
        print("\n" + "=" * 80)
        print(f"SYMBOL CORRELATIONS ({args.days}-day rolling)")
        print("=" * 80)

        for symbol in sorted(correlations.keys()):
            print(f"\n{symbol}:")
            for hedge, corr in sorted(correlations[symbol].items()):
                print(f"  {hedge}: {corr:+.4f}")

        # Generate Python code
        print("\n" + "=" * 80)
        print("PYTHON CODE FOR auto_hedge.py")
        print("=" * 80)
        print("\nSYMBOL_CORRELATIONS = {")
        for symbol in sorted(correlations.keys()):
            corr_dict = ", ".join([f'"{h}": {c}' for h, c in sorted(correlations[symbol].items())])
            print(f'    "{symbol}": {{{corr_dict}}},')
        print("}")

        # Write to file if specified
        if args.output:
            # Read existing file
            content = args.output.read_text()

            # Find and replace SYMBOL_CORRELATIONS
            lines = content.split('\n')
            new_lines = []
            in_correlation_block = False
            indent = ""

            i = 0
            while i < len(lines):
                line = lines[i]

                if 'SYMBOL_CORRELATIONS = {' in line:
                    # Start of correlation block
                    in_correlation_block = True
                    indent = line[:len(line) - len(line.lstrip())]

                    # Write new correlations
                    new_lines.append(f"{indent}SYMBOL_CORRELATIONS = {{")
                    for symbol in sorted(correlations.keys()):
                        corr_dict = ", ".join([f'"{h}": {c}' for h, c in sorted(correlations[symbol].items())])
                        new_lines.append(f'{indent}    "{symbol}": {{{corr_dict}}},')
                    new_lines.append(f"{indent}}}")

                    # Skip old correlation block
                    i += 1
                    while i < len(lines) and not lines[i].strip().startswith('}'):
                        i += 1
                    i += 1  # Skip closing brace
                    continue

                new_lines.append(line)
                i += 1

            # Write back
            args.output.write_text('\n'.join(new_lines))
            print(f"\n✅ Updated {args.output}")

        print("\n" + "=" * 80)
        print(f"✅ Correlations calculated successfully")
        print(f"   Data points: {sum(len(v) for v in correlations.values())} pairs")
        print(f"   Symbols: {len(correlations)}")
        print("=" * 80)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
