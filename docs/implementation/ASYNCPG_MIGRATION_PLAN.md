# PostgreSQL AsyncPG Migration Plan

## Executive Summary

**Goal**: Migrate from synchronous `psycopg2` to async `asyncpg` to eliminate event loop blocking in the trading system.

**Impact**:
- ✅ Non-blocking database operations
- ✅ Improved latency for order execution (1-5ms reduction per DB call)
- ✅ Better scalability for high-frequency operations
- ✅ Aligns with original system design (doc/system/35.md, 39.md)

**Estimated Time**: 4-6 hours
**Risk Level**: Medium (requires careful testing)
**Rollback Strategy**: Keep psycopg2 in separate branch for quick rollback

---

## Problem Statement

### Current State (psycopg2)

```python
# libs/db/postgres.py - BLOCKS EVENT LOOP
class PostgresDB:
    def execute(self, query: str, params: Optional[tuple] = None):
        with self.get_cursor() as cursor:
            cursor.execute(query, params)  # BLOCKING CALL (1-5ms)
```

**Impact on Trading System:**
```
Timeline of a blocked order placement:
T+0ms:   Order decision made
T+1ms:   pg.execute() called → EVENT LOOP BLOCKED
T+3ms:   Database write completes → EVENT LOOP UNBLOCKED
         During blocking:
         - IB market data updates: QUEUED ❌
         - Kafka messages: QUEUED ❌
         - RuntimeReconciler: DELAYED ❌
         - Other async tasks: STALLED ❌
```

### Target State (asyncpg)

```python
# libs/db/postgres.py - NON-BLOCKING
class PostgresDB:
    async def execute(self, query: str, *params):
        async with self.pool.acquire() as conn:
            await conn.execute(query, *params)  # NON-BLOCKING
```

**Benefits:**
- Event loop remains responsive during DB operations
- Better latency for order placement (critical for HFT)
- Matches original async design intent

---

## Migration Plan

### Phase 1: Preparation (30 minutes)

#### 1.1 Create Backup Branch
```bash
cd .
git checkout -b backup/before-asyncpg-migration
git push origin backup/before-asyncpg-migration
git checkout -b feature/asyncpg-migration
```

#### 1.2 Update Dependencies

**File**: `requirements.txt`
```diff
# Database drivers
-psycopg2-binary>=2.9.10
+asyncpg>=0.29.0
 clickhouse-connect>=0.7.0
 redis>=5.0.0
```

**Install:**
```bash
source venv/bin/activate
pip install asyncpg>=0.29.0
pip uninstall psycopg2-binary  # Remove old driver
pip freeze > requirements.txt
```

#### 1.3 Review Affected Files

Files that use PostgresDB:
```bash
grep -r "PostgresDB" --include="*.py" | cut -d: -f1 | sort -u
```

Expected files:
- `libs/db/postgres.py` (core rewrite)
- `services/exec/execution_service.py`
- `services/reconciliation/runtime_reconciler.py`
- `services/decision/__init__.py`
- `run_system.py`
- Any test files

---

### Phase 2: Core Database Class Rewrite (1 hour)

#### 2.1 New PostgresDB Implementation

**File**: `libs/db/postgres.py`

```python
"""
PostgreSQL database connection using asyncpg
Async connection pool for non-blocking database operations
"""
import asyncpg
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager

from ..config import config
from ..logger import get_logger

logger = get_logger(__name__)


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
        if self.pool is not None:
            logger.warning("Connection pool already exists")
            return

        # Convert config format from psycopg2 to asyncpg
        dsn = (
            f"postgresql://{self._pg_config['user']}:{self._pg_config['password']}"
            f"@{self._pg_config['host']}:{self._pg_config['port']}"
            f"/{self._pg_config['database']}"
        )

        self.pool = await asyncpg.create_pool(
            dsn,
            min_size=self.min_conn,
            max_size=self.max_conn,
            command_timeout=60
        )

        logger.info("PostgreSQL connection pool created",
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

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return dict(row) if row else None

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
                # Temporarily set pool to single connection for transaction
                old_pool = self.pool
                self.pool = conn
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
```

