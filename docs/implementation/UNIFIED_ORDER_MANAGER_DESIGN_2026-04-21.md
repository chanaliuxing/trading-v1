# UnifiedOrderManager / Protection Registry / PositionLedger Design (2026-04-21)

**Status**: Draft / Phase 0 Gate
**Owner**: Codex + operator review
**Related documents**:
- `docs/implementation/SYSTEM_HEALTH_CONTROLLER_DESIGN_2026-04-21.md`
- `docs/implementation/IBKR_ORDER_TRUTH_AND_RECONNECT_FINAL_PLAN_2026-04-17.md`
- `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md`
- `docs/implementation/QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md`

## 1. Decision Summary

We agree with Claude's correction: choose option B.

```text
UnifiedOrderManager is a new outer layer.
ExecutionService remains below it.
```

This is not an `ExecutionService` rename.

Reason:

1. `ExecutionService` is already large and should not absorb more lifecycle arbitration.
2. `UnifiedOrderManager` owns order intent arbitration, protection idempotency, FlipGuard coordination, and ledger gating.
3. `ExecutionService` should focus on broker-facing execution and existing persistence paths.
4. Migrating callers is work, but it is bounded and worth doing once.

## 2. Problem Statement

The system already has meaningful order hardening:

1. Deterministic `idempotency_key` in `ExecutionService`.
2. IB order truth and quarantine paths.
3. `FlipGuard`.
4. SafetyKernel OCO/protective order persistence and coalescing tests.
5. Runtime reconciliation for orphan/ghost detection.

The gap is that these controls are not exposed as one mandatory order lifecycle authority.

The operator requirement is stricter:

> Ghost orders, repeated stop/target orders, reverse-open orders, ledger drift, and IB/system mismatch must be solved once at the platform layer, not repeatedly inside every strategy.

## 3. Architecture Boundary

### 3.1 Final Call Chain

```text
Strategy / Plugin
  -> UnifiedOrderManager
  -> ExecutionService
  -> SafetyKernel / IBAdapter
  -> IB
```

Strategies may produce:

1. `EntryIntent`
2. `ExitIntent`
3. `FlattenIntent`
4. `ProtectionIntent`
5. `CancelIntent`

Strategies may not:

1. Call `ExecutionService` directly.
2. Call `IBAdapter` directly.
3. Create broker orders directly.
4. Modify protective orders directly.
5. Decide reverse-open eligibility locally.

### 3.2 Responsibility Split

`UnifiedOrderManager` owns:

1. Intent admission.
2. System health gate.
3. FlipGuard gate.
4. Existing exposure and pending-order gate.
5. Idempotency key normalization.
6. Protection key arbitration.
7. Lot-aware protective order lifecycle.
8. PositionLedger write/read gate.
9. Reconciler handoff.
10. Caller migration contract.

`ExecutionService` owns:

1. Existing order persistence writes.
2. Risk reservation calls that already live there until migrated.
3. Broker-facing submit/update/cancel delegation.
4. Fill handling integration points.
5. Existing notification/audit emitters until UOM audit becomes canonical.

`SafetyKernel` owns:

1. Low-level OCO attach behavior.
2. Broker adapter calls.
3. Existing OCO child quantity sync.
4. Protection failure manual-lock behavior.

`PositionLedger` owns:

1. Append-only position and financial events.
2. Derived current positions.
3. Realized/unrealized PnL basis.
4. IB/system position reconciliation surface.

## 4. UnifiedOrderManager API

Proposed module:

```text
services/orders/
  __init__.py
  manager.py
  intents.py
  protection_registry.py
  ledger.py
  reconciler_contracts.py
  migration_audit.py
```

Primary API:

```python
class UnifiedOrderManager:
    async def submit_entry(self, intent: EntryIntent) -> OrderDecision: ...
    async def submit_exit(self, intent: ExitIntent) -> OrderDecision: ...
    async def submit_flatten(self, intent: FlattenIntent) -> OrderDecision: ...
    async def ensure_protection(self, intent: ProtectionIntent) -> ProtectionDecision: ...
    async def cancel_order(self, intent: CancelIntent) -> CancelDecision: ...
    async def on_order_event(self, event: OrderEvent) -> None: ...
    async def on_fill(self, fill: FillEvent) -> None: ...
```

Admission result:

```python
OrderDecision(
    accepted: bool,
    action: str,              # submit, modify, no_op, block, quarantine
    reason: str,
    idempotency_key: str,
    order_ref: str | None,
    protection_key: str | None,
    ledger_event_id: str | None,
    broker_order_id: int | None,
    perm_id: int | None,
    metadata: dict,
)
```

## 5. ProtectionOrderRegistry

### 5.1 Protection Key

