# Execution System Fix Plan

## Current Status (Honest Assessment)

**Code Status**: ✅ Compiles, imports work
**Database Integration**: ❌ **BROKEN** - Multiple schema mismatches
**Testing**: ❌ **NOT TESTED** - No integration tests run
**Production Ready**: ❌ **NO** - Would fail at runtime

---

## Actual PostgreSQL Schema (from `schema_trading_2025-10-10_20-37-04.sql`)

### `core.core_order` Table:
```sql
CREATE TABLE core.core_order (
    order_id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_id text NOT NULL,
    symbol text NOT NULL,
    idempotency_key text NOT NULL,
    broker_order_id text,                -- IB order ID (int as text)
    ib_perm_id bigint,                  -- IB permanent ID
    order_ref text,                     -- orderRef for reconciliation
    side text NOT NULL,                 -- BUY/SELL
    order_type text NOT NULL,           -- MKT/LMT/STP_LMT/STP
    qty numeric(18,6) NOT NULL,
    limit_px numeric(18,6),
    stop_px numeric(18,6),
    tif text,
    state text NOT NULL,                -- NEW/SUBMITTED/PARTIAL/FILLED/CANCELLED/REJECTED
    parent_order_id uuid,               -- For OCO linkage
    oca_group text,                     -- OCO group
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    submitted_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,

    -- Constraints:
    CHECK (order_type IN ('MKT', 'LMT', 'STP_LMT', 'STP'))
    CHECK (side IN ('BUY', 'SELL'))
    CHECK (state IN ('NEW', 'SUBMITTED', 'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED'))
);
```

