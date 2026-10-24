from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Gatekeeper option-data smoke test (IVR/RR25/ATM spread).")
    parser.add_argument(
        "--config",
        default="tool/gatekeeper/config.json",
        help="Path to gatekeeper config.json",
    )
    parser.add_argument("--symbol", default="META", help="Underlying symbol (e.g., META).")
    parser.add_argument("--client-id", type=int, default=None, help="Override IBKR clientId for this run.")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        return 1

    gatekeeper_root = config_path.parent
    if str(gatekeeper_root) not in sys.path:
        sys.path.insert(0, str(gatekeeper_root))

    from core.cache import GatekeeperCache
    from core.resolver import GatekeeperResolver
    from providers.ibkr import IBKRProvider

    cfg = _load_config(config_path)
    if args.client_id is not None:
        cfg.setdefault("data", {}).setdefault("ibkr", {})["clientId"] = int(args.client_id)
    provider = IBKRProvider()
    resolver = GatekeeperResolver(config=cfg, provider=provider, cache=GatekeeperCache(), manual_overrides={})
    ok, msg = resolver.connect_ibkr()
    print(json.dumps({"connect_ok": ok, "message": msg}, ensure_ascii=True))
    if not ok:
        return 2

    symbol = args.symbol.strip().upper()
    out = {"symbol": symbol}

    start = time.perf_counter()
    px, px_err = provider.fetch_last_price(symbol)
    out["underlying_price"] = px
    out["underlying_price_error"] = px_err
    out["underlying_price_elapsed_s"] = round(time.perf_counter() - start, 3)

    start = time.perf_counter()
    ivr, ivr_err = provider.fetch_ivr(symbol)
    out["ivr"] = ivr
    out["ivr_error"] = ivr_err
    out["ivr_elapsed_s"] = round(time.perf_counter() - start, 3)

    start = time.perf_counter()
    rr25, rr25_err = provider.fetch_skew_value(symbol)
    out["rr25"] = rr25
    out["rr25_error"] = rr25_err
    out["rr25_elapsed_s"] = round(time.perf_counter() - start, 3)

    start = time.perf_counter()
    spread, spread_err = provider.fetch_atm_option_spread_pct(symbol)
    out["atm_spread_pct"] = spread
    out["atm_spread_error"] = spread_err
    out["atm_spread_elapsed_s"] = round(time.perf_counter() - start, 3)

    print(json.dumps(out, indent=2, ensure_ascii=True))
    try:
        provider.disconnect()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
