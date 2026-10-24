# IB Order Recovery Safety Lane TODO - 2026-05-03

## Goal

Make IB restart/disconnect recovery converge broker truth, order truth, and strategy intent before the system resumes normal entry flow. A live nonzero broker position must never be left without a valid protection, close-only exit, flatten order, or explicit quarantine/manual lock.

## Problem Summary

1. `IBConnectionManager` currently publishes `system_resumed` when IB market/exec/master sessions and master open-order snapshot are ready. That proves API availability, but not order truth or ledger convergence.
2. UOM blocks every non-cancel order while system health is `paused`, `recovering`, `degraded`, or `halted`. This blocks new entries, but also blocks protective stops, close-only exits, and forced flatten actions.
3. Some strategies, especially QQQ intraday, perform an additional strategy-local health block before submitting protection/flatten intents. That prevents UOM from applying a safety-lane decision.
4. UOM rejects `exit`, `flatten`, and `protection` when the internal ledger is flat. If IB has a nonzero position and DB ledger is stale/missing, this prevents the repair action needed to converge truth.
5. ClickHouse fallback paths can silently return empty data after driver/API mismatch, creating strategy data gaps that look like valid no-op cycles.

## Target Architecture

```text
IBConnectionManager
  -> broker_api_ready / system_recovering
  -> BrokerTruthService / RuntimeReconciler
  -> StrategyRecoveryPolicy
  -> UnifiedOrderManager safety lane
  -> ExecutionService
  -> BrokerTruthReconcile
  -> SystemHealthController(system_resumed)
```

## Invariants

- IB broker position is final truth for live exposure.
- DB ledger is an explainable local projection and may be repaired from broker truth.
- Normal entries require system state `running` and clean broker truth.
- `cancel`, `protection`, `close_only exit`, and `flatten` are safety actions and must not be blocked by ordinary `paused`, `recovering`, or `degraded` states.
- `halted` remains a hard block unless an explicit emergency override is added.
- Safety actions require an executable IB session and idempotent order identity.
- A nonzero broker position must have one of:
  - valid protection order,
  - valid close-only/flatten order,
  - active quarantine/manual intervention lock.

## Implementation Plan

### P0 - Resume Ownership

- Change `IBConnectionManager._runtime_recovery_loop()` to publish `broker_api_ready` or `system_recovering`, not `system_resumed`.
- RuntimeReconciler/BrokerTruthConvergence becomes the only runtime owner allowed to publish final `system_resumed`.
- Preserve startup resume from `run_system.runtime_reconciler` after `ensure_startup_ready()`.
- RuntimeReconciler must publish `system_resumed` after a clean runtime pass when health is `recovering` or `degraded`; otherwise runtime recovery can get stuck after `broker_api_ready`.

### P0 - UOM Safety Lane

- Keep health blocking for `entry`.
- Allow `protection`, `exit`, and `flatten` through `paused`, `recovering`, and `degraded`.
- Keep `halted` blocked by default.
- In broker-truth gate, require `exec_ready` for safety actions; do not require market data readiness for close-only safety actions.
- Strategy admission still applies to normal entries, but recovery safety actions with a nonzero broker/position quantity hint may bypass strategy admission so residual broker exposure can be repaired even if the strategy was removed from the active live baseline.

### P0 - Ledger Repair Gate

- If ledger is flat but safety intent carries nonzero `position_qty_hint`, `broker_qty`, or `broker_position_qty_hint`, allow the safety action and mark metadata as `ledger_repair_from_broker_truth`.
- Continue rejecting safety actions when both ledger and broker/position hints are flat or unknown.
- Clamp requested safety quantity to known nonzero broker/position hint when it is smaller than requested qty.

### P0 - Strategy Safety Pre-Block

- Remove QQQ strategy-local health pre-block for protective stop and flatten intents.
- Keep entry pre-blocks.
- Let UOM decide whether a safety intent can execute.

### P1 - StrategyRecoveryPolicy

- Introduce a required policy interface for active/live strategies:
  - input: broker position, DB ledger state, broker open orders, fills, strategy config, recovery reason;
  - output: standard order intents: `ensure_protection`, `flatten`, `cancel_replace`, `quarantine`.
- RuntimeReconciler evaluates policy output on position mismatch and submits executable recovery actions through UOM; non-executable or unowned actions go to quarantine/manual fallback.
- Strategies without a strategy-specific policy fall back to the platform default close-only flatten policy; QQQ intraday has a registered strategy-specific policy.
- PositionReconciler resolves strategy ownership from a single nonzero ledger lot first, then explicit configured symbol ownership, then current working safety/recovery/protection ownership only when those orders identify exactly one strategy; stale flat `core_position` rows and ambiguous working safety owners are not used for owner fallback.
- Runtime recovery flatten uses stable `decision_id`/`intent_id`, UOM no-ops duplicate working recovery flatten orders, and cooldown suppresses repeated audit/action attempts inside one reconciler process.

