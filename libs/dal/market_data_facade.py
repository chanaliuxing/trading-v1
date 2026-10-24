"""
Market Data Facade
Unified interface to access market data from ClickHouse
Based on doc/10.md and doc/33.md specifications
"""
from typing import List, Dict, Any, Optional, Callable, Awaitable
from datetime import datetime, timedelta
import asyncio
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

from libs.db.ch_pool import get_client, get_threadlocal_client, threadsafe_query
from libs.logger import get_logger

logger = get_logger(__name__)

# Shared thread pool for offloading blocking ClickHouse operations
# Max 8 workers since MarketDataFacade has many concurrent queries
_ch_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ch_query")

# Semaphore to limit concurrent ClickHouse queries (prevent overwhelming the database)
_ch_semaphore = asyncio.Semaphore(6)


class MarketDataFacade:
    """
    Market data access facade

    Provides high-level API to fetch:
    - 5-second bars
    - 10-second bars
    - 1-minute bars
    - Calculated features
    """

    def __init__(self, ch = None):
        """
        Initialize facade

        Args:
            ch: ClickHouse client (uses global pool if None)
        """
        self.ch = ch or get_client()
        logger.info("MarketDataFacade initialized (using global connection pool)")

    @staticmethod
    def _bars_query_sql(*, res: str, current_view: bool) -> str:
        if (not current_view) and str(res).lower() == "5s":
            return """
        SELECT
            ts,
            open,
            high,
            low,
            close,
            volume
        FROM
        (
            SELECT
                symbol,
                ts_start,
                ts_end as ts,
                open,
                high,
                low,
                close,
                volume
            FROM trading.bars_5s
            PREWHERE symbol = {symbol:String}
            WHERE ts_end >= now() - INTERVAL 3 DAY
            ORDER BY symbol ASC, ts_start ASC, ver DESC
            LIMIT 1 BY symbol, ts_start
        )
        ORDER BY ts DESC
        LIMIT {limit:UInt32}
        """
        table = f"trading.bars_{res}_current" if current_view else f"trading.bars_{res}"
        ts_expr = "ts_start" if current_view else "ts_end"
        time_filter = "" if current_view else "  AND ts_end >= now() - INTERVAL 3 DAY\n"
        return f"""
        SELECT
            {ts_expr} as ts,
            open,
            high,
            low,
            close,
            volume
        FROM {table}
        WHERE symbol = {{symbol:String}}
{time_filter}        ORDER BY ts DESC
        LIMIT {{limit:UInt32}}
        """

    @staticmethod
    def _should_query_current_view_first(res: str) -> bool:
        # bars_5s_current is a plain dedup view over bars_5s; on latest-bar
        # queries it can scan/sort the full table before the outer filter applies.
        return str(res).lower() != "5s"

    def _query_recent_bars(self, *, symbol: str, res: str, limit: int, current_view: bool) -> List[Dict[str, Any]]:
        sql = self._bars_query_sql(res=res, current_view=current_view)
        result = threadsafe_query(self.ch, sql, {"symbol": symbol, "limit": limit})
        if isinstance(result, list):
            result.reverse()
        return result

    @staticmethod
    def _bars_1m_from_5s_sql(*, current_view: bool, limit: int) -> str:
        table = "trading.bars_5s_current" if current_view else "trading.bars_5s"
        lookback_minutes = max(60, min(3 * 24 * 60, int(limit) * 3))
        if not current_view:
            return f"""
        SELECT
            ts,
            argMin(open, first_ts) as open,
            max(high) as high,
            min(low) as low,
            argMax(close, last_ts) as close,
            sum(volume) as volume
        FROM
        (
            SELECT
                toStartOfMinute(ts_start) as ts,
                ts_start as first_ts,
                ts_end as last_ts,
                open,
                high,
                low,
                close,
                volume
            FROM
            (
                SELECT
                    symbol,
                    ts_start,
                    ts_end,
                    open,
                    high,
                    low,
                    close,
                    volume
                FROM trading.bars_5s
                PREWHERE symbol = {{symbol:String}}
                WHERE ts_start >= now() - INTERVAL {lookback_minutes} MINUTE
                ORDER BY symbol ASC, ts_start ASC, ver DESC
                LIMIT 1 BY symbol, ts_start
            )
        )
        GROUP BY ts
        ORDER BY ts DESC
        LIMIT {{limit:UInt32}}
        """
        return f"""
        SELECT
            ts,
            argMin(open, first_ts) as open,
            max(high) as high,
            min(low) as low,
            argMax(close, last_ts) as close,
            sum(volume) as volume
        FROM
        (
            SELECT
                toStartOfMinute(ts_start) as ts,
                ts_start as first_ts,
                ts_end as last_ts,
                open,
                high,
                low,
                close,
                volume
            FROM {table}
            WHERE symbol = {{symbol:String}}
              AND ts_start >= now() - INTERVAL {lookback_minutes} MINUTE
        )
        GROUP BY ts
        ORDER BY ts DESC
        LIMIT {{limit:UInt32}}
        """

    def _query_recent_1m_from_5s(
        self,
        *,
        symbol: str,
        limit: int,
        current_view: bool,
    ) -> List[Dict[str, Any]]:
        sql = self._bars_1m_from_5s_sql(current_view=current_view, limit=limit)
        result = threadsafe_query(self.ch, sql, {"symbol": symbol, "limit": limit})
        if isinstance(result, list):
            result.reverse()
        return result

    def _query_recent_1m_from_5s_any(self, *, symbol: str, limit: int) -> List[Dict[str, Any]]:
        for current_view in (False,):
            try:
                result = self._query_recent_1m_from_5s(
                    symbol=symbol,
                    limit=limit,
                    current_view=current_view,
                )
                if result:
                    logger.warning(
                        "bars_1m_rollup_from_5s_used",
                        symbol=symbol,
                        rows=len(result),
                        source="bars_5s_current" if current_view else "bars_5s",
                    )
                    return result
            except Exception as exc:
                logger.warning(
                    "bars_1m_rollup_from_5s_failed",
                    symbol=symbol,
                    source="bars_5s_current" if current_view else "bars_5s",
                    error=str(exc),
                )
        return []

    @staticmethod
    def _is_rth_now() -> bool:
        try:
            from services.data_availability import DataAvailabilityService

            return bool(DataAvailabilityService.is_rth())
        except Exception:
            return False

    @classmethod
    def _log_fallback_used(cls, *, event: str, symbol: str, res: str, rows: int) -> None:
        payload = {"symbol": symbol, "res": res, "source": "base", "rows": int(rows)}
        if int(rows) > 0:
            logger.debug(event, **payload)
            return
        if cls._is_rth_now():
            logger.warning(event, **payload)
            return
        logger.debug(event, **payload)

    def get_bars_5s(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Get 5-second bars

        Args:
            symbol: Symbol
            start_time: Start time
            end_time: End time
            limit: Max bars to return

        Returns:
            List of bar dicts
        """
        sql = """
        SELECT
            symbol,
            ts_start,
            ts_end,
            open,
            high,
            low,
            close,
            vwap,
            volume,
            trades
        FROM bars_5s FINAL
        WHERE symbol = {symbol:String}
          AND ts_start >= {start:DateTime}
          AND ts_start < {end:DateTime}
        ORDER BY ts_start
        LIMIT {limit:UInt32}
        """

        params = {
            'symbol': symbol,
            'start': start_time,
            'end': end_time,
            'limit': limit
        }

        try:
            result = self.ch.query(sql, parameters=params)
            logger.debug("Fetched 5s bars", symbol=symbol, count=len(result))
            return result
        except Exception as e:
            logger.error("Failed to fetch 5s bars", error=str(e), symbol=symbol)
            return []

    def get_bars_5s_df(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000
    ) -> pd.DataFrame:
        """
        Get 5-second bars as DataFrame

        Args:
            symbol: Symbol
            start_time: Start time
            end_time: End time
            limit: Max bars

        Returns:
            DataFrame with OHLCV data
        """
        bars = self.get_bars_5s(symbol, start_time, end_time, limit)
        return pd.DataFrame(bars)

    def get_latest_bar_5s(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get latest 5-second bar

        Args:
            symbol: Symbol

        Returns:
            Latest bar or None
        """
        sql = """
        SELECT
            symbol,
            ts_start,
            ts_end,
            open,
            high,
            low,
            close,
            vwap,
            volume,
            trades
        FROM bars_5s FINAL
        WHERE symbol = {symbol:String}
        ORDER BY ts_start DESC
        LIMIT 1
        """

        try:
            result = self.ch.query(sql, parameters={'symbol': symbol})
            return result[0] if result else None
        except Exception as e:
            logger.error("Failed to fetch latest bar", error=str(e), symbol=symbol)
            return None

    def get_features_5s(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Get 5-second calculated features

        Args:
            symbol: Symbol
            start_time: Start time
            end_time: End time
            limit: Max records

        Returns:
            List of feature dicts
        """
        sql = """
        SELECT *
        FROM features_5s FINAL
        WHERE symbol = {symbol:String}
          AND ts >= {start:DateTime}
          AND ts < {end:DateTime}
        ORDER BY ts
        LIMIT {limit:UInt32}
        """

        params = {
            'symbol': symbol,
            'start': start_time,
            'end': end_time,
            'limit': limit
        }

        try:
            result = self.ch.query(sql, parameters=params)
            logger.debug("Fetched 5s features", symbol=symbol, count=len(result))
            return result
        except Exception as e:
            logger.error("Failed to fetch features", error=str(e), symbol=symbol)
            return []

    def get_recent_bars(
        self,
        symbol: str,
        lookback_minutes: int = 60,
        bar_size: str = '5s'
    ) -> List[Dict[str, Any]]:
        """
        Get recent bars

        Args:
            symbol: Symbol
            lookback_minutes: Minutes to look back
            bar_size: Bar size ('5s', '10s', '1m')

        Returns:
            List of bars
        """
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=lookback_minutes)

        table_map = {
            '5s': 'bars_5s',
            '10s': 'bars_10s',
            '1m': 'bars_1m'
        }

        table = table_map.get(bar_size, 'bars_5s')

        sql = f"""
        SELECT *
        FROM {table} FINAL
        WHERE symbol = {{symbol:String}}
          AND ts_start >= {{start:DateTime}}
        ORDER BY ts_start
        """

        params = {
            'symbol': symbol,
            'start': start_time
        }

        try:
            result = self.ch.query(sql, parameters=params)
            return result
        except Exception as e:
            logger.error("Failed to fetch recent bars",
                        error=str(e), symbol=symbol, bar_size=bar_size)
            return []

    def get_vwap(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime
    ) -> Optional[float]:
        """
        Calculate VWAP over period

        Args:
            symbol: Symbol
            start_time: Period start
            end_time: Period end

        Returns:
            VWAP or None
        """
        sql = """
        SELECT
            sum(close * volume) / sum(volume) as vwap
        FROM bars_5s FINAL
        WHERE symbol = {symbol:String}
          AND ts_start >= {start:DateTime}
          AND ts_start < {end:DateTime}
        """

        params = {
            'symbol': symbol,
            'start': start_time,
            'end': end_time
        }

        try:
            result = self.ch.query(sql, parameters=params)
            if result and result[0]['vwap']:
                return float(result[0]['vwap'])
            return None
        except Exception as e:
            logger.error("Failed to calculate VWAP", error=str(e), symbol=symbol)
            return None

    def get_bar_count(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime
    ) -> int:
        """
        Get number of bars in period

        Args:
            symbol: Symbol
            start_time: Period start
            end_time: Period end

        Returns:
            Bar count
        """
        sql = """
        SELECT count() as cnt
        FROM bars_5s FINAL
        WHERE symbol = {symbol:String}
          AND ts_start >= {start:DateTime}
          AND ts_start < {end:DateTime}
        """

        params = {
            'symbol': symbol,
            'start': start_time,
            'end': end_time
        }

        try:
            result = self.ch.query(sql, parameters=params)
            return int(result[0]['cnt']) if result else 0
        except Exception as e:
            logger.error("Failed to count bars", error=str(e), symbol=symbol)
            return 0

    def health_check(self) -> bool:
        """
        Check if data access is healthy

        Returns:
            True if healthy
        """
        return self.ch.health_check()

    # ========================================================================
    # Strategy Data Layer (doc/strategy/1.md)
    # ========================================================================

    def bars(self, symbol: str, res: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get recent N bars for a symbol at given resolution.
        Unified method for strategy data access.

        Args:
            symbol: Symbol (e.g., 'TSLA', 'SPY', 'VIX')
            res: Resolution ('1m', '5m', '15m', '30m', '1h', etc.)
            limit: Number of bars to fetch

        Returns:
            List of bars, sorted by ts ascending (oldest to newest)
            Each bar: {ts, open, high, low, close, volume}
        """
        if self._should_query_current_view_first(res):
            try:
                result = self._query_recent_bars(symbol=symbol, res=res, limit=limit, current_view=True)
                if result:
                    logger.debug(f"Fetched {len(result)} bars", symbol=symbol, res=res, source="current")
                    return result
            except Exception as exc:
                logger.warning("bars_current_fetch_failed", symbol=symbol, res=res, error=str(exc))

        try:
            result = self._query_recent_bars(symbol=symbol, res=res, limit=limit, current_view=False)
            if result:
                self._log_fallback_used(
                    event="bars_fetch_fallback_used",
                    symbol=symbol,
                    res=res,
                    rows=len(result),
                )
                self._clear_data_gate(symbol=symbol, res=res)
                return result
            if str(res).lower() == "1m":
                rollup = self._query_recent_1m_from_5s_any(symbol=symbol, limit=limit)
                if rollup:
                    self._clear_data_gate(symbol=symbol, res=res)
                    return rollup
            self._log_fallback_used(
                event="bars_fetch_fallback_used",
                symbol=symbol,
                res=res,
                rows=0,
            )
            self._mark_data_gate(
                symbol=symbol,
                res=res,
                reason="bars_fallback_empty",
                source="base",
            )
            return []
        except Exception as e:
            logger.error(f"Failed to fetch bars", error=str(e), symbol=symbol, res=res)
            self._mark_data_gate(
                symbol=symbol,
                res=res,
                reason="bars_fetch_failed",
                source="clickhouse",
                error=str(e),
            )
            return []

    async def bars_async(self, symbol: str, res: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Async version of bars() - offloads to thread pool to avoid blocking event loop.

        Args:
            symbol: Symbol (e.g., 'TSLA', 'SPY', 'VIX')
            res: Resolution ('1m', '5m', '15m', '30m', '1h', etc.)
            limit: Number of bars to fetch

        Returns:
            List of bars, sorted by ts ascending (oldest to newest)
        """
        def _query_in_thread() -> list:
            last_error = None
            if self._should_query_current_view_first(res):
                try:
                    result = self._query_recent_bars(symbol=symbol, res=res, limit=limit, current_view=True)
                    if result:
                        return result
                except Exception as exc:
                    last_error = str(exc)
                    logger.warning("bars_async_current_fetch_failed", symbol=symbol, res=res, error=last_error)

            try:
                result = self._query_recent_bars(symbol=symbol, res=res, limit=limit, current_view=False)
                if result:
                    self._log_fallback_used(
                        event="bars_async_fallback_used",
                        symbol=symbol,
                        res=res,
                        rows=len(result),
                    )
                    self._clear_data_gate(symbol=symbol, res=res)
                    return result
                if str(res).lower() == "1m":
                    rollup = self._query_recent_1m_from_5s_any(symbol=symbol, limit=limit)
                    if rollup:
                        self._clear_data_gate(symbol=symbol, res=res)
                        return rollup
                self._log_fallback_used(
                    event="bars_async_fallback_used",
                    symbol=symbol,
                    res=res,
                    rows=0,
                )
                self._mark_data_gate(
                    symbol=symbol,
                    res=res,
                    reason="bars_async_fallback_empty",
                    source="base",
                )
                return []
            except Exception as exc:
                last_error = str(exc)
                logger.error("bars_async_fetch_failed", symbol=symbol, res=res, error=last_error)
                self._mark_data_gate(
                    symbol=symbol,
                    res=res,
                    reason="bars_async_fetch_failed",
                    source="clickhouse",
                    error=last_error,
                )
                return []

        try:
            async with _ch_semaphore:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(_ch_executor, _query_in_thread)
            logger.debug(f"Fetched {len(result)} bars (async)", symbol=symbol, res=res)
            return result
        except Exception as e:
            logger.error(f"Failed to fetch bars (async)", error=str(e), symbol=symbol, res=res)
            self._mark_data_gate(
                symbol=symbol,
                res=res,
                reason="bars_async_fetch_failed",
                source="clickhouse",
                error=str(e),
            )
            return []

    @staticmethod
    def _mark_data_gate(
        *,
        symbol: str,
        res: str,
        reason: str,
        source: str,
        error: str = "",
    ) -> None:
        try:
            from services.data_availability import get_data_availability_service

            get_data_availability_service().mark_unavailable(
                dataset="market_bars",
                symbol=str(symbol or "").strip().upper(),
                resolution=str(res or "").strip(),
                reason=reason,
                source=source,
                severity="entry_blocking",
                error=error,
            )
        except Exception:
            pass

    @staticmethod
    def _clear_data_gate(*, symbol: str, res: str) -> None:
        try:
            from services.data_availability import get_data_availability_service

            get_data_availability_service().clear_symbol(
                dataset="market_bars",
                symbol=str(symbol or "").strip().upper(),
                resolution=str(res or "").strip(),
            )
        except Exception:
            pass

    def last_quote(
        self,
        symbol: str,
        asof: Optional[datetime] = None,
        staleness_sec: int = 5
    ) -> Dict[str, Optional[float]]:
        """
        Get latest L1 quote (bid/ask) for a symbol.

        Args:
            symbol: Symbol
            asof: Timestamp to query as of (default: now)
            staleness_sec: Max age of quote in seconds (default: 5)

        Returns:
            Dict with keys:
            - bid, ask: latest L1 fields
            - quote_ts: timestamp of latest quote used (if available)
        """
        if asof is None:
            # CRITICAL: Use timezone-aware datetime to match lake.quotes_l1 ts column
            from zoneinfo import ZoneInfo
            asof = datetime.now(ZoneInfo("America/New_York"))

        # CRITICAL FIX: clickhouse_connect doesn't handle timezone-aware datetime parameters correctly
        # Calculate time range in Python and pass as strings (INTERVAL clause with params doesn't work)
        asof_str = asof.strftime('%Y-%m-%d %H:%M:%S')
        asof_minus_staleness = asof - timedelta(seconds=staleness_sec)
        asof_minus_staleness_str = asof_minus_staleness.strftime('%Y-%m-%d %H:%M:%S')

        sql = """
        SELECT
            argMax(bid, ts) as bid,
            argMax(ask, ts) as ask,
            max(ts) as quote_ts
        FROM lake.quotes_l1
        WHERE symbol = {symbol:String}
          AND ts <= '""" + asof_str + """'
          AND ts > '""" + asof_minus_staleness_str + """'
        """

        try:
            result = self.ch.query(sql, {
                "symbol": symbol,
                "staleness": staleness_sec
            })
            if result and len(result) > 0:
                return {
                    "bid": result[0].get("bid"),
                    "ask": result[0].get("ask"),
                    "quote_ts": result[0].get("quote_ts"),
                }
            return {"bid": None, "ask": None, "quote_ts": None}
        except Exception as e:
            logger.error(f"Failed to fetch quote", error=str(e), symbol=symbol)
            return {"bid": None, "ask": None, "quote_ts": None}

    async def last_quote_async(
        self,
        symbol: str,
        asof: Optional[datetime] = None,
        staleness_sec: int = 5
    ) -> Dict[str, Optional[float]]:
        """
        Async version of last_quote() - offloads to thread pool to avoid blocking event loop.

        Args:
            symbol: Symbol
            asof: Timestamp to query as of (default: now)
            staleness_sec: Max age of quote in seconds (default: 5)

        Returns:
            Dict with keys:
            - bid, ask: latest L1 fields
            - quote_ts: timestamp of latest quote used (if available)
        """
        if asof is None:
            # CRITICAL: Use timezone-aware datetime to match lake.quotes_l1 ts column
            from zoneinfo import ZoneInfo
            asof = datetime.now(ZoneInfo("America/New_York"))

        # CRITICAL FIX: clickhouse_connect doesn't handle timezone-aware datetime parameters correctly
        # Calculate time range in Python and pass as strings (INTERVAL clause with params doesn't work)
        asof_str = asof.strftime('%Y-%m-%d %H:%M:%S')
        asof_minus_staleness = asof - timedelta(seconds=staleness_sec)
        asof_minus_staleness_str = asof_minus_staleness.strftime('%Y-%m-%d %H:%M:%S')

        sql = """
        SELECT
            argMax(bid, ts) as bid,
            argMax(ask, ts) as ask,
            max(ts) as quote_ts
        FROM lake.quotes_l1
        WHERE symbol = {symbol:String}
          AND ts <= '""" + asof_str + """'
          AND ts > '""" + asof_minus_staleness_str + """'
        """

        def _query_in_thread() -> dict:
            """Execute query in thread using global connection pool"""
            client = get_threadlocal_client()
            try:
                result = client.query(sql, {
                    "symbol": symbol,
                    "staleness": staleness_sec
                })
                # Client wrapper already returns list
                if result and len(result) > 0:
                    return {
                        "bid": result[0].get("bid"),
                        "ask": result[0].get("ask"),
                        "quote_ts": result[0].get("quote_ts"),
                    }
                return {"bid": None, "ask": None, "quote_ts": None}
            except Exception:
                return {"bid": None, "ask": None, "quote_ts": None}
            # No close() needed - connection pool handles reuse

        try:
            async with _ch_semaphore:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(_ch_executor, _query_in_thread)
            return result
        except Exception as e:
            logger.error(f"Failed to fetch quote (async)", error=str(e), symbol=symbol)
            return {"bid": None, "ask": None, "quote_ts": None}

    def vix_symbol(self) -> str:
        """
        Determine which VIX symbol to use (VIX or VIXY fallback).

        Returns:
            'VIX' if available, otherwise 'VIXY'
        """
        sql = """
        SELECT 1
        FROM trading.bars_1m_current
        WHERE symbol = 'VIX'
        LIMIT 1
        """

        try:
            result = self.ch.query(sql)
            if result and len(result) > 0:
                logger.debug("Using VIX symbol")
                return "VIX"
            logger.debug("VIX not available, falling back to VIXY")
            return "VIXY"
        except Exception:
            logger.warning("Error checking VIX availability, using VIXY")
            return "VIXY"

    async def vix_symbol_async(self) -> str:
        """
        Async version of vix_symbol() - offloads to thread pool to avoid blocking event loop.

        Returns:
            'VIX' if available, otherwise 'VIXY'
        """
        sql = """
        SELECT 1
        FROM trading.bars_1m_current
        WHERE symbol = 'VIX'
        LIMIT 1
        """

        def _query_in_thread() -> str:
            """Execute query in thread using global connection pool"""
            client = get_threadlocal_client()
            try:
                result = client.query(sql)
                # Client wrapper already returns list
                if result and len(result) > 0:
                    return "VIX"
                return "VIXY"
            except Exception:
                return "VIXY"
            # No close() needed - connection pool handles reuse

        try:
            async with _ch_semaphore:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(_ch_executor, _query_in_thread)
            logger.debug(f"Using {result} symbol (async)")
            return result
        except Exception:
            logger.warning("Error checking VIX availability (async), using VIXY")
            return "VIXY"

    async def poll_new_bar(
        self,
        symbol: str,
        res: str,
        poll_sec: int,
        on_new_bar: Callable[[Dict[str, Any]], Awaitable[None]]
    ) -> None:
        """
        Poll for new bars and trigger callback when a new bar appears.
        Runs continuously until cancelled.

        Args:
            symbol: Symbol to watch
            res: Resolution ('1m', '5m', etc.)
            poll_sec: Polling interval in seconds
            on_new_bar: Async callback function(bar_dict)
        """
        last_ts = None
        stale_window_minutes = 5
        if str(res) == "5s":
            stale_window_minutes = 2
        elif str(res) == "5m":
            stale_window_minutes = 30

        # Helper function to execute query in thread using global connection pool
        def _query_in_thread(sql: str, params: dict) -> list:
            """Execute ClickHouse query in thread using global connection pool"""
            client = get_threadlocal_client()
            try:
                # Client wrapper already returns list
                return client.query(sql, params)
            except Exception:
                return []
            # No close() needed - connection pool handles reuse

        def _query_1m_rollup_from_5s_in_thread(params: dict) -> list:
            """Build live 1m bars from the 5s stream when native 1m bars are absent."""
            client = get_threadlocal_client()
            for table in ("trading.bars_5s",):
                sql = f"""
                SELECT
                    ts,
                    argMin(open_px, first_ts) as open,
                    max(high_px) as high,
                    min(low_px) as low,
                    argMax(close_px, last_ts) as close,
                    sum(vol) as volume,
                    if(sum(vol) > 0, sum(ifNull(vwap_px, close_px) * vol) / sum(vol), argMax(close_px, last_ts)) as vwap,
                    sum(trades) as trades
                FROM
                (
                    SELECT
                        toStartOfMinute(ts_start) as ts,
                        ts_start as first_ts,
                        ts_end as last_ts,
                        open as open_px,
                        high as high_px,
                        low as low_px,
                        close as close_px,
                        volume as vol,
                        vwap as vwap_px,
                        trades
                    FROM
                    (
                        SELECT
                            ts_start,
                            ts_end,
                            symbol,
                            open,
                            high,
                            low,
                            close,
                            volume,
                            vwap,
                            trades
                        FROM {table}
                        PREWHERE symbol = {{symbol:String}}
                        WHERE ts_start >= now() - INTERVAL 20 MINUTE
                        ORDER BY symbol ASC, ts_start ASC, ver DESC
                        LIMIT 1 BY symbol, ts_start
                    )
                )
                GROUP BY ts
                ORDER BY ts DESC
                LIMIT 2
                """
                try:
                    result = client.query(sql, params)
                except Exception:
                    result = []
                if result:
                    return result
            return []

        while True:
            try:
                # Fetch the latest 2 bars
                if self._should_query_current_view_first(res):
                    table = f"trading.bars_{res}_current"
                    sql = f"""
                SELECT
                    ts_end as ts,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    vwap,
                    trades
                FROM {table}
                WHERE symbol = {{symbol:String}}
                  AND ts_end >= now() - INTERVAL {stale_window_minutes} MINUTE
                ORDER BY ts DESC
                LIMIT 2
                """
                else:
                    sql = f"""
                SELECT
                    ts,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    vwap,
                    trades
                FROM
                (
                    SELECT
                        ts_end as ts,
                        ts_start,
                        symbol,
                        open,
                        high,
                        low,
                        close,
                        volume,
                        vwap,
                        trades
                    FROM trading.bars_{res}
                    PREWHERE symbol = {{symbol:String}}
                    WHERE ts_end >= now() - INTERVAL {stale_window_minutes} MINUTE
                    ORDER BY symbol ASC, ts_start ASC, ver DESC
                    LIMIT 1 BY symbol, ts_start
                )
                ORDER BY ts DESC
                LIMIT 2
                """

                # Offload blocking query to thread executor to avoid blocking event loop
                # Use dedicated client per thread to avoid concurrent query errors
                async with _ch_semaphore:
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(_ch_executor, _query_in_thread, sql, {"symbol": symbol})
                    if (not result) and str(res) == "1m":
                        result = await loop.run_in_executor(
                            _ch_executor,
                            _query_1m_rollup_from_5s_in_thread,
                            {"symbol": symbol},
                        )

                if result and len(result) > 0:
                    latest = result[0]
                    latest_ts = latest.get("ts")

                    # Check if we have a new bar
                    if last_ts is None:
                        last_ts = latest_ts
                        logger.debug(f"Started polling", symbol=symbol, res=res, ts=latest_ts)
                    elif latest_ts > last_ts:
                        logger.debug(f"New bar detected", symbol=symbol, res=res, ts=latest_ts)
                        last_ts = latest_ts
                        # Trigger callback
                        await on_new_bar(latest)

            except asyncio.CancelledError:
                logger.info(f"Polling cancelled", symbol=symbol, res=res)
                raise
            except Exception as e:
                logger.error(f"Polling error", error=str(e), symbol=symbol, res=res)

            # Wait before next poll
            await asyncio.sleep(poll_sec)
