# Strategy Execution Reliability Review

Date: 2026-05-12

## Executive Verdict

The recent fixes are directionally correct, but they are not sufficient proof
that strategy execution is healthy.

They address three local failure modes:

- NVDA option quote health now checks the strategy's DTE range instead of a
  hard-coded 0DTE universe.
- Validator DLB now has a system-level minimum tolerance.
- Non-manual, non-stale `system_health:recovering` no longer creates a
  self-locking hard blocker.

Those changes reduce false blocks, but they do not yet prove the full execution
chain is correct. The next priority should be an evidence gate that traces:

`strategy signal -> decision envelope -> validator -> submit_intent -> UOM delegate -> execution gate -> paper/shadow broker -> ack/fill attribution`

No live canary should proceed until this path is observable with real
paper/shadow runtime samples.

## First Principles

1. Strategy decisions must be based on a tradable market state that matches the
   strategy's actual contract universe.
2. Execution validation must distinguish signal age, queue delay, quote age,
   system readiness, and broker readiness.
3. Health recovery must not self-lock, but hard safety gates must remain
   independent and fail closed.
4. Entry, exit, flatten, and protection routing must be explicit. Execution
   must not infer intent ownership or intent type from side, symbol, or active
   strategy state.
5. Unit tests prove contracts. Runtime paper/shadow artifacts prove the system
   is actually operating under those contracts.

## Review: NVDA `quote_missing`

### Current State

`strategies/nvda_option_smoke_strategy.py` overrides `_latest_quote_status()` and
passes `dte_min` / `dte_max` from strategy extras into `latest_quote_status()`.
This fixes the immediate false `quote_missing` caused by a parent path checking
0DTE quotes while NVDA trades a wider DTE range.

### Assessment

The hotfix is reasonable and low risk because it changes data-health evaluation,
not alpha entry/exit conditions. It does not loosen profit logic, stop logic, or
signal thresholds.

The architectural placement is not ideal. Quote-health policy should be a common
option contract/data policy derived from strategy parameters, not a per-strategy
override. Otherwise, each option strategy can drift into its own quote-health
implementation and create another false `quote_missing` class.

### Required Next Step

Move this responsibility into a shared option data policy:

- `dte_min`
- `dte_max`
- preferred DTE or expiry policy
- underlying
- quote freshness threshold
- minimum usable bid/ask/mid requirements

Regression tests should prove that NVDA, QQQ, SPY, and Vegas strategies evaluate
quote health against their own contract universe.

### Evidence Gate Requirement

The NVDA-specific `_latest_quote_status()` override is acceptable only as a
short-term mitigation. It must not become the production standard for option
data health.

The execution reliability evidence gate must quantify quote failures by:

- strategy
- underlying
- contract policy
- quote source
- quote age

If `quote_missing` concentrates in one option strategy or underlying, the next
batch should move that strategy onto a shared option data/contract policy before
any strategy parameter tuning. Per-strategy hard-coded quote fallback is a
temporary repair surface, not a long-term architecture.

## Review: Validator `min_dlb_ms`

### Current State

`services/decision/validator.py` computes:

```text
effective_dlb_ms = max(configured_min_dlb_ms, envelope_dlb_ms)
```

The current config sets `decision_pipeline.validator.min_dlb_ms` to `3000`.

### Assessment

The direction is valid: a production execution system should not reject good
signals solely because of normal internal queueing or scheduling latency.

The remaining risk is that the current DLB check still conflates distinct
concepts:

- signal age
- decision envelope age
- system queue delay
- validator delay
- market quote age

Widening DLB without measuring those components can allow stale price decisions
to pass. Quote freshness checks still exist and must remain hard blockers,
especially for options and marketable orders.

### Required Next Step

Add runtime diagnostics for each approved/rejected decision:

- `signal_ts`
- `decision_ts`
- `validation_ts`
- `submit_intent_ts`
- `decision_age_ms`
- `queue_delay_ms`
- `validation_delay_ms`
- `market_quote_age_sec`
- `effective_dlb_ms`
- `dlb_exceeded`
- `quote_freshness_passed`

Rules:

- Queue delay may justify a bounded DLB cushion.
- Stale quote must still reject.
- Stale signal must still reject.
- Further DLB changes require p95/p99 paper/shadow evidence and fill-quality
  review.

## Review: `system_health:recovering`

### Current State

`tool/ops/run_runtime_operations_watchdog.py` ignores non-stale
`system_health` state `recovering` unless the reason is
`manual_recovery_required`.

### Assessment

This is architecturally sound. `recovering` should not by itself deadlock
production readiness. The real safety gates are:

- broker truth missing/stale/not broker-backed
- position safety no-go
- active quarantine
- owner or ledger mismatch
- manual recovery required
- runtime code stale
- unknown/manual broker order requiring review

If those gates are green, `recovering` should not form a circular blocker where
the system cannot become healthy because it is already marked recovering.

### Required Next Step

Normalize health categories:

