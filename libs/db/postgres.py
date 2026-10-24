"""
PostgreSQL database connection using asyncpg
Async connection pool for non-blocking database operations
"""
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager
import os

from ..config import config
from ..logger import get_logger

logger = get_logger(__name__)

try:
    import asyncpg  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    asyncpg = None


class PostgresDB:
    """PostgreSQL database connection manager with asyncpg"""

    def __init__(self, min_conn: int = 1, max_conn: int = 10):
        """
        Initialize PostgreSQL connection pool manager

        Args:
            min_conn: Minimum number of connections in pool
            max_conn: Maximum number of connections in pool
        """
        self.min_conn = min_conn
        self.max_conn = max_conn
        self.pool: Optional[asyncpg.Pool] = None
        self._pg_config = config.pg_config

        logger.info("PostgresDB initialized (asyncpg)",
                   min_conn=min_conn, max_conn=max_conn)

    async def connect(self):
        """Create connection pool (call once at startup)"""
        if asyncpg is None:  # pragma: no cover
            raise ModuleNotFoundError(
                "Missing dependency `asyncpg` required for PostgresDB. Install requirements.txt / asyncpg to use Postgres."
            )
        if self.pool is not None:
            logger.warning("Connection pool already exists")
            return

        # Convert config format from psycopg2 to asyncpg
        dsn = (
            f"postgresql://{self._pg_config['user']}:{self._pg_config['password']}"
            f"@{self._pg_config['host']}:{self._pg_config['port']}"
            f"/{self._pg_config['database']}"
        )

        tz = self._pg_config.get("timezone") or os.getenv("PG_SESSION_TIMEZONE", "America/New_York")

        async def _init_conn(conn):
            if tz:
                try:
                    await conn.execute(f"SET TIME ZONE '{tz}'")
                except Exception:
                    logger.warning("pg_set_timezone_failed", timezone=tz)

        self.pool = await asyncpg.create_pool(
            dsn,
            min_size=self.min_conn,
            max_size=self.max_conn,
            command_timeout=60,
            init=_init_conn
        )

        logger.info("PostgreSQL connection pool created (asyncpg)",
                   min_size=self.min_conn,
                   max_size=self.max_conn,
                   host=self._pg_config['host'],
                   database=self._pg_config['database'])

    async def execute(self, query: str, *params) -> str:
        """
        Execute a query without returning results

        Args:
            query: SQL query (use $1, $2, ... for parameters)
            *params: Query parameters

        Returns:
            Status string from database
        """
        if not self.pool:
            await self.connect()

        # Check if we're in a transaction (pool is ConnectionWrapper)
        if hasattr(self.pool, 'execute') and not hasattr(self.pool, 'acquire'):
            # Inside transaction - use wrapper directly
            return await self.pool.execute(query, *params)

        async with self.pool.acquire() as conn:
            return await conn.execute(query, *params)

    async def fetchone(self, query: str, *params) -> Optional[Dict[str, Any]]:
        """
        Fetch one result

        Args:
            query: SQL query (use $1, $2, ... for parameters)
            *params: Query parameters

        Returns:
            Single row as dict or None
        """
        if not self.pool:
            await self.connect()

        # Check if we're in a transaction (pool is ConnectionWrapper)
        if hasattr(self.pool, 'fetchone') and not hasattr(self.pool, 'acquire'):
            # Inside transaction - use wrapper directly
            return await self.pool.fetchone(query, *params)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return dict(row) if row else None

    async def fetchrow(self, query: str, *params) -> Optional[Dict[str, Any]]:
        """
        Compatibility alias for code paths that use asyncpg-style naming.

        Returns:
            Single row as dict or None
        """
        return await self.fetchone(query, *params)

    async def fetchall(self, query: str, *params) -> List[Dict[str, Any]]:
        """
        Fetch all results

        Args:
            query: SQL query (use $1, $2, ... for parameters)
            *params: Query parameters

        Returns:
            List of rows as dicts
        """
        if not self.pool:
            await self.connect()

        # Check if we're in a transaction (pool is ConnectionWrapper)
        if hasattr(self.pool, 'fetchall') and not hasattr(self.pool, 'acquire'):
            # Inside transaction - use wrapper directly
            return await self.pool.fetchall(query, *params)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def bulk_insert(self, table: str, columns: List[str], values: List[tuple]) -> None:
        """
        Bulk insert rows

        Args:
            table: Table name (with schema if needed)
            columns: Column names
            values: List of value tuples
        """
        if not self.pool:
            await self.connect()

        if not values:
            return

        # Build column list
        cols = ', '.join(columns)

        # Build placeholders ($1, $2, ..., $N)
        placeholders = ', '.join(f'${i+1}' for i in range(len(columns)))

        query = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"

        async with self.pool.acquire() as conn:
            await conn.executemany(query, values)

    async def call_procedure(self, proc_name: str, *params) -> List[Dict[str, Any]]:
        """
        Call a stored procedure

        Args:
            proc_name: Procedure name
            *params: Procedure parameters

        Returns:
            Results from procedure
        """
        if not self.pool:
            await self.connect()

        # Build CALL statement
        placeholders = ', '.join(f'${i+1}' for i in range(len(params)))
        query = f"CALL {proc_name}({placeholders})"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    @asynccontextmanager
    async def acquire_conn(self):
        """
        Acquire a raw asyncpg connection.

        Prefer this over the `transaction()` wrapper when you need a real single-connection
        transaction boundary (e.g. advisory locks).
        """
        if not self.pool:
            await self.connect()
        # If pool was temporarily replaced by a wrapper (rare), we can't acquire.
        if not hasattr(self.pool, "acquire"):
            yield self.pool
            return
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self):
        """
        Context manager for transactions

        Usage:
            async with db.transaction():
                await db.execute("INSERT ...")
                await db.execute("UPDATE ...")
        """
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Temporarily create a wrapper that uses this connection
                old_pool = self.pool

                # Create a simple wrapper for execute/fetchone/fetchall that uses conn
                class ConnectionWrapper:
                    @staticmethod
                    async def execute(query, *params):
                        return await conn.execute(query, *params)

                    @staticmethod
                    async def fetchone(query, *params):
                        row = await conn.fetchrow(query, *params)
                        return dict(row) if row else None

                    @staticmethod
                    async def fetchall(query, *params):
                        rows = await conn.fetch(query, *params)
                        return [dict(row) for row in rows]

                # Temporarily replace pool with connection wrapper
                self.pool = ConnectionWrapper()
                try:
                    yield
                finally:
                    self.pool = old_pool

    async def close(self):
        """Close all connections in pool"""
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("PostgreSQL connection pool closed")

    async def health_check(self) -> bool:
        """
        Check database health

        Returns:
            True if healthy, False otherwise
        """
        try:
            result = await self.fetchone("SELECT 1 AS health")
            return result is not None and result.get('health') == 1
        except Exception as e:
            logger.error("PostgreSQL health check failed", error=str(e))
            return False
