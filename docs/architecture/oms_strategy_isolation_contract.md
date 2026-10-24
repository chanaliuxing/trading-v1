# OMS Strategy Isolation Contract

This document defines the minimum safety contract for order management, strategy isolation, protective orders, IBKR synchronization, and overnight risk handling. It is intentionally stricter than the current implementation. A system that does not satisfy this contract must not be described as safe for multiple strategies trading the same symbol.

## 1. Non-negotiable safety rule

Broker truth:

- Broker position is `account_id + symbol` aggregate net truth only.
- Broker open order is broker-level truth only.
- Broker snapshot must never directly assign strategy ownership.

Strategy truth:

- Strategy position must be `account_id + strategy_instance_id + symbol + lot_id`.
- Strategy PnL must be derived from fills/ledger, not broker aggregate position.
- Protective orders must bind to `position_owner`, `lot_id`, `bracket_id`, and `oca_group`.

## 2. Required owner identity

Every order, fill, order event, protection order, position lot, quarantine record, broker rebind record, and recovery action must carry or resolve to an immutable owner key:

```text
owner_key =
  account_id
  strategy_id
  strategy_instance_id
  strategy_run_id or run_id
  symbol
  lot_id when position-bearing
  intent_id
  client_order_id
  broker_order_id
  ib_perm_id
  ib_client_id
  ib_connection_epoch
```

Missing owner fields are not a warning-only condition for live entry orders. They are a safety failure.

## 3. Order roles

Order role must be explicit. The OMS must distinguish:

- `ENTRY`
- `EXIT`
- `STOP_LOSS`
- `TAKE_PROFIT`
- `TRAILING_STOP`
- `SCALE_IN`
- `SCALE_OUT`
- `STRATEGY_FLATTEN`
- `ACCOUNT_EMERGENCY_FLATTEN`
- `MANUAL_BROKER_ORDER`
- `RECONCILER_DISCOVERED_ORDER`

`close_only` and `reduce_only` are execution constraints, not sufficient order roles by themselves.

## 4. Forbidden behavior

The following behavior is forbidden in live OMS paths:

- `cancel(symbol)`
- `flatten(symbol)`
- `quarantine(symbol only)`
- `position_by_symbol` as strategy position
- safety plan keyed only by `strategy_id:symbol`
- adopting unknown broker order to a strategy
- using `order_ref` substring match as ownership proof
- allowing a `close_only` or `reduce_only` order to cross zero
- treating opposite-direction same-symbol strategies as safe without an execution arbiter

Exact `order_ref` can be a rebind token only when it is a durable DB owner token, not when ownership is guessed from substring or prefix conventions.

## 5. Rebind precedence

Broker order rebind after reconnect must use this precedence:

1. Durable DB owner record by `ib_perm_id`.
2. Durable DB owner record by `client_order_id`, `intent_id`, or exact `order_ref` token.
3. `broker_order_id` only if protected by `ib_client_id + ib_connection_epoch + DB state`.
4. Otherwise classify as `UNKNOWN_OWNER` and quarantine.

Never use:

- symbol alone
- side alone
- recent timestamp alone
- `order_ref` substring
- strategy_id guessed from config

## 6. Quarantine scope

Quarantine must support these scopes:

- `GLOBAL`
- `ACCOUNT`
- `ACCOUNT_SYMBOL`
- `STRATEGY`
- `STRATEGY_INSTANCE`
- `ORDER_OWNER`
- `BROKER_ORDER`

Symbol-only quarantine is allowed only for explicit `ACCOUNT_SYMBOL` scope, not as the default.

## 7. Acceptance gates

The OMS cannot be considered safe for same-symbol multi-strategy live execution until these gates are green:

- `same_symbol_multi_strategy_safe`
- `strategy_instance_isolation`
- `per_strategy_position_ledger`
- `strategy_a_cannot_cancel_strategy_b`
- `strategy_a_stop_cannot_close_strategy_b`
- `reduce_only_cannot_reverse`
- `reconnect_rebinds_to_strategy_instance`
- `ghost_order_quarantined_not_cancelled`
- `orphan_order_does_not_duplicate_submit`
- `broker_aggregate_position_does_not_overwrite_virtual_positions`
- `intraday_flat_only_closes_owner_position`
- `overnight_carry_owner_resolution_or_quarantine`

## Batch 3 decision notes

Batch 3 lands only the first owner-identity surface:

