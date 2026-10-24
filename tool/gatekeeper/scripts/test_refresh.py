from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Gatekeeper refresh smoke test.")
    parser.add_argument(
        "--config",
        default="tool/gatekeeper/config.json",
        help="Path to gatekeeper config.json",
    )
    parser.add_argument("--anchor", default="QQQ", help="Anchor symbol.")
    parser.add_argument(
        "--tickers",
        default="META,MSFT,NVDA,TSLA,ORCL",
        help="Comma-separated ticker list.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        return 1

    gatekeeper_root = config_path.parent
    if str(gatekeeper_root) not in sys.path:
        sys.path.insert(0, str(gatekeeper_root))

    from core.cache import GatekeeperCache
    from core.engine import GatekeeperEngine
    from providers.ibkr import IBKRProvider

    config = _load_config(config_path)
    provider = IBKRProvider()
    cache = GatekeeperCache()
    engine = GatekeeperEngine(
        config=config,
        provider=provider,
        cache=cache,
        manual_overrides={},
    )

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    status = engine.refresh_data(anchor_symbol=args.anchor.strip().upper(), tickers=tickers)
    print(json.dumps(status, indent=2, ensure_ascii=True))
    try:
        provider.disconnect()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
