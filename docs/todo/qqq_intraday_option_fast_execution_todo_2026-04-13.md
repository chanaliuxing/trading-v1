# QQQ Intraday Option Fast Execution TODO (2026-04-13)

## Goal

Align `qqq_intraday_option_v1_001` with the trader execution model:

- stock signal from `qqq_intraday_001` is the only entry authority
- option strategy should immediately fetch the fastest available option chain
- select a contract and submit once
- if not filled quickly, cancel and optionally retry once
- if the moment is missed, give up instead of reusing stale stock signals

## Current Gaps

1. Option entry still depends on a minute-bar callback path.
2. Stock signal artifacts are reusable for too long relative to the desired execution model.
3. Contract-selection failures are not observable enough in downstream logs.
4. Entry quote selection currently fails closed if live-state is unavailable unless fallback is enabled.

## P0 Landed In This Round

- Enable entry quote ClickHouse fallback for `qqq_intraday_option_v1_001`.
- Enable fast entry timeout cancel:
  - `entry_timeout_sec = 5.0`
  - `entry_retry_enabled = true`
  - `entry_retry_max_attempts = 2`
  - `entry_retry_wait_sec = 1.0`
- Enrich `no_contract_selection` observability with the contract-policy reject reason.

## P1 Immediate Next

1. Break the option entry path out of the minute-bar timing model.
2. Trigger option execution directly from the stock signal publication path or an equivalent sub-second consumer path.
3. Reduce stock signal validity from the current long-lived cache semantics to a trader-valid execution window.
4. Prevent repeated reuse of the same stock trigger after the first option entry attempt finishes or expires.

## P2 Observability

Add end-to-end timing fields:

- `source_trigger_ts`
- `option_trigger_received_ts`
- `quote_query_start_ts`
- `quote_query_end_ts`
- `contract_selected_ts`
- `order_submit_ts`
- `broker_ack_ts`
- `first_fill_ts`
- `cancel_ts`

Derived metrics:

- `signal_to_submit_ms`
- `submit_to_fill_ms`
- `submit_to_cancel_ms`
- `quote_age_ms`

## P3 Execution Policy Review

1. Confirm final stock-signal execution window target after real runtime measurement.
2. Confirm whether one retry is acceptable or whether a single-shot policy is preferred.
3. Confirm whether option exits should stay underlying-driven or add premium-based rules.

## Acceptance

1. Stock signal arrival can trigger option submission inside a trader-valid short window.
2. Contract-selection failure reason is visible in logs.
3. Entry orders are cancelled after `5s` if still unfilled.
4. Retry behavior is bounded and observable.
5. Same stock trigger is not reused indefinitely by later minute callbacks.
