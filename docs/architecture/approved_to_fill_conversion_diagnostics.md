# Approved-to-Fill Conversion Diagnostics

## Purpose

An approved strategy decision is not evidence of a fill. The production
diagnostic chain must explain whether an approved signal became an intent,
passed reservation, passed execution gates, reached broker submit, received
broker ack, filled, was attributed to an owner, and became PnL replay-ready.

This report is offline evidence only. It does not submit orders, relax validators,
change execution gates, or tune strategy parameters.

## Chain

`signal -> approved -> submit_intent -> reservation -> execution_accept -> broker_submit -> broker_ack -> fill -> attribution -> pnl_replay`

Each chain record includes owner identity, correlation IDs, timestamps, latency,
fill quantity, attribution status, and PnL replay readiness.

## Correlation Priority

1. `intent_id` / `client_order_id`
2. `trace_id + decision_id`
3. `broker_order_id + durable owner row`
4. `ib_perm_id + durable owner row`
5. never symbol-only

If only `symbol` matches, records are not correlated. Unknown broker or fill
events remain unknown until durable owner evidence exists.

## Drop Stages

- `post_approval`: approved decision did not produce a submit intent.
- `reservation`: intent did not pass reservation.
- `execution_gate`: reservation passed but execution did not accept.
- `broker_submit`: execution accepted but no broker submit.
- `broker_ack`: broker submit without ack.
- `fill`: ack without fill or partial/low fill.
- `attribution`: fill exists but owner attribution or PnL replay is incomplete.

## Future Fix Gate

Do not tune strategy parameters until the conversion loss stage is known.
Do not increase signal frequency until reservation/execution/broker bottlenecks
are known. Do not loosen validators until rejection distributions are measured.
Do not treat broker-submitted as filled. Do not treat filled as attributed unless
owner identity is complete. Do not treat attributed as PnL replay-ready unless
fill, commission, order role, and lot owner are complete.
