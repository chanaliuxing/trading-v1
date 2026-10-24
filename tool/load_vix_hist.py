#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VIX Historical Data Loader for IBKR

VIX is an Index, not a Stock, so it requires special handling.
This script loads VIX historical data using Index contract type.

Usage:
    python tool/load_vix_hist.py --days 180 --bar-size 1m
    python tool/load_vix_hist.py --days 180 --bar-size 5m
"""
import asyncio
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from ib_insync import IB, Index, util as ib_util

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)

# Bar size settings (from load_hist_multi.py)
_BAR_SIZE_ALIASES = {
    "5s": "5 secs", "5sec": "5 secs", "5secs": "5 secs",
    "1m": "1 min", "2m": "2 mins", "3m": "3 mins", "5m": "5 mins",
    "10m": "10 mins", "15m": "15 mins", "30m": "30 mins",
    "60m": "1 hour", "1h": "1 hour", "1hr": "1 hour",
    "1d": "1 day", "1day": "1 day",
}

_DEFAULT_MAX_DAYS_PER_REQUEST = {
    "5 secs": 1,
    "15 secs": 1,
    "1 min": 30,
    "2 mins": 365,
    "3 mins": 365,
    "5 mins": 365,
    "10 mins": 365,
    "15 mins": 365,
    "30 mins": 365,
    "1 hour": 365,
    "1 day": 365,
}


def normalize_bar_size(s: str) -> str:
    """Normalize bar size string to IBKR format."""
    x = s.strip().lower()
    if x in _BAR_SIZE_ALIASES:
        return _BAR_SIZE_ALIASES[x]
    known = {
        "5 secs", "15 secs",
        "1 min", "2 mins", "3 mins", "5 mins", "10 mins", "15 mins", "30 mins",
        "1 hour", "1 day",
    }
    if x in known:
        return x
    raise ValueError(f"Unsupported bar_size: {s}. Use e.g. '1 min', '5 mins', '1 hour', or aliases like 1m/5m/1h.")


def bar_size_seconds(bar_size: str) -> int:
    """Convert bar size to seconds."""
    if bar_size == "5 secs":
        return 5
    if bar_size == "15 secs":
        return 15
    if bar_size.endswith("min") or bar_size.endswith("mins"):
        return int(bar_size.split()[0]) * 60
    if bar_size == "1 hour":
        return 3600
    if bar_size == "1 day":
        return 86400
    raise ValueError(f"Cannot get seconds for bar_size '{bar_size}'")


def table_name_for_bar(bar_size: str) -> str:
    """Return ClickHouse table name for a given IBKR bar size."""
    if bar_size == "5 secs":
        return "bars_5s_ibkr"
    if bar_size == "15 secs":
        return "bars_15s_ibkr"
    if bar_size.endswith("min") or bar_size.endswith("mins"):
        return f"bars_{int(bar_size.split()[0])}m_ibkr"
    if bar_size == "1 hour":
        return "bars_1h_ibkr"
    if bar_size == "1 day":
        return "bars_1d_ibkr"
    return f"bars_{bar_size.replace(' ', '_')}_ibkr"


def safe_max_days_per_request(bar_size: str, override: Optional[int]) -> int:
    """Get max days per request for a bar size."""
    return override if (override and override > 0) else _DEFAULT_MAX_DAYS_PER_REQUEST.get(bar_size, 30)


class VIXHistoricalDataLoader:
    """VIX-specific historical data loader using Index contract type."""

    def __init__(
        self,
        ib_host: str,
        ib_port: int,
        ib_client_id: int,
        ib_timezone_str: Optional[str] = None,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        pacing_sleep_sec: float = 1.2,
    ):
        self.ib_host = ib_host
        self.ib_port = ib_port
        self.ib_client_id = ib_client_id
        self.ib = IB()
        self.ch = ClickHouseDB()
        self.what_to_show = what_to_show
        self.use_rth = use_rth
        self.pacing_sleep_sec = pacing_sleep_sec

        tz = ib_timezone_str or config.get('ib.timezone', 'America/New_York')
        self.ib_timezone = ZoneInfo(tz)
        tz_label = getattr(self.ib_timezone, "key", str(self.ib_timezone))
        if tz_label == "America/New_York":
            tz_label = "US/Eastern"
        self.ib_timezone_label = tz_label

        logger.info(
            "VIXHistoricalDataLoader initialized",
            ib_host=self.ib_host,
            ib_port=self.ib_port,
            timezone=str(self.ib_timezone),
            what_to_show=self.what_to_show,
            use_rth=self.use_rth,
        )

    async def connect(self) -> None:
        """Connect to IB Gateway and qualify VIX contract."""
        logger.info("Connecting to IB Gateway...", host=self.ib_host, port=self.ib_port)
        await self.ib.connectAsync(
            host=self.ib_host,
            port=self.ib_port,
            clientId=self.ib_client_id,
            timeout=10,
        )
        logger.info("✅ Connected to IB Gateway")

        # Try to qualify VIX contract with multiple exchanges
        self.vix_contract = None
        exchanges = ['CBOE', 'CBOEIND', 'SMART']

        for exchange in exchanges:
            try:
                contract = Index('VIX', exchange, 'USD')
                details = await self.ib.reqContractDetailsAsync(contract)
                if details:
                    qualified = await self.ib.qualifyContractsAsync(contract)
                    if qualified:
                        self.vix_contract = qualified[0]
                        logger.info(
                            "✅ VIX contract qualified",
                            exchange=exchange,
                            conId=self.vix_contract.conId,
                            localSymbol=self.vix_contract.localSymbol
                        )
                        break
            except Exception as exc:
                logger.warning("Failed to qualify VIX on exchange", exchange=exchange, error=str(exc))

        if not self.vix_contract:
            raise RuntimeError("Failed to qualify VIX contract on any exchange. Check IBKR entitlements.")

    async def disconnect(self) -> None:
        """Disconnect from IB Gateway."""
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway")

    def _get_last_ts_from_db(self, symbol: str, table: str) -> Optional[datetime]:
        """Get last timestamp from database for symbol."""
        try:
            return self.ch.get_last_bar_timestamp(symbol, table)
        except TypeError:
            try:
                return self.ch.get_last_bar_timestamp(symbol)
            except Exception:
                return None
        except Exception:
            return None

    async def load_vix_data(
        self,
        days: int,
        bar_size: str,
        fill_mode: str = "forward",
        max_days_per_req_override: Optional[int] = None,
    ) -> int:
        """Load VIX historical data."""
        symbol = "VIX"
        bar_size = normalize_bar_size(bar_size)
        bar_secs = bar_size_seconds(bar_size)
        table = table_name_for_bar(bar_size)
        max_days_per_req = safe_max_days_per_request(bar_size, max_days_per_req_override)

        logger.info(
            "========== Begin VIX Load ==========",
            symbol=symbol,
            bar_size=bar_size,
            table=table,
            days=days,
            fill_mode=fill_mode
        )

        total_bars_inserted = 0
        last_ts_in_db = self._get_last_ts_from_db(symbol, table)
        last_ts_local = last_ts_in_db.replace(tzinfo=timezone.utc).astimezone(self.ib_timezone) if last_ts_in_db else None

        if last_ts_local:
            logger.info("Last record in DB", symbol=symbol, table=table, ts=last_ts_local.strftime('%Y-%m-%d %H:%M:%S'))

        # Calculate date window
        today_local_eod = datetime.now(self.ib_timezone).replace(hour=23, minute=59, second=59, microsecond=0)
        request_start_day = (today_local_eod - timedelta(days=days - 1)).date()

        if fill_mode == "backfill":
            first_day_local = request_start_day
        else:
            if last_ts_local:
                candidate = (last_ts_local + timedelta(seconds=bar_secs)).date()
                first_day_local = max(request_start_day, candidate)
            else:
                first_day_local = request_start_day

        if first_day_local > today_local_eod.date():
            logger.info("Up to date; nothing to fetch.", symbol=symbol, table=table)
            return 0

        # Fetch data in chunks
        start_day = first_day_local
        while start_day <= today_local_eod.date():
            remaining_days = (today_local_eod.date() - start_day).days + 1
            chunk_days = min(remaining_days, max_days_per_req)
            chunk_end_day = start_day + timedelta(days=chunk_days - 1)

            # For the most recent chunk, use empty string (means "now")
            # For historical chunks, use specific end date
            is_latest_chunk = (chunk_end_day >= today_local_eod.date())

            if is_latest_chunk:
                end_date_str = ''  # Empty string means "now" for IBKR
            else:
                end_date_local = datetime.combine(chunk_end_day, datetime.max.time()).replace(
                    hour=23, minute=59, second=59, microsecond=0, tzinfo=self.ib_timezone
                )
                # Use simpler format: yyyymmdd HH:MM:SS timezone
                end_date_str = f"{end_date_local.strftime('%Y%m%d %H:%M:%S')} {self.ib_timezone_label}"

            duration_str = f"{chunk_days} D"

            logger.info(
                "Fetching VIX chunk",
                symbol=symbol,
                bar_size=bar_size,
                start=str(start_day),
                end=str(chunk_end_day),
                duration=duration_str
            )

            try:
                bars = await self.ib.reqHistoricalDataAsync(
                    contract=self.vix_contract,
                    endDateTime=end_date_str,
                    durationStr=duration_str,
                    barSizeSetting=bar_size,
                    whatToShow=self.what_to_show,
                    useRTH=self.use_rth,
                    formatDate=2,
                )

                if not bars:
                    logger.warning("No data returned", symbol=symbol, start=str(start_day), end=str(chunk_end_day))
                else:
                    inserted = await self._insert_bars_to_clickhouse(
                        symbol=symbol,
                        table=table,
                        bar_secs=bar_secs,
                        bars=bars,
                        is_backfill=(1 if fill_mode == "backfill" else 0),
                        meta=dict(
                            what_to_show=self.what_to_show,
                            is_rth=(1 if self.use_rth else 0),
                            source="IBKR",
                            exchange=self.vix_contract.exchange,
                            currency="USD",
                            tz=str(self.ib_timezone),
                        ),
                    )
                    total_bars_inserted += inserted
                    logger.info("Inserted", symbol=symbol, table=table, rows=inserted)

                await asyncio.sleep(self.pacing_sleep_sec)

            except Exception as exc:
                logger.error(
                    "Error fetching VIX chunk",
                    symbol=symbol,
                    start=str(start_day),
                    end=str(chunk_end_day),
                    error=str(exc),
                    exc_type=type(exc).__name__,
                )
                await asyncio.sleep(self.pacing_sleep_sec)

            start_day = chunk_end_day + timedelta(days=1)

        logger.info(
            "========== Finished VIX Load ==========",
            symbol=symbol,
            table=table,
            total_inserted=total_bars_inserted
        )
        return total_bars_inserted

    def _is_regular_trading_hours(self, dt: datetime) -> bool:
        """Check if datetime is within regular trading hours."""
        if dt.weekday() >= 5:  # Weekend
            return False
        market_open = dt.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = dt.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= dt < market_close

    async def _insert_bars_to_clickhouse(
        self,
        symbol: str,
        table: str,
        bar_secs: int,
        bars: List,
        is_backfill: int,
        meta: dict,
    ) -> int:
        """Insert bars to ClickHouse."""
        if not bars:
            return 0

        bars_sorted = sorted(bars, key=lambda b: b.date)
        rows = []
        skipped_non_rth = 0

        for bar in bars_sorted:
            avg = getattr(bar, 'average', None)
            vwap = float(avg) if avg not in (None, -1, -1.0) else None

            # Handle date/timestamp conversion
            dt_val = getattr(bar, 'date', None)
            if isinstance(dt_val, pd.Timestamp):
                if dt_val.tz is not None:
                    dt_val = dt_val.tz_localize(None)
                ts_start = dt_val.to_pydatetime()
            elif isinstance(dt_val, datetime):
                ts_start = dt_val
                if getattr(ts_start, 'tzinfo', None) is not None:
                    ts_start = ts_start.replace(tzinfo=None)
            else:
                ts_start = pd.to_datetime(str(dt_val)).to_pydatetime()
                if getattr(ts_start, 'tzinfo', None) is not None:
                    ts_start = ts_start.replace(tzinfo=None)

            # Filter non-RTH if enabled (skip for daily bars)
            if self.use_rth and bar_secs < 86400:
                ts_with_tz = ts_start.replace(tzinfo=self.ib_timezone)
                if not self._is_regular_trading_hours(ts_with_tz):
                    skipped_non_rth += 1
                    continue

            ts_end = ts_start + timedelta(seconds=bar_secs)
            minute_of_day = ts_start.hour * 60 + ts_start.minute

            rows.append({
                "ts_start": ts_start,
                "ts_end": ts_end,
                "symbol": symbol,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "vwap": vwap,
                "volume": int(getattr(bar, 'volume', 0) or 0),
                "trades": int(getattr(bar, 'barCount', 0) or 0),
                "bid_last": None,
                "ask_last": None,
                "mid_last": float(bar.close),
                "ver": int(datetime.now().timestamp() * 1e6),
                "is_backfill": is_backfill,
                # Optional metadata
                "what_to_show": meta.get("what_to_show"),
                "is_rth": meta.get("is_rth"),
                "source": meta.get("source"),
                "exchange": meta.get("exchange"),
                "currency": meta.get("currency"),
                "tz": meta.get("tz"),
                "d": ts_start.date(),
                "minute_of_day": minute_of_day,
                "ingest_ts": datetime.now(),
            })

        if skipped_non_rth > 0:
            logger.info("Filtered non-RTH bars", symbol=symbol, table=table, skipped=skipped_non_rth)

        df = pd.DataFrame(rows)
        if df.empty:
            logger.info("No bars to insert after RTH filtering", symbol=symbol, table=table)
            return 0

        df["volume"] = df["volume"].astype("int64")
        df["trades"] = df["trades"].astype("int64")
        df.drop_duplicates(subset=["symbol", "ts_start"], inplace=True)

        # Try full insert first, fallback to base columns
        try:
            self.ch.client.insert_df(table=table, df=df)
            return len(df)
        except Exception as exc_all:
            logger.warning("Full-column insert failed; fallback to base columns", table=table, error=str(exc_all))

        base_cols = [
            "ts_start", "ts_end", "symbol", "open", "high", "low", "close",
            "vwap", "volume", "trades", "bid_last", "ask_last", "mid_last",
            "ver", "is_backfill",
        ]
        df_base = df[base_cols].copy()

        try:
            self.ch.client.insert_df(table=table, df=df_base)
            return len(df_base)
        except Exception as exc_base:
            logger.error("Base-column insert failed", table=table, error=str(exc_base))
            return 0


async def main() -> None:
    parser = argparse.ArgumentParser(description="Load VIX historical market data from IBKR")
    parser.add_argument("--days", type=int, default=None, help="Calendar days to fetch")
    parser.add_argument("--sessions", type=int, default=None, help="Trading sessions (converted to calendar days)")
    parser.add_argument("--bar-size", dest="bar_size", type=str, required=True,
                       help="Bar size: '1/5/10/15/30 mins' | '1 hour' | '1 day' (aliases: 1m/5m/1h/1d)")
    parser.add_argument("--fill-mode", choices=["forward", "backfill"], default="forward",
                       help="forward: only after last DB ts; backfill: ignore DB and fetch full window")
    parser.add_argument("--what-to-show", dest="what_to_show", type=str, default="TRADES",
                       help="TRADES|INDEX|MIDPOINT|BID|ASK|... (default: TRADES, try INDEX if fails)")
    parser.add_argument("--use-rth", dest="use_rth", action="store_true", default=True, help="Use RTH only")
    parser.add_argument("--no-rth", dest="use_rth", action="store_false", help="Include all hours")
    parser.add_argument("--tz", dest="ib_timezone_str", type=str, default=None,
                       help="Exchange timezone (IANA). Default from config or America/New_York.")
    parser.add_argument("--chunk-days", dest="chunk_days", type=int, default=None,
                       help="Override max days per IB request")

    args = parser.parse_args()

    # Convert sessions to days if provided
    if args.sessions is not None:
        days = int(math.ceil(args.sessions * 7 / 5.0)) + 4
    elif args.days is not None:
        days = args.days
    else:
        parser.error("You must specify either --sessions or --days")

    # IB connection settings
    ib_cfg = config.get("ib", {})
    ib_host = os.getenv("IB_HOST", ib_cfg.get("host", "127.0.0.1"))
    ib_port = int(os.getenv("IB_PAPER_PORT", ib_cfg.get("paper_port", 4002)))
    ib_client_id = ib_cfg.get("client_id", 1) + 300  # Avoid conflicts with other processes

    loader = VIXHistoricalDataLoader(
        ib_host=ib_host,
        ib_port=ib_port,
        ib_client_id=ib_client_id,
        ib_timezone_str=args.ib_timezone_str,
        what_to_show=args.what_to_show,
        use_rth=args.use_rth,
        pacing_sleep_sec=1.2,
    )

    try:
        await loader.connect()
        await loader.load_vix_data(
            days=days,
            bar_size=args.bar_size,
            fill_mode=args.fill_mode,
            max_days_per_req_override=args.chunk_days,
        )
    except Exception as exc:
        logger.error("Unhandled error during VIX data loading", error=str(exc))
    finally:
        await loader.disconnect()


if __name__ == "__main__":
    ib_util.patchAsyncio()
    asyncio.run(main())
