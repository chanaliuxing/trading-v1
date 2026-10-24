"""
Global shared ClickHouse connection pool for all services.

This module creates a single, large urllib3 connection pool that is shared
across all ClickHouse clients in the application. This solves the
"Connection pool is full" warning that occurs when 25+ services each create
their own small connection pools.

Usage:
    from libs.db.ch_pool import get_client, get_async_client

    # Synchronous client
    client = get_client()
    result = client.query("SELECT 1")

    # Asynchronous client
    async_client = await get_async_client()
    result = await async_client.query("SELECT 1")
"""
import os
import threading
import urllib3
import clickhouse_connect
from typing import Any, Optional

from ..config import config
from ..logger import get_logger

logger = get_logger(__name__)
_THREAD_LOCAL = threading.local()

# =============================================================================
# Global Shared Connection Pool
# =============================================================================
# Create a single, large urllib3 PoolManager that will be shared by ALL
# ClickHouse clients in the application.
#
# Parameters:
#   maxsize: Maximum connections per host (128 is enough for 25+ services)
#   num_pools: Number of connection pools to cache (64 is conservative)
#   block: When True, requests wait instead of being discarded when pool is full
#
# This pool is created once at module import time and reused by all clients.
# =============================================================================

GLOBAL_POOL = urllib3.PoolManager(
    maxsize=128,
    num_pools=64,
    block=True
)

logger.info(
    "Global ClickHouse connection pool created",
    maxsize=128,
    num_pools=64,
    block=True
)


class ClickHouseClientWrapper:
    """
    Wrapper around clickhouse_connect.Client that provides backward compatibility
    with ClickHouseDB interface (query returns List[Dict] instead of QueryResult).

    NOTE: Each call to get_client() returns a *new* wrapper instance. The
    underlying HTTP connections are still pooled via GLOBAL_POOL, but we avoid
    sharing a single Client/session across threads to prevent
    "concurrent queries within the same session" errors.
    """

    def __init__(self, client, default_settings: Optional[dict] = None):
        self._client = client
        self._default_settings = dict(default_settings or {})
        # Optionally disable query cache globally
        if os.getenv("CH_DISABLE_QUERY_CACHE", "1").lower() not in ("0", "false"):
            self._default_settings["use_query_cache"] = False

    def _merge_settings(self, kwargs):
        # Merge caller settings with defaults (caller wins)
        settings = dict(self._default_settings)
        if "settings" in kwargs and isinstance(kwargs["settings"], dict):
            settings.update(kwargs["settings"])
        kwargs["settings"] = settings
        return kwargs

    def query(self, sql: str, parameters=None, **kwargs):
        """Execute query and return List[Dict[str, Any]] for compatibility.

        Newer versions of clickhouse_connect may return either a raw
        QueryResult (with .named_results()) or a plain list of dict
        rows from .query(). We normalise both into List[Dict].
        """
        kwargs = self._merge_settings(kwargs)
        result = self._client.query(sql, parameters=parameters, **kwargs)
        # Fast path: already a list of dicts
        if isinstance(result, list):
            return result
        # Legacy / QueryResult path
        if hasattr(result, "named_results"):
            return list(result.named_results())
        # Fallback: return as-is; callers that depend on the exact shape
        # can handle it, but the common case remains List[Dict].
        return result

    def execute(self, sql: str, parameters=None, **kwargs):
        """Backward-compatible clickhouse_driver-style execute.

        Legacy callers expect SELECT results as List[Tuple]. Keep query()
        returning List[Dict] for newer shared-pool callers, and expose execute()
        for old code that still indexes rows positionally.
        """
        sql_text = str(sql or "").lstrip()
        is_query = sql_text.upper().startswith(
            ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXISTS")
        )
        kwargs = self._merge_settings(kwargs)
        if is_query:
            result = self._client.query(sql, parameters=parameters, **kwargs)
            if hasattr(result, "result_rows"):
                return list(result.result_rows)
            if isinstance(result, list):
                if not result or not isinstance(result[0], dict):
                    return result
                return [tuple(row.values()) for row in result]
            return result
        return self._client.command(sql, parameters=parameters, **kwargs)

    def command(self, sql: str, parameters=None, **kwargs):
        """Execute command without returning results."""
        kwargs = self._merge_settings(kwargs)
        return self._client.command(sql, parameters=parameters, **kwargs)

    def insert(self, table: str, data, column_names=None):
        """Insert data into table."""
        return self._client.insert(table, data, column_names=column_names)

    def insert_df(self, table: str, df, column_names=None):
        """Insert DataFrame into table."""
        return self._client.insert_df(table, df, column_names=column_names)

    def close(self):
        """Close client (no-op for pooled connections)."""
        # Do not actually close the underlying PoolManager; wrappers are cheap
        # and HTTP connections are managed globally by GLOBAL_POOL.
        pass

    def health_check(self) -> bool:
        """Check database health."""
        try:
            result = self.query("SELECT 1 AS health")
            return len(result) > 0 and result[0].get("health") == 1
        except Exception as e:
            logger.error("ClickHouse health check failed", error=str(e))
            return False

    def __getattr__(self, name):
        """Delegate all other attributes to wrapped client."""
        return getattr(self._client, name)


