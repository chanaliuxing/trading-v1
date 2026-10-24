# Manual Canary Review - 2026-05-12

## Decision

`MANUAL_REVIEW_PACKAGE_READY`

This package is ready for human review before a separate live operator opt-in drill. It does not start canary, approve full live trading, approve unattended live trading, or permit real broker order/cancel/modify.

## Evidence Summary

| Evidence | Status | Detail |
|---|---|---|
| Final landing decision | PASS | `READY_FOR_MANUAL_CANARY_REVIEW` |
| Soak gate | PASS | blockers `[]`, warnings `[]` |
| `SHADOW_ONLY` samples | PASS | `20` |
| `PAPER_ONLY` samples | PASS | `20` |
| Strategy execution reliability | PASS | `NO_BOTTLENECK`; UOM delegated `20`, skipped `20`; paper submit/ack/fill/attribution/PnL each `20` |
| Approved-to-fill | PASS | `NO_BOTTLENECK`; broker submit drops `0` |
| Vegas DLB/edge | PASS | `NO_BOTTLENECK`; DLB breach `0`; stale decision `0`; edge insufficient `0` |
| Canary checklist | PASS | generated |
| no-live guard | PASS | `4 passed` |
| services | PASS | `656 passed` |
| exec | PASS | `293 passed` |
| reconciliation | PASS | `76 passed` |
| reports | PASS | `61 passed` |
| IB reconnect fast | PASS | `32 passed` |

## Canary Sizing Fields Requiring Human Confirmation

| Field | Current State | Required Human Action |
|---|---|---|
| `strategy_id` | `REQUIRED_MANUAL_INPUT` | Select exact canary strategy. |
| `strategy_instance_id` | `REQUIRED_MANUAL_INPUT` | Select exact strategy instance. |
| `symbol` | `REQUIRED_MANUAL_INPUT` | Confirm symbol or option contract universe. |
| `account_id` | `REQUIRED_LIVE_OPT_IN_CHECK` | Confirm only in separate live operator opt-in drill. |
| `permission_tier` | `REQUIRED_MANUAL_REVIEW` | Confirm strategy surface tier and no silent production promotion. |
| `max_contracts_or_shares` | `REQUIRED_MANUAL_INPUT` | Set conservative per-order and total position cap. |
| `max_notional` | `REQUIRED_MANUAL_INPUT` | Set account and strategy notional cap. |
| `max_daily_loss` | `REQUIRED_MANUAL_INPUT` | Set hard daily stop. |
| `max_open_orders` | `REQUIRED_MANUAL_INPUT` | Set max simultaneous open orders. |
| `max_trades_per_day` | `REQUIRED_MANUAL_INPUT` | Set max daily canary trade count. |
| `allowed_session` | `REQUIRED_MANUAL_INPUT` | Confirm permitted session/window. |
| `close_only_fallback` | owner-scoped only | Confirm close/reduce remains owner-scoped. |
| `kill_switch` | `REQUIRED_MANUAL_CONFIRMATION` | Confirm entry stop and emergency stop path. |
| `manual_operator` | `REQUIRED_MANUAL_INPUT` | Assign named operator. |
| `rollback_condition` | `REQUIRED_MANUAL_INPUT` | Define explicit stop/rollback triggers. |

## Operator Must Confirm

| Item | Required Before Live Opt-In? |
|---|---|
| IB Gateway state | yes |
| No unexpected live positions/open orders | yes |
| Broker truth fresh and broker-backed | yes |
| Position safety fresh and go | yes |
| Runtime freshness current | yes |
| Strategy surface tier reviewed | yes |
| Paper/shadow soak `PASS` reviewed | yes |
| Unknown/manual broker order policy remains manual-review only | yes |
| Emergency stop and kill switch understood | yes |
| Canary sizing caps entered and reviewed | yes |

## Launch Blocker Checklist

| Blocker | Required State |
|---|---|
| no-live guard | must remain green for review package |
| unexpected live broker state | blocks live opt-in |
| broker truth missing/stale/not broker-backed | blocks live opt-in |
| position safety stale/no-go/lookup failed | blocks live opt-in |
| runtime stale | blocks live opt-in |
| active quarantine/manual recovery required | blocks live opt-in |
| unknown broker order auto-cancel enabled | blocks live opt-in |
| unknown owner auto-flatten enabled | blocks live opt-in |
| symbol-only attribution | blocks live opt-in |
| `order_ref` prefix/substr attribution | blocks live opt-in |
| missing canary sizing caps | blocks live opt-in |
| missing kill switch confirmation | blocks live opt-in |

## Go/No-Go Manual Review Table

| Gate | Current Status | Manual Review Result |
|---|---|---|
| Runtime evidence | GO_FOR_REVIEW | Requires human acceptance. |
| Soak gate | GO_FOR_REVIEW | Requires human acceptance. |
| OMS ownership contracts | GO_FOR_REVIEW | Requires human acceptance. |
| Diagnostics | GO_FOR_REVIEW | Requires human acceptance. |
| Canary sizing | NO_GO_UNTIL_FILLED | Human must fill sizing fields. |
| Operator live state | NO_GO_UNTIL_SEPARATE_OPT_IN | Must be checked in live operator drill. |
| Emergency controls | NO_GO_UNTIL_CONFIRMED | Human must confirm. |
| Final canary launch | NOT_APPROVED_BY_THIS_PACKAGE | Separate opt-in drill required. |

## Separate Live Operator Opt-In Drill

This package may proceed to a separate live operator opt-in drill plan. It may not start that drill automatically, set `TRADING_TEST_ALLOW_LIVE_IBKR=1`, connect to IBKR, or place/cancel/modify orders.

## Approved Scope

- Manual review of the canary evidence package.
- Manual completion of canary sizing fields.
- Preparation of a separate live operator opt-in drill.

## Not Approved Scope

- Starting canary from this package.
- Live trading approval from this package.
- Full live trading.
- Unattended live trading.
- Real broker order/cancel/modify.
- Auto-cancel unknown broker orders.
- Auto-flatten unknown owner positions.
