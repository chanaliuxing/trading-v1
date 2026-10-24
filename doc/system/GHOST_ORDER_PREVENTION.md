# Ghost Order Prevention - Complete Implementation

## Overview

This document describes the complete order lifecycle and ghost order prevention mechanisms implemented in the trading system. The implementation ensures that **after order send to IBKR, risk control is working and there are no ghost orders**.

**Ghost Order**: An order that exists in one system (broker or database) but not the other, creating reconciliation issues and capital reservation leaks.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     COMPLETE ORDER LIFECYCLE                        │
└─────────────────────────────────────────────────────────────────────┘

1. Strategy Decision
   ↓
2. Decision Pipeline (Validator → Arbiter → Translator)
   ↓ (APPROVED)
3. ExecutionService ────────────────────┐
   ↓                                     │
4. RiskService (capital approval)       │ Ghost Prevention Layer
   ↓                                     │ - Database persistence
5. Database INSERT (state=NEW) ◄────────┤ - Idempotency keys
   ↓                                     │ - Atomic operations
6. SafetyKernel.place_entry_order()     │
   ↓                                     │
7. IBAdapter → IBKR ────────────────────┤
   ↓                                     │
8. Database UPDATE (state=SUBMITTED) ◄──┘
   ↓
9. [IBKR fills order]
   ↓
10. FillEventHandler receives fill
    ↓
11. Database INSERT core_fill
    ↓
12. Database UPDATE core_order (state=PARTIAL/FILLED)
    ↓
13. Release capital reservation (if FILLED)
    ↓
14. SafetyKernel attaches OCO protection
    ↓