#### 2.2 Key Changes from psycopg2

| Aspect | psycopg2 | asyncpg |
|--------|----------|---------|
| **Parameter placeholders** | `%s` | `$1, $2, ...` |
| **Connection pool** | `SimpleConnectionPool` | `asyncpg.create_pool()` |
| **Methods** | Synchronous | Async (`async def` + `await`) |
| **Cursor** | Required | Not needed |
| **Row format** | `RealDictCursor` for dicts | `Record` (dict-like) |
| **Params format** | `tuple` | `*args` |

**Migration Examples:**

```python
# OLD (psycopg2)
db.execute("UPDATE table SET x=%s WHERE id=%s", (value, id))

# NEW (asyncpg)
await db.execute("UPDATE table SET x=$1 WHERE id=$2", value, id)
```

---

### Phase 3: Update Service Code (2 hours)

#### 3.1 RuntimeReconciler

**File**: `services/reconciliation/runtime_reconciler.py`

**Changes:**
```python
# ALREADY FIXED (keep these changes):
async def _fetch_db_open_orders(self) -> List[Dict[str, Any]]:
    try:
        # Changed from: rows = self.pg.fetchall(...)
        rows = await self.pg.fetchall(  # ADD await
            """
            SELECT order_id, broker_order_id, strategy_id, symbol,
                   side, qty, state, idempotency_key,
                   created_at, updated_at, note
            FROM core.core_order
            WHERE state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')
              AND (note IS NULL OR note NOT LIKE '%GHOST%')
            ORDER BY created_at DESC
            """
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Failed to fetch DB open orders", error=str(e))
        return []

async def _cleanup_ghosts(self, ghosts: List[Dict[str, Any]]) -> None:
    import json
    for ghost in ghosts:
        try:
            note_data = json.dumps({
                "ghost_detected": True,
                "detected_at": str(ghost.get('updated_at')),
                "original_state": ghost.get('state')
            })

            # Changed from: self.pg.execute(...)
            await self.pg.execute(  # ADD await
                """
                UPDATE core.core_order
                SET state = 'CANCELLED',
                    note = $1,
                    updated_at = now()
                WHERE order_id = $2
                """,
                note_data,
                ghost['order_id']
            )
            # ... rest of method
```

#### 3.2 ExecutionService

**File**: `services/exec/execution_service.py`

**Changes:**
```python
async def _persist_order_new(
    self,
    decision_id: str,
    strategy_id: str,
    symbol: str,
    plan: ExecPlan,
    idempotency_key: str
) -> None:
    try:
        import json
        note_data = json.dumps({"decision_id": decision_id})

        # Changed from: self.pg.execute(...)
        await self.pg.execute(  # ADD await
            """
            INSERT INTO core.core_order (
                order_id, strategy_id, symbol, idempotency_key,
                side, order_type, qty, limit_px, stop_px, tif,
                state, note, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), $1, $2, $3,
                $4, $5, $6, $7, $8, $9,
                'NEW', $10, now(), now()
            )
            """,
            strategy_id, symbol, idempotency_key,
            plan.side, plan.order_type, plan.qty,
            plan.limit_px, plan.stop_px, plan.tif,
            note_data
        )
        # ... rest of method

async def _update_order_submitted(
    self,
    idempotency_key: str,
    order_id: int,
    broker_order_id: Optional[str],
    perm_id: Optional[int],
    order_ref: str
) -> None:
    try:
        # Changed from: self.pg.execute(...)
        await self.pg.execute(  # ADD await
            """
            UPDATE core.core_order
            SET broker_order_id = $1,
                ib_perm_id = $2,
                order_ref = $3,
                state = 'SUBMITTED',
                submitted_at = now(),
                updated_at = now()
            WHERE idempotency_key = $4
            """,
            str(order_id), perm_id, order_ref, idempotency_key
        )
        # ... rest of method

async def _update_order_rejected(
    self,
    idempotency_key: str,
    reason: str
) -> None:
    try:
        import json
        note_data = json.dumps({"reject_reason": reason})

        # Changed from: self.pg.execute(...)
        await self.pg.execute(  # ADD await
            """
            UPDATE core.core_order
            SET state = 'REJECTED',
                note = $1,
                updated_at = now()
            WHERE idempotency_key = $2
            """,
            note_data, idempotency_key
        )
        # ... rest of method
```

