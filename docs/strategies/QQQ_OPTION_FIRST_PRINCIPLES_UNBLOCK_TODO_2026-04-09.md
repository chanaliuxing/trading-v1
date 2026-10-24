# QQQ Option First-Principles Unblock Todo (2026-04-09)

## Purpose

This document answers one concrete question:

How do we unblock the platform so that `QQQ option` can reliably reuse the same `QQQ` underlying truth and the same `QQQ` stock signal, while still becoming a truly pluggable option architecture?

This is not a generic cleanup list.
It is the focused unblock plan for the current production gap.

## First-Principles Goal

The system must separate three concerns:

1. `underlying truth`
2. `signal authority`
3. `execution expression`

For `QQQ`:

1. the underlying truth should be computed once
2. the stock strategy should remain the live signal authority during `P0`
3. the option strategy should express the same signal through option execution

The target architecture is:

- one shared `QQQ` truth plane
- one live signal authority
- multiple execution consumers

## Current Critical Review

### Critical blocker 1: shared underlying stream fanout is incomplete

Current code reuses the same polling task for the same `(symbol, resolution)`, but only the first subscriber is actually called.

That means:

1. the stream is reused physically
2. the callback is not distributed logically

This directly blocks the requirement:

- stock and option using the same `QQQ` data stream in the same process

### Critical blocker 2: stock signal artifact is only process-local

Current stock signal publication lives in module memory.

That means:

1. same-process stock->option follow works
2. cross-process stock->option follow does not work

This blocks the long-term requirement:

- truly pluggable strategies regardless of process topology

### Critical blocker 3: upstream live mode still depends on local replay evaluation for observability context

The option plugin still runs local `QQQIntradayRuleEngine.evaluate(...)` before consuming upstream signal.

This is acceptable as temporary observability scaffolding, but not as final architecture.

Why:

1. live authority should not be coupled to replay readiness
2. explain context should come from the same truth plane as live signal

### Critical blocker 4: no formal shared `QQQ` truth fabric object exists yet

Today multiple parts still read raw feature dictionaries instead of a formal shared truth contract.

That means:

1. we have compatibility
2. we do not yet have clean platform boundaries

## Non-Negotiable Invariants

1. `./strategies/qqq_intraday_strategy.py` remains the protected stock baseline during this rollout.
2. `QQQ option` live mode has exactly one entry authority.
3. `shadow replay` may produce parity evidence, but may not submit live entries.
4. option chain data may affect contract selection and execution only.
5. stock truth may not be inferred from option chain state.
6. shared signal artifacts may not contain order ownership or broker execution state.
7. one underlying subscription path must serve multiple consumers inside one process.
8. the architecture must later support cross-process sharing without changing trader-facing strategy definitions.

## Optimal Solution

The optimal solution is a three-layer platform design.

### Layer 1: Underlying Truth Fabric

Create a first-class shared object for `QQQ` market truth.

It should contain:

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

This fabric must be:

1. readable by stock and option consumers
2. built once per process for each `(underlying, resolution family)`
3. upgradeable later to a cross-process state layer

### Layer 2: Signal Authority Layer

The stock strategy publishes a normalized signal artifact.

The artifact means:

- market interpretation says `LONG` or `SHORT`

The artifact must not mean:

- place this stock order

This is the trader-facing truth bridge.

### Layer 3: Execution Expression Layer

Two strategies may consume the same truth and same signal:

1. stock expression:
   - `LONG -> TQQQ`
   - `SHORT -> SQQQ`
2. option expression:
   - `LONG -> CALL`
   - `SHORT -> PUT`

This is where instrument-specific behavior belongs.

## Phased Todo

## P0: Remove Immediate Blockers

- [x] P0.1 Fix shared stream fanout so all registered consumers receive the same `QQQ` bar callback.
- [x] P0.2 Add tests that prove two strategies in the same process both execute on the same shared `QQQ` stream.
- [x] P0.3 Keep `QQQ option` live mode on `upstream_stock_signal`.
- [x] P0.4 Keep `independent_rule_replay` shadow-only by default and prove it cannot submit live entries.
- [x] P0.5 Ensure downstream result logs always include one explicit result code per evaluated minute.
- [x] P0.6 Preserve stock baseline behavior and keep existing `QQQ` stock regression tests green.

## P1: Formalize Shared Truth Fabric

- [x] P1.1 Introduce a typed `QQQ` underlying truth fabric contract instead of raw ad-hoc feature dict reads.
- [x] P1.2 Route both stock and option signal consumers through the same fabric accessor.
- [x] P1.3 Remove unnecessary replay-eval dependency from `upstream_stock_signal` live path.
- [x] P1.4 Keep replay mode reading the same truth fabric instead of a private duplicate runtime.

## P2: Cross-Process Signal And Truth Plane

- [x] P2.1 Replace process-local signal artifact storage with a cross-process real-time state layer.
- [x] P2.2 Publish `QQQ` stock signal artifact into that state layer with TTL and source identity.
- [x] P2.3 Make option consumers read from the shared state layer first-class, not from module globals.
- [x] P2.4 Add startup and heartbeat diagnostics that clearly show signal-plane connectivity state.

## P3: Trader-Facing Plugability

- [x] P3.1 Keep one `signal_source` contract for all option strategies.
- [x] P3.2 Allow trader config to choose between:
  `upstream_stock_signal`
  `independent_rule_replay`
- [x] P3.3 Keep execution, contract selection, and exit policy independent from signal source implementation details.
- [x] P3.4 Add platform docs that explain how to build new option strategies from reusable truth/signal/expression modules.

## P4: Signal Plane Reliability (Trader First)

- [x] P4.1 Redis failure is not one-shot fatal: implement retry with exponential backoff.
- [x] P4.2 Keep `auto` fallback to memory explicit and observable (`signal_plane_state` in heartbeat).
- [x] P4.3 Add remote clear capability for signal plane to avoid stale day pollution.
- [x] P4.4 Add opening acceptance metrics for signal-plane health/degraded ratio.

## Stop Conditions

Do not mark this architecture as complete until:

1. same-process stock and option can share one `QQQ` truth stream
2. cross-process stock and option can share one `QQQ` signal plane
3. option live mode can follow stock signal without replay coupling
4. every non-trade minute remains explainable

## Reference

- [QQQ_OPTION_SIGNAL_PLUGABILITY_GUIDE_2026-04-09.md](./docs/strategies/QQQ_OPTION_SIGNAL_PLUGABILITY_GUIDE_2026-04-09.md)
