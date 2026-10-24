# INDEX_SUPER_WIN Replica TODO / Test Plan / Acceptance

## 1. Scope

This document is the current execution checklist for the new `index_super_win` replica path.

It reflects the codebase state as of 2026-03-24 and supersedes older generic `v2` TODO lists for replica work.

This is the document to use for:

1. remaining implementation work
2. test planning
3. cut-in acceptance

It is not a declaration that the replica is ready to enable.

## 2. Current Status

Already landed:

1. `qqq` baseline freeze
2. replay/parity tooling foundation
3. config-only manifest foundation
4. replica entry lifecycle core
5. TP1 partial exit intent builder
6. protective stop intent builder
7. flatten intent builder
8. pure position-management rules for:
   - TP1
   - trailing stop
   - breakeven shield
   - forced EOD flatten
9. strategy-shell wiring for:
   - live entry submit
   - order-update convergence
   - TP1 partial submit
   - breakeven stop submit
   - EOD flatten submit
10. restart runtime restore
11. restart stop rebuild
12. broker position update -> protection self-heal
13. broker position update -> flat reset
14. replica entry gate helper for:
   - market data health
   - decision pipeline health
   - required data missing
   - restart degrade active
   - first-entry seed readiness
   - QQQ/QQEW freshness
   - QQQ vs VWAP direction gate
   - QQEW confirmation direction gate
15. unprotected-live kill-switch fail-safe
16. exit order-update convergence for:
   - TP1
   - protective stop terminal/fill
   - flatten terminal/fill
17. short-side parity for:
   - TP1 partial exit intent direction
   - protective stop intent direction
   - breakeven stop placement
   - trailing stop decision path
18. explicit protection audit states for:
   - stop live
   - stop missing
   - self-heal attempted
   - kill-switch fired
19. broker-open-orders-aware protection audit with miss-count thresholds for:
   - broker stop live recovery
   - delayed self-heal after configurable misses
   - delayed kill-switch after configurable misses
   - runtime fallback when broker open orders are unavailable
   - warning / recovered audit evidence
20. synthetic replay fixture catalog for:
   - gate allow/block cases
   - TP1 / trailing / breakeven / EOD exit cases
   - restart missing-protection kill-switch case
   - protection audit self-heal threshold case
   - protection audit recovered-stop-live case
   - protection audit kill-switch threshold case
21. fixture replay runner for comparing one catalog fixture against one actual trace
21. shadow sample summary includes minimum state needed for parity tooling
22. shadow sample -> expected trace builder exists
23. shadow session parity report builder exists for batch same-symbol proof
24. existing isolation hardening remains in place
25. same-symbol shadow proof runner exists to combine session report and parity report
26. replica trace exporter exists for mapping captured session samples back to nearby audit traces
27. generic strategy trace exporter exists so `qqq` and replica can share the same session-export workflow
28. dual-strategy same-symbol proof runner exists to export `qqq`, export replica, and build one combined proof report
29. daily same-symbol proof entrypoint exists with default dated sample/proof directories
30. daily summary comparison exists for dual-strategy proof packages, covering:
   - gate reason
   - submit timing
   - stop rebuild visibility
   - flatten reason
31. pure position-management now includes:
   - logic_retreat
   - first add-on submission
   - weighted entry price update after add intent
   - dynamic stop widening with `qqq`-style hard cap semantics
32. higher-fidelity stable-entry parity now includes:
   - fail-closed QQQ/QQEW age-based data-integrity gating
   - opening-window unknown `price_base_source` block
   - startup-anchor embargo by source + ET time window
   - QQEW-decoupled skip path for confirm-VWAP / confirm-direction gates
33. dual-strategy same-symbol proof now includes foreign-safety evidence for:
   - blocked-sample forbidden replica actions
   - attribution ambiguity / broker-order-id fallback
   - cancelled-broker-ghost-order evidence surfacing
34. dual-strategy same-symbol proof now also produces enablement attestation output for:
   - minimum total sample count
   - minimum blocked-sample count
   - zero foreign cancel
   - zero foreign flatten
   - zero foreign ledger attribution
35. real-session batch proof runner now exists to:
   - iterate dated same-symbol sample directories
   - run dual proof per day
   - accumulate enablement-attestation results across days
36. real-session proof readiness report now exists to:
   - scan dated sample directories
   - count blocked samples and same-symbol `qqq`-expected samples
   - verify audit-day presence
   - report whether any day is ready to run real dual proof
