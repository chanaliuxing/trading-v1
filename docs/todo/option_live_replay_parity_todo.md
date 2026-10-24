# Option Engine Final TODO

## Review And Critical

## What Was Right In The Previous Plan
- [ ] `Kernel + Plugin` is the correct direction for long-term reuse.
- [ ] Keeping old strategy code intact during migration is mandatory.
- [ ] Shadow first, then small live rollout, then cutover is the only safe rollout path.
- [ ] Strategy id isolation and config-gated rollout are non-negotiable.

## What Was Missing
- [ ] `Kernel + 3 hooks` is not enough for a real option engine.
- [ ] Without an explicit `ExecutionProfile`, replay/live will drift again strategy by strategy.
- [ ] Without an explicit `OwnershipLedger`, same-option-symbol multi-strategy trading will eventually collide.
- [ ] Without a dedicated `ParityRunner`, migration cannot be proven; it can only be guessed.
- [ ] Without a stable domain model, the plugin layer becomes a thin wrapper over old strategy code and does not become reusable infrastructure.

## Final Architectural Decision
- [ ] Build the system as five layers:
  - `OptionKernel`
  - `OptionPlugin`
  - `ExecutionProfile`
  - `OwnershipLedger`
  - `ParityRunner`
- [ ] Keep the current live strategies untouched until a v2 path passes acceptance.
- [ ] Make all new paths opt-in per strategy instance.

## Goal
- [ ] Build a general, pluggable option trading engine that can host multiple option strategies safely.
- [ ] Allow multiple option strategies on the same underlying.
- [ ] Allow multiple option strategies on the same option symbol without order or position conflicts.
- [ ] Make live behavior match replay as closely as practical under IBKR constraints.
- [ ] Ensure rollback is a config change, not a rewrite.

## Non-Negotiable Constraints
- [ ] Do not change the current `qqq_1dte_ma10_reject` live trading behavior during migration.
- [ ] Do not change default execution behavior for existing strategies.
- [ ] Do not let one strategy cancel, flatten, modify, or adopt another strategy's orders or lots.
- [ ] Do not require risky infra/schema changes before parity evidence exists.
- [ ] Do not allow shared mutable runtime state without explicit ownership.

## Core Domain Model

## OptionKernel
- [ ] Own lifecycle orchestration only:
  - market-data readiness
  - bar-close scheduling
  - plugin hook dispatch
  - intent submission
  - audit
  - strategy-scoped synchronization
- [ ] Do not embed strategy-specific signal logic.
- [ ] Do not embed strategy-specific position management logic.

## OptionPlugin
- [ ] Own strategy differences only:
  - signal rules
  - entry structure
  - contract preference
  - exit policy inputs
- [ ] Never talk directly to IBKR.
- [ ] Never directly mutate broker state outside kernel-managed intents.

## ExecutionProfile
- [ ] Be the shared contract between replay and live.
- [ ] Explicitly define:
  - decision cadence
  - timeout bars
  - repricing schedule
  - premium expansion cap
  - partial TP rules
  - stop rules
  - hard/soft time-stop rules
  - stale quote handling
- [ ] Version execution behavior with an explicit profile id.

## OwnershipLedger
- [ ] Project strategy-owned lots from:
  - decisions
  - intents
  - orders
  - fills
  - broker open orders
  - broker net positions
- [ ] Store stable ownership keys:
  - `strategy_id`
  - `trigger_id`
  - `decision_id`
  - `position_lot_id`
  - `entry_snapshot_id`
  - `parent_order_ref`
  - `execution_profile_id`
- [ ] Ensure all exit/flatten/cancel logic uses owned lots, not net symbol quantity only.

## ParityRunner
- [ ] Run old strategy and v2 strategy on the same recorded inputs.
- [ ] Compare:
  - signal timestamp
  - branch
  - contract
  - execution profile
  - expected stop
  - expected TP
  - exit reason
- [ ] Produce machine-readable diffs.

## Minimal Plugin Contract
- [ ] `apply_default_extras(extras)`
- [ ] `build_signal(ctx) -> Optional[Signal]`
- [ ] `build_entry_plan(ctx, signal) -> Optional[EntryPlan]`
- [ ] `select_contract(ctx, entry_plan) -> Optional[Selection]`
- [ ] `build_execution_profile(ctx, entry_plan, selection) -> ExecutionProfile`
- [ ] `build_exit_plan(ctx, lot, market_state) -> Optional[ExitPlan]`
- [ ] `component_name`

## Why The Old 3-Hook Design Is Rejected
- [ ] `maybe_enter` is too broad and hides signal / plan / contract differences in one function.
- [ ] `sync_and_manage_position` becomes a strategy-specific state machine trap.
- [ ] It prevents replay/live parity from being defined at the right abstraction level.
- [ ] It makes the engine reusable in name only.

