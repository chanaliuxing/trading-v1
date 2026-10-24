# QQQ Intraday Rule Modularization TODO

## Objective

Refactor the real `QQQIntradayStrategy` entry logic into reusable, trader-configurable rule modules, then reuse that rule stack for stock and option strategies.

Reference:

- `./docs/strategies/QQQ_INTRADAY_ENTRY_RULES_MODULARIZATION.md`

## Phase 1: Freeze Current Behavior

- [x] inventory all active `entry_model` branches in `./strategies/qqq_intraday_strategy.py`
- [x] identify which branch is the production source of truth today
- [x] map every current block reason to a stable trader-facing code
- [x] write a one-page branch matrix: `baseline`, `v47_fast_drift`, `v46_ab`, `v44_elite`

## Phase 2: Define Rule Contracts

- [x] add `EntryContext` dataclass for QQQ-specific derived state
- [x] add `RuleDecision` contract
- [x] add `DirectionDecision` contract
- [x] add `TranslationDecision` contract
- [x] define a module registry for entry rules

## Phase 3: Extract Session Modules

- [x] extract model-dependent session windows from live QQQ logic
- [x] implement:
  - [x] `fast_drift_window`
  - [x] `ab_opening_window`
  - [x] `baseline_rth_window`
- [x] add unit tests for each session window

## Phase 4: Extract Operational Guards

- [x] extract `amp_lock_guard`
- [x] extract `stable_entry_guard`
- [x] extract `restart_degrade_guard`
- [x] extract `startup_anchor_guard`
- [x] add unit tests for each operational guard

## Phase 5: Extract Execution Isolation Guards

- [x] extract `open_orders_guard`
- [x] extract `live_position_guard`
- [x] extract `pending_decision_sync_guard`
- [x] extract `ownership_guard`
- [x] wire guard outputs into normalized audit payloads

## Phase 6: Extract Data Integrity Guards

- [x] extract `qqq_freshness_guard`
- [x] extract `qqew_freshness_guard`
- [x] extract `continuity_180s_guard`
- [x] extract `atr_ready_guard`
- [x] extract `vwap_ready_guard`
- [x] extract `volume_ready_guard`
- [x] make QQEW participation configurable, not hardcoded per strategy fork

## Phase 7: Extract Market Context Modules

- [x] extract `v3m_context`
- [x] extract `atr_effective_context`
- [x] extract `vol_surge_context`
- [x] extract `distance_from_vwap_context`
- [x] extract `qqew_resonance_context`
- [x] define which fields are mandatory for downstream direction modules

## Phase 8: Extract Direction Modules

- [x] implement `baseline_direction`
- [x] implement `fast_drift_direction`
- [x] implement `ab_direction`
- [x] implement `elite_direction`
- [x] ensure direction modules output only `LONG/SHORT/NONE`, not execution symbols
- [x] add parity tests against current live QQQ strategy for each branch

## Phase 9: Extract Opening Filters

- [x] migrate `early_override` logic from live QQQ path
- [x] migrate `atr_rank_skip` logic
- [x] migrate `delayed_confirm` logic
- [x] migrate `early_lock_openclose_mismatch` logic
- [x] move reusable logic into dedicated module files under `strategies/qqq_intraday_*logic.py`

## Phase 10: Extract Veto And Overlay Modules

- [x] extract `rollback_veto`
- [x] extract `rebound_fail_veto`
- [x] extract `high_vol_confirm_veto`
- [x] extract `shadow_extreme_overlay`
- [x] extract `anti_chase_overlay`
- [x] extract `reentry_cooldown_overlay`
- [x] extract trader-specific day-shape overlays

## Phase 11: Extract Translation Modules

- [x] implement `tqqq_sqqq_translation`
- [x] implement `qqq_option_translation`
- [x] ensure translation is downstream of accepted direction only
- [x] define contract for stock strategy and option strategy to share the same upstream rule recipe

## Phase 12: Make Trader Recipes Configurable

- [x] add recipe schema to config for:
  - [x] session module
  - [x] direction module
  - [x] guard modules
  - [x] veto modules
  - [x] translator
- [x] document allowed combinations and invalid combinations
- [x] add validation that prevents incompatible module mixes

## Phase 13: Rebuild QQQ Option On Top Of Real Rule Modules

- [x] disable simplified fallback logic in `qqq_intraday_option_v1`
- [x] make QQQ option consume the real extracted QQQ rule recipe
- [x] keep option-specific logic limited to:
  - [x] contract selection
  - [x] quote health
  - [x] execution profile
  - [x] option exit execution details

## Phase 14: Acceptance

- [x] stock recipe reproduces current QQQ live decisions for sampled days
- [x] option recipe matches stock signal timestamps and directions
- [x] block reasons are stable and trader-readable
- [x] no new strategy fork is required to create a new QQQ rule combination
- [x] old QQQ option legacy paths can stay disabled permanently

## Critical Rule

- [x] do not claim parity complete until decision-by-decision replay proves equivalence against `QQQIntradayStrategy`