37. real-session proof pipeline now exists to:
   - run readiness first
   - stop cleanly at `readiness_only` when no ready day exists
   - run batch proof automatically when ready days exist
38. real-session proof daily summary now exists to:
   - summarize readiness and batch outputs
   - expose `needs_more_real_samples`
   - highlight non-green proof days
39. automation wrappers and launchd templates now exist for:
   - intraday same-symbol shadow capture
   - post-close proof pipeline
   - post-close proof daily summary
40. final enablement checklist builder now exists to:
   - combine config disabled-state
   - combine preflight result
   - combine real-session daily summary
   - combine batch-proof green-day counts
   - emit explicit blocker codes before any enable attempt
41. replica preflight CLI can now write a durable JSON artifact for downstream automation
42. replica runtime snapshot is now persisted to a durable JSON artifact
43. flat broker-position snapshot builder now exists for disabled-state preflight
44. preflight export wrapper now exists to produce current machine input artifacts before checklist evaluation
45. post-close bundle wrapper now exists to run preflight -> proof -> summary -> checklist in fixed order

Not yet landed:

1. deeper `qqq` entry gate parity beyond currently landed stable guards
2. remaining exit lifecycle parity beyond current high-value slices, after landing logic-retreat / add-on / dynamic-stop-widen
3. same-symbol replay/shadow/live proof against real `qqq` sessions

## 3. Implementation TODO

## 3.0 Detailed Execution Backlog

The remaining work is now broken into concrete execution items.

Each item should only be marked complete when:

1. code is landed
2. focused tests are added
3. the broader relevant regression suite still passes

### Entry Gate Backlog

- [x] `GATE-01` Port `required_data_missing` source mapping so replica reports the same missing symbols/categories as `qqq`
- [x] `GATE-02` Port ATR seed readiness contract:
  - [x] seed ready boolean
  - [x] minimum sample count
  - [x] first-entry-only guard semantics
- [x] `GATE-03` Port restart degrade semantics:
  - [x] active window
  - [x] unlock behavior
  - [x] block reason parity
- [x] `GATE-04` Port market freshness gating:
  - [x] QQQ freshness
  - [x] QQEW freshness
  - [x] stale/data-not-ready block reasons
- [x] `GATE-05` Port direction/confirmation gate:
  - [x] VWAP alignment
  - [x] QQEW confirmation
  - [x] direction mismatch reasons
- [x] `GATE-06` Add replay fixtures for:
  - [x] allowed long
  - [x] blocked required-data-missing
  - [x] blocked restart-degrade
  - [x] blocked market-data-unavailable
- [x] `GATE-07` Add explicit optional guards for:
  - [x] continuity-180s
  - [x] VWAP-ready
  - [x] confirm-symbol VWAP-ready
  - [x] ATR-ready
  - [x] shadow-extreme distance-from-VWAP-ATR
- [x] `GATE-08` Port higher-fidelity stable-entry semantics from `qqq`:
  - [x] fail-closed age-based data-integrity checks
  - [x] opening-window unknown base-source block
  - [x] startup-anchor embargo by ET time window
  - [x] QQEW-decoupled confirm-gate bypass

### Exit Lifecycle Backlog

- [x] `EXIT-01` Persist TP1 runtime state transitions after partial fill
- [x] `EXIT-02` Persist trailing-stop runtime state transitions after TP1
- [x] `EXIT-03` Port remaining high-value post-entry branches used by `qqq`:
  - [x] `logic_retreat`
  - [x] first add-on submission
  - [ ] remaining deeper post-TP1 branches
- [x] `EXIT-04` Add short-side exit parity tests
- [x] `EXIT-05` Add replay fixtures for:
  - [x] TP1 partial fill
  - [x] TP1 then trailing stop
  - [x] breakeven stop
  - [x] force EOD flatten
- [x] `EXIT-06` Verify order-update convergence for:
  - [x] TP1 terminal reject/cancel
  - [x] flatten reject/cancel recovery
  - [x] stop filled -> flat

### Protection / Kill-Switch Backlog

- [x] `PROT-01` Add explicit protection audit loop entrypoint in replica strategy
- [x] `PROT-02` Add protection dedupe state beyond boolean `protection_active`
- [x] `PROT-03` Add protection rebuild replay fixture for restart with live qty + missing protection
- [x] `PROT-04` Add explicit audit/preview states for:
  - [x] stop live
  - [x] stop missing
  - [x] self-heal attempted
  - [x] self-heal rejected
  - [x] kill-switch fired
