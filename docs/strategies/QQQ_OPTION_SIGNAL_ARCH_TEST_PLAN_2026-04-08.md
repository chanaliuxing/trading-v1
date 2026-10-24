# QQQ Option Signal Architecture Test Plan (2026-04-08)

## Scope

This plan validates the architecture defined in:

- `./docs/strategies/QQQ_OPTION_SIGNAL_ARCH_OPTIMIZATION_2026-04-08.md`

The target is not only "can it trade".
The target is:

1. `QQQ` option can safely consume `QQQ` stock signal as the live source
2. the system remains modular enough to support independent replay later
3. `QQQ` stock stable path is not broken
4. every non-trade outcome is explainable

## First-Principles Test Goals

1. One market interpretation should be reusable by multiple execution expressions.
2. Live signal authority must be deterministic.
3. The option stack must never silently change signal source in production.
4. The stable stock strategy must remain behaviorally unchanged in `P0`.
5. Shadow parity must be measurable without becoming a second live writer.

## Phase Boundaries

### P0

Validate:

1. `upstream_stock_signal` publication
2. option consumption of normalized stock signal artifact
3. single-writer enforcement
4. source-aware audit trail

### P1

Validate:

1. shared feature fabric extraction
2. `independent_rule_replay` signal source
3. drift reporting between stock signal and replay signal

This test plan covers both, but `P0` is the immediate live gate.

## Critical Risks To Test

1. duplicate entry from dual signal writers
2. silent fallback from upstream signal to replay signal
3. stock strategy behavior drift caused by export hooks
4. stale or missing source signal consumed as live input
5. option strategy missing trade with no source-aware audit reason
6. replay mode contaminating live execution
7. duplicate underlying subscriptions created when adding another strategy consumer
8. stock-truth logic accidentally sourced from option-chain state

## Test Layers

## 1) Unit: Signal Artifact Contract

Target responsibilities:

1. stock strategy emits normalized signal artifact
2. artifact contains enough information for option consumption
3. artifact is not an order intent proxy

Cases:

1. `entry_allowed` stock minute emits signal artifact with:
   - `strategy_id`
   - `trigger_id`
   - `decision_id`
   - `minute`
   - `direction`
   - `signal_family`
   - `context_payload`
2. blocked stock minute does not emit live-entry signal artifact
3. repeated same-minute artifact is deduplicated by signal identity
4. artifact age calculation is deterministic

Recommended test file:

- `tests/unit/strategies/test_qqq_stock_signal_artifact.py`

## 2) Unit: Option Signal Source Contract

Target responsibilities:

1. `upstream_stock_signal` reads normalized stock signal artifact
2. `independent_rule_replay` returns normalized signal decision from shared features
3. both implementations share one normalized output schema

Cases:

1. upstream stock `LONG` maps to option `CALL`
2. upstream stock `SHORT` maps to option `PUT`
3. expired source signal is rejected with source-aware reason
4. missing source signal is rejected with source-aware reason
5. replay signal output preserves shared direction truth
6. normalized contract fields are present in both modes

Recommended test files:

- `tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py`
- `tests/unit/strategies/options_kernel/test_rule_replay_signal_source.py`

## 3) Unit: Single Writer And Fallback Rules

Target responsibilities:

1. one option strategy instance has one live signal writer
2. fallback cannot become a silent live entry path unless explicitly enabled

Cases:

1. config with two live writers is rejected
2. `fallback_mode` defaults to shadow-only
3. shadow replay cannot submit live entry
4. live source failure does not automatically trigger replay entry in same minute
5. explicit failover flag, if later added, requires explicit audit mark

Recommended test file:

- `tests/unit/strategies/options_kernel/test_signal_source_authority_rules.py`

## 4) Unit: `QQQ` Stock Stable Path Freeze

Target responsibilities:

1. `P0` hooks do not change stock strategy entry/exit behavior

Cases:

1. golden entry-allowed samples still produce same stock direction
2. no new order envelopes are created by signal publication
3. existing stock replay/parity tests remain green

Recommended commands:

```bash
./venv/bin/pytest -q \
  tests/unit/strategies/test_qqq_intraday_rule_engine.py \
  tests/unit/strategies/test_qqq_v47_chase_guard.py \
  tests/unit/strategies/test_qqq_trace_fields.py
```

## 4A) Unit: Shared Data Plane Discipline

Target responsibilities:

1. one underlying data subscription can serve multiple consumers
2. stock-truth feature builders do not depend on option-chain state

Cases:

1. adding a second strategy consumer does not require a second underlying subscription in the same process
2. shared fabric can expose underlying state to both stock and option consumers
3. stock/replay signal path rejects missing underlying truth rather than silently reading option-chain-derived substitutes
4. option-chain fields may augment execution, but not replace stock-truth inputs

