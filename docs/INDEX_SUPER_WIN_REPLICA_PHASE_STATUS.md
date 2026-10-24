# INDEX_SUPER_WIN Replica Phase Status

## Scope

This document records what is already landed for the new `index_super_win` replica path, what was validated, and what remains incomplete.

This is a phase-status and acceptance checkpoint, not a declaration that 100% `qqq` parity is finished.

For the current master TODO, test plan, and enablement acceptance checklist, use:

1. `./docs/INDEX_SUPER_WIN_REPLICA_TODO_ACCEPTANCE.md`

## Safety Status

1. `index_super_win_001` remains disabled in `./config.yml`.
2. The new replica strategy is not wired into live config.
3. No new replica code is running in production.

## Landed Components

### 1. Baseline Freeze

1. `qqq` baseline generator: `./tool/build_qqq_replica_baseline.py`
2. Baseline JSON: `./docs/baselines/qqq_intraday_001_replica_baseline.json`
3. Baseline MD: `./docs/baselines/QQQ_INTRADAY_001_REPLICA_BASELINE.md`

### 2. Replay / Parity Proof Tooling

1. Parity recorder: `./services/strategy_runtime_v2/parity_recorder.py`
2. Replay compare CLI: `./tool/replay/qqq_index_replica_replay.py`
3. Audit trace exporter: `./tool/replay/export_audit_trace.py`
4. Synthetic fixture catalog builder: `./tool/replay/build_replica_fixture_catalog.py`
5. Synthetic fixture catalog JSON: `./docs/baselines/index_super_win_replica_fixture_catalog.json`
6. Fixture replay runner: `./tool/replay/run_replica_fixture_replay.py`
7. Shadow sample -> expected trace builder: `./tool/replay/build_expected_trace_from_shadow_sample.py`
8. Shadow session parity report builder: `./tool/replay/build_shadow_session_parity_report.py`
9. Same-symbol shadow proof runner: `./tool/replay/run_same_symbol_shadow_proof.py`
10. Replica trace exporter for captured shadow sessions: `./tool/replay/export_replica_traces_for_shadow_session.py`
11. Generic strategy trace exporter for captured shadow sessions: `./tool/replay/export_strategy_traces_for_shadow_session.py`
12. Dual-strategy same-symbol proof runner: `./tool/replay/run_dual_strategy_same_symbol_proof.py`
13. Daily same-symbol proof entrypoint with default session/proof directories: `./tool/replay/run_daily_dual_strategy_same_symbol_proof.py`
14. Daily summary comparison builder for `qqq` vs replica same-symbol proof: `./tool/replay/build_dual_strategy_daily_summary_compare.py`
15. Foreign-safety report builder for dual-strategy same-symbol proof: `./tool/replay/build_dual_strategy_foreign_safety_report.py`
16. Enablement attestation builder for dual-strategy same-symbol proof: `./tool/replay/build_dual_strategy_enablement_attestation.py`
17. Real-session batch dual-proof runner over dated sample directories: `./tool/replay/run_real_session_dual_proof_batch.py`
18. Real-session proof-readiness report builder: `./tool/replay/build_real_session_proof_readiness.py`
19. Real-session proof pipeline orchestrator: `./tool/replay/run_real_session_proof_pipeline.py`
20. Real-session proof daily summary builder: `./tool/replay/build_real_session_proof_daily_summary.py`
21. Final enablement checklist builder: `./tool/replay/build_replica_final_enablement_checklist.py`
22. Flat broker snapshot builder: `./tool/build_replica_flat_broker_snapshot.py`
23. Automation wrappers:
   - `./scripts/automation/real_session_shadow_capture.sh`
   - `./scripts/automation/real_session_proof_pipeline.sh`
   - `./scripts/automation/real_session_proof_daily_summary.sh`
   - `./scripts/automation/real_session_enablement_checklist.sh`
   - `./scripts/automation/real_session_preflight_export.sh`
   - `./scripts/automation/real_session_postclose_bundle.sh`
