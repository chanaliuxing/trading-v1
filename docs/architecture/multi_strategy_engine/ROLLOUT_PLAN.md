# Rollout Plan (run_system-safe)

## Baseline Rule
No behavior change in daily `run_system` until all pre-integration gates are green.

## Phase 0: Spec Freeze
- Freeze PRD/schema/acceptance docs.
- ValidateAgent PASS required.

## Phase 1: Write-Only Trace Enrichment
- Add missing IDs at execution ingress.
- Persist audit events but do not alter routing behavior.
- Feature flags default OFF.

## Phase 2: Shadow Validation
- Enable schema validation and trace completeness checks in shadow.
- Emit warnings/errors; no order-blocking yet.

## Phase 3: Paper Canary
- Enable for selected `strategy_id` allowlist.
- Keep direct mode default, netted opt-in per symbol.

## Phase 4: Expand and Harden
- Increase strategy coverage gradually.
- Turn on strict blocking for malformed payloads.

## Rollback
- Single switch: disable new feature flags and revert to direct legacy path.
- Keep event persistence optional and non-blocking.

## Operational Checklist
- pre-open health check: schema validator, DB migration state, broker connectivity.
- intraday watch: trace-missing metric, orphan-order metric, reconciliation correction rate.
- post-close: acceptance smoke and incident review template.


## Failure Injection Gate
- Before canary promotion, run failure-injection suite for duplicate/late/out-of-order callbacks and transient DB faults.
- Any failed failure-injection case is release-blocking.

## Rollback SLO Gate
- Rollback must complete in < 5 minutes (RTO).
- Attribution fields must retain complete correctness after rollback (RPO=0).
- Rollback drill evidence must be attached to release ticket.
