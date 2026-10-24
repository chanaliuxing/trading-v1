# INDEX_SUPER_WIN Execution Master TODO / Test Plan / Acceptance

## 1. Purpose

This document is the single factual execution source for `index_super_win_001` as of `2026-03-25`.

It is intended to replace fragmented status tracking across older replica/TODO/status docs when making rollout decisions.

This document answers four questions:

1. What is already landed in code.
2. What is still not complete.
3. What must be tested next.
4. What must be true before the strategy can be treated as production-safe.

## 2. Factual Current State

### 2.1 Code / Config / Evidence Are Not In The Same State

Current facts:

1. `config.yml` currently has `index_super_win_001` set to `enabled: true`.
2. `index_super_win_001` points to:
   - module: `strategies.index_super_win_replica_strategy`
   - class: `IndexSuperWinReplicaStrategy`
3. Final machine checklist is still `ready_to_enable=false`.
4. Current machine blockers are:
   - `no_ready_real_session_day`
   - `real_session_pipeline_not_completed`
   - `insufficient_green_proof_days`
   - `insufficient_green_attestation_days`
5. Current observed proof state is:
   - `green_days = 0`
   - `attestation_green_days = 0`
   - `pipeline_status = readiness_only`
6. Current trace parity evidence for `2026-03-25` is:
   - `seed_sample_count = 7`
   - `pass_count = 0`
   - `fail_count = 0`
   - `missing_count = 7`

Pragmatic conclusion:

1. Code and tooling have progressed materially.
2. Evidence is still not sufficient to claim `100%` parity.
3. The biggest rollout risk right now is not “known direct isolation bug in tests”; it is “config already enabled while proof is still non-green”.

## 3. What Is Landed

The following are already landed and validated at code/test level.

### 3.1 Strategy Runtime / Isolation

1. Replica strategy shell exists.
2. Independent order namespace exists:
   - `iswv2:*`
3. Independent runtime scope exists.
4. Manifest-driven symbol binding exists.
5. Side-specific execution symbol routing exists.
6. Foreign order updates with empty `strategy_id` are filtered by owned namespace.
7. Foreign position updates with missing `strategy_id` are ignored.
8. Same-symbol open-order blocking is symbol-scoped, not global strategy-scoped.
9. Pending-entry stale cleanup exists.
10. Entry timeout cleanup + cooldown exists.
11. Cooldown lifecycle cleanup exists.
12. Restart runtime restore exists.
13. Restart stop rebuild exists.
14. Position-sync protection self-heal exists.
15. Flat-sync orphan-order cleanup exists.
16. Unexpected short inventory flatten protection exists.
17. Protection audit loop exists.
18. Protection dedupe / kill-switch dedupe exists.

### 3.2 Replica Behavior Slices

1. Entry intent build / submit / order-update convergence.
2. TP1 partial exit intent / convergence.
3. Protective stop submit / convergence.
4. Flatten submit / convergence.
5. TP1 / trailing / EOD / breakeven / hard-stop / logic-retreat / add-on slices are wired in replica core.
6. Stop re-entry cooldown exists.
7. Round-trip success counting exists.
8. Derived runtime normalization on live reconcile exists.

### 3.3 Observability / Proof Tooling

1. Local audit bridge exists.
2. Replica now emits trace-bearing stages for allowed entry path.
3. Replica now also emits trace-bearing `gate_evaluated` for blocked/no-attempt branches including:
   - pending decision blocked
   - open orders blocked
   - real position blocked
   - gate blocked
   - signal inactive
   - stop re-entry cooldown
4. Generic strategy trace exporter exists.
5. Dual-strategy trace parity report exists.
6. Seeded parity runner exists.
7. Seeded parity sample builder now supports generic baseline prefixes and aliases, not only `qqq`.
8. Real-session proof readiness / batch / summary / checklist tooling exists.

### 3.4 Recent Verified Test Status

Latest focused verified results in current workspace:

1. `tests/unit/strategies/test_index_super_win_replica_strategy.py`
2. `tests/unit/tool/test_export_strategy_traces_for_shadow_session.py`
3. `tests/unit/tool/test_build_dual_strategy_trace_parity_report.py`
4. `tests/unit/tool/test_run_dual_strategy_trace_parity.py`
5. `tests/unit/tool/test_build_qqq_trace_parity_samples.py`
6. `tests/unit/tool/test_run_seeded_dual_strategy_trace_parity.py`
   - result: `111 passed`
