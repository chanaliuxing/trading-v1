#!/usr/bin/env python3
"""
Probe IBKR for NASDAQ advance-decline internals (ADD-NASD / AD-NASD) and print live values.

This is intentionally standalone so we can validate the correct contract symbol/exchange
without touching run_system.

Example:
  python tools/test_add_nasd_ibkr.py --host 127.0.0.1 --port 4002 --client-id 199 --seconds 30
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from ib_insync import IB, Index, Ticker


@dataclass(frozen=True)
class Candidate:
    alias: str
    symbol: str
    exchange: str
    currency: str = "USD"


def _candidates() -> list[Candidate]:
    # We try a small set of likely combinations. IB naming can vary by account/data permissions.
    syms = ["AD-NDX", "ADD-NASD", "AD-NASD", "ADD", "AD"]
    exs = ["NASDAQ", "NASD", "SMART", "NYSE"]
    out: list[Candidate] = []
    for s in syms:
        for ex in exs:
            out.append(Candidate(alias="ADD-NASD", symbol=s, exchange=ex))
    # De-dup while preserving order
    seen = set()
    dedup = []
    for c in out:
        k = (c.symbol, c.exchange, c.currency)
        if k in seen:
            continue
        seen.add(k)
        dedup.append(c)
    return dedup


def _ticker_value(t: Ticker) -> Optional[float]:
    # Internals often come through as last/close/marketPrice
    for attr in ("last", "close"):
        try:
            v = getattr(t, attr, None)
            if v is not None:
                fv = float(v)
                if math.isfinite(fv) and fv != 0.0:
                    return fv
        except Exception:
            pass
    try:
        mp = t.marketPrice()
        if mp is not None:
            fmp = float(mp)
            if math.isfinite(fmp) and fmp != 0.0:
                return fmp
    except Exception:
        pass
    return None


async def _run(host: str, port: int, client_id: int, seconds: int, verbose: bool) -> int:
    ib = IB()
    await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=10.0)
    print(f"Connected: {host}:{port} clientId={client_id}")

    # Prefer delayed-frozen if live not available.
    try:
        ib.reqMarketDataType(3)
    except Exception:
        pass

    best_ticker: Optional[Ticker] = None
    best_contract = None
    best_value: Optional[float] = None

    # Step 1: find a contract that resolves.
    for cand in _candidates():
        contract = Index(cand.symbol, cand.exchange, cand.currency)
        try:
            details = await ib.reqContractDetailsAsync(contract)
        except Exception as exc:
            if verbose:
                print(f"details FAIL symbol={cand.symbol} ex={cand.exchange} err={exc}")
            continue
        if not details:
            if verbose:
                print(f"details EMPTY symbol={cand.symbol} ex={cand.exchange}")
            continue

        resolved = details[0].contract
        print(f"Resolved: requested=({cand.symbol},{cand.exchange}) -> resolved=({resolved.symbol},{resolved.exchange}) conId={resolved.conId}")

        # Step 2: subscribe briefly and see if a value appears.
        t = ib.reqMktData(resolved, genericTickList="", snapshot=False, regulatorySnapshot=False)
        t.updateEvent += lambda _: None

        t0 = time.time()
        got = None
        while time.time() - t0 < 5.0:
            await asyncio.sleep(0.2)
            v = _ticker_value(t)
            if v is not None:
                got = v
                break

        ib.cancelMktData(resolved)
        await asyncio.sleep(0.1)

        if got is not None:
            best_ticker = t
            best_contract = resolved
            best_value = got
            print(f"Candidate OK: symbol={cand.symbol} ex={cand.exchange} value={got}")
            break
        else:
            print(f"Candidate NO TICKS: symbol={cand.symbol} ex={cand.exchange}")

    if best_contract is None:
        print("FAIL: could not resolve+tick any candidate contract.")
        ib.disconnect()
        return 2

    # Step 3: re-subscribe and print updates for the requested duration.
    print(f"Subscribing for {seconds}s: resolved=({best_contract.symbol},{best_contract.exchange}) conId={best_contract.conId}")
    t = ib.reqMktData(best_contract, genericTickList="", snapshot=False, regulatorySnapshot=False)

    start = time.time()
    last_print = 0.0
    while time.time() - start < seconds:
        await asyncio.sleep(0.2)
        v = _ticker_value(t)
        if v is None:
            continue
        if v != best_value or (time.time() - last_print) > 2.0:
            best_value = v
            last_print = time.time()
            print(f"ADD_NASD value={v} bid={getattr(t,'bid',None)} ask={getattr(t,'ask',None)} time={getattr(t,'time',None)}")

    ib.cancelMktData(best_contract)
    await asyncio.sleep(0.1)
    ib.disconnect()
    print("Done.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=199)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    rc = asyncio.run(_run(args.host, args.port, args.client_id, args.seconds, args.verbose))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
