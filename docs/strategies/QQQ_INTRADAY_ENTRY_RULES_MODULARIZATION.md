# QQQ Intraday Entry Rules Modularization

## Purpose

This document extracts the real entry logic from `QQQIntradayStrategy` into a modular model that can be reused by:

- QQQ stock strategy
- QQQ option strategy
- future QQQ-related variants
- trader-configured strategy recipes

Goal:

- stop hiding trading logic inside one large strategy class
- define stable module boundaries
- make rule combinations trader-readable and engineer-implementable

This document is based on the real live strategy:

- `./strategies/qqq_intraday_strategy.py`

## First Principle

From a trader perspective, a strategy is not one monolithic algorithm.

It is a sequence of rule families:

1. Can I trade now
2. Is the market data trustworthy
3. Which direction is valid
4. Which regime am I in
5. Does this setup survive special opening/high-vol filters
6. Is this entry still worth taking
7. How should this signal be translated into execution

If these rule families are modular, traders can combine them.
If they remain hidden in one class, every new variant becomes a fork.

## Real QQQ Entry Stack

The current live QQQ strategy evaluates entry in this order:

### Layer 0: cadence

The strategy decides how often to evaluate entries:

- every second by default
- optional once-per-minute mode

Source:

- `./strategies/qqq_intraday_strategy.py:3340`

### Layer 1: session window

The active time window depends on `entry_model`:

- `v47_fast_drift`
- `v46_ab`
- `v44_elite`
- baseline

This means time window is not a generic fixed gate. It is model-dependent.

Source:

- `./strategies/qqq_intraday_strategy.py:3351`

### Layer 2: operational embargo / startup safety

Before market logic is even considered, the strategy can block on:

- `amp_locked`
- stable-entry guard
- restart degrade window
- startup anchor embargo

These are operational safety rules, not market alpha rules.

Sources:

- `./strategies/qqq_intraday_strategy.py:3380`
- `./strategies/qqq_intraday_strategy.py:3384`
- `./strategies/qqq_intraday_strategy.py:709`

### Layer 3: position / order isolation

The strategy blocks new entries when:

- there are active open orders
- broker already has live TQQQ/SQQQ position
- previous decision sync is still unresolved

These are execution integrity rules.

Sources:

- `./strategies/qqq_intraday_strategy.py:3389`
- `./strategies/qqq_intraday_strategy.py:3418`

### Layer 4: data integrity

The strategy requires:

- fresh QQQ data
- optionally fresh QQEW data
- continuity over recent seconds
- valid VWAP / ATR / volume context

This is much richer than a generic `freshness` gate.

Sources:

- `./strategies/qqq_intraday_strategy.py:3446`
- `./strategies/qqq_intraday_strategy.py:687`

### Layer 5: market context derivation

Once data is accepted, the strategy derives:

- `v3m_signed`
- `vwap_now`
- `qqew_vs_vwap`
- `atr_live`
- `atr_seed`
- `atr_eff`
- `vol_surge_ratio`
- `distance_from_vwap_atr`

These are not just raw features. They are normalized trader-facing context variables.

Sources:

- `./strategies/qqq_intraday_strategy.py:3492`
- `./strategies/qqq_intraday_strategy.py:3532`

### Layer 6: model-specific regime gates

This is the largest source of logic branching.

The live strategy currently contains at least these entry families:

- `baseline`
- `v47_fast_drift`
- `v46_ab`
- `v44_elite`

Each family has different thresholds and different blocking reasons.

Source:

- `./strategies/qqq_intraday_strategy.py:3549`

### Layer 7: local veto / anti-chase / day-shape filters

Even after a direction is chosen, entry can still be blocked by:

- shadow extreme filter
- anti-chase
- fourth-short / small-green rule
- stop re-entry cooldown

These are trader overlays, not core direction engines.

Sources:

- `./strategies/qqq_intraday_strategy.py:4516`
- `./strategies/qqq_intraday_strategy.py:4558`
- `./strategies/qqq_intraday_strategy.py:4597`
- `./strategies/qqq_intraday_strategy.py:4669`

### Layer 8: entry translation

Only after the full stack passes does the strategy convert signal into:

- side
- execution symbol
- decision ids / trigger ids
- sizing
- order submission context

This should be a separate translation layer.

Source:

- `./strategies/qqq_intraday_strategy.py:4623`

## What Should Become Modules

To make the strategy truly pluggable, we should stop thinking in terms of one `signal module`.

We need a rule graph made of these module types.

### 1. Session Modules

Responsibility:

- determine entry evaluation window
- support model-dependent windows

Examples:

- `fast_drift_window`
- `ab_opening_window`
- `baseline_rth_window`

### 2. Operational Guard Modules

Responsibility:

- block entry on startup / runtime safety conditions

Examples:

- `amp_lock_guard`
- `restart_degrade_guard`
- `startup_anchor_guard`
- `stable_entry_guard`

### 3. Execution Isolation Modules

Responsibility:

- block entry if order or position state is unsafe

Examples:

- `open_orders_guard`
- `live_position_guard`
- `pending_decision_sync_guard`
- `ownership_guard`

### 4. Data Integrity Modules

Responsibility:

- validate market data trustworthiness

Examples:

- `qqq_freshness_guard`
- `qqew_freshness_guard`
- `continuity_180s_guard`
- `atr_ready_guard`
- `vwap_ready_guard`
- `volume_ready_guard`

### 5. Market Context Modules

Responsibility:

- derive normalized context used by all later rules

Examples:

- `v3m_context`
- `atr_effective_context`
- `vol_surge_context`
- `distance_from_vwap_context`
- `qqew_resonance_context`

