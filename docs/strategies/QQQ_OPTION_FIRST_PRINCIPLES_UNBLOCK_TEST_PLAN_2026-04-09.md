# QQQ Option First-Principles Unblock Test Plan (2026-04-09)

## Scope

This plan validates the unblock work defined in:

- `./docs/strategies/QQQ_OPTION_FIRST_PRINCIPLES_UNBLOCK_TODO_2026-04-09.md`

The question is not only whether `QQQ option` can trade.

The real question is:

1. does it reuse the same `QQQ` truth correctly
2. does it reuse the same `QQQ` stock signal correctly
3. does it avoid duplicated or ambiguous live authority
4. does it stay explainable

## First-Principles Test Goals

1. One market truth can feed multiple execution consumers.
2. One live signal authority can feed multiple execution expressions.
3. Execution expression does not redefine market truth.
4. Shared-stream optimization must not drop consumers.
5. Replay mode must remain measurable without becoming live authority.

## Test Layers

## 1) Shared Stream Fanout

Target:

1. the same `(QQQ, 1m)` stream is created once
2. multiple strategies receive the callback

Cases:

1. first strategy subscribes to `QQQ 1m` -> one task created
2. second strategy subscribes to `QQQ 1m` -> no second task created
3. both strategies receive bar callbacks
4. heartbeats are emitted for both consumers

Recommended files:

- `tests/unit/services/market/test_underlying_fabric_single_subscription.py`
- `tests/integration/strategies/test_shared_underlying_stream_fanout.py`

## 2) Stock Signal Reuse

Target:

1. stock live signal is reusable as a normalized artifact
2. option live mode consumes that artifact directly

Cases:

1. stock `LONG` -> option `CALL`
2. stock `SHORT` -> option `PUT`
3. stale signal -> explicit block reason
4. missing signal -> explicit block reason
5. same-minute valid signal -> downstream result exists

Recommended files:

- `tests/unit/strategies/test_qqq_stock_signal_artifact.py`
- `tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py`
- `tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py`
- `tests/integration/strategies/test_qqq_option_upstream_signal_flow.py`

## 3) Shared Truth Discipline

Target:

1. replay mode reads the same underlying truth plane
2. stock truth is not synthesized from option chain state

Cases:

1. missing underlying truth does not fall back to option chain values
2. option chain fields do not create replay signal by themselves
3. stock and option can both consume same truth fields in the same process

Recommended files:

- `tests/unit/strategies/options_kernel/test_stock_truth_not_derived_from_option_chain.py`
- `tests/unit/services/market/test_underlying_fabric_single_subscription.py`
- `tests/unit/strategies/test_qqq_truth_fabric.py`

## 4) Single Writer And Shadow Safety

Target:

1. one live writer per option strategy instance
2. replay shadow cannot submit live entries

Cases:

1. invalid source mode rejected
2. upstream mode requires explicit source strategy id
3. shadow replay returns signal but no live entry plan
4. source failure does not trigger silent replay failover

Recommended files:

- `tests/unit/strategies/options_kernel/test_signal_source_authority_rules.py`
- `tests/unit/strategies/options_kernel/test_rule_replay_signal_source.py`
- `tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py`

## 5) Explainability

Target:

1. each evaluated minute yields one explainable downstream status
2. there is no silent heartbeat-only failure mode

Cases:

1. valid signal -> `signal_consumed`
2. missing signal -> `source_signal_missing`
3. stale signal -> `source_signal_stale`
4. no contract -> `no_contract_selection`
5. submit ok -> `entry_intent_submitted`

Recommended files:

- `tests/unit/strategies/options_kernel/test_qqq_option_signal_source_audit.py`
- `tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py`
- `scripts/check_qqq_option_signal_runtime_acceptance.py`

## 6) Signal Plane Reliability

Target:

1. Redis transient failure should recover automatically.
2. Fallback state should be visible to traders and audit.
3. Signal plane should support remote cleanup to avoid stale cross-day signals.

Cases:

1. first Redis connect fails, later retry succeeds.
2. heartbeat contains `signal_plane_state`, `signal_plane_error_streak`, `signal_plane_retry_in_sec`.
3. remote clear removes shared signal entries.
4. opening acceptance report includes signal-plane degraded ratio.

Recommended files:

- `tests/unit/strategies/test_qqq_stock_signal_artifact.py`
- `tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py`
- `tests/unit/scripts/test_check_qqq_option_signal_runtime_acceptance.py`

## 7) Stock Baseline Freeze

Target:

1. stock entry/exit behavior is not changed by unblock work

Cases:

1. stock rule engine regression remains green
2. stock observability regression remains green
3. signal publication does not create order-side drift

Recommended commands:

```bash
./venv/bin/pytest -q \
  tests/unit/strategies/test_qqq_intraday_rule_engine.py \
  tests/unit/strategies/test_qqq_intraday_observability.py
```

## Runtime Validation

Run first 30 minutes after open:

1. stock strategy alive
2. option strategy alive
3. stock emits signal artifacts
4. option logs downstream results
5. no duplicate option submit in same minute
6. shared underlying stream metrics show one stream with multiple consumers

## Command Set

```bash
./venv/bin/pytest -q \
  tests/unit/scripts/test_check_qqq_option_signal_runtime_acceptance.py \
  tests/unit/services/market/test_underlying_fabric_single_subscription.py \
  tests/integration/strategies/test_shared_underlying_stream_fanout.py \
  tests/unit/strategies/options_kernel/test_stock_truth_not_derived_from_option_chain.py \
  tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py \
  tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py \
  tests/unit/strategies/options_kernel/test_rule_replay_signal_source.py \
  tests/unit/strategies/options_kernel/test_signal_source_authority_rules.py \
  tests/unit/strategies/options_kernel/test_qqq_option_signal_source_audit.py \
  tests/unit/strategies/test_qqq_stock_signal_artifact.py \
  tests/unit/strategies/test_qqq_truth_fabric.py \
  tests/integration/strategies/test_qqq_option_upstream_signal_flow.py \
  tests/integration/strategies/test_qqq_option_signal_source_shadow_drift.py \
  tests/unit/strategies/options_kernel/test_qqq_intraday_option_parity.py \
  tests/unit/strategies/test_qqq_intraday_rule_engine.py
```

Runtime gate:

```bash
./venv/bin/python scripts/check_qqq_option_signal_runtime_acceptance.py \
  --date 2026-04-09 \
  --stock-strategy-id qqq_intraday_001 \
  --option-strategy-id qqq_intraday_option_v1_001 \
  --plugin-log-glob 'runtime/logs/*.jsonl'
```

## Exit Criteria

This unblock plan is complete only when:

1. shared stream fanout is correct
2. option consumes stock signal directly
3. replay shadow cannot submit live entries
4. all non-trade minutes are explainable
5. stock baseline is still stable
6. process topology no longer determines correctness
