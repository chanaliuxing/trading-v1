# Protection Engine Native Trail TODO - 2026-05-14

## Current Landing Status

Merged into `main` via merge commit `394a63c15`.

Implemented:

- `OrderStyle` now supports `TRAIL` and `TRAIL_LIMIT` with `trail_amount`, `trail_percent`, `trail_stop_px`, `lmt_price_offset`, `protective_stop`, `close_only`, `reduce_only`, `role`, `tif`, and `outside_rth`.
- `validate_order_style()` rejects invalid trailing orders before translation.
- `ExecPlan` carries native trailing fields through the decision pipeline.
- `IntentTranslator` preserves forced `TRAIL` / `TRAIL_LIMIT` order styles and rounds `trail_stop_px` / `lmt_price_offset` to tick.
- `OrderType` and `PlacedOrder` understand `TRAIL` / `TRAIL_LIMIT` and persist trail metadata in memory objects.
- `SafetyKernel.place_order()` routes `TRAIL` / `TRAIL_LIMIT` to the broker adapter.
- `SafetyKernel.place_order()` applies protective close-only checks when `position_qty_hint` is present:
  - protective action must match position direction;
  - protective qty cannot exceed current position qty;
  - long protective stop cannot move down unless manual/emergency;
  - short protective stop cannot move up unless manual/emergency.
- `IBInsyncAdapter` can place native IBKR `TRAIL` and `TRAIL LIMIT` orders.
- `IBInsyncAdapter` has helpers for:
  - `modify_stop_order()`;
  - `replace_protective_order_atomicish()`;
  - `emergency_exit()`.
- `ProtectionSpec`, `ProtectiveOrderState`, and `ProtectionEngine` exist as the core protection abstraction.
- `ProtectionEngine` includes `TRAIL_LIMIT` trigger/timeout fallback predicates.
- `ReplicaEntryCore` can build native trailing protective intents.
- `IndexSuperWinReplicaStrategy` submits native runner `TRAIL` / `TRAIL_LIMIT` after TP1 confirmation when enabled.
- TP1 runner trail submission carries `replace_protective_order_id` so execution can replace the old fixed stop via atomicish replacement.
- `SafetyKernel.replace_protective_order_atomicish()` now delegates to the adapter replacement path and validates close-only/reduce-only protection invariants.
- Same-type protective `STP -> STP` changes now use adapter `modify_stop_order()` instead of cancel/replace.
- ExecutionService routes protective intents with `replace_protective_order_id` through SafetyKernel replacement instead of plain placement.
- Protection audit recognizes live `STP`, `STP_LMT`, `TRAIL`, and `TRAIL_LIMIT` broker orders.
- `SafetyKernel` inspection loop now includes a `TRAIL_LIMIT` watchdog that escalates triggered, unfilled trail-limit protection to emergency `MKT` or marketable `LMT`.
- `SafetyKernel` inspection loop now reconciles active protective families:
  - cancels stale duplicate protection in the same family;
  - cancels residual protection when the visible symbol position is flat;
  - resizes protection when an explicit family target qty is supplied;
  - shrinks protection when local protection qty exceeds visible broker symbol qty.
- TP1 runner native trail now starts from a `ProtectionSpec` at the strategy boundary and carries `protection_spec_mode`, `protection_position_id`, and `runner_qty` through envelope extras.
- Daily trade summaries and `inspect_ghost_orders.py` expose native trailing protection details from order notes.
- Unit coverage added for translator, SafetyKernel routing, IBAdapter order construction, ProtectionEngine invariants, and replica trailing intent creation.

Verified:

```bash
./venv_ml/bin/python -m pytest \
  tests/unit/services/test_daily_trade_summary_service.py \
  tests/unit/exec/test_safety_kernel_protection_family_reconcile.py \
  tests/unit/exec/test_safety_kernel_protective_replacement.py \
  tests/unit/exec/test_safety_kernel_trail_limit_watchdog.py \
  tests/unit/exec/test_execution_service_runtime_identity.py \
  tests/unit/strategies/test_index_super_win_replica_core.py \
  tests/unit/exec/test_protection_engine_trailing_orders.py \
  tests/unit/services/exec/test_ib_adapter_open_orders.py \
  tests/unit/strategies/test_index_super_win_replica_strategy.py::test_replica_strategy_protection_audit_marks_stop_live_from_broker_orders \
  tests/unit/strategies/test_index_super_win_replica_strategy.py::test_replica_strategy_protection_audit_marks_native_trail_live_from_broker_orders \
  tests/unit/strategies/test_index_super_win_replica_strategy.py::test_replica_strategy_tp1_fill_submits_native_runner_trail_replacing_fixed_stop -q
```

