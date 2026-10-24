#!/usr/bin/env python3
"""
Test whether IBKR can provide AD-NASD 1-minute historical data
covering at least the most recent 15 completed trading days.

Usage:
  venv/bin/python tools/test_ad_nasd_15d_history.py --host 127.0.0.1 --port 4002 --client-id 9401
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ib_insync import IB, Index

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ContractCandidate:
    symbol: str
    exchange: str
    currency: str = "USD"


def _candidates() -> list[ContractCandidate]:
    # Prefer the production symbol first.
    return [
        ContractCandidate("AD-NASD", "NASDAQ"),
        ContractCandidate("AD-NDX", "NASDAQ"),
    ]


def _bar_dt_et(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw.astimezone(ET) if raw.tzinfo else raw.replace(tzinfo=ET)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)
        except Exception:
            return None
    return None


async def _resolve_contract(ib: IB) -> Optional[Any]:
    for cand in _candidates():
        c = Index(cand.symbol, cand.exchange, cand.currency)
        try:
            details = await ib.reqContractDetailsAsync(c)
        except Exception:
            continue
        if not details:
            continue
        contract = details[0].contract
        print(
            "Resolved contract:",
            f"requested=({cand.symbol},{cand.exchange})",
            f"resolved=({contract.symbol},{contract.exchange})",
            f"conId={contract.conId}",
        )
        return contract
    return None


async def _fetch_history(ib: IB, contract: Any, what_to_show: str) -> list[Any]:
    # Pull enough calendar days to usually include >=15 sessions.
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr="30 D",
        barSizeSetting="1 min",
        whatToShow=what_to_show,
        useRTH=True,
        formatDate=2,
        keepUpToDate=False,
    )
    return list(bars or [])


def _session_coverage(bars: list[Any]) -> tuple[int, dict[str, int]]:
    by_day: dict[str, int] = defaultdict(int)
    for b in bars:
        dt = _bar_dt_et(getattr(b, "date", None))
        if dt is None:
            continue
        # RTH regular session window.
        if not (dtime(9, 30) <= dt.time() <= dtime(16, 0)):
            continue
        by_day[dt.strftime("%Y-%m-%d")] += 1
    # Count days with meaningful bars.
    qualified = {d: n for d, n in by_day.items() if n >= 60}
    return len(qualified), dict(sorted(qualified.items()))


async def _run(host: str, port: int, client_id: int) -> int:
    ib = IB()
    errors: list[tuple[int, str]] = []

    def _on_error(req_id: int, code: int, msg: str, contract: Any) -> None:
        errors.append((int(code), str(msg)))
        print(f"IB_ERROR code={code} reqId={req_id} msg={msg}")

    ib.errorEvent += _on_error
    await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=15.0)
    print(f"Connected: {host}:{port} clientId={client_id}")

    contract = await _resolve_contract(ib)
    if contract is None:
        print("FAIL: could not resolve AD contract")
        ib.disconnect()
        return 2

    # Try TRADES first, then MIDPOINT fallback.
    for what in ("TRADES", "MIDPOINT"):
        print(f"Requesting history: whatToShow={what}")
        try:
            bars = await _fetch_history(ib, contract, what_to_show=what)
        except Exception as exc:
            print(f"history request failed: {exc}")
            continue
        print(f"bars_received={len(bars)}")
        sessions, by_day = _session_coverage(bars)
        print(f"sessions_with_data={sessions}")
        if by_day:
            first = next(iter(by_day.items()))
            last = list(by_day.items())[-1]
            print(f"day_range={first[0]}..{last[0]}")
            print("last_5_days=", list(by_day.items())[-5:])
        if sessions >= 15:
            print("PASS: got >=15 trading sessions of AD history")
            ib.disconnect()
            return 0

    print("FAIL: did not get 15 sessions of AD history")
    if errors:
        print("Recent IB errors:")
        for code, msg in errors[-8:]:
            print(f"  code={code} msg={msg}")
    ib.disconnect()
    return 2


def main() -> None:
    ap = argparse.ArgumentParser(description="Test AD-NASD 15-session history from IBKR.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=9401)
    args = ap.parse_args()
    rc = asyncio.run(_run(args.host, args.port, args.client_id))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()

