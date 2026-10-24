# QQQ Option Signal Architecture Optimization

## Purpose

This document answers one platform question:

How should the repository evolve so that:

1. `QQQ` option can reliably follow the real `QQQ` stock signal
2. future option strategies become truly pluggable
3. traders can choose between multiple signal sourcing modes without forking strategy code
4. stable stock logic remains the protected source of truth

This is an architecture optimization document, not a single-bug fix note.

Related context:

- `./docs/strategies/QQQ_STOCK_SIGNAL_OPTION_STRATEGY_DESIGN.md`
- `./docs/strategies/QQQ_INTRADAY_ENTRY_RULES_MODULARIZATION.md`
- `./docs/guides/OPTION_STRATEGY_FIRST_PRINCIPLES_MASTER_PLAN.md`

## Executive Conclusion

Do not choose only one of the two approaches.

The correct platform design is:

1. define a first-class `signal_source` contract
2. support both:
   - `upstream_stock_signal`
   - `independent_rule_replay`
3. make both consume the same shared market state / feature fabric
4. let each option strategy instance choose which signal source to use
5. keep `QQQ` live production on `upstream_stock_signal` first
6. keep `independent_rule_replay` as parity, research, fallback, and future generalized mode

Short version:

- for production reliability, `QQQ option` should first buy option from the real stock signal
- for platform completeness, the system should also support full rule replay from shared real-time features
- both modes must live under one plugin architecture, not two unrelated strategy families

## Review And Critical Corrections

The first draft direction was correct, but it had 5 important gaps that would still allow the implementation to drift.

### Gap 1: it did not explicitly protect the stable `QQQ` stock path

Correction:

1. `P0` must not change the live trading logic of `./strategies/qqq_intraday_strategy.py`
2. `P0` may only add:
   - normalized signal publication
   - diagnostics
   - feature export hooks
3. any change to stock-side entry or exit semantics is out of scope for this architecture rollout

Why:

- the stable stock strategy is the baseline, not the refactor target

### Gap 2: it did not define a single-writer rule for live entries

Correction:

At any given time, an option strategy instance must have exactly one live entry authority:

1. either `signal_source.mode = upstream_stock_signal`
2. or `signal_source.mode = independent_rule_replay`

but never both as live writers.

If both modes run simultaneously:

1. one may be `live`
2. the other must be `shadow_only`

Why:

- otherwise the same minute can produce duplicate entries from two equally valid code paths

### Gap 3: it allowed ambiguous fallback semantics

Correction:

`fallback_mode` must be explicit and must not silently open trades through a backup path unless the strategy definition opts into it.

Default live rule:

1. primary mode decides live trading
2. fallback mode only emits audit/shadow evidence
3. no automatic cross-mode failover entry in the same minute by default

Why:

- silent failover creates unexplainable fills
- traders need deterministic entry authority

### Gap 4: it did not separate signal artifacts from order artifacts strongly enough

Correction:

The stock strategy must publish a reusable signal artifact, not an order intent surrogate.

Signal artifact must mean:

1. "market interpretation says enter"
2. not "submit this stock order envelope"

Why:

- option followers must not depend on stock execution details
- one signal may map to multiple option expressions

### Gap 5: it did not define what must be shared in memory versus what may remain private

Correction:

Shared in-memory state must include only reusable market truth:

1. underlying prices and derived features
2. freshness and continuity state
3. normalized signal artifacts

Private strategy state may still include:

1. option position state
2. strategy-specific pending orders
3. strategy-specific contract selection state
4. strategy-specific exit lifecycle

Why:

- otherwise the "shared fabric" expands into a new monolith

## Non-Negotiable Invariants

These invariants are mandatory for implementation.

1. `QQQIntradayStrategy` remains the live market truth baseline during `P0`.
2. `QQQ option` must not infer stock fills; it must consume stock signal artifacts.
3. one option strategy instance has one live signal writer.
4. `signal_source` selection is config-driven, not code-fork-driven.
5. execution, contract selection, position policy, and flatten policy must not care where the signal came from.
6. every non-trade minute must be explainable by audit with a source-mode-aware reason.
7. shared memory contains market truth, not cross-strategy order ownership.
8. underlying market data is subscribed once per process/fabric, then shared across consumers.
9. stock market truth must not be inferred from option chain data in production.