- [x] `PROT-05` Add tests proving kill-switch does not touch foreign orders/positions
- [x] `PROT-06` Add broker-open-orders miss-count audit semantics:
  - [x] broker stop present resets miss count
  - [x] self-heal waits until configured miss threshold
  - [x] kill-switch waits until configured miss threshold
  - [x] broker-open-orders unavailable falls back to runtime protection flag

### Same-Symbol Proof Backlog

- [ ] `PROOF-01` Export real `qqq` audit traces for same-symbol fixture set
  - [x] replica-side batch trace exporter for captured shadow sessions
  - [x] generic strategy-side batch trace exporter for captured shadow sessions
  - [x] real-session batch dual-proof runner exists for dated sample directories
  - [x] real-session proof-readiness report exists for dated sample directories
  - [x] real-session proof pipeline exists for dated sample directories
  - [x] automation wrappers exist for capture + proof + summary
- [ ] `PROOF-02` Build expected parity traces from real sessions
  - [x] shadow sample -> expected trace builder
  - [x] shadow session parity report builder
- [ ] `PROOF-03` Run same-symbol shadow compare against `qqq`
  - [x] same-symbol shadow proof runner
  - [x] dual-strategy same-symbol proof runner
  - [x] daily same-symbol proof entrypoint
- [x] `PROOF-04` Add daily summary comparison for:
  - [x] gate reason
  - [x] submit timing
  - [x] stop rebuild
  - [x] flatten reason
- [ ] `PROOF-05` Confirm and document:
  - [x] foreign-safety report builder exists
  - [x] blocked-sample forbidden-action detection exists
  - [x] attribution ambiguity / fallback evidence is surfaced
  - [x] enablement attestation builder exists
  - [ ] zero foreign cancel from real same-symbol sessions
  - [ ] zero foreign flatten from real same-symbol sessions
  - [ ] zero foreign ledger attribution from real same-symbol sessions

### Enablement Backlog

- [x] `ENABLE-01` Wire preflight into an enablement runbook
- [ ] `ENABLE-02` Produce one green preflight report from representative runtime/broker snapshots
- [ ] `ENABLE-03` Produce one green same-symbol shadow report set
- [x] `ENABLE-04` Create cut-in checklist with explicit go/no-go items
- [ ] `ENABLE-05` Keep replica disabled until all above are green
- [x] `ENABLE-06` Write durable preflight JSON for checklist automation
- [ ] `ENABLE-07` Produce three green real-session proof days so final checklist turns green

## 3.1 Current Machine Blockers

Current machine-generated checklist:

`./runtime/state/index_super_win_same_symbol_proof/final_enablement_checklist.json`

Current blockers:

1. `no_ready_real_session_day`
2. `real_session_pipeline_not_completed`
3. `insufficient_green_proof_days`
4. `insufficient_green_attestation_days`

This reduces the remaining work to four concrete actions:

1. collect dated real same-symbol samples
2. run post-close proof until at least 3 green proof/attestation days exist
3. keep `index_super_win_001` disabled until the checklist is green

## Phase 1: Entry Gate Parity

Goal:

Make single-symbol entry decisions match `qqq_intraday` instead of only matching the generic feature-driven entry path.

TODO:

- [ ] Port required-data gate parity
- [ ] Port ATR seed / daily ATR dependency contract
- [ ] Port QQEW / VWAP / freshness gating contract
- [ ] Port restart-degrade / no-new-entry gate
- [ ] Port block-reason taxonomy so replay can compare exact reasons
- [ ] Add single-symbol entry parity fixtures from baseline traces

Exit gate:

1. replay parity can prove `allow/block`, `reason`, `side`, and `qty` match `qqq`

## Phase 2: Exit Lifecycle Parity

Goal:

Complete the real `qqq` exit chain rather than the currently landed minimum path.

TODO:

- [ ] Add partial-exit runtime state transitions after TP1
- [ ] Add trailing-stop parity fields and state transitions
- [ ] Add stop-widen / retreat branches where `qqq` requires them
- [ ] Add post-TP1 EOD handling parity
- [ ] Add short-side parity coverage for exit lifecycle
- [ ] Add parity fixtures for:
  - target hit
  - tp1 then trailing stop
  - breakeven stop
  - forced EOD flatten

Exit gate:

1. replay parity can prove exit action and reason match `qqq`

## Phase 3: Protection / Kill-Switch Parity

Goal:

