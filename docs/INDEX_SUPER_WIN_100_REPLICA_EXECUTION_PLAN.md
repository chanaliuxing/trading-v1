# INDEX_SUPER_WIN 100% Replica Execution Plan

## 1. Objective

Build a new `index_super_win` that satisfies all four goals at the same time:

1. Behavior is 100% replica-equivalent to current `qqq_intraday`.
2. It can run independently when `qqq` is disabled.
3. It can run multiple different symbols in one strategy instance.
4. It can run in parallel with `qqq` and any other strategy without order, position, or ledger conflicts.

This plan is strict on one point:

`100% replica` is not declared by code similarity. It is declared only after replay parity, shadow parity, and conflict-free runtime proof all pass.

## 2. Non-Negotiable Constraints

1. Do not modify current `qqq_intraday` trading behavior during replica work.
2. Do not let `index_super_win` touch foreign orders, foreign positions, or foreign cleanup paths.
3. Do not use account net position as strategy-owned truth.
4. Do not mix observability work with logic rewrites in the same slice unless required for proof.
5. Any missing broker truth must fail-safe to `block` or `quarantine`, never optimistic continue.
6. Adding or removing symbols must be configuration-only; no strategy code edits are allowed for symbol rollout.
7. Behavior parity must be proven first in single-symbol mode before multi-symbol orchestration is accepted.
8. Shared execution and attribution services are part of the replica baseline and must be version-frozen together with `qqq`.

## 3. Current Fact-Based Gap

Current `index_super_win` is safer than before, but it is still not a 100% behavioral replica of `qqq_intraday`.

Main missing areas:

1. No full `qqq` loop/state-machine parity.
2. No full exit lifecycle parity:
   - TP1 partial
   - trailing stop
   - breakeven shield
   - EOD flatten
3. No full protective stop parity:
   - exchange stop submit
   - stop dedupe
   - stop rebuild after restart
   - stop audit self-heal
   - kill-switch
4. No ATR seed / daily ATR / QQEW / VWAP gating parity.
5. No deterministic replay proof against `qqq_intraday`.

Therefore the target architecture must separate:

1. `behavior replica`
2. `runtime isolation`
3. `proof tooling`
4. `configuration/manifest layer`

## 3.1 Replica Contract Model

The new strategy must satisfy two contracts at the same time:

1. `behavior parity contract`
2. `safety isolation contract`

Behavior parity contract means:

1. same entry decision
2. same exit decision
3. same TP1 / trailing / breakeven / stop behavior
4. same restart recovery behavior

Safety isolation contract means:

1. foreign orders are never treated as owned
2. foreign positions are never flattened as owned
3. foreign fills are never silently accepted into local ledger truth
4. same-symbol contention blocks safely

The target is not "copy qqq exactly and inherit all risk".
The target is:

1. `qqq` behavior inside the strategy boundary
2. existing isolation hardening outside the strategy boundary

## 4. Target Architecture

### 4.1 Replica Core

Add a dedicated `qqq` behavioral core for replica use.

Proposed files:

1. `strategies/index_super_win_replica_core.py`
2. `strategies/index_super_win_replica_strategy.py`
3. `strategies/index_super_win_replica_types.py`

Rules:

1. Logic is ported from `qqq_intraday_strategy.py` in behavior-preserving slices.
2. No "cleanup" or "optimization" during port.
3. Each migrated slice must keep the same decision contract and side effects.
4. Replica core is first built and proven in single-symbol mode.

### 4.2 Isolation Adapter

Keep the new replica behind existing safe isolation boundaries.

Must remain true:

1. `index_super_win` namespace: `iswv2:*`
2. `qqq` namespace: `qqqv1:*`
3. Separate runtime scope and state paths
4. Separate observability paths
5. Shared process-level broker connection is allowed
6. Strategy-level ownership is never shared

### 4.3 Multi-Symbol Layer

Replica must support:

1. one `index_super_win` instance
2. many configured symbols
3. independent state machine per symbol

Required design:

1. One symbol = one runtime state machine
2. One symbol = one trade lifecycle container
3. One symbol = one protection/audit lifecycle
4. Shared service objects allowed, shared mutable symbol state not allowed

### 4.4 Manifest / Config Layer

The strategy must support configuration-only symbol rollout.

Proposed files:

1. `strategies/index_super_win_manifest.py`
2. `configs/index_super_win/*.yml`

Required config contract:

1. signal symbol mapping
2. confirm symbol mapping
3. long exec symbol mapping
4. short exec symbol mapping
5. ATR / VWAP / QQEW source mapping
6. session timing
7. stop / TP1 / trailing / breakeven parameters
8. runtime scope / state path / observability path suffixing

