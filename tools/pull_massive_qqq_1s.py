#!/usr/bin/env python3
"""
Pull QQQ 1-second aggregates from Massive with free-tier-safe rate limiting.

Free tier constraint:
- 5 API calls / minute (enforced by default)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.error import HTTPError
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


def with_api_key(url: str, api_key: str) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "apiKey" not in q:
        q["apiKey"] = api_key
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


@dataclass
class RateLimiter:
    max_calls_per_min: int = 5
    call_times: list[float] = field(default_factory=list)

    def wait(self) -> None:
        now = time.time()
        self.call_times = [t for t in self.call_times if now - t < 60.0]
        if len(self.call_times) < self.max_calls_per_min:
            return
        earliest = min(self.call_times)
        sleep_sec = max(0.0, 60.0 - (now - earliest)) + 0.05
        print(f"[rate-limit] sleeping {sleep_sec:.2f}s to stay <= {self.max_calls_per_min} calls/min")
        time.sleep(sleep_sec)
        now2 = time.time()
        self.call_times = [t for t in self.call_times if now2 - t < 60.0]

    def mark(self) -> None:
        self.call_times.append(time.time())


def get_json(url: str, timeout_sec: int = 30) -> tuple[int, dict]:
    req = Request(url, headers={"User-Agent": "trading-massive-1s-pull/1.0"})
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            return int(getattr(resp, "status", 200)), json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"message": str(e)}
        return int(e.code), body


def ts_to_et(ms: int) -> str:
    # Massive aggregate timestamps are Unix ms (UTC-based epoch)
    return datetime.fromtimestamp(ms / 1000.0).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull Massive 1s bars for QQQ with free-tier rate limit")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--base-url", default="https://api.massive.com")
    parser.add_argument("--symbol", default="QQQ")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--adjusted", default="true", choices=["true", "false"])
    parser.add_argument("--limit", type=int, default=50000)
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--max-calls-per-min", type=int, default=5)
    args = parser.parse_args()

    load_env_file(Path(args.env_file))
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if not api_key:
        print("ERROR: MASSIVE_API_KEY missing")
        return 2

    rl = RateLimiter(max_calls_per_min=max(1, args.max_calls_per_min))

    url = (
        f"{args.base_url}/v2/aggs/ticker/{args.symbol}/range/1/second/"
        f"{args.start_date}/{args.end_date}"
        f"?adjusted={args.adjusted}&sort=asc&limit={args.limit}&apiKey={api_key}"
    )

    all_rows = []
    page = 0
    status_seen = None

    while url and page < args.max_pages:
        page += 1
        rl.wait()
        rl.mark()
        http_code, data = get_json(url)
        if http_code >= 400:
            print(
                f"[page {page}] HTTP {http_code} status={data.get('status')} "
                f"message={data.get('message')}"
            )
            return 1

        status = data.get("status")
        status_seen = status if status_seen is None else status_seen
        results = data.get("results") or []
        all_rows.extend(results)

        print(
            f"[page {page}] status={status} queryCount={data.get('queryCount')} "
            f"resultsCount={data.get('resultsCount')} cumulative={len(all_rows)}"
        )

        next_url = data.get("next_url")
        url = with_api_key(next_url, api_key) if next_url else ""

    if not all_rows:
        print(f"NO DATA: symbol={args.symbol} range={args.start_date}..{args.end_date} status={status_seen}")
        return 0

    first = all_rows[0]
    last = all_rows[-1]
    print(
        f"DONE rows={len(all_rows)} status={status_seen} "
        f"first_ts={first.get('t')} ({ts_to_et(first.get('t'))}) "
        f"last_ts={last.get('t')} ({ts_to_et(last.get('t'))})"
    )
    print(
        f"first_bar o={first.get('o')} h={first.get('h')} l={first.get('l')} c={first.get('c')} v={first.get('v')}"
    )
    print(
        f"last_bar  o={last.get('o')} h={last.get('h')} l={last.get('l')} c={last.get('c')} v={last.get('v')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
