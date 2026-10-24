# Option Replay Engine Roadmap

## Goal

Evolve the current isolated intraday option replay implementation into a reusable option backtest engine that:

- supports multiple single-leg intraday option strategies first
- gets as close as feasible to real trading behavior
- keeps strategy logic separate from market data, selection, execution, accounting, and audit
- remains isolated from the existing shared backtest engines until the design is proven

This roadmap is intentionally pragmatic. The target is not "perfect simulation"; the target is:

- signal logic parity
- contract-selection parity
- execution behavior that is conservative, explainable, and calibratable with live logs


## Current Baseline

Current isolated implementation:

- [services/backtest/spy_option_0324_replay.py](./services/backtest/spy_option_0324_replay.py)
- [scripts/backtest_spy_option_0324.py](./scripts/backtest_spy_option_0324.py)
- [tests/unit/backtest/test_spy_option_0324_replay.py](./tests/unit/backtest/test_spy_option_0324_replay.py)

Current capabilities already present:

- underlying 1m bar replay
- option chain snapshot lookup with recent fallback
- single-leg long call/put support
- contract selection with delta, DTE, spread, and OI filters
- two-step adaptive-style entry approximation
- position lifecycle with stop/time-based exits
- entry audit and skip-reason output
- minimal strategy interface with a 0324 strategy plugin

Current limitations:

- still too much logic in one module
- execution lifecycle is not yet a full reusable order engine
- selection audit is incomplete
- no general orders/fills/bracket tables yet
- no second strategy to validate the abstraction


## Target Architecture

Phase target directory layout:

```text
services/
  option_replay/
    core/
      types.py
      market_data.py
      selector.py
      execution.py
      accounting.py
      audit.py
      engine.py
    strategies/
      ma10_reject_0324.py
      <second_strategy>.py
```

Compatibility entrypoint:

- keep [scripts/backtest_spy_option_0324.py](./scripts/backtest_spy_option_0324.py)
- internally point it to the new modular engine


## Design Principles

1. Keep this engine isolated from existing shared backtest engines until the abstraction is validated by at least two strategies.
2. Prefer conservative execution assumptions over optimistic fills.
3. Every fallback or degraded-data decision must be explicit in audit output.
4. Strategies express intent only; core handles order state, fills, accounting, and output.
5. Every refactor step must preserve reproducibility on a fixed regression window.


## Phase A: Modularize The Current Skeleton

### Objective

Split the current single-file isolated implementation into reusable core modules without changing 0324 strategy behavior.

### Deliverables

- `services/option_replay/core/types.py`
- `services/option_replay/core/market_data.py`
- `services/option_replay/core/selector.py`
- `services/option_replay/core/execution.py`
- `services/option_replay/core/accounting.py`
- `services/option_replay/core/audit.py`
- `services/option_replay/core/engine.py`
- `services/option_replay/strategies/ma10_reject_0324.py`

### Concrete Tasks

1. Move data classes and config structures out of the current isolated file.
2. Move chain loading and fallback logic into `market_data.py`.
3. Move contract filtering/ranking into `selector.py`.
4. Move pending-entry logic and fill simulation into `execution.py`.
5. Move cash/PnL/MTM helpers into `accounting.py`.
6. Move audit writers/builders into `audit.py`.
7. Keep `OptionReplayStrategy` interface in a stable core location.
8. Move 0324-specific regime/setup/confirm/exit logic into a dedicated strategy module.
9. Keep the CLI stable and produce the same output schema as before.

### Tests

- unit tests still pass without changes in expected outcomes
- py_compile on all new modules
- replay on the fixed regression window still runs successfully

### Acceptance

- no logic duplication between core and strategy modules
- no direct strategy mutation of engine cash/order internals
- CLI still works
- current baseline metrics do not drift materially


## Phase B: Build A Real Execution Core

### Objective

Replace the current scattered pending-entry logic with a reusable order lifecycle simulator.