Hard rule:

1. New symbol onboarding must be achieved by config only.
2. No ticker-specific if/else branches are allowed in strategy logic.

### 4.5 Plugin Boundary

To keep the system modular and pluggable, the following boundaries must remain explicit:

1. `behavior_core`
2. `symbol_runtime_container`
3. `broker_truth_adapter`
4. `execution_adapter`
5. `state_store`
6. `observability_adapter`
7. `manifest_loader`

No module is allowed to directly reach across layers when an adapter contract exists.

## 5. Delivery Strategy

Use four parallel tracks, but land them in strict order:

1. Replica parity spec
2. Replica core slices
3. Isolation hardening for multi-strategy safety
4. Proof harness and acceptance

Do not cut live before all four are green.

## 6. Phase Plan

## Phase A: Freeze QQQ Baseline

Goal:

Create an immutable behavioral baseline for current `qqq_intraday`.

Tasks:

1. Freeze current `qqq_intraday` strategy file hash.
2. Freeze live config values that affect behavior:
   - entry window
   - ATR parameters
   - QQEW gating parameters
   - stop parameters
   - TP1 parameters
   - breakeven parameters
   - flatten timing
   - stop audit timing
3. Freeze all output events to be compared:
   - gate allow/block result
   - block reason
   - side
   - qty
   - order_ref kind
   - protective stop submit
   - flatten reason
   - runtime transitions
   - restart recovery transitions
4. Freeze shared runtime environment that affects live behavior:
   - `ExecutionService` version
   - `RuntimeReconciler` version
   - `fill_event_handler` version
   - broker attribution behavior
   - market input contract and readiness gates

Deliverables:

1. `qqq` baseline config snapshot
2. parity field contract doc
3. baseline replay fixtures list
4. shared execution-chain version snapshot

Exit gate:

No more changes to `qqq` behavior until parity proof is complete.

## Phase B: Behavioral Slice Map

Goal:

Break `qqq` into controlled slices and migrate one slice at a time.

Slices:

1. startup and restore
2. required data gate
3. entry gate
4. entry submit and pending tracking
5. fill/order-update convergence
6. position synchronization
7. protective stop submit
8. stop audit self-heal
9. TP1 partial exit
10. trailing stop
11. breakeven shield
12. forced EOD flatten
13. restart protection rebuild
14. orphan/open-order cleanup parity

Rules:

1. One slice lands only with tests.
2. No next slice before parity tests for current slice pass.
3. Any intentional difference must be documented and rejected unless explicitly approved.
4. First acceptance target is single-symbol `qqq` parity, not multi-symbol parity.

## Phase C: New Replica Runtime Model

Goal:

Create a symbol-scoped runtime model that can host `qqq` behavior for many symbols.

Required work:

1. Introduce per-symbol replica lifecycle object:
   - decision state
   - pending state
   - live position state
   - stop state
   - TP1/trailing state
   - restart recovery state
2. Introduce per-symbol broker truth adapter:
   - position view
   - open-order view
   - protection-order view
3. Introduce per-symbol state persistence:
   - runtime state file
   - ATR seed state
   - daily ATR state
   - observability snapshot

Hard requirement:

No symbol can corrupt another symbol's state in memory or on disk.

Execution order inside this phase:

1. build single-symbol runtime container
2. pass single-symbol replay parity
3. replicate container N times for multi-symbol orchestration
4. prove configuration-only symbol rollout

## Phase D: Isolation Contract Expansion

Goal:

Make sure replica-safe behavior also remains safe against all other strategies.

Tasks:

1. Keep all-symbol `entry_isolation` enabled.
2. Expand same-symbol contention tests to:
   - `qqq` vs `index`
   - `index` vs foreign non-qqq strategy
   - two `index` instances with different strategy ids
3. Add explicit proof that:
   - foreign order refs are ignored
   - foreign fills are ignored
   - foreign open orders are quarantined
   - foreign broker inventory is quarantined
4. Add explicit cleanup contract:
   - `index` cleanup never cancels `qqqv1:*`
   - `qqq` cleanup never cancels `iswv2:*`
   - `index` flatten never uses foreign broker qty
5. Add explicit ledger attribution contract:
   - `perm_id/order_ref` is primary truth
   - `broker_order_id` fallback must never be silent
   - ambiguous attribution must remain unresolved evidence, not confirmed ownership

Exit gate:

All isolation tests must remain green while replica slices are added.

