# Scoped Tradability Architecture Review

Date: 2026-05-12

## Scope

This review is RCA plus contract plus red tests. It does not connect to IBKR,
does not place/cancel/modify orders, and does not change production execution
behavior.

Files changed:

- `docs/architecture/scoped_tradability_contract.md`
- `docs/reports/scoped_tradability_architecture_review_20260512.md`
- `docs/reports/artifacts/scoped_tradability_architecture_review_20260512.json`
- `tests/unit/services/test_scoped_tradability_contract.py`

Production files changed: no.

## Baseline

- `tests/unit/safety/test_no_live_ibkr_in_unit_tests.py`: 4 passed.
- `tests/unit/exec`: 293 passed.
- `tests/unit/reconciliation`: 76 passed.
- `tests/unit/reports`: 61 passed.
- `tests/unit/services`: 656 passed.
- `scripts/run_ib_reconnect_regression.sh fast`: 32 passed, live IBKR disabled.
- `git diff --check`: passed.

Live marker search note: repository logs contain historical IBKR connection,
portfolio, and execution-detail markers. Process inspection also found
pre-existing `run_system.py` and IB Gateway processes. This RCA/test run did
not start live IBKR, did not call live order placement, and ran tests with
`TRADING_TEST_ALLOW_LIVE_IBKR=0`.

## Current Gate Map

| Location | Current Gate | Scope | Flow Affected | Global or Scoped? | Risk | Planned Contract |
| --- | --- | --- | --- | --- | --- | --- |
| `services/exec/execution_service.py:643` | `manual_intervention_lock_active` before entry | `strategy_id + symbol` | Entry only | Scoped but incomplete | Missing `account_id` and `strategy_instance_id` in lock key can overblock same-symbol strategies | Move into TradabilityService as strategy-instance scoped block |
| `services/exec/execution_service.py:682` | unresolved quarantine plus trading gate | symbol plus optional owner hints | Entry only | Mixed | Execution has its own gate separate from production state | TradabilityService owns decision; execution performs second check |
| `services/exec/execution_service.py:760` | live entry owner identity guard | account/strategy instance required only in live entry | Entry | Scoped | Good owner guard, but separate from tradability authority | Preserve as OMS ownership contract, include owner evidence in decision |
| `services/exec/execution_service.py:938` | close-only guard | owner/symbol from extras | Exit/flatten/protection | Scoped | Correctly protects reversal, but not expressed as tradability override | Keep guard and record FLOW_OVERRIDE evidence |
| `tool/ops/run_runtime_operations_watchdog.py:3113` | `entry_go_no_go` derived as go/no_go | global artifact | Entry readiness | Global coarse | Cannot express partial entry allow | Add `partial`, `blocked_scopes`, and flow overrides |
| `tool/ops/run_runtime_operations_watchdog.py:3131` | `go_no_go` mirrors `safety_go_no_go` | platform | All production readiness | Global | Correct for platform trust, but not sufficient execution authority | Keep global halt, make ProductionState non-authorizing |
| `tool/ops/run_runtime_operations_watchdog.py:1720` | position safety missing/stale/no_go | account/platform artifact | All entry and normal override | Global | Correct hard blocker | Keep GLOBAL_HALT unless future records prove account/symbol scoped divergence |
| `tool/ops/run_runtime_operations_watchdog.py:1334` | broker truth snapshot missing/stale/not backed | platform/account artifact | All entry and normal override | Global | Correct hard blocker | Keep GLOBAL_HALT |
| `tool/ops/run_runtime_operations_watchdog.py:2112` | runtime version freshness | runtime process | All entry | Global | Correct hard blocker | Keep GLOBAL_HALT |
| `tool/ops/run_runtime_operations_watchdog.py:2163` | manual recovery required | gateway/system health | All entry and normal override | Global | Correct hard blocker | Keep GLOBAL_HALT |
| `services/production/strategy_tradability.py:246` | heartbeat-derived tradable entry readiness | strategy only | Entry readiness | Scoped but aggregate emits global no_go | Strategy blockers can force global entry no_go | Convert records into scoped blockers |
| `strategies/stocks_kernel/strategy.py:725` | direct `_system_paused` early return | strategy runtime | Entry signal path, but still runs safety invariants | Global coarse | Strategy can bypass future scoped permission model by returning before requesting authorization | Replace entry return with TradabilityService request in later phase |
| `strategies/stocks_kernel/strategy.py:1585` | session entry window decision | strategy/session | Entry | Scoped | Session/EOD policy is embedded in strategy | Move session close policy into TradabilityService contract |
| `strategies/stocks_kernel/strategy.py:4417` | EOD flatten due check | strategy owned lots | Strategy flatten | Scoped | Good owner-lot flatten direction, but no unified override decision | Add STRATEGY_FLATTEN decision before submit |
| `services/production/session_end_supervisor.py:109` | no-overnight broker position verification | strategy/symbol policy | Recovery/manual lock | Scoped data, global no_go artifact | Violation escalates production state globally | Express as scoped session/account-symbol block unless global/manual recovery required |
| `services/exec/manual_intervention_lock.py:15` | lock key | `strategy_id + symbol` | Entry | Scoped but insufficient | Same strategy id across accounts/instances can collide | Add account and strategy instance to target key |

