"""ClickHouse database connection and operations"""
from datetime import datetime
from typing import List, Dict, Any, Optional

from ..config import config
from ..logger import get_logger
from .ch_pool import get_client as _get_pooled_client

logger = get_logger(__name__)


class ClickHouseDB:
    """ClickHouse database connection manager"""

    def __init__(self):
        """Initialize ClickHouse client using the shared pooled client.

        Historically, this class created its own urllib3 PoolManager and its own
        clickhouse_connect.Client instance per usage site. That pattern works for
        short‑lived scripts but causes connection churn and, over many hours, can
        exhaust the OS ephemeral port range in long‑running services.

        We now delegate client creation to libs.db.ch_pool.get_client(), which
        returns a per‑process singleton Client backed by a shared PoolManager.
        """
        ch_config = config.clickhouse_config
        self.config = {
            'host': ch_config.get('host'),
            'port': ch_config.get('port'),
            'database': ch_config.get('database'),
            'username': ch_config.get('user'),
            'password': ch_config.get('password'),
        }
        # Use the pooled client wrapper – it exposes query/command/insert_df
        # with the same semantics that existing callers expect.
        self.client = _get_pooled_client()
        self.async_client = None

        safe_cfg = {k: ("***" if "password" in k.lower() else v) for k, v in self.config.items()}
        logger.debug("ClickHouse client initialized (pooled)", config=safe_cfg)

    async def init_async_client(self):
        """Initialize the asynchronous ClickHouse client."""
        if self.async_client is None:
            # Reuse the async client helper from ch_pool rather than creating
            # a fresh async client per ClickHouseDB instance.
            from .ch_pool import get_async_client as _get_async_client
            self.async_client = await _get_async_client()
            logger.debug("Async ClickHouse async_client initialized (pooled)")

    async def insert_async(self, table: str, data: List[List[Any]], column_names: Optional[List[str]] = None) -> None:
        """
        Asynchronously insert data into table.

        Args:
            table: Table name
            data: List of rows (each row is a list of values)
            column_names: Column names (optional)
        """
        if self.async_client is None:
            await self.init_async_client()
        await self.async_client.insert(table, data, column_names=column_names)
        logger.debug(f"Inserted {len(data)} rows into {table} asynchronously")

    def query(self, sql: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Execute a query and return results as a list of dicts.

        With the pooled client we usually receive a List[Dict[str, Any]]
        directly from ClickHouseClientWrapper.query(). For backwards
        compatibility with older call sites (and tests) that expect the
        clickhouse_connect.QueryResult API, we also handle that shape by
        calling .named_results() when available.
        """
        result = self.client.query(sql, parameters=parameters)
        # Pooled wrapper already returns List[Dict]; fast‑path that case.
        if isinstance(result, list):
            return result
        # Fallback for raw clickhouse_connect.QueryResult
        if hasattr(result, "named_results"):
            return list(result.named_results())
        return result

    def insert(self, table: str, data: List[List[Any]], column_names: Optional[List[str]] = None) -> None:
        """
        Insert data into table

        Args:
            table: Table name
            data: List of rows (each row is a list of values)
            column_names: Column names (optional)
        """
        self.client.insert(table, data, column_names=column_names)
        logger.debug(f"Inserted {len(data)} rows into {table}")

    def insert_df(self, table: str, df: Any, column_names: Optional[List[str]] = None) -> None:
        """
        Insert pandas DataFrame into table

        Args:
            table: Table name
            df: Pandas DataFrame
            column_names: Column names (optional, uses DataFrame columns if None)
        """
        self.client.insert_df(table, df, column_names=column_names)
        logger.debug(f"Inserted {len(df)} rows from DataFrame into {table}")

    def execute(self, sql: str, parameters: Optional[Dict[str, Any]] = None) -> None:
        """
        Execute a command without returning results

        Args:
            sql: SQL command
            parameters: Command parameters
        """
        self.client.command(sql, parameters=parameters)

    def get_last_bar_timestamp(self, symbol: str, table: Optional[str] = None) -> Optional[datetime]:
        """Return the latest ts_start for a symbol from the specified bars table.

        Falls back to `bars_5s` when table is not provided for backward compatibility.
        """
        table_name = table or 'bars_5s'
        sql = f"""
        SELECT max(ts_start) AS last_ts
        FROM {table_name}
        WHERE symbol = {{symbol:String}}
        """
        result = self.query(sql, {'symbol': symbol})
        if result and result[0].get('last_ts'):
            return result[0]['last_ts']
        return None

    def get_bars_5s(
        self,
        symbol: str,
        start_ts: str,
        end_ts: str,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Get 5-second bars for a symbol

        Args:
            symbol: Symbol name
            start_ts: Start timestamp (ISO format)
            end_ts: End timestamp (ISO format)
            limit: Maximum number of bars to return

        Returns:
            List of bars
        """
        sql = """
        SELECT *
        FROM bars_5s FINAL
        WHERE symbol = {symbol:String}
          AND ts_start >= {start_ts:DateTime}
          AND ts_start <= {end_ts:DateTime}
        ORDER BY ts_start
        LIMIT {limit:UInt32}
        """
        return self.query(sql, {'symbol': symbol, 'start_ts': start_ts, 'end_ts': end_ts, 'limit': limit})

    def get_features_5s(
        self,
        symbol: str,
        start_ts: str,
        end_ts: str,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Get 5-second features for a symbol

        Args:
            symbol: Symbol name
            start_ts: Start timestamp (ISO format)
            end_ts: End timestamp (ISO format)
            limit: Maximum number of feature records to return

        Returns:
            List of feature records
        """
        sql = """
        SELECT *
        FROM features_5s FINAL
        WHERE symbol = {symbol:String}
          AND ts >= {start_ts:DateTime}
          AND ts <= {end_ts:DateTime}
        ORDER BY ts
        LIMIT {limit:UInt32}
        """
        return self.query(sql, {'symbol': symbol, 'start_ts': start_ts, 'end_ts': end_ts, 'limit': limit})

    def health_check(self) -> bool:
        """
        Check database health

        Returns:
            True if healthy, False otherwise
        """
        try:
            result = self.query("SELECT 1 AS health")
            return len(result) > 0 and result[0].get('health') == 1
        except Exception as e:
            logger.error("ClickHouse health check failed", error=str(e))
            return False

    def close(self):
        """Close the client connection.

        For pooled clients this is effectively a no‑op: the shared client and
        underlying PoolManager live for the duration of the process. We keep the
        method for backward compatibility with older call sites.
        """
        logger.debug("ClickHouseDB.close() called (pooled client, no action)")

    async def close_async(self):
        """Close the async client connection"""
        if self.async_client:
            await self.async_client.close()
            logger.info("Async ClickHouse client closed")
