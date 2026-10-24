# Canary Sizing and Manual Launch Checklist

This runbook prepares a manual-gated canary launch review. It does not approve live canary, unattended live, full live trading, or real broker mutation by itself.

## Hard Stops

- Do not set `TRADING_TEST_ALLOW_LIVE_IBKR=1` in this checklist.
- Do not place, cancel, or modify real broker orders from this checklist.
- Do not auto-cancel unknown broker orders.
- Do not auto-flatten unknown owner positions.
- Do not use symbol-only attribution.
- Do not use `order_ref` prefix or substring owner attribution.
- Do not tune DLB, TTL, edge-cost, validator gates, execution gates, or strategy entry/exit logic.
- Do not call canary launched until the separate live operator opt-in gate is completed.

## Evidence Required Before Manual Review

| Evidence | Required State |
|---|---|
| Final OMS regression gate | `CANARY_PREP_ALLOWED` |
| Paper/shadow soak gate | `PASS` from bounded runtime artifacts |
| Strategy execution reliability | sample count above one and no silent UOM loss |
| Approved-to-fill conversion | present, with shadow-only skip separated from broker submit failures |
| Vegas DLB/edge measurement | present, with no parameter tuning recommendation from this gate |
| no-live guard | pass |
| services / exec / reconciliation / reports | pass |
| IB reconnect regression | fast suite pass |
| Manual broker order policy | manual-review only for unknown/manual orders |
| Emergency stop | operator-reviewed before any canary opt-in |

## Sizing Review

The sizing review is manual and conservative. This checklist does not change strategy parameters.

| Item | Manual Check |
|---|---|
| Strategy list | Confirm exact canary strategies and permission tiers. |
| Instrument universe | Confirm symbols/contracts match paper/shadow evidence. |
| Quantity cap | Confirm canary max quantity is explicitly lower than normal production. |
| Notional cap | Confirm account-level notional cap and per-strategy notional cap. |
| Loss cap | Confirm daily and per-trade loss stop. |
| Order type | Confirm no unreviewed market-order behavior for options. |
| Close-only fallback | Confirm close/reduce is owner-scoped and permitted when entry is blocked. |
| Unknown owner policy | Confirm unknown/manual orders never auto-cancel or auto-flatten. |
| Kill switch | Confirm operator can stop new entries and close owner-known risk. |

## Manual Launch Review

Before any live canary launch, a human operator must verify:

- IB Gateway state and account identity.
- No unexpected live positions or open orders.
- Broker truth and position safety are fresh and broker-backed.
- Runtime version freshness is current.
- Production state is explainable.
- Strategy surface has no production-ineligible strategy promoted silently.
- Unknown/manual broker order policy is manual-review only.
- Canary sizing and kill switch are understood.
- Launch command and rollback command are written down separately.

## Decision Values

- `CANARY_MANUAL_REVIEW_READY`: the evidence package is ready for human review.
- `CANARY_MANUAL_REVIEW_BLOCKED`: one or more checklist items is missing or failed.

Neither decision starts canary. A live operator opt-in drill remains required before any real broker interaction.
