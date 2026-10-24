# QQQ Signal Decoupling Test Plan (2026-04-10)

## Objective

Verify that shared QQQ signal publication is decoupled from stock-only execution blockers, while preserving existing QQQ stock strategy execution behavior.

## Test Matrix

### Unit: stock signal publication behavior

1. Baseline entry-allowed path
- Setup a valid QQQ entry environment.
- Expect `ENTRY_ALLOWED`.
- Expect stock signal artifact published.
- Expect stock submit path still runs.

2. Open-orders blocked stock path
- Setup a valid QQQ entry environment.
- Inject active stock-side open orders for the same strategy.
- Expect stock signal artifact published.
- Expect stock order submit does not run.
- Expect stock block remains active.

3. Live-position blocked stock path
- Setup a valid QQQ entry environment.
- Inject non-zero live stock position (`TQQQ` or `SQQQ`).
- Expect stock signal artifact published.
- Expect stock order submit does not run.
- Expect strategy marks itself traded / synced from real position as before.

### Unit: upstream option consumer compatibility

4. Upstream consumer reads artifact unchanged
- Publish stock artifact.
- Build option plugin signal in `upstream_stock_signal` mode.
- Expect `CALL` / `PUT` translation unchanged.

5. Upstream heartbeat / audit fields unchanged
- Expect `signal_source_status`, `signal_source_age_sec`, strategy id, and signal-plane fields still present.

## Commands

Run the minimum focused suite:

```bash
./venv/bin/pytest -q tests/unit/strategies/test_qqq_intraday_observability.py
./venv/bin/pytest -q tests/unit/strategies/test_qqq_hardening.py
./venv/bin/pytest -q tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py
./venv/bin/pytest -q tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py
```

If time allows, run:

```bash
./venv/bin/pytest -q tests/integration/strategies/test_qqq_option_upstream_signal_flow.py
```

## Risk Checks

- No new stock submits when open orders already exist.
- No regression in stock real-position safety gate.
- No consumer break from additive payload fields.
- No dependence on stock submit success for option signal visibility.

## Failure Criteria

This change is rejected if any of the following happens:

- stock submit runs when open orders should block it
- stock submit runs when live position should block it
- option upstream consumer no longer reads stock artifacts
- existing QQQ hardening tests regress

## Execution Record (2026-04-10)

Executed:

```bash
./venv/bin/pytest -q tests/unit/strategies/test_qqq_intraday_observability.py
./venv/bin/pytest -q tests/unit/strategies/test_qqq_hardening.py
./venv/bin/pytest -q tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py tests/unit/strategies/options_kernel/test_qqq_option_signal_source_audit.py tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py
./venv/bin/pytest -q tests/integration/strategies/test_qqq_option_upstream_signal_flow.py
./venv/bin/pytest -q tests/unit/services/test_runtime_reconciler_position_sync.py tests/unit/services/test_runtime_reconciler_polling.py -k "option_mapping_unknown or broker_position_unknown or flat_protective_cancel"
./venv/bin/pytest -q tests/unit/strategies/test_qqq_shared_signal_producer.py
./venv/bin/pytest -q tests/unit/strategies/test_qqq_stock_executor.py
```

Outcome:

- `test_qqq_intraday_observability.py`: passed
- `test_qqq_hardening.py`: passed
- options upstream unit suites: passed
- upstream integration flow: passed
- reconciler unknown-mapping / unknown-position protective-cancel suites: passed
