"""
Database connection modules
"""
from .clickhouse import ClickHouseDB
from .redis_client import RedisClient

try:
    from .postgres import PostgresDB
except ModuleNotFoundError as exc:  # pragma: no cover
    class PostgresDB:  # type: ignore
        def __init__(self, *args, **kwargs):
            raise ModuleNotFoundError(
                "Missing optional dependency for PostgresDB. Install `asyncpg` (or the repo requirements) to use Postgres."
            ) from exc

__all__ = ["PostgresDB", "ClickHouseDB", "RedisClient"]