### Deliverables

- reusable `OrderState`
- reusable `OrderEvent`
- reusable `FillEvent`
- reusable `BracketState`

### Minimum Order States

- `CREATED`
- `SUBMITTED`
- `WORKING`
- `PARTIAL_FILLED`
- `FILLED`
- `CANCELLED`
- `EXPIRED`
- `REPLACED`

### Minimum Order Types

- `LMT`
- `MKTABLE_LMT`
- `STOP`
- `STOP_LIMIT`
- `ADAPTIVE_NORMAL`
- `ADAPTIVE_URGENT`

### Concrete Tasks

1. Introduce a generic order object separate from positions.
2. Represent parent entry orders independently from positions.
3. Only activate bracket children after parent fill.
4. Cancel the sibling child on child fill.
5. Support retry / replace logic for adaptive orders.
6. Add timeouts for unfilled entry orders.
7. Add optional partial fill behavior if quote size is available.
8. Define deterministic fallback behavior when only minute data exists.

### Tests

- order lifecycle contract tests
- OCO cancellation tests
- second-step adaptive fill tests
- timeout and cancel tests
- stale-quote forced-exit tests

### Acceptance

- no direct position creation without a fill event
- no direct position closure without an execution path
- all fills trace back to an order lifecycle


## Phase C: Complete Selection Audit

### Objective

Make contract selection fully explainable and calibratable.

### Deliverables

- `selection_audit.csv`
- selector reject reasons for each candidate
- explicit fallback metadata

### Candidate Audit Fields

- `signal_id`
- `ts`
- `side`
- `grade`
- `regime`
- `candidate_rank`
- `expiry`
- `strike`
- `dte`
- `delta`
- `iv`
- `bid`
- `ask`
- `spread_pct`
- `open_interest`
- `quote_age_sec`
- `used_chain_fallback`
- `selected`
- `reject_reason`

### Concrete Tasks

1. Capture top N candidates for every signal.
2. Record exact reject reason for every non-selected candidate.
3. Record whether the chain came from exact minute or fallback lookup.
4. Record quote age.
5. Record whether delta fallback band was used.

### Tests

- selection audit schema test
- selected contract appears exactly once with `selected=true`
- every rejected candidate has a reason

### Acceptance

- every skipped signal can be explained from selection audit alone
- every filled trade can be traced back to the selected contract row


## Phase D: Build Complete Output Tables

### Objective

Make the replay engine production-grade from a research observability standpoint.

### Required Output Files

- `summary.json`
- `trades.csv`
- `skips.csv`
- `entry_audit.csv`
- `selection_audit.csv`
- `orders.csv`
- `fills.csv`
- `positions.csv`
- `equity_curve.csv`

### Concrete Tasks

1. Add stable IDs: `signal_id`, `order_id`, `fill_id`, `position_id`.
2. Emit `orders.csv` from execution lifecycle.
3. Emit `fills.csv` from actual fills.
4. Emit `positions.csv` from lot/accounting state transitions.
5. Keep CSV schemas deterministic across runs.

### Tests

- output file existence test
- schema contract test
- referential consistency:
  - every fill references an order
  - every trade references fills or position lifecycle

### Acceptance

- a single replay run produces all required artifacts
- outputs can be joined for step-by-step debugging


## Phase E: Validate The Abstraction With A Second Strategy

### Objective

Prove this is a reusable engine, not just a renamed 0324 strategy file.

### Candidate Second Strategies

- ORB breakout buy call/put
- VWAP reclaim continuation
- first pullback after opening impulse

### Concrete Tasks

1. Implement a second strategy plugin using the same core.
2. Do not modify core unless the abstraction is genuinely insufficient.
3. If core must change, document the interface gap explicitly.

### Tests

- second strategy can run through the same CLI wrapper pattern
- shared selector/execution/accounting tests remain unchanged
- second strategy gets its own focused unit tests