## Directory Plan
- [ ] `strategies/options_kernel/base.py`
- [ ] `strategies/options_kernel/plugin.py`
- [ ] `strategies/options_kernel/types.py`
- [ ] `strategies/options_kernel/execution_profile.py`
- [ ] `strategies/options_kernel/ownership.py`
- [ ] `strategies/options_kernel/parity.py`
- [ ] `strategies/options_plugins/qqq_1dte_ma10_reject.py`
- [ ] `strategies/qqq_1dte_ma10_reject_v2_strategy.py`
- [ ] `tests/unit/strategies/options_kernel/*`
- [ ] `tests/unit/strategies/test_qqq_1dte_ma10_reject_parity.py`
- [ ] `tests/integration/options_kernel/*`

## Implementation TODO

## Phase 0: Freeze Contracts
- [ ] Define stable dataclasses or typed models for:
  - `Signal`
  - `EntryPlan`
  - `Selection`
  - `ExecutionProfile`
  - `PositionLot`
  - `ExitPlan`
  - `ParityDiff`
- [ ] Define one profile id for the first migrated strategy:
  - `qqq_option_ma_ext_v1`
- [ ] Freeze one market-data profile for parity:
  - `standard_close_extended_sma`

## Phase 1: Build OwnershipLedger First
- [ ] Implement strategy-owned `PositionLot` projection.
- [ ] Support same option symbol across different strategies.
- [ ] Support multiple lots on the same strategy from different bar timestamps.
- [ ] Make order adoption strategy-safe.
- [ ] Make orphan handling strategy-safe.
- [ ] Make manual flatten strategy-safe.
- [ ] Make reconciliation strategy-safe.

## Phase 2: Build OptionKernel Skeleton
- [ ] Move shared subscription, cadence, audit, and intent orchestration into kernel.
- [ ] Keep plugin hooks pure and side-effect minimal.
- [ ] Keep v2 path behind a strategy-level enable flag.
- [ ] Keep legacy strategy classes untouched.

## Phase 3: Build ExecutionProfile Layer
- [ ] Define shared replay/live execution profile fields.
- [ ] Map replay settings to live order semantics explicitly.
- [ ] Record profile id in:
  - snapshots
  - audit
  - replay summary
  - live intents

## Phase 4: Build ParityRunner
- [ ] Create a runner that feeds the same bars/quotes to old and v2 logic.
- [ ] Compare every decision, not just final PnL.
- [ ] Emit diff files for:
  - missing decisions
  - extra decisions
  - contract mismatches
  - exit-plan mismatches

## Phase 5: Migrate `qqq_1dte_ma10_reject`
- [ ] Build `QQQ1DTEMa10RejectPlugin`.
- [ ] Start with adapter-style migration so old logic remains the source of truth.
- [ ] Create `QQQ1DTEMa10RejectV2Strategy` as `Kernel + Plugin`.
- [ ] Add v2 config entry with `enabled: false`.

## Phase 6: Shadow And Comparison
- [ ] Run v2 in shadow mode.
- [ ] Compare legacy vs v2 daily.
- [ ] Require parity evidence before any live order submission.

## Phase 7: Controlled Cutover
- [ ] Enable v2 for one strategy instance only.
- [ ] Keep old strategy disabled but intact.
- [ ] Keep rollback to one config flag flip.
- [ ] Do not migrate any second option strategy until first migration passes acceptance.

## Phase 8: Generalize For More Option Strategies
- [ ] Add second plugin after the first one passes acceptance.
- [ ] Prove same-underlying multi-strategy coexistence.
- [ ] Prove same-option-symbol multi-strategy coexistence.
- [ ] Only then call the engine "general-purpose".

## Test Plan

## Unit Tests
- [ ] `OwnershipLedger` assigns distinct `position_lot_id` values for:
  - same option symbol, different strategy
  - same strategy, different entry bar
- [ ] `OwnershipLedger` never merges lots across strategies.
- [ ] `ExecutionProfile` maps replay fields to live execution settings deterministically.
- [ ] `OptionKernel` does not submit orders when plugin returns no signal.
- [ ] Plugin hook ordering is deterministic.
- [ ] Decision snapshot failure blocks v2 order submission.
- [ ] Duplicate decision protection blocks second submission for the same bar/contract/signal.
- [ ] Exit plan only targets owned lots.
- [ ] Cancel/replace only targets owned orders.

## Parity Tests
- [ ] Old strategy vs v2 on identical inputs have identical:
  - entry timestamps
  - side
  - signal branch
  - selected contract
  - execution profile id
  - expected stop / TP
