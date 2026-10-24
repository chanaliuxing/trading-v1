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
    parser = argparse.ArgumentParser(description="Gatekeeper offline run (no IBKR).")
    parser.add_argument(
        "--config",
        default="tool/gatekeeper/config.json",
        help="Path to gatekeeper config.json",
    )
    parser.add_argument("--symbol", default="META", help="Ticker symbol.")
    parser.add_argument("--anchor", default="QQQ", help="Anchor symbol.")
    parser.add_argument("--profile", default="gatekeeper_v1_1", help="Profile name.")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        return 1

    gatekeeper_root = config_path.parent
    if str(gatekeeper_root) not in sys.path:
        sys.path.insert(0, str(gatekeeper_root))

    from core.cache import GatekeeperCache, make_field_id
    from core.engine import GatekeeperEngine
    from core.resolver import GlobalInputs
    from core.types import ResolvedInput
    from providers.ibkr import IBKRProvider

    cfg = _load_config(config_path)
    provider = IBKRProvider()
    cache = GatekeeperCache()

    # Minimal set of inputs to let the pipeline run deterministically.
    # This mirrors the rulebook: manual overrides always win.
    anchor = args.anchor.strip().upper()
    symbol = args.symbol.strip().upper()
    ts = "offline"
    manual_overrides = {
        make_field_id("GLOBAL", "bofa_9_3"): ResolvedInput(value=7.5, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("GLOBAL", "spread_width_usd"): ResolvedInput(value=5.0, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("ANCHOR", "anchor_price", symbol=anchor): ResolvedInput(value=612.0, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("ANCHOR", "anchor_vwap", symbol=anchor): ResolvedInput(value=611.0, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("ANCHOR", "vix", symbol=anchor): ResolvedInput(value=18.0, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("ANCHOR", "vvix", symbol=anchor): ResolvedInput(value=105.0, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("ANCHOR", "anchor_chop_score", symbol=anchor): ResolvedInput(value=0.5, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("TICKER", "ticker_price", symbol=symbol): ResolvedInput(value=611.5, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("TICKER", "ticker_vwap", symbol=symbol): ResolvedInput(value=610.5, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("TICKER", "ticker_bars_1m", symbol=symbol): ResolvedInput(
            value=[
                {"time": ts, "open": 610.2, "high": 611.1, "low": 610.0, "close": 610.9, "volume": 1000.0},
                {"time": ts, "open": 610.9, "high": 611.4, "low": 610.8, "close": 611.2, "volume": 1200.0},
                {"time": ts, "open": 611.2, "high": 611.8, "low": 611.0, "close": 611.5, "volume": 900.0},
            ],
            source="MANUAL",
            ts=ts,
            is_manual_lock=True,
        ),
        make_field_id("TICKER", "ivr", symbol=symbol): ResolvedInput(value=36.0, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("TICKER", "skew_value", symbol=symbol): ResolvedInput(value=6.2, source="MANUAL", ts=ts, is_manual_lock=True),
        make_field_id("TICKER", "atm_spread_pct", symbol=symbol): ResolvedInput(value=0.9, source="MANUAL", ts=ts, is_manual_lock=True),
    }

    engine = GatekeeperEngine(config=cfg, provider=provider, cache=cache, manual_overrides=manual_overrides)
    global_inputs = GlobalInputs(
        anchor_symbol=anchor,
        event_confirmed=False,
        event_level="None",
        bofa_9_3=7.5,
        spread_width_usd=5.0,
    )

    result = engine.run_symbol(symbol=symbol, profile=str(args.profile), global_inputs=global_inputs, fetch_auto=False)
    print(json.dumps(result.card.to_dict(), indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