## RCA

The current architecture already distinguishes some safety blockers from entry
blockers, but the final artifact is still coarse. `ProductionState` can say
`entry_go_no_go=no_go` without showing which scope is blocked. `ExecutionService`
then applies manual lock, quarantine, trading gate, owner identity, risk, and
close-only checks independently. That creates the exact split-brain risk: the
operator artifact may look broadly green while execution rejects a scoped entry,
or execution may allow a close/protective flow without a single explainable
tradability decision.

The correct direction is to keep the hard global halt path and introduce a
single scoped authorization source. `ProductionState` should become platform
trust plus scoped blocker evidence. `TradabilityService` should become the only
answer to "may this scope perform this flow now?"

## Proposed Contract

| Concept | Definition | Evidence |
| --- | --- | --- |
| GLOBAL_HALT | Platform/broker/runtime trust is not safe for new risk | broker truth, position safety, runtime freshness, manual recovery, order truth, safety kernel halt |
| SCOPED_BLOCK | A blocker applies only to account/strategy instance/symbol/session/flow | manual lock, symbol lock, data stale, session policy, risk cap |
| FLOW_OVERRIDE | Close/reduce/protective/flatten flow may pass an entry blocker with owner evidence | `overrides`, `close_only_allowed`, `reduce_only_allowed` |
| ProductionState | Operator artifact for platform trust and blocked scopes | `global_halt`, `blocked_scopes`, `flow_overrides` |
| TradabilityDecision | Per-flow authorization evidence | authority, scope, flow, allow/deny, blockers, overrides, owner identity, timestamp |

## Contract Tests

`tests/unit/services/test_scoped_tradability_contract.py` adds the required
contract test names. The original P0 red tests have been converted as the
phases landed; the suite now passes without xfail.

