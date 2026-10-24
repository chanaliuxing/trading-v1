# Canary Sizing and Manual Launch Checklist - 2026-05-12

## Decision

`CANARY_MANUAL_LAUNCH_CHECKLIST_READY`

This checklist is ready for manual canary launch review only. It does not start canary, approve unattended live, approve full live, or permit real broker mutation.

## Evidence

| Gate | Status | Evidence |
|---|---|---|
| Paper/shadow bounded runtime | PASS | `SHADOW_ONLY=20`, `PAPER_ONLY=20` |
| Strategy execution reliability | PASS | `sample_count=40`, `primary_bottleneck=NO_BOTTLENECK` |
| UOM evidence | PASS | `delegated=20`, `skipped=20` |
| Paper broker submit/ack/fill/PnL | PASS | submit `20`, ack `20`, fill `20`, attribution `20`, PnL replay `20` |
| Approved-to-fill | PASS | `sample_count=40`, `primary_conversion_loss=NO_BOTTLENECK`, broker submit drops `0` |
| Vegas DLB/edge | PASS | DLB breach `0`, stale decision `0`, edge insufficient `0`, no parameter calibration signal |
| Soak gate | PASS | blockers `[]`, warnings `[]` |
| no-live guard | PASS | `4 passed` |
| services | PASS | `656 passed` |
| exec | PASS | `293 passed` |
| reconciliation | PASS | `76 passed` |
| reports | PASS | `61 passed` |
| IB reconnect fast | PASS | `32 passed` |

## Canary Scope

| Field | Value / Required Action |
|---|---|
| `strategy_id` | REQUIRED_MANUAL_INPUT |
| `strategy_instance_id` | REQUIRED_MANUAL_INPUT |
| `symbol` | REQUIRED_MANUAL_INPUT |
| `account_id` | REQUIRED_LIVE_OPT_IN_CHECK |
| `permission_tier` | REQUIRED_MANUAL_REVIEW |
| `max_contracts_or_shares` | REQUIRED_MANUAL_INPUT |
| `max_notional` | REQUIRED_MANUAL_INPUT |
| `max_daily_loss` | REQUIRED_MANUAL_INPUT |
| `max_open_orders` | REQUIRED_MANUAL_INPUT |
| `max_trades_per_day` | REQUIRED_MANUAL_INPUT |
| `allowed_session` | REQUIRED_MANUAL_INPUT |
| `close_only_fallback` | owner-scoped only |
| `kill_switch` | REQUIRED_MANUAL_CONFIRMATION |
| `manual_operator` | REQUIRED_MANUAL_INPUT |
| `rollback_condition` | REQUIRED_MANUAL_INPUT |

## Manual Approvals

| Approval | Status |
|---|---|
| Operator confirms IB Gateway state | REQUIRED_LIVE_OPT_IN_CHECK |
| Operator confirms no unexpected positions/open orders | REQUIRED_LIVE_OPT_IN_CHECK |
| Operator confirms broker truth fresh | REQUIRED_LIVE_OPT_IN_CHECK |
| Operator confirms position safety fresh | REQUIRED_LIVE_OPT_IN_CHECK |
| Operator confirms runtime freshness | REQUIRED_LIVE_OPT_IN_CHECK |
| Operator confirms strategy surface tier | REQUIRED_MANUAL_REVIEW |
| Operator confirms paper/shadow soak PASS | PASS |
| Operator confirms unknown broker policy | PASS |
| Operator confirms emergency stop | REQUIRED_MANUAL_CONFIRMATION |

## Approved Scope

- Manual canary launch review.
- Review paper/shadow evidence.
- Prepare separate live operator opt-in drill.

## Not Approved Scope

- Start canary from this checklist.
- Full live trading.
- Unattended live trading.
- Real broker order/cancel/modify.
- Auto-cancel unknown broker orders.
- Auto-flatten unknown owner positions.
- Symbol-only attribution.
- `order_ref` prefix/substr owner attribution.
- DLB/TTL/edge-cost tuning.
- Strategy entry/exit logic changes.
