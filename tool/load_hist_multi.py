#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Historical Data Loader (IBKR, Multi-Interval) with Backfill/Forward modes.

Features
--------
- barSizeSetting 与 IBKR 对齐: "5 secs", "1/2/3/5/10/15/30 mins", "1 hour", "1 day"
- --fill-mode forward/backfill：
  * forward：仅续传（从 DB 最后一条之后开始）
  * backfill：忽略 DB 最后时间，严格覆盖 --days / --sessions 窗口
- --sessions N：按交易日抓取，自动换算日历天（含周末/节假日缓冲）
- 自动分片 (durationStr=K D) 以规避 IBKR pacing / limits
- 可选插入元数据列（若目标表无该列则自动回退仅基础列）
- ClickHouse ReplacingMergeTree(ver) 幂等写入；批内去重

Dependencies
------------
- ib_insync, pandas
- libs.db.clickhouse.ClickHouseDB (需提供 .client.insert_df(table=..., df=...))
- libs.logger.get_logger
- libs.config.config
"""
import asyncio
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone, date
from typing import Dict, List, Optional, Union
from zoneinfo import ZoneInfo

import pandas as pd
from ib_insync import IB, Stock, Index, Contract, util as ib_util

# Ensure project root for local imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)

# ---------------- IBKR bar-size normalization & limits ---------------- #

_ALLOWED_MINUTES = {1, 2, 3, 5, 10, 15, 30}

# 保守的“单次请求最大日历天数”上限（可用 --chunk-days 覆盖）
_DEFAULT_MAX_DAYS_PER_REQUEST = {
    "1 secs": 1,
    "5 secs": 1,
    "15 secs": 1,
    # IBKR is strict for 1m; use 1 day per request for reliability
    "1 min": 1,
    "2 mins": 365,
    "3 mins": 365,
    "5 mins": 365,
    "10 mins": 365,
    "15 mins": 365,
    "30 mins": 365,
    "1 hour": 365,
    "1 day": 365,
}

# 便捷别名
_BAR_SIZE_ALIASES = {
    "1s": "1 secs", "1sec": "1 secs", "1secs": "1 secs",
    "5s": "5 secs", "5sec": "5 secs", "5secs": "5 secs",
    "1m": "1 min", "2m": "2 mins", "3m": "3 mins", "5m": "5 mins",
    "10m": "10 mins", "15m": "15 mins", "30m": "30 mins",
    "60m": "1 hour", "1h": "1 hour", "1hr": "1 hour",
    # daily
    "1d": "1 day", "1day": "1 day",
}

def normalize_bar_size(s: str) -> str:
    x = s.strip().lower()
    if x in _BAR_SIZE_ALIASES:
        return _BAR_SIZE_ALIASES[x]
    known = {
        "1 secs",
        "5 secs",
        "15 secs",
        "1 min","2 mins","3 mins","5 mins","10 mins","15 mins","30 mins",
        "1 hour","1 day",
    }
    if x in known:
        return x
    if x.endswith("min"):
        n = x[:-3].strip()
        if n.isdigit() and int(n) in _ALLOWED_MINUTES:
            return f"{int(n)} min" if int(n) == 1 else f"{int(n)} mins"
    if x.endswith("mins"):
        n = x[:-4].strip()
        if n.isdigit() and int(n) in _ALLOWED_MINUTES:
            return f"{int(n)} min" if int(n) == 1 else f"{int(n)} mins"
    raise ValueError("Unsupported bar_size. Use e.g. '1 min', '5 mins', '1 hour', or aliases like 1m/5m/1h.")

def bar_size_seconds(bar_size: str) -> int:
    if bar_size == "1 secs":
        return 1
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
    """Return ClickHouse table name for a given IBKR bar size, using IBKR-suffixed tables.

    Examples:
    - "5 secs"  -> "bars_5s_ibkr"
    - "1 min"   -> "bars_1m_ibkr"
    - "10 mins" -> "bars_10m_ibkr"
    - "1 hour"  -> "bars_1h_ibkr"
    - "1 day"   -> "bars_1d_ibkr"
    """
    if bar_size == "5 secs":
        return "bars_5s_ibkr"
    if bar_size == "1 secs":
        return "bars_1s_ibkr"
    if bar_size == "15 secs":
        return "bars_15s_ibkr"
    if bar_size.endswith("min") or bar_size.endswith("mins"):
        return f"bars_{int(bar_size.split()[0])}m_ibkr"
    if bar_size == "1 hour":
        return "bars_1h_ibkr"
    if bar_size == "1 day":
        return "bars_1d_ibkr"
    # Fallback: normalize spaces and append ibkr
    return f"bars_{bar_size.replace(' ', '_')}_ibkr"

def safe_max_days_per_request(bar_size: str, override: Optional[int]) -> int:
    return override if (override and override > 0) else _DEFAULT_MAX_DAYS_PER_REQUEST.get(bar_size, 30)

# ---------------- Loader ---------------- #

class HistoricalDataLoader:
    """IBKR 多周期历史数据加载器，支持前向增量与回填模式。"""

    def __init__(
        self,
        ib_host: str,
        ib_port: int,
        ib_client_id: int,
        ib_timezone_str: Optional[str] = None,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        pacing_sleep_sec: float = 3.0,
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
        # Map to IB-friendly timezone label when needed
        tz_label = getattr(self.ib_timezone, "key", str(self.ib_timezone))
        if tz_label == "America/New_York":
            tz_label = "US/Eastern"
        self.ib_timezone_label = tz_label

        # Cache per-symbol qualified contracts (Stock vs Index, etc.)
        self._contract_cache: Dict[str, Contract] = {}

        logger.info(
            "HistoricalDataLoader initialized",
            ib_host=self.ib_host,
            ib_port=self.ib_port,
            timezone=str(self.ib_timezone),
            what_to_show=self.what_to_show,
            use_rth=self.use_rth,
        )

    async def connect(self) -> None:
        logger.info("Connecting to IB Gateway...", host=self.ib_host, port=self.ib_port)
        await self.ib.connectAsync(
            host=self.ib_host,
            port=self.ib_port,
            clientId=self.ib_client_id,
            timeout=60,
        )
        logger.info("✅ Connected to IB Gateway")

    async def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway")

    def _get_last_ts_from_db(self, symbol: str, table: str) -> Optional[datetime]:
        """兼容 get_last_bar_timestamp(symbol, table) 或 get_last_bar_timestamp(symbol)。"""
        try:
            return self.ch.get_last_bar_timestamp(symbol, table)  # type: ignore[arg-type]
        except TypeError:
            try:
                return self.ch.get_last_bar_timestamp(symbol)  # type: ignore[call-arg]
            except Exception:
                return None
        except Exception:
            return None

    def _ensure_target_table_exists(self, table: str, bar_secs: int) -> None:
        """
        Ensure target table exists for 1s backfill integration.
        For non-1s tables we keep current behavior (assume pre-created).
        """
        if table != "bars_1s_ibkr":
            return
        ddl = f"""
        CREATE TABLE IF NOT EXISTS trading.{table}
        (
            symbol String,
            bar_sec UInt16 DEFAULT {int(bar_secs)},
            ts_start DateTime,
            ts_end DateTime,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            vwap Nullable(Float64),
            volume Float64,
            trades UInt64,
            bid_last Nullable(Float64),
            ask_last Nullable(Float64),
            mid_last Nullable(Float64),
            what_to_show LowCardinality(String) DEFAULT 'TRADES',
            is_rth UInt8 DEFAULT 1,
            source LowCardinality(String) DEFAULT 'IBKR',
            exchange LowCardinality(String) DEFAULT 'SMART',
            currency LowCardinality(String) DEFAULT 'USD',
            tz LowCardinality(String) DEFAULT 'America/New_York',
            d Date DEFAULT toDate(ts_start),
            minute_of_day UInt16 DEFAULT (toHour(ts_start) * 60 + toMinute(ts_start)),
            ver UInt64,
            is_backfill UInt8 DEFAULT 0,
            created_at DateTime DEFAULT now(),
            ingest_ts DateTime64(3) DEFAULT now64(3),
            ohlc4 Float64 ALIAS (((open + high) + low) + close) / 4,
            hlc3 Float64 ALIAS ((high + low) + close) / 3,
            range Float64 ALIAS high - low,
            body Float64 ALIAS close - open,
            notional Float64 ALIAS volume * close
        )
        ENGINE = ReplacingMergeTree(ver)
        PARTITION BY toYYYYMMDD(ts_start)
        ORDER BY (symbol, ts_start)
        TTL ts_start + toIntervalDay(90)
        SETTINGS index_granularity = 8192
        """
        self.ch.query(ddl)

    async def _get_contract(self, symbol: str) -> Contract:
        """Resolve and qualify the appropriate IB contract for a symbol."""
        cached = self._contract_cache.get(symbol)
        if cached:
            return cached

        contract: Contract
        symbol_upper = symbol.upper()

        if symbol_upper == "VIX":
            exchanges = ["CBOE", "CBOEIND", "SMART"]
            last_exc: Optional[Exception] = None
            for exchange in exchanges:
                try:
                    candidate = Index("VIX", exchange, "USD")
                    qualified = await self.ib.qualifyContractsAsync(candidate)
                    if qualified:
                        contract = qualified[0]
                    else:
                        contract = candidate
                    self._contract_cache[symbol] = contract
                    logger.info(
                        "qualified_contract",
                        symbol=symbol,
                        exchange=exchange,
                        conId=getattr(contract, "conId", None),
                        secType=contract.secType,
                    )
                    return contract
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    logger.warning(
                        "contract_qualification_failed",
                        symbol=symbol,
                        exchange=exchange,
                        error=str(exc),
                    )
                    await asyncio.sleep(self.pacing_sleep_sec)
            raise RuntimeError(f"Unable to qualify contract for {symbol}: {last_exc}") from last_exc

        contract = Stock(symbol, "SMART", "USD")
        try:
            qualified = await self.ib.qualifyContractsAsync(contract)
            if qualified:
                contract = qualified[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("contract_qualification_failed", symbol=symbol, error=str(exc))
        self._contract_cache[symbol] = contract
        return contract

    def _expected_bars_for_day(self, bar_size: str, day: date) -> int:
        """
        Estimate expected bars for a single calendar day, used for coverage checks.

        For intraday bars with RTH enabled, assume 6.5h session; otherwise 24h.
        Weekends are treated as non-trading days when use_rth=True.
        """
        try:
            secs = bar_size_seconds(bar_size)
        except Exception:
            return 0

        # Daily bars: expect exactly 1 per day
        if secs >= 86400:
            return 1

        # Skip known market holidays/weekends
        if self.use_rth:
            from libs.utils.market_calendar import is_market_closed
            if is_market_closed(day):
                return 0

        # For RTH-only intraday data, skip weekends entirely
        if self.use_rth and day.weekday() >= 5:
            return 0

        trading_seconds = 6.5 * 3600 if self.use_rth else 24 * 3600
        if self.use_rth:
            from libs.utils.market_calendar import is_early_close_day
            if is_early_close_day(day):
                trading_seconds = 3.5 * 3600
        return max(1, int(trading_seconds // secs)) if trading_seconds > 0 else 0

    def _get_incomplete_dates(
        self,
        symbol: str,
        table: str,
        bar_size: str,
        start_day: date,
        end_day: date,
        min_coverage: float,
        max_scan_days: Optional[int] = None,
    ) -> set[date]:
        """
        Return a set of calendar dates in [start_day, end_day] where coverage < min_coverage.

        Coverage is computed as (bars_in_db / expected_bars_for_day).
        """
        if start_day > end_day:
            return set()

        # Safety: optionally clamp scan window to avoid CH OOM on very liquid symbols.
        # max_scan_days <= 0 means "scan full requested window".
        if max_scan_days is None:
            max_scan_days = int(os.environ.get("GAP_SCAN_MAX_DAYS", "45"))
        if max_scan_days > 0:
            max_start = end_day - timedelta(days=max_scan_days - 1)
            if start_day < max_start:
                start_day = max_start

        start_ts = f"{start_day.isoformat()} 00:00:00"
        # end bound is exclusive
        end_ts = f"{(end_day + timedelta(days=1)).isoformat()} 00:00:00"
        sql = f"""
        SELECT
            toDate(ts_start) AS d,
            count() AS c
        FROM {table}
        WHERE symbol = %(sym)s
          AND ts_start >= toDateTime(%(s)s)
          AND ts_start < toDateTime(%(e)s)
        GROUP BY d
        ORDER BY d
        SETTINGS
            max_bytes_before_external_group_by = 200000000,
            max_bytes_before_external_sort = 200000000,
            max_memory_usage = 6442450944
        """
        try:
            rows = self.ch.query(sql, {"sym": symbol, "s": start_ts, "e": end_ts})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gap_scan_clickhouse_failed",
                symbol=symbol,
                table=table,
                error=str(exc),
            )
            return set()

        counts_by_date = {}
        for r in rows:
            d_val = r.get("d")
            if isinstance(d_val, datetime):
                d_key = d_val.date()
            else:
                d_key = d_val  # type: ignore[assignment]
            try:
                c = int(r.get("c", 0) or 0)
            except Exception:
                c = 0
            if isinstance(d_key, date):
                counts_by_date[d_key] = c

        incomplete: set = set()
        cur = start_day
        while cur <= end_day:
            expected = self._expected_bars_for_day(bar_size, cur)
            # Non-trading day for RTH: skip
            if expected <= 0:
                cur = cur + timedelta(days=1)
                continue
            got = counts_by_date.get(cur, 0)
            cov = (got / expected) if expected > 0 else 0.0
            if cov < min_coverage:
                incomplete.add(cur)
            cur = cur + timedelta(days=1)

        return incomplete

    async def load_data_for_symbol(
        self,
        symbol: str,
        days: int,
        bar_size: str,
        fill_mode: str = "forward",
        max_days_per_req_override: Optional[int] = None,
        skip_covered_days: bool = False,
        coverage_threshold: float = 0.98,
        gap_scan_max_days: Optional[int] = None,
    ) -> int:
        """
        拉取并写入 *days* 的历史数据。
        fill_mode:
          - 'forward'：从 DB 最后时间往后续传
          - 'backfill'：忽略 DB 最后时间，严格覆盖窗口（解决“传 --days 20 但不够”的问题）
        """
        bar_size = normalize_bar_size(bar_size)
        bar_secs = bar_size_seconds(bar_size)
        table = table_name_for_bar(bar_size)
        max_days_per_req = safe_max_days_per_request(bar_size, max_days_per_req_override)
        self._ensure_target_table_exists(table, bar_secs)

        logger.info("========== Begin ==========", symbol=symbol, bar_size=bar_size, table=table, days=days, fill_mode=fill_mode)

        total_bars_inserted = 0
        last_ts_in_db = self._get_last_ts_from_db(symbol, table)
        last_ts_local = last_ts_in_db.replace(tzinfo=timezone.utc).astimezone(self.ib_timezone) if last_ts_in_db else None
        if last_ts_local:
            logger.info("Last record in DB", symbol=symbol, table=table, ts=last_ts_local.strftime('%Y-%m-%d %H:%M:%S'))

        # 目标窗口（按本地交易所时区计算）
        today_local_eod = datetime.now(self.ib_timezone).replace(hour=23, minute=59, second=59, microsecond=0)
        request_start_day = (today_local_eod - timedelta(days=days - 1)).date()

        # 起始日：依据 fill_mode 决定是否参考 DB 最后时间
        if fill_mode == "backfill":
            first_day_local = request_start_day
        else:
            if last_ts_local:
                candidate = (last_ts_local + timedelta(seconds=bar_secs)).date()
                first_day_local = max(request_start_day, candidate)
            else:
                first_day_local = request_start_day

        incomplete_dates: Optional[set] = None
        if fill_mode == "backfill" and skip_covered_days:
            # For gap-fill mode, default to full-window scan so older missing dates
            # are not silently ignored by a short scan clamp.
            effective_gap_scan_max_days = 0 if gap_scan_max_days is None else int(gap_scan_max_days)
            try:
                incomplete_dates = self._get_incomplete_dates(
                    symbol=symbol,
                    table=table,
                    bar_size=bar_size,
                    start_day=first_day_local,
                    end_day=today_local_eod.date(),
                    min_coverage=coverage_threshold,
                    max_scan_days=effective_gap_scan_max_days,
                )
                logger.info(
                    "gap_scan_completed",
                    symbol=symbol,
                    table=table,
                    bar_size=bar_size,
                    days=days,
                    incomplete_days=len(incomplete_dates),
                    coverage_threshold=coverage_threshold,
                    gap_scan_max_days=effective_gap_scan_max_days,
                )
                if not incomplete_dates:
                    logger.info("gap_scan_all_days_covered", symbol=symbol, table=table)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gap_scan_failed",
                    symbol=symbol,
                    table=table,
                    error=str(exc),
                )
                incomplete_dates = None

        if first_day_local > today_local_eod.date():
            logger.info("Up to date; nothing to fetch.", symbol=symbol, table=table)
            return 0

        # 分片抓取
        start_day = first_day_local
        while start_day <= today_local_eod.date():
            remaining_days = (today_local_eod.date() - start_day).days + 1
            chunk_days = min(remaining_days, max_days_per_req)
            chunk_end_day = start_day + timedelta(days=chunk_days - 1)

            # Decide whether this chunk needs fetching based on coverage (gap-fill mode)
            if incomplete_dates is not None:
                chunk_dates = [start_day + timedelta(days=i) for i in range(chunk_days)]
                # For RTH intraday data, ignore pure weekend chunks
                if self.use_rth and bar_size != "1 day":
                    trading_dates = [d for d in chunk_dates if d.weekday() < 5]
                else:
                    trading_dates = chunk_dates
                if not trading_dates:
                    logger.info(
                        "skip_chunk_non_trading_days",
                        symbol=symbol,
                        bar_size=bar_size,
                        start=str(start_day),
                        end=str(chunk_end_day),
                    )
                    start_day = chunk_end_day + timedelta(days=1)
                    continue
                if all(d not in incomplete_dates for d in trading_dates):
                    logger.info(
                        "skip_chunk_already_covered",
                        symbol=symbol,
                        bar_size=bar_size,
                        start=str(start_day),
                        end=str(chunk_end_day),
                    )
                    start_day = chunk_end_day + timedelta(days=1)
                    continue

            end_date_local = datetime.combine(chunk_end_day, datetime.max.time()).replace(
                hour=23, minute=59, second=59, microsecond=0, tzinfo=self.ib_timezone
            )
            end_date_param: Union[str, datetime] = end_date_local
            duration_str = f"{chunk_days} D"

            logger.info("Fetching chunk", symbol=symbol, bar_size=bar_size,
                        start=str(start_day), end=str(chunk_end_day), duration=duration_str)

            try:
                contract = await self._get_contract(symbol)
                if bar_size == "1 secs":
                    bars = []
                    one_day = start_day
                    while one_day <= chunk_end_day:
                        if self.use_rth:
                            from libs.utils.market_calendar import is_market_closed
                            if is_market_closed(one_day):
                                one_day = one_day + timedelta(days=1)
                                continue
                        # IB 1-second bars are limited per request; use 30-minute chunks.
                        if self.use_rth:
                            day_start = datetime.combine(one_day, datetime.min.time()).replace(
                                hour=9, minute=30, second=0, microsecond=0, tzinfo=self.ib_timezone
                            )
                            day_end = datetime.combine(one_day, datetime.min.time()).replace(
                                hour=16, minute=0, second=0, microsecond=0, tzinfo=self.ib_timezone
                            )
                        else:
                            day_start = datetime.combine(one_day, datetime.min.time()).replace(
                                hour=0, minute=0, second=0, microsecond=0, tzinfo=self.ib_timezone
                            )
                            day_end = datetime.combine(one_day, datetime.min.time()).replace(
                                hour=23, minute=59, second=59, microsecond=0, tzinfo=self.ib_timezone
                            )

                        window_end = day_start + timedelta(minutes=30)
                        while window_end <= day_end + timedelta(seconds=1):
                            end_param = min(window_end, day_end).strftime("%Y%m%d %H:%M:%S ") + self.ib_timezone_label
                            bars_win = await self.ib.reqHistoricalDataAsync(
                                contract=contract,
                                endDateTime=end_param,
                                durationStr="1800 S",
                                barSizeSetting=bar_size,
                                whatToShow=self.what_to_show,
                                useRTH=self.use_rth,
                                formatDate=2,
                            )
                            if bars_win:
                                bars.extend(list(bars_win))
                            await asyncio.sleep(self.pacing_sleep_sec)
                            window_end += timedelta(minutes=30)
                        one_day = one_day + timedelta(days=1)
                else:
                    bars = await self.ib.reqHistoricalDataAsync(
                        contract=contract,
                        endDateTime=end_date_param,
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
                            exchange=getattr(contract, "exchange", "SMART"),
                            currency=getattr(contract, "currency", "USD"),
                            tz=str(self.ib_timezone),
                        ),
                    )
                    total_bars_inserted += inserted
                    logger.info("Inserted", symbol=symbol, table=table, rows=inserted)

                await asyncio.sleep(self.pacing_sleep_sec)

            except Exception as exc:  # noqa: BLE001
                # Log richer diagnostics for easier troubleshooting
                logger.error(
                    "Error fetching chunk",
                    symbol=symbol,
                    start=str(start_day),
                    end=str(chunk_end_day),
                    error=str(exc),
                    exc_type=type(exc).__name__,
                    exc_repr=repr(exc),
                )
                await asyncio.sleep(self.pacing_sleep_sec)

            start_day = chunk_end_day + timedelta(days=1)

        logger.info("========== Finished ==========", symbol=symbol, table=table, total_inserted=total_bars_inserted)
        return total_bars_inserted

    def _is_regular_trading_hours(self, dt: datetime) -> bool:
        """Check if a given datetime falls within regular trading hours (9:30-16:00 ET, Mon-Fri)."""
        # Assume dt is already in the correct timezone (self.ib_timezone)
        if dt.weekday() >= 5:  # Saturday=5, Sunday=6
            return False

        # Regular trading hours: 9:30 AM - 4:00 PM
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
        if not bars:
            return 0

        bars_sorted = sorted(bars, key=lambda b: b.date)
        rows = []
        skipped_non_rth = 0
        for bar in bars_sorted:
            avg = getattr(bar, 'average', None)
            vwap = float(avg) if avg not in (None, -1, -1.0) else None

            # 兼容 IBKR 返回的 datetime / pandas.Timestamp / str
            dt_val = getattr(bar, 'date', None)
            if isinstance(dt_val, pd.Timestamp):
                # 去掉时区信息但不改变本地时刻（与原实现 replace(tzinfo=None) 语义一致）
                if dt_val.tz is not None:
                    dt_val = dt_val.tz_localize(None)
                ts_start = dt_val.to_pydatetime()
            elif isinstance(dt_val, datetime):
                ts_start = dt_val
                if getattr(ts_start, 'tzinfo', None) is not None:
                    ts_start = ts_start.replace(tzinfo=None)
            elif isinstance(dt_val, date):
                # IBKR 日线有时返回纯 date
                ts_start = datetime.combine(dt_val, datetime.min.time())
            elif isinstance(dt_val, str):
                ts_start = pd.to_datetime(dt_val).to_pydatetime()
                if getattr(ts_start, 'tzinfo', None) is not None:
                    ts_start = ts_start.replace(tzinfo=None)
            else:
                # 不识别的类型，尝试字符串化再解析
                ts_start = pd.to_datetime(str(dt_val)).to_pydatetime()
                if getattr(ts_start, 'tzinfo', None) is not None:
                    ts_start = ts_start.replace(tzinfo=None)

            # Filter out non-trading hours if use_rth is enabled
            # For daily bars (86400 seconds), skip RTH check as daily bars represent the entire day
            # if self.use_rth and bar_secs < 86400:
            #     # Add timezone info to check RTH (assumes ts_start is naive and in ib_timezone)
            #     ts_with_tz = ts_start.replace(tzinfo=self.ib_timezone)
            #     if not self._is_regular_trading_hours(ts_with_tz):
            #         skipped_non_rth += 1
            #         continue

            ts_end = ts_start + timedelta(seconds=bar_secs)

            minute_of_day = ts_start.hour * 60 + ts_start.minute  # 可选元数据列

            rows.append(
                {
                    # 基础列（所有 bars_* 表均存在）
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
                    # 这些字段在你的 DDL 里有默认值（不一定需要显式写）：
                    "is_backfill": is_backfill,

                    # ---- 可选元数据列（可能不存在，插入失败会自动回退到基础列） ---- #
                    "what_to_show": meta.get("what_to_show"),
                    "is_rth": meta.get("is_rth"),
                    "source": meta.get("source"),
                    "exchange": meta.get("exchange"),
                    "currency": meta.get("currency"),
                    "tz": meta.get("tz"),
                    "d": ts_start.date(),
                    "minute_of_day": minute_of_day,
                    "ingest_ts": datetime.now(),
                }
            )

        # Log skipped bars if any were filtered
        if skipped_non_rth > 0:
            logger.info("Filtered non-RTH bars", symbol=symbol, table=table, skipped=skipped_non_rth)

        df = pd.DataFrame(rows)
        if df.empty:
            logger.info("No bars to insert after RTH filtering", symbol=symbol, table=table)
            return 0

        # 类型对齐 & 批内幂等去重
        df["volume"] = df["volume"].astype("int64")
        df["trades"] = df["trades"].astype("int64")  # 目标是 UInt64，pandas 用 int64 即可
        df.drop_duplicates(subset=["symbol", "ts_start"], inplace=True)

        # --- 第一阶段：尝试全列插入（如表包含元数据列则一次成功） --- #
        try:
            self.ch.client.insert_df(table=table, df=df)
            return len(df)
        except Exception as exc_all:
            logger.warning("Full-column insert failed; fallback to base columns", table=table, error=str(exc_all))

        # --- 第二阶段：仅基础列插入（确保与最小 DDL 对齐，必成功） --- #
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

# ---------------- CLI ---------------- #

async def main() -> None:
    parser = argparse.ArgumentParser(description="Load historical market data from IBKR (multi-interval).")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols, e.g., TSLA NVDA AAPL")
    parser.add_argument("--days", type=int, default=None, help="Calendar days to fetch (inclusive window).")
    parser.add_argument("--sessions", type=int, default=None, help="Trading sessions (converted to calendar days with buffer).")
    parser.add_argument("--bar-size", dest="bar_size", type=str, required=True,
                        help="Bar size: '5 secs' | '1/2/3/5/10/15/30 mins' | '1 hour' | '1 day' (aliases: 1m/5m/1h/1d)")
    parser.add_argument("--fill-mode", choices=["forward", "backfill"], default="forward",
                        help="forward: only after last DB ts; backfill: ignore DB and fetch full window")
    parser.add_argument("--what-to-show", dest="what_to_show", type=str, default="TRADES",
                        help="TRADES|MIDPOINT|BID|ASK|BID_ASK|... (default: TRADES)")
    parser.add_argument("--use-rth", dest="use_rth", action="store_true", default=True, help="Use RTH only (default True)")
    parser.add_argument("--no-rth", dest="use_rth", action="store_false", help="Include all hours (RTH=False)")
    parser.add_argument("--tz", dest="ib_timezone_str", type=str, default=None,
                        help="Exchange timezone (IANA). Default from config or America/New_York.")
    parser.add_argument("--chunk-days", dest="chunk_days", type=int, default=None,
                        help="Override max days per IB request (expert).")
    parser.add_argument("--client-id", dest="client_id", type=int, default=None,
                        help="Override IBKR client id (default: config client_id + 200).")
    parser.add_argument(
        "--skip-covered-days",
        action="store_true",
        help="Skip days/chunks that appear fully covered in ClickHouse (gap-fill backfill).",
    )
    parser.add_argument(
        "--coverage-threshold",
        type=float,
        default=0.98,
        help="Per-day coverage threshold (0-1) for considering a day complete when --skip-covered-days is enabled.",
    )
    parser.add_argument(
        "--gap-scan-max-days",
        type=int,
        default=None,
        help="Max days to scan for coverage in --skip-covered-days mode; <=0 scans full window (default).",
    )

    args = parser.parse_args()

    # sessions -> days（含周末/节假日缓冲）
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
    default_client = ib_cfg.get("client_id", 1) + 200  # 避免与其他进程冲突
    ib_client_id = args.client_id if args.client_id is not None else default_client

    pacing_env = os.getenv("LOAD_HIST_PACING_SEC")
    try:
        pacing_sleep_sec = float(pacing_env) if pacing_env else 3.0
    except Exception:
        pacing_sleep_sec = 3.0

    loader = HistoricalDataLoader(
        ib_host=ib_host,
        ib_port=ib_port,
        ib_client_id=ib_client_id,
        ib_timezone_str=args.ib_timezone_str,
        what_to_show=args.what_to_show,
        use_rth=args.use_rth,
        pacing_sleep_sec=pacing_sleep_sec,
    )

    try:
        await loader.connect()
        # Process each symbol argument, also handling comma-separated lists within a single argument
        for raw_sym_arg in args.symbols:
            for sym in [s.strip().upper() for s in raw_sym_arg.split(',') if s.strip()]:
                await loader.load_data_for_symbol(
                    symbol=sym,
                    days=days,
                    bar_size=args.bar_size,
                    fill_mode=args.fill_mode,
                    max_days_per_req_override=args.chunk_days,
                    skip_covered_days=bool(getattr(args, "skip_covered_days", False)),
                    coverage_threshold=float(getattr(args, "coverage_threshold", 0.98)),
                    gap_scan_max_days=getattr(args, "gap_scan_max_days", None),
                )
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error during data loading", error=str(exc))
    finally:
        await loader.disconnect()

if __name__ == "__main__":
    ib_util.patchAsyncio()
    asyncio.run(main())


# # 取 TSLA 最近 7 天的 1 分钟 K，RTH 时段
# python scripts/load_hist_multi.py --symbols TSLA --days 20 --bar-size "1 min" fill-mode backfill

# # 取 NVDA/TSLA 最近 30 天的 5 分钟 K（默认按 365 天分片上限自动切片）
# python scripts/load_hist_multi.py --symbols NVDA TSLA --days 30 --bar-size 5m

# # 取 AAPL 最近 180 天的 1 小时 K，并指定每次请求最大 60 天（高级调参）
# python scripts/load_hist_multi.py --symbols AAPL --days 180 --bar-size 1h --chunk-days 60

# python scripts/load_hist_multi.py --symbols MSFT NVDA GOOGL AMZN META TSLA AMD AAPL  --days 180 --bar-size 5m --chunk-days 5

# # 拉 5 秒线（保持兼容）
# python scripts/load_hist_multi.py --symbols MSFT --days 3 --bar-size "5 secs"
"""
历史回填
python scripts/load_hist_multi.py 
    --symbols MSFT NVDA GOOGL AMZN META TSLA AMD AAPL 
    --sessions 20 
    --bar-size "1 min" 
    --fill-mode backfill


当日增量
python scripts/load_hist_multi.py 
  --symbols TSLA NVDA 
  --days 1 
  --bar-size 1m 
  --fill-mode forward

    python tool/load_hist_multi.py \
    --symbols AAPL TSLA NVDA AAPL MSFT META GOOGL AMZN AMD QQQ \
    --days 180 \
    --bar-size 5m \
    --fill-mode backfill \
    --chunk-days 5

    strategy:
  python tool/load_hist_multi.py \
    --symbols TSLA NVDA AAPL MSFT META GOOGL AMZN AMD QQQ \
    --days 180 \
    --bar-size 5m \
    --fill-mode backfill \
    --chunk-days 5
"""
