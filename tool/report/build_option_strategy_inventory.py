from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.options_kernel.inventory import build_option_strategy_inventory_from_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Build option strategy active/retired inventory from config.yml")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    report = build_option_strategy_inventory_from_config(args.config)
    payload = json.dumps(report, indent=2, sort_keys=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        return
    print(payload)


if __name__ == "__main__":
    main()