#### 3.3 Decision Pipeline Services

**File**: `services/decision/__init__.py`

Search for all PostgresDB usage and add `await`:

```bash
# Find all pg.execute/fetchone/fetchall calls
grep -n "self\.pg\.\|pg\.\(execute\|fetchone\|fetchall\)" services/decision/*.py
```

Add `await` to each call and ensure containing function is `async def`.

#### 3.4 run_system.py

**Changes needed:**

```python
# Initialize PostgresDB
pg = PostgresDB()
await pg.connect()  # ADD THIS - connect pool at startup

# ... later in initialization

# Test database connection
if not await pg.health_check():  # ADD await
    logger.error("PostgreSQL health check failed")
else:
    logger.info("PostgreSQL connection verified")
```

**In shutdown:**
```python
async def stop(self):
    # ... other cleanup

    # Close PostgreSQL pool
    if hasattr(self, 'pg') and self.pg:
        await self.pg.close()  # ADD await
        logger.info("PostgreSQL pool closed")
```

---

### Phase 4: Testing Strategy (1 hour)

#### 4.1 Unit Tests

Create test file: `tests/test_postgres_asyncpg.py`

```python
"""
Test asyncpg PostgreSQL implementation
"""
import pytest
import asyncio
from libs.db.postgres import PostgresDB


@pytest.mark.asyncio
async def test_connection_pool():
    """Test connection pool creation"""
    db = PostgresDB(min_conn=1, max_conn=5)
    await db.connect()

    # Verify pool exists
    assert db.pool is not None

    # Health check
    assert await db.health_check() is True

    await db.close()


@pytest.mark.asyncio
async def test_fetchone():
    """Test fetchone query"""
    db = PostgresDB()
    await db.connect()

    result = await db.fetchone("SELECT 1 AS test")
    assert result is not None
    assert result['test'] == 1

    await db.close()


@pytest.mark.asyncio
async def test_fetchall():
    """Test fetchall query"""
    db = PostgresDB()
    await db.connect()

    results = await db.fetchall("SELECT generate_series(1, 5) AS num")
    assert len(results) == 5
    assert results[0]['num'] == 1
    assert results[4]['num'] == 5

    await db.close()


@pytest.mark.asyncio
async def test_execute():
    """Test execute with INSERT/UPDATE"""
    db = PostgresDB()
    await db.connect()

    # Create temp table
    await db.execute("""
        CREATE TEMP TABLE test_table (
            id SERIAL PRIMARY KEY,
            name TEXT
        )
    """)

    # Insert
    await db.execute(
        "INSERT INTO test_table (name) VALUES ($1)",
        "test_name"
    )

    # Fetch
    result = await db.fetchone("SELECT name FROM test_table WHERE id = 1")
    assert result['name'] == "test_name"

    await db.close()


@pytest.mark.asyncio
async def test_transaction():
    """Test transaction context manager"""
    db = PostgresDB()
    await db.connect()

    # Create temp table
    await db.execute("""
        CREATE TEMP TABLE test_txn (
            id SERIAL PRIMARY KEY,
            value INT
        )
    """)

    # Transaction that commits
    async with db.transaction():
        await db.execute("INSERT INTO test_txn (value) VALUES ($1)", 100)
        await db.execute("INSERT INTO test_txn (value) VALUES ($1)", 200)

    results = await db.fetchall("SELECT value FROM test_txn ORDER BY id")
    assert len(results) == 2

    # Transaction that rolls back
    try:
        async with db.transaction():
            await db.execute("INSERT INTO test_txn (value) VALUES ($1)", 300)
            raise Exception("Rollback test")
    except:
        pass

    results = await db.fetchall("SELECT value FROM test_txn ORDER BY id")
    assert len(results) == 2  # Still only 2 rows

    await db.close()


@pytest.mark.asyncio
async def test_concurrent_queries():
    """Test concurrent database access"""
    db = PostgresDB(min_conn=2, max_conn=5)
    await db.connect()

    async def query_task(n):
        result = await db.fetchone("SELECT $1 AS num", n)
        return result['num']

    # Run 10 concurrent queries
    tasks = [query_task(i) for i in range(10)]
    results = await asyncio.gather(*tasks)

    assert results == list(range(10))

    await db.close()
```