Make live positions impossible to run unprotected without an explicit fail-safe outcome.

TODO:

- [ ] Add protective-stop audit loop equivalent for replica path
- [ ] Add stop dedupe / active protection tracking beyond current `protection_active` flag
- [ ] Add kill-switch behavior when live position cannot be protected
- [ ] Add protection rebuild parity for restart + live inventory + missing stop
- [ ] Add explicit audit events / preview states for protection failure modes

Exit gate:

1. a live replicated position never remains silently unprotected
2. kill-switch path is deterministic and tested

## Phase 4: Pre-Enable Readiness / Preflight

Goal:

Before any future enablement, force a full local readiness check.

TODO:

- [x] Add replica preflight CLI
- [x] Validate manifest completeness
- [x] Validate runtime restore payload sanity
- [x] Validate live inventory vs local runtime sanity
- [x] Validate protection status for every live symbol
- [x] Validate audit / observability paths exist and are writable
- [x] Validate baseline and replay artifacts exist
- [x] Output machine-readable readiness report

Exit gate:

1. enablement is blocked unless preflight passes

## Phase 5: Same-Symbol Proof

Goal:

Prove the replica can coexist with `qqq` safely and match behavior.

TODO:

- [ ] Build same-symbol replay fixture set from real `qqq` traces
- [ ] Run same-symbol shadow against `qqq`
- [ ] Compare:
  - entry allow/block
  - submit timing
  - exit reason
  - stop rebuild behavior
  - broker reconcile actions
- [ ] Confirm zero foreign cancel actions
- [ ] Confirm zero foreign flatten actions
- [ ] Confirm zero foreign ledger attribution

Exit gate:

1. shadow sessions produce no cross-strategy contamination

## 4. Test Plan

## 4.1 Unit Tests

Purpose:

Lock down each replica slice before wiring more behavior.

Required coverage:

1. entry gate parity helper logic
2. TP1 / trailing / breakeven / EOD decision rules
3. stop rebuild / self-heal / kill-switch decisions
4. protection dedupe / repeated self-heal / repeated kill-switch suppression
5. manifest parsing and config-only symbol mapping
6. replay comparator normalization
7. runtime restore serialization / deserialization
8. position-update synchronization and flat reset
9. preflight readiness report logic
10. broker-open-orders-aware protection audit miss counting
11. protection audit warning / recovered local audit emission
12. fail-closed data-integrity branches from age/base-source semantics
13. startup-anchor embargo by ET time window
14. QQEW-decoupled confirm-gate bypass

Must pass:

1. focused replica suites
2. manifest/unit suites
3. proof tooling unit suites

## 4.2 Integration Tests

Purpose:

Validate service-to-service behavior across strategy shell, execution adapter, and isolation boundaries.

Required cases:

1. replica entry submit -> order update -> live convergence
2. restart with live runtime snapshot -> stop rebuild
3. position update with missing protection -> self-heal
4. zero position update -> flat reset
5. multiple symbols loaded from manifest in one strategy instance
6. side-specific execution symbol mapping works for long and short
7. foreign execution symbol update is ignored
8. foreign position snapshot is ignored
9. duplicate protection rebuild request is deduped
10. duplicate kill-switch request is deduped
11. replay export + compare path works from audit artifacts
12. preflight CLI reports ready/fail for representative snapshots
13. protection audit uses broker open orders when available and runtime fallback when unavailable
14. protection audit emits warning/recovered evidence that replay/proof tooling can consume

## 4.3 Isolation / Regression Tests

Purpose:

Prove new replica work does not degrade existing safe isolation behavior.

Required cases:

1. `index_super_win` still uses `iswv2:*` only
2. `qqq` cleanup paths do not match `iswv2:*`
3. same-symbol contention still blocks safely
4. close-only flatten still uses strategy-owned qty only
5. runtime reconciler still refuses account-level position sync
6. multi-strategy isolation smoke remains green

## 4.4 Replay Tests

Purpose:

Use deterministic evidence, not subjective log reading, to prove parity.

Required fixture groups:

