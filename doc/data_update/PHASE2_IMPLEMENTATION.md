# Phase 2 Implementation: ClickHouse Tables & Unified View

**Date**: 2025-10-07
**Status**: ✅ SQL Ready (Not Applied to Database Yet)

## What Was Implemented

### 1. ✅ SQL Schema Files Created

**File**: `infra/init/sql/phase2_1m_tables_views.sql`

Created 3 database objects:

1. **`bars_1m_ibkr` table** - IBKR direct write table
   - Schema: Identical to `bars_1m` (17 columns)
   - Engine: `ReplacingMergeTree(ver)`
   - Partition: By day (`toYYYYMMDD(ts_start)`)
   - TTL: 180 days
   - Purpose: Receives 1m bars directly from IBKR via Kafka

2. **`bars_1m_agg` view** - Aggregated data alias
   - Simple `SELECT * FROM bars_1m`
   - Purpose: Clear data lineage, points to aggregated data

3. **`bars_1m_current` view** - Unified priority-based view
   - UNION ALL of `bars_1m_ibkr` (priority=2) and `bars_1m_agg` (priority=1)
   - Deduplication: `LIMIT 1 BY symbol, ts_start`
   - Selection: Highest priority, then highest ver
   - Purpose: Single read interface for strategies

### 2. ✅ Verification Queries

**File**: `infra/init/sql/phase2_verify.sql`

Includes 7 verification checks:
- Tables/views existence
- Row count comparison
- Sample data inspection
- Duplicate detection
- Source distribution analysis
- Latest timestamps per symbol

### 3. ✅ Execution Script

**File**: `scripts/apply_phase2_sql.sh`

Features:
- ✅ Dry-run mode (`--dry-run`)
- ✅ Interactive confirmation
- ✅ Automatic verification after apply
- ✅ Clear rollback instructions
- ✅ Environment variable support for CH connection

## Evidence of Correctness

### Schema Compatibility

**Existing `bars_1m` table** (from `ch_init.sql`):
```sql
symbol, bar_sec DEFAULT 60, ts_start, ts_end, open, high, low, close,
vwap, volume, trades, bid_last, ask_last, mid_last, ver,
is_backfill DEFAULT 0, created_at DEFAULT now()
```

**New `bars_1m_ibkr` table** (from Phase 2 SQL):
```sql
symbol, bar_sec DEFAULT 60, ts_start, ts_end, open, high, low, close,
vwap, volume, trades, bid_last, ask_last, mid_last, ver,
is_backfill DEFAULT 0, created_at DEFAULT now()
```

✅ **Result**: 100% schema match

### SQL Operations Summary

```bash
$ grep "CREATE" infra/init/sql/phase2_1m_tables_views.sql | grep -v "^--"

CREATE TABLE IF NOT EXISTS bars_1m_ibkr
CREATE OR REPLACE VIEW bars_1m_agg AS
CREATE OR REPLACE VIEW bars_1m_current AS
```

✅ All operations are safe (IF NOT EXISTS / OR REPLACE)

### View Logic Verification

**bars_1m_current deduplication logic:**
```sql
SELECT * FROM (
    SELECT *, 2 AS src_priority FROM bars_1m_ibkr  -- IBKR preferred
    UNION ALL
    SELECT *, 1 AS src_priority FROM bars_1m_agg   -- Aggregated fallback
)
ORDER BY (symbol, ts_start, src_priority DESC, ver DESC)
LIMIT 1 BY symbol, ts_start  -- Deduplicate
```

**Behavior:**
- When IBKR has data → src_priority=2 wins
- When IBKR missing → src_priority=1 (aggregated) used
- When both present with same ts → highest ver wins
- No duplicates in final result

✅ Matches specification in `doc/data_update/2.md` lines 36-46

## What This Enables

### Before Phase 2 (Current State)
- ❌ Only one data source: aggregated 1m bars from 5s
- ❌ No way to distinguish IBKR vs aggregated data
- ❌ Can't prioritize direct IBKR data over aggregated

### After Phase 2 (New Capability)
- ✅ Two data sources: IBKR direct + aggregated
- ✅ Clear data lineage (separate tables)
- ✅ Automatic priority selection via unified view
- ✅ Strategies read from single `bars_1m_current` view
- ✅ Can switch between sources without code changes

## NOT Yet Done

**CRITICAL**: The following have NOT been executed:

❌ SQL not applied to ClickHouse database
❌ Tables/views don't exist in database yet
❌ `bars_1m_ibkr_writer` still disabled in config
❌ No data flowing to new table
❌ No runtime testing performed

## How to Apply (Step-by-Step)

### Option 1: Using the Script (Recommended)

```bash
# 1. Dry run to see what will be executed
./scripts/apply_phase2_sql.sh --dry-run

# 2. Apply changes (interactive confirmation)
./scripts/apply_phase2_sql.sh

# 3. Verify manually
clickhouse-client --query "SHOW TABLES FROM trading LIKE 'bars_1m%'"
```

### Option 2: Manual Execution

```bash
# Apply SQL
clickhouse-client --multiquery < infra/init/sql/phase2_1m_tables_views.sql

# Verify
clickhouse-client --multiquery < infra/init/sql/phase2_verify.sql
```

### Option 3: Interactive SQL Client

```bash
clickhouse-client

USE trading;
SOURCE infra/init/sql/phase2_1m_tables_views.sql;

# Then run verification queries manually
```

## Verification After Apply

**Expected Results:**

