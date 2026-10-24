"""Market Data Fetcher - Fetches data from ClickHouse and Redis"""

import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import redis.asyncio as redis
import clickhouse_connect
from clickhouse_connect.driver.client import Client
import json

NY_TZ = ZoneInfo("America/New_York")


class MarketDataFetcher:
    """Fetches market data from ClickHouse and Redis"""

    def __init__(
        self,
        ch_host: str = "localhost",
        ch_port: int = 8123,
        ch_user: str = "default",
        ch_password: str = "",
        ch_database: str = "trading",
        redis_url: str = "redis://localhost:6379/0",
    ):
        self.ch_host = ch_host
        self.ch_port = ch_port
        self.ch_user = ch_user
        self.ch_password = ch_password
        self.ch_database = ch_database
        self.redis_url = redis_url

        self.ch_client: Optional[Client] = None
        self.redis_client: Optional[redis.Redis] = None

    async def connect(self):
        """Connect to ClickHouse and Redis"""
        # ClickHouse connection (sync)
        self.ch_client = clickhouse_connect.get_client(
            host=self.ch_host,
            port=self.ch_port,
            username=self.ch_user,
            password=self.ch_password,
            database=self.ch_database,
        )

        # Redis connection (async)
        self.redis_client = await redis.from_url(self.redis_url)
        print(f"Connected to ClickHouse and Redis")

    async def close(self):
        """Close connections"""
        if self.ch_client:
            self.ch_client.close()
        if self.redis_client:
            await self.redis_client.close()

    async def get_latest_5s_features(
        self, symbol: str, limit: int = 12
    ) -> List[Dict[str, Any]]:
        """Get latest 5s features from ClickHouse (sfs.sfs_5s table)

        Args:
            symbol: Stock symbol
            limit: Number of recent records to fetch (default 12 = 1 minute)

        Returns:
            List of 5s feature records
        """
        if not self.ch_client:
            raise RuntimeError("ClickHouse client not connected")

        query = f"""
        SELECT
            symbol,
            ts,
            open,
            high,
            low,
            close,
            vwap,
            volume,
            trades,
            ret_5s
        FROM sfs.sfs_5s
        WHERE symbol = '{symbol}'
        ORDER BY ts DESC
        LIMIT {limit}
        """

        result = self.ch_client.query(query)
        rows = result.result_rows

        # Convert to list of dicts
        features = []
        for row in rows:
            features.append({
                "symbol": row[0],
                "ts": row[1],
                "open": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "close": float(row[5]),
                "vwap": float(row[6]),
                "volume": float(row[7]),
                "trades": int(row[8]),
                "ret_5s": float(row[9]),
            })

        return features

    async def get_sfs_features(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get latest SFS features from trading.sfs_features table

        NOTE: trading.sfs_features is currently EMPTY (待激活). This method will return None.
        Use get_1s_bars_from_redis() or sfs.sfs_5s table for live features instead.

        Args:
            symbol: Stock symbol

        Returns:
            Dict of SFS features or None (currently always None as table is empty)
        """
        if not self.ch_client:
            raise RuntimeError("ClickHouse client not connected")

        # ⚠️ trading.sfs_features table is currently empty (Phase 1: Schema就绪, 待激活)
        # Skip querying to avoid unnecessary overhead
        # TODO: Remove this early return once sfs_features is populated
        return None

        # Original query (disabled until table is populated):
        # query = f"""
        # SELECT
        #     symbol,
        #     ts,
        #     low,
        #     high,
        #     close,
        #     volume,
        #     vwap_1m_tumble,
        #     atr_1m_tumble,
        #     orb5_high,
        #     orb5_low,
        #     q80_ewma,
        #     k_ewma,
        #     p_up_15,
        #     trend_push_15,
        #     regime
        # FROM trading.sfs_features
        # WHERE symbol = '{symbol}'
        # ORDER BY ts DESC
        # LIMIT 1
        # """
        #
        # result = self.ch_client.query(query)
        # rows = result.result_rows
        #
        # if not rows:
        #     return None
        #
        # row = rows[0]
        # return {
        #     "symbol": row[0],
        #     "ts": row[1],
        #     "low": float(row[2]),
        #     "high": float(row[3]),
        #     "close": float(row[4]),
        #     "volume": float(row[5]),
        #     "vwap_1m_tumble": float(row[6]) if row[6] else None,
        #     "atr_1m_tumble": float(row[7]) if row[7] else None,
        #     "orb5_high": float(row[8]) if row[8] else None,
        #     "orb5_low": float(row[9]) if row[9] else None,
        #     "q80_ewma": float(row[10]) if row[10] else None,
        #     "k_ewma": float(row[11]) if row[11] else None,
        #     "p_up_15": float(row[12]) if row[12] else None,
        #     "trend_push_15": float(row[13]) if row[13] else None,
        #     "regime": row[14] if row[14] else None,
        # }

    async def get_1s_bars_from_redis(
        self, symbol: str, n: int = 30
    ) -> List[Dict[str, Any]]:
        """Get 1s bars from Redis (if SFS 1s service is running)

        Args:
            symbol: Stock symbol
            n: Number of recent bars (default 30)

        Returns:
            List of 1s bar dicts
        """
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")

        key = f"sfs:1s:{symbol}"
        try:
            # Get last n items from list
            items = await self.redis_client.lrange(key, 0, n - 1)
            bars = []
            for item in items:
                bar = json.loads(item)
                bars.append(bar)
            return bars
        except Exception as e:
            print(f"Redis fetch error for {symbol}: {e}")
            return []

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price from latest 5s data

        Args:
            symbol: Stock symbol

        Returns:
            Current price or None
        """
        features = await self.get_latest_5s_features(symbol, limit=1)
        if features:
            return features[0]["close"]
        return None

    async def get_comprehensive_market_data(
        self, symbol: str
    ) -> Dict[str, Any]:
        """Get comprehensive market data for a symbol

        Args:
            symbol: Stock symbol

        Returns:
            Dict with all available market data, including real data timestamp
        """
        from datetime import timezone

        # Fetch all data concurrently
        results = await asyncio.gather(
            self.get_latest_5s_features(symbol, limit=12),
            self.get_sfs_features(symbol),
            self.get_1s_bars_from_redis(symbol, n=30),
            self.get_current_price(symbol),
            return_exceptions=True,
        )

        features_5s = results[0] if not isinstance(results[0], Exception) else []
        sfs_features = results[1] if not isinstance(results[1], Exception) else {}
        bars_1s = results[2] if not isinstance(results[2], Exception) else []
        current_price = results[3] if not isinstance(results[3], Exception) else 0.0

        # Extract real data timestamp (priority: Redis 1s > CH 5s > CH sfs)
        data_timestamp = None
        data_source = None

        # Priority 1: Redis 1s bars (most recent, highest frequency)
        if bars_1s and isinstance(bars_1s, list) and len(bars_1s) > 0:
            latest_bar = bars_1s[0]
            ts_value = latest_bar.get('ts')
            if ts_value:
                # Redis timestamp is in milliseconds
                if isinstance(ts_value, (int, float)):
                    data_timestamp = datetime.fromtimestamp(ts_value / 1000, tz=NY_TZ)
                    data_source = "redis_1s"
                elif isinstance(ts_value, datetime):
                    data_timestamp = ts_value.replace(tzinfo=NY_TZ) if ts_value.tzinfo is None else ts_value.astimezone(NY_TZ)
                    data_source = "redis_1s"

        # Priority 2: ClickHouse 5s features
        if data_timestamp is None and features_5s:
            ts_value = features_5s[0].get('ts')
            if ts_value:
                if isinstance(ts_value, datetime):
                    data_timestamp = ts_value.replace(tzinfo=NY_TZ) if ts_value.tzinfo is None else ts_value.astimezone(NY_TZ)
                    data_source = "clickhouse_5s"

        # Priority 3: ClickHouse sfs_features
        if data_timestamp is None and sfs_features and sfs_features.get('ts'):
            ts_value = sfs_features.get('ts')
            if isinstance(ts_value, datetime):
                data_timestamp = ts_value.replace(tzinfo=NY_TZ) if ts_value.tzinfo is None else ts_value.astimezone(NY_TZ)
                data_source = "clickhouse_sfs"

        # Convert to ISO format string
        timestamp_iso = data_timestamp.isoformat() if data_timestamp else None

        return {
            "symbol": symbol,
            "timestamp": timestamp_iso,  # Real data timestamp (not system time)
            "data_source": data_source,  # Which source provided the timestamp
            "current_price": current_price,
            "features_5s": features_5s,
            "sfs_features": sfs_features,
            "bars_1s": bars_1s,
        }

    async def get_multiple_symbols_data(
        self, symbols: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Get comprehensive data for multiple symbols concurrently

        Args:
            symbols: List of stock symbols

        Returns:
            Dict mapping symbol -> market data
        """
        tasks = [self.get_comprehensive_market_data(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        data = {}
        for i, symbol in enumerate(symbols):
            if not isinstance(results[i], Exception):
                data[symbol] = results[i]
            else:
                print(f"Error fetching data for {symbol}: {results[i]}")
                data[symbol] = {
                    "symbol": symbol,
                    "error": str(results[i]),
                    "current_price": 0.0,
                    "features_5s": [],
                    "sfs_features": {},
                    "bars_1s": [],
                }

        return data

    async def prefill_5s_from_clickhouse(
        self, symbol: str, minutes: int = 20
    ) -> List[Dict[str, Any]]:
        """从 ClickHouse sfs.sfs_5s 取最近 N 分钟 5s bar（仅用于冷启动预填充）。

        Args:
            symbol: Stock symbol
            minutes: Number of minutes to look back

        Returns:
            List of 5s bar dicts
        """
        if not self.ch_client:
            raise RuntimeError("ClickHouse client not connected")

        try:
            since = datetime.now(NY_TZ) - timedelta(minutes=max(1, int(minutes)) + 2)
            query = f"""
            SELECT ts, open, high, low, close, vwap, volume, trades
            FROM sfs.sfs_5s
            WHERE symbol = %(sym)s
              AND ts >= %(since)s
            ORDER BY ts ASC
            """
            result = self.ch_client.query(
                query, parameters={"sym": symbol, "since": since}
            )
            rows = result.result_rows

            out = []
            for r in rows:
                ts, o, h, l, c, vwap, vol, tr = r
                out.append({
                    "ts": ts,
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "vwap": float(vwap) if vwap else None,
                    "volume": float(vol),
                    "trades": int(tr),
                })
            return out
        except Exception as e:
            print(f"[prefill_5s] CH error for {symbol}: {e}")
            return []

    async def get_latest_vix(self) -> Optional[float]:
        """从 refs_vix_ticks 抓一个最近值；没有就返回 None。

        Returns:
            Latest VIX value or None
        """
        if not self.ch_client:
            raise RuntimeError("ClickHouse client not connected")

        try:
            query = """
            SELECT vix
            FROM trading.refs_vix_ticks
            WHERE ts >= now() - INTERVAL 30 MINUTE
            ORDER BY ts DESC
            LIMIT 1
            """
            result = self.ch_client.query(query)
            rows = result.result_rows
            return float(rows[0][0]) if rows else None
        except Exception as e:
            print(f"[vix] CH error: {e}")
            return None
