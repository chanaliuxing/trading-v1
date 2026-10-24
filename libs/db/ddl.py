"""
ClickHouse DDL helpers

Provides idempotent schema initialization for SFS components.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from .ch_pool import get_client
from ..logger import get_logger

logger = get_logger(__name__)


def _split_sql(sql: str) -> List[str]:
    """Very simple splitter for SQL statements by semicolon.

    Note: This will not handle complex cases with semicolons in strings.
    Our schema files are simple DDLs so this is sufficient.
    """
    parts = []
    buf = []
    for line in sql.splitlines():
        s = line.strip()
        if not s:
            buf.append(line)
            continue
        buf.append(line)
        if s.endswith(";"):
            parts.append("\n".join(buf).rstrip(";\n "))
            buf = []
    if buf:
        parts.append("\n".join(buf))
    return [p for p in parts if p.strip()]


def ensure_sfs_schema() -> None:
    """Ensure ClickHouse SFS schema exists (database + tables/views).

    Executes DDLs idempotently for:
      - schema/clickhouse/sfs_5s.sql
      - schema/clickhouse/sfs_1m_tumble.sql
    """
    ddl_files = [
        Path("schema/clickhouse/sfs_5s.sql"),
        Path("schema/clickhouse/sfs_1m_tumble.sql"),
    ]

    ch = get_client()
    for ddl_path in ddl_files:
        if not ddl_path.exists():
            logger.warning("SFS DDL file not found", path=str(ddl_path))
            continue
        sql = ddl_path.read_text(encoding="utf-8")
        statements = _split_sql(sql)
        for stmt in statements:
            try:
                ch.command(stmt)
            except Exception as exc:  # noqa: BLE001
                # Ignore benign 'already exists' errors, log others
                msg = str(exc)
                if "already exists" in msg or "Code: 57" in msg:
                    logger.debug("DDL already applied", sql=stmt[:80])
                    continue
                logger.error("SFS DDL execution failed", error=msg, sql=stmt)
                # Continue to next statement to be resilient
