# Option Replay Audit Schema

This document defines the current audit outputs emitted by the isolated option replay engine under `./services/option_replay`.

The goal is stability for downstream analysis, not a frozen public API. New columns may be added, but existing semantics should not silently change.

## Files

The replay CLI currently emits:

- `summary.json`
- `trades.csv`
- `skips.csv`
- `entry_audit.csv`
- `selection_audit.csv`
- `selection_candidates.csv`
- `bracket_audit.csv`
- `orders.csv`
- `fills.csv`
- `cash_ledger.csv`
- `equity_curve.csv`

## `orders.csv`

Represents the order lifecycle as the simulator sees it.

Core columns:

- `ts`: event timestamp in ISO format
- `order_id`: unique order identifier
- `status`: one of `CREATED`, `WORKING`, `PARTIAL_FILLED`, `FILLED`, `REPLACED`, `CANCELLED`, `EXPIRED`

Common optional columns:

- `parent_order_id`: links child orders to the filled entry order
- `order_type`: entry style such as `ADAPTIVE_NORMAL` or `ADAPTIVE_URGENT`
- `order_role`: child role such as `STOP_CHILD`, `TAKE_PROFIT_CHILD`, `FLATTEN_CHILD`
- `side`: `CALL` or `PUT`
- `grade`: strategy signal grade
- `expiry`
- `strike`
- `qty`
- `remaining_qty`
- `limit_price`
- `trigger_underlying`
- `attempt`
- `quote_ask`
- `chase_limit`
- `cancel_reason`
- `exit_reason`

## `fills.csv`

Represents actual fills applied to accounting.

Core columns:

- `ts`
- `fill_id`
- `order_id`
- `fill_price`
- `qty`

Common optional columns:

- `side`
- `expiry`
- `strike`
- `fill_reason`

Notes:

- A partially filled order may generate multiple fill rows over time.
- `fills.csv` is the source of truth for economic fills; `orders.csv` is the lifecycle trace.

## `cash_ledger.csv`

Represents realized cash movements.

Core columns:

- `ts`
- `event`: `ENTRY_FILL` or `EXIT_FILL`
- `amount`: signed cash movement after commissions
- `fees`
- `cash_after`
- `order_id`
- `side`
- `qty`
- `option_price`

Notes:

- `cash_ledger.csv` is the source of truth for cash accounting.
- `trades.csv` remains the higher-level closed-trade summary.

## `entry_audit.csv`

Represents trigger-to-entry behavior.

Core columns:

- `ts`
- `status`

Current statuses:

- `created`
- `missed`
- `partial_filled`
- `filled`
- `timeout`
- `quote_missing`

Common optional columns:

- `order_id`
- `side`
- `grade`
- `regime`
- `expiry`
- `strike`
- `attempt`
- `fill_qty`
- `remaining_qty`
- `limit_price`
- `chase_limit`
- `fill_price`
- `ask`
- `bid`
- `delta`
- `dte`

## `selection_audit.csv`

Represents option contract selection at signal time.

Core columns:

- `ts`
- `side`
- `grade`
- `regime`
- `right`
- `spot`
- `selected`
- `rejection_reason`
- `candidate_count`
- `top_candidates`

Selection columns when a contract is found:

- `selected_expiry`
- `selected_strike`
- `selected_dte`
- `selected_delta`

Notes:

- `top_candidates` is currently serialized as a list-like payload in the CSV cell.
- This file is meant for calibration and debugging, not as a normalized warehouse table.

## `selection_candidates.csv`

Represents a normalized expansion of `selection_audit.top_candidates`.

Core columns:

- `selection_id`
- `ts`
- `side`
- `grade`
- `regime`
- `right`
- `spot`
- `selected`
- `rejection_reason`
- `candidate_rank`

Candidate columns:

- `candidate_expiry`
- `candidate_strike`
- `candidate_dte`
- `candidate_delta`
- `candidate_bid`
- `candidate_ask`
- `candidate_mid`
- `candidate_spread_pct`
- `candidate_open_interest`

Notes:

- `selection_candidates.csv` is the normalized analysis table.
- `selection_audit.csv` remains the raw strategy-time snapshot.

## `bracket_audit.csv`

Represents the child-order lifecycle for bracket behavior.

Core columns:

- `ts`
- `event`

Current events:

- `CHILDREN_CREATED`
- `CHILD_TRIGGERED`
- `CHILD_PARTIAL_TRIGGERED`
- `CHILD_QTY_SYNC`
- `FLATTEN_TRIGGERED`

Common optional columns:

- `parent_order_id`
- `stop_order_id`
- `tp_order_id`
- `fill_order_id`
- `cancelled_order_id`
- `reason`
- `qty`
- `filled_qty`
- `remaining_qty`

Semantics:

- `CHILDREN_CREATED`: stop/take-profit child orders become active after the parent entry fill.
- `CHILD_TRIGGERED`: one child order exits the remaining position and the peer is cancelled.
- `CHILD_PARTIAL_TRIGGERED`: partial take-profit fired but the position remains open.
- `CHILD_QTY_SYNC`: remaining child order quantities were updated after a partial exit or partial entry accumulation.
- `FLATTEN_TRIGGERED`: an explicit flatten path was used instead of the standing children.

## Stability Rules

- New columns may be appended.
- Existing column names should not be repurposed.
- Existing status and event values should not change meaning silently.
- If a semantic change is required, add a new value rather than overloading an old one.
