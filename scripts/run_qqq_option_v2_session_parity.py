#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.options_kernel.harness import run_minute_session_parity_scenario_sync


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a minute-session parity check for legacy vs v2 QQQ option strategy.")
    ap.add_argument("--scenario", required=True, help="Path to JSON scenario file")
    ap.add_argument("--out", default=None, help="Optional JSON output path")
    args = ap.parse_args()

    scenario_path = Path(args.scenario).expanduser()
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    payload = run_minute_session_parity_scenario_sync(scenario)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
