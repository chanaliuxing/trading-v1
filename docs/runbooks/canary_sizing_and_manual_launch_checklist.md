# Canary Sizing and Manual Launch Checklist

This checklist is a manual review package only. It does not start canary, approve unattended live trading, approve full live trading, or permit real broker mutation.

## Canary Scope Template

| Field | Required Manual Value |
|---|---|
| `strategy_id` | Operator must confirm exact strategy ID. |
| `strategy_instance_id` | Operator must confirm exact instance ID. |
| `symbol` | Operator must confirm exact symbol/contract universe. |
| `account_id` | Operator must confirm account before live opt-in. |
| `permission_tier` | Must be reviewed from strategy surface; no silent promotion. |
| `max_contracts_or_shares` | Conservative operator-entered cap required. |
| `max_notional` | Conservative operator-entered cap required. |
| `max_daily_loss` | Operator-entered hard stop required. |
| `max_open_orders` | Operator-entered cap required. |
| `max_trades_per_day` | Operator-entered cap required. |
| `allowed_session` | Operator confirms allowed market/session window. |
| `close_only_fallback` | Must remain owner-scoped. |
| `kill_switch` | Operator confirms entry stop and emergency stop. |
| `manual_operator` | Named operator required before live opt-in. |
| `rollback_condition` | Operator documents explicit rollback/stop criteria. |

## Manual Approvals Required

- Operator confirms IB Gateway state.
- Operator confirms no unexpected positions/open orders.
- Operator confirms broker truth fresh.
- Operator confirms position safety fresh.
- Operator confirms runtime freshness.
- Operator confirms strategy surface tier.
- Operator confirms paper/shadow soak `PASS`.
- Operator confirms unknown broker order policy.
- Operator confirms emergency stop.

## Hard Stops

- Do not set `TRADING_TEST_ALLOW_LIVE_IBKR=1` in this checklist.
- Do not place, cancel, or modify real broker orders from this checklist.
- Do not auto-cancel unknown broker orders.
- Do not auto-flatten unknown owner positions.
- Do not use symbol-only attribution.
- Do not use `order_ref` prefix or substring owner attribution.
- Do not tune DLB, TTL, edge-cost, validator gates, execution gates, or strategy entry/exit logic.
- Do not call canary launched until a separate live operator opt-in gate is completed.

## Decision Values

- `CANARY_MANUAL_LAUNCH_CHECKLIST_READY`
- `CANARY_BLOCKED_BY_EVIDENCE_GAP`
