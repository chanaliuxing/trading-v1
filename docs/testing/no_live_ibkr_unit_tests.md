# No-Live IBKR Unit Test Boundary

Unit tests must not connect to a real IBKR TWS/Gateway process. By default,
`tests/conftest.py` blocks `ib_insync.IB.connect` and
`ib_insync.IB.connectAsync`, including local Gateway/TWS ports
`127.0.0.1:4001` and `127.0.0.1:4002`.

Tests that need broker behavior must use a fake adapter, stubbed connection
manager, or deterministic snapshot fixture. Unit tests must not read local
account, position, order, or execution state from TWS/Gateway.

Future live integration tests must be isolated outside the unit suite, marked
with `@pytest.mark.live_ibkr`, and run only with explicit opt-in:

```bash
TRADING_TEST_ALLOW_LIVE_IBKR=1 pytest tests/integration/ibkr -m live_ibkr
```

Live integration tests must remain read-only unless a separate, explicit order
safety gate is added for broker mutation paths.

## IB Reconnect Regression Entry

For reconnect/circuit/startup-gate changes, use the no-live regression wrapper:

```bash
scripts/run_ib_reconnect_regression.sh fast
scripts/run_ib_reconnect_regression.sh full
```

The wrapper explicitly sets `TRADING_TEST_ALLOW_LIVE_IBKR=0` and includes the
no-live boundary test, so accidental socket use against local TWS/Gateway ports
fails the regression.