Result: `74 passed`.

## Not Fully Landed Yet

The native order capability is in the main execution path, but the complete protection-state-machine workflow is not fully wired into live strategy behavior yet.

Remaining gaps:

- `TRAIL_LIMIT` watchdog is wired in SafetyKernel, but live quote availability depends on broker adapter quote/snapshot provider or supplied market snapshots.
- Emergency fallback is wired for trail-limit timeout, but not yet for every failed replacement recovery path.
- Persistent DB schema is not expanded with dedicated trail columns; trail metadata remains in order objects and `note.order_style` by design for this landing.
- Full paper soak has not been executed in this coding turn.

## TODO

### P0 - Wire Native Trail Into Runner Protection

- Add config flags:
  - `protection_engine_enabled`;
  - `runner_native_trail_enabled`;
  - `runner_trail_mode`: `TRAIL` / `TRAIL_LIMIT` / `RATCHET_STP`;
  - `runner_trail_basis`: `ATR` / `PERCENT` / `ABS`;
  - `runner_trail_atr_mult`;
  - `runner_trail_percent_min`;
  - `runner_trail_percent_max`;
  - `trail_limit_enabled`;
  - `trail_limit_offset_bps`;
  - `trail_limit_unfilled_timeout_sec`;
  - `trail_limit_fallback`: `MKT` / `MARKETABLE_LMT`.
- [x] After TP1 fill confirmation, submit a native trail intent for the remaining runner qty.
- [x] Submit native runner trail through the existing `DecisionEnvelope -> translator -> SafetyKernel -> IBAdapter` path.
- [x] Mark `state.extras["native_trail_active"] = True` only after accepted broker/order update confirmation.
- [x] Replace the current direct intent build with a full `ProtectionSpec(mode=NATIVE_TRAIL)` object at strategy boundary.
- Keep software trailing as audit/fallback only while native runner trail is active.

Acceptance:

- [x] TP1 fill produces a native `TRAIL` protective order for runner qty.
- [x] Existing fixed `STP` is not cancelled until the native trail order is accepted.
- If native trail submission fails, old `STP` remains live or is rebuilt.

### P0 - Atomicish Protection Replacement in SafetyKernel

- [x] Add SafetyKernel method `replace_protective_order_atomicish()`.
- [x] Use adapter `replace_protective_order_atomicish()` for cross-type replacement:
  - `STP -> TRAIL`;
  - `STP -> TRAIL_LIMIT`;
  - `TRAIL -> TRAIL_LIMIT`;
  - `TRAIL_LIMIT -> TRAIL`.
- Replacement order flow:
  - place new protection;
  - wait for `Submitted` / `PreSubmitted`;
  - persist/audit new order;
  - cancel old protection;
  - reconcile open orders.
- [x] Same-type `STP -> STP` should use `modify_stop_order()` when only qty/stop changes.
- Reject replacement if it would temporarily leave no active protection and no emergency fallback.

Acceptance:

- Unit test proves old `STP` remains untouched when new `TRAIL` submission fails.
- Unit test proves old `STP` is cancelled only after new `TRAIL` is accepted.
- [x] Unit test proves same-type `STP` amendments use modify, not cancel/replace.

### P0 - Protection Audit Must Understand TRAIL

- [x] Update stop/protection audit logic to classify these as live protection:
  - `STP`;
  - `STP_LMT`;
  - `TRAIL`;
  - `TRAIL_LIMIT`.
- Include trail metadata in local audit:
  - old order id;
  - new order id;
  - order type;
  - stop px;
  - trail amount;
  - trail percent;
  - limit offset;
  - position qty before/after;
  - replacement reason.
- Avoid self-heal rebuilding a duplicate `STP` when a native `TRAIL` is already live.

Acceptance:

- Audit loop reports `PROTECTION_AUDIT_STOP_LIVE` or equivalent when only native `TRAIL` is live.
- Missing-protection self-heal does not duplicate protection if native `TRAIL` exists.

### P0 - TRAIL LIMIT Watchdog and Emergency Fallback

- [x] Add runtime watchdog loop for active `TRAIL_LIMIT` protection orders.
- [x] Detect triggered-but-unfilled via:
  - broker order status if available;
  - fallback market trigger condition using latest price and known trail stop.
- [x] If unfilled longer than `trail_limit_unfilled_timeout_sec`:
  - cancel trail-limit order;
  - submit emergency `MKT` or marketable `LMT`;
  - audit fallback reason and old/new broker order IDs.
- [x] Add marketable LMT helper:
  - long close: sell at `bid - buffer`;
  - short close: buy at `ask + buffer`.

Acceptance:

- [x] Unit test: triggered `TRAIL_LIMIT` older than timeout submits emergency fallback.
- [x] Unit test: terminal/filled trail-limit does not fallback.
- [x] Unit test: untriggered trail-limit does not fallback.

### P1 - Centralize Protection Reconciliation by Position ID

- [x] Promote protection reconciliation from design/helper into active SafetyKernel service.
- [x] Define one active protective family per parsed protection family key.
- Reconcile:
  - `position qty == 0`: cancel residual protection;
  - `position qty > 0 and no protection`: rebuild protection;
  - `protection qty > position qty`: reduce/cancel excess;
  - `protection qty < position qty`: amend or add protection;
  - duplicated protection families: keep newest valid family, cancel stale family.
- [x] Implemented safe reconciliation actions:
  - `position qty == 0`: cancel residual protection;
  - `protection qty > position qty`: reduce excess;
  - duplicated protection families: keep newest valid family, cancel stale family.
- [x] Full missing-protection rebuild is available in SafetyKernel when explicit `desired_specs_by_family` is supplied.
- [ ] Automatic protection expansion remains explicit-target only.
- [x] Use durable owner fields where available:
  - account id;
  - strategy id;
  - strategy instance id;
  - lot id / position id;
  - symbol;
  - order ref;
  - broker order id;
  - perm id.

Note:

- Automatic expansion when `protection qty < position qty` requires explicit `target_qty_by_family` / `desired_specs_by_family`; the live loop avoids blindly upsizing each strategy's protection from symbol-level broker qty because multiple strategies can share one symbol.

Acceptance:

- [x] Unit tests for partial fill qty repair.
- [x] Unit tests for position-closed stale protection cancellation.
- [x] Unit tests for duplicate protection family cleanup.

### P1 - Strategy-Side ProtectionSpec Generation

- [x] Move runner protection computation out of ad hoc strategy branches for TP1 native trail handoff.
- [x] Build `ProtectionSpec` from runtime state:
  - side;
  - live qty;
  - runner qty;
  - entry px;
  - current px;
  - bid/ask;
  - initial stop;
  - entry ATR;
  - TP1 state;
  - trail high/low.
- Long protection:
  - initial floor;
  - breakeven floor;
  - ATR trail floor;
  - locked-R floor.
- Short protection:
  - initial ceiling;
  - breakeven ceiling;
  - ATR trail ceiling;
  - locked-R ceiling.
- [x] Preserve monotonic stop movement through SafetyKernel validation:
  - long stop only up;
  - short stop only down.

Acceptance:

- Tests for long STP ratchet.
- Tests for short STP ratchet.
- [x] Tests for TP1 -> native trail mode selection.
- Tests for software trailing fallback when native trail disabled.

### P1 - DB / Audit Schema Hardening

- [x] Decide whether to add dedicated DB columns:
  - `trail_amount`;
  - `trail_percent`;
  - `trail_stop_px`;
  - `lmt_price_offset`;
  - `protection_family_id`;
  - `position_id`.
- [x] Until schema migration is done, keep trail metadata in `note.order_style`.
- [x] Update reports and order inspectors to display native trailing protection details.

Acceptance:

- [x] `inspect_ghost_orders.py` and daily trade summaries show `TRAIL` / `TRAIL_LIMIT` details.
- [x] Reports distinguish fixed stop, native trail, trail limit, and emergency fallback metadata when present in notes.

### P2 - Full Paper Soak

- Run paper trading soak with:
  - fixed `STP`;
  - `RATCHET_STP`;
  - TP1 runner `TRAIL`;
  - paper-only `TRAIL_LIMIT`;
  - emergency fallback.
- Capture:
  - order lifecycle;
  - audit chain;
  - fill attribution;
  - duplicate protection count;
  - unprotected live position seconds;
  - replacement failure count;
  - fallback count.

Acceptance:

- No unprotected live position window during replacement.
- No duplicate active protective families for the same `position_id`.
- Trail-limit fallback fires in controlled simulated timeout.
- No false kill-switch when native `TRAIL` is live.

## Immediate Next Command Set

Recommended next focused implementation target:

```bash
./venv_ml/bin/python -m pytest \
  tests/unit/strategies/test_index_super_win_replica_core.py \
  tests/unit/exec/test_protection_engine_trailing_orders.py \
  tests/unit/services/exec/test_ib_adapter_open_orders.py \
  tests/unit/exec/test_safety_kernel_oco_persistence.py -q
```

Then implement P0 in this order:

1. SafetyKernel atomicish replacement.
2. Protection audit recognizes `TRAIL` / `TRAIL_LIMIT`.
3. TP1 runner submits native `TRAIL`.
4. Trail-limit watchdog fallback.