**Run tests:**
```bash
pytest tests/test_postgres_asyncpg.py -v
```

#### 4.2 Integration Tests

**Test RuntimeReconciler:**
```bash
# Start system in test mode
TRADING_MODE=shadow python run_system.py

# Watch for errors in RuntimeReconciler
tail -f logs/trader.log | grep -i "reconcil\|postgres"
```

**Test ExecutionService:**
```bash
# Place a test order (shadow mode)
# Monitor logs for DB operations
tail -f logs/trader.log | grep -i "execution\|persist\|database"
```

#### 4.3 Performance Testing

Create: `tests/test_postgres_performance.py`

```python
"""
Compare psycopg2 vs asyncpg performance
"""
import asyncio
import time
from libs.db.postgres import PostgresDB


async def benchmark_queries(db: PostgresDB, num_queries: int = 100):
    """Benchmark query performance"""
    start = time.time()

    tasks = []
    for i in range(num_queries):
        tasks.append(db.fetchone("SELECT $1 AS num", i))

    await asyncio.gather(*tasks)

    elapsed = time.time() - start
    qps = num_queries / elapsed

    print(f"Completed {num_queries} queries in {elapsed:.2f}s")
    print(f"Queries per second: {qps:.0f}")
    print(f"Average latency: {elapsed/num_queries*1000:.2f}ms")


async def main():
    db = PostgresDB(min_conn=5, max_conn=10)
    await db.connect()

    print("=== asyncpg Performance Test ===")
    await benchmark_queries(db, 100)
    await benchmark_queries(db, 500)
    await benchmark_queries(db, 1000)

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Results:**
- asyncpg should show ~2-5x better throughput than psycopg2
- Concurrent queries should not block each other

---

### Phase 5: Migration Execution (1 hour)

#### 5.1 Pre-Migration Checklist

- [ ] All code changes committed to `feature/asyncpg-migration` branch
- [ ] Backup branch created (`backup/before-asyncpg-migration`)
- [ ] Unit tests written and passing
- [ ] Integration test plan ready
- [ ] Team notified (if applicable)

#### 5.2 Execute Migration

```bash
# 1. Stop running system
pkill -f "python run_system.py"

# 2. Checkout migration branch
git checkout feature/asyncpg-migration

# 3. Install new dependencies
source venv/bin/activate
pip install asyncpg>=0.29.0
pip uninstall psycopg2-binary -y

# 4. Run tests
pytest tests/test_postgres_asyncpg.py -v

# 5. Start system in test mode
TRADING_MODE=shadow python run_system.py

# 6. Monitor logs for 5-10 minutes
tail -f logs/trader.log
```

#### 5.3 Verification Checklist

- [ ] System starts without errors
- [ ] PostgreSQL connection pool created successfully
- [ ] RuntimeReconciler runs without errors
- [ ] ExecutionService (if enabled) works correctly
- [ ] No "blocking" warnings in logs
- [ ] Database queries complete successfully
- [ ] Performance improved (check logs for query times)

#### 5.4 Rollback Plan (If Needed)

```bash
# Stop system
pkill -f "python run_system.py"

# Checkout backup branch
git checkout backup/before-asyncpg-migration

# Reinstall psycopg2
pip install psycopg2-binary>=2.9.10
pip uninstall asyncpg -y

# Restart system
python run_system.py
```

---

### Phase 6: Post-Migration (30 minutes)

#### 6.1 Monitor Production

**Watch for:**
- Database connection pool exhaustion
- Slow queries (should be faster now)
- Event loop responsiveness (should improve)
- Memory usage (asyncpg is more efficient)

**Metrics to track:**
```python
# Add to observability
recon_db_query_latency = Histogram(
    'postgres_query_latency_seconds',
    'PostgreSQL query latency',
    ['operation']
)

