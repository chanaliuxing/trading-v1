# Canary Readiness Manual Gate - 2026-05-12

## Decision

`CANARY_PREP_PACKAGE_READY`

This report prepares the canary evidence package only. It does not approve live canary, full live trading, unattended live, or real broker mutation.

## Preconditions

| Item | Status | Evidence |
|---|---|---|
| Final OMS Regression Gate | PASS | `CANARY_PREP_ALLOWED` in `docs/reports/oms_final_regression_gate_20260512.md` |
| Paper/Shadow Soak Gate | PASS | `SOAK_PASS_READY_FOR_CANARY_MANUAL_GATE` in `docs/reports/runtime_paper_shadow_real_artifact_collection_20260512.md` |
| no-live guard | PASS | `4 passed` |
| Operator proof CLI | PASS | `12 passed` |
| Fast IB reconnect regression | PASS | `32 passed` |
| Broker truth contract | PASS | production readiness services/watchdog tests |
| Position safety contract | PASS | production readiness services/watchdog tests |
| Tradability contract | PASS | production readiness services/watchdog tests |
| Production state aggregation | PASS | production readiness services/watchdog tests |
| Strategy surface tiering | PASS | strategy surface/report tests |
| Runtime freshness | PASS | production readiness services/watchdog tests |
| Approved-to-fill diagnostics | PRESENT | `reports/approved_to_fill_conversion/approved_to_fill_conversion_latest.json` |
| Vegas diagnostics | PRESENT | `reports/vegas_dlb_edge_measurement/vegas_dlb_edge_measurement_latest.json` |
| Unknown/manual broker order policy | PASS | manual review only; no auto-cancel/auto-flatten approval |
| Attribution policy | PASS | no symbol-only or order-ref heuristic owner proof |

## Manual Gate Evidence

| Gate | Status | Evidence |
|---|---|---|
| Lease-denied exit code regression | FIXED | targeted test `1 passed`; full CLI `12 passed` |
| Lease denied does not construct IB | PASS | `test_operator_proof_cli_lease_denied_does_not_construct_ib` |
| Proof failure exit code | PASS | operator proof CLI suite |
| Missing live flag / ack / unauthorized exit code | PASS | operator proof CLI suite |
| Dry-run / success exit code | PASS | operator proof CLI suite |
| Paper/shadow artifacts | PASS | soak gate `PASS` |
| OMS invariants | PASS | zero OMS red tests; soak gate invariants pass |
| Diagnostics | PASS | approved-to-fill and Vegas reports present |

## Blocking Items

None for canary preparation package. Live canary launch remains blocked until the separate manual launch gate is completed.

## Manual Approvals Required

- Operator confirms IB Gateway state.
- Operator confirms no unexpected live positions/open orders.
- Operator confirms strategy list and canary sizing.
- Operator confirms kill switch / emergency stop.
- Operator confirms unknown broker order policy.
- Operator confirms runtime freshness.

## Approved Scope

- Prepare canary checklist.
- Review paper/shadow artifacts.
- Manual operator readiness review.

## Not Approved Scope

- Start live canary.
- Full live trading.
- Unattended live.
- Real broker mutation.
- Auto-cancel unknown broker orders.
- Auto-flatten unknown owner positions.

## Recommended Next Batch

`Canary Sizing and Manual Launch Checklist`