- [ ] Exit-plan parity is verified separately from fill parity.
- [ ] Diff output is stable and machine-readable.

## Integration Tests
- [ ] Same underlying, two option strategies, different contracts: no collisions.
- [ ] Same underlying, two option strategies, same contract symbol: no collisions.
- [ ] Same contract symbol, both strategies submit exits: only owned orders are touched.
- [ ] Reconciliation on broker net position does not rewrite ownership incorrectly.
- [ ] Orphan adoption does not attach a broker order to the wrong strategy.
- [ ] Restart recovery reconstructs the same active lots and owned orders.
- [ ] Manual flatten for strategy A does not flatten strategy B.

## Replay-Live Drift Tests
- [ ] Shadow-mode live decisions match replay on `should_trade`.
- [ ] Shadow-mode live decisions match replay on `should_not_trade`.
- [ ] Contract mismatch rate is measured.
- [ ] Fill drift is measured separately and not confused with decision drift.
- [ ] Exit reason drift is measured separately and not confused with fill drift.

## Regression Tests
- [ ] Existing live strategies remain unchanged when v2 path is disabled.
- [ ] Existing order routing remains unchanged when v2 path is disabled.
- [ ] Existing reconciliation tests remain green.
- [ ] Existing Telegram/manual intervention tests remain green.
- [ ] Existing replay baselines remain unchanged for strategies not using v2.

## Manual Verification
- [ ] Run one shadow session on `QQQ`.
- [ ] Inspect one replay-vs-v2 diff report manually.
- [ ] Verify one same-underlying multi-strategy case manually.
- [ ] Verify one same-option-symbol multi-strategy case manually.
- [ ] Verify rollback by disabling v2 and confirming system behavior returns to legacy path.

## Acceptance Checklist

## Core Parity Gates
- [ ] Legacy vs v2 have identical trade count on accepted replay sessions.
- [ ] Legacy vs v2 have identical entry timestamps or documented tolerated bar-close equivalence.
- [ ] Legacy vs v2 have identical side and contract selection on accepted parity sessions.
- [ ] Legacy vs v2 have identical exit reason classification on accepted parity sessions.

## Multi-Strategy Isolation Gates
- [ ] Cross-strategy order mutation count = `0`.
- [ ] Cross-strategy position-lot collision count = `0`.
- [ ] Cross-strategy flatten mis-target count = `0`.
- [ ] Cross-strategy orphan adoption mis-ownership count = `0`.
- [ ] Cross-strategy reconciliation ownership rewrite count = `0`.

## Same-Symbol Hard Gates
- [ ] Two strategies can hold the same option symbol simultaneously.
- [ ] Broker net position can be projected back into correct strategy-owned lots.
- [ ] One strategy's stop/tp/cancel never modifies the other strategy's same-symbol order.

## Operational Gates
- [ ] V2 is disabled by default.
- [ ] Shadow mode can run without broker order submission.
- [ ] Daily parity report is generated automatically.
- [ ] Rollback is one config change.

## Definition Of "Good Enough To Enable"
- [ ] `should_trade parity >= 95%`
- [ ] `should_not_trade parity >= 95%`
- [ ] contract mismatch rate within agreed tolerance
- [ ] duplicate decision count = `0`
- [ ] cross-strategy mutation count = `0`
- [ ] no unresolved ownership drift after reconciliation

## Rollout Order
1. [ ] Freeze domain model and execution profile contract.
2. [ ] Build `OwnershipLedger`.
3. [ ] Build `OptionKernel`.
4. [ ] Build `ParityRunner`.
5. [ ] Migrate `qqq_1dte_ma10_reject` into plugin form.
6. [ ] Run legacy-vs-v2 parity tests.
7. [ ] Run one-day live shadow.
8. [ ] Fix drift sources.
9. [ ] Enable one v2 strategy instance only.
10. [ ] Keep all other option strategies on legacy path until acceptance is proven.

## Out Of Scope
- [ ] Perfectly identical market fills under all market conditions.
- [ ] Replacing IBKR broker semantics.
- [ ] Migrating every option strategy at once.
- [ ] Refactoring unrelated live strategy families in the same phase.

## Deliverables
- [ ] Versioned kernel/plugin/execution-profile interfaces.
- [ ] Strategy-owned ownership ledger.
- [ ] Legacy-vs-v2 parity runner and diff report.
- [ ] `QQQ1DTEMa10RejectPlugin`.
- [ ] `QQQ1DTEMa10RejectV2Strategy`.
- [ ] Multi-strategy isolation tests.
- [ ] Rollout and rollback checklist.