## First-Principles Analysis

### What problem are we actually solving

A trader does not care whether the code path is called "stock strategy" or "option strategy".

A trader cares about 4 things:

1. whether the market interpretation is correct
2. whether the option expression is correct
3. whether execution is safe
4. whether the system is explainable and configurable

Therefore the platform must separate:

1. market interpretation
2. instrument expression
3. execution and risk

If these three are mixed in one strategy file, the system cannot become modular.

### Why the current `QQQ option` path failed

Today the repository already reuses `QQQIntradayRuleEngine`, but the live `QQQ option` strategy still did not trade when the stock strategy traded.

That means:

1. code reuse alone is not enough
2. signal parity requires input parity
3. input parity requires shared real-time state, not only shared logic classes

This is the key architectural lesson.

The true source of parity is:

`same market state -> same signal decision`

not:

`same Python rule engine name -> same live result`

## Two Valid Signal Modes

### Mode A: Upstream Stock Signal

Definition:

- stock strategy produces a normalized signal event
- option strategy consumes that event and converts it into option entry

Shape:

- stock `LONG` -> option `CALL`
- stock `SHORT` -> option `PUT`

Strengths:

1. safest for production
2. directly inherits the proven stock truth
3. minimal risk of signal drift
4. easiest to explain to trader
5. easiest to audit: "stock entered, option followed"

Weaknesses:

1. depends on upstream stock signal publisher
2. option strategy is no longer a fully independent alpha engine
3. cannot by itself answer "what if option strategy wanted same features but different thresholds"

Best use:

- first live deployment of `QQQ option`
- stable parity path
- paper/live rollout where missing entries are more harmful than architectural purity

### Mode B: Independent Rule Replay

Definition:

- option strategy reads shared real-time market features
- option strategy independently runs the same rule graph
- only execution layer differs

Strengths:

1. fully modular
2. generalizes to future strategies
3. supports "same feature family, different trader recipe"
4. supports research and replay without depending on stock runtime

Weaknesses:

1. harder to make live-identical
2. requires full feature parity, not partial parity
3. more likely to drift if state sourcing is not centralized

Best use:

- replay and parity verification
- strategy research
- future trader-composed variants
- eventual mature platform mode after shared feature fabric is complete

## Architecture Decision

The platform should support both modes under one contract:

```text
Market State Fabric
    -> Signal Source
        -> Direction Mapping
            -> Contract Selection
                -> Execution / Position / Risk / Flatten
```

The critical design move is:

`signal source` becomes a configurable module, not a hardcoded implementation detail.

## Recommended Target Architecture

### Layer 1: Shared Market State Fabric

This layer becomes the only live source of truth for underlying state.

Responsibilities:

1. ingest real-time `QQQ`, `QQEW`, and other required symbols once
2. compute trader-facing normalized features once
3. hold short-horizon in-memory history once
4. expose freshness, continuity, ATR, VWAP, volume, derived context once
5. allow multiple strategies to read the same state

This layer should not know about:

1. option contract selection
2. order placement
3. strategy-specific capital limits

It should only know:

1. underlying market data
2. derived features
3. signal input context

### Layer 1A: Data Plane Split

The shared fabric must be split conceptually into two data planes:

1. underlying market data plane
2. option market data plane

#### Underlying market data plane

This is the source of truth for:

1. stock strategies
2. stock-driven option strategies
3. replay-driven option strategies
4. any trader rule that reasons about the underlying

Typical payload:

1. trade/quote-derived spot
2. bars
3. VWAP
4. ATR
5. continuity
6. freshness
7. cross-symbol context such as `QQEW`

#### Option market data plane

This is the source of truth for:

1. contract selection
2. option quote freshness
3. spread gating
4. option execution and mark logic

Typical payload:

1. chain snapshots
2. bid/ask/mid
3. greeks when available
4. quote age
5. liquidity metadata

The key rule is:

1. stock truth comes from the underlying data plane
2. option execution truth comes from the option data plane

The option data plane may extend the underlying plane, but it must not replace it.

