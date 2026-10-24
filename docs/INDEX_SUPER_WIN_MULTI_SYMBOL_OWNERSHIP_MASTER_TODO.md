# INDEX_SUPER_WIN Multi-Symbol / Same-Symbol Ownership Master TODO

## 1. Goal

This document defines the remaining architecture work required to satisfy the real target:

1. `index_super_win` must behave like `qqq_intraday_001`.
2. `index_super_win` must run in parallel with `qqq_intraday_001`.
3. `index_super_win` must support multiple symbols in one strategy instance.
4. The same execution symbol must be allowed to run under multiple strategies without cross-flatten, cross-cancel, or ownership theft.

This is stricter than simple parity. It requires strategy-owned order/fill/position attribution.

## 2. Non-Negotiable Rules

1. Do not rely on broker net position alone to decide strategy ownership.
2. Do not flatten or cancel based on account-level net inventory.
3. All owned live inventory must be reconstructable from:
   - `order_ref`
   - `broker_order_id`
   - `perm_id`
   - owned fill ledger
   - owned lot ledger
4. Foreign same-symbol inventory must never be auto-claimed as replica-owned inventory.

## 3. Architecture Phases

### Phase A. Ownership Data Model

- [x] Add stock-style lot ledger for replica strategies
- [x] Track mappings:
  - order_ref -> lot
  - broker_order_id -> lot
  - perm_id -> lot
- [x] Track lot lifecycle:
  - opening
  - live
  - partial_exit
  - closed
  - quarantined / recovered conflict evidence

### Phase B. Strategy Runtime Integration

- [x] Record entry intent into ownership ledger before broker submit
- [x] Attach broker identities on owned order updates
- [x] Mark lot live on owned fill/entry confirmation
- [x] Record partial exit fills into the lot ledger
- [x] Record flatten / protective stop fills into the lot ledger
- [x] Clear per-trade runtime state without destroying historical lot evidence

### Phase C. Same-Symbol Safety

- [x] Add explicit owned-lot projection by `signal_symbol`
- [x] Add explicit owned-lot projection by `exec_symbol`
- [x] Block or quarantine only when owned-lot state is inconsistent
- [x] Never treat foreign same-symbol broker net inventory as replica-owned lot state

### Phase D. Reconcile / Recovery

- [x] Rebuild lot ownership from persisted runtime + owned order identities on restart
- [x] Use owned-lot state to explain:
  - why flatten is allowed
  - why stop is allowed
  - why a same-symbol signal is blocked
- [x] Add recovery paths for:
  - owned lot live but runtime flat
  - runtime pending but no owned lot progress
  - broker update arrives without `strategy_id` but with owned namespace / ids

### Phase E. Proof

- [x] Add deterministic tests for same-symbol multiple strategies with distinct lots
- [x] Add replay/proof tooling for owned-lot continuity
- [ ] Add real-session proof that blocked same-symbol samples contain no foreign actions
- [ ] Add real-session proof that owned-lot evidence exists for all live/exit actions

## 4. Detailed TODO

### OWN-01 Ledger Foundation

- [x] Add `StockOwnershipLedger`
- [x] Add lot record dataclass
- [x] Add intent record dataclass
- [x] Add exit/fill record dataclass
- [x] Add snapshot output

### OWN-02 Identity Mapping

- [x] Attach `order_ref`
- [x] Attach `broker_order_id`
- [x] Attach `perm_id`
- [x] Query lot by any of the above

### OWN-03 Runtime Hooks

- [x] Entry build -> create lot id
- [x] Entry submit -> persist intent record
- [x] Entry confirm -> mark lot live
- [x] TP1 confirm -> reduce owned qty
- [x] Flatten confirm -> close owned qty
- [x] Stop fill -> close owned qty

### OWN-04 Runtime Usage

- [x] Add owned-lot snapshot to strategy state output
- [x] Add owned-lot evidence to audit payloads where relevant
- [x] Add helper to compute owned live qty by symbol/side

### OWN-05 Same-Symbol Proof Rules

- [x] Same exec symbol across two strategies must be represented as two separate owned lots
- [x] One strategy closing its lot must not reduce the other strategy's lot
- [x] One strategy stop rebuild must not attach to foreign lot/order ids

## 5. Test Plan

### Unit

- [x] Same symbol across different strategies -> separate lots
- [x] Multiple lots same strategy same symbol -> separate lots
- [x] Broker identity attach by `broker_order_id`
- [x] Broker identity attach by `perm_id`
- [x] Partial exit reduces remaining qty only on the owned lot
- [x] Full exit closes only the targeted lot

### Strategy Integration

- [x] Entry path records owned lot
- [x] Entry confirmation marks lot live
- [x] TP1 confirmation reduces lot qty
- [x] Flatten / stop confirmation closes lot
- [x] Runtime clear does not erase ownership history

### Isolation

- [x] Same-symbol foreign order update does not mutate owned lot
- [x] Same-symbol foreign position update does not create owned lot
- [x] Same-symbol two-strategy scenario produces two distinct active lots

### Replay / Proof

- [x] Owned-lot snapshot / ownership evidence present in replay-proof tooling
- [x] Same-symbol proof includes owned-lot evidence gating

## 6. Acceptance

All items below must be true.

- [x] Replica strategy has a stock ownership ledger in runtime
- [x] Owned live qty is derivable from ledger, not only broker net position
- [x] Same execution symbol across strategies can be represented as separate lots
- [x] No flatten/cancel path requires foreign net inventory inference
- [x] Same-symbol tests and isolation suites are green
- [ ] Real-session proof shows zero foreign actions and owned-lot evidence is present

## 7. Current Execution Order

1. Re-run parity / same-symbol proof on a fresh session
2. Verify real-session ownership evidence is present for all live/exit actions
3. Only then consider enablement