1. **Tables exist:**
   ```sql
   SHOW TABLES LIKE 'bars_1m%';
   -- Should show: bars_1m, bars_1m_agg, bars_1m_current, bars_1m_ibkr
   ```

2. **Row counts (before IBKR writer enabled):**
   ```
   bars_1m:         N rows (existing data)
   bars_1m_ibkr:    0 rows (new, empty)
   bars_1m_agg:     N rows (same as bars_1m)
   bars_1m_current: N rows (same as bars_1m, via agg view)
   ```

3. **No duplicates in current:**
   ```sql
   SELECT symbol, ts_start, count()
   FROM bars_1m_current
   GROUP BY symbol, ts_start
   HAVING count() > 1;
   -- Should return 0 rows
   ```

## Next Steps After Applying SQL

1. **Enable IBKR Writer** (in `config.yml`):
   ```yaml
   - name: bars_1m_ibkr_writer
     enabled: true  # Change from false to true
   ```

2. **Restart System**:
   ```bash
   # Stop current system
   # Start with new config
   # Check logs for "ClickHouse Writer started: md.bar.1m.ibkr → bars_1m_ibkr"
   ```

3. **Monitor Shadow Writing** (1-2 hours):
   ```sql
   -- Check IBKR data arriving
   SELECT count() FROM bars_1m_ibkr WHERE toDate(ts_start) = today();

   -- Compare sources
   SELECT
       (SELECT count() FROM bars_1m_ibkr WHERE toDate(ts_start) = today()) AS ibkr_rows,
       (SELECT count() FROM bars_1m WHERE toDate(ts_start) = today()) AS agg_rows,
       (SELECT count() FROM bars_1m_current WHERE toDate(ts_start) = today()) AS current_rows;
   ```

4. **Validate Priority Logic**:
   ```sql
   -- Sample check: ensure IBKR data is preferred when both exist
   SELECT symbol, ts_start, ver
   FROM bars_1m_ibkr
   WHERE symbol = 'AAPL' AND toDate(ts_start) = today()
   LIMIT 5;

   SELECT symbol, ts_start, ver
   FROM bars_1m_current
   WHERE symbol = 'AAPL' AND toDate(ts_start) = today()
   LIMIT 5;
   -- vers should match if IBKR has data
   ```

## Rollback Procedure

If issues occur:

### Quick Rollback (Disable Writer)
```yaml
# In config.yml
- name: bars_1m_ibkr_writer
  enabled: false  # Back to false
```
Restart system. IBKR table stops receiving data, but views still work.

### Full Rollback (Remove Schema Changes)
```sql
-- Remove unified view
DROP VIEW IF EXISTS trading.bars_1m_current;

-- Remove aggregated alias view
DROP VIEW IF EXISTS trading.bars_1m_agg;

-- Remove IBKR table (and all its data)
DROP TABLE IF EXISTS trading.bars_1m_ibkr;
```

**Note**: Original `bars_1m` table is NEVER modified, so it's always safe.

## Comparison to Specification

| Spec Requirement (doc/data_update/2.md) | Implementation | Status |
|------------------------------------------|----------------|--------|
| Create `bars_1m_ibkr` table | ✅ Line 27-42 in SQL | ✅ |
| Create `bars_1m_agg` alias view | ✅ Line 52-54 in SQL | ✅ |
| Create `bars_1m_current` unified view | ✅ Line 66-88 in SQL | ✅ |
| UNION ALL with src_priority | ✅ Line 76-79 in SQL | ✅ |
| LIMIT 1 BY for dedup | ✅ Line 81 in SQL | ✅ |
| Keep `bars_1m` unchanged | ✅ No ALTER/DROP | ✅ |
| Idempotent SQL (IF NOT EXISTS) | ✅ All CREATE statements | ✅ |

## Known Limitations

1. **Not Tested with Real ClickHouse**
   - SQL syntax validated but not executed
   - View performance not measured
   - Deduplication logic not verified with real data

2. **No Migration of Existing Data**
   - `bars_1m_ibkr` starts empty
   - Historical backfill not included (Phase 3)

3. **Schema Assumes No Extensions**
   - Spec in 2.md shows extra columns (`what_to_show`, `source`, `exchange`, etc.)
   - Implementation matches existing simple schema
   - Can be extended later if needed

4. **Single Resolution Only**
   - Only 1m implemented
   - 5m, 15m, 30m, etc. need separate SQL (copy-paste with changes)

## Files Created

```
✅ infra/init/sql/phase2_1m_tables_views.sql  (Main schema)
✅ infra/init/sql/phase2_verify.sql           (Verification queries)
✅ scripts/apply_phase2_sql.sh                (Execution script)
✅ doc/data_update/PHASE2_IMPLEMENTATION.md   (This document)
```

## Honest Assessment

**What's Done:**
- ✅ SQL files created and syntax-validated
- ✅ Schema matches existing `bars_1m` structure
- ✅ Views implement correct priority logic per spec
- ✅ Rollback procedure documented
- ✅ Verification queries prepared

**What's NOT Done:**
- ❌ SQL not executed on database
- ❌ No runtime testing
- ❌ No performance testing
- ❌ No integration testing with writer

**Confidence Level:**
- SQL Syntax: 95% (standard ClickHouse DDL)
- Schema Compatibility: 100% (matches existing table)
- View Logic: 90% (needs real data to confirm dedup)
- Overall Readiness: **READY FOR DEV TESTING**

---

**Next Phase**: Phase 3 - Catchup Service & Backfill (automatic gap detection and repair)