### Layer 1B: Option-First, Stock-Later Scenario

The architecture must also support this sequence:

1. an option strategy exists first
2. later a stock strategy is added

In that case, the system should still avoid opening a duplicate underlying data channel.

Correct design:

1. subscribe to underlying market data once
2. publish it into shared fabric once
3. let both stock and option strategies consume the same underlying state
4. subscribe to option chain data only for consumers that actually need it

This means:

1. adding a later stock strategy should add a new consumer, not a second underlying feed
2. adding a later option strategy should add option-chain consumption, not rebuild stock features privately

### Layer 1C: What Option Data Can And Cannot Reuse

If option strategy exists first, stock strategy can reuse:

1. the shared underlying feed used by the option strategy
2. the shared derived underlying features if they are produced from the underlying feed

Stock strategy should not use as primary truth:

1. option chain snapshots
2. option greeks
3. inferred underlying from option prices

Why:

1. option chain is an execution market, not the cleanest market-interpretation truth
2. option quotes can be sparse, wide, stale, or strike-dependent
3. deriving stock truth from option chain introduces unnecessary noise and hidden coupling

Therefore the invariant is:

`reuse the same underlying data channel`

not:

`derive stock truth from option chain data`

### Layer 2: Signal Source Contract

Define a new platform contract:

```text
SignalSource
```

Required output:

1. `allowed`
2. `direction`
3. `signal_family`
4. `source_mode`
5. `source_instance_id`
6. `source_ts`
7. `context_payload`
8. `block_reason`

Recommended implementations:

1. `UpstreamStockSignalSource`
2. `RuleReplaySignalSource`

#### 2.1 UpstreamStockSignalSource

Reads:

1. published stock signal event
2. optional market state snapshot for diagnostics

Returns:

1. exact direction from stock path
2. exact source timestamp
3. exact source trigger id / decision id

#### 2.2 RuleReplaySignalSource

Reads:

1. shared feature fabric

Runs:

1. `QQQIntradayRuleEngine`
2. chosen entry recipe

Returns:

1. translated option-side direction
2. trader-readable block reason if rejected

### Layer 3: Direction Mapping

This remains separate from signal sourcing.

Why:

- stock direction truth and option expression truth are different concerns

Examples:

1. `LONG -> CALL`
2. `SHORT -> PUT`
3. future variants:
   - `LONG -> CALL debit spread`
   - `SHORT -> PUT runner`

### Layer 4: Contract Selection

Once signal is valid, contract selection remains independent:

1. DTE
2. delta band
3. spread guard
4. quote freshness
5. open interest / liquidity

This means the same `QQQ` signal can express into:

1. single call
2. single put
3. 0DTE scalp contract
4. 1DTE carry contract
5. spread structures later

### Layer 5: Execution / Position / Risk / Flatten

This layer must not know whether signal came from stock event or replay.

It should only consume the normalized signal contract plus selected contract.

That is the point of modularity.

## Why This Is Better Than Picking Only One Mode

### If we choose only upstream stock signal

We get reliability, but the platform stalls.

Problems:

1. no independent replay mode
2. hard to build non-stock-derived future strategies
3. weak trader composability

### If we choose only independent rule replay

We get elegance, but near-term live reliability suffers.

Problems:

1. requires full real-time feature fabric first
2. current system has already shown this is where parity breaks
3. higher live rollout risk for `QQQ`

### If we support both under one contract

We get:

1. immediate production safety
2. long-term platform completeness
3. no strategy-family fork
4. one trader-facing configuration model

This is the best first-principles answer.

## Recommended Production Policy

### For `QQQ` option live now

Use:

- `signal_source.mode = upstream_stock_signal`

Reason:

1. `QQQ` stock strategy is already the stable truth
2. user requirement is exactly "QQQ stock signal triggers option buy"
3. this minimizes live drift

### For `QQQ` option shadow/parity

Also run:

- `signal_source.mode = independent_rule_replay`

Purpose:

1. verify shared feature fabric quality
2. detect drift between replay and stock signal
3. prepare future generalized modular mode

This can be:

1. shadow-only
2. audit-only
3. acceptance-only