## Phase E: Deterministic Replay Harness

Goal:

Prove behavior parity against current `qqq_intraday`.

New tooling:

1. `tool/replay/qqq_index_replica_replay.py`
2. `services/strategy_runtime_v2/parity_recorder.py`

Replay input must include:

1. market snapshots
2. QQQ / QQEW / exec symbol prices
3. VWAP state
4. ATR inputs and ATR seed data
5. broker positions
6. open orders
7. order update events
8. restart checkpoints
9. deterministic wall-clock and loop cadence

Initial minimum fixture set:

1. normal entry -> TP1/trailing -> exit
2. restart with live position and missing protection
3. same-symbol foreign open-order conflict

Only after these three pass do we expand the replay matrix.

Replay comparison categories:

1. gate mismatch
2. qty mismatch
3. side mismatch
4. timing mismatch
5. protective stop mismatch
6. flatten reason mismatch
7. recovery mismatch
8. orphan cleanup mismatch

Pass criteria:

1. side equality: 100%
2. block reason equality: 100%
3. protective stop side effects: 100%
4. flatten reason equality: 100%
5. no duplicate entry actions
6. no missing exit actions

## Phase F: Shadow Runtime Proof

Goal:

Prove runtime parity and conflict-free coexistence in real sessions.

Shadow sequence:

1. `index` standalone shadow, 1 full session
2. `index` multi-symbol shadow, 1 full session
3. `qqq` live + `index` different-symbol shadow, 1 full session
4. `qqq` live + `index` same-symbol shadow, minimum 3 full sessions

Must record:

1. parity report
2. isolation report
3. foreign conflict report
4. order attribution report

Critical failure triggers:

1. foreign cancel attempt
2. foreign flatten attempt
3. duplicate entry
4. reverse entry
5. live position with missing protection
6. ambiguous fill attribution

Any critical failure means no cut-in.

## Phase G: Controlled Live Cut-In

Goal:

Cut over with minimal blast radius.

Order:

1. single symbol, tiny notional
2. multiple symbols, tiny notional
3. same symbol with `qqq` disabled
4. optional final migration if replica proves stable

Rollback conditions:

1. any position conflict
2. any foreign cleanup/cancel
3. any protection gap above threshold
4. any runtime/broker drift not auto-healed
5. any parity regression against latest `qqq` baseline

Kill criteria before cut-in:

1. protection/restart parity cannot converge on replay
2. same-symbol shadow produces any foreign cleanup or foreign flatten
3. ledger attribution produces unresolved critical ambiguity

## 7. Detailed TODO

## 7.1 Replica Core TODO

- [ ] Create `index_super_win_replica_core.py`
- [ ] Build single-symbol replica core first
- [ ] Create symbol-scoped trade state object
- [ ] Port startup gate and required-data gate
- [ ] Port `qqq` entry gate logic exactly
- [ ] Port pending-entry and fill-confirm contract
- [ ] Port position synchronization contract
- [ ] Port `_submit_protective_stop_intent`
- [ ] Port `_ensure_live_protection`
- [ ] Port `_audit_stop_order_loop`
- [ ] Port TP1 partial exit
- [ ] Port trailing-stop logic
- [ ] Port breakeven shield
- [ ] Port forced EOD flatten
- [ ] Port restart stop rebuild
- [ ] Port unexpected short inventory handling
- [ ] Port state persistence and restore

## 7.2 Multi-Symbol TODO

- [ ] Create manifest/config loader for symbol onboarding
- [ ] Replace single-lifecycle assumptions with per-symbol lifecycle containers
- [ ] Add per-symbol ATR seed persistence
- [ ] Add per-symbol daily ATR context persistence
- [ ] Add per-symbol stop audit task management
- [ ] Add per-symbol shutdown flush and state save
- [ ] Add per-symbol trader-view output
- [ ] Prove `AAPL/MSFT/NVDA` can be enabled by config only

## 7.3 Module Boundary TODO

- [ ] Define adapter contracts for `behavior_core`
- [ ] Define adapter contracts for `broker_truth_adapter`
- [ ] Define adapter contracts for `execution_adapter`
- [ ] Define adapter contracts for `state_store`
- [ ] Define adapter contracts for `observability_adapter`
- [ ] Ban direct ticker-specific branching in behavior logic

## 7.4 Isolation TODO

- [ ] Keep namespace contract tests green
- [ ] Add foreign fill ignore tests
- [ ] Add foreign order update ignore tests
- [ ] Add same-symbol multi-strategy contention replay tests
- [ ] Add two-index-instance isolation tests
- [ ] Add cleanup contract tests
- [ ] Add ledger attribution contract tests
- [ ] Add ambiguous attribution unresolved-evidence tests