def get_client(**kwargs):
    """
    Get a synchronous ClickHouse client using the global shared connection pool.

    All services should use this function instead of creating their own
    ClickHouseDB instances. This ensures all clients share the same large
    connection pool, avoiding the "Connection pool is full" warning.

    Args:
        **kwargs: Optional overrides for connection parameters
                  (host, port, database, username, password)

    Returns:
        ClickHouseClientWrapper: A wrapped ClickHouse client with compatible interface

    Example:
        >>> from libs.db.ch_pool import get_client
        >>> client = get_client()
        >>> result = client.query("SELECT count() FROM bars_5s")
    """
    ch_config = config.clickhouse_config

    # Build connection parameters with sensible defaults
    final_kwargs = {
        'host': ch_config.get('host', 'localhost'),
        'port': int(ch_config.get('port', 8123)),
        'database': ch_config.get('database', 'trading'),
        'username': ch_config.get('user', 'default'),
        'password': ch_config.get('password', ''),
        'pool_mgr': GLOBAL_POOL,  # ⭐ Use the shared global pool
        # Keep sync callers from pinning the event loop for minutes when CH degrades.
        'connect_timeout': float(ch_config.get('connect_timeout_sec', 2.0)),
        'send_receive_timeout': float(ch_config.get('send_receive_timeout_sec', 5.0)),
        'query_retries': int(ch_config.get('query_retries', 0)),
        # ClickHouse sessions are single-flight. Strategy runtime performs many
        # concurrent reads; disabling auto sessions avoids SESSION_IS_LOCKED
        # while still reusing the shared HTTP pool.
        'autogenerate_session_id': False,
    }

    # Allow caller to override any parameter
    final_kwargs.update(kwargs)

    client = clickhouse_connect.get_client(**final_kwargs)

    default_settings = {}
    tz = ch_config.get('timezone')
    if tz:
        default_settings["session_timezone"] = tz

    # Wrap client to provide backward compatible interface. Each wrapper gets
    # its own logical client while still sharing the HTTP connection pool.
    return ClickHouseClientWrapper(client, default_settings=default_settings)


def get_ch_client(**kwargs):
    """Backward-compatible alias for older strategy components."""
    return get_client(**kwargs)


def get_threadlocal_client(**kwargs):
    """Return a reusable ClickHouse client scoped to the current worker thread.

    ClickHouse client construction performs server-setting discovery queries.
    Market-data polling runs frequently inside a small ThreadPoolExecutor, so a
    thread-local client keeps those discovery queries off the hot path while
    preserving isolation between concurrent worker threads.
    """
    if kwargs:
        return get_client(**kwargs)
    client = getattr(_THREAD_LOCAL, "client", None)
    if client is None:
        client = get_client()
        _THREAD_LOCAL.client = client
    return client


def _threadsafe_client(client: Any) -> Any:
    """Return a client safe to use inside worker threads.

    Pooled sync clients are session-bound and should not be reused concurrently
    across threads. When we detect the shared wrapper (directly or behind a
    ClickHouseDB-like facade), create a fresh pooled client that shares the
    global HTTP pool but uses its own session.
    """
    if isinstance(client, ClickHouseClientWrapper):
        return get_threadlocal_client()

    wrapped = getattr(client, "client", None)
    if isinstance(wrapped, ClickHouseClientWrapper):
        return get_threadlocal_client()

    return client


def threadsafe_query(client: Any, sql: str, parameters=None, **kwargs):
    """Run a query with a thread-safe client/session."""
    return _threadsafe_client(client).query(sql, parameters=parameters, **kwargs)


def threadsafe_command(client: Any, sql: str, parameters=None, **kwargs):
    """Run a command with a thread-safe client/session."""
    return _threadsafe_client(client).command(sql, parameters=parameters, **kwargs)


def threadsafe_insert(client: Any, table: str, data, column_names=None):
    """Run an insert with a thread-safe client/session."""
    return _threadsafe_client(client).insert(table, data, column_names=column_names)


async def get_async_client(**kwargs):
    """
    Get an asynchronous ClickHouse client using the global shared connection pool.

    Use this for async/await operations. Like get_client(), this shares the
    global connection pool to avoid resource contention.

    Args:
        **kwargs: Optional overrides for connection parameters

    Returns:
        clickhouse_connect.driver.AsyncClient: An async ClickHouse client

    Example:
        >>> from libs.db.ch_pool import get_async_client
        >>> async_client = await get_async_client()
        >>> result = await async_client.query("SELECT count() FROM bars_5s")
    """
    ch_config = config.clickhouse_config

    final_kwargs = {
        'host': ch_config.get('host', 'localhost'),
        'port': int(ch_config.get('port', 8123)),
        'database': ch_config.get('database', 'trading'),
        'username': ch_config.get('user', 'default'),
        'password': ch_config.get('password', ''),
        'pool_mgr': GLOBAL_POOL,  # ⭐ Use the shared global pool
        'connect_timeout': float(ch_config.get('connect_timeout_sec', 2.0)),
        'send_receive_timeout': float(ch_config.get('send_receive_timeout_sec', 5.0)),
    }

    final_kwargs.update(kwargs)

    async_client = await clickhouse_connect.get_async_client(**final_kwargs)

    # Set timezone if configured
    tz = ch_config.get("timezone")
    if tz:
        try:
            await async_client.command(f"SET session_timezone = '{tz}'")
            logger.debug("Async ClickHouse session timezone set", timezone=tz)
        except Exception as exc:
            logger.warning("Failed to set async ClickHouse timezone", timezone=tz, error=str(exc))

    return async_client


# =============================================================================
# Migration Notes
# =============================================================================
# To migrate existing code from ClickHouseDB to the shared pool:
#
# OLD CODE:
#   from libs.db.clickhouse import ClickHouseDB
#   self.ch = ClickHouseDB()
#   result = self.ch.query("SELECT 1")
#
# NEW CODE:
#   from libs.db.ch_pool import get_client
#   self.ch = get_client()
#   result = self.ch.query("SELECT 1")
#
# The API is almost identical. Main differences:
# - No need to call ch.close() (pool is reused)
# - Better performance under high concurrency
# - No more "Connection pool is full" warnings
# =============================================================================