- `OwnerKey`, `OrderRole`, and `IntentFlags` are first-class execution models.
- `OrderIntent` now carries `account_id`, `strategy_instance_id`, `intent_id`, `client_order_id`, `order_role`, `reduce_only`, `close_only`, `allow_reverse`, `same_symbol_multi_strategy_safe`, `source_event_id`, and `ib_connection_epoch`.
- Live IB adapter ENTRY submission fails closed when `account_id` or `strategy_instance_id` is missing.
- Live ENTRY rejects `close_only` / `reduce_only` semantics; those flags are only valid for exit/reduce paths.
- Live `SELL_SHORT` ENTRY requires an explicit opposite-direction policy token such as `allow_reverse`, `allow_opposite_direction_same_symbol`, `execution_arbiter`, or `virtual_hedge`.
- Order events now carry a minimal `source_event_id` and `ib_connection_epoch` guard surface.
- `QuarantineScope` and a nullable draft migration define the owner-aware quarantine shape; runtime quarantine enforcement is still deferred.

This does not complete same-symbol strategy isolation. It does not add a durable virtual position ledger, broker reconnect rebind, protection order ownership rewrite, owner-scoped quarantine, or same-symbol execution arbiter.

## Batch 4 decision notes

Batch 4 lands owner-scoped quarantine matching for the execution entry gate:

- `quarantine_matches_intent()` applies explicit `QuarantineScope` matching in code after loading candidate quarantine rows.
- `STRATEGY_INSTANCE` quarantine blocks only the matching `account_id + strategy_instance_id`; it does not block another strategy instance trading the same symbol.
- `ACCOUNT_SYMBOL` and `GLOBAL` remain broad blocking scopes.
- Legacy rows without `quarantine_scope` are treated as `ACCOUNT_SYMBOL` when `account_id + symbol` are available. Symbol-only legacy rows are conservative live-mode blocks only and are not ownership proof.
- `close_only` and `reduce_only` cleanup flows continue to bypass the entry quarantine gate.

This does not implement safety plan key isolation, protection order owner binding, durable cancel ownership, broker reconnect rebind, virtual positions, or end-of-day flatten ownership.

## Batch 5 decision notes

Batch 5 lands in-memory safety plan key isolation:

- New safety plans are keyed by `account_id + strategy_instance_id + symbol + lot_id`.
- `strategy_id` remains on the plan for reporting and compatibility, but it is not sufficient identity for plan storage.
- The transitional legacy path may synthesize an unsafe legacy instance only for old non-live callers; live ENTRY owner validation must still reject missing `account_id` or `strategy_instance_id`.
- The default lot id is owner-scoped as `lot:{account_id}:{strategy_instance_id}:{symbol}`.
- OCO protection continues to use the existing lifecycle, but its default `position_lot_id` now comes from the owner-scoped plan lot instead of `strategy_id:symbol`.

This does not implement durable cancel ownership, full protection order owner binding, broker reconnect rebind, virtual position arbitration, broker aggregate reconciliation, manual/eod flatten ownership, or overnight carry resolution.

## Batch 6 decision notes

Batch 6 lands durable cancel ownership for execution pre-cancel paths:

- Cancel selection is based on a durable `core_order` owner row plus broker open-order verification.
- `order_ref` prefix and substring matching are not owner proof.
- Exact `order_ref` equality is only a broker row verification check when the durable DB row already proves ownership.
- Broker-only unknown orders are not cancelled by this path; they require owner rebind, quarantine, or manual adjudication.
- Strategy instance ownership is matched through durable row fields or owner metadata in persisted order notes.

This does not implement stop/TP owner-position binding, broker reconnect rebind, virtual position arbitration, broker aggregate reconciliation, manual/eod flatten ownership, or overnight carry resolution.

## Batch 7 decision notes

Batch 7 lands stop-loss owner-position binding:

- Stop-loss fills require `account_id`, `strategy_id`, `strategy_instance_id`, `symbol`, `lot_id`, and `order_role=STOP_LOSS` before position mutation.
- Stop-loss fills write to the owner-scoped position ledger lane and do not fall back to `strategy_id + symbol` legacy `core_position` mutation.
- Missing stop-loss owner identity fails closed before position mutation; no symbol-level or broker-aggregate reduction is allowed.
- Protection registry and position ledger DDL now include nullable owner fields such as `strategy_instance_id`, `position_owner_key`, `bracket_id`, and `order_role`.
- OCO child stop notes persist owner metadata so later fills can recover the owning strategy instance and lot.

This does not implement IBKR reconnect rebind, full take-profit owner binding, virtual position arbitration, broker aggregate reconciliation, manual/eod flatten ownership, overnight carry resolution, or manual/unknown broker order adjudication.

## Batch 8 decision notes

Batch 8 lands durable reconnect rebind classification:

