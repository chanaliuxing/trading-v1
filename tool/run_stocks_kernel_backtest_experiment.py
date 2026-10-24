#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.backtest.stocks_kernel_adapter import StocksKernelBacktestAdapter


def _load_json(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    return dict(payload) if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a stocks-kernel backtest experiment spec and print its summary.")
    parser.add_argument("--spec", required=True, help="Path to a stocks-kernel experiment JSON spec.")
    parser.add_argument("--output", default="", help="Optional path to write summary JSON.")
    args = parser.parse_args()

    adapter = StocksKernelBacktestAdapter()
    raw_result = adapter.run(_load_json(str(args.spec)))
    summary = adapter.summarize(raw_result)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if str(args.output).strip():
        out = Path(str(args.output)).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if bool(summary.get("ok")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
