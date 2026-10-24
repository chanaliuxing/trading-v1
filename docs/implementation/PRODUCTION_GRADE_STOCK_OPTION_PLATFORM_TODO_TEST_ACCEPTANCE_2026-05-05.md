# Production Grade Stock/Option Platform TODO, Test Plan, and Acceptance Checklist

Date: 2026-05-05

Merged sources:

- `PRODUCTION_STABILITY_ORDER_STRATEGY_TODO_2026-05-04.md`
- 2026-05-05 production-grade review covering broker truth, fill attribution, recovery incidents, safety lane, protection, EOD/session, strategy contracts, options, IB restart, and observability
- 2026-05-05 landed implementation notes through current restart validation

Scope: make the trading platform production-grade for unattended paper/live operation, including stock strategies, option strategies, pluggable strategy onboarding, IB restart recovery, order/fill attribution, position accounting, protection, EOD/no-overnight handling, and machine-checkable operations.

This document is intentionally strict. A module is not considered complete because tests pass, logs look quiet, or watchdog emits `HOLD`. A module is complete only when it can prove a clean terminal state with fresh artifacts under normal operation and under fault drills.

## Critical Review of This Plan

This plan is useful as a comprehensive inventory, but a production program will fail if it is treated as a flat checklist. The work has hard dependencies:

- Fill attribution and broker truth must be fixed before PnL, ledger, recovery ownership, or strategy performance can be trusted.
- Recovery incident closure must be fixed before PositionSafetySupervisor and EOD can be considered more than fail-closed monitors.
- Strategy manifests must be enforced before adding more stock or option strategies.
- Option trigger/contract selection ledgers must be built before option strategy performance can be evaluated.
- Observability must be tied to the current process before any runtime acceptance artifact is trusted.

The current strongest behavior is fail-closed detection. The missing behavior is verified convergence. Every module below therefore has two acceptance layers:

- **Gate acceptance**: the module blocks unsafe trading.
- **Convergence acceptance**: the module resolves or escalates the unsafe state to a durable terminal state.

Production-grade requires both.

## How to Use This Document

Use this as the release gate for stock and option production readiness.

Rules:

- Do not mark a module complete from unit tests alone.
- Do not mark a module complete from a stale or pre-process artifact.
- Do not mark a module complete if it only emits `HOLD` but cannot converge or escalate.
- Do not enable more live strategies while any P0 module is incomplete.
- Do not trust strategy PnL until fill attribution, broker truth, and ledger integrity are complete.
- Do not use option strategy results for production evaluation until option trigger and contract selection verdicts are durable.

Status values:

- `NOT_STARTED`: no production implementation.
- `PARTIAL_GATE`: can block unsafe operation, but cannot converge.
- `PARTIAL_CONVERGENCE`: can converge in some paths, but fault drills are incomplete.
- `PRODUCTION_READY`: unit, integration, e2e, runtime artifact, and fault drill all pass.
- `REGRESSED`: previously complete behavior failed in current runtime.

Severity values:

- `P0`: can create or leave real broker risk, corrupt accounting, or produce false production go.
- `P1`: can block production, make strategy onboarding unsafe, or hide important degraded behavior.
- `P2`: observability, ergonomics, and hardening gaps that do not directly create broker risk.

## Module Scorecard

This table is the executive release view. It must be updated when implementation status changes.

| Module | Current status | Required status | Priority | Main blocker |
| --- | --- | --- | --- | --- |
| Broker/DB/Ledger Truth | PARTIAL_CONVERGENCE | PRODUCTION_READY | P0 | Broker snapshot now requires fresh live broker evidence and WAC/realized replay is fixed; completed-execution provenance and real broker snapshot persistence remain |
| Order/Fill Attribution | PARTIAL_CONVERGENCE | PRODUCTION_READY | P0 | Strong attribution is account-scoped and unknown/ambiguous fills quarantine; operator UI/runbook and live polluted replay remain |
| Recovery Incident Closure | PARTIAL_CONVERGENCE | PRODUCTION_READY | P0 | Durable incidents now track active intents, retry/backoff, cancel-replace, and terminal consistency; execution-loop integration and richer identity remain |
| Close-only/Safety Lane | PARTIAL_CONVERGENCE | PRODUCTION_READY | P0 | Broker-truth recovery lane exists for close-only guard; full mode split and per-intent artifact are incomplete |
| Protection/Reverse-open Prevention | PARTIAL_GATE | PRODUCTION_READY | P0 | Protection must be broker-verified, reduce-only, and price-sane |
| EOD/Session/No Overnight | PARTIAL_CONVERGENCE | PRODUCTION_READY | P0 | Platform EOD continuously rechecks after verified flat; richer per-session policy and e2e drills remain |
| Strategy Tradability | PARTIAL_CONVERGENCE | PRODUCTION_READY | P1 | Central artifact and idle semantics exist; full entry/safety/data/session schema is not complete |
| Data/Warmup/Feature Seed | PARTIAL_CONVERGENCE | PRODUCTION_READY | P1 | Every data failure must be persisted and centrally consumed |
| Option Strategy Platform | PARTIAL_CONVERGENCE | PRODUCTION_READY | P1 | Option selected is no longer terminal without lifecycle evidence; contract identity now requires multiplier/exchange/tradingClass/broker identity; adapter migration and RTH/e2e drills remain |
| IB Restart Recovery | PARTIAL_CONVERGENCE | PRODUCTION_READY | P1 | Current-process drill generation/checking exists and existing no-go/stale artifacts block; full recovery session id and fault drill suite remain |
| Observability/Artifacts | PARTIAL_CONVERGENCE | PRODUCTION_READY | P1 | Unified production state and disk-space hard gate exist; all artifacts do not yet satisfy the unified schema |
| Stock Strategy Plugin Interface | PARTIAL_CONVERGENCE | PRODUCTION_READY | P1 | Manifest/plugin boundary exists for stock plugins; scaffold/dry-wire/e2e onboarding gate remains |
| Option Strategy Plugin Interface | NOT_STARTED/PARTIAL_GATE | PRODUCTION_READY | P1 | Contract selection and trigger ledger must be platform services |

## Optimized Execution Queue

This queue replaces the flat TODO ordering when deciding what to build next. Work only moves down the queue after the current row has either passed runtime acceptance or produced a durable manual-required blocker.

| Rank | Track | Why it is next | Concrete next acceptance | Remaining |
| --- | --- | --- | --- | ---: |
| 1 | Current-process IB restart drill | Current paper-runtime blocker is cleared; keep as regression gate for every restart | Real or simulated IB disconnect/reconnect after current process start produces `paper_ib_restart_latest.go_no_go=go` with stable-window evidence | 0% for operator-paper acceptance; real RTH/non-flat drills remain in rank 5 |
| 2 | Account-scoped fill attribution ledger | Strong fill attribution is account/order/perm/symbol/side/qty/time-window scoped; first-class ledger, adjudication API/CLI, daily stats, and polluted fixture replay are live | Unknown/ambiguous fills write first-class attribution records, expose adjudication state, and PnL excludes them by schema | 2% |
| 3 | Recovery incident state machine | Incidents now carry active intent/retry/cancel-replace fields; execution loops and richer account/snapshot identity are not fully centralized | One active intent per account/symbol/anomaly with retry/backoff/cancel-replace and terminal evidence artifact | 12% |
| 4 | Broker truth snapshot contract | Snapshot artifact/gate now fails DB-projection-only truth and consumes fresh position-safety broker evidence; completed-execution snapshot provenance and core_position projection columns remain | `BrokerTruthSnapshot -> OrderTruth -> PositionLedger -> CorePosition` is enforced and artifacted | 10% |
| 5 | Option platformization | OptionTriggerLedger lifecycle hardening and strict contract identity landed; adapter migration and RTH/e2e drills remain | `OptionTriggerLedger` + `ContractSelectionService` + exact option fill attribution gate production | 10% |
| 6 | Strategy onboarding hard gate | Stock plugin/manifest, runtime manifest/session gate, and CLI admission report are live; dry-wire/e2e gate is incomplete | New stock strategy passes manifest validation, dry-wire, production-wire smoke, session flat drill | 18% |

Current live/paper-live rule: do not expand enabled strategies while rank 1 is red or any P0 artifact is `no_go`.

Current remaining production gap snapshot after 2026-05-06 02:10 ET validation:

- Current paper runtime go/no-go gate: 0% remaining for current-process operator-paper IB restart acceptance.
- Stock strategy production pluggability: 15-20% remaining.
- Option strategy production pluggability: 15-20% remaining.
- Order/fill accounting trust: 2-4% remaining.
- Unattended recovery/fault-drill certification: 20-25% remaining.
- Overall production-grade platform gap: about 4-7% remaining, dominated by completed-execution broker provenance, full option adapter migration, recovery execution-loop centralization, and real RTH/after-hours/non-flat fault drills.

## Non-negotiable Invariants

These invariants override individual strategy logic:

- Broker truth wins for safety actions.
- Dirty ledger blocks entry, not broker-truth flatten.
- Unknown fill/order never mutates PnL, ledger, or ownership before attribution.
- Every nonzero broker position must be protected, flattening, allowed overnight, or manual-required.
- Every live broker order must be known or quarantined.
- Every enabled strategy starts fail-closed until fresh readiness is persisted.
- Every option trigger must end in a terminal verdict.
- Every artifact used for go/no-go must be current-process fresh.
- Runtime disk free space below configured minimum must be `no_go`; artifact/log write failures cannot be informational only.
- A live process with stopped critical services must exit or enter manual halt after bounded time.

## Dependency Order

Implement in this order unless a production incident requires immediate mitigation:

1. **Risk freeze and cleanup**
   - manually verify current broker positions and open orders.
   - resolve current NVDA residual and active quarantines.
   - disable onboarding of new live strategies.
2. **Truth and attribution**
   - unknown fill/order quarantine.
   - broker truth snapshot contract.
   - ledger lot/account/notional integrity.
3. **Recovery closure**
   - recovery incident service.
   - broker-truth-first close-only safety lane.
   - position safety and EOD linked to incidents.
4. **Strategy contracts**
   - stock manifest/schema/plugin scaffold.
   - strategy readiness schema.
   - data contract enforcement.
5. **Option platform**
   - option trigger ledger.
   - contract selection service.
   - option fill attribution.
