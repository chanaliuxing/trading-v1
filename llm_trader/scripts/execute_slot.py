#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add llm_trader root to path
script_dir = Path(__file__).parent.parent  # ./llm_trader
sys.path.insert(0, str(script_dir))

from llm_trader.daily_executor import execute_slot


def main():
    ap = argparse.ArgumentParser(description="Execute one slot of the daily plan (P0)")
    ap.add_argument("--slot", required=True, help="Slot id, e.g., premarket_08:00, intraday_10:30, eod_15:58")
    ap.add_argument("--base", default=None, help="Base runs dir for llm_trader (default runs/llm_trader)")
    args = ap.parse_args()

    res = execute_slot(args.slot, args.base)
    print(res)


if __name__ == "__main__":
    main()

