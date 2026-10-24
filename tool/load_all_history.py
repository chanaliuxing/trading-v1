```python
"""
Historical Data Loader for Trading System

Fetches historical bars from IBKR (5s, 15s, 1m, 5m, 15m, 30m, 60m, 1d)
and loads them into ClickHouse, skipping dates that already exist in the database.

Examples:
python3 scripts/load_historical_data.py --symbols AAPL MSFT NVDA GOOGL AMZN META TSLA AMD --days 20
python3 scripts/load_historical_data.py --symbols AAPL MSFT NVDA GOOGL AMZN META TSLA JPM BAC V MA XOM CVX NEE UNH JNJ ABBV PG KO PEP CAT BA AVGO AMD AMT IBM SMCI PLTR SNOW DDOG NOW SHOP SOFI RIVN ARM SPY QQQ DIA SMH SOXX XLF XLE XLV XLP XLI XLRE XLU ARKK QQQJ CIBR ICLN TAN GLD VIXY UVXY --days 20
"""
import asyncio
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from ib_insync import IB, Stock, util as ib_util

# Ensure project root is on sys.path for local imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)


class HistoricalDataLoader:
    """Connects to IBKR, fetches historical data, and inserts it into ClickHouse."""

    def __init__(self, ib_host: str, ib_port: int, ib_client_id: int):
        self.ib_host = ib_host
        self.ib_port = ib_port
        self.ib_client_id = ib_client_id
        self.ib = IB()
        self.ch = ClickHouseDB()
        self.ib_timezone = ZoneInfo(config.get('ib.timezone', 'America/New_York'))
        logger.info(
            "HistoricalDataLoader initialized",
            ib_host=self.ib_host,
            ib_port=self.ib_port,
        )

        # (barSizeSetting, clickhouse_table, seconds_per_bar)
        self.bar_configs: List[Tuple[str, str, int]] = [
            ("5 secs", "bars_5s_ibkr", 5),
            ("15 secs", "bars_15s_ibkr", 15),
            ("1 min", "bars_1m_ibkr", 60),
            ("5 mins", "bars_5m_ibkr", 300),
            ("15 mins", "bars_15m_ibkr", 900),
            ("30 mins", "bars_30m_ibkr", 1800),
            ("1 hour", "bars_1h_ibkr", 3600),
            ("1 day", "bars_1d_ibkr", 86400),
        ]

    async def connect(self) -> None:
        logger.info("Connecting to IB Gateway...", host=self.ib_host, port=self.ib_port)
        await self.ib.connectAsync(
            host=self.ib_host,
            port=self.ib_port,
            clientId=self.ib_client_id,
            timeout=10,
        )
        logger.info("✅ Connected to IB Gateway")

    async def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway")

    async def load_data_for_symbol(self, symbol: str, days: int) -> int:
        logger.info("========== Starting data load for %s ==========" % symbol)
        total_bars_inserted = 0

        last_ts_in_db = self.ch.get_last_bar_timestamp(symbol)
        last_ts_local = None
        if last_ts_in_db:
            last_ts_local = last_ts_in_db.replace(tzinfo=timezone.utc).astimezone(self.ib_timezone)
            logger.info(
                "Last existing record",
                symbol=symbol,
                timestamp=last_ts_local.strftime('%Y-%m-%d %H:%M:%S'),
            )
        else:
            logger.info("No existing data found for %s. Starting fresh." % symbol)

        for i in range(days, 0, -1):
            end_date_local = datetime.now(self.ib_timezone) - timedelta(days=i - 1)
            end_date_local = end_date_local.replace(hour=23, minute=59, second=59, microsecond=0)

            if last_ts_local and end_date_local.date() <= last_ts_local.date():
                logger.info(
                    "Skipping %s for %s (data already present)",
                    end_date_local.strftime('%Y-%m-%d'),
                    symbol,
                )
                continue

            tz_label = getattr(self.ib_timezone, "key", str(self.ib_timezone))
            end_date_str = f"{end_date_local.strftime('%Y%m%d %H:%M:%S')} {tz_label}"

            logger.info("Fetching data for %s on %s...", symbol, end_date_local.strftime('%Y-%m-%d'))
            try:
                # Fetch & insert for each configured bar size / table
                for bar_size, table, sec_per_bar in self.bar_configs:
                    bars = await self.ib.reqHistoricalDataAsync(
                        contract=Stock(symbol, "SMART", "USD"),
                        endDateTime=end_date_str,
                        durationStr="1 D",
                        barSizeSetting=bar_size,
                        whatToShow="TRADES",
                        useRTH=True,
                        formatDate=2,
                    )

                    if not bars:
                        logger.warning(
                            "No data returned for %s on %s (%s)",
                            symbol,
                            end_date_local.strftime('%Y-%m-%d'),
                            bar_size,
                        )
                        # small pause before next request anyway
                        await asyncio.sleep(2)
                        continue

                    inserted_count = await self._insert_bars_to_clickhouse(
                        symbol=symbol,
                        bars=bars,
                        table=table,
                        seconds_per_bar=sec_per_bar,
                    )
                    total_bars_inserted += inserted_count
                    logger.info(
                        "Inserted %d bars for %s on %s (%s -> %s)",
                        inserted_count,
                        symbol,
                        end_date_local.strftime('%Y-%m-%d'),
                        bar_size,
                        table,
                    )

                    # simple pacing
                    await asyncio.sleep(2)

            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Error fetching data for %s on %s",
                    symbol,
                    date=end_date_local.strftime('%Y-%m-%d'),
                    error=str(exc),
                )
                continue

        logger.info(
            "========== Finished data load for %s: Total %d bars inserted ==========" % (symbol, total_bars_inserted)
        )
        return total_bars_inserted

    async def _insert_bars_to_clickhouse(self, symbol: str, bars: List, table: str, seconds_per_bar: int) -> int:
        if not bars:
            return 0

        rows = []
        for bar in bars:
            avg = getattr(bar, 'average', None)
            vwap = float(avg) if avg not in (None, -1, -1.0) else None
            ts_start = bar.date.replace(tzinfo=None)
            rows.append(
                {
                    "ts_start": ts_start,
                    "ts_end": ts_start + timedelta(seconds=seconds_per_bar),
                    "symbol": symbol,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume) if getattr(bar, 'volume', 0) else 0,
                    "vwap": vwap,
                    "trades": int(bar.barCount) if getattr(bar, 'barCount', None) else 0,
                    "bid_last": None,
                    "ask_last": None,
                    "mid_last": float(bar.close),
                    "ver": int(datetime.now().timestamp() * 1e6),
                }
            )

        df = pd.DataFrame(rows)
        df["volume"] = df["volume"].astype("int64")
        df["trades"] = df["trades"].astype("int32")

        try:
            self.ch.client.insert_df(table=table, df=df)
            return len(df)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to insert bars into ClickHouse", symbol=symbol, table=table, error=str(exc))
            return 0


async def main() -> None:
    parser = argparse.ArgumentParser(description="Load historical market data from IBKR.")
    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="Space-separated list of stock symbols to load (e.g., TSLA GOOGL).",
    )
    parser.add_argument(
        "--days",
        type=int,
        required=True,
        help="Number of past days of data to load.",
    )
    args = parser.parse_args()

    ib_cfg = config.get("ib", {})
    ib_host = os.getenv("IB_HOST", ib_cfg.get("host", "127.0.0.1"))
    ib_port = int(os.getenv("IB_PAPER_PORT", ib_cfg.get("paper_port", 4002)))
    ib_client_id = ib_cfg.get("client_id", 1) + 100

    loader = HistoricalDataLoader(ib_host=ib_host, ib_port=ib_port, ib_client_id=ib_client_id)

    try:
        await loader.connect()
        for symbol in args.symbols:
            await loader.load_data_for_symbol(symbol.upper(), args.days)
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error during data loading", error=str(exc))
    finally:
        await loader.disconnect()


if __name__ == "__main__":
    ib_util.patchAsyncio()
    asyncio.run(main())
```