## Implementation Mapping (2026-04-02)

The following module set has been implemented and wired into `qqq_intraday_option_v1`:

- Contracts and engine:
  - `./strategies/qqq_intraday_rules/contracts.py`
  - `./strategies/qqq_intraday_rules/engine.py`
- Trader recipe and validation:
  - `./strategies/qqq_intraday_rules/recipe.py`
- Session model extraction:
  - `./strategies/qqq_intraday_rules/session.py`
- Runtime-to-context adapter:
  - `./strategies/qqq_intraday_rules/runtime_context.py`
- Guard modules:
  - `./strategies/qqq_intraday_rules/guards.py`
- Direction modules:
  - `./strategies/qqq_intraday_rules/direction.py`
- Overlay modules:
  - `./strategies/qqq_intraday_rules/overlays.py`
- Translation module:
  - `./strategies/qqq_intraday_rules/translator.py`
- Replay parity harness:
  - `./strategies/qqq_intraday_rules/parity.py`
- Option signal integration:
  - `./strategies/options_kernel/modules/qqq_intraday_stock_signal.py`
- Plugin-level trader configuration defaults:
  - `./strategies/options_plugins/qqq_intraday_option.py`

Notes:

- The simplified fallback path in `qqq_intraday_stock_signal` has been removed.
- Option signal generation now consumes rule-engine decisions.
- Trader can configure `entry_recipe` and `entry_model` via strategy extras.

These modules should output named context fields, not submission decisions.

### 6. Direction Modules

Responsibility:

- choose `LONG`, `SHORT`, or `NONE`

Examples:

- `baseline_direction`
- `fast_drift_direction`
- `elite_direction`

Only one direction module should be active in a given model recipe.

### 7. Opening Filters

Responsibility:

- apply early-window special logic

Examples:

- `early_override_filter`
- `atr_rank_skip_filter`
- `delayed_confirm_filter`
- `openclose_mismatch_filter`

These already map cleanly to the extracted helper file:

- `./strategies/qqq_intraday_v47_logic.py`

### 8. Veto Modules

Responsibility:

- reject entries even if direction is valid

Examples:

- `rollback_veto`
- `rebound_fail_veto`
- `high_vol_sign_confirm_veto`
- `overextension_veto`

### 9. Trader Overlay Modules

Responsibility:

- inject discretionary-style controls without rewriting core alpha

Examples:

- `anti_chase_overlay`
- `shadow_extreme_overlay`
- `reentry_cooldown_overlay`
- `trade_sequence_overlay`

This is where trader customization should mostly live.

### 10. Translation Modules

Responsibility:

- translate accepted signal into execution intent

Examples:

- `qqq_stock_translation`
- `qqq_option_translation`
- `tqqq_sqqq_translation`

This is the most important separation for reuse:

- market logic chooses `LONG/SHORT`
- translation chooses how to express that view

## Trader-Facing Rule Standard

Future strategy configuration should be readable by a trader.

That means each rule must have:

1. `module_id`
2. `rule_type`
3. `enabled`
4. `purpose`
5. `inputs`
6. `outputs`
7. `config knobs`
8. `block reasons`
9. `audit fields`

### Example shape

```yaml
entry_recipe:
  session_module: fast_drift_window
  direction_module: fast_drift_direction
  guards:
    - amp_lock_guard
    - open_orders_guard
    - live_position_guard
    - qqq_freshness_guard
    - qqew_freshness_guard
    - continuity_180s_guard
  filters:
    - early_override_filter
    - atr_rank_skip_filter
    - delayed_confirm_filter
    - rollback_veto
    - rebound_fail_veto
    - shadow_extreme_overlay
    - anti_chase_overlay
  translator: qqq_option_translation
```

This is how traders should think:

- choose one direction engine
- choose required safety guards
- choose optional veto/overlay rules
- choose translation target

## Recommended Rule Schema

We should standardize on these payload contracts.

### EntryContext

Contains:

- current time / session
- QQQ / QQEW prices
- VWAP values
- ATR live / seed / effective
- volume state
- continuity state
- day-state fields
- operational state

### RuleDecision

Contains:

- `allowed: bool`
- `reason`
- `trader_reason_code`
- `payload`

### DirectionDecision

Contains:

- `direction: LONG | SHORT | NONE`
- `exec_hint`
- `confidence`
- `payload`

### TranslationDecision

Contains:

- `instrument_family`
- `execution_target`
- `sizing_mode`
- `risk_payload`

## What Must Not Be Modularized Wrong

Some mistakes will create fake modularity.

### Wrong pattern 1: one mega signal module

If one module still owns:

- data integrity
- direction
- veto
- translation

then nothing is actually pluggable.

### Wrong pattern 2: trader config only changes numbers

If traders can only change thresholds, but cannot choose rule composition, the system is still not modular.

### Wrong pattern 3: option strategy forks stock logic

Market logic must stay upstream.

Option strategy should only add:

- option contract selection
- option quote health rules
- option execution translation

## Recommended Migration Order

We should extract the real QQQ rules in this sequence:

1. session modules
2. data integrity modules
3. context derivation modules
4. direction modules
5. opening filters
6. veto / overlay modules
7. translation modules

This order preserves correctness while gradually increasing configurability.

## Immediate Design Decision

The current `stocks_kernel` abstraction is too shallow to represent the live QQQ strategy.

It should be upgraded from:

- `feature_set + gate_pipeline + policy`

to:

- `context modules + guard modules + direction module + veto modules + translation module`

That is the minimum architecture needed for traders to configure rule combinations without forking strategy classes.
