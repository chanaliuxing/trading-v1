"""Build expanded watchlist file for V3.1 scarcity controller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="Base watchlist JSON (list of tickers)")
    parser.add_argument("--extended", required=True, help="Extended watchlist JSON (list of tickers)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    base = json.loads(Path(args.base).read_text("utf-8"))
    extended = json.loads(Path(args.extended).read_text("utf-8"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"watchlist_base": base, "watchlist_extended": extended}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
