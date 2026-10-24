# Close-Only Safety Architecture (QQQ / IBKR)

## Goal

Prevent three failure classes at the same time:

1. Under-closing caused by DB lag (`intent qty > submitted qty`).
2. Cross-strategy accidental liquidation caused by account-level broker position bleed.
3. Ghost-order loops caused by mismatched strategy/broker/state views.

## Current Safety Contract

For non-option `close_only` orders:

- `flatten_action` / `protective_stop` must be strategy-scoped first.
- Runtime `position_qty_hint` (from strategy) is preferred over DB position.
- Broker position is used only as a cap for the hint (never as the primary non-manual source).
- If hint and broker sign mismatch, fail-closed (`qty_live=0`).
- If no hint for non-manual close-only action, fail-closed to avoid cross-strategy bleed.
- Manual intervention flatten keeps broker-first semantics.

For option symbols:

- Keep strategy-ledger path (`OPT:` internal symbol isolation) and `position_qty_hint` shortcut.

## Required Envelope Fields (QQQ Intraday)

`flatten_action` / `protective_stop` intents now include:

- `close_only=true`
- `position_qty_hint` (signed strategy runtime qty)
- `position_qty_hint_source="strategy_runtime"`

## Execution Path (Summary)

1. Strategy submits close-only intent with reason + hint.
2. Execution `close_only` guard resolves live qty with:
   - hint (preferred) -> broker cap -> safety clamp
   - otherwise strategy DB
   - otherwise fail-closed for non-manual flatten/protective-stop
3. Order submits only with clamped safe qty.
4. Rejections are emitted as `order_rejected` with explicit reason.

## Operational Acceptance Checklist

1. `intent_received.target_qty` and `order_submitted.qty` should match unless explicit clamp.
2. Any clamp must show `close_only_qty_clamped` with `qty_req`, `qty_live`, `qty_safe`.
3. No repeated `close_only_no_long_position` while broker still shows long qty for same strategy action.
4. No non-manual close-only order should pass with strategy flat and no hint.
5. `audit_self_heal` protective-stop events should not spam mobile notifications.

## Residual Risk

- If strategy emits wrong `position_qty_hint`, execution may still clamp incorrectly (capped by broker, but not strategy-ownership aware at broker layer).
- Multi-strategy same-symbol trading remains an architectural high-risk pattern; enforce symbol ownership policy per strategy for production.