| Test | Purpose | Current Result | Expected Future Result |
| --- | --- | --- | --- |
| `test_global_halt_blocks_entry_for_all_scopes` | Hard halt blocks all entry scopes | pass | pass |
| `test_broker_truth_missing_is_global_halt` | Broker truth remains hard blocker | pass | pass |
| `test_position_safety_no_go_is_global_halt` | Position safety remains hard blocker | pass | pass |
| `test_runtime_stale_is_global_halt` | Runtime freshness remains hard blocker | pass | pass |
| `test_manual_recovery_required_is_global_halt` | Manual recovery remains hard blocker | pass | pass |
| `test_strategy_manual_lock_blocks_only_matching_strategy_instance` | Manual lock becomes owner-scoped | pass | pass |
| `test_symbol_lock_blocks_only_matching_account_symbol_scope` | Symbol lock is account/symbol scoped | pass | pass |
| `test_eod_rth_only_blocks_entry_after_rth_close` | RTH-only session policy blocks entry | pass | pass |
| `test_eod_extended_strategy_allows_post_market_entry` | Extended strategy can enter post-market | pass | pass |
| `test_options_default_rth_only_unless_policy_allows_extended` | Options default to RTH only | pass | pass |
| `test_protective_stop_allowed_when_entry_blocked_by_session` | Protective stop override | pass | pass |
| `test_strategy_flatten_allowed_when_entry_blocked_by_eod` | Strategy flatten override | pass | pass |
| `test_flow_override_cannot_bypass_broker_truth_missing` | Hard global halt defeats normal override | pass | pass |
| `test_strategy_system_paused_no_longer_only_hard_return_source` | Strategy requests authorization | pass | pass |
| `test_execution_service_requires_tradability_decision_before_entry` | Execution second-check | pass | pass |
| `test_production_state_can_express_partial_entry_go_no_go` | Partial entry state | pass | pass |
| `test_decision_evidence_contains_scope_flow_blockers_authority` | Evidence schema | pass | pass |

## Implementation Plan

| Phase | Objective | Files likely changed | Tests | Rollback risk |
| --- | --- | --- | --- | --- |
| 1 | Add `TradabilityDecision`, `TradabilityScope`, enums, pure evaluator | `services/production/scoped_tradability.py`, tests | contract tests for hard/scoped/override decisions | Complete |
| 2 | Add production_state partial/scoped output | `tool/ops/run_runtime_operations_watchdog.py` | production state contract tests | Complete, additive artifact fields |
| 3 | Execution entry second-check | `services/exec/execution_service.py` | execution unit test for pre-risk denial and evidence | Complete, validation-only |
| 4 | Replace strategy pause return with decision request | `strategies/stocks_kernel/strategy.py` | contract presence test | Complete, evidence-only in paused path |
| 5 | Session-scoped EOD policy | `services/production/scoped_tradability.py` | RTH/extended/options policy tests | Complete in evaluator; supervisor behavior unchanged |
| 6 | Monitoring dashboards/evidence | docs and serializable decision payloads | decision evidence tests | Complete as evidence contract; dashboards deferred |

## Decision

Scoped tradability phases 1-6 are implemented at the contract/integration layer
without live IBKR access and without changing OMS ownership contracts. EOD
flatten behavior remains unchanged; session-scoped behavior is enforced through
the pure evaluator and surfaced as production/execution evidence.

Operational hardening follow-up added the production runtime input path:
`run_system.py` now injects a read-only readiness-artifact provider into
`ExecutionService`. The provider maps broker truth, position safety, runtime
freshness, manual recovery, and scoped production-state blockers into
`TradabilityInputs` before risk approval or order persistence.

Final hardening update:

- `PlatformEODSupervisor` now accepts session close policies and uses
  strategy-scoped flatten when such policies are configured. Runtime startup
  derives those policies from enabled strategy production manifests.
- production_state treats EOD/manual lock as scoped entry evidence rather than
  global halt, while preserving hard global blockers.
- stocks/options paused paths now emit `TradabilityDecision` evidence.
- broker truth convergence carries durable order owner lane fields into position
  ledger fills.
- monitoring draft artifact added at
  `docs/reports/artifacts/scoped_tradability_alert_rules_20260512.json`.
- final hardening report and artifact added at
  `docs/reports/scoped_tradability_final_hardening_20260512.md` and
  `docs/reports/artifacts/scoped_tradability_final_hardening_20260512.json`.

Phase status:

- Phase 1 evaluator: done.
- Phase 2 production_state scoped output: done.
- Phase 3 execution second-check: done.
- Phase 4 strategy pause handling: done.
- Phase 5 EOD/session policy: done.
- Phase 6 monitoring draft: done.

Decision for next work: manual canary review before broad production rollout.

## Recommended Next Batch

Manual Canary Review