6. **Runtime acceptance**
   - IB restart current-process drill.
   - full fault drill suite.
   - one RTH plus one after-hours/night clean session.

## Per-module Definition of Done

Every module must satisfy all items below before `PRODUCTION_READY`:

- Code path is implemented and enabled in `run_system.py` or the relevant production startup path.
- Unit tests cover success, failure, stale, ambiguous, and unavailable cases.
- Integration tests use the real Postgres schema when DB state is involved.
- E2E or fault drill proves behavior through production wiring.
- Runtime artifact is generated by the current process.
- `production_state_latest.json` consumes the artifact.
- Stale artifact fails or is ignored.
- Failure has clear issue key, severity, owner component, and clear condition.
- Manual-required path is durable and blocks further unsafe entry.
- Documentation names the exact terminal states.

## Current Production Readiness Verdict

Current state is not production-grade.

The platform has improved from false-green behavior to fail-closed behavior. That is necessary but not sufficient. Production-grade requires the platform to converge:

```text
detect anomaly
  -> assign ownership or mark unknown
  -> submit exactly one durable recovery intent
  -> verify broker/order/position truth
  -> clear or escalate with a durable manual-required incident
```

The open hard gaps are:

- Broker truth, DB order truth, fill truth, and ledger truth can still diverge.
- Unknown fills can still poison strategy ownership and PnL unless quarantined before adoption.
- Recovery can create quarantine/manual locks without closing the loop to verified flat/protected/manual-required.
- Broker-truth recovery flatten can be blocked by stale DB/strategy position facts.
- EOD can be true at one time and stale after after-hours/night strategies trade again.
- Option strategy execution is not yet a full event-sourced platform flow.
- New strategy onboarding still requires too much hand-wired config and hidden platform knowledge.

## Production Definition

The system is production-grade only when all of these are true:

- `production_state_latest.json` is `go`, fresh, generated by the current `run_system.py` process, and tied to the current git/config/runtime version.
- IB broker truth, DB order truth, fill truth, position ledger, protection registry, and strategy tradability are all clean or explicitly quarantined/manual-required.
- Every enabled production-blocking stock and option strategy has a manifest, data contract, session policy, recovery policy, protection policy, EOD policy, and tradability heartbeat.
- Every nonzero broker position is verified protected, actively flattening, verified quarantined with manual lock, or verified allowed overnight.
- Every live broker order is linked to exactly one platform order or quarantined.
- Every fill is linked to exactly one verified platform order or quarantined as unknown fill.
- Entry can be blocked aggressively, but safety actions cannot be blocked by stale strategy memory or stale DB ledger when broker truth says action is needed.
- IB disconnect/reconnect recovery is proven by a current-process runtime drill artifact, including stable-window checks.
- Adding a new stock or option strategy requires implementing a small plugin/manifest surface, not manually wiring order, ledger, recovery, protection, and EOD internals.

## Module 1: Broker, DB, Ledger, and Position Truth

### Problem

IB positions, `core.core_order`, `core.core_fill`, `core.core_position`, position ledger, lot ownership, and protection registry can diverge. Net flat does not prove clean truth because lot/account/notional residues can still corrupt avg price, ownership, recovery actions, and PnL.

### Production Target

Broker truth is the primary fact for safety actions. DB order/fill/ledger truth is a verified projection of broker truth. Dirty ledger blocks new entry but must not block broker-truth flatten/protection.

### TODO

- Define a single `BrokerTruthSnapshot` object containing positions, open orders, executions/fills, account id, perm ids, order refs, timestamps, and snapshot completeness.
- Define a `TruthReconciliationResult` with explicit fields:
  - `broker_snapshot_complete`
  - `order_truth_clean`
  - `fill_truth_clean`
  - `position_ledger_clean`
  - `lot_truth_clean`
  - `account_truth_clean`
  - `protection_registry_clean`
  - `unknown_fill_count`
  - `unknown_order_count`
  - `active_quarantine_count`
- Extend ledger integrity checks beyond net quantity:
  - null lot nonzero qty
  - null lot offsetting real lots
  - `UNKNOWN_ACCOUNT`
  - mixed account ids
  - qty zero with nonzero notional
  - net-flat but lot-dirty symbols
  - avg price derived from dirty lots
- Prevent dirty ledger-derived avg price from being used as protection price anchor.
- Add a broker-first repair mode:
  - if IB position is nonzero and DB says flat, broker truth wins for safety action.
  - if DB position is nonzero and broker says flat, DB/ledger is repaired or quarantined, not flattened at broker.
- Make `core_position` a projection with provenance:
  - `source_snapshot_id`
  - `ledger_clean`
  - `broker_confirmed`
  - `last_reconciled_at`

### Tests

- Unit: ledger integrity flags net-flat null-lot offsetting residues.
- Unit: ledger integrity flags qty=0/notional!=0 rows.
- Unit: broker nonzero + ledger flat produces safety flatten/protect candidate.
- Unit: broker flat + ledger nonzero produces ledger repair/quarantine, not broker order.
- Integration: load mixed account/UNKNOWN_ACCOUNT fixture and verify production state `no_go`.
- E2E: after a broker fill, `core_position` and ledger projection update with broker snapshot id.

### Acceptance

- No strategy can enter when ledger is dirty for its account/symbol/root.
- Safety flatten/protect can execute when broker truth is nonzero even if ledger is stale.
- `production_state_latest.json` includes `ledger_integrity` with lot/account/notional diagnostics.
- A dirty but net-flat symbol is still reported as `no_go`.
- Protection price generation refuses dirty avg price unless a fresh market reference exists.

### Merged Implementation Notes

- Landed: `SessionEndSupervisor` generates `runtime/readiness/session_end/session_end_latest.json` for no-overnight/session-end verification.
- Landed: enabled strategies with `production_manifest.eod_contract.no_overnight=true` are checked after their `force_flatten_time` or the platform default deadline.
- Landed: missed session-end flatness upserts `session_end_supervisor` recovery incidents; verified flat resolves source-scoped session-end incidents to `verified_flat`.
- Landed: persistent session-end violations escalate after `manual_required_after_sec` to `manual_required_acknowledged`, create `session_end_manual_required` lock evidence, and remain `session_end` no-go until broker truth is flat.
- Landed: `production_state_latest.json` consumes `session_end` through the runtime watchdog and blocks on missing/stale/no-go/lookup-failed session-end artifacts when the supervisor is enabled.
- Tests landed: session-end violation creates an incident, stale violation escalates manual-required, flat broker truth resolves the incident, watchdog flags session-end no-go/manual-required, and disabled supervisor does not create false blockers.
- Remaining: full manifest fields for `session_name`, `entry_windows`, `exit_deadline`, `allowed_overnight`, `afterhours_allowed`, and `manual_required_after` are not yet enforced.
- Remaining: strategy-specific session windows and overnight-allowed strategies need richer policy handling beyond a single deadline.
- Remaining: e2e fault drill for after-hours missed flatten -> no-go/manual-required is not complete.

- Landed: watchdog ledger integrity flags nonzero `UNKNOWN_ACCOUNT` and null-lot rows even when symbol net quantity is zero.
- Landed: `PositionSafetySupervisor` clears stale `core.core_position` only when broker truth is flat, `position_ledger_current` net qty is flat, and no non-terminal `core.core_order` remains.
- Landed: option `core_position` residuals are eligible for the same conservative cleanup for production-blocking option strategies.
- Landed: stale position/recovery quarantines and manual locks can be auto-resolved only after broker, ledger, DB order, and IB open-order truth are all flat.
- Landed: `core.core_position.avg_px` is no longer reconstructed from ledger notional; protection must use fill-accounting semantics plus fresh market reference.
- Landed: `LedgerIntegrityService` now produces `runtime/readiness/ledger_integrity/ledger_integrity_latest.json`.
- Landed: `production_state_latest.json` now includes canonical `ledger_integrity` plus compatibility `position_ledger_integrity`.
- Landed: the artifact reports lot/account/notional blockers for unknown/null lot rows, mixed account positions, and qty-zero/nonzero-notional residuals.
- Landed: `BrokerTruthSnapshotService` now produces `runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json` and production state consumes it as a hard gate.
- Landed: broker truth snapshot gate reports active broker/order quarantine rows, open core orders, nonzero core/ledger positions, and `position_ledger_current` versus `core_position` projection divergence.
- Landed: broker truth snapshot artifact now carries source process start, git head, config hash, max age, age, stale flag, and artifact-before-process-start flag; watchdog rejects stale or pre-process artifacts.
- Landed: broker truth snapshot no longer accepts readiness wrappers as live truth; the live input must be a `live_broker_truth_snapshot` with PID, account key, generated_at, positions/open_orders lists, matching counts, and non-failed lookup state.
- Landed: `PositionSafetySupervisor` emits a nested `live_broker_snapshot` built from its live IB position/open-order reads, and watchdog passes only that nested broker-truth payload into the broker snapshot gate.
- Runtime proof: current process `47275` emitted `broker_truth_snapshot.go_no_go=go`, `stale=false`, `artifact_before_process_start=false`, `projection_divergence_count=0`, `open_core_order_count=0`, and `order_truth_active_quarantine_count=0` at 2026-05-06 00:24 ET.
- Remaining: this artifact still needs direct startup wiring outside watchdog/emitter.
- Remaining: `core_position` lacks explicit `source_snapshot_id`, `ledger_clean`, `broker_confirmed`, and `last_reconciled_at` provenance fields.
- Remaining: replace the recursive `core.position_ledger_current` view with an incremental/materialized current-position projection plus bounded repair/rebuild jobs before ledger growth makes go/no-go checks unbounded.

## Module 2: Order and Fill Attribution

### Problem

Unknown broker fills and orders can be adopted into strategy ledger/PnL without a unique identity match. This can contaminate strategy ownership, PnL, recovery ownership, and later flatten/protection decisions.

### Production Target

Every live order and fill is either uniquely attributed or quarantined. Unknown attribution never mutates strategy PnL, position ledger, or strategy ownership until adjudicated.

### TODO

- Create `core.unknown_broker_fill` or equivalent quarantine ledger.
- Create `core.unknown_broker_order` or equivalent order quarantine ledger.
- Require unique attribution match by:
  - account id
  - broker order id or perm id
  - order ref when present
  - contract identity
  - symbol/root
  - side
  - qty/fill qty tolerance
  - time window
