# Netted Execution (Staged Rollout Plan)

Status: implemented behind feature flags. Default path remains direct mode.

## Safety rule for production
- No runtime integration before feature flag rollout.
- Default execution path remains direct mode.
- Existing strategies (qqq v1 / index_super_win / qqq v2) remain unchanged.

## Problem
- Broker position is account+symbol net position.
- Multiple strategies trading same symbol cannot be isolated at broker level.
- Strategy-layer compensation does not scale and is error-prone.

## Target model
- Parent order: one broker-facing net order per symbol/group.
- Child order: strategy-attributed virtual order linked by `parent_order_id`.
- Fill fan-out: parent fills deterministically allocated into child fills.

## Phase plan
1. Phase 0 (done):
- Add isolated modules: router/coordinator/fill allocator/models.
- Add unit tests for deterministic behavior.

2. Phase 1 (done behind flag):
- Add ExecutionRouter entry in pipeline/execution, default OFF.
- Add config flags and symbol/strategy scoped mode overrides.

3. Phase 2 (done behind flag):
- NettingExecutionService with parent/child order persistence.
- Keep RuntimeReconciler compatible with parent-only broker order ids.

4. Phase 3 (done behind flag):
- Fill fan-out to child fills.
- Terminal propagation and reservation release safety.

## Runtime flags
- `execution.netting.enabled`: enable netted execution routing.
- `execution.netting.default_mode`: `direct` (recommended) or `netted`.
- `execution.netting.symbols`: symbol allowlist for netting mode.
- `execution.netting.strategy_mode_overrides`: per strategy mode override.
- `execution.netting.symbol_mode_overrides`: per symbol mode override.
- `execution.netting.batch_window_ms`: micro-batching window.
- `execution.netting.strategy_id`: system strategy id for parent net orders.
- `execution.netting.fill_fanout_enabled`: enable parent->child fill allocation.

## Compatibility constraints
- Keep backward compatibility for existing envelope semantics.
- Add explicit semantics marker in extras (delta/target) before runtime routing.
- Preserve existing tests and regression suite before any integration merge.