without affecting live entry decisions

## Proposed Trader-Facing Configuration

Add a strategy-level field group like:

```yaml
signal_source:
  mode: upstream_stock_signal
  source_strategy_id: qqq_intraday_001
  source_signal_family: qqq_intraday_entry
  require_source_alive: true
  max_signal_age_sec: 5
  fallback_mode: independent_rule_replay
```

Alternative:

```yaml
signal_source:
  mode: independent_rule_replay
  feature_fabric_id: qqq_intraday_fabric_v1
  recipe_id: qqq_v47_fast_drift_v1
```

This lets traders choose behavior without changing the execution stack.

## Proposed Platform Contracts

### 1. Market State Fabric Contract

Suggested object identity:

- `fabric_id`
- `underlying`
- `session_date`
- `source_symbols`

Required payload:

1. `spot`
2. `vwap`
3. `qqew_now`
4. `qqew_vwap_now`
5. `v3m_signed`
6. `atr_live`
7. `atr_seed`
8. `atr_eff`
9. `vol_surge_ratio`
10. `distance_from_vwap_atr`
11. `data_age_qqq_sec`
12. `data_age_qqew_sec`
13. `continuity_180s_ok`
14. `close_by_minute`
15. `session_open_price`
16. `day_position_rank`

### 2. Normalized Signal Contract

Suggested object identity:

- `signal_id`
- `signal_family`
- `signal_source_mode`
- `source_strategy_id`

Required payload:

1. `allowed`
2. `direction`
3. `option_side`
4. `reason`
5. `trigger_ts`
6. `source_decision_id`
7. `underlying_context`
8. `explain_payload`

### 3. Signal Publication Contract

For stock-to-option follow mode, the stock strategy should publish a durable in-memory event:

1. `strategy_id`
2. `trigger_id`
3. `decision_id`
4. `minute`
5. `direction`
6. `instrument`
7. `entry_allowed`
8. `context_payload`

This is not an order intent.
This is a reusable market decision artifact.

## Required Refactor Directions

### A. Extract `QQQ` market state from strategy class

Current problem:

- `QQQIntradayStrategy` privately owns too much real-time state

Target:

- move reusable real-time state building into a shared `QQQIntradayFeatureFabric`

This fabric should be readable by:

1. stock strategy
2. option strategy
3. shadow replay tools
4. diagnostics

### B. Extract stock signal publication

Current problem:

- stock strategy decides and immediately acts, but does not expose a clean reusable signal artifact

Target:

- stock strategy emits a normalized signal event before stock/option execution branches consume it

### C. Keep `OptionKernel` execution-focused

Current problem:

- `OptionKernel` currently tries to be both signal runtime and execution runtime

Target:

- `OptionKernel` should consume normalized signal objects
- signal sourcing becomes a pluggable upstream stage

This keeps option architecture clean.

### D. Add signal-source observability

Every option strategy heartbeat and audit row should include:

1. `signal_source_mode`
2. `signal_source_status`
3. `signal_source_age_sec`
4. `signal_source_strategy_id`
5. `fabric_id`
6. `block_reason`

Without this, future failures will keep looking like "why no trade?" with no answer.

## Mapping To Current Repository

This architecture should be implemented by extending current modules, not by creating a second platform.

### Existing modules to reuse

1. execution shell:
   - `./strategies/options_kernel/base.py`
2. plugin contract:
   - `./strategies/options_kernel/plugin.py`
3. strategy definition registry:
   - `./strategies/options_kernel/modules/strategy_definitions.py`
4. current `QQQ` option plugin:
   - `./strategies/options_plugins/qqq_intraday_option.py`
5. current `QQQ` stock logic source:
   - `./strategies/qqq_intraday_strategy.py`
6. current modular rule engine:
   - `./strategies/qqq_intraday_rules/`

### Recommended insertion points

#### 1. `QQQIntradayStrategy`

Add:

1. normalized signal publication hook
2. optional feature-export hook

Do not add:

1. option contract selection
2. option execution logic
3. option ownership logic

#### 2. `QQQIntradayOptionPlugin`

Add:

1. `signal_source` selection logic
2. source-aware audit fields
3. source-mode-aware block reasons

