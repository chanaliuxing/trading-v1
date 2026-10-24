#!/usr/bin/env python3
"""
Debug script to check VWAP signals during backtest
"""
import pandas as pd
import numpy as np
import clickhouse_connect

# Fetch Oct 10 data (day with -3.7% VWAP deviation)
client = clickhouse_connect.get_client(host='localhost', port=8123, username='default', password='')

query = """
SELECT
    ts,
    open,
    high,
    low,
    close,
    volume
FROM trading.features_1m
WHERE symbol='TSLA'
  AND toDate(ts) = '2025-10-10'
  AND toHour(ts) >= 9 AND toHour(ts) < 16
ORDER BY ts
"""

df = client.query_df(query)
print(f"Loaded {len(df)} bars for 2025-10-10")

# Calculate session VWAP
df['pq'] = df['close'] * df['volume']
df['vwap'] = df['pq'].cumsum() / df['volume'].cumsum()
df['dev'] = (df['close'] - df['vwap']) / df['vwap']

# Find bars where deviation <= -0.001 (ultra-relaxed threshold)
signals = df[df['dev'] <= -0.001].copy()

print(f"\n=== Bars meeting A1_thr_sigma=-0.001 threshold ===")
print(f"Total signal bars: {len(signals)}")

if len(signals) > 0:
    print("\nTop 10 strongest signals:")
    signals_sorted = signals.sort_values('dev')
    print(signals_sorted[['ts', 'close', 'vwap', 'dev', 'volume']].head(10).to_string())

    # Check at 11:33 (the -3.7% deviation we found earlier)
    bar_1133 = df[df['ts'] == '2025-10-10 11:33:00']
    if not bar_1133.empty:
        print(f"\n=== Bar at 11:33 (strongest deviation) ===")
        print(bar_1133[['ts', 'close', 'vwap', 'dev', 'volume']].to_string())
else:
    print("\nNO bars meeting threshold!")
    print("\nClosest deviations:")
    print(df.nsmallest(10, 'dev')[['ts', 'close', 'vwap', 'dev', 'volume']].to_string())
