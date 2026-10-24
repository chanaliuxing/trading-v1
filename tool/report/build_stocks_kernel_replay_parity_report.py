#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tool.report.build_stocks_kernel_parity_report import build_stocks_kernel_parity_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build replay parity report for stocks kernel")
    parser.add_argument("--expected", required=True)
    parser.add_argument("--actual", required=True)
    args = parser.parse_args()

    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    actual = json.loads(Path(args.actual).read_text(encoding="utf-8"))
    payload: dict[str, Any] = build_stocks_kernel_parity_report(expected=expected, actual=actual)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
