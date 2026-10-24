# PostgreSQL Schema Audit & Fixes

**Date:** 2025-10-16
**Status:** Complete

## Summary

Conducted comprehensive audit of PostgreSQL schema usage across the codebase to identify and fix mismatches between database DDL and actual code usage.

## Issues Found & Fixed

### 1. AsyncPG Migration Issues (CRITICAL - FIXED)
**Files Affected:** 6 files
**Issue:** Code using deprecated `.fetch()` method after AsyncPG migration

**Fixes Applied:**
- ✅ `services/decision/providers.py:156` - Changed `pg.fetch()` → `pg.fetchall()`
- ✅ `services/strategy/engine.py:138` - Changed `pg.fetch()` → `pg.fetchall()`
- ✅ `services/strategy/engine.py:184` - Changed `pg.fetch()` → `pg.fetchall()`
- ✅ `services/execution/trading_mode.py:339` - Changed `pg.fetch()` → `pg.fetchall()`
- ✅ `services/compliance/eod_reconciliation.py:352` - Changed `pg.fetch()` → `pg.fetchall()`
- ✅ `services/compliance/eod_reconciliation.py:385` - Changed `pg.fetch()` → `pg.fetchall()`

### 2. Schema Column Mismatches (HIGH PRIORITY - FIXED)

#### 2a. Missing `filled_qty` Column
**Schema:** `core.core_order` table does NOT have a `filled_qty` column
**Location:** `services/decision/providers.py:158`

**Before:**
```sql
SELECT strategy_id, symbol, side, SUM(qty - filled_qty) as open_qty
FROM core_order
WHERE status IN ('PENDING', 'SUBMITTED', 'PARTIAL')
```

**After:**
```sql
SELECT strategy_id, symbol, side, SUM(qty) as open_qty
FROM core.core_order
WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL')
```

**Note:** `filled_qty` must be calculated from `core.core_fill` table, not stored in `core.core_order`

#### 2b. Wrong Column Name: `status` → `state`
**Schema:** Column is named `state`, not `status` in `core.core_order`
**Locations:**
- `services/decision/providers.py:160`
- `services/strategy/engine.py:188`

**Fix:** Changed all references from `WHERE status IN` to `WHERE state IN`

#### 2c. Wrong State Value: `'PENDING'` → `'NEW'`
**Schema:** Valid states are: NEW, SUBMITTED, PARTIAL, FILLED, CANCELLED, REJECTED
**Locations:**
- `services/decision/providers.py:160`
- `services/strategy/engine.py:188`

**Fix:** Changed `'PENDING'` to `'NEW'` in all WHERE clauses

### 3. Missing Schema Qualification (LOW PRIORITY - FIXED)
**Issue:** Table references missing `core.` schema prefix

**Fixes:**
- ✅ Changed `FROM core_order` → `FROM core.core_order` throughout
- ✅ `services/reconciliation/reconciler.py:128` - Changed `FROM core_order` → `FROM core.core_order`
- ✅ `services/reconciliation/reconciler.py:310` - Changed `UPDATE core_order` → `UPDATE core.core_order`

## Core Order Table Schema Reference

```sql
CREATE TABLE core.core_order (
    order_id uuid PRIMARY KEY,
    strategy_id text NOT NULL,
    symbol text NOT NULL,
    idempotency_key text NOT NULL,
    broker_order_id text,
    ib_perm_id int8,
    order_ref text,
    side text NOT NULL,             -- 'BUY' or 'SELL'
    order_type text NOT NULL,       -- 'MKT', 'LMT', 'STP_LMT', 'STP'
    qty numeric(18, 6) NOT NULL,
    limit_px numeric(18, 6),
    stop_px numeric(18, 6),
    tif text,
    state text NOT NULL,            -- 'NEW', 'SUBMITTED', 'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED'
    parent_order_id uuid,
    oca_group text,
    note text,
    created_at timestamptz DEFAULT now() NOT NULL,
    submitted_at timestamptz,
    updated_at timestamptz DEFAULT now() NOT NULL
);
```

**Key Points:**
- ❌ NO `filled_qty` column - must calculate from `core.core_fill`
- ❌ NO `avg_fill_px` column - must calculate from `core.core_fill`
- ✅ Column name is `state`, NOT `status`
- ✅ Valid states: NEW (not PENDING), SUBMITTED, PARTIAL, FILLED, CANCELLED, REJECTED

## Files with Correct filled_qty Handling

These files correctly calculate `filled_qty` from `core.core_fill`:
- ✅ `services/exec/safety_kernel.py` - Calculates via JOIN with core_fill
- ✅ `services/portfolio/ledger.py` - Uses separate fill tracking

## Testing

Run the audit script to verify fixes:
```bash
python scripts/audit_pg_schema.py
```

Expected result: No HIGH priority issues

## Prevention

1. **Schema Documentation:** Keep `/db/pg/full_core_ddl.sql` up to date
2. **Audit Tool:** Run `scripts/audit_pg_schema.py` before deployments
3. **Code Review:** Check all SQL queries reference correct columns and states
4. **Migration Checklist:** When changing DB schema, grep for all column references

## Related Files

- Audit script: `scripts/audit_pg_schema.py`
- Schema DDL: `db/pg/full_core_ddl.sql`
- AsyncPG migration: `ASYNCPG_MIGRATION_COMPLETE.md`