24. Launchd templates and installers:
   - `./ops/launchd/com.trading.real_session.shadow_capture.intraday.plist`
   - `./ops/launchd/com.trading.real_session.proof.postclose.plist`
   - `./ops/launchd/com.trading.real_session.proof.summary.postclose.plist`
   - `./ops/launchd/com.trading.real_session.postclose.bundle.plist`
   - `./scripts/setup_real_session_proof_launchd.sh`
   - `./scripts/uninstall_real_session_proof_launchd.sh`

### 3. Config-Only Symbol Rollout Foundation

1. Manifest loader: `./strategies/index_super_win_manifest.py`
2. Manifest-aware bindings: `./strategies/index_super_win_components.py`
3. Example config: `./configs/index_super_win/qqq_replica_manifest.example.yml`

### 4. Replica Core Slices

1. Types: `./strategies/index_super_win_replica_types.py`
2. Core: `./strategies/index_super_win_replica_core.py`
3. Strategy shell: `./strategies/index_super_win_replica_strategy.py`

Landed behavior slices:

1. entry intent build
2. submit result handling
3. entry order-update convergence
4. protective stop intent build
5. flatten intent build
6. TP1 partial-exit intent build
7. pure position-management decision rules for:
   - TP1
   - trailing stop
   - breakeven shield
   - forced EOD flatten
8. strategy-shell wiring for:
   - dry-run preview
   - live entry submit
   - filled -> live convergence
   - live TP1 partial submit
   - live breakeven stop submit
   - live EOD flatten submit
   - restart runtime restore
   - restart protective-stop rebuild preview/submit
   - restart unprotected-blocked fail-safe
   - broker position update -> live qty sync
   - broker position update -> missing protection self-heal
   - broker position update -> flat reset
   - pre-enable readiness / preflight CLI
   - partial replica entry gate helper
   - seed/freshness/direction gate coverage
   - unprotected-live kill-switch fail-safe
   - exit order-update convergence
   - short-side TP1 / breakeven / protective-stop direction parity
   - explicit live-protection audit entrypoint with:
     - stop-live audit state
     - inactive-stop self-heal state
     - missing-stop kill-switch state
   - local audit bridge for:
     - gate-blocked
     - entry preview / submit
     - order-update convergence
     - position sync
     - protection audit

## Acceptance Completed In This Phase

### Code-Level Acceptance