Recommended test files:

- `tests/unit/services/market/test_underlying_fabric_single_subscription.py`
- `tests/unit/strategies/options_kernel/test_stock_truth_not_derived_from_option_chain.py`

## 5) Unit: Option Consumer Integration

Target responsibilities:

1. option strategy consumes signal artifact and proceeds into contract selection / submit path
2. audit rows expose source mode and block reason

Cases:

1. live stock signal -> option `Signal` object created
2. no signal artifact -> no entry and explicit audit reason
3. stale signal artifact -> no entry and explicit audit reason
4. valid signal artifact + no contract -> `no_contract_selection`
5. valid signal artifact + valid selection -> submit path invoked
6. heartbeat contains:
   - `signal_source_mode`
   - `signal_source_status`
   - `signal_source_age_sec`
   - `signal_source_strategy_id`

Recommended test files:

- `tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py`
- `tests/unit/strategies/options_kernel/test_qqq_option_signal_source_audit.py`

## 6) Integration: End-To-End P0 Path

Scenario:

1. stock strategy produces entry signal
2. option strategy consumes it
3. option strategy selects contract
4. option strategy submits entry intent
5. runtime/state audit remains explainable

Cases:

1. stock `LONG` minute leads to option `CALL` entry attempt
2. stock `SHORT` minute leads to option `PUT` entry attempt
3. same-minute duplicate stock signal does not create duplicate option entry
4. option shadow replay may emit evidence but may not submit live entry

Recommended test file:

- `tests/integration/strategies/test_qqq_option_upstream_signal_flow.py`

## 7) Integration: Replay Shadow Drift

Scenario:

1. live source is stock signal
2. shadow source is independent replay
3. system records parity / drift without dual submission

Cases:

1. matching directions produce `parity_ok`
2. mismatching directions produce drift evidence only
3. drift cannot trigger live replay order

Recommended test file:

- `tests/integration/strategies/test_qqq_option_signal_source_shadow_drift.py`

## 8) Runtime Validation: Paper Trading

### Pre-open checks

1. `QQQ` stock strategy alive and heartbeating
2. `QQQ option` strategy alive and heartbeating
3. `signal_source.mode=upstream_stock_signal`
4. source strategy id matches `qqq_intraday_001`
5. source-aware audit fields present in heartbeat
6. no duplicate underlying subscription is opened for `QQQ` in the same runtime process

### Open-session checks

Observe first 30-60 minutes:

1. every stock `entry_allowed` minute is visible as reusable signal artifact
2. option strategy shows one of:
   - `signal_consumed`
   - `source_signal_missing`
   - `source_signal_stale`
   - `no_contract_selection`
   - `entry_intent_submitted`
3. there is no minute with "no trade" and no reason
4. there are no duplicate option submissions from dual modes
5. there is no evidence that stock truth is being inferred from option chain state

### Intraday evidence check

For each stock signal minute:

1. capture stock audit
2. capture option strategy audit
3. verify mapping and result path

Required evidence rows:

1. stock `entry_allowed`
2. option source-aware heartbeat or signal-consumed event
3. option downstream result

## Commands

Baseline unit run:

```bash
./venv/bin/pytest -q \
  tests/unit/strategies/test_qqq_intraday_rule_engine.py \
  tests/unit/services/market/test_underlying_fabric_single_subscription.py \
  tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py \
  tests/unit/strategies/options_kernel/test_rule_replay_signal_source.py \
  tests/unit/strategies/options_kernel/test_signal_source_authority_rules.py \
  tests/unit/strategies/options_kernel/test_qqq_option_signal_source_audit.py \
  tests/unit/strategies/options_kernel/test_stock_truth_not_derived_from_option_chain.py \
  tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py \
  tests/unit/strategies/test_qqq_stock_signal_artifact.py
```

Integration run:

```bash
./venv/bin/pytest -q \
  tests/integration/strategies/test_qqq_option_upstream_signal_flow.py \
  tests/integration/strategies/test_qqq_option_signal_source_shadow_drift.py
```

Runtime acceptance script:

```bash
./venv/bin/python scripts/check_qqq_option_signal_runtime_acceptance.py \
  --date 2026-04-09 \
  --stock-strategy-id qqq_intraday_001 \
  --option-strategy-id qqq_intraday_option_v1_001 \
  --plugin-log-glob 'runtime/logs/*.jsonl'
```

## Exit Criteria

This test plan is complete only when:

1. `P0` unit and integration tests pass
2. `QQQ` stock baseline behavior remains unchanged
3. paper runtime shows source-aware explainability for every non-trade minute
4. one and only one live signal writer exists per option strategy instance
5. shadow replay cannot create live entries
6. one and only one underlying subscription path exists per process/fabric for the same underlying
