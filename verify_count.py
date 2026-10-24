import argparse
import json
from datetime import datetime
from typing import List, Dict, Any, Tuple
import sys
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

import clickhouse_connect
from libs.config import config

# Copied from ch_pool.py and modified to NOT set the session timezone
def get_client_no_tz(**kwargs):
    ch_config = config.clickhouse_config
    final_kwargs = {
        'host': ch_config.get('host', 'localhost'),
        'port': int(ch_config.get('port', 8123)),
        'database': ch_config.get('database', 'trading'),
        'username': ch_config.get('user', 'default'),
        'password': ch_config.get('password', ''),
    }
    final_kwargs.update(kwargs)
    client = clickhouse_connect.get_client(**final_kwargs)
    # The original get_client() sets session_timezone here. We are omitting it.
    return client

def _resolve_ts_column(cli, table: str) -> str:
    # Simplified for this test
    return "ts"

def main():
    cli = get_client_no_tz()
    table = 'trading.features_1m'
    sym = 'NVDA'
    s = '2025-11-28 09:30:00'
    e = '2025-11-28 16:00:00'
    ts_col = _resolve_ts_column(cli, table)

    # This is the same query logic as in _seed_active
    query = f"SELECT count() FROM {table} FINAL WHERE symbol = %(sym)s AND {ts_col} >= toDateTime(%(s)s) AND {ts_col} < toDateTime(%(e)s)"
    count = cli.query(query, parameters={"sym": sym, "s": s, "e": e}).result_rows[0][0]

    print(f"Direct count for {sym} in {table} from {s} to {e} is: {count}")

if __name__ == "__main__":
    main()
