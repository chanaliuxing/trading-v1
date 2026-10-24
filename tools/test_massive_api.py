#!/usr/bin/env python3
"""
Simple Massive REST API connectivity test.

Reads MASSIVE_API_KEY from environment (or .env) and verifies:
1) Reference endpoint (v3/reference/tickers)
2) Aggregates endpoint (v2/aggs/ticker/.../range/1/day/...)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def get_json(url: str, timeout_sec: int = 20) -> dict:
    req = Request(url, headers={"User-Agent": "trading-massive-test/1.0"})
    with urlopen(req, timeout=timeout_sec) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def masked_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Massive REST API key and data access.")
    parser.add_argument("--env-file", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument("--base-url", default="https://api.massive.com", help="Massive API base URL")
    parser.add_argument("--symbol", default="QQQ", help="Ticker for aggregate test (default: QQQ)")
    parser.add_argument("--days", type=int, default=5, help="Recent days window for aggregate test")
    args = parser.parse_args()

    load_env_file(Path(args.env_file))
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if not api_key:
        print("ERROR: MASSIVE_API_KEY is missing (env or .env).")
        return 2

    print(f"Using MASSIVE_API_KEY={masked_key(api_key)}")

    ref_params = {
        "market": "stocks",
        "active": "true",
        "limit": 1,
        "apiKey": api_key,
    }
    ref_url = f"{args.base_url}/v3/reference/tickers?{urlencode(ref_params)}"
    ref_data = get_json(ref_url)
    ref_status = ref_data.get("status")
    ref_count = ref_data.get("count")
    first_ticker = None
    results = ref_data.get("results") or []
    if results:
        first_ticker = results[0].get("ticker")

    print(f"[reference] status={ref_status}, count={ref_count}, first_ticker={first_ticker}")
    if ref_status != "OK":
        print(f"[reference] response={json.dumps(ref_data, ensure_ascii=False)[:500]}")
        return 1

    end_d = date.today()
    start_d = end_d - timedelta(days=max(1, args.days))
    aggs_params = {
        "adjusted": "true",
        "sort": "asc",
        "limit": 5000,
        "apiKey": api_key,
    }
    aggs_url = (
        f"{args.base_url}/v2/aggs/ticker/{args.symbol}/range/1/day/"
        f"{start_d.isoformat()}/{end_d.isoformat()}?{urlencode(aggs_params)}"
    )
    aggs_data = get_json(aggs_url)
    aggs_status = aggs_data.get("status")
    aggs_count = aggs_data.get("resultsCount", len(aggs_data.get("results") or []))
    print(f"[aggs] status={aggs_status}, symbol={args.symbol}, range={start_d}..{end_d}, bars={aggs_count}")

    if aggs_status not in {"OK", "DELAYED"}:
        print(f"[aggs] response={json.dumps(aggs_data, ensure_ascii=False)[:500]}")
        return 1

    bars = aggs_data.get("results") or []
    if bars:
        b0 = bars[0]
        b1 = bars[-1]
        print(
            "[aggs] first_bar="
            f"t={b0.get('t')} o={b0.get('o')} h={b0.get('h')} l={b0.get('l')} c={b0.get('c')} v={b0.get('v')}"
        )
        print(
            "[aggs] last_bar="
            f"t={b1.get('t')} o={b1.get('o')} h={b1.get('h')} l={b1.get('l')} c={b1.get('c')} v={b1.get('v')}"
        )

    print("Massive API test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
