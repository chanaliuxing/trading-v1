#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.options_kernel.harness import run_clickhouse_day_parity_scenario_sync


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a full-day ClickHouse-backed parity check for legacy vs v2 QQQ option strategy.")
    ap.add_argument("--day", required=True, help="Trading day in YYYY-MM-DD")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--session-mode", default="extended", choices=["rth", "extended"])
    ap.add_argument("--quote", default=None, help="Optional JSON file to force a fixed quote payload")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    scenario = {
        "day": args.day,
        "symbol": args.symbol,
        "session_mode": args.session_mode,
        "snapshot_root": f"runtime/state/decision_snapshots_day_parity/{args.day}_{os.getpid()}",
    }
    if args.quote:
        scenario["quote"] = json.loads(Path(args.quote).expanduser().read_text(encoding="utf-8"))
    payload = run_clickhouse_day_parity_scenario_sync(scenario)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