- `RECOVERING_SOFT`
- `MANUAL_RECOVERY_REQUIRED`
- `SAFETY_NO_GO`
- `RUNTIME_STALE`
- `BROKER_TRUTH_UNAVAILABLE`

Production state should show the category explicitly and avoid turning soft
recovery into per-strategy entry failures.

## Additional Current Risk: Execution Delegate Routing

The current diff includes explicit UOM delegate routing in
`services/decision/pipeline.py`. That is likely necessary if entry and exit
intents were reaching the wrong delegate, but it is a behavior-critical change.

This path needs explicit tests before any canary decision:

- entry/open intents route to `submit_entry`
- exit/reduce/close intents route to `submit_exit`
- flatten/protection intents do not route through generic entry
- unknown intent kind fails closed
- emitted audit records include the selected delegate method

If strategies still appear not to execute, this routing layer is one of the
first places to inspect.

## Why Previous Work Did Not Fully Solve Strategy Execution

The completed OMS and production-readiness contracts mostly prevent dangerous
misattribution, stale ownership, and unsafe broker-state mutation. They do not
guarantee that a strategy produces a valid order path.

The remaining problem is conversion evidence:

1. Did the strategy produce a signal?
2. Did it create a decision envelope?
3. Did validator reject it, and why?
4. Did approved intent reach the UOM delegate?
5. Did reservation/execution gate accept it?
6. Did paper/shadow broker receive it?
7. Did ack/fill attribution complete?
8. Did PnL replay receive an owner-complete fill?

Without this chain, local fixes can be correct while runtime execution remains
broken.

## Execution Reliability Plan

### Phase 0: Freeze Safety Scope

Do not start live canary and do not widen strategy parameters while execution
evidence is incomplete.

Required constraints:

- no live IBKR connection
- no real order/cancel/modify
- no auto-cancel unknown broker orders
- no auto-flatten unknown owner positions
- no symbol-only attribution
- no `order_ref` prefix or substring ownership

### Phase 1: Strategy Execution Evidence Gate

Create a runtime report that joins strategy, validator, UOM, execution, and
paper/shadow broker audit records by:

- `trace_id`
- `decision_id`
- `intent_id`
- `client_order_id`
- `strategy_instance_id`
- `lot_id`

The report must show drop stage counts:

- no signal
- decision not emitted
- validator rejected
- approved but no submit intent
- reservation rejected
- execution gate rejected
- delegate routing failed
- paper/shadow broker submit missing
- ack missing
- fill missing
- attribution incomplete

### Phase 2: Shared Option Data Policy

Move option quote-health lookup out of NVDA-specific override and into a common
policy layer. Keep the NVDA hotfix until the shared path has coverage.

Acceptance:

- NVDA DTE 1-7 fresh quote does not produce false `quote_missing`.
- 0DTE strategies still require their 0DTE quote universe.
- Missing/stale option quotes still block entry.

### Phase 3: Validator Latency Decomposition

Instrument and test:

- signal age
- queue delay
- validator delay
- quote age
- DLB limit
- rejection reason

Acceptance:

- System queue delay is visible.
- Stale quote rejects even when DLB cushion is active.
- Stale signal rejects even when quote is fresh.
- Further DLB changes require measured p95/p99 evidence.

### Phase 4: Execution Routing Contract

Make intent kind and order role explicit inputs to UOM routing. Do not infer
entry/exit from symbol, side, or active strategy.

Acceptance:

- Entry routes to entry delegate.
- Exit routes to exit delegate.
- Flatten/protection routes to owner-scoped paths.
- Unknown or incomplete intent fails closed with audit evidence.

### Phase 5: Health Recovery Contract

Keep soft recovery from self-locking while preserving hard blockers.

Acceptance:

- `recovering` without stale/manual reason does not block alone.
- `manual_recovery_required` blocks.
- broker truth, position safety, quarantine, runtime stale, and ledger mismatch
  remain hard blockers.

### Phase 6: Paper/Shadow Runtime Soak With Real Samples

Run a bounded paper/shadow session that produces time-sequenced artifacts, not
static simulated output.

Acceptance:

- production state is explainable
- approved-to-fill report has runtime samples or explicit no-signal evidence
- Vegas DLB/edge report has runtime samples or explicit no-signal evidence
- owner identity is complete for every paper/shadow intent
- no ghost/orphan owner pollution
- no live broker leak

## Immediate Recommended Batch

Run a dedicated **Strategy Execution Reliability Evidence Gate** before any
canary manual launch work.

The batch should not tune strategy parameters. It should answer one question:

```text
Where exactly does each strategy-approved decision stop in the execution chain?
```

Only after that evidence exists should the team decide whether the next fix is:

- option data policy cleanup
- validator latency decomposition
- UOM routing correction
- production health gate refinement
- strategy-specific data readiness repair
- paper/shadow runtime harness improvement

## Not Approved

This review does not approve:

- full live trading
- unattended live trading
- further DLB widening without measurements
- strategy parameter tuning
- auto-cancel of unknown broker orders
- auto-flatten of unknown owner positions
- symbol-only or `order_ref` heuristic attribution