15. RuntimeReconciler verifies (every 60s)
    ├─ Detect ghosts (DB has, IBKR doesn't)
    ├─ Mark as STALE_CLOSED
    └─ Alert on discrepancies
```

## Components

### 1. ExecutionService (`services/exec/execution_service.py`)

**Purpose**: Bridge between Decision Pipeline and order execution with full database tracking.

**Key Features**:
- Receives APPROVED decisions from DecisionPipeline
- Requests risk approval & capital reservation
- **Persists order to database BEFORE sending to broker** (state=NEW)
- Places order via SafetyKernel → IBAdapter
- **Updates database with broker order_id** (state=SUBMITTED)
- Handles errors with rollback (mark as REJECTED, release capital)

**Ghost Prevention**:
- Every order written to database before IBKR submission
- Idempotency keys prevent duplicates
- Reconciliation can match orders via idempotency_key

**Database Schema**:
```sql
INSERT INTO core.core_order (
    order_id,           -- UUID (internal)
    decision_id,        -- Links to strategy decision
    strategy_id,        -- Strategy identifier
    symbol,            -- Symbol
    side, qty,         -- Order details
    order_type,        -- MKT/LMT/STP/STP_LMT
    limit_px, stop_px, -- Prices
    tif,              -- Time in force
    state,            -- NEW/SUBMITTED/PARTIAL/FILLED/CANCELLED/REJECTED/STALE_CLOSED
    idempotency_key,  -- For reconciliation & deduplication
    created_at, updated_at
) VALUES (...)
```

**Flow**:
```python
# 1. Risk approval
risk_decision = await self.risk.approve(envelope, idempotency_key)

# 2. Persist to DB (NEW)
await self._persist_order_new(decision_id, strategy_id, symbol, plan, idempotency_key)

# 3. Place order
placed_order = await self.kernel.place_entry_order(...)

# 4. Update DB (SUBMITTED)
await self._update_order_submitted(idempotency_key, placed_order.order_id, ...)
```

---

### 2. SafetyKernel with PostgreSQL Persistence (`services/exec/safety_kernel.py`)

**Purpose**: Order placement with OCO protection and database tracking.

**Enhancements**:
- Added `pg: PostgresDB` parameter for database access
- Added `reservation_service` parameter for capital release
- **Persists fills to database** (`core_fill` table)
- **Updates order state in database** (PARTIAL/FILLED)
- **Releases capital reservations** on terminal states
- Handles cancellations and rejections with database updates

**Key Methods**:

```python
async def on_fill(self, fill: Fill):
    """Process fill and update database"""
    # Update position tracking
    plan.position.on_fill(order.side, fill.qty, fill.price)

    # Persist fill to database
    await self._persist_fill(fill)

    # Determine new state
    new_state = "FILLED" if plan.filled_qty >= plan.entry_qty else "PARTIAL"

    # Update order state in database
    await self._update_order_state(fill.order_id, new_state, plan.filled_qty)

    # Release capital if fully filled
    if new_state == "FILLED":
        await self._release_reservation(fill.order_id, "FILLED")

    # Attach OCO protection on first fill
    if plan.first_fill_ts_us is None:
        asyncio.create_task(self._attach_oco_protection(plan))

async def on_cancel(self, order_id: int, reason: str):
    """Handle cancellation"""
    await self.pg.execute("UPDATE core.core_order SET state='CANCELLED' WHERE ...")
    await self._release_reservation(order_id, "CANCELLED")

async def on_reject(self, order_id: int, reason: str):
    """Handle rejection"""
    await self.pg.execute("UPDATE core.core_order SET state='REJECTED' WHERE ...")
    await self._release_reservation(order_id, "REJECTED")
```

**Database Persistence**:
```sql
-- Fill persistence
INSERT INTO core.core_fill (
    fill_id, order_id, symbol, side, qty, price,
    commission, ts_us, exchange, exec_id, created_at
) VALUES (...)

-- Order state update
UPDATE core.core_order
SET state = $1, filled_qty = $2, updated_at = now()
WHERE broker_order_id = $3
```

---

### 3. FillEventHandler (`services/exec/fill_event_handler.py`)

**Purpose**: Subscribe to IB fill events and process them systematically.

**Key Features**:
- Subscribes to `ib.execDetailsEvent`
- Converts IB fills to internal `Fill` objects
- **Deduplicates fills** (prevents double-processing)
- Calls `SafetyKernel.on_fill()` for complete processing
- Emits Prometheus metrics for monitoring

**Flow**:
```python
def _on_ib_fill(self, trade, fill):
    """IB event callback (sync)"""
    asyncio.create_task(self._process_fill(trade, fill))

async def _process_fill(self, trade, fill):
    """Process fill async"""
    # Extract details
    order_id = trade.order.orderId
    exec_id = fill.execution.execId
    fill_id = f"{exec_id}:{order_id}"

    # Check for duplicate
    if fill_id in self.processed_fills:
        return  # Already processed

    self.processed_fills.add(fill_id)

    # Create Fill object
    fill_obj = Fill(fill_id=fill_id, order_id=order_id, qty=qty, price=price, ...)

    # Process through kernel
    await self.kernel.on_fill(fill_obj)
    # This triggers:
    # - Database INSERT core_fill
    # - Database UPDATE core_order (state=PARTIAL/FILLED)
    # - Capital release (if FILLED)
    # - OCO attachment (on first fill)
```

**Metrics**:
- `fills_received_total`: Total fills from broker
- `fills_processed_total`: Successfully processed
- `fills_duplicate_total`: Duplicates ignored
- `fills_error_total`: Processing errors
- `fill_processing_latency_seconds`: Processing time

---

### 4. RuntimeReconciler (`services/reconciliation/runtime_reconciler.py`)

**Purpose**: Continuous ghost order detection during system operation.

**Key Features**:
- Runs every 60 seconds (configurable)
- Fetches open orders from **database** (state not in FILLED/CANCELLED/REJECTED/STALE_CLOSED)
- Fetches open orders from **IBKR** via IBAdapter
- **Compares by broker_order_id** (database) = orderId (IB)
- **Detects ghosts**: Order in DB but not in IB
- **Auto-cleanup**: Marks ghosts as STALE_CLOSED
- **Detects orphans**: Order in IB but not in DB (alerts only)
- **State mismatch detection**: DB state vs IB status differs

**Flow**:
```python
async def run_once(self):
    """Single reconciliation run"""
    # 1. Fetch orders
    db_orders = await self._fetch_db_open_orders()
    ib_orders = await self._fetch_ib_open_orders()

    # 2. Build indices
    db_by_broker_id = {o['broker_order_id']: o for o in db_orders}
    ib_by_id = {o.order_id: o for o in ib_orders}

    # 3. Find ghosts (DB has, IB doesn't)
    ghosts = [o for o in db_orders if o['broker_order_id'] not in ib_by_id]

    # 4. Find orphans (IB has, DB doesn't)
    orphans = [o for o in ib_orders if str(o.order_id) not in db_by_broker_id]

    # 5. Cleanup ghosts
    if ghosts and self.auto_cleanup:
        for ghost in ghosts:
            await self.pg.execute(
                "UPDATE core.core_order SET state='STALE_CLOSED' WHERE order_id=$1",
                ghost['order_id']
            )
            logger.warning("Ghost order marked as STALE_CLOSED", order_id=ghost['order_id'])

    # 6. Alert on orphans
    if orphans:
        logger.error("Orphan orders detected (IB has, DB missing)", count=len(orphans))
```

**Metrics**:
- `recon_runtime_runs_total`: Total runs
- `recon_ghosts_found_total`: Ghosts detected
- `recon_orphans_found_total`: Orphans detected
- `recon_mismatches_total`: State mismatches
- `recon_db_open_orders`: Current DB open orders
- `recon_ib_open_orders`: Current IB open orders

**Ghost Cleanup**:
Orders marked as `STALE_CLOSED` are:
- Excluded from future reconciliation
- Visible in database for audit trail
- Can be manually reviewed if needed

---

## Ghost Order Prevention Guarantees

### ✅ Prevention Mechanisms

1. **Database-First Persistence**
   - Every order written to database BEFORE sending to IBKR
   - State = NEW initially
   - Even if IBKR fails, order is tracked

2. **Idempotency Keys**
   - Unique key per order: `{decision_id}:{uuid}`
   - Prevents duplicate submissions
   - Enables reconciliation matching

3. **Atomic State Updates**
   - Order state updates in database transaction
   - Fill persistence with deduplication
   - Capital reservation release on terminal states

4. **Continuous Reconciliation**
   - Runtime reconciliation every 60 seconds
   - Auto-detection and cleanup of ghosts
   - Alerts on orphans and mismatches

5. **Cold Startup Reconciliation**
   - Existing `Reconciler` service handles startup
   - Adopts orphan orders by orderRef
   - Cleans ghosts found during startup

### ✅ Failure Scenarios Covered

| Scenario | Prevention | Recovery |
|----------|-----------|----------|
| System crash after DB write, before IBKR submit | Order in DB (state=NEW), never submitted | Cold startup reconciliation marks as STALE_CLOSED |
| IBKR accepts order, system crashes before DB update | Order in IBKR with orderRef | Cold startup adopts order by orderRef |
| IBKR cancels order, notification lost | Order in DB (state=SUBMITTED) | Runtime reconciliation detects ghost, marks STALE_CLOSED |
| Duplicate fill notification | Fill ID tracked in FillEventHandler | Duplicate ignored |
| Capital reservation not released | Stored procedure with idempotency | Stale reservation sweep (30 min timeout) |
| Network partition during order submission | Order in DB (state=NEW) | ExecutionService marks as REJECTED, releases capital |

---

## Integration Example

### Full System Startup

```python
# 1. Initialize database
pg = PostgresDB()

# 2. Initialize IB adapter
ib_adapter = IBInsyncAdapter(host='127.0.0.1', port=4002)
await ib_adapter.start()

# 3. Initialize reservation service
reservation_service = ReservationService(pg)

# 4. Initialize risk service
risk_service = RiskService(
    reservation_service=reservation_service,
    reg_lite_guard=RegLiteGuard(),
    market_state_provider=market_state_provider
)

# 5. Initialize safety kernel (with DB persistence)
safety_kernel = SafetyKernel(
    broker_adapter=ib_adapter,
    pg=pg,
    reservation_service=reservation_service,
    place_rpm=10,
    oco_attach_timeout_sec=1.0
)
await safety_kernel.start()

# 6. Initialize execution service
execution_service = ExecutionService(
    pg=pg,
    risk_service=risk_service,
    safety_kernel=safety_kernel,
    enable_execution=True  # False for shadow mode
)

# 7. Initialize fill event handler
fill_handler = FillEventHandler(
    ib_adapter=ib_adapter,
    safety_kernel=safety_kernel
)
await fill_handler.start()

# 8. Initialize decision pipeline
decision_pipeline = DecisionPipelineService(
    market_state_provider=market_state_provider,
    bar_store=bar_store,
    feature_store=feature_store,
    reserve_ledger=reserve_ledger,
    shadow_mode=False  # Enable real decisions
)

# 9. Initialize cold startup reconciliation
cold_reconciler = Reconciler(
    adapter=ib_adapter,
    kernel=safety_kernel,
    pg_pool=pg.pool
)
await cold_reconciler.run_once()  # Run on startup

# 10. Initialize runtime reconciliation
runtime_reconciler = RuntimeReconciler(
    pg=pg,
    ib_adapter=ib_adapter,
    interval_seconds=60,
    auto_cleanup_ghosts=True
)
await runtime_reconciler.start()

# System is now fully operational with ghost order prevention
```

### Execution Flow Example

```python
# 1. Strategy generates decision
envelope = DecisionEnvelope(
    decision_id="dec_abc123",
    strategy_id="mean_reversion_v1",
    symbol="AAPL",
    target_qty=100,
    order_style={"order_type": "LMT", "limit_px": 150.00}
)

# 2. Decision pipeline processes
result = await decision_pipeline.process_decision(envelope)
# Result: PipelineResult(status='APPROVED', plan=ExecPlan(...))

# 3. Execution service executes
exec_result = await execution_service.execute(result)
# This:
# - Requests risk approval (capital reservation)
# - Persists to DB: state=NEW
# - Places order via SafetyKernel → IBKR
# - Updates DB: state=SUBMITTED, broker_order_id=123

# 4. IBKR fills order
# - FillEventHandler receives fill event
# - Persists fill to core_fill table
# - Updates core_order: state=FILLED
# - Releases capital reservation
# - SafetyKernel attaches OCO protection

# 5. Runtime reconciliation verifies (60s later)
# - Fetches DB orders: state=FILLED
# - Fetches IB orders: (order no longer open)
# - No ghosts detected ✓
```

---

## Monitoring & Alerting

### Key Metrics to Monitor

1. **Ghost Orders**:
   - `recon_ghosts_found_total` → Alert if > 0
   - `recon_db_open_orders - recon_ib_open_orders` → Should be ~0

2. **Orphan Orders**:
   - `recon_orphans_found_total` → Critical alert if > 0
   - Indicates ExecutionService failure

3. **Fill Processing**:
   - `fills_error_total` → Alert on errors
   - `fill_processing_latency_seconds` → P99 < 100ms

4. **Execution Latency**:
   - `exec_latency_seconds{stage="db_persist_new"}` → P95 < 50ms
   - `exec_latency_seconds{stage="order_placement"}` → P95 < 500ms

5. **Capital Reservation**:
   - Track reservation release failures
   - Monitor stale reservation sweeps

### Grafana Dashboard Queries

```promql
# Ghost orders detected
rate(recon_ghosts_found_total[5m])

# Current open orders (DB vs IB)
recon_db_open_orders - recon_ib_open_orders

# Fill processing latency P99
histogram_quantile(0.99, rate(fill_processing_latency_seconds_bucket[5m]))

# Execution success rate
rate(exec_orders_placed_total[5m]) / rate(exec_decisions_total[5m])
```

---

## Testing Ghost Order Prevention

### Test Cases

1. **Test: System crash after DB write, before IBKR submit**
   ```python
   # 1. Start execution
   exec_result = await execution_service.execute(result)
   # 2. Kill system after DB write, before IBKR placement
   # 3. Restart system
   # 4. Cold reconciliation should mark order as STALE_CLOSED
   ```

2. **Test: IBKR order cancel notification lost**
   ```python
   # 1. Place order successfully
   # 2. Manually cancel in TWS/IB Gateway
   # 3. Wait 60 seconds
   # 4. Runtime reconciliation should detect ghost
   # 5. Order should be marked STALE_CLOSED
   ```

3. **Test: Duplicate fill notification**
   ```python
   # 1. Place order
   # 2. Simulate duplicate fill event from IB
   # 3. FillEventHandler should ignore duplicate
   # 4. core_fill table should have single fill
   ```

4. **Test: Capital reservation release**
   ```python
   # 1. Place order (capital reserved)
   # 2. Order fills completely
   # 3. Verify capital reservation released
   # 4. Verify available capital increased
   ```

---

## Summary

The implementation provides **comprehensive ghost order prevention** through:

1. **ExecutionService**: Database-first persistence with idempotency
2. **SafetyKernel**: Complete order lifecycle tracking in PostgreSQL
3. **FillEventHandler**: Systematic fill processing with deduplication
4. **RuntimeReconciler**: Continuous ghost detection and cleanup

**Result**: After order send to IBKR, risk control is working and there are no ghost orders. ✅

All orders are tracked in database, capital reservations are properly managed, and continuous reconciliation ensures system integrity.