Canonical key:

```text
protection_key =
  account_id
  + strategy_id
  + symbol
  + lot_id
  + protection_kind
  + side
  + qty_bucket
  + price_band_id
```

Fields:

1. `account_id`: required; prevents paper/live collision.
2. `strategy_id`: owning strategy.
3. `symbol`: broker execution symbol, not just underlying.
4. `lot_id`: durable lot identity from entry.
5. `protection_kind`: `stop_loss`, `take_profit`, `trailing_stop`, `time_exit`.
6. `side`: protective order side.
7. `qty_bucket`: normalized quantity group for partial fills.
8. `price_band_id`: stable price band to avoid 1-cent repeated modify churn.

### 5.2 Price Band Rule

`price_band_id` must be deterministic.

Example:

```text
stop_loss band = round(stop_price / max(tick_size, configured_band_width))
take_profit band = round(limit_price / max(tick_size, configured_band_width))
```

Rule:

1. Same `protection_key`: no new broker order.
2. Same lot/kind/side/qty bucket but new price band: modify existing order if broker supports it.
3. Modify failure: mark `PROTECTION_DEGRADED` and require controlled recovery.
4. Flat position: cancel or quarantine all protection orders for the lot.

### 5.3 Qty Bucket Rule

`qty_bucket` must support partial fills.

Examples:

1. Full fill 100 shares: `qty_bucket=100`.
2. Partial fill from 100 to 60 remaining: new bucket only if protection quantity actually changes.
3. Broker pending child order with unknown perm id: defer duplicate placement until truth probe resolves.

## 6. Proposed DDL

### 6.1 Protection Registry

```sql
CREATE TABLE IF NOT EXISTS core.core_protection_order_registry (
    protection_key TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    protection_kind TEXT NOT NULL,
    side TEXT NOT NULL,
    qty_bucket TEXT NOT NULL,
    price_band_id TEXT NOT NULL,
    desired_qty NUMERIC NOT NULL,
    desired_price NUMERIC,
    broker_order_id BIGINT,
    ib_perm_id BIGINT,
    order_ref TEXT,
    state TEXT NOT NULL,
    degraded_reason TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_broker_sync_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_core_protection_registry_lot
ON core.core_protection_order_registry(account_id, strategy_id, symbol, lot_id);

CREATE INDEX IF NOT EXISTS idx_core_protection_registry_state
ON core.core_protection_order_registry(state);
```

Allowed states:

1. `DESIRED`
2. `PENDING_SUBMIT`
3. `LIVE`
4. `PENDING_MODIFY`
5. `PENDING_CANCEL`
6. `CANCELLED`
7. `FILLED`
8. `DEGRADED`
9. `QUARANTINED`

### 6.2 Position Ledger Events

```sql
CREATE TABLE IF NOT EXISTS core.position_ledger_event (
    ledger_event_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lot_id TEXT,
    event_type TEXT NOT NULL,
    event_ts TIMESTAMPTZ NOT NULL,
    side TEXT,
    qty_delta NUMERIC NOT NULL DEFAULT 0,
    price NUMERIC,
    commission NUMERIC NOT NULL DEFAULT 0,
    fees NUMERIC NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    broker_order_id BIGINT,
    ib_perm_id BIGINT,
    exec_id TEXT,
    idempotency_key TEXT,
    order_ref TEXT,
    source TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_position_ledger_exec
ON core.position_ledger_event(account_id, exec_id)
WHERE exec_id IS NOT NULL AND exec_id <> '';

CREATE INDEX IF NOT EXISTS idx_position_ledger_lot
ON core.position_ledger_event(account_id, strategy_id, symbol, lot_id);

CREATE INDEX IF NOT EXISTS idx_position_ledger_symbol
ON core.position_ledger_event(account_id, symbol, event_ts);
```

### 6.3 Current Position View

```sql
CREATE OR REPLACE VIEW core.position_ledger_current AS
SELECT
    account_id,
    strategy_id,
    symbol,
    lot_id,
    SUM(qty_delta) AS qty,
    SUM(qty_delta * price) FILTER (WHERE price IS NOT NULL) AS notional_sum,
    SUM(commission) AS commission_sum,
    SUM(fees) AS fees_sum,
    MAX(event_ts) AS last_event_ts
FROM core.position_ledger_event
GROUP BY account_id, strategy_id, symbol, lot_id;
```

Note: production average price/PnL calculation should be implemented in a tested ledger fold, not only in SQL.

## 7. Reconciler Split Contract

The current large runtime reconciler should become a facade over four smaller contracts.

### 7.1 StartupReconciler

Runs before trading-enabled state.

Responsibilities:

1. Fetch IB open orders.
2. Fetch DB open orders.
3. Fetch ledger current positions.
4. Block trading on unresolved mismatch.
5. Produce startup gate report.

### 7.2 OrderTruthReconciler

Responsibilities:

1. Compare IB open/completed orders with `core.core_order`.
2. Use `ib_order_truth`.
3. Quarantine unknown live broker orders.
4. Never silently adopt unknown orders into a strategy.

### 7.3 PositionReconciler

Responsibilities:

1. Compare IB positions with `position_ledger_current`.
2. Detect symbol/strategy/lot drift.
3. Create reconcile events.
4. Require operator ack or controlled auto-heal policy before resume.

### 7.4 AckReconciler

Responsibilities:

1. Repair missing order ack.
2. Repair missing fill ack.
3. Release reservations on terminal state.
4. Avoid mutating broker truth.

## 8. Order Admission Gates

Every intent must pass these gates in order:

1. `SystemHealthGate`
   - blocks entries while paused/degraded/recovering.
2. `StrategyAdmissionGate`
   - strategy must be health-subscribed and enabled.
3. `LedgerTruthGate`
   - current ledger state must be readable.
4. `BrokerTruthGate`
   - no unresolved quarantine for affected symbol.
5. `FlipGuardGate`
   - no same-bar reversal or forbidden reverse-open.
6. `PendingOrderGate`
   - no conflicting pending order.
7. `ProtectionRegistryGate`
   - for protection intents, enforce `protection_key`.
8. `ExecutionServiceDelegate`
   - submit/modify/cancel only after all gates pass.

## 9. Caller Migration Plan

Phase 2 must audit and migrate all direct order callers.

Required report:

```text
reports/orders/uom_caller_surface_YYYYMMDD.json
```

Minimum fields:

1. file path
2. strategy id if known
3. direct call target
4. intent kind
5. migration status
6. risk level

Known likely migration areas:

1. QQQ monolith execution intent path.
2. QQQ option live strategies.
3. stocks kernel submit bridge.
4. options kernel pure plugin execution path.
5. manual flatten service.
6. strategy runtime v2 coordinator.
7. any tests/tools that call `ExecutionService` as production path.

Temporary compatibility:

1. `ExecutionService` may remain callable by tests and UOM internals.
2. Production strategy code must move to UOM.
3. A runtime lint must flag direct production caller bypass.

## 10. QQQ-Specific Requirements

QQQ migration cannot cut over until:

1. QQQ entry intents route through UOM.
2. QQQ protective stop/take profit intents route through UOM.
3. QQQ flat/reverse behavior is governed by UOM + FlipGuard.
4. QQQ legacy monolith and kernel shadow produce the same intent before UOM admission.
5. Any UOM block is recorded as a platform block, not hidden as a strategy signal diff.

## 11. QQQ Parity Drift Policy

Claude's drift correction is accepted.

Cutover gate remains:

```text
3 complete trading days with zero unapproved diff
```

If a diff occurs:

1. It must be attributed within 48 hours.
2. If attributed to a bug:
   - fix it
   - reset the 3-day count
3. If attributed to acceptable non-semantic drift:
   - create a parity whitelist entry
   - require PR review
   - do not reset the count after approval
4. If whitelist entries exceed 5:
   - pause cutover
   - return to QQQ signal authority design
   - do not add more whitelist exceptions

Whitelist examples that may be acceptable:

1. Microsecond timestamp serialization difference with same decision minute.
2. Field ordering difference in JSON.
3. Equivalent numeric representation within an explicitly defined epsilon.

Whitelist examples that are not acceptable:

1. Different side.
2. Different exec symbol.
3. Different target quantity.
4. Different entry allow/block decision.
5. Different protection price band.

## 12. MarketDataPlane Scheduling Decision

We accept Claude's scheduling correction:

1. Phase 3 should only enforce stock data routing through existing `UnderlyingSharedDataPlane`.
2. `OptionDataPlane` and `ReplayPlane` move to Phase 5.5, after QQQ cutover and before the second stock plugin / mixed stock-option plugin push.
3. This protects QQQ parity from being polluted by a large cross-strategy data-plane rewrite.

Immediate data rule:

```text
QQQ and stock kernel paths must use UnderlyingSharedDataPlane for stock/ETF data.
Option quote/greeks/IV unification is deferred, documented, and not allowed to block QQQ cutover.
```

## 13. Stress Tests

### 13.1 Protection Idempotency Stress

Scenario:

1. Create one lot.
2. Emit the same stop/target desire 100 times.
3. Vary price by less than one band.
4. Vary price across one band.
5. Vary quantity through partial-fill buckets.

Expected:

1. Same band + same qty bucket creates one broker order.
2. Same band does not repeatedly modify.
3. New band modifies existing order, not place duplicate.
4. New qty bucket modifies or rebinds according to policy.
5. No duplicate live protection for the same lot/kind/side bucket.

