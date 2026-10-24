# Production Safety Review TODO / Test / Acceptance

Date: 2026-05-08

## Verdict

I agree with the review direction. The repository has production-oriented components, but it should not be called production-grade until broker-truth safety actions, fill attribution, recovery durability, account identity, and platform-owned option selection are closed with tests.

## P0 TODO

1. Broker-truth safety lane
   - Safety intents are `exit`, `flatten`, `protection`, and `cancel`.
   - A global paused/degraded/recovering/halted state must not block broker flatten/protect.
   - Safety actions may still be blocked by broker truth availability, execution readiness, idempotency, risk cap, and manual lock.
   - Ledger flat/stale/missing state must not block broker-truth-driven flatten/protect.

2. Fill attribution before mutation
   - Normal fill handling must not mutate `core_fill`, `core_position`, or `position_ledger_event` unless attribution has one unique proof.
   - Proof must include broker identity (`perm_id`, `order_ref`, or broker order id), symbol, side, quantity bounds, and order/fill time sanity.
   - Unknown, ambiguous, stale, or mismatched fills must fall through to convergence/quarantine, not persistence.

3. Account identity
   - `UNKNOWN_ACCOUNT` remains migration-tolerated in existing code paths, but new production write paths should either resolve an account or write quarantine-only state.
   - Add DB admission/check constraints after migration cleanup confirms no live rows depend on `UNKNOWN_ACCOUNT`.

4. Recovery incidents
   - Incident artifacts must include runtime fingerprint: source PID, process start, git SHA, and config hash.
   - File writes must be atomic; DB-backed recovery truth remains the preferred end-state.

5. Option contract selection
   - Move contract selection into a platform service boundary.
   - Plugins should emit preferences/constraints only; platform returns the selected contract and writes the selection verdict before execution.

6. Go/no-go regression tests
   - Prove stale ledger/data/artifact cannot block broker flatten.
   - Prove weak fill attribution cannot mutate position truth.

## Landed In This Slice

- `UnifiedOrderManager` now lets safety intents bypass global health states, including `halted`.
- `UnifiedOrderManager` no longer returns `ledger_truth_flat_no_close_allowed` for broker-truth safety intents when ledger qty is flat.
- `SafetyKernel` fill resolution now requires a unique verified order candidate before persistence/position mutation.
- `FillEventHandler` passes IB-derived fill side into the kernel.
- Production fill mutation now requires a resolved account; `UNKNOWN_ACCOUNT` fills do not write `core_fill`, `core_position`, or ledger state and must fall through to convergence/quarantine.
- `RecoveryIncidentService` writes runtime fingerprint fields and uses locked atomic JSON replacement for incident and index artifacts.
- Option kernel contract selection now goes through `services.production.contract_selection_service.select_option_contract_for_entry`; legacy plugin selection is still supported during migration, but platform owns verdict payload and identity validation fields.
- Production can reject legacy plugin-private option contract selection by setting `REQUIRE_PLATFORM_OPTION_CONTRACT_SELECTION=1`; `ALLOW_LEGACY_OPTION_CONTRACT_SELECTION=1` is the explicit migration escape hatch.
- Strict production can also hard-reject selected option contracts with incomplete broker identity by setting `REQUIRE_COMPLETE_OPTION_CONTRACT_IDENTITY=1`; this is implied when legacy plugin selection is disabled.
- Position ledger and protection registry schema now add `NOT VALID` account identity constraints that reject new blank/`UNKNOWN_ACCOUNT` rows without requiring historical cleanup first.
- `PositionLedger` runtime writes now reject `UNKNOWN_ACCOUNT` before attempting DB mutation.

## Tests

Targeted tests added or updated:

- `tests/unit/services/test_unified_order_manager.py`
  - safety lane is allowed while system health is `halted`
  - broker flatten is not blocked by flat ledger
- `tests/unit/exec/test_safety_kernel_position_accounting.py`
  - side mismatch does not persist fill or mutate position
  - ambiguous candidate set does not resolve
  - unknown account blocks production fill mutation
- `tests/unit/services/test_recovery_incident_service.py`
  - recovery incident and index artifacts include runtime fingerprint
  - atomic temporary files are not left behind
  - incident and index writes create per-artifact lock files
- `tests/unit/services/test_contract_selection_service.py`
  - platform contract-selection boundary wraps legacy plugin selector and surfaces identity gaps
  - strict platform-selection mode rejects legacy plugin-private selector before it is called
  - strict contract-identity mode rejects incomplete selected contract identity before entry execution
- `tests/unit/services/test_position_ledger.py`
  - DDL contains unknown-account check constraints for protection registry, ledger events, and current state
  - runtime ledger append rejects unknown account before DB writes
- `tests/e2e/test_runtime_recovery_drill.py`
  - broker safety flatten still delegates when system health is halted, market/data artifacts are stale, and ledger projection is flat

Recommended command:

```bash
venv/bin/python -m pytest \
  tests/unit/services/test_unified_order_manager.py \
  tests/unit/exec/test_safety_kernel_position_accounting.py \
  tests/unit/services/test_recovery_incident_service.py -q
```

## Acceptance Bar

This slice is accepted only if:

- Safety flatten/protection delegates when broker truth is ready, even if system state is halted and ledger reads flat.
- Entry orders remain blocked by global health and broker-truth admission gates.
- Fill side/symbol/qty/time mismatches do not write fill, position, or ledger state.
- Multiple matching fill candidates do not write fill, position, or ledger state.
- Production fill handling with `UNKNOWN_ACCOUNT` does not write fill, position, order-state, or ledger state.
- Recovery incident JSON files contain `source_process_pid`, `process_start_time`, `git_head`, and `config_hash`.
- No non-atomic temporary recovery JSON file remains after a successful write.
- Recovery incident JSON/index/history writes are protected by per-artifact file locks.
- Option kernel entry selection is routed through the platform contract-selection service, and selected contract verdict payloads include `platform_service`, `legacy_plugin_selection`, `selected_contract_identity_valid`, and missing identity fields.
- With `REQUIRE_PLATFORM_OPTION_CONTRACT_SELECTION=1`, legacy `plugin.select_contract_pure` is not called unless explicitly allowed; production selection must use the platform preference/service path.
- New ledger/protection DB rows cannot use blank or `UNKNOWN_ACCOUNT` account ids once the migration is applied; runtime ledger writes fail closed before DB mutation when account identity is unresolved.
- A broker-truth flatten with nonzero broker hint delegates even when health is halted, market readiness is false, production artifact is stale, and ledger projection is flat.

## Remaining Before Production-Grade

- Validate and then enforce account identity constraints after migration cleanup by running `VALIDATE CONSTRAINT` in production maintenance.
- Replace artifact-only recovery incidents with DB-backed transactional state or add locking around file-backed writers.
- Complete plugin migration from legacy selector to preference-only selectors; strict production mode rejects legacy selectors, while paper/test still allow them during migration.
- Add full end-to-end recovery drill proving broker nonzero can always flatten/protect under stale ledger/data/artifact conditions.
