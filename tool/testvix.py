import os
import argparse
from typing import List
from ib_insync import IB, Index, Stock, util


def _print_kv(title: str, **kw):
    kv = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"[{title}] {kv}")


def test_vix(
    host: str,
    port: int,
    client_id: int,
    md_type: int,
    exchanges: List[str],
    what_to_show: List[str],
    stream_seconds: float,
    try_vixy: bool,
):
    util.logToConsole(False)
    ib = IB()
    _print_kv("connect", host=host, port=port, clientId=client_id)
    try:
        ib.connect(host, port, clientId=client_id)
    except Exception as exc:
        _print_kv("connect_failed", error=repr(exc))
        return 2

    errs = []

    def on_error(reqId, code, msg, advancedOrderRejectJson=None):
        errs.append((reqId, code, msg))
    ib.errorEvent += on_error

    try:
        ib.reqMarketDataType(md_type)
        _print_kv("md_type", value=md_type)
    except Exception as exc:
        _print_kv("md_type_failed", error=str(exc))

    # Resolve contract (VIX preferred)
    contract = None
    for ex in exchanges:
        try:
            c = Index('VIX', ex, 'USD')
            cds = ib.reqContractDetails(c)
            if cds:
                q = ib.qualifyContracts(c)
                if q:
                    contract = q[0]
                    _print_kv("vix_contract", exchange=ex, conId=contract.conId, local=contract.localSymbol)
                    break
        except Exception as exc:
            _print_kv("vix_contract_failed", exchange=ex, error=str(exc))

    # Optional fallback to VIXY
    if contract is None and try_vixy:
        try:
            vixy = Stock('VIXY', 'SMART', 'USD')
            cds = ib.reqContractDetails(vixy)
            if cds:
                q = ib.qualifyContracts(vixy)
                if q:
                    contract = q[0]
                    _print_kv("vixy_contract", exchange=contract.exchange, conId=contract.conId, local=contract.localSymbol)
        except Exception as exc:
            _print_kv("vixy_contract_failed", error=str(exc))

    if contract is None:
        _print_kv("no_contract", note="VIX/VIXY unavailable — check entitlements")
        ib.disconnect()
        return 3

    # Print trading hours
    try:
        d = ib.reqContractDetails(contract)[0]
        _print_kv("hours", tradingHours=d.tradingHours, liquidHours=d.liquidHours)
    except Exception as exc:
        _print_kv("hours_failed", error=str(exc))

    # Historical checks with multiple whatToShow and RTH/ALL sessions
    for wts in what_to_show:
        for use_rth in (True, False):
            try:
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime='',
                    durationStr='2 D',
                    barSizeSetting='1 min',
                    whatToShow=wts,
                    useRTH=use_rth,
                    formatDate=2,
                )
                last_repr = str(bars[-1]) if bars else None
                _print_kv("hist", whatToShow=wts, useRTH=use_rth, count=len(bars), last=last_repr)
            except Exception as exc:
                _print_kv("hist_failed", whatToShow=wts, useRTH=use_rth, error=str(exc))

    # Streaming snapshot
    try:
        ticker = ib.reqMktData(contract, genericTickList='', snapshot=False, regulatorySnapshot=False)
        ib.sleep(stream_seconds)
        _print_kv(
            "stream",
            last=ticker.last,
            close=ticker.close,
            bid=ticker.bid,
            ask=ticker.ask,
            high=ticker.high,
            low=ticker.low,
            time=ticker.time,
        )
        ib.cancelMktData(contract)
    except Exception as exc:
        _print_kv("stream_failed", error=str(exc))

    # Print recent errors
    if errs:
        print("IB error codes (last 10):")
        for e in errs[-10:]:
            print("  ", e)

    ib.disconnect()
    return 0


def main():
    ap = argparse.ArgumentParser(description="VIX capability tester (IBKR)")
    ap.add_argument('--host', default=os.getenv('IB_HOST', '127.0.0.1'))
    ap.add_argument('--port', type=int, default=int(os.getenv('IB_PORT', '4002')))
    ap.add_argument('--client-id', type=int, default=int(os.getenv('IB_CLIENT_ID', '7001')))
    ap.add_argument('--md-type', type=int, default=int(os.getenv('IB_MD_TYPE', '3')), help='3=delayed-frozen, 4=delayed, 1=live')
    ap.add_argument('--exchanges', default='CBOE,CBOEIND,SMART')
    ap.add_argument('--wts', default='TRADES,MIDPOINT,INDEX', help='whatToShow list')
    ap.add_argument('--stream-seconds', type=float, default=2.5)
    ap.add_argument('--try-vixy', action='store_true')
    args = ap.parse_args()

    ret = test_vix(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        md_type=args.md_type,
        exchanges=[x.strip() for x in args.exchanges.split(',') if x.strip()],
        what_to_show=[x.strip() for x in args.wts.split(',') if x.strip()],
        stream_seconds=args.stream_seconds,
        try_vixy=bool(args.try_vixy),
    )
    raise SystemExit(ret)


if __name__ == '__main__':
    main()