### P1 - ClickHouse Data Gate

- Unify ClickHouse wrapper API or remove old `execute` calls.
- Fix invalid imports such as `get_ch_client`.
- Convert data fallback failures into explicit `data_unavailable` gates instead of silent empty inputs.
- Data-unavailable gates block entry/signal generation only; existing position management, protection repair, and EOD flatten must continue through the safety lane.

## Test Plan

### Unit

- IB manager runtime recovery emits `broker_api_ready`, not `system_resumed`.
- Health controller does not move to `running` from IB manager `broker_api_ready`.
- RuntimeReconciler clean runtime pass after `broker_api_ready` emits `system_resumed` and moves health to `running`.
- UOM blocks entry while paused/recovering/degraded.
- UOM allows protection/exit/flatten while paused/recovering/degraded when exec truth is ready.
- UOM blocks safety lane when exec truth is not ready.
- UOM allows recovery safety action with nonzero broker/position qty hint even when strategy admission provider would reject the strategy.
- UOM allows flatten/protection when ledger is flat but broker/position qty hint is nonzero.
- UOM rejects flatten/protection when ledger and broker/position hints are both flat.
- QQQ protective stop and flatten intents reach `ctx.submit_intent()` while system is recovering; UOM remains final gate.
- StrategyRecoveryPolicy registry emits close-only flatten recovery actions for broker nonzero / ledger stale mismatch.
- RuntimeReconciler submits flatten recovery actions through UOM and quarantines rejected/unowned actions.
- RuntimeReconciler emits `strategy_recovery_policy_unresolved` when broker exposure has no strategy owner and creates a quarantine action.
- PositionReconciler attaches `strategy_id`, `owner_strategy_id`, and `owner_strategy_source` when ownership can be inferred.
- Duplicate recovery actions use stable idempotency and are no-opped only when the exact matching `recovery_intent_id`/idempotency key already has a working recovery flatten; broad recovery-note substring matching is not used.
- Owner fallback refuses stale flat `core_position` ownership and plain working entries; only explicit configured ownership, single nonzero ledger lot, or validated working safety/recovery/protection ownership can attach a strategy.
- `RuntimeReconciler.run_once()` executes policy-generated flatten actions through UOM in the position mismatch path.
- ClickHouse wrapper supports legacy `execute()` and `get_ch_client()` callers.
- HistoryProvider ClickHouse fallback uses parameterized query execution and logs driver/API failures.
- HistoryProvider/QQQ V2 warmup surface ClickHouse driver failures into `data_unavailable_gate` / `data_integrity_warning` instead of treating them as ordinary empty history.
- QQQ V2 `data_integrity_warning` blocks entry through EntryGatekeeper but does not short-circuit fabric ingest, active position management, or 15:59 EOD flatten.

### Integration

- Simulate IB disconnect and recovery:
  - health enters paused/recovering;
  - entries are blocked;
  - safety actions are allowed when exec reconnects;
  - final `system_resumed` is emitted only after RuntimeReconciler reports clean truth.
- Simulate IB nonzero position with stale DB flat ledger:
  - recovery policy emits flatten/protection;
  - UOM permits safety action with broker qty hint;
  - reconciler repairs ledger or quarantines.
- Simulate orphan/ghost orders:
  - system does not resume normal entries;
  - safety lane can cancel/flatten as needed.

### Gameday

- Stop IB Gateway for 15 minutes during paper runtime.
- Restart IB Gateway.
- Verify:
  - no connection storm;
  - no `system_resumed` before reconciler clean result;
  - no new entry before clean truth;
  - existing position receives protection/flatten or quarantine.

## Acceptance Criteria

- `system_resumed` during runtime reconnect is not emitted by `IBConnectionManager`.
- RuntimeReconciler emits final runtime `system_resumed` after clean broker/order/position truth.
- During `recovering`, entry orders are blocked but protection/exit/flatten can reach execution gates.
- A broker-only nonzero position cannot be ignored because DB ledger is flat.
- Any live nonzero position after recovery is protected, flattening, or quarantined within the configured recovery SLA.
- ClickHouse fallback errors are observable and do not silently look like valid empty data.
- ClickHouse/data-unavailable warning cannot skip active position management, protection repair, or EOD flatten.

## Implementation Status

- P0 resume ownership, UOM safety lane, ledger repair gate, and QQQ safety pre-block removal are implemented.
- P1 StrategyRecoveryPolicy registry is implemented with default close-only flatten policy and QQQ-specific policy registration during `run_system` startup. Recovery flatten actions are submitted through UOM with stable idempotency, duplicate working-order no-op, and cooldown suppression; unresolved actions are quarantined.
- P1 ClickHouse compatibility/fallback fix is implemented. QQQ V2 data-unavailable state now blocks entry only and keeps safety management/EOD flatten running.
- Direct production caller gate currently reports zero production-pending UOM bypass files.