**MISSING Columns in schema**:
- ❌ `decision_id` - Not in core_order (it's in shadow_orders)
- ❌ `filled_qty` - Not tracked in core_order
- ❌ `perm_id` - Called `ib_perm_id` instead
- ❌ `reject_reason` - Not in schema
- ❌ `cancel_reason` - Not in schema

**MISSING State**:
- ❌ `STALE_CLOSED` - Not in state constraint (only NEW/SUBMITTED/PARTIAL/FILLED/CANCELLED/REJECTED)

### `core.core_fill` Table:
```sql
CREATE TABLE core.core_fill (
    fill_id uuid DEFAULT gen_random_uuid() NOT NULL,
    order_id uuid NOT NULL,             -- FK to core_order.order_id
    ts timestamp with time zone NOT NULL,
    qty numeric(18,6) NOT NULL,
    price numeric(18,6) NOT NULL,
    liquidity text,
    commission_usd numeric(18,6) DEFAULT 0,
    exchange text,
    exec_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
```

**MISSING Columns**:
- ❌ `symbol` - Not in core_fill
- ❌ `side` - Not in core_fill
- ❌ `order_ref` - Not in core_fill
- ❌ `ts_us` - Called `ts` (timestamptz, not bigint)

### `core.shadow_orders` Table (for shadow mode):
```sql
CREATE TABLE core.shadow_orders (
    shadow_id varchar(50) PRIMARY KEY,
    decision_id varchar(50) NOT NULL,   -- ✓ This is where decision_id lives!
    strategy_id varchar(50) NOT NULL,
    symbol varchar(20) NOT NULL,
    side varchar(10) NOT NULL,
    qty numeric(18,6) NOT NULL,
    order_type varchar(20),
    limit_px numeric(12,4),
    timestamp timestamp NOT NULL,
    would_submit boolean DEFAULT true,
    reason varchar(200),
    created_at timestamp DEFAULT CURRENT_TIMESTAMP
);
```

---

## Original Design Intent (from doc/system/1.md)

The original design mentions:
- **phase4_executor.py**: `TradeLifecycleManager` with order state machine
- **reconciliation.py**: `adopt_live_orders`, `mark_ghost_orders`, `rebuild_context`
- **pg_lock.py**: Advisory locks for atomic operations
- **Stored procedures**: `core_sp_risk_approve_and_reserve`, `core_sp_release_on_terminal`

The design is **database-centric** with:
1. Orders tracked in PostgreSQL from inception
2. Idempotency keys for deduplication
3. Atomic capital reservation/release
4. Reconciliation by orderRef matching

---

## Bugs Found (Schema Mismatches)

### Bug #1: ExecutionService - Wrong table for decision_id
**Location**: `services/exec/execution_service.py:292`
**Issue**: Tries to write `decision_id` to `core_order` table
```python
INSERT INTO core.core_order (
    order_id, decision_id, strategy_id, ...  # ❌ decision_id doesn't exist
)
```
**Fix**: Remove `decision_id` from INSERT. Link via `idempotency_key` instead (or add to `note` field).

---

### Bug #2: ExecutionService - filled_qty column doesn't exist
**Location**: Multiple places
**Issue**: Code tries to update `filled_qty` column
```python
UPDATE core.core_order SET filled_qty = $1 ...  # ❌ Column doesn't exist
```
**Fix**: Track fills in `core_fill` table only. Calculate filled_qty by SUM(core_fill.qty) when needed.

---

### Bug #3: SafetyKernel - Wrong core_fill columns
**Location**: `services/exec/safety_kernel.py:467`
**Issue**: Insert references non-existent columns
```python
INSERT INTO core.core_fill (
    fill_id, order_id, symbol, side, qty, ...  # ❌ symbol, side don't exist
)
```
**Fix**: Remove `symbol`, `side`, `order_ref` from INSERT. Use only: `fill_id`, `order_id`, `ts`, `qty`, `price`, `liquidity`, `commission_usd`, `exchange`, `exec_id`

---

### Bug #4: SafetyKernel - Timestamp column type
**Location**: `services/exec/safety_kernel.py:472`
**Issue**: Tries to convert microseconds to timestamp
```python
to_timestamp($8::bigint / 1000000.0)  # Awkward conversion
```
**Fix**: Pass timestamptz directly, or use `to_timestamp(ts_us / 1000000.0)` correctly

---

### Bug #5: SafetyKernel - perm_id vs ib_perm_id
**Location**: `services/exec/execution_service.py:234`
**Issue**: Column is called `ib_perm_id` not `perm_id`
```python
UPDATE core.core_order SET perm_id = $1 ...  # ❌ Wrong column name
```
**Fix**: Use `ib_perm_id`

---

### Bug #6: SafetyKernel - Missing cancel_reason column
**Location**: `services/exec/safety_kernel.py:598`
**Issue**: Tries to write to non-existent column
```python
UPDATE core.core_order SET cancel_reason = $1 ...  # ❌ Column doesn't exist
```
**Fix**: Use `note` column instead for reasons

---

### Bug #7: RuntimeReconciler - STALE_CLOSED state violates constraint
**Location**: `services/reconciliation/runtime_reconciler.py:309`
**Issue**: Tries to set state to STALE_CLOSED
```python
UPDATE core.core_order SET state = 'STALE_CLOSED' ...  # ❌ Violates CHECK constraint
```
**Fix**: Use `state = 'CANCELLED'` and `note = 'GHOST_DETECTED'` instead

---

### Bug #8: Type mismatches (UUID vs string)
**Location**: Multiple places
**Issue**: Mixing UUID and string types
```python
WHERE broker_order_id = $1  # Passing int, column is text - OK
WHERE order_id = $1  # Passing string, column is UUID - ❌ FAIL
```
**Fix**: Ensure UUIDs are passed as UUID type or cast properly

---

## Fix Plan (Priority Order)

### Phase 1: Schema Alignment (2-3 hours)
**Priority**: P0 - Must fix before anything works

1. ✅ **Read and document actual schema**
2. **Fix ExecutionService database operations**:
   - Remove `decision_id` from `core_order` INSERT
   - Remove `filled_qty` from UPDATE operations
   - Fix `perm_id` → `ib_perm_id`
   - Use `note` field for metadata instead of missing columns

3. **Fix SafetyKernel database operations**:
   - Fix `_persist_fill()` to match core_fill schema
   - Remove symbol/side/order_ref from INSERT
   - Fix timestamp handling (use timestamptz)
   - Fix UUID vs string handling
   - Use `note` field for cancel/reject reasons

4. **Fix RuntimeReconciler state handling**:
   - Change `STALE_CLOSED` → `CANCELLED` with `note='GHOST'`
   - Update state constraint checks

### Phase 2: Missing Functionality (3-4 hours)
**Priority**: P1 - Need for ghost prevention

5. **Add filled quantity tracking**:
   - Create helper function to calculate filled_qty from core_fill table
   - Update on_fill logic to query current filled amount

6. **Add decision_id linkage**:
   - Store decision_id in `note` field as JSON: `{"decision_id": "..."}`
   - Or create mapping table if needed for queries

7. **Add order state transition validation**:
   - Ensure state changes are valid (NEW→SUBMITTED→PARTIAL→FILLED)
   - Prevent invalid transitions

### Phase 3: Integration (2-3 hours)
**Priority**: P1 - Wire components together

8. **Integrate ExecutionService into strategy engine**:
   - Modify DecisionPipeline to call ExecutionService
   - Pass approved results to ExecutionService.execute()

9. **Integrate FillEventHandler with IB adapter**:
   - Subscribe to IB fill events
   - Wire to SafetyKernel.on_fill()

10. **Initialize components in run_system.py**:
    - Create execution service with proper dependencies
    - Start fill handler
    - Start runtime reconciler

### Phase 4: Testing (4-5 hours)
**Priority**: P0 - Must validate before production

11. **Unit tests for database operations**:
    - Test ExecutionService persist operations
    - Test SafetyKernel fill persistence
    - Test RuntimeReconciler queries

12. **Integration test end-to-end**:
    - Mock decision → execution → fill → reconciliation
    - Verify database state at each step

13. **Test ghost order scenarios**:
    - Test system crash after DB write
    - Test IB cancel notification lost
    - Test duplicate fill handling

### Phase 5: Documentation (1-2 hours)
**Priority**: P2 - For maintenance

14. **Update architecture docs**:
    - Document actual vs planned differences
    - Update GHOST_ORDER_PREVENTION.md with correct schema

15. **Create runbook**:
    - Deployment steps
    - Monitoring queries
    - Troubleshooting guide

---

## Implementation Order (One-by-one)

### Task 1: Fix ExecutionService database operations
**Time**: 30 min
**Files**: `services/exec/execution_service.py`

Changes needed:
```python
# BEFORE (WRONG):
INSERT INTO core.core_order (
    order_id, decision_id, strategy_id, symbol, ...
)

# AFTER (CORRECT):
INSERT INTO core.core_order (
    order_id, strategy_id, symbol, idempotency_key,
    side, order_type, qty, limit_px, stop_px, tif,
    state, note, created_at, updated_at
) VALUES (
    gen_random_uuid(), $1, $2, $3,
    $4, $5, $6, $7, $8, $9,
    'NEW', $10, now(), now()
)
-- Store decision_id in note as: {"decision_id": "dec_123"}
```

### Task 2: Fix SafetyKernel fill persistence
**Time**: 30 min
**Files**: `services/exec/safety_kernel.py`

Changes needed:
```python
# BEFORE (WRONG):
INSERT INTO core.core_fill (
    fill_id, order_id, symbol, side, qty, price,
    commission, ts_us, exchange, exec_id
)

# AFTER (CORRECT):
INSERT INTO core.core_fill (
    fill_id, order_id, ts, qty, price,
    liquidity, commission_usd, exchange, exec_id, created_at
) VALUES (
    gen_random_uuid(), $1, $2, $3, $4,
    $5, $6, $7, $8, now()
)
-- ts is timestamptz, pass datetime directly
```

### Task 3: Fix SafetyKernel order state updates
**Time**: 20 min
**Files**: `services/exec/safety_kernel.py`

Changes needed:
```python
# BEFORE (WRONG):
UPDATE core.core_order
SET state = $1, filled_qty = $2, updated_at = now()
WHERE broker_order_id = $3

# AFTER (CORRECT):
UPDATE core.core_order
SET state = $1, updated_at = now()
WHERE broker_order_id = $2

-- Calculate filled_qty separately if needed:
SELECT COALESCE(SUM(qty), 0) as filled_qty
FROM core.core_fill
WHERE order_id = (SELECT order_id FROM core.core_order WHERE broker_order_id = $1)
```

### Task 4: Fix RuntimeReconciler ghost handling
**Time**: 15 min
**Files**: `services/reconciliation/runtime_reconciler.py`

Changes needed:
```python
# BEFORE (WRONG):
UPDATE core.core_order
SET state = 'STALE_CLOSED', updated_at = now()
WHERE order_id = $1

# AFTER (CORRECT):
UPDATE core.core_order
SET state = 'CANCELLED', note = 'GHOST_DETECTED', updated_at = now()
WHERE order_id = $1
```

### Task 5: Add filled_qty helper function
**Time**: 20 min
**Files**: `services/exec/safety_kernel.py`

Add new method:
```python
async def _get_filled_qty(self, order_id: int) -> float:
    """Get total filled quantity for order from core_fill table"""
    if not self.pg:
        return 0.0

    result = await self.pg.fetchone(
        """
        SELECT COALESCE(SUM(f.qty), 0) as filled_qty
        FROM core.core_fill f
        JOIN core.core_order o ON f.order_id = o.order_id
        WHERE o.broker_order_id = $1
        """,
        str(order_id)
    )
    return float(result['filled_qty']) if result else 0.0
```

---

## Testing Checklist

- [ ] ExecutionService persists order to database
- [ ] SafetyKernel persists fills to database
- [ ] Fill quantity calculated correctly from core_fill table
- [ ] Order state transitions follow constraints
- [ ] RuntimeReconciler marks ghosts correctly
- [ ] Capital reservation released on FILLED
- [ ] Idempotency keys prevent duplicates
- [ ] Fill handler deduplicates fills
- [ ] End-to-end flow: decision → execution → fill → reconciliation

---

## Risk Assessment

**HIGH RISK** (before fixes):
- Database operations will fail at runtime
- Ghost orders not prevented (reconciler broken)
- Capital reservations not released (wrong columns)
- Constraint violations on state updates

**MEDIUM RISK** (after fixes, before testing):
- Integration points not verified
- Performance not tested
- Edge cases not covered

**LOW RISK** (after testing):
- Schema-aligned operations
- Tested ghost prevention
- Validated capital release

---

## Timeline

- **Phase 1 (Schema Alignment)**: 2-3 hours
- **Phase 2 (Missing Functionality)**: 3-4 hours
- **Phase 3 (Integration)**: 2-3 hours
- **Phase 4 (Testing)**: 4-5 hours
- **Phase 5 (Documentation)**: 1-2 hours

**Total**: ~15 hours (2 working days)

---

## Success Criteria

✅ All database operations execute without errors
✅ Ghost orders detected and marked correctly
✅ Capital reservations released on terminal states
✅ Fill deduplication works
✅ End-to-end integration test passes
✅ No constraint violations
✅ Reconciliation finds and fixes ghosts within 60 seconds