1. entry allow fixture
2. entry blocked fixture
3. target / TP1 fixture
4. breakeven / stop fixture
5. restart stop rebuild fixture
6. restart missing-protection kill-switch fixture
7. same-symbol conflict fixture
8. synthetic gate/exit replay fixture catalog
9. shadow session parity report over captured same-symbol samples
10. same-symbol shadow proof runner over one captured session
11. batch export of nearby replica audit traces for one captured same-symbol session
12. batch export of nearby `qqq` audit traces for one captured same-symbol session
13. dual-strategy same-symbol proof runner over one captured session
14. daily same-symbol proof entrypoint over one dated session directory
15. daily summary comparison builder over one dated dual-strategy proof package
16. dual-strategy foreign-safety report over blocked samples + audit evidence
17. dual-strategy enablement attestation over proof package outputs
18. batch dual-proof runner over dated real-session directories
19. real-session proof-readiness report over dated sample + audit roots
20. real-session proof pipeline over readiness + batch proof
21. real-session proof daily summary over pipeline outputs
22. automation wrappers / launchd templates for capture + proof + summary
23. final enablement checklist over:
   - config disabled-state
   - durable preflight JSON
   - real-session proof daily summary
   - batch proof green-day thresholds
24. disabled-state preflight export over:
   - durable runtime snapshot
   - manifest-derived flat broker positions
   - preflight JSON output

Compared fields:

1. block reason
2. side
3. qty
4. order-ref kind
5. protection action
6. flatten action
7. runtime transition

## 4.5 Shadow Tests

Purpose:

Prove same-symbol coexistence before any live cut-in.

Required sessions:

1. replica alone, single symbol
2. replica alone, multi-symbol
3. replica with `qqq`, different symbols
4. replica with `qqq`, same symbol for at least 3 full sessions

Shadow checks:

1. zero foreign cancel
2. zero foreign flatten
3. zero foreign ownership claim
4. zero reverse entry
5. zero duplicate entry
6. matching gate/submit/exit explanations

## 4.6 Pre-Enable Checks

Purpose:

No manual enablement without a hard readiness report.

Required checks:

1. config and manifest complete
2. runtime snapshot sane
3. protection status sane
4. observability outputs healthy
5. replay fixtures available
6. shadow proof already green

## 5. Acceptance Checklist

All items below must be green before enablement.

## 5.1 Functional Acceptance

- [ ] single-symbol entry behavior matches `qqq`
- [ ] single-symbol exit behavior matches `qqq`
- [ ] TP1 / trailing / breakeven / EOD behavior matches `qqq`
- [ ] restart protection rebuild behavior matches `qqq`
- [ ] kill-switch behavior is deterministic and tested
- [x] protection dedupe state exists beyond boolean `protection_active`
- [x] protection audit miss-count thresholds are deterministic and tested
- [x] protection audit warning / recovered evidence is deterministic and tested

## 5.2 Safety Acceptance

- [ ] no foreign cancel
- [ ] no foreign flatten
- [ ] no foreign position claim
- [ ] no foreign ledger attribution
- [ ] same-symbol contention blocks instead of taking over
- [ ] live position never remains silently unprotected
- [x] foreign strategy order/position updates do not trigger replica self-heal or kill-switch

## 5.3 Modularity Acceptance

- [ ] new symbols are added by config only
- [ ] no ticker-specific `if/else` logic is required for rollout
- [ ] single strategy instance can load multiple symbol manifests
- [ ] one symbol failure does not corrupt another symbol state

## 5.4 Proof Acceptance

- [ ] replay parity fixtures are green
- [ ] same-symbol shadow sessions are green
- [ ] multi-strategy isolation regression remains green
- [ ] preflight report is green
- [ ] final enablement checklist is green
- [x] foreign-safety report generation is green on synthetic/session test fixtures
- [x] enablement attestation generation is green on synthetic/session test fixtures
- [x] real-session batch proof runner is green on synthetic dated-session fixtures
- [x] real-session proof-readiness generation is green on synthetic readiness fixtures
- [x] real-session proof pipeline is green on synthetic readiness/batch fixtures
- [x] real-session proof daily summary is green on synthetic readiness/batch fixtures
- [x] final enablement checklist generation is green on synthetic fixtures
- [x] automation shell wrappers pass shell syntax validation
- [x] disabled-state preflight export is green on synthetic/local fixtures
- [x] post-close bundle completes cleanly under non-green evidence states

## 5.5 Enablement Acceptance

- [ ] replica strategy is still disabled until all above items pass
- [ ] live cut-in uses controlled rollout, not direct replacement
- [ ] first enablement happens only after explicit readiness review

## 6. Current Test Snapshot

Latest verified results at document update time:

1. `./venv/bin/pytest tests/unit/strategies/test_index_super_win_replica_gate.py tests/unit/strategies/test_index_super_win_replica_strategy.py tests/unit/strategies/test_index_super_win_replica_core.py -q`
   - `72 passed`