### Acceptance

- second strategy runs without copy-pasting core logic
- core changes, if any, improve both strategies


## Phase F: Improve Data Fidelity

### Objective

Reduce the largest remaining realism gap: data coverage and quote quality.

### Desired Fields In `options_quotes_1m`

- `bid`
- `ask`
- `mid`
- `last`
- `last_size`
- `bid_size`
- `ask_size`
- `open_interest`
- `volume`
- `iv`
- `delta`
- `gamma`
- `theta`
- `vega`
- `quote_ts`
- `snapshot_ts`
- `quote_count_in_minute`

### Concrete Tasks

1. Add schema support for optional size and quote-age fields.
2. Use size if available for partial-fill and fill-confidence modeling.
3. Record chain quality score per minute.
4. Distinguish:
   - exact chain snapshot
   - fallback chain snapshot
   - too-stale-to-use

### Tests

- data-quality tests with sparse and missing chains
- fallback behavior tests
- stale-data rejection tests

### Acceptance

- missing or stale data never silently degrades into optimistic behavior
- all degradations are explicit in audit output


## Phase G: Live Calibration

### Objective

Make the execution model calibratable against real broker behavior.

### Inputs

- IBKR execution logs
- order submit / replace / fill timestamps
- actual fill prices
- actual misses and cancels

### Concrete Tasks

1. Define a live-log ingestion schema.
2. Build a calibration comparison report:
   - selected contract drift
   - fill price drift
   - missed-entry drift
   - holding-time drift
3. Estimate execution parameters:
   - normal adaptive chase
   - urgent adaptive chase
   - timeout behavior
   - stop slippage penalty

### Tests

- calibration data schema tests
- comparison report generation tests

### Acceptance

- execution parameters are no longer hand-tuned only
- backtest/live drift is measurable and reviewable


## Test Strategy

### Unit Tests

- selector behavior
- execution behavior
- accounting behavior
- audit record generation
- strategy-level signal/confirm/exit rules

### Contract Tests

- `OptionReplayStrategy` interface contract
- output schema contract
- order state transition contract

### Integration Tests

- one-day QQQ replay with real bars + available option chain data
- multi-week QQQ replay regression window
- second strategy replay through the same core

### Regression Window

Use these windows consistently:

1. one-day smoke window
2. one-week stability window
3. full regression window currently used:
   - `2026-02-02` through `2026-03-24`


## Acceptance Checklist

### Architecture

- [ ] Core and strategy logic are in separate modules
- [ ] At least two strategies run on the same core
- [ ] CLI compatibility is preserved

### Execution Realism

- [ ] No direct position creation without an order fill
- [ ] No direct position deletion on missing exit quote
- [ ] Bracket lifecycle exists in orders/fills output
- [ ] Retry / timeout behavior is explicit

### Data Realism

- [ ] Exact-vs-fallback chain use is recorded
- [ ] Quote age is recorded
- [ ] Missing chain data does not silently optimistic-fill

### Auditability

- [ ] Every skipped signal has an explicit reason
- [ ] Every filled trade maps back to selected contract + order + fill
- [ ] Every output schema is stable and test-covered

### Reusability

- [ ] A second strategy uses the same core without copy-paste
- [ ] Core changes are not 0324-specific

### Calibration Readiness

- [ ] Execution parameters are isolated in config
- [ ] Audit outputs contain enough detail to compare with live logs


## Recommended Execution Order

1. Phase A: modularize current skeleton
2. Phase B: real execution core
3. Phase C: full selection audit
4. Phase D: stable output tables
5. Phase E: second strategy validation
6. Phase F: data fidelity upgrades
7. Phase G: live calibration


## Immediate Next Task

Start Phase A by creating the new `services/option_replay/core/` package and moving:

- types/config
- market data lookup
- selector logic
- audit builders
- strategy plugin

Do not change the CLI contract during this step.