## 7.5 Replay/Proof TODO

- [ ] Build baseline recorder for current `qqq`
- [ ] Build deterministic replay adapter for new replica
- [ ] Generate mismatch report format
- [ ] Add daily parity summary tool
- [ ] Add replay fixture set:
  - normal entry/exit
  - stop hit
  - TP1 then trailing
  - EOD flatten
  - restart with open position
  - restart with missing stop
  - stale pending entry
  - foreign open-order conflict

## 7.6 Shadow TODO

- [ ] Run standalone index shadow
- [ ] Run multi-symbol index shadow
- [ ] Run qqq + index different-symbol shadow
- [ ] Run qqq + index same-symbol shadow for 3 sessions
- [ ] Review all alerts, parity reports, and attribution reports

## 8. Test Strategy

## 8.1 Unit Tests

Need exact behavioral tests for each migrated slice.

Minimum suite:

1. entry gate parity
2. side selection parity
3. qty parity
4. ATR stop distance parity
5. TP1 trigger parity
6. trailing line parity
7. breakeven shield parity
8. EOD flatten parity
9. stop audit self-heal parity
10. restart stop rebuild parity
11. shutdown persistence parity

## 8.2 Integration Tests

Minimum suite:

1. standalone index boot
2. 3-symbol parallel run in one instance using config only
3. same-symbol with qqq in shadow mode
4. foreign open-order quarantine
5. foreign broker position quarantine
6. order update convergence after fill
7. protection order persistence across restart
8. two-index-instance parallel isolation
9. ledger attribution unresolved path stays isolated

## 8.3 Replay Tests

Minimum suite:

1. exact event-by-event parity replay
2. time-bucket parity replay
3. restart replay
4. delayed broker update replay
5. ambiguous attribution replay
6. config-only symbol rollout replay

## 8.4 Operational Tests

Minimum suite:

1. monitor and observability remain quiet during normal sessions
2. alerts fire on conflict only
3. shadow reports are complete
4. disk outputs remain bounded and readable
5. adding a new symbol requires config changes only

## 9. Acceptance Standard

Replica is accepted only when all sections below pass.

## 9.1 Replica Acceptance

1. All replay parity checks are green.
2. All behavior slices are event-equivalent to `qqq`.
3. No known intentional logic deviations remain.
4. Single-symbol parity is proven before multi-symbol acceptance is claimed.

## 9.2 Independent-Run Acceptance

1. `index` runs with `qqq` disabled.
2. No `qqq` state file or `qqqv1` dependency remains.
3. Entry, exit, protection, and recovery all work standalone.

## 9.3 Multi-Symbol Acceptance

1. At least 3 symbols run in one instance without state contamination.
2. One symbol failure or quarantine does not corrupt other symbols.
3. Per-symbol protection lifecycle works independently.
4. New symbol rollout requires config changes only.
5. No strategy code diff is needed to add or remove symbols.

## 9.4 Parallel-Run Acceptance

1. `index` never cancels or flattens foreign orders/positions.
2. `qqq` never cancels or flattens `index` orders/positions.
3. Same-symbol contention blocks safely.
4. No ledger or attribution corruption occurs.
5. Ambiguous attribution never becomes silent owned ledger truth.

## 9.5 Live-Cut Acceptance

1. Three same-symbol shadow sessions pass.
2. No critical incident occurred.
3. Daily summary and audit proof are complete.

## 10. Recommended Execution Order

The safest order is:

1. Freeze baseline
2. Build minimal replay harness
3. Build single-symbol replica core
4. Port startup/entry slices
5. Port protection slices
6. Port exit slices
7. Port restart/recovery slices
8. Prove single-symbol deterministic parity
9. Add manifest/config layer
10. Complete multi-symbolization
11. Run deterministic replay and config-only rollout proof
12. Run shadow proof
13. Controlled live cut-in

## 11. Critical Review

This plan is intentionally slower than a direct rewrite.

Reason:

1. A direct rewrite risks breaking both parity and isolation.
2. A generic "make index more like qqq" refactor will miss the exact side effects that matter in live trading.
3. The most dangerous bugs are not entry bugs. They are:
   - wrong cleanup
   - missing protection
   - stale runtime after restart
   - foreign attribution
   - same-symbol double action

So the right strategy is:

1. first preserve exact behavior
2. then prove exact behavior
3. then allow live cut-in

Anything weaker is not `100% replica`.