- Disable production mutation from unattributed fill adoption:
  - no `core_fill`
  - no `position_ledger_event`
  - no PnL
  - no strategy ownership
  until attribution is verified.
- Add an adjudication workflow:
  - auto-adjudicate only if exactly one candidate exists with strong broker identity.
  - otherwise manual-required.
- Add a fill attribution audit artifact per day:
  - total broker executions
  - attributed count
  - unknown count
  - ambiguous count
  - stale candidate count

### Tests

- Unit: fill with no order id/perm id goes to unknown fill quarantine.
- Unit: fill with duplicate candidate order ids goes to ambiguous quarantine.
- Unit: fill with unique perm id updates core fill and ledger.
- Unit: option fill with old expiry cannot attach to current-day option order.
- Integration: replay TQQQ/SOXL/NVDA polluted fill fixture and verify no strategy ledger is mutated.
- E2E: IB execution after reconnect is attributed exactly once.

### Acceptance

- `unknown_fill_count=0` is required for `production_state=go`, unless all unknown fills are manual-acknowledged and no open position risk remains.
- PnL reports explicitly exclude unknown/ambiguous fills.
- RuntimeReconciler cannot use unknown fills to assign strategy ownership.
- Fill attribution artifacts are fresh and tied to current process/day.

### Merged Implementation Notes

- Landed: `BrokerTruthConvergence.converge_unattributed_fill()` no longer creates adopted `FILLED` core orders when no unique existing `core_order` broker identity exists.
- Landed: unknown broker fills now upsert `core.core_order_quarantine` with reason `unattributed_broker_fill`; no `core_fill`, ledger, PnL, or strategy ownership mutation is produced in that path.
- Landed: existing order identity convergence now requires exactly one strong candidate by broker order id / IB perm id plus symbol, side, qty coverage, and order ref when present.
- Landed: duplicate, stale, symbol-mismatched, side-mismatched, qty-insufficient, or order-ref-mismatched candidates are quarantined as `ambiguous_broker_fill`; no `core_fill`, ledger, PnL, or strategy ownership mutation is produced in that path.
- Tests landed: unattributed fill quarantine, ambiguous duplicate quarantine, weak symbol mismatch quarantine, no ledger mutation, and strong existing-order identity convergence.
- Landed: `FillAttributionService` now produces `runtime/readiness/fill_attribution/fill_attribution_latest.json`.
- Landed: `production_state_latest.json` consumes `fill_attribution` and blocks on active `unattributed_broker_fill`, `unknown_broker_fill`, or `ambiguous_broker_fill` quarantine rows.
- Landed: active unknown/ambiguous fill artifact includes `unknown_fill_count`, `ambiguous_fill_count`, blockers, and clear conditions.
- Landed: `core.fill_attribution_ledger` is now created by DDL/migration and records unknown, ambiguous, and verified broker fills with account id, broker order id, perm id, order ref, symbol, side, quantity, source, candidate order ids, status, and `pnl_eligible`/`ledger_eligible` flags.
- Landed: positive attribution writes a verified fill-attribution ledger row before mutating `core_fill` or `position_ledger`; unknown/ambiguous fills are ledger-recorded and remain ineligible for PnL/ledger mutation.
- Landed: fill attribution artifact now carries source process start, git head, config hash, max age, age, stale flag, and artifact-before-process-start flag; watchdog rejects stale or pre-process artifacts.
- Landed: positive attribution now requires an active order state, a 36-hour order/fill timestamp window, and exact parseable internal `OPT:UNDERLYING:YYYYMMDD:STRIKE:C/P` symbol identity for option fills.
- Landed: stale broker id/perm id candidates and old-expiry option candidates are quarantined as ambiguous instead of mutating `core_fill`, ledger, PnL, or strategy ownership.
- Landed: `adjudicate_fill_attribution()` records bounded manual decisions (`manual_resolved`, `manual_required`, `ignored`) and only makes rows PnL/ledger eligible when a matched core order and strategy id are supplied.
- Landed: `tool/ops/adjudicate_fill_attribution.py` provides the operator CLI for the same bounded adjudication path; operators should not update `core.fill_attribution_ledger` directly.
- Landed: fill attribution readiness now emits `daily_stats` with total, status buckets, manual-required, PnL-eligible, and ledger-eligible counts.
- Landed: polluted TQQQ/SOXL/NVDA replay fixture verifies symbol mismatch, side mismatch, and stale same-symbol candidate rows quarantine as ambiguous without mutating `core_fill` or position ledger.
- Runtime proof: current process `80229` emitted `fill_attribution.go_no_go=go`, `ledger_present=true`, `daily_stats.total_count=0`, `daily_stats.manual_required_count=0`, `unknown_fill_count=0`, and `ambiguous_fill_count=0` at 2026-05-06 00:43 ET.
- Remaining: real broker execution replay from archived IB activity should be collected as an integration fixture before PnL is trusted for live option strategy evaluation.

## Module 3: Recovery Incident and Quarantine Closure

### Problem

Runtime reconciliation can repeatedly create quarantine rows or manual locks without converging the anomaly. Detecting risk is not enough; the system must resolve or escalate to a durable terminal state.

### Production Target

Every anomaly becomes one durable recovery incident with one active recovery intent and a terminal state.

### TODO

- Add `RecoveryIncidentService`.
- Incident identity:
  - `account_id`
  - `symbol/root`
  - `broker_position_sign`
  - `broker_position_qty`
  - `owner_strategy_id` or `unknown_owner`
  - anomaly class: `unknown_order`, `unknown_fill`, `position_mismatch`, `naked_position`, `stale_protection`, `ledger_dirty`
- Incident states:
  - `DETECTED`
  - `OWNERSHIP_RESOLVED`
  - `RECOVERY_INTENT_CREATED`
  - `SUBMITTED`
  - `VERIFYING`
  - `VERIFIED_FLAT`
  - `VERIFIED_PROTECTED`
  - `QUARANTINED`
  - `MANUAL_REQUIRED`
  - `CLOSED`
- Upsert incident instead of appending duplicate active quarantine rows.
- Add bounded retry/cancel-replace:
  - max attempts
  - backoff
  - broker open-order verification
  - final manual-required escalation.
- Add incident artifacts:
  - latest active incidents
  - per-incident timeline
  - terminal verification evidence.

### Tests

- Unit: same NVDA mismatch does not create duplicate active incidents.
- Unit: failed recovery submit moves incident to retry state, not duplicate quarantine.
- Unit: broker position flat moves incident to `VERIFIED_FLAT`.
- Unit: active protection order verified at IB moves incident to `VERIFIED_PROTECTED`.
- Integration: simulate `db_persist_submitted_failed` and verify one incident with manual-required terminal path.
- E2E: broker-only +1 position after restart is flattened or manual-required with one incident id.

### Acceptance

- Active quarantine count cannot grow unbounded for the same anomaly.
- Every active quarantine has a linked recovery incident.
- Every manual lock has a linked recovery incident and clear condition.
- `production_state` reports active incident count and terminality.

### Merged Implementation Notes

- Landed first slice: `RecoveryIncidentService` writes file-backed durable incidents under `runtime/readiness/recovery_incidents`.
- Landed first slice: one active incident per strategy/symbol lane, with states `detected`, `protected_pending_verified_flat`, `recovery_submitted`, `manual_required_acknowledged`, and `verified_flat`.
- Landed first slice: `PositionSafetySupervisor` upserts an incident before recovery flatten, marks submitted recovery intents, marks manual-required terminal state, and resolves active incidents to `verified_flat` when broker truth and quarantine are clean.
- Landed: `production_state_latest.json` now consumes top-level `recovery_incidents` and blocks on missing, stale, lookup-failed, or active non-terminal incidents.
- Landed: `PlatformEODSupervisor` now links `MANUAL_REQUIRED` EOD failures to terminal `manual_required_acknowledged` recovery incidents, and resolves active platform-EOD incidents after `VERIFIED_FLAT`.
- Landed: `RuntimeReconciler` now owns a `RecoveryIncidentService`, links order/position quarantine upserts to active incidents, and resolves runtime quarantine incidents to `verified_flat` when broker, DB, ledger, and open-order truth are clean.
- Landed: runtime quarantine incident resolution is source-scoped so EOD and position-safety incidents are not accidentally cleared by generic RuntimeReconciler cleanup.
- Landed: terminal transitions clear `active_quarantine` and `protected_or_flattening`; production-state rejects terminal incidents that still carry active quarantine or lack terminal evidence such as `verified_flat_at` or manual-required lock evidence.
- Landed: recovery incident artifacts now report `active_intent_count`, `retry_due_count`, and `cancel_replace_pending_count`.
- Landed: `mark_recovery_attempt_result()` records retry/backoff attempts and escalates exhausted retries to `manual_required_pending`; `mark_cancel_replace_requested()` records cancel-replace pending state.
- Tests landed: incident idempotency, recovery submit tracking, retry/backoff, cancel-replace pending state, manual-required terminality, verified-flat resolution, terminal active-quarantine rejection, and missing terminal-evidence rejection.
- Remaining: incident identity must expand to account id, sign, owner/unknown owner, anomaly class, and broker snapshot evidence.
- Remaining: unknown fill adjudication, generic manual locks, and full account-scoped order/fill quarantine tables must all link to the same incident service.
- Remaining: execution submit/cancel-replace loops must consume the incident retry/backoff fields as the single source of truth.

## Module 4: Close-only and Safety Lane

### Problem

Safety actions can still be blocked by stale DB/strategy position facts during broker/ledger split-brain. That is unacceptable for broker-truth recovery.

### Production Target

Entry is strictly gated. Safety actions use broker truth as primary and remain executable unless broker truth is unavailable or the action would increase risk.

### TODO

- Split execution guard modes:
  - `ENTRY`
  - `NORMAL_EXIT`
  - `PROTECTION`
  - `RECOVERY_FLATTEN`
  - `PLATFORM_EOD_FLATTEN`
  - `CANCEL`
- For `RECOVERY_FLATTEN` and `PLATFORM_EOD_FLATTEN`:
  - use broker position qty/sign as primary.
  - DB qty/hint can only reduce risk, not veto broker truth.
  - adapter/broker truth failure returns `BROKER_TRUTH_UNAVAILABLE`, not qty=0.