7. `tests/unit/strategies/test_namespace_isolation_contract.py`
8. `tests/unit/exec/test_execution_service_namespace_isolation.py`
9. `tests/unit/exec/test_execution_service_entry_isolation_mode.py`
10. `tests/unit/reconciliation/test_runtime_reconciler_orphans.py`
   - result: `18 passed`
11. After parity tool genericization:
   - tool-focused subset: `13 passed`
   - strategy/isolation subset: `117 passed`

## 4. What Is Still Not Complete

These are the real remaining gaps. This section is intentionally strict.

### 4.1 Not Yet Proven As `100%` QQQ Replica

Still not complete:

1. Real-session parity evidence is missing.
2. `2026-03-25` seeded parity still shows `7/7 missing_trace`.
3. No new post-change trading session has yet proven that the added blocked/no-attempt traces are actually emitted in live runtime.
4. No multi-day `pass/fail` parity evidence exists yet for real sessions.

Conclusion:

`index_super_win_001` is not yet acceptable to describe as “100% replicated qqq strategy”.

### 4.2 Not Yet Fully Proven For Same-Symbol Parallel Runtime

Still not complete:

1. Same-symbol parallel test coverage is strong at unit/integration level.
2. Same-symbol real-session proof is still not green.
3. We still do not have multi-day evidence proving zero:
   - foreign cancel
   - foreign flatten
   - foreign ownership claim
   - same-symbol takeover under broker noise

Conclusion:

Code safety is much better than before, but same-symbol concurrent production confidence is still evidence-limited.

### 4.3 Not Yet Fully Generalized As A Plug-In Strategy

Still not complete:

1. Replica gate/core are much less `QQQ`-hardcoded than before.
2. Main control path is now moving to `primary_*` and binding-driven gate symbols.
3. But compatibility names and `qqq_*` leftovers still exist in config and fallback semantics.
4. Real proof still only covers `QQQ -> TQQQ/SQQQ`.

Conclusion:

It is evolving into a general plug-in strategy, but it is not yet fully proven as a ticker-agnostic production engine.

## 5. Master TODO List

Only the items below should be treated as the active remaining backlog.

### A. Evidence Alignment

- [ ] `EVID-00` Regenerate machine evidence artifacts before any rollout decision.
  - Required artifacts:
    - preflight report
    - real-session summary
    - final enablement checklist
  - Rule:
    - no rollout discussion should rely on stale checklist fields
  - Why:
    - current generated checklist can lag behind live config changes

- [x] `EVID-01` Force config-level safe default while evidence is red.
  - Target:
    - keep `index_super_win_001` disabled in config until fresh proof turns green
  - Why:
    - documentation alone is not an execution guard
    - a red checklist plus `enabled=true` is an avoidable operational risk

- [ ] `EVID-01.5` Add a pre-session trace smoke gate.
  - Required before using the next session as parity evidence:
    - replica emits `strategy_started`
    - replica emits `strategy_ready`
    - replica emits at least one trace-bearing decision-chain event
  - Minimum accepted stages:
    - `gate_evaluated`
    - `entry_gate_blocked`
    - `entry_intent_requested`
  - Why:
    - otherwise the next parity run may again produce only `missing_trace`

- [ ] `EVID-02` Run first post-change seeded parity on a fresh session.
  - Input:
    - new session audit after blocked/no-attempt trace coverage landed
  - Required output:
    - seeded parity report
    - missing/pass/fail breakdown

- [ ] `EVID-03` Convert parity status from `missing_trace` to `pass/fail`.
  - Acceptance:
    - replica must export trace-bearing rows for the same seeded sample set
    - no more “all missing”

- [ ] `EVID-04` Accumulate 3 green proof days.
  - Acceptance:
    - `green_days >= 3`
    - `attestation_green_days >= 3`
    - final checklist green

### B. Parity Gap Closure

- [ ] `PARITY-01` Build a mismatch inventory from the first non-missing dual trace report.
  - Categories:
    - gate reason mismatch
    - missing stage
    - extra stage
    - qty mismatch
    - timing/order of lifecycle mismatch

- [ ] `PARITY-02` Eliminate remaining gate/manage/exit mismatches one category at a time.
  - Rule:
    - no logic “optimization”
    - parity first, cleanup later

- [ ] `PARITY-03` Re-run deterministic parity after each mismatch batch.

### C. Same-Symbol Concurrency Proof

- [ ] `SAFE-01` Capture same-symbol `qqq + index_super_win` session evidence on fresh data.
- [ ] `SAFE-02` Generate foreign-safety report on that session.
- [ ] `SAFE-03` Verify zero forbidden actions on blocked samples:
  - foreign cancel
  - foreign flatten
  - foreign ledger attribution

### D. Generalization / Plug-In Completion