2. `./venv/bin/pytest tests/unit/tool/test_build_dual_strategy_enablement_attestation.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py -q`
   - `6 passed`
3. `./venv/bin/pytest tests/unit/tool/test_run_real_session_dual_proof_batch.py tests/unit/tool/test_build_dual_strategy_enablement_attestation.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py -q`
   - `8 passed`
4. `./venv/bin/pytest tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_dual_proof_batch.py tests/unit/tool/test_build_dual_strategy_enablement_attestation.py -q`
   - `6 passed`
5. `./venv/bin/pytest tests/unit/tool/test_build_real_session_proof_daily_summary.py tests/unit/tool/test_run_real_session_proof_pipeline.py tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_dual_proof_batch.py -q`
   - `8 passed`
6. `./venv/bin/pytest tests/unit/tool/test_build_replica_final_enablement_checklist.py tests/unit/tool/test_index_super_win_replica_preflight.py tests/unit/tool/test_index_super_win_replica_preflight_cli_output.py tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_proof_pipeline.py tests/unit/tool/test_build_real_session_proof_daily_summary.py tests/unit/tool/test_build_dual_strategy_enablement_attestation.py tests/unit/tool/test_run_real_session_dual_proof_batch.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py -q`
   - `21 passed`
7. `./venv/bin/pytest tests/unit/strategies/test_index_super_win_replica_runtime_snapshot.py tests/unit/tool/test_build_replica_flat_broker_snapshot.py tests/unit/tool/test_build_replica_final_enablement_checklist.py tests/unit/tool/test_index_super_win_replica_preflight.py tests/unit/tool/test_index_super_win_replica_preflight_cli_output.py tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_proof_pipeline.py tests/unit/tool/test_build_real_session_proof_daily_summary.py -q`
   - `15 passed`
8. `bash -n scripts/automation/real_session_shadow_capture.sh scripts/automation/real_session_proof_pipeline.sh scripts/automation/real_session_proof_daily_summary.sh scripts/automation/real_session_enablement_checklist.sh scripts/automation/real_session_preflight_export.sh scripts/automation/real_session_postclose_bundle.sh scripts/setup_real_session_proof_launchd.sh scripts/uninstall_real_session_proof_launchd.sh`
   - `passed`
9. `bash scripts/automation/real_session_postclose_bundle.sh`
   - `RC=0` with non-green evidence state handled as expected
10. `./venv/bin/pytest tests/unit/tool/test_index_super_win_replica_preflight.py tests/unit/tool/test_compare_index_super_win_shadow.py tests/unit/tool/test_index_super_win_shadow_session_tools.py tests/unit/tool/test_build_expected_trace_from_shadow_sample.py tests/unit/tool/test_build_shadow_session_parity_report.py tests/unit/tool/test_run_same_symbol_shadow_proof.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_real_session_dual_proof_batch.py tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_proof_pipeline.py tests/unit/tool/test_build_real_session_proof_daily_summary.py tests/unit/tool/test_export_strategy_traces_for_shadow_session.py tests/unit/tool/test_export_replica_traces_for_shadow_session.py tests/unit/tool/test_build_replica_fixture_catalog.py tests/unit/tool/test_run_replica_fixture_replay.py tests/unit/tool/test_build_dual_strategy_daily_summary_compare.py tests/unit/tool/test_build_dual_strategy_foreign_safety_report.py tests/unit/tool/test_build_dual_strategy_enablement_attestation.py tests/unit/strategies/test_index_super_win_replica_gate.py tests/unit/strategies/test_index_super_win_replica_core.py tests/unit/strategies/test_index_super_win_replica_strategy.py tests/unit/strategies/test_index_super_win_manifest.py tests/unit/strategies/test_index_super_win_strategy.py tests/unit/strategies/test_namespace_isolation_contract.py tests/unit/services/strategy_runtime_v2/test_parity_recorder.py tests/unit/tool/test_qqq_index_replica_replay.py tests/unit/tool/test_build_qqq_replica_baseline.py tests/unit/tool/test_export_audit_trace.py -q`
   - `165 passed`
11. `./venv/bin/python tool/smoke/test_multi_strategy_isolation_chain.py`
   - `10/10 passed`

## 7. Usage Rule

When the implementation continues, update this document first when:

1. a TODO item becomes complete
2. a new blocking gap is found
3. acceptance scope changes
4. latest verified test counts change