1. Multi-symbol config foundation exists and does not require ticker-specific logic branches in strategy shell.
2. Side-specific execution symbol mapping works for `LONG` and `SHORT`.
3. Replica strategy can keep live state by signal symbol while receiving order updates on execution symbols.
4. Replica strategy can submit exit intents from live state without touching any live config.
5. Short-side exit intents now use signed quantity semantics instead of reusing long-only assumptions.
6. Replica path now emits local audit records, so replay/export tools can consume real replica traces instead of only previews.
7. Shadow session samples can now be converted into per-sample expected traces and batch parity reports for same-symbol proof.
8. One top-level proof command now exists to combine session report + parity report into one same-symbol proof artifact.
9. Captured shadow samples can now be matched back to nearby replica audit traces by symbol, time window, and strategy id.
10. The same trace-export flow is now reusable for `qqq` and replica, not only replica-side proof.
11. One dual-strategy proof command now exists to export `qqq`, export replica, and build the replica-side same-symbol proof in one run.
12. One daily proof entrypoint now exists so captured session samples can be turned into a dated proof package without manually wiring directories.
13. Replica gate helper now covers additional stable `qqq`-style guards for continuity, ATR readiness, and shadow-extreme overextension under explicit config flags.
14. Replica protection handling now tracks pending protection / pending kill-switch state so repeated self-heal and repeated flatten fail-safe requests can be deduped instead of resubmitted.
15. Replica now ignores foreign `strategy_id` order updates and foreign `strategy_id` position snapshots before any self-heal or kill-switch logic can run.
16. Synthetic replay catalog now includes a restart missing-protection kill-switch fixture, so protection failure replay is no longer purely implied by unit tests.
17. Replica protection audit now checks broker open orders when available, resets miss counts on broker-visible stop recovery, and uses configurable miss thresholds before self-heal or kill-switch.
18. Replica protection audit now emits explicit `warning`, `self_heal`, and `recovered` audit stages, so replay/proof tooling can distinguish transient stop loss disappearance from actual fail-safe actions.
19. Replica pure position-management now covers `logic_retreat` and first add-on submission semantics, including weighted average entry-price update after add intent emission.
20. Replica dynamic stop widening now mirrors the high-value `qqq` widening contract: live ATR input, hard-cap at `1.5x` pre-stop distance, and stop resubmission only when distance meaningfully increases.
21. Dual-strategy same-symbol proof now also produces a day-level summary comparison for gate reason, submit timing, stop-rebuild visibility, and flatten reason parity.
22. Dual-strategy same-symbol proof now also produces a foreign-safety report that flags blocked-sample forbidden replica actions and high-risk attribution evidence from audit rows.
23. Replica gate helper now covers higher-fidelity `qqq` stable-entry semantics for fail-closed age checks, opening-window unknown base source, startup-anchor embargo windows, and QQEW-decoupled confirm-gate bypass.
24. Dual-strategy same-symbol proof now also produces an enablement attestation with explicit evidence thresholds and zero-foreign-action checks for cut-in gating.
25. The proof stack now has a real-session batch runner, so dated shadow-session directories can be turned into one multi-day evidence report without manual day-by-day execution.
26. The proof stack now has a real-session readiness report, so the machine can state whether there are enough dated samples and audit files to run real dual proof at all.
27. The proof stack now has a real-session pipeline runner, so one command can stop at readiness-only when inputs are missing or continue into batch proof when ready.
28. The proof stack now has a real-session daily summary, so operators can read one compact JSON instead of manually inspecting readiness, pipeline, and batch outputs.
29. The real-session proof path now has fixed automation wrappers and launchd jobs, so intraday sample capture and post-close proof collection can run on a schedule without touching `run_system`.
30. The replica enablement path now has a final machine checklist, so config state, preflight state, and proof state are merged into one blocker list before any enable attempt.
31. The disabled-state preflight path now has durable runtime snapshot persistence plus a manifest-derived flat broker snapshot export, so missing-input preflight failures are no longer blocking local readiness checks.
32. The post-close evidence path now has a bundle wrapper, so preflight, proof, summary, and checklist run in one ordered pass without noisy failures when evidence is still non-green.

### Test Acceptance

Focused suites passed:

1. `./venv/bin/pytest tests/unit/strategies/test_index_super_win_replica_gate.py tests/unit/strategies/test_index_super_win_replica_strategy.py tests/unit/strategies/test_index_super_win_replica_core.py -q`
   - result: `72 passed`
2. `./venv/bin/pytest tests/unit/tool/test_build_dual_strategy_daily_summary_compare.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py -q`
   - result: `6 passed`
3. `./venv/bin/pytest tests/unit/tool/test_build_dual_strategy_foreign_safety_report.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py -q`
   - result: `5 passed`
4. `./venv/bin/pytest tests/unit/tool/test_build_dual_strategy_enablement_attestation.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py -q`
   - result: `6 passed`
5. `./venv/bin/pytest tests/unit/tool/test_run_real_session_dual_proof_batch.py tests/unit/tool/test_build_dual_strategy_enablement_attestation.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py -q`
   - result: `8 passed`
6. `./venv/bin/pytest tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_dual_proof_batch.py tests/unit/tool/test_build_dual_strategy_enablement_attestation.py -q`
   - result: `6 passed`
7. `./venv/bin/pytest tests/unit/tool/test_build_real_session_proof_daily_summary.py tests/unit/tool/test_run_real_session_proof_pipeline.py tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_dual_proof_batch.py -q`
   - result: `8 passed`
8. `./venv/bin/pytest tests/unit/tool/test_build_replica_final_enablement_checklist.py tests/unit/tool/test_index_super_win_replica_preflight.py tests/unit/tool/test_index_super_win_replica_preflight_cli_output.py tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_proof_pipeline.py tests/unit/tool/test_build_real_session_proof_daily_summary.py tests/unit/tool/test_build_dual_strategy_enablement_attestation.py tests/unit/tool/test_run_real_session_dual_proof_batch.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py -q`
   - result: `21 passed`
