"""Simple VIX ingestion loop.

Subscribes to IBKR's VIX index via ib_insync and persists snapshots into
ClickHouse table `refs_vix_ticks`. Intended to be run as a lightweight
sidecar process.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from ib_insync import IB, Index

from libs.db import ch_pool
from libs.logger import get_logger

LOGGER = get_logger(__name__)


async def ingest_vix(
    ib: Optional[IB] = None,
    ib_host: str = "127.0.0.1",
    ib_port: int = 4002,
    client_id: int = 142,
    poll_interval_sec: float = 55.0,
    source: str = "ibkr",
    retry_sleep_sec: float = 10.0,
) -> None:
    """Stream VIX quotes from IBKR into ClickHouse."""
    # Use async ClickHouse client to avoid blocking event loop
    ch = await ch_pool.get_async_client()
    LOGGER.info("VIX ingestion loop starting", poll_interval=poll_interval_sec)
    last_insert_bucket: Optional[datetime] = None

    while True:
        shared = ib is not None
        ib_local = ib or IB()
        try:
            if not shared:
                LOGGER.info(
                    "Connecting to IBKR for VIX stream",
                    host=ib_host,
                    port=ib_port,
                    client_id=client_id,
                )
                await ib_local.connectAsync(ib_host, ib_port, clientId=client_id, timeout=10)
                try:
                    ib_local.reqMarketDataType(4)  # 4 = delayed
                except Exception:
                    pass

            # Resolve VIX contract
            contract = None
            for ex in ("CBOE", "CBOEIND"):
                try:
                    c = Index(symbol="VIX", exchange=ex, currency="USD")
                    cds = await ib_local.reqContractDetailsAsync(c)
                    if cds:
                        qualified = await ib_local.qualifyContractsAsync(c)
                        if qualified:
                            contract = qualified[0]
                        break
                except Exception:
                    continue
            if contract is None:
                # Fallback to VIXY ETF (equity feed is more commonly entitled)
                try:
                    from ib_insync import Stock
                    vixy = Stock(symbol="VIXY", exchange="SMART", currency="USD")
                    cds = await ib_local.reqContractDetailsAsync(vixy)
                    if cds:
                        qualified = await ib_local.qualifyContractsAsync(vixy)
                        if qualified:
                            contract = qualified[0]
                            source = "vixy"  # mark source for downstream diagnostics
                            LOGGER.warning("VIX not available; falling back to VIXY ETF (SMART)")
                except Exception:
                    pass

            if contract is None:
                LOGGER.warning("VIX contract not found; will retry", host=ib_host, port=ib_port)
                try:
                    ib.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(retry_sleep_sec)
                continue

            ticker = ib_local.reqMktData(contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
            LOGGER.info("VIX ingestion streaming", shared_ib=shared)

            while True:
                await asyncio.sleep(poll_interval_sec)

                price: Optional[float] = None
                if ticker.last:
                    price = float(ticker.last)
                elif ticker.close:
                    price = float(ticker.close)
                else:
                    mp = ticker.marketPrice()
                    if mp and mp > 0:
                        price = float(mp)

                if price is None:
                    continue

                ts = datetime.now(timezone.utc)
                ts_bucket = ts.replace(second=0, microsecond=0)
                if last_insert_bucket == ts_bucket:
                    continue
                try:
                    # Keep VIX writes at most once per minute to avoid tiny-part buildup.
                    await ch.insert(
                        "refs_vix_ticks",
                        [(ts_bucket, price, source)],
                        column_names=["ts", "vix", "src"],
                    )
                    last_insert_bucket = ts_bucket
                except Exception as ins_err:
                    LOGGER.warning("VIX insert failed", error=str(ins_err))
                    continue
                LOGGER.debug("Inserted VIX tick", ts=ts_bucket.isoformat(), vix=price)

        except Exception as exc:
            LOGGER.warning("VIX ingestion cycle error; will retry", error=str(exc))
        finally:
            try:
                if not shared:
                    ib_local.disconnect()
            except Exception:
                pass
            await asyncio.sleep(retry_sleep_sec)


def main() -> None:
    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    asyncio.run(ingest_vix())


if __name__ == "__main__":
    main()