### 13.2 Ghost Order Restart Stress

Scenario:

1. Place order.
2. Kill runtime before local ack completion.
3. Restart.
4. IB has live order.
5. DB state incomplete.

Expected:

1. StartupReconciler blocks affected symbol.
2. OrderTruthReconciler quarantines or matches by durable identity.
3. No new entry until resolved.
4. Telegram/system alert records the block.

### 13.3 Reverse-Open Stress

Scenario:

1. Fill long position.
2. Flatten it.
3. Same bar emits short entry.
4. Another strategy emits short entry on same symbol.

Expected:

1. UOM + FlipGuard blocks reverse-open.
2. Block reason is platform-level.
3. Strategy does not need custom code.

## 14. Release Gates

Phase 2 is complete only when:

1. UOM is the only production strategy order entrypoint.
2. `ExecutionService` direct production bypass is linted and blocked.
3. Protection registry DDL exists and is covered by tests.
4. Position ledger DDL exists and fill event append path is tested.
5. Reconciler split contracts exist with focused tests.
6. Protection idempotency stress passes.
7. Ghost order restart stress passes.
8. Reverse-open stress passes.
9. QQQ still produces identical pre-UOM signal/intent parity.

## 15. Daily EOD Report Requirements

Add these sections to the daily report:

1. IB positions vs ledger positions diff.
2. IB orders vs system orders diff.
3. Ghost/quarantine count.
4. Protection duplicate count.
5. Telegram push audit.
6. QQQ signal parity status.
7. PnL/cash/commission diff.

Any missing section makes the report `INCOMPLETE`, not `PASS`.

## 16. Decision Record

### Accepted

1. UOM is a new outer layer, not an `ExecutionService` rename.
2. Protection key uses 8 practical fields including account id, qty bucket, and price band.
3. PositionLedger becomes the single truth for positions and financial ledger events.
4. RuntimeReconciler should be split into startup/order/position/ack units.
5. QQQ cutover gets drift attribution and whitelist governance.
6. OptionDataPlane and ReplayPlane move to Phase 5.5.

### Rejected / Deferred

1. Continuing to put more lifecycle arbitration into `ExecutionService` is rejected.
2. Treating current OCO coalescing as complete protection idempotency is rejected; it is useful existing work but must be lifted into a cross-strategy registry.
3. Plugin-chain work remains deferred until health, order safety, and QQQ parity are under control.

## 17. Resolved Questions / v1.0

The following items are now fixed as implementation decisions, not discussion notes.

### Q1. Where does `lot_id` come from?

Decision:

1. `lot_id` is created at accepted entry-intent time by UOM.
2. It becomes the durable identity used by protection registry, ledger, and reconciler.
3. Strategies may pass a hint, but may not own the final `lot_id`.

### Q2. What is the single truth for positions and financial accounting during migration?

Decision:

1. `PositionLedger` is the target single truth.
2. During migration, legacy tables remain as inputs and audit surfaces.
3. After UOM admission and fill handling are wired, all current-position views used for release gates must be derived from `PositionLedger` or a tested fold over its events.

### Q3. When does UOM block new trading because of broker/DB mismatch?

Decision:

1. Any unresolved symbol-scoped quarantine blocks new entry for that symbol.
2. Unknown scope blocks all new entry.
3. Flatten and explicit risk-reduction actions remain allowed through controlled paths.

### Q4. Are "shadow-named but live" strategies allowed?

Decision:

1. No.
2. Phase 0 baseline found two such strategies and corrected them to true shadow mode.
3. Freeze gate must reject future configs where strategy id/name implies shadow while config resolves to live.

### Q5. What is the required migration rule for direct execution callers?

Decision:

1. New production strategy code may not call `ExecutionService` directly.
2. Existing callers must be surfaced in `uom_caller_surface_report`.
3. Migration completes only when production strategy order flow enters through UOM.
4. Test and tooling exceptions must be explicitly marked as non-production.

### Q6. What data-plane assumption is valid for Phase 2?

Decision:

1. Equity/ETF strategy paths must assume `UnderlyingSharedDataPlane` as the required stock-data plane.
2. Option quote/greeks/IV unification is deferred and may not silently be treated as already solved.
3. Reports and gates must distinguish:
   - `underlying_shared_plane`
   - `engine_managed_features`
   - `option_private_data_path`
   - `unknown`

### Q7. What constitutes acceptable parity drift at cutover?

Decision:

1. Only approved whitelist items qualify.
2. The whitelist is for non-semantic drift, not trading behavior differences.
3. Whitelist count above 5 pauses cutover and forces architecture review.