- Add reduce-only proof:
  - long broker position -> only SELL reduce/flatten.
  - short broker position -> only BUY reduce/flatten.
  - flat broker position -> close/protection orders are unsafe and must cancel/quarantine.
- Add safety-lane execution artifact per intent:
  - source incident id
  - broker qty used
  - side chosen
  - qty capped
  - broker verification result.

### Tests

- Unit: broker +1, DB 0, recovery SELL allowed.
- Unit: broker -1, DB 0, recovery BUY allowed.
- Unit: broker 0, DB +1, recovery SELL blocked and ledger repair incident created.
- Unit: broker lookup error returns manual-required, not no-position.
- Integration: recovery flatten uses broker qty and produces verified flat.

### Acceptance

- Close-only rejections are impossible for broker-truth recovery when broker position is known and side reduces risk.
- Every safety lane order carries an incident id and idempotency key.
- Safety lane never opens or increases a position from flat.

### Merged Implementation Notes

- Landed: close-only guard has a broker-truth recovery lane for `runtime_recovery_action`, `broker_truth_recovery`, `position_safety_recovery`, and `recovery_requires_uom_safety_lane`.
- Landed: broker-truth recovery uses broker position qty as primary, overriding stale DB/strategy hints.
- Landed: broker truth lookup failure returns `close_only_manual_required:broker_position_truth_unavailable` instead of treating qty as zero.
- Landed: `PositionSafetySupervisor` submits bounded close-only market flatten through UOM/recovery manager before manual-required locks.
- Landed: recovery flatten metadata carries `runtime_recovery_action`, `flatten_action`, `close_only`, `broker_truth_recovery`, broker qty hint, target qty, and recovery incident id.
- Tests landed: broker +1 DB-flat recovery SELL allowed, broker truth unavailable becomes manual-required, and position-safety recovery submit/reject paths.
- Remaining: full guard-mode split (`ENTRY`, `NORMAL_EXIT`, `PROTECTION`, `RECOVERY_FLATTEN`, `PLATFORM_EOD_FLATTEN`, `CANCEL`) and per-intent safety artifact are not complete.

## Module 5: Protection Orders and Reverse-open Prevention

### Problem

Protection can be missing, stale, wrong-side, wrong-qty, or linked only to DB state rather than broker open-order truth. Historical avg price pollution also showed that protection prices can be unsafe if ledger is dirty.

### Production Target

Every nonzero broker position is protected or flattening. Protection orders are reduce-only, broker-verified, price-sane, and registry-clean.

### TODO

- Require protection orders to prove:
  - owner strategy id
  - broker position sign
  - reduce-only side
  - qty coverage
  - broker open-order presence
  - order ref or broker order id linkage
  - protection role: stop, target, trailing, OCO leg, cancel-replace
  - market reference sanity.
- Protection registry reconciliation:
  - registry row without broker order -> terminalize or quarantine.
  - broker protection without registry -> adopt only with strong identity or quarantine.
- Add protection price sanity:
  - use fresh bid/ask/mid/last reference.
  - reject prices too far from market unless explicitly allowed by strategy manifest.
  - do not trust dirty ledger avg price.
- Flat-account live safety orders:
  - if broker position is flat and live reduce-only order remains, cancel or quarantine.

### Tests

- Unit: long position + BUY stop is rejected as wrong-side.
- Unit: short position + SELL stop is rejected as wrong-side.
- Unit: DB submitted protection missing from IB is unsafe.
- Unit: broker protection missing from registry is quarantined unless strongly matched.
- Unit: polluted avg price with fresh market ref rejects impossible stop/target.
- E2E: after entry fill, platform verifies protection exists at broker within SLA.

### Acceptance

- `position_safety_latest.json` cannot classify protected using stale DB-only orders.
- Every protected position includes broker order ids or order refs.
- No flat account can have live close/protection orders without `order_safety_no_go`.

### Merged Implementation Notes

- Landed: `PositionSafetySupervisor` only counts safety-like working orders as protection/flatten evidence.
- Landed: safety-order evidence must match candidate strategy, broker open-order truth, reduce broker-position direction, and cover broker quantity.
- Landed: stale DB `SUBMITTED` safety rows missing from IB no longer classify a broker position as protected/flattening.
- Landed: flat broker account with live close/flatten/protection order is `position_safety` no-go until the broker order is cancelled, filled-and-reconciled, or re-adopted.
- Landed: protection registry rows are terminalized when broker position is flat or the matching broker open order is gone.
- Landed: protection registry write failure after broker submit creates `manual_intervention_lock` reason `protection_registry_write_failed`.
- Landed: SafetyKernel validates OCO stop/target prices against entry and market reference; impossible prices fail closed with protection lock.
- Remaining: broker protection adoption without registry still needs strict identity/quarantine semantics.
- Remaining: strategy-specific protection repair before flatten/manual-required is not yet rich enough for every strategy.

## Module 6: EOD, Session End, and No Overnight

### Problem

Platform EOD can verify flat at one time, but after-hours/night strategies can trade later and leave positions. EOD must be a continuous session-aware invariant, not one daily artifact.

### Production Target

Every strategy has a session policy. At each strategy session end, the platform verifies flat/protected/allowed-overnight. Global no-overnight is enforced continuously.

### TODO

- Add required manifest fields:
  - `session_name`
  - `entry_windows`
  - `exit_deadline`
  - `force_flatten_deadline`
  - `allowed_overnight`
  - `afterhours_allowed`
  - `manual_required_after`
- Add `StrategySessionSupervisor`.
- Add per-strategy session artifact:
  - session state
  - entry enabled
  - safety enabled
  - flatten deadline
  - verified flat/protected/allowed overnight.
- Global EOD supervisor must keep running after platform EOD if any strategy remains enabled for after-hours/night.
- If strategy trades after global EOD, the next session-end artifact supersedes the old EOD flat artifact.

### Tests

- Unit: strategy with afterhours disabled cannot enter after EOD.
- Unit: afterhours strategy must flatten by its own deadline.
- Unit: global EOD flat artifact becomes stale if later broker fill opens a position.
- Integration: afterhours NVDA entry followed by missed flatten produces no-go/manual-required.
- E2E: no-overnight drill verifies flat at final deadline.

### Acceptance

- `production_state` includes both platform EOD and per-strategy session state.
- Old EOD flat artifact cannot make production state green after later trading activity.
- Every production-blocking strategy has an active session policy.

### Merged Implementation Notes

- Landed: `PlatformEODSupervisor` tracks per-day state/attempt count and marks a day complete only for `VERIFIED_FLAT` or terminal escalation.
- Landed: submit exceptions and `SUBMITTED_NOT_VERIFIED` remain retryable for the same day.
- Landed: terminal `MANUAL_REQUIRED` creates platform manual-lock JSON artifacts and DB manual-intervention locks consumed by UOM entry gating.
- Landed: runtime watchdog fails after EOD cutoff when EOD artifact is missing, no-go, unverified, or has active platform manual locks.
- Landed: `NO_TARGETS` is verified flat only when both DB and IB position truth were read successfully.
- Remaining: `StrategySessionSupervisor` and per-strategy session-end artifacts are not implemented.
- Remaining: old global EOD verified-flat artifact is not sufficient after later after-hours/night trading; session-specific supersession still needs implementation.

## Module 7: Strategy Tradability and Health Contract

### Problem

Strategy-local blockers can be missed unless they are persisted in the central go/no-go path. `tradable` semantics can be ambiguous between idle, healthy, and entry-ready.

### Production Target

Every enabled strategy is fail-closed until a fresh heartbeat proves entry readiness or explicitly reports idle/non-entry state. Central production state consumes only the standardized schema.

### TODO

- Standardize strategy readiness fields:
  - `entry_ready`
  - `safety_ready`
  - `data_ready`
  - `warmup_ready`
  - `session_state`
  - `order_admission_ready`
  - `position_state`
  - `blockers`
  - `clear_conditions`
- Deprecate ambiguous top-level `tradable` or define it strictly as `entry_ready`.
- Require persisted heartbeat for every production-blocking strategy.
- Fail closed on:
  - missing heartbeat
  - stale heartbeat
  - unknown schema version
  - data contract missing
  - manifest missing
  - strategy paused without safety lane ready.
- Include strategy readiness in `SystemHealthController` and `production_state`.

### Tests

- Unit: missing heartbeat -> no_go.
- Unit: idle outside entry window -> not entry-ready but not data failure.
- Unit: system_paused -> strategy no_go for entry, safety remains ready if broker/order path works.
- Unit: local symbol kill switch emits central blocker.
- Integration: strategy with callback heartbeat but entry blocked is no_go.

### Acceptance

- No production state can be green if a production-blocking strategy is entry-eligible but not entry-ready.
- Idle/non-entry-window is represented separately from data failure.
- Strategy heartbeat schema version is checked.

### Merged Implementation Notes

- Landed: central `strategy_tradability` snapshot is built from strategy data contracts, data availability, and strategy heartbeats/liveness.
- Landed: runtime watchdog includes `data_availability` and `strategy_tradability` in `production_state_latest.json`.
- Landed: strategy heartbeats now distinguish idle outside entry window from entry-ready; flat idle outside entry window is accepted as operationally healthy without implying entry readiness.
- Landed: strategy heartbeats with positions/pending work are not allowed to hide behind `idle_not_in_entry_window`.
- Landed: stocks-kernel runtime heartbeat includes data readiness, pending lifecycle, and symbol kill switches.
- Remaining: full schema versioning and standardized fields `entry_ready`, `safety_ready`, `data_ready`, `warmup_ready`, `session_state`, and `order_admission_ready` must be enforced for all strategies.

## Module 8: Data Availability, Warmup, and Feature Seed

### Problem

Data failures historically degraded into empty warmup or strategy-local logs. Production strategies need first-class data contracts and platform gates.

### Production Target

Every strategy declares required datasets. Entry is blocked when required data is unavailable. Safety actions continue.

### TODO

- Strengthen `StrategyDataContract`:
  - dataset
  - symbols
  - resolution
  - TTL
  - warmup min rows
  - RTH/ETH semantics
  - source priority
  - stale behavior
  - whether production-blocking.
- Startup validates every required dataset provider can be constructed.
- Runtime validates actual fresh data, not only config presence.
- Persist data availability gates to shared artifact/DB.
- Data gate event triggers immediate health refresh.
- Define safety lane bypass for data gates.

