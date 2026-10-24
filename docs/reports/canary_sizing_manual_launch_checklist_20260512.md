# Canary Sizing and Manual Launch Checklist - 2026-05-12

## Decision

`CANARY_MANUAL_REVIEW_READY`

This package is ready for manual canary launch review only. It does not approve live canary execution, unattended live trading, full live trading, or real broker mutation.

## Evidence Summary

| Gate | Status | Evidence |
|---|---|---|
| Final OMS regression | PASS | `docs/reports/oms_final_regression_gate_20260512.md` |
| Canary readiness manual gate | PASS | `CANARY_PREP_PACKAGE_READY` |
| Paper/shadow bounded runtime | PASS | 10 `SHADOW_ONLY` samples and 10 `PAPER_ONLY` fake paper broker samples |
| Soak gate | PASS | `reports/paper_shadow_soak_gate/paper_shadow_soak_gate_latest.json` |
| Strategy execution reliability | PASS | `sample_count=20`, UOM `delegated=10`, `skipped=10`, drop stage `none=20`, primary bottleneck `NO_BOTTLENECK` |
| Approved-to-fill conversion | PASS | `sample_count=20`, broker submit drops `0`, primary conversion loss `NO_BOTTLENECK` |
| Vegas DLB/edge | PASS | DLB breaches `0`, edge insufficient `0`, stale decisions `0`, primary driver `NO_BOTTLENECK` |
| no-live guard | PASS | `4 passed` |
| services | PASS | `656 passed` |
| exec | PASS | `293 passed` |
| reconciliation | PASS | `76 passed` |
| reports | PASS | `61 passed` |
| IB reconnect fast regression | PASS | `32 passed` |

## Sizing Review

| Item | Required Manual Review | Status |
|---|---|---|
| Strategy list | Operator confirms exact strategy IDs and surface permission tiers. | READY_FOR_REVIEW |
| Quantity cap | Operator sets canary max quantity below production size before launch. | REQUIRED_MANUAL_INPUT |
| Notional cap | Operator sets account and per-strategy notional cap before launch. | REQUIRED_MANUAL_INPUT |
| Loss cap | Operator confirms per-trade and daily stop before launch. | REQUIRED_MANUAL_INPUT |
| Order type | Operator confirms order type and option quote freshness policy. | REQUIRED_MANUAL_INPUT |
| Kill switch | Operator confirms entry stop and emergency stop procedure. | REQUIRED_MANUAL_INPUT |
| Unknown/manual broker orders | Policy remains manual-review only; no auto-cancel or auto-flatten. | PASS |

## Launch Checklist

| Check | Required State | Status |
|---|---|---|
| `TRADING_TEST_ALLOW_LIVE_IBKR` | Must remain `0` until separate live operator opt-in gate. | PASS_FOR_THIS_GATE |
| Broker truth | Fresh and broker-backed in the future live opt-in gate. | REQUIRED_LIVE_GATE_CHECK |
| Position safety | Fresh and go in the future live opt-in gate. | REQUIRED_LIVE_GATE_CHECK |
| Runtime freshness | Current in the future live opt-in gate. | REQUIRED_LIVE_GATE_CHECK |
| Unknown owner policy | Manual review only. | PASS |
| Symbol-only attribution | Forbidden. | PASS |
| `order_ref` heuristic attribution | Forbidden. | PASS |
| Auto-cancel unknown broker order | Forbidden. | PASS |
| Auto-flatten unknown owner position | Forbidden. | PASS |

## Approved Scope

- Manual canary launch review preparation.
- Review of paper/shadow evidence and sizing checklist.
- Operator readiness review.
- Writing a separate live opt-in launch plan.

## Not Approved Scope

- Start live canary.
- Full live trading.
- Unattended live trading.
- Real broker order/cancel/modify.
- Auto-cancel unknown broker orders.
- Auto-flatten unknown owner positions.
- DLB/TTL/edge-cost tuning.
- Strategy entry/exit logic changes.

## Next Required Gate

`Live Operator Opt-In Drill` if the operator chooses to proceed after manual review.
