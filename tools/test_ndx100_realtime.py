#!/usr/bin/env python3
"""
Probe IBKR entitlement for NDX100 (NASDAQ-100 index) real-time data.

This script does two passes per attempt:
1) marketDataType=1 (live): checks whether real-time data is available.
2) marketDataType=3 (delayed): fallback probe to distinguish entitlement vs no-data.

If both fail, it can retry with backoff while keeping the same IB session alive.

Exit codes:
  0 = live entitlement confirmed (got data in marketDataType=1)
  2 = no data in live or delayed probe
  3 = delayed data available, but live data unavailable (likely no real-time entitlement)
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from ib_insync import IB, Index, Ticker


@dataclass(frozen=True)
class Candidate:
    symbol: str
    exchange: str
    currency: str = "USD"


def _candidates() -> list[Candidate]:
    # Keep a short candidate list to avoid unnecessary IB requests.
    return [
        Candidate("NDX", "NASDAQ"),
        Candidate("NDX", "CBOE"),
        Candidate("NDX", "SMART"),
    ]


def _ticker_value(t: Ticker) -> Optional[float]:
    for attr in ("last", "close", "bid", "ask"):
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


async def _resolve_contract(ib: IB, verbose: bool) -> Optional[Any]:
    for cand in _candidates():
        contract = Index(cand.symbol, cand.exchange, cand.currency)
        try:
            details = await ib.reqContractDetailsAsync(contract)
        except Exception as exc:
            if verbose:
                print(f"resolve FAIL symbol={cand.symbol} ex={cand.exchange} err={exc}")
            continue
        if not details:
            if verbose:
                print(f"resolve EMPTY symbol={cand.symbol} ex={cand.exchange}")
            continue
        resolved = details[0].contract
        print(
            "Resolved contract:",
            f"requested=({cand.symbol},{cand.exchange})",
            f"resolved=({resolved.symbol},{resolved.exchange})",
            f"conId={resolved.conId}",
        )
        return resolved
    return None


async def _probe_once(ib: IB, contract: Any, md_type: int, seconds: int) -> tuple[bool, Optional[float], Optional[int]]:
    if not ib.isConnected():
        return False, None, None
    ib.reqMarketDataType(md_type)
    ticker = ib.reqMktData(contract, genericTickList="", snapshot=False, regulatorySnapshot=False)

    got = False
    val: Optional[float] = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        await asyncio.sleep(0.2)
        cur = _ticker_value(ticker)
        if cur is not None:
            got = True
            val = cur
            break

    mdt = None
    try:
        mdt = int(getattr(ticker, "marketDataType", 0) or 0)
    except Exception:
        mdt = None

    ib.cancelMktData(contract)
    await asyncio.sleep(0.1)
    return got, val, mdt


async def _run(host: str, port: int, client_id: int, seconds: int, verbose: bool) -> int:
    ib = IB()
    errors: list[tuple[int, str]] = []

    def _on_error(req_id: int, code: int, msg: str, contract: Any) -> None:
        errors.append((int(code), str(msg)))
        if verbose:
            print(f"IB_ERROR code={code} reqId={req_id} msg={msg}")

    def _on_disconnected() -> None:
        print("IB_DISCONNECTED: peer closed connection")

    ib.errorEvent += _on_error
    if hasattr(ib, "disconnectedEvent"):
        ib.disconnectedEvent += _on_disconnected

    await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=10.0)
    print(f"Connected: {host}:{port} clientId={client_id}")

    contract = await _resolve_contract(ib, verbose=verbose)
    if contract is None:
        print("FAIL: could not resolve NDX contract.")
        ib.disconnect()
        return 2

    async def _single_attempt(attempt_idx: int, total: int) -> int:
        if not ib.isConnected():
            print("FAIL: IB disconnected before probe attempt.")
            return 2
        print(f"Attempt {attempt_idx}/{total}: Probe LIVE (marketDataType=1) for {seconds}s ...")
        live_ok, live_val, live_mdt = await _probe_once(ib, contract, md_type=1, seconds=seconds)
        print(f"LIVE result: got={live_ok} value={live_val} ticker.marketDataType={live_mdt}")
        if live_ok:
            print("PASS: real-time NDX data is available.")
            return 0

        if not ib.isConnected():
            print("FAIL: IB disconnected after LIVE probe.")
            return 2
        print(f"Attempt {attempt_idx}/{total}: Probe DELAYED (marketDataType=3) for {seconds}s ...")
        try:
            delayed_ok, delayed_val, delayed_mdt = await _probe_once(ib, contract, md_type=3, seconds=seconds)
        except Exception as exc:
            print(f"DELAYED probe error: {exc}")
            return 2
        print(f"DELAYED result: got={delayed_ok} value={delayed_val} ticker.marketDataType={delayed_mdt}")

        if delayed_ok:
            print("WARN: delayed NDX data is available, but live data is unavailable.")
            print("Likely no real-time entitlement for NDX.")
            return 3
        return 2

    rc = await _single_attempt(1, _RUN_RETRIES)
    if rc in (0, 3):
        if _HOLD_SECONDS > 0:
            print(f"Holding IB connection for {_HOLD_SECONDS}s for observation ...")
            await asyncio.sleep(_HOLD_SECONDS)
        if errors:
            print("Recent IB errors:")
            for code, msg in errors[-5:]:
                print(f"  code={code} msg={msg}")
        if ib.isConnected():
            ib.disconnect()
        return rc

    # Retry failures without reconnect churn; this avoids short connect/disconnect loops.
    for i in range(2, _RUN_RETRIES + 1):
        saw_10197 = any(code == 10197 for code, _ in errors[-10:])
        if saw_10197:
            print(f"Detected IB 10197 (competing live session). Backing off {_RETRY_BACKOFF_SEC}s before retry ...")
        else:
            print(f"No data yet. Backing off {_RETRY_BACKOFF_SEC}s before retry ...")
        await asyncio.sleep(_RETRY_BACKOFF_SEC)
        rc = await _single_attempt(i, _RUN_RETRIES)
        if rc in (0, 3):
            if _HOLD_SECONDS > 0:
                print(f"Holding IB connection for {_HOLD_SECONDS}s for observation ...")
                await asyncio.sleep(_HOLD_SECONDS)
            if errors:
                print("Recent IB errors:")
                for code, msg in errors[-5:]:
                    print(f"  code={code} msg={msg}")
            if ib.isConnected():
                ib.disconnect()
            return rc

    print("FAIL: no live or delayed NDX data received after retries.")
    if errors:
        print("Recent IB errors:")
        for code, msg in errors[-8:]:
            print(f"  code={code} msg={msg}")
    if _HOLD_SECONDS > 0:
        print(f"Holding IB connection for {_HOLD_SECONDS}s for observation ...")
        await asyncio.sleep(_HOLD_SECONDS)
    if ib.isConnected():
        ib.disconnect()
    return 2


def main() -> None:
    ap = argparse.ArgumentParser(description="Test IBKR real-time entitlement for NDX100.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=9209)
    ap.add_argument("--seconds", type=int, default=8)
    ap.add_argument("--retries", type=int, default=3, help="Total probe attempts in one IB session")
    ap.add_argument("--retry-backoff-sec", type=int, default=30, help="Backoff between failed attempts")
    ap.add_argument("--hold-seconds", type=int, default=0, help="Keep session alive after result for observation")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    global _RUN_RETRIES, _RETRY_BACKOFF_SEC, _HOLD_SECONDS
    _RUN_RETRIES = max(1, int(args.retries))
    _RETRY_BACKOFF_SEC = max(1, int(args.retry_backoff_sec))
    _HOLD_SECONDS = max(0, int(args.hold_seconds))

    rc = asyncio.run(_run(args.host, args.port, args.client_id, args.seconds, args.verbose))
    raise SystemExit(rc)


if __name__ == "__main__":
    _RUN_RETRIES = 3
    _RETRY_BACKOFF_SEC = 30
    _HOLD_SECONDS = 0
    main()