- [ ] `GEN-01` Remove remaining non-essential `qqq_*` compatibility usage from replica primary path.
- [ ] `GEN-02` Make baseline parity sample tooling use neutral naming in documentation and ops wrappers.
- [ ] `GEN-03` Prove one non-QQQ manifest path with the same proof pipeline.

### E. Ops / Cut-In Control

- [ ] `OPS-01` Update rollout runbook with explicit paper/live state.
- [ ] `OPS-02` Define one rollback command path for replica only.
- [ ] `OPS-03` Ensure post-close proof bundle is the default decision source for enable/disable.

## 6. Test Plan

### 6.1 Unit Tests

Goal:

Prove local logic and tool selection are correct.

Required coverage:

1. Blocked/no-attempt gate branches emit trace-bearing `gate_evaluated`.
2. Empty `strategy_id` foreign order/position events are ignored.
3. Open-order ownership remains namespace-scoped.
4. Pending timeout / cooldown lifecycle stays deterministic.
5. Exported trace selection prefers decision-chain traces over noise traces.
6. Seeded parity sample builder supports generic baseline prefix and alias mapping.

Pass rule:

1. All targeted unit suites green.

### 6.2 Integration Tests

Goal:

Prove service isolation still holds after parity/trace changes.

Required coverage:

1. Namespace isolation contract.
2. Execution entry isolation mode.
3. Execution namespace isolation.
4. Reconciliation orphan handling.
5. Same-symbol ownership filtering in strategy runtime.

Pass rule:

1. No regression in isolation suites.

### 6.3 Deterministic Replay / Parity Tests

Goal:

Turn parity from “missing” into “measured”.

Required steps:

1. Seed baseline trace samples from fresh baseline strategy audit.
2. Export baseline traces.
3. Export replica traces.
4. Produce dual trace parity report.
5. Classify each sample as:
   - pass
   - fail
   - missing_trace

Pass rule:

1. `missing_trace` is no longer dominant.
2. Remaining failures have concrete mismatch categories.

### 6.4 Real Session Proof

Goal:

Prove runtime behavior under real broker/market noise.

Required outputs per day:

1. same-symbol proof package
2. foreign-safety report
3. enablement attestation
4. daily summary
5. final checklist input artifacts

Pass rule:

1. zero high-value isolation events
2. attestation green
3. day counted as green

## 7. Acceptance Checklist

All items below must be true before claiming the strategy is “fully landed”.

### 7.1 Code Acceptance

- [x] Replica module/class are wired.
- [x] Namespace isolation exists.
- [x] Strategy-scoped order/position handling is in place.
- [x] Trace-bearing blocked/no-attempt observability is in place.
- [x] Proof tooling exists and is runnable.

### 7.2 Safety Acceptance

- [x] Unit/integration isolation suites are green.
- [ ] Same-symbol real-session proof shows zero forbidden foreign actions.
- [ ] No multi-day broker/runtime conflict evidence remains unresolved.

### 7.3 Parity Acceptance

- [ ] Fresh seeded parity produces actual replica traces for expected samples.
- [ ] Dual trace parity is measuring `pass/fail`, not mostly `missing`.
- [ ] Critical parity categories are zero:
  - side mismatch
  - block-reason mismatch
  - duplicate entry action
  - reverse entry action
  - foreign cleanup action

### 7.4 Enablement Acceptance

- [ ] `final_enablement_checklist.json` reports `ready_to_enable=true`
- [ ] enablement decision is based on freshly regenerated artifacts, not stale files
- [ ] `green_days >= 3`
- [ ] `attestation_green_days >= 3`
- [ ] `high_value_isolation_event_count == 0`

## 8. Immediate Next Actions

This is the shortest high-value sequence from the current state.

1. Freeze rollout intent:
   - paper-only or disabled-until-green
2. Run the first fresh session after the new trace coverage changes.
3. Re-run seeded parity immediately after that session.
4. Convert `missing_trace` into actual `pass/fail` evidence.
5. Fix only the mismatches revealed by that report.
6. Repeat until:
   - seeded parity is mostly `pass/fail`
   - same-symbol proof is green
   - 3 green days are accumulated

## 9. Final Verdict Today

Current verdict on `2026-03-25`:

1. Significant code hardening and tooling are landed.
2. The strategy is not yet acceptable to call:
   - `100% perfect qqq replica`
   - `production-ready same-symbol concurrent engine`
   - `fully proven generic plug-in strategy`
3. The remaining work is now concentrated in proof/evidence, not large new architecture.
4. The next correct move is not broad refactor; it is disciplined evidence collection and mismatch closure.