# Usage
with recon_db_query_latency.labels(operation='fetchall').time():
    rows = await pg.fetchall(...)
```

#### 6.2 Update Documentation

Create: `doc/system/ASYNCPG_MIGRATION.md`

Document:
- Why we migrated
- What changed
- How to use new API
- Performance improvements observed

#### 6.3 Merge to Main

```bash
# After 24-48 hours of stable operation
git checkout main
git merge feature/asyncpg-migration
git push origin main

# Tag the migration
git tag -a v1.0-asyncpg -m "Migrated to asyncpg for non-blocking DB"
git push origin v1.0-asyncpg
```

---

## Common Issues and Solutions

### Issue 1: "RuntimeError: Event loop is closed"

**Cause**: Trying to use database after event loop shutdown

**Solution**: Ensure `await db.close()` in shutdown handler

```python
async def stop(self):
    # Close DB pool BEFORE closing event loop
    if self.pg:
        await self.pg.close()
```

### Issue 2: "Connection pool exhausted"

**Cause**: Too many concurrent queries, not enough connections

**Solution**: Increase pool size

```python
pg = PostgresDB(min_conn=5, max_conn=20)  # Increase max_conn
```

### Issue 3: "asyncpg.exceptions.UndefinedColumnError"

**Cause**: Query syntax error or missing column

**Solution**: Check query - asyncpg provides better error messages than psycopg2

### Issue 4: "$1 placeholder not working"

**Cause**: Using old `%s` syntax

**Solution**: Replace all `%s` with `$1, $2, ...`

```python
# OLD
query = "SELECT * FROM table WHERE id=%s"
await db.execute(query, (id,))

# NEW
query = "SELECT * FROM table WHERE id=$1"
await db.execute(query, id)
```

---

## Performance Expectations

### Before (psycopg2)

```
Order Placement Timeline:
T+0ms:  Decision made
T+1ms:  pg.execute() called → BLOCKS
T+4ms:  DB write completes → UNBLOCKS
T+5ms:  IB order placed

Total: 5ms (3ms blocked)
```

### After (asyncpg)

```
Order Placement Timeline:
T+0ms:  Decision made
T+1ms:  await pg.execute() → NON-BLOCKING
T+1ms:  IB order placed (concurrent with DB)
T+4ms:  DB write completes

Total: 4ms (0ms blocked, parallel execution)
```

**Expected Improvements:**
- Order latency: 20-30% reduction
- RuntimeReconciler: No more multi-second blocks
- Event loop responsiveness: Smooth, no stuttering
- Throughput: 2-5x increase in concurrent operations

---

## Success Criteria

✅ **Migration is successful when:**

1. System starts without errors
2. All database operations use `await`
3. No event loop blocking observed
4. RuntimeReconciler runs smoothly
5. ExecutionService places orders successfully
6. Performance metrics show improvement
7. No rollbacks needed after 48 hours

---

## Timeline Summary

| Phase | Duration | Status |
|-------|----------|--------|
| 1. Preparation | 30 min | ⏳ Pending |
| 2. Core Rewrite | 1 hour | ⏳ Pending |
| 3. Service Updates | 2 hours | ⏳ Pending |
| 4. Testing | 1 hour | ⏳ Pending |
| 5. Execution | 1 hour | ⏳ Pending |
| 6. Post-Migration | 30 min | ⏳ Pending |
| **Total** | **6 hours** | |

---

## Next Steps

1. **Review this plan** - Understand all changes
2. **Create backup** - `git checkout -b backup/before-asyncpg-migration`
3. **Start Phase 1** - Update dependencies
4. **Execute migration** - Follow phases 2-6
5. **Monitor production** - Track performance improvements

---

**Migration Author**: Claude Code
**Date**: 2025-10-15
**System**: Trading System v7.2
**Python Version**: 3.13
**PostgreSQL Version**: 15.14