### Tests

- Unit: ClickHouse exception creates data-unavailable gate.
- Unit: empty warmup during required session creates no_go.
- Unit: stale 1m bars block entry for configured strategy.
- Unit: data gate does not block flatten/protection.
- Integration: stocks kernel missing feature seed prevents entry and appears in production state.
- E2E: data provider outage drill.

### Acceptance

- Every enabled strategy lists data contract in production state.
- Data unavailable never becomes silent no-signal.
- `entry_ready=false` and reason codes are machine-checkable.

### Merged Implementation Notes

- Landed: strategy data contracts and central data availability gates exist and are consumed by production state.
- Landed: stocks-kernel local data blockers are promoted into central tradability/data paths in current production wiring.
- Remaining: startup provider construction, warmup min-row verification, ClickHouse/market-bars/option-chain provider diagnostics, and persistent data-availability artifact schema need full hardening.

## Module 9: Option Strategy Platform

### Problem

Option strategies currently have pieces of a platform flow but not a full production-grade event ledger. Contract selection and signal handoff must be durable and centrally consumed.

### Production Target

Option execution is event-sourced from trigger to contract selection to order to fill. Every trigger receives a terminal verdict.

### TODO

- Create `OptionTriggerLedger`:
  - trigger id
  - source strategy id
  - source decision id
  - underlying
  - direction
  - trigger timestamp
  - consumed/expired/rejected/submitted terminal state.
- Disable latest-cache fallback in production.
- Create `ContractSelectionService`:
  - option chain source
  - quote freshness
  - expiry/strike/right validation
  - spread/liquidity filters
  - risk veto
  - terminal verdict.
- Persist `ContractSelectionVerdict`:
  - selected
  - no_chain
  - stale_chain
  - no_nbbo
  - spread_too_wide
  - no_liquidity
  - risk_veto
  - expired_trigger
  - config_error.
- Add option contract identity checks:
  - underlying
  - expiry
  - strike
  - right
  - multiplier
  - exchange/trading class
  - local symbol/conid when available.
- Route option orders through UOM/ExecutionService.
- Add combo-order support only after UOM-managed combo intent exists.

### Tests

- Unit: trigger without selection verdict blocks option strategy readiness.
- Unit: stale trigger expires once and does not poll latest cache.
- Unit: no NBBO writes verdict and no order intent.
- Unit: old expiry fill cannot attach to current expiry order.
- Unit: selected contract validates full identity.
- Integration: stock signal -> option trigger -> selected contract -> order intent -> terminal verdict.
- E2E: option strategy day with zero submits still emits per-trigger rejected/expired reasons.

### Acceptance

- Every option trigger has a terminal verdict.
- Production state consumes option trigger/contract selection health.
- No production option order can bypass UOM.
- Option PnL excludes unknown/ambiguous fills.

### Merged Implementation Notes

- Landed: QQQ stock-to-option handoff has trigger-level pending queue semantics; the option consumer processes oldest non-terminal trigger and terminalizes as submitted/rejected/expired/risk-blocked.
- Landed: handoff records are written to `runtime/state/qqq_stock_option_handoff.jsonl` and can be replayed after restart for the current single-consumer paper runtime.
- Landed: QQQ option latest-signal fallback is hard-disabled for production even if config requests it.
- Landed: contract-selection rejection payloads include normalized diagnostics such as `NO_QUOTES`, `STALE_QUOTE`, `SPREAD_TOO_WIDE`, `DELTA_OUT_OF_RANGE`, `OI_LOW`, `QUOTE_AGE`, `CHAIN_EMPTY`, and `RISK_VETO`.
- Landed: production state consumes `contract_selection_latest.json` and turns fresh rejected/blocked/failed verdicts into HOLD where required.
- Landed: `OptionTriggerLedgerService` now produces `runtime/readiness/option_trigger_ledger/option_trigger_ledger_latest.json` from QQQ stock-option handoff and contract-selection verdict files.
- Landed: production state consumes option trigger ledger health and blocks on active triggers without terminal verdicts, rejected/failed required verdicts, stale triggers, or lookup failures.
- Landed: option trigger ledger artifact now carries source process start, git head, config hash, max age, age, stale flag, and artifact-before-process-start flag; watchdog rejects stale or pre-process artifacts.
- Landed: `ContractSelectionService` now builds `runtime/readiness/contract_selection/contract_selection_status_latest.json` and is consumed by production state.
- Landed: contract-selection status blocks missing/stale/negative verdicts for recent required handoffs and validates selected internal `OPT:` identity against expiry, strike, and right.
- Runtime proof: current process `44335` emitted `contract_selection.go_no_go=go`, `selected_contract_identity_valid=true`, `option_trigger_ledger.go_no_go=go`, `active_trigger_count=0`, and no blockers at 2026-05-06 00:48 ET.
- Remaining: all option strategy adapters still need to migrate fully to the shared service contract and RTH/e2e option trigger-selection-order-fill drills.
- Runtime proof: current process `47275` emitted `option_trigger_ledger.go_no_go=go`, `stale=false`, `artifact_before_process_start=false`, `active_trigger_count=0`, and no blockers at 2026-05-06 00:24 ET.
- Remaining: shared `ContractSelectionService` is not yet fully centralized across all option strategies.
- Remaining: option fills still need strict full contract identity attribution and unknown/ambiguous quarantine before option PnL can be trusted.
- Remaining: option combo execution remains hard-disabled until represented as UOM/ExecutionService order intent.

## Module 10: IB Restart and Market/Data Plane Recovery

### Problem

IB reconnect alone is not recovery. Order truth, position truth, market data, bar/tick pipeline, strategy tradability, and stable-window checks must all converge.

### Production Target

Every IB disconnect/reconnect generates a recovery session id. The system resumes only after broker/order/position/data/tradability truth is clean and stable.

### TODO

- Add `recovery_session_id` shared by:
  - IBConnectionManager
  - MarketDataService
  - RuntimeReconciler
  - SystemHealthController
  - ProductionStateEmitter.
- Runtime automatically writes restart drill artifact for current process.
- Drill must include:
  - disconnect time
  - reconnect time
  - broker API ready
  - broker truth complete
  - open order snapshot complete
  - position snapshot complete
  - market subscriptions ready
  - tick heartbeat ready
  - bar publish heartbeat ready
  - data gates clear or blocking
  - strategy paused ack
  - system resumed
  - stable window duration
  - flap counts.
- Add thresholds:
  - max reconnect attempts
  - max time to broker API ready
  - max time to clean reconcile
  - max day flap count
  - max recent-window flap count
  - minimum stable running window.

### Tests

- Unit: artifact before process start is rejected.
- Unit: flapping day fails even if final reconnect succeeds.
- Unit: missing market data readiness prevents resume.
- Unit: stale bar/tick heartbeat degrades running system.
- Integration: fake IB reconnect with partial subscriptions.
- E2E: real/simulated IB restart drill producing current-process artifact.

### Acceptance

- `system_resumed` cannot be emitted without platform recovery readiness.
- Drill artifact is generated by the current process after the tested restart.
- `production_state` rejects stale or pre-process drill artifacts.

### Merged Implementation Notes

- Landed: IB restart drill artifact builder creates machine-checkable artifacts from runtime audit system/recon JSONL evidence.
- Landed: watchdog consumes same-day IB restart drill artifacts when disconnect/recovery evidence exists.
- Landed: watchdog rejects drill artifacts generated before the current `run_system.py` process start.
- Landed: an existing same-day IB restart drill artifact that is `no_go`, missing `generated_at`, stale to process start, or generated before current process start now blocks production state even when no post-start disconnect makes the drill `required=false`.
- Landed: old same-day disconnects before current process start do not require a fresh drill for the new process; current-process disconnects still require one.
- Landed: `ProductionStateEmitter` can generate a current-process paper IB restart artifact when post-start disconnect evidence exists and the existing artifact is stale, missing, pre-process, or no-go.
- Landed: drill artifact includes flapping thresholds, time-to-resume, stable-window evidence, and no-go blockers.
- Remaining: shared `recovery_session_id` across IBConnectionManager, MarketDataService, RuntimeReconciler, HealthController, and ProductionStateEmitter is not complete.
- Remaining: full RTH drills with non-flat/safety-lane scenarios still need collection.

## Module 11: Observability, Artifacts, and Operations

### Problem

Old daily reports or stale artifacts can contradict current runtime truth. Operators need one authoritative state.

### Production Target

`production_state_latest.json` is the single go/no-go artifact. All other reports are supporting evidence and must be freshness-checked.

### TODO

- Production state includes:
  - generated_at
  - process pid
  - process start
  - git head
  - git dirty
  - config hash
  - loaded module mtimes/hashes
  - stale modules
  - system health
  - recovery readiness
  - data availability
  - strategy tradability
  - order truth
  - fill truth
  - ledger integrity
  - position safety
  - EOD/session state
  - IB drill
  - option trigger/contract selection health
  - active incidents
  - manual locks.
- All module artifacts have:
  - schema version
  - generated_at
  - source pid
  - process start
  - max age
  - go/no-go
  - blockers
  - clear conditions.
- Operations tools consume production state first.
- Stale artifact defaults to no-go or ignored, never pass.

### Tests

- Unit: stale system health fails.
- Unit: stale position safety fails.
- Unit: stale contract selection fails when required.
- Unit: loaded code stale fails.
- Integration: watchdog writes production state with all modules.
- E2E: kill a safety service and verify production state no_go within TTL.

### Acceptance

- There is exactly one current go/no-go source.
- Every failure has owner service, issue key, severity, first seen, last seen, and clear condition.
- A running process with stopped critical services exits or enters manual halt after bounded no-go duration.

### Merged Implementation Notes

- Landed: runtime watchdog writes unified `runtime/readiness/production_state/production_state_latest.json`.
- Landed: `run_system.py` starts in-process `ProductionStateEmitter`, refreshing production state every 30 seconds.
- Landed: production state includes runtime version metadata: git head/dirty, process pid/start time, and key module mtimes/sizes; loaded-code-stale gates production state to no-go.
- Landed: system health heartbeat is refreshed by ProductionStateEmitter so stable `running` does not go stale just because no transition occurred.
- Landed: opening health report reads the same production state artifact after opening grace.
- Landed: current restart validation after merged P0 slice showed `production_state=go`, watchdog `PASS`, no active quarantines, and no nonzero ledger positions.
- Remaining: every module artifact must be upgraded to the unified artifact contract with schema version, source pid, process start, max age, blockers, clear conditions, and terminal state.
- Remaining: bounded process exit/manual-halt on persistent stopped critical services still needs full lifecycle policy enforcement.