Keep:

1. direction mapping
2. contract selection
3. option execution and position semantics

#### 3. `StrategyDefinition`

Add explicit schema support for:

1. `signal_source_mode`
2. `source_strategy_id`
3. `fallback_mode`
4. `feature_fabric_id`

This must remain config-driven.

#### 4. Shared runtime service layer

If a reusable in-memory fabric is added, it should live in a shared service/module layer, not inside a specific strategy plugin.

Reason:

- multiple strategies must read it

## Anti-Patterns To Avoid

The following would look fast, but are architecturally wrong.

### Anti-pattern 1: option strategy reading stock orders instead of stock signals

Wrong:

- option follows "stock order submitted"

Correct:

- option follows "stock market interpretation says enter"

### Anti-pattern 2: cloning stock strategy internals into option plugin

Wrong:

- copy buffers, freshness logic, ATR state, and signal state into option plugin

Correct:

- extract reusable market state once

### Anti-pattern 3: two live signal paths racing each other

Wrong:

- upstream stock signal and replay signal both able to submit entries

Correct:

- one live writer, one optional shadow writer

### Anti-pattern 4: hidden live fallback

Wrong:

- source A fails, source B submits live trade without explicit operator intent

Correct:

- fallback is explicit and visible in audit, defaulting to non-live

### Anti-pattern 5: opening a second underlying feed when adding another strategy

Wrong:

- option strategy opens one `QQQ` feed
- later stock strategy opens another `QQQ` feed for the same process

Correct:

- one underlying feed
- multiple strategy consumers

### Anti-pattern 6: using option chain as stock-truth substitute

Wrong:

- stock or stock-driven signal logic is computed primarily from option chain state

Correct:

- stock truth comes from underlying market data
- option chain remains an execution/selection layer

## What Should Be Live, Shadow, and Research

### Live

1. `QQQ` stock strategy
2. `QQQ option` with `upstream_stock_signal`

### Shadow

1. `QQQ option` with `independent_rule_replay`
2. drift detector between stock signal and replay signal

### Research

1. new recipes
2. alternate direction mapping
3. alternate contract expression

This gives operational clarity.

## Decision Matrix

| Dimension | Upstream Stock Signal | Independent Rule Replay |
|---|---|---|
| Live reliability | Highest | Medium until fabric is complete |
| Signal drift risk | Lowest | Higher |
| Platform modularity | Medium | Highest |
| Research flexibility | Medium | Highest |
| Rollout speed | Fastest | Slower |
| Best role | Production | Shadow / generalized future mode |

## Final Recommendation

The best architecture is:

1. build a shared `QQQ` real-time feature fabric
2. define a first-class `signal_source` contract
3. support both `upstream_stock_signal` and `independent_rule_replay`
4. run live `QQQ option` on upstream stock signal first
5. run replay mode in parallel for parity and future platform evolution

This satisfies both goals:

1. near-term trading correctness
2. long-term trader-configurable modularity

## Implementation Roadmap

### P0

1. add `signal_source` contract to option strategy definition / plugin extras
2. add `upstream_stock_signal` implementation
3. publish normalized signal artifact from `QQQIntradayStrategy`
4. switch `qqq_intraday_option_v1_001` live path to consume upstream signal
5. add audit fields proving source mode and source trigger

### P1

1. extract `QQQIntradayFeatureFabric` from stock strategy internals
2. make stock and option both read the same feature fabric
3. implement `independent_rule_replay` signal source on top of fabric
4. add drift report between stock signal and replay signal

### P2

1. generalize feature fabric pattern to future underlying families
2. expose trader-facing templates for:
   - stock-signal-follow option
   - replay-driven option
   - hybrid compare mode
3. add acceptance checklist per signal source mode

## Acceptance Standard

This architecture is only considered complete when:

1. `QQQ option` can trade live using `upstream_stock_signal`
2. every live non-trade minute has an explicit signal-source reason
3. replay mode can be run independently from the same shared feature fabric
4. the trader can switch signal source mode by config, not code fork
5. execution / position / contract selection modules do not care which signal source was chosen

Only then is the system truly pluggable.