9. `bash -n scripts/automation/real_session_shadow_capture.sh scripts/automation/real_session_proof_pipeline.sh scripts/automation/real_session_proof_daily_summary.sh scripts/automation/real_session_enablement_checklist.sh scripts/automation/real_session_preflight_export.sh scripts/automation/real_session_postclose_bundle.sh scripts/setup_real_session_proof_launchd.sh scripts/uninstall_real_session_proof_launchd.sh`
   - result: passed
10. `bash scripts/automation/real_session_postclose_bundle.sh`
   - result: `RC=0` with non-green evidence state handled as expected
11. `./venv/bin/python -m compileall strategies/index_super_win_replica_gate.py strategies/index_super_win_replica_strategy.py tool/replay/build_dual_strategy_enablement_attestation.py tool/replay/run_dual_strategy_same_symbol_proof.py tool/replay/run_daily_dual_strategy_same_symbol_proof.py tool/replay/run_real_session_dual_proof_batch.py tool/replay/build_real_session_proof_readiness.py tool/replay/run_real_session_proof_pipeline.py tool/replay/build_real_session_proof_daily_summary.py tool/replay/build_replica_final_enablement_checklist.py tool/index_super_win_replica_preflight.py tool/build_replica_flat_broker_snapshot.py`
   - result: passed

Broader relevant suites passed:

1. `./venv/bin/pytest tests/unit/tool/test_index_super_win_replica_preflight.py tests/unit/tool/test_compare_index_super_win_shadow.py tests/unit/tool/test_index_super_win_shadow_session_tools.py tests/unit/tool/test_build_expected_trace_from_shadow_sample.py tests/unit/tool/test_build_shadow_session_parity_report.py tests/unit/tool/test_run_same_symbol_shadow_proof.py tests/unit/tool/test_run_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_daily_dual_strategy_same_symbol_proof.py tests/unit/tool/test_run_real_session_dual_proof_batch.py tests/unit/tool/test_build_real_session_proof_readiness.py tests/unit/tool/test_run_real_session_proof_pipeline.py tests/unit/tool/test_build_real_session_proof_daily_summary.py tests/unit/tool/test_export_strategy_traces_for_shadow_session.py tests/unit/tool/test_export_replica_traces_for_shadow_session.py tests/unit/tool/test_build_replica_fixture_catalog.py tests/unit/tool/test_run_replica_fixture_replay.py tests/unit/tool/test_build_dual_strategy_daily_summary_compare.py tests/unit/tool/test_build_dual_strategy_foreign_safety_report.py tests/unit/tool/test_build_dual_strategy_enablement_attestation.py tests/unit/strategies/test_index_super_win_replica_gate.py tests/unit/strategies/test_index_super_win_replica_core.py tests/unit/strategies/test_index_super_win_replica_strategy.py tests/unit/strategies/test_index_super_win_manifest.py tests/unit/strategies/test_index_super_win_strategy.py tests/unit/strategies/test_namespace_isolation_contract.py tests/unit/services/strategy_runtime_v2/test_parity_recorder.py tests/unit/tool/test_qqq_index_replica_replay.py tests/unit/tool/test_build_qqq_replica_baseline.py tests/unit/tool/test_export_audit_trace.py -q`
   - result: `165 passed`
2. `./venv/bin/python tool/smoke/test_multi_strategy_isolation_chain.py`
   - result: `10/10 passed`

## Still Not Accepted

The following are still open, so 100% `qqq` replica is not yet complete:

1. full `qqq` entry gate parity beyond the currently landed synthetic replay fixture set
2. full exit lifecycle wiring beyond the currently landed minimum path, especially deeper post-TP1 and protection-audit branches
3. same-symbol shadow parity against real `qqq` session exports
4. live cut-in proof with conflict-free runtime evidence

## Next Required Acceptance Gates

1. replay fixture proof for single-symbol `qqq` entry/exit/protection slices
2. restart recovery parity proof
3. same-symbol shadow proof against `qqq`
4. multi-symbol config-only load proof
5. cut-in gate with foreign-order/foreign-position zero-conflict evidence

## Current Machine Blockers

Current machine-generated checklist:

`./runtime/state/index_super_win_same_symbol_proof/final_enablement_checklist.json`

Current blockers:

1. `no_ready_real_session_day`
2. `real_session_pipeline_not_completed`
3. `insufficient_green_proof_days`
4. `insufficient_green_attestation_days`