## Module 12: Pluggable Stock Strategy Interface

### Problem

New stock strategies require too much manual config and hidden platform knowledge. That increases risk and slows onboarding.

### Production Target

A new stock strategy is built by implementing a plugin and manifest. The platform handles data, order admission, protection, recovery, EOD, tradability, and accounting.

### Required Plugin Surface

```text
StockStrategyPlugin
  manifest() -> StockStrategyManifest
  on_bar(snapshot) -> list[SignalIntent]
  recovery_policy(context) -> list[RecoveryAction]
  protection_policy(position, market) -> ProtectionSpec
  eod_policy(context) -> EODSpec
```

### Required Manifest Fields

- strategy id
- account id
- symbols/root
- trading session
- entry windows
- data contract
- risk limits
- max position
- order types allowed
- protection requirements
- recovery policy
- EOD/no-overnight policy
- tradability heartbeat requirement
- kill switch rules
- feature seed/warmup requirements.

### TODO

- Create stock strategy scaffolding template.
- Add manifest schema validation.
- Generate stocks-kernel config from manifest where possible.
- Fail startup when manifest is missing or inconsistent.
- Add dry-wire and paper-wire strategy load tests.
- Add plugin conformance tests.

### Tests

- Unit: manifest missing session fails.
- Unit: manifest missing recovery policy fails.
- Unit: strategy cannot submit direct broker order.
- Unit: plugin signal creates standardized `SignalIntent`.
- Integration: new sample stock plugin loads with production wiring.
- E2E: sample stock plugin trades one test cycle and exits verified flat.

### Acceptance

- A new stock strategy can be added with plugin + manifest + tests only.
- It cannot bypass UOM/ExecutionService.
- It inherits platform recovery/protection/EOD automatically.

### Merged Implementation Notes

- Landed: `StrategyProductionManifest`, `BaseStockPlugin.production_manifest()`, and `StockStrategyManifest` exist.
- Landed: `NVDAMinuteFlipStockPlugin` declares its own manifest including NVDA data contract, UOM-only entry, close-only recovery, no-overnight EOD, protection fallback, dynamic policy registry spec, and tradability reporting.
- Landed: manifest-to-stocks-kernel config generation exists for NVDA minute flip.
- Landed: startup blocks mandatory enabled strategies when manifest, data contract, UOM-only entry, recovery, no-overnight EOD, or tradability reporting fields are missing.
- Landed: manifest recovery policy ids are validated against known registered policies; implicit `default_close_only` fallback is rejected for enabled paper strategies.
- Landed: runtime recovery registry is built from production manifests instead of guessing by strategy id.
- Landed: `tool/build_stocks_kernel_instance_config.py --plugin-cls ... --validate-production` emits a machine-checkable stock onboarding admission report containing generated YAML and the manifest/session gate verdict.
- Remaining: migrate all new stock strategies to manifest-generated config and add paper-wire/e2e onboarding drills.

## Module 13: Pluggable Option Strategy Interface

### Problem

Option strategy onboarding is more complex than stock onboarding because contract selection, option chain quality, trigger lineage, and option fill attribution are not fully platformized.

### Production Target

A new option strategy declares trigger source, option universe, contract selection preferences, risk, and exit/protection policy. The platform handles trigger ledger, contract selection ledger, UOM execution, fill attribution, and PnL isolation.

### Required Plugin Surface

```text
OptionStrategyPlugin
  manifest() -> OptionStrategyManifest
  trigger_filter(trigger) -> TriggerDecision
  selection_preferences(trigger, chain) -> SelectionPreferences
  risk_policy(selection, account_state) -> RiskDecision
  exit_policy(position, market) -> ExitSpec
```

### Required Manifest Fields

- strategy id
- underlying/root
- trigger source ids
- allowed expiries
- allowed rights
- strike selection rules
- max spread
- min NBBO quality
- quote TTL
- option chain source
- max contracts
- max premium
- max daily loss
- session windows
- expiry-day handling
- exercise/assignment policy
- recovery/EOD policy.

### TODO

- Build option strategy scaffolding.
- Add contract selection service adapter.
- Add trigger ledger adapter.
- Add option order/fill attribution tests.
- Add option session/EOD handling for 0DTE and 1DTE separately.
- Add option contract identity validator.

### Tests

- Unit: missing option chain creates no-chain verdict.
- Unit: stale quote creates stale-quote verdict.
- Unit: wide spread creates spread-too-wide verdict.
- Unit: expired trigger creates expired verdict.
- Unit: old expiry fill is quarantined.
- Integration: stock trigger drives option selected/rejected verdict.
- E2E: option smoke strategy runs without open position or unknown fill at session end.

### Acceptance

- Every option strategy trigger has a terminal verdict.
- Every option order/fill is tied to exact contract identity.
- Option strategy can be production-blocking only when trigger, data, contract selection, UOM, and fill attribution checks are all green.

### Merged Implementation Notes

- Landed: option production path has partial trigger queue and contract verdict visibility for QQQ option flow.
- Landed: legacy option direct execution is hard-disabled when `OPTION_ML_EXECUTION_ENABLED=1`; `run_system.py` fails fast instead of allowing direct IB combo submission.
- Landed: `OptionStrategyManifest`, template plugin manifest, option strategy config builder, `tool/build_option_strategy_instance_config.py --validate-production` admission report, and runtime option policy checks inside `strategy_manifest_gate` now exist.
- Remaining: trigger-ledger adapter, contract-selection adapter migration for all live option strategies, and 0DTE/1DTE session/EOD fault drills are not complete.

## Unified Artifact Contract

Every module-level artifact consumed by `production_state_latest.json` must follow this minimum shape:

```json
{
  "schema_version": 1,
  "artifact_type": "module_specific_name",
  "generated_at": "ISO-8601 timestamp with timezone",
  "source_process_pid": 0,
  "source_process_start_time": "ISO-8601 timestamp with timezone",
  "git_head": "short or full sha",
  "config_hash": "sha256",
  "go_no_go": "go|no_go|hold",
  "status": "machine_state",
  "max_age_sec": 90,
  "blockers": [],
  "records": [],
  "clear_conditions": [],
  "terminal": false,
  "evidence": {}
}
```

Rules:

- If `generated_at` is stale, the artifact cannot produce `go`.
- If `source_process_start_time` is before the current `run_system.py` process start and the artifact claims runtime readiness, it cannot produce `go`.
- If `schema_version` is missing or unsupported, the artifact is `no_go`.
- If an artifact reports `go` while any record has active quarantine/manual lock/unsafe state, the artifact is internally contradictory and must be `no_go`.
- If a module cannot inspect its truth source, it reports truth unavailable; it must not report empty/clean.

## Required Artifact Inventory

| Artifact | Owner | Required for go | Main truth source |
| --- | --- | --- | --- |
| `production_state_latest.json` | ProductionStateEmitter/watchdog | yes | all module artifacts |
| `system_health_transition_*.jsonl` | SystemHealthController | yes | health state machine |
| `runtime_reconciliation_latest.json` | RuntimeReconciler | yes | broker/order/position truth |
| `position_safety_latest.json` | PositionSafetySupervisor | yes | broker positions + broker open orders |
| `fill_attribution_latest.json` | BrokerTruthConvergence/FillAttributionService | yes | broker executions + core_order |
| `ledger_integrity_latest.json` | LedgerIntegrityService | yes | ledger/core_position/core_fill |
| `recovery_incidents_latest.json` | RecoveryIncidentService | yes | active incidents/quarantine/manual locks |
| `platform_eod_flatten_YYYY-MM-DD.json` | PlatformEODSupervisor | yes after cutoff | broker/DB positions |
| `strategy_session_latest.json` | StrategySessionSupervisor | yes | strategy manifests + broker truth |
| `strategy_tradability_latest.json` | StrategyTradabilityService | yes | strategy heartbeats |
| `data_availability_latest.json` | DataAvailabilityService | yes | data contracts/providers |
| `option_trigger_ledger_latest.json` | OptionTriggerLedger | required for enabled option strategies | trigger ledger |
| `contract_selection_latest.json` | ContractSelectionService | required for enabled option strategies | option chain/quotes |
| `ib_restart_latest.json` | IB recovery drill writer | required after reconnect/drill windows | recovery session |

## Fault Drill Artifact Contract

Every e2e drill must emit a JSON artifact with:

- drill name
- process pid and process start
- git/config version
- fault injection time
- expected state transitions
- actual state transitions
- broker order ids involved
- broker position snapshots before/after
- DB order/fill/ledger snapshots before/after
- strategy ids involved
- recovery incident ids
- manual lock ids
- duplicate order count
- unknown fill/order count
- final go/no-go
- reason codes for every failed assertion.

The drill is invalid if it only checks logs. It must read machine artifacts and broker/DB truth.

## Runtime Stop Policy

Fail-closed is not enough for unattended production if the process continues indefinitely with stopped critical services.

Required behavior:

- If system health is `no_go` because a critical service stopped, the process gets a bounded recovery window.
- If the service does not recover before the threshold, `run_system.py` exits or enters explicit manual halt.
- External supervisor restart is preferred for infrastructure failure.
- Manual halt is preferred for broker/account/order ambiguity.

Critical services:

- IB connection manager
- ExecutionService
- UOM/DecisionPipeline
- RuntimeReconciler
- PositionSafetySupervisor
- PlatformEODSupervisor
- MarketDataService
- ProductionStateEmitter
- DataAvailabilityService

Acceptance:

- Kill each critical service in a drill.
- `production_state` becomes `no_go` within TTL.
- Process exits or manual-halts within configured threshold.
- No new entry orders are accepted during the failure.

## Strategy Onboarding Gate

No stock or option strategy may be enabled as production-blocking unless this gate passes.

Required files or generated records:

- strategy manifest
- data contract
- session policy
- risk policy
- protection policy
- recovery policy
- EOD/no-overnight policy
- tradability heartbeat implementation
- unit tests
- dry-wire integration test
- paper-wire integration test
- one e2e trade/no-trade drill.

Stock strategy minimum drill:

- warmup ready
- one signal accepted
- one entry intent submitted through UOM
- protection verified at broker or fake broker
- exit/flatten verified
- ledger clean
- no unknown fills/orders.

Option strategy minimum drill:

- trigger accepted
- contract selected or rejected with verdict
- order submitted through UOM if selected
- fill attributed to exact option contract or quarantined
- expiry/session handling verified
- no stale latest-cache handoff.

## Global Test Matrix

### Unit Test Gates

- Truth model invariants.
- Fill attribution.
- Unknown fill/order quarantine.
- Recovery incident idempotency.
- Safety lane close-only semantics.
- Protection order side/qty/price sanity.
- EOD/session policy.
- Strategy manifest validation.
- Data contract validation.
- Option contract selection verdicts.
- Artifact freshness/version checks.

### Integration Test Gates

- Real Postgres schema for order/fill/ledger/quarantine/manual lock.
- StrategyEngine real dependency dry-wire.
- UOM/ExecutionService safety lane with fake broker adapter.
- RuntimeReconciler with broker/DB mismatch fixtures.
- MarketData/DataAvailabilityService with stale/missing data.
- PositionSafetySupervisor with broker position/open order fixtures.
- Option trigger/selection/order pipeline.

### E2E/Fault Drill Gates

- IB disconnect/reconnect.
- IB flapping.
- Broker-only position after restart.
- Unknown broker order.
- Unknown fill.
- Partial fill then disconnect.
- Submitted flatten not verified.
- Protection registry stale.
- Data provider outage.
- Bar/tick producer outage.
- Strategy loop stopped.
- After-hours entry after global EOD.
- No-overnight final deadline.
- Option no-chain/no-NBBO/stale-quote day.

## Go/No-go Acceptance Checklist

Use this before enabling unattended production or adding more live strategies.

### Platform

- [ ] `production_state_latest.json` is fresh and `go`.
- [ ] Loaded code is current: no stale modules.
- [ ] Config hash matches expected deployment.
- [ ] System health is `running`.
- [ ] Recovery readiness is fresh and clean.
- [ ] IB drill artifact is current-process and `go`.
- [ ] No critical artifact is stale/missing.

### Broker and Orders

- [ ] Broker positions match verified platform position truth.
- [ ] Broker open orders match verified platform order truth.
- [ ] Unknown broker orders count is zero or manual-acknowledged with no risk.
- [ ] Unknown fills count is zero.
- [ ] No stale DB `SUBMITTED` order missing at broker.
- [ ] No flat-account live reduce-only orders.
- [ ] No direct IB order paths enabled.

### Ledger and Accounting

- [ ] No null-lot residue.
- [ ] No `UNKNOWN_ACCOUNT` residue.
- [ ] No qty=0/notional!=0 residue.
- [ ] No mixed-account symbol truth.
- [ ] No dirty avg price used for protection.
- [ ] PnL excludes unknown/ambiguous fills.

### Recovery and Safety

- [ ] Active recovery incidents are zero, or terminal manual-required with locks.
- [ ] Active quarantine count is zero, or all linked to incidents.
- [ ] Every nonzero broker position is protected/flattening/manual-required/allowed-overnight.
- [ ] Safety lane flatten works with broker truth.
- [ ] Protection registry is broker-reconciled.

### Strategy Readiness

- [ ] Every enabled production-blocking strategy has fresh heartbeat.
- [ ] Every strategy has manifest.
- [ ] Every strategy has data contract.
- [ ] Every strategy has session/EOD policy.
- [ ] Every strategy has recovery/protection policy.
- [ ] No strategy reports entry-ready while platform is paused/degraded.

### Stock Strategies

- [ ] Stock plugin conformance passes.
- [ ] Warmup/feature seed ready.
- [ ] Signal-to-order path goes through UOM.
- [ ] Entry, protection, exit, and recovery intents are standardized.
- [ ] Session-end flat/protected state is verified.

### Option Strategies

- [ ] Option trigger ledger is enabled.
- [ ] Latest-cache production fallback is disabled.
- [ ] Contract selection verdicts are fresh.
- [ ] Option chain/quote data is fresh.
- [ ] No stale/old-expiry fill attribution.
- [ ] Option orders go through UOM.
- [ ] Option session/EOD state is verified.

## Milestones

### Milestone 0: Risk Freeze

Goal: no new live strategy expansion until current truth is clean.

Exit criteria:

- Current broker account is manually verified.
- Current NVDA residual is flat/protected/manual-required with explicit incident.
- Active quarantine/manual locks are explained.
- Unknown fills/orders are quarantined.

Current merged status:

- Runtime restart completed after strict fill-attribution, recovery-incident terminal consistency, IB-drill gate hardening, strategy manifest gate hardening, and strategy-tradability/data-gate reconciliation.
- Current process `95805` passed an operator-paper IB disconnect/reconnect drill after restart; `paper_ib_restart_latest.go_no_go=go`, no blockers, and the artifact was generated after process start.
- Current runtime evidence at `2026-05-06T11:16:01-04:00` is `production_state=go`, `failure_count=0`, `broker_truth_snapshot.go_no_go=go`, `broker_truth_snapshot.broker_truth_backed=true`, `position_safety=go`, `recovery_incidents=go`, `fill_attribution=go`, `contract_selection=go`, and `option_trigger_ledger=go`.
- Disk-space hard gate landed after an observed `No space left on device` runtime failure path; production state now emits `disk_space` and blocks below `operations.runtime_watchdog.min_free_disk_bytes` / `PRODUCTION_MIN_FREE_DISK_BYTES`.
- Clean subsystems in current runtime: `position_safety=go`, `ledger_integrity=go`, `broker_truth_snapshot=go`, `fill_attribution=go`, `contract_selection=go`, `option_trigger_ledger=go`, no active broker/order quarantines, no nonzero ledger positions, `unknown_fill_count=0`, `ambiguous_fill_count=0`, and `recovery_incidents_latest.active_count=0`.
- This clears 100% of current machine-checkable paper-runtime blockers. It is still not unattended live production certification until real RTH/after-hours drills and non-flat recovery drills pass with fresh artifacts.

### Milestone 1: Truth and Attribution Hardening

Goal: DB/fill/ledger cannot poison system truth.

Exit criteria:

- Unknown fill/order quarantine exists.
- Strong attribution is required.
- Ledger integrity catches lot/account/notional residues.
- PnL excludes unknown/dirty facts.

Current merged status:

- Strict unknown-fill quarantine, strong unique existing-order fill attribution, ambiguous-fill quarantine, `core.fill_attribution_ledger`, and fill-attribution readiness artifact landed; adjudication workflow and exact option contract attribution remain.
- First-class ledger integrity artifact landed for UNKNOWN/null lots, account divergence, and zero-qty notional residuals; provenance/max-age hardening remains.
- Broker truth snapshot artifact/gate landed for active quarantine, open order, nonzero projection, and ledger/core-position divergence checks; full live broker execution provenance remains.
- Broker truth snapshot no longer passes as DB-projection-only: production state now requires a fresh live broker-backed position-safety artifact unless explicitly disabled.
- PositionLedger `current_position` now replays ordered ledger events to preserve weighted average cost through reductions and returns realized PnL evidence instead of signed-notional divided by net qty.

### Milestone 2: Recovery Closure

Goal: every runtime anomaly reaches terminal state.

Exit criteria:

- RecoveryIncidentService is live.
- PositionSafetySupervisor links to incidents.
- Close-only recovery uses broker truth.
- EOD/session supervisors verify terminal state.

Current merged status:

- File-backed RecoveryIncidentService is live in position safety.
- Broker-truth close-only recovery lane landed.
- EOD manual-required/verified-flat linkage landed.
- Platform EOD no longer stops for the day after a same-day `VERIFIED_FLAT`; it periodically re-runs broker-truth verification after the final flatten deadline to catch late same-day position reappearance.
- RuntimeReconciler order/position quarantine linkage landed.
- Session-end no-overnight supervisor linkage landed.
- Terminal incident consistency landed; current legacy `verified_flat` runtime incident was normalized to clear `active_quarantine=false` only because it had `verified_flat_at` evidence.
- Active intent, retry/backoff, retry exhaustion, cancel-replace pending state, action queue, and artifact counters landed.
- Rich per-session execution-loop consumption of incident retry/cancel-replace remains for broader unattended fault-drill certification.

### Milestone 3: Strategy Contract Hardening

Goal: stock strategies are production-grade pluggable.

Exit criteria:

- StockStrategyManifest schema is enforced.
- Stock plugin scaffold exists.
- Sample stock strategy passes e2e trade/flatten drill.
- New stock strategy onboarding checklist is under one page.

Current merged status:

- Stock manifest/plugin path exists and is enforced for current enabled strategies.
- Scaffold/admission CLI is live; sample real-session e2e onboarding and richer protection policy semantics remain.

### Milestone 4: Option Platformization

Goal: option strategies are production-grade pluggable.

Exit criteria:

- OptionTriggerLedger exists.
- ContractSelectionService exists.
- Option fill attribution is contract-identity strict.
- Sample option strategy passes trigger/selection/order/fill/expiry drill.

Current merged status:

- QQQ option trigger handoff and contract verdicts are partially durable.
- Platform OptionTriggerLedger artifact/gate is live and current runtime is `go` with zero active triggers.
- ContractSelectionService artifact/gate, option strategy scaffold/admission CLI, and exact option fill attribution are live; selected contracts now require multiplier, exchange, tradingClass, and broker contract identity.
- OptionTriggerLedger no longer treats `selected` as terminal; a trigger must reach filled/rejected/expired/cancelled/risk-blocked lifecycle evidence before it leaves active state. Full adapter migration and RTH/e2e drills remain.

### Milestone 5: Unattended Production Acceptance

Goal: run without manual intervention under defined fault drills.

Exit criteria:

- Full e2e fault drill suite passes.
- One full RTH session and one after-hours/night session produce `go` artifacts.
- IB restart drill passes for current process.
- No unknown fill/order/quarantine remains unlinked.
- EOD/no-overnight final state is verified flat or allowed overnight.

Current merged status:

- Existing same-day IB drill `no_go` now correctly blocks production even when no post-start disconnect makes the drill optional.
- ProductionStateEmitter now rebuilds a current-process drill artifact for existing bad/stale/pre-process drill artifacts even when no post-start disconnect makes `required=false`.
- Watchdog no longer double-counts `system_health:degraded` when it was emitted by production_state itself with `reason=production_state_no_go`.
- Operator-paper drill can now be triggered in-process with `SIGUSR1`; it emits disconnect/reconnect/reconcile/resume audit evidence, waits for the configured stable window, and rebuilds the drill artifact to terminal `go`.
- IB drill artifact builder now pins stable-window timing to the operator drill verified-resume event; later `production_state_soft_no_go` heartbeat resumes no longer reset the stable-window clock.
- Current runtime passed this drill on process `95805`: `production_state=go`, `failure_count=0`, `paper_ib_restart_latest.go_no_go=go`, `broker_truth_snapshot=go`, `broker_truth_backed=true`, `fill_attribution=go`, `contract_selection=go`, `option_trigger_ledger=go`, `strategy_manifest_gate=go`, `strategy_tradability=go`, and `data_availability.ok=true`.

## Remaining Percent Snapshot

As of `2026-05-06T11:16:01-04:00` code and runtime validation:

- Current machine-checkable paper-runtime blockers: `0%` remaining.
- Current stock/option pluggability code-path blockers in this todo: `0%` remaining for manifest/admission/gate enforcement; `5-10%` remains for real-session e2e onboarding evidence.
- Order/fill accounting hardening blockers in this todo: `0%` remaining for current artifacts and strict account-scoped attribution gates; `2-4%` remains for broader live broker replay coverage and operator adjudication UX.
- Unattended live production certification: `10-15%` remaining, because it still requires fresh real RTH/after-hours session artifacts and non-flat recovery fault drills that cannot be manufactured from the current after-hours state.
- Overall code/artifact implementation gap for this merged todo: `0-2%` remaining; remaining risk is acceptance evidence plus completed-execution broker provenance, not a known current-runtime blocker after the next restart/drill.

## Required Release Rule

No module is 100% production-grade until it satisfies:

- deterministic unit tests,
- real-schema integration tests,
- e2e/fault drill,
- fresh machine artifact,
- go/no-go consumption by `production_state`,
- documented clear conditions,
- and a current-process runtime proof.

Passing `pytest` alone is not sufficient.

## 2026-05-06 21:02 ET Live Runtime TODO Merge

Current runtime is intentionally `NO_GO`, not production-ready. The code path is fail-closed and current-process artifacts are fresh, but live broker truth still reports a nonzero option position:

- `production_state_latest.json`: `go_no_go=no_go`
- `position_safety_latest.json`: `go_no_go=no_go`
- live broker position: `OPT:NVDA:20260506:207.5:P qty=1`
- platform EOD state: `AWAITING_MARKET_REOPEN`
- EOD blocker reason: `manual_flatten_awaiting_option_market_reopen`

### Newly Landed

- `position_ledger_current` no longer depends on an unbounded recursive view; it now reads `core.position_ledger_current_state`, with lane-scoped refresh on new ledger events and one-time bootstrap from historical lanes.
- Low-price option manual flatten limit generation no longer rounds a `0.02` reference down to `0.01`.
- Manual flatten limit guard allows one minimum tick of unavoidable deviation for low-priced instruments while still rejecting materially out-of-band orders.
- Option manual flatten outside RTH no longer submits invalid overnight option orders. It returns `manual_flatten_awaiting_option_market_reopen`.
- Platform EOD treats market-reopen waits as non-attempt states, writes `awaiting_market_reopen_not_before`, and hydrates this state after restart to avoid retry spam.

### Immediate TODO

| Priority | Item | Owner path | Acceptance |
| --- | --- | --- | --- |
| P0 | Resolve current broker option residual | Broker/manual operation, then runtime verification | IB position `OPT:NVDA:20260506:207.5:P` is zero, position safety resolves to `go`, EOD becomes `VERIFIED_FLAT`, and active recovery/manual-lock artifacts clear or terminalize |
| P0 | RTH option flatten verification | `services/telegram/manual_flatten_service.py`, `services/production/eod_supervisor.py` | At next option RTH window, the residual either submits a valid close-only flatten and verifies flat, or produces durable `manual_required` with broker evidence |
| P0 | Recovery/manual lock terminal cleanup after broker flat | `services/production/recovery_incident_service.py`, `services/production/position_safety_supervisor.py` | Once broker is flat, all incidents for the lane are terminal `verified_flat` or acknowledged manual-required; no terminal incident carries active quarantine |
| P1 | Real RTH/after-hours fault drill evidence | runtime drill artifacts | One RTH and one after-hours session produce fresh current-process artifacts proving no false green and clean convergence |

### Current Remaining Percent Snapshot

- Current runtime production go: `100% remaining until broker residual is flat or manual-required is explicitly acknowledged`; this is an external broker-state blocker, not a green code path.
- Code/artifact implementation gap for reviewed P0/P1 items: about `0-3%` remaining after the EOD awaiting-market-reopen persistence fix.
- Stock strategy pluggability: about `10-15%` remaining, dominated by real-session e2e onboarding evidence.
- Option strategy pluggability: about `15-20%` remaining, dominated by RTH trigger/selection/order/fill/expiry drills and current residual closure.
- Unattended recovery certification: about `20-25%` remaining until the non-flat option residual is proven to converge or escalate under current broker truth.

## 2026-05-06 21:40 ET Live Runtime Follow-up

Current runtime remains intentionally `NO_GO` because IB broker truth still reports:

- `OPT:NVDA:20260506:207.5:P qty=1`
- `platform_eod.state=AWAITING_MARKET_REOPEN`
- `awaiting_market_reopen_not_before=2026-05-07T09:30:00-04:00`

### Newly Landed

- NVDA smoke strategy tradability now separates entry readiness from safety readiness. During off-session/no-entry windows it emits `entry_ready=false`, `safety_ready=true`, and, when EOD/position-safety owns the residual, `safety_supervised=true`.
- Central strategy tradability no longer treats a supervised off-session position as an entry blocker. It still rejects unsupervised idle positions.
- Platform EOD now writes `AWAITING_MARKET_REOPEN` into RecoveryIncidentService as a durable active recovery intent with `next_action=wait_until_market_reopen`.
- Platform EOD hydration migrates old awaiting-market-reopen artifacts by writing `recovery_incident_ids` back into the EOD artifact.
- RecoveryIncidentService preserves `awaiting_market_reopen` state across later detector upserts, so runtime reconciler/position safety cannot downgrade the incident back to naked `detected`.

### Current Machine Evidence

- `production_state_latest.go_no_go=no_go`
- `production_state_latest.failure_count=8`
- `position_safety_latest.go_no_go=no_go`
- `position_safety_latest.broker_position_count=1`
- `position_safety_latest.active_quarantine_count=2`
- `recovery_incidents_latest.active_count=2`
- `recovery_incidents_latest.active_intent_count=2`
- Active NVDA incidents are `awaiting_market_reopen` with `next_action=wait_until_market_reopen`.
- EOD artifact now links both active lane incidents:
  - `recovery_incident:nvda_option_churn_smoke_001:OPT:NVDA:20260506:207.5:P`
  - `recovery_incident:runtime_reconciler:OPT:NVDA:20260506:207.5:P`

### Latest Verification

- `venv/bin/python -m py_compile services/production/eod_supervisor.py services/production/recovery_incident_service.py services/production/strategy_tradability.py strategies/nvda_option_smoke_strategy.py`
- `venv/bin/python -m pytest tests/unit/services/test_recovery_incident_service.py tests/unit/services/test_production_readiness_services.py tests/unit/strategies/test_qqq_0dte_opt_live_parity.py tests/unit/strategies/options_kernel/test_base.py -q`
- Result: `138 passed, 3 warnings`

### Updated Remaining Percent Snapshot

- Current runtime production go: `100% remaining until broker residual is actually flat, or the next RTH attempt produces explicit manual-required evidence`; production state must stay `NO_GO` before that.
- Code/artifact implementation gap for the current reviewed P0/P1 items: `0-2%` remaining; the remaining code risk is drill breadth, not a known false-green path.
- Stock strategy pluggability: `10-15%` remaining, still dominated by real-session e2e onboarding evidence.
- Option strategy pluggability: `12-18%` remaining after entry/safety readiness and EOD recovery-intent wiring; remaining work is RTH trigger/selection/order/fill/expiry evidence and current residual closure.
- Unattended recovery certification: `18-22%` remaining until the current non-flat option residual converges or escalates under live broker truth during an eligible option market window.

## 2026-05-06 22:03 ET Blocker Semantics Follow-up

Current runtime remains `NO_GO`, but blocker semantics are now more precise and machine-actionable:

- `PLATFORM_EOD` reports `platform_eod_awaiting_market_reopen`, with `awaiting_market_reopen_not_before=2026-05-07T09:30:00-04:00`.
- `RECOVERY_INCIDENTS` reports `active_recovery_incidents_awaiting_market_reopen`, not generic active incident text.
- `recovery_incidents_latest.awaiting_market_reopen_count=2`.
- `MANUAL_LOCK` reports `awaiting_market_reopen_count=1` and `manual_lock_without_awaiting_context_count=1`.
- Position-safety-created manual locks now inherit `awaiting_market_reopen_not_before` and linked recovery incident ids from RecoveryIncidentService.
- Existing older `strategy_recovery_action_quarantine` locks may still lack awaiting-market context until refreshed/recreated; production state now exposes that count explicitly.

### Latest Verification

- `venv/bin/python -m pytest tests/unit/services/test_recovery_incident_service.py tests/unit/services/test_production_readiness_services.py tests/unit/tool/test_run_runtime_operations_watchdog.py tests/unit/strategies/test_qqq_0dte_opt_live_parity.py tests/unit/strategies/options_kernel/test_base.py -q`
- Result: `198 passed, 3 warnings`

### Updated Remaining Percent Snapshot

- Current runtime production go: still blocked until the broker residual is flat or the RTH retry reaches durable manual-required evidence.
- Code/artifact implementation gap for current P0/P1 reviewed items: `0-1%` remaining. The known false-green holes in this slice are closed; remaining work is acceptance evidence and stale lock context refresh.
- Option strategy pluggability: `12-18%` remaining, dominated by real RTH trigger/selection/order/fill/expiry drills and residual closure.
- Unattended recovery certification: `18-22%` remaining until current non-flat option residual converges under live broker truth.
