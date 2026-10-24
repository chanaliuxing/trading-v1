# Option Strategy Modularization Plan

This document remains the implementation-oriented modularization plan.  
For first-principles architecture, long-range platform direction, and the formal todo/test/acceptance checklist, use:

- `./docs/guides/OPTION_STRATEGY_FIRST_PRINCIPLES_MASTER_PLAN.md`
- `./docs/guides/OPTION_STRATEGY_TODO_TEST_ACCEPTANCE.md`

## Goal

Build a composable option-strategy framework where:

- signal logic is pluggable
- contract selection is shared and configurable
- execution behavior is shared and configurable
- position management is shared and configurable
- SPY / QQQ and future strategies run independently without namespace conflicts

This migration must not change live trading behavior during Phase 1.

## Current Gaps

- SPY and QQQ still reuse behavior through inheritance-heavy plugins
- signal / contract / execution / position logic are not first-class modules
- strategy defaults and diagnostics are still partially embedded in plugin subclasses
- same-symbol multi-strategy coexistence depends on runtime conventions, not explicit definitions

## Target Architecture

Each strategy instance is assembled from a `StrategyDefinition`:

- `signal_module`
- `contract_policy`
- `execution_policy`
- `position_policy`
- `strategy_id`
- `underlying`
- `risk_symbol`
- `audit_namespace`

The live kernel continues to own:

- history loading
- runtime state
- intent submission
- reconciliation
- audit emission
- lot ownership

## Migration Principles

1. No live behavior changes in Phase 1.
2. Add new composition interfaces first.
3. Adapt existing SPY / QQQ plugins gradually onto those interfaces.
4. Preserve current audit chain while adding module identity.
5. Keep strategy isolation explicit through definition metadata.

## Phases

### Phase 1

Add module interfaces and definition registry only.

Deliverables:

- `strategies/options_kernel/modules/`
- interface classes for signal / contract / execution / position
- `StrategyDefinition`
- `StrategyDefinitionRegistry`
- static definitions for:
  - `QQQ_MA10_REJECT_0324`
  - `SPY_MA_CROSS_STACK_CONT_V2_DUAL`

### Phase 2

Extract current QQQ / SPY logic into module implementations without changing live behavior.

### Phase 3

Make plugins consume `StrategyDefinition` and act as assembly adapters.

### Phase 4

Replay parity validation and same-symbol coexistence testing.

## Acceptance Criteria

1. Strategy definitions fully describe strategy composition.
2. SPY and QQQ can point to different signal modules while sharing runtime infrastructure.
3. Same-symbol strategies can coexist with explicit isolation metadata.
4. Replay parity can compare old plugin path vs modular path.
5. Audit logs can show definition identity and module identity for every strategy instance.

## Test Plan

### Unit

- definition construction
- registry lookup
- module interface compatibility
- definition metadata isolation

### Integration

- SPY definition and QQQ definition can be instantiated side by side
- no shared mutable state across definitions
- strategy IDs / risk symbols / audit namespaces remain distinct

### Replay Parity

- compare old vs modular path on:
  - 2026-03-26
  - 2026-03-27
  - 2026-03-30
  - 2026-03-31

## Non-Goals For Phase 1

- no order execution changes
- no signal threshold changes
- no position-management changes
- no config migration yet