- Broker open orders can bind to a strategy instance only through durable owner rows.
- Rebind precedence is exact `ib_perm_id + ib_client_id + account_id`, then exact `client_order_id` / `intent_id`, then exact `broker_order_id` protected by `ib_client_id + ib_connection_epoch`, then exact `order_ref` only when the durable row also contains account, strategy instance, and symbol ownership.
- `order_ref` prefix and substring matching remain forbidden.
- Symbol, side, timestamp, and active strategy config are not ownership proof.
- Unknown, ambiguous, and stale-epoch broker orders remain non-mutating classifications for quarantine/manual review; this batch does not auto-cancel or assign them.
- `core.core_order` has nullable owner/rebind fields and indexes for `strategy_instance_id`, client/intent ids, `ib_client_id`, and epoch-backed lookup.

This does not implement broker aggregate reconciliation, owner-scoped intraday flatten, overnight carry resolution, or full manual/unknown broker order adjudication.

## Batch 9 decision notes

Batch 9 lands the broker aggregate vs strategy virtual position boundary:

- Broker positions are `account_id + symbol` aggregate truth only.
- Strategy virtual positions are `account_id + strategy_instance_id + symbol + lot_id` owner truth from the position ledger.
- `BrokerAccountPosition`, `StrategyVirtualPosition`, and `PositionReconciliationResult` model the read-only comparison.
- `reconcile_broker_aggregate_to_virtual_positions()` validates broker quantity against the sum of virtual owner lanes and always returns `must_not_mutate_virtual_positions=true`.
- A broker/virtual mismatch produces `MISMATCH`, `UNKNOWN_OWNER`, or `UNKNOWN_BROKER_TRUTH` for audit/manual review; it does not write strategy lots.
- Position safety no longer clears `core.core_position` residual rows from broker aggregate evidence. It reports `manual_review_required` with `broker_aggregate_must_not_overwrite_virtual_position`.
- Runtime position sync from IB is disabled as a mutation path; direct broker aggregate upsert/zeroing into `core.core_position` is not allowed.

This does not implement owner-scoped intraday flatten, overnight carry owner resolution, or full manual/unknown broker order adjudication.

## Batch 10 decision notes

Batch 10 lands owner-scoped strategy flatten:

- Strategy flatten targets are loaded from `core.position_ledger_current`, not legacy `strategy_id + symbol` `core_position` rows.
- Strategy flatten requires `account_id`, `strategy_instance_id`, `symbol`, and `lot_id` before a close order is built.
- Strategy flatten emits `order_role=STRATEGY_FLATTEN`, `flatten_scope=STRATEGY_INSTANCE`, `close_only=true`, and `reduce_only=true`.
- Strategy flatten quantity comes from the owner ledger lane. Broker aggregate quantity is not used for strategy-level flatten.
- Account emergency flatten is explicitly separate: it emits `order_role=ACCOUNT_EMERGENCY_FLATTEN`, `flatten_scope=ACCOUNT_EMERGENCY`, and may use broker aggregate quantity only under that emergency scope.
- A missing owner identity fails closed instead of routing to an account-wide flatten.

This does not implement overnight carry owner resolution or full manual/unknown broker order adjudication.

## Batch 11 decision notes

Batch 11 lands overnight carry detection after restart:

- Position safety emits an `overnight_carry` artifact section when a no-overnight owner lane or unknown broker residual survives restart.
- Owner-resolved carry incidents include `account_id`, `strategy_id`, `strategy_instance_id`, `symbol`, `lot_id`, and `position_owner_key`.
- Unknown broker aggregate positions are classified as `UNKNOWN_OWNER` and require manual review; they are not assigned to active strategies by symbol.
- Broker/virtual mismatches become manual-review incidents and do not overwrite virtual position lanes.
- Overnight carry blocks new ENTRY for the affected owner or safe scope while preserving close-only/reduce-only recovery.
- Detection is non-mutating: it does not auto-flatten the account and does not use broker aggregate position as strategy owner proof.

This does not implement full manual/unknown broker order adjudication.

## Batch 12 decision notes

Batch 12 lands manual/unknown broker order adjudication:

- Broker orders without a durable owner row are classified as `UNKNOWN_OWNER`, `MANUAL_BROKER_ORDER`, `FOREIGN_CLIENT_ORDER`, or `AMBIGUOUS_OWNER`; all require manual review.
- Only exact durable owner evidence may bind an order to a strategy instance: `ib_perm_id + ib_client_id + account_id`, `client_order_id` / `intent_id`, epoch-protected broker order identity, or exact `order_ref` plus complete owner row.
- Symbol-only, side-only, timestamp-only, active-strategy, and `order_ref` prefix/substring matching are not owner proof.
- Non-bound broker orders explicitly disallow auto-cancel, strategy attribution, PnL attribution, and virtual-position mutation.
- Unknown broker fills remain quarantined/manual-required in fill attribution; they are not eligible for ledger or PnL replay until adjudicated.

This completes the Batch 2 red-line OMS ownership contract tests. Remaining work is hardening and end-to-end regression gating, not closing known red contract tests.
