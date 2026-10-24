# IBKR Order Truth And Reconnect Test Plan (2026-04-17)

## 1. Goal

Validate that the IBKR remediation program fixes reconnect instability and ghost-order amplification without introducing a new failure mode during migration.

## 2. Test Principles

1. Reconciler safety must be proven before clientId drift removal.
2. Startup adoption must be tested as a first-class flow, not as a side effect.
3. Epoch safety must be validated on every long-lived IB consumer.
4. Production acceptance requires failure-injection, not only unit coverage.
5. The primary milestone gate is based on 3 system-level behaviors, not broad test count.

## 2.1 Package Map

The 5 high-risk milestone packages map to test focus areas as follows:

1. Package 1:
   - reconciler disarmament
   - quarantine/block behavior
2. Package 2:
   - master truth session
   - startup adoption
   - canonical `trading_enabled`
3. Package 3:
   - order identity persistence
   - quarantine schema behavior
4. Package 4:
   - clientId registry
   - runtime no-drift reconnect
   - auxiliary-session suppression
5. Package 5:
   - supervisor-only reconnect ownership
   - `IBHandle` epoch safety across all long-lived consumers

## 3. Environments

1. Local unit-test environment
2. Integration environment with mocked IB session behavior
3. Paper IB Gateway / TWS fault-injection environment
4. Production shadow / limited rollout environment

## 4. Unit Test Matrix

## 4.1 Reconciler

Add or update tests under:

1. `tests/unit/reconciliation/test_runtime_reconciler_orphans.py`
2. `tests/unit/services/test_runtime_reconciler_ghost_cleanup.py`
3. `tests/unit/services/test_runtime_reconciler_polling.py`

Cases:

1. Report-only mode never terminalizes DB ghost rows.
2. Report-only mode never auto-cancels runtime orphan rows.
3. Unknown broker order creates quarantine record.
4. Quarantined symbol blocks new intent generation.
5. Existing audit and alert emission still happens in report-only mode.
6. DB yes / broker open no path does not jump straight to destructive action before execution truth check.
7. M1 path validation proves destructive runtime behavior is absent from normal runtime flow, not merely disabled by config.

## 4.2 ClientId Registry

Add tests under:

1. `tests/unit/services/test_ib_connection_manager.py`

Cases:

1. Registry resolves deterministic ids per role.
2. Runtime reconnect reuses same clientId for each role.
3. Startup namespace selection obeys config and fallback rules.
4. Production mode with `allow_startup_id_rotation=false` refuses unsafe startup rotation.
5. Persisted state file is stable across restarts.

## 4.3 Master Truth Session

Add tests under:

1. `tests/unit/services/test_ib_connection_manager.py`

Cases:

1. Master session is created as read-only role.
2. Master session is not used for order placement.
3. Startup readiness requires master truth completion.
4. Adoption compares broker truth by:
   - `perm_id`
   - `order_ref`
   - latest `broker_order_id`
5. canonical `trading_enabled` remains false until master truth completion.

## 4.4 Epoch Handles

Add tests under:

1. `tests/unit/services/market/test_ib_market_data_premarket_ports.py`
2. new tests for market data handle semantics
3. new tests for order monitor handle semantics

Cases:

1. Stale handle rejects calls after epoch rollover.
2. Consumer refreshes handle and succeeds on new epoch.
3. Market-data service resubscribes after epoch rollover.
4. Order monitor / sidecars do not keep stale IB references.

## 4.5 Execution Persistence

Add or update tests under:

1. `tests/unit/services/exec/...`

Cases:

1. Submitted order persists:
   - `broker_order_id`
   - `ib_perm_id`
   - `ib_client_id`
   - `ib_session_role`
   - `ib_connection_epoch`
2. OCO child orders persist same identity metadata.
3. Netted execution path persists same identity metadata.

## 5. Integration Test Matrix

## 5.1 Startup Adoption

Build integration tests that simulate:

1. DB working order + broker working order on same `perm_id`
2. DB working order + broker open order missing but execution present
3. Broker working order with no DB row

Expected:

1. case 1 is adopted and enriched
2. case 2 is resolved via execution truth path, not destructive reconciler shortcut
3. case 3 becomes quarantine, not auto-cancel

## 5.2 Reconnect Ownership

Cases:

1. market disconnect event and exec disconnect event fire close together
2. only supervisor owns reconnect flow
3. no distributed reconnect loops race each other

Expected:

1. one recovery flow
2. no clientId churn
3. no repeated gatekeeper contention between business modules

## 5.3 Sidecar Consumers

Cases:

1. `options_data` survives epoch rollover
2. `backfill_service` survives epoch rollover
3. `vix_ingestor` survives epoch rollover
4. any direct-session sidecar migrated to handle-based supervisor access
5. unresolved quarantine on symbol blocks new symbol-level trading despite broker connectivity being healthy

## 6. Fault Injection / GameDay

Use existing fault-injection coverage as base and extend it.

Suggested files:

1. `tests/gameday/fault_injection.py`
2. `tests/system_integrity_stress_test.py`

Required scenarios:

1. kill TWS/Gateway during active market-data subscriptions
2. kill TWS/Gateway during active working orders
3. recover TWS/Gateway while old session id is still temporarily occupied
4. present external unknown broker order on same account
5. simulate `326 client id in use`
6. simulate API timeout during handshake

Pass criteria:

1. reconnect stabilizes without runtime clientId drift
2. no new ghost-order amplification
3. unresolved unknown broker order enters quarantine
4. new trading on quarantined symbol is blocked

## 7. Production Shadow / Rollout Checks

Run in stages:

## Stage 1

1. report-only reconciler only
2. no clientId drift removal yet

Observe:

1. no new destructive reconciler actions
2. anomaly visibility retained

## Stage 2

1. master truth session enabled
2. startup adoption enabled

Observe:

1. startup produces stable broker truth snapshot
2. adoption count and unresolved quarantine count are explainable

## Stage 3

1. clientId registry enabled
2. runtime clientId drift disabled

Observe:

1. reconnects no longer hop ids
2. startup does not silently pick random new ids

## Stage 4

1. epoch handles enabled across all consumers

Observe:

1. no stale handle exceptions left unresolved
2. market-data and sidecars recover without manual restart

## 8. Acceptance Metrics

Track before / after.

Primary hard gates:

1. clientId churn count
2. fresh-probe / owner-cancel auxiliary session count
3. destructive reconciler action count

Secondary supporting metrics:

1. `326` conflict count
2. reconnect timeout count
3. ghost count
4. orphan count
5. quarantine active count
6. manual intervention count after cutover

## 9. Final Pass Criteria

The remediation program passes only if all of the following hold:

1. 20 forced restart drills complete without runtime clientId drift.
2. Runtime creates zero normal-flow fresh-probe / owner-cancel auxiliary recovery sessions after cutover.
3. Runtime reconciler performs zero destructive ghost cleanup or auto-cancel actions in production mode.
4. Startup adoption succeeds on known broker truth cases.
5. Unknown broker orders are quarantined and block new intents.
6. All new orders carry full identity metadata.
7. No long-lived consumer remains dependent on stale raw `IB` references.
