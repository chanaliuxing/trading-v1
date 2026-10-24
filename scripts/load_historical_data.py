"""
Historical Data Loader for Trading System

Fetches historical 5-second bars from IBKR and loads them into ClickHouse,
skipping dates that already exist in the database.
python3 scripts/load_historical_data.py --symbols AAPL MSFT NVDA GOOGL AMZN META TSLA AMD --days 20
python3 scripts/load_historical_data.py --symbols AAPL MSFT NVDA GOOGL AMZN META TSLA JPM BAC V MA XOM CVX NEE UNH JNJ ABBV PG KO PEP CAT BA AVGO AMD AMT IBM SMCI PLTR SNOW DDOG NOW SHOP SOFI RIVN ARM SPY QQQ DIA SMH SOXX XLF XLE XLV XLP XLI XLRE XLU ARKK QQQJ CIBR ICLN TAN GLD VIXY UVXY --days 20
"""
import asyncio
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone, time as dt_time
from typing import List, Optional
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


def _parse_local_dt(value: str, tz: ZoneInfo) -> datetime:
    dt = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=tz)


def _is_regular_trading_hours(ts_local: datetime) -> bool:
    if ts_local.weekday() > 4:
        return False
    t = ts_local.time()
    return dt_time(9, 30) <= t < dt_time(16, 0)


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

    async def load_data_for_symbol(
        self,
        symbol: str,
        days: int,
        *,
        use_rth: bool = True,
        explicit_start: Optional[datetime] = None,
        explicit_end: Optional[datetime] = None,
        force_reload: bool = False,
    ) -> int:
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

        if explicit_start and explicit_end:
            windows = [(explicit_start, explicit_end)]
        else:
            windows = []
            for i in range(days, 0, -1):
                end_date_local = datetime.now(self.ib_timezone) - timedelta(days=i - 1)
                end_date_local = end_date_local.replace(hour=23, minute=59, second=59, microsecond=0)
                start_date_local = end_date_local.replace(hour=0, minute=0, second=0, microsecond=0)
                windows.append((start_date_local, end_date_local))

        for start_date_local, end_date_local in windows:
            if not force_reload and last_ts_local and end_date_local.date() <= last_ts_local.date() and explicit_start is None:
                logger.info(
                    "Skipping %s for %s (data already present)",
                    end_date_local.strftime('%Y-%m-%d'),
                    symbol,
                )
                continue

            tz_label = getattr(self.ib_timezone, "key", str(self.ib_timezone))
            end_date_str = f"{end_date_local.strftime('%Y%m%d %H:%M:%S')} {tz_label}"
            duration_seconds = max(1, int((end_date_local - start_date_local).total_seconds()))
            duration_str = f"{duration_seconds} S"

            logger.info(
                "Fetching data window",
                symbol=symbol,
                start=start_date_local.strftime('%Y-%m-%d %H:%M:%S'),
                end=end_date_local.strftime('%Y-%m-%d %H:%M:%S'),
                use_rth=use_rth,
            )
            try:
                bars = await self.ib.reqHistoricalDataAsync(
                    contract=Stock(symbol, "SMART", "USD"),
                    endDateTime=end_date_str,
                    durationStr=duration_str,
                    barSizeSetting="5 secs",
                    whatToShow="TRADES",
                    useRTH=use_rth,
                    formatDate=2,
                )

                if not bars:
                    logger.warning(
                        "No data returned for %s on %s",
                        symbol,
                        end_date_local.strftime('%Y-%m-%d %H:%M:%S'),
                    )
                    continue

                inserted_count = await self._insert_bars_to_clickhouse(symbol, bars)
                total_bars_inserted += inserted_count
                logger.info(
                    "Inserted %d bars for %s on %s",
                    inserted_count,
                    symbol,
                    end_date_local.strftime('%Y-%m-%d %H:%M:%S'),
                )

                await asyncio.sleep(2)

            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Error fetching data for %s on %s",
                    symbol,
                    date=end_date_local.strftime('%Y-%m-%d %H:%M:%S'),
                    error=str(exc),
                )
                continue

        logger.info(
            "========== Finished data load for %s: Total %d bars inserted ==========" % (symbol, total_bars_inserted)
        )
        return total_bars_inserted

    async def _insert_bars_to_clickhouse(self, symbol: str, bars: List) -> int:
        if not bars:
            return 0

        rows = []
        for bar in bars:
            avg = getattr(bar, 'average', None)
            vwap = float(avg) if avg not in (None, -1, -1.0) else None
            dt = getattr(bar, "date", None)
            if not isinstance(dt, datetime):
                continue
            if dt.tzinfo is None:
                dt_utc = dt.replace(tzinfo=timezone.utc)
            else:
                dt_utc = dt.astimezone(timezone.utc)
            dt_local = dt_utc.astimezone(self.ib_timezone)
            ts_start = dt_utc.replace(tzinfo=None)
            is_rth = 1 if _is_regular_trading_hours(dt_local.replace(tzinfo=None)) else 0
            rows.append(
                {
                    "ts_start": ts_start,
                    "ts_end": ts_start + timedelta(seconds=5),
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
                    "bar_sec": 5,
                    "what_to_show": "TRADES",
                    "is_rth": is_rth,
                    "source": "IBKR_HIST",
                    "exchange": "SMART",
                    "currency": "USD",
                    "tz": str(self.ib_timezone),
                    "is_backfill": 1,
                    "ver": int(datetime.now().timestamp() * 1e6),
                }
            )

        df = pd.DataFrame(rows)
        df["volume"] = df["volume"].astype("int64")
        df["trades"] = df["trades"].astype("int32")

        try:
            self.ch.client.insert_df(table="bars_5s", df=df)
            return len(df)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to insert bars into ClickHouse", symbol=symbol, error=str(exc))
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
        default=0,
        help="Number of past days of data to load.",
    )
    parser.add_argument("--start-datetime", help="Explicit local ET start datetime: YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end-datetime", help="Explicit local ET end datetime: YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--use-rth", action="store_true", help="Fetch regular-session data only")
    parser.add_argument("--force-reload", action="store_true", help="Do not skip windows just because later bars exist")
    args = parser.parse_args()

    ib_cfg = config.get("ib", {})
    ib_host = os.getenv("IB_HOST", ib_cfg.get("host", "127.0.0.1"))
    ib_port = int(os.getenv("IB_PAPER_PORT", ib_cfg.get("paper_port", 4002)))
    ib_client_id = ib_cfg.get("client_id", 1) + 100

    loader = HistoricalDataLoader(ib_host=ib_host, ib_port=ib_port, ib_client_id=ib_client_id)
    explicit_start = _parse_local_dt(args.start_datetime, loader.ib_timezone) if args.start_datetime else None
    explicit_end = _parse_local_dt(args.end_datetime, loader.ib_timezone) if args.end_datetime else None
    use_rth = bool(args.use_rth)

    try:
        await loader.connect()
        for symbol in args.symbols:
            await loader.load_data_for_symbol(
                symbol.upper(),
                args.days,
                use_rth=use_rth,
                explicit_start=explicit_start,
                explicit_end=explicit_end,
                force_reload=bool(args.force_reload),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error during data loading", error=str(exc))
    finally:
        await loader.disconnect()


if __name__ == "__main__":
    ib_util.patchAsyncio()
    asyncio.run(main())
