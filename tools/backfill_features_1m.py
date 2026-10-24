"""
tools/backfill_features_1m.py

Compute features_1m for a historical time range using bars_1m_ibkr.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import sys
from typing import List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.market.features.feature_calculator import FeatureCalculator


def _parse_dt(value: str) -> datetime:
    if len(value.strip()) == 10:
        return datetime.fromisoformat(f"{value.strip()} 00:00:00")
    return datetime.fromisoformat(value.strip())


async def _run(start: datetime, end: datetime, symbols: List[str]) -> None:
    calc = FeatureCalculator(resolution="1m")
    await calc.compute_features_for_range(start, end, symbols or None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--symbols", default="")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_dt = _parse_dt(args.start)
    end_dt = _parse_dt(args.end)
    asyncio.run(_run(start_dt, end_dt, symbols))


if __name__ == "__main__":
    main()
