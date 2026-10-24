#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tool.rebuild_daily_atr_context import rebuild_daily_atr_context


def main() -> int:
    result = rebuild_daily_atr_context(symbol="SPY")
    print(f"rebuilt {result['out_path']} rows={result['rows']} symbol={result['symbol']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
