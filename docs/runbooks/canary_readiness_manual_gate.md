# Canary Readiness Manual Gate

This manual gate prepares canary evidence only. It does not start live canary, approve unattended live, or permit real broker mutation.

## Preconditions

- Final OMS Regression Gate verdict is `CANARY_PREP_ALLOWED`.
- Paper/shadow soak decision is `SOAK_PASS_READY_FOR_CANARY_MANUAL_GATE`.
- No-live guard is green.
- Operator IB Gateway recovery proof CLI tests are green.
- Fast IB reconnect regression is green.
- Broker truth, position safety, tradability, production state, strategy surface, and runtime freshness contracts are green.
- Approved-to-fill and Vegas diagnostics are present.

## Manual Approvals Required

- Operator confirms IB Gateway state.
- Operator confirms no unexpected live positions/open orders.
- Operator confirms strategy list and canary sizing.
- Operator confirms kill switch / emergency stop.
- Operator confirms unknown broker order policy.
- Operator confirms runtime freshness.

## Hard Stops

- Do not set `TRADING_TEST_ALLOW_LIVE_IBKR=1` in this gate.
- Do not start live canary from this gate.
- Do not place, cancel, or modify real broker orders.
- Do not auto-cancel unknown broker orders.
- Do not auto-flatten unknown owner positions.
- Do not use symbol-only attribution.
- Do not use `order_ref` prefix or substring as owner proof.

## Evidence Checklist

| Gate | Required Evidence |
|---|---|
| OMS regression | `docs/reports/oms_final_regression_gate_20260512.md` |
| Paper/shadow soak | `docs/reports/runtime_paper_shadow_real_artifact_collection_20260512.md` |
| No-live guard | `tests/unit/safety/test_no_live_ibkr_in_unit_tests.py` |
| Operator proof | `tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py` |
| Fast reconnect | `scripts/run_ib_reconnect_regression.sh fast` |
| Soak gate | `reports/paper_shadow_soak_gate/paper_shadow_soak_gate_latest.json` |
| Approved-to-fill | `reports/approved_to_fill_conversion/approved_to_fill_conversion_latest.json` |
| Vegas measurement | `reports/vegas_dlb_edge_measurement/vegas_dlb_edge_measurement_latest.json` |

## Decision Values

- `CANARY_PREP_PACKAGE_READY`: evidence package is ready for manual operator review.
- `CANARY_PREP_BLOCKED`: one or more required gates are missing or failed.

This gate may only prepare the canary checklist and evidence package. A separate manual launch gate is required before any live canary.
