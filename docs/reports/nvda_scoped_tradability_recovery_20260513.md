# NVDA Paper Runtime Scoped Tradability Recovery - 2026-05-13

## Context

This note documents the paper-runtime issue where scoped tradability had already been wired into the order path, but NVDA strategies were still blocked by legacy global or symbol-level safety state after restart.

The observed chain was:

1. Signal generation and strategy decision were healthy.
2. `TradabilityDecision` allowed entry for the exact account/strategy instance/symbol scope.
3. Execution could submit paper orders.
4. Legacy recovery/quarantine and stale kill-switch state could still keep production readiness at `partial` or block one strategy instance.

The goal was not to bypass hard safety. The goal was to make stale or symbol-level state converge only when broker truth, DB state, owner lane, and protection evidence were all consistent.

## Symptoms

### Symptom 1: Stale synthetic quarantine blocked a protected live owner lane

After restart, runtime had:

- broker NVDA position: nonzero
- owner lane: `nvda_minute_flip_paper_002`
- `core.position_ledger_current`: matching `_002` lot quantity
- `core.core_position`: matching `_002` position
- live broker protection orders: stop loss and take profit for `_002`
- position safety: `no_go`
- active quarantine: `recovery_action_not_executable`

The quarantine row had no durable broker/order identity and was a synthetic recovery quarantine from an earlier failed recovery path. Because `core.core_order_quarantine` is symbol-scoped, the old row made the whole NVDA symbol look unsafe even though the live owner lane was protected.

### Symptom 2: Stale strategy kill switch survived after broker/core/ledger were flat

After the stale quarantine was resolved, the broker eventually became flat and all NVDA DB lanes were flat, but `nvda_minute_flip_paper_002` still reported:

```text
symbol_kill_switch:NVDA:unsafe_position_without_protection
```

That kill switch had been correct when the strategy believed it had an unsafe position. It became stale after broker truth and position safety proved flat.

### Symptom 3: User-visible state looked contradictory

At different points, runtime showed combinations such as:

```text
production_state.go_no_go=go
entry_go_no_go=partial
global_halt=false
```

This is valid under scoped tradability: platform safety can be globally healthy while specific strategy instances or account-symbol scopes are temporarily entry-blocked.

### Symptom 4: Protection order refs were treated as broker truth orphans

After paper execution resumed, `_001` filled an NVDA entry and attached live stop/take-profit protection. The broker open-order refs used the protection format:

```text
sl:DUO112752|nvda_minute_flip_paper_001|NVDA|
tp:DUO112752|nvda_minute_flip_paper_001|NVDA|
```

`BrokerTruthSyncModule` only recognized durable execution-kernel bindings. Protection orders can be broker-visible before a local binding exists, so the sync incorrectly classified these refs as `orphan_order_refs` and raised:

```text
symbol_kill_switch:NVDA:broker_truth_mismatch
```

This was a false mismatch. The protection refs contain explicit account, strategy instance, and symbol owner fields. The correct behavior is to accept own protection refs for broker truth sync and ignore foreign owner-lane protection refs when evaluating another strategy instance.

### Symptom 5: Flatten timeout kill switch survived after confirmed flat

After the strategy submitted a scoped flatten and the broker filled it, broker truth, `core.core_position`, and `core.position_ledger_current` all showed NVDA flat. The remaining blocker was:

```text
symbol_kill_switch:NVDA:flatten_not_converged_timeout
```

That reason is recoverable after confirmed flat convergence. It should not survive when position safety is `go`, there are no active locks/quarantines, broker has no position, and broker has no open orders for the symbol.

### Symptom 6: Aggregate recovery flatten lacked complete owner identity

After the EOD/platform scoped fix, NVDA traded again, but a later runtime recovery action created:

```text
scoped_tradability_denied:owner_identity_incomplete
```

This was not cross-strategy order mixing. The root cause was that `PositionReconciler` reduced an account+symbol aggregate mismatch to `strategy_id` only, and `RuntimeReconciler` then attempted a recovery flatten using broker aggregate quantity. That is unsafe for same-symbol multi-strategy operation because broker positions are account-symbol netted while ownership lanes are account+strategy_instance+symbol+lot.

The correct contract is stricter:

- recovery flatten requires complete owner identity: account, strategy, strategy instance, symbol, lot, owner key
- recovery flatten quantity must match one exact owner lane
- aggregate residuals without exact owner lane are quarantine/manual review, not executable flatten
- stale no-order synthetic quarantines may be auto-resolved only when current broker/core/ledger/protection evidence proves one owner lane is safe

## Fixes Landed

### 1. Protected owner-lane quarantine cleanup

Commit: `66abfdce7 position safety: resolve protected owner quarantine`

File: `services/production/position_safety_supervisor.py`

Added `protected_quarantine_cleanup` to `PositionSafetySupervisor.run_once()`.

The cleanup resolves only stale synthetic `recovery_action_not_executable` quarantine rows when all of the following are true:

- quarantine has no broker/order identity (`broker_order_id`, `ib_perm_id`, `order_ref` are empty)
- broker has a nonzero position for the symbol
- exactly one nonflat `core.core_position` owner lane exists for that symbol
- owner-lane `core.core_position` quantity matches broker quantity
- owner-lane `core.position_ledger_current` quantity matches broker quantity
- durable `core.core_order` protection rows exist for the same strategy instance
- live broker open orders exactly match durable protection rows by broker order id or order ref
- protection order quantity covers the broker position

This deliberately does not resolve:

- unknown broker order quarantines
- orphan order quarantines
- quarantines with broker/order identity
- ambiguous owner lanes
- cases where broker, ledger, core position, or protection evidence disagree

Tests added:

- `test_position_safety_supervisor_resolves_synthetic_quarantine_when_owner_lane_protected`
- `test_position_safety_supervisor_keeps_synthetic_quarantine_when_owner_lane_ambiguous`

### 2. Flat readiness stale kill-switch release

Commit: `b6f16716e stocks kernel: release stale flat kill switch`

File: `strategies/stocks_kernel/strategy.py`

Added heartbeat-time release of stale position-safety kill switches when broker and position safety both prove the symbol is flat.

The release applies to:

- `unsafe_position_without_protection`
- `live_position_missing_protection`

The release requires:

- `position_safety.go_no_go == go`
- no active manual locks
- no active quarantine
- no position safety record for that symbol
- live broker truth `go`
- no broker position for that symbol
- no broker open order for that symbol

Test added:

- `test_composable_strategy_heartbeat_releases_stale_unsafe_position_kill_switch`

### 3. Protection ref owner-aware broker truth sync

Commit: `stocks kernel: recognize protection order refs`

File: `strategies/stocks_kernel/modules/broker_truth_sync.py`

Added strict parsing for protection refs in the form:

```text
sl:<account_id>|<strategy_instance_id>|<symbol>|...
tp:<account_id>|<strategy_instance_id>|<symbol>|...
```

The broker truth sync now:

- accepts unbound own protection refs as known broker-side protection
- ignores another strategy instance's protection refs on the same symbol
- keeps malformed protection refs as orphan orders
- keeps entry refs strict: entry refs still require durable binding and still freeze on mismatch

Tests added:

- `test_broker_truth_sync_accepts_unbound_own_protection_order_ref`
- `test_broker_truth_sync_ignores_foreign_protection_order_ref_same_symbol`
- `test_broker_truth_sync_malformed_protection_ref_remains_orphan`
- `test_broker_truth_sync_entry_ref_still_requires_binding`

### 4. Flat readiness release for flatten timeout

Commit: `stocks kernel: release stale flatten timeout kill switch`

File: `strategies/stocks_kernel/strategy.py`

Added `flatten_not_converged_timeout` to the recoverable symbol kill-switch reasons when flat readiness proves the symbol has converged:

- position safety is `go`
- active manual lock count is zero
- active quarantine count is zero
- position safety has no symbol record
- live broker truth is `go`
- broker has no position for the symbol
- broker has no open order for the symbol

Test added:

- `test_composable_strategy_heartbeat_releases_stale_flatten_timeout_kill_switch`

### 5. Owner-complete recovery flatten contract

Commit: pending

Files:

- `services/reconciliation/contracts.py`
- `services/reconciliation/runtime_reconciler.py`
- `services/reconciliation/strategy_recovery_policy.py`
- `services/production/position_safety_supervisor.py`

Changes:

- `PositionReconciler` now carries `account_id`, `strategy_instance_id`, `lot_id`, and `position_owner_key` from `core.position_ledger_current` into mismatch metadata when exactly one complete owner lane exists.
- `RuntimeReconciler` no longer submits recovery flatten for aggregate mismatches unless owner identity is complete and broker quantity matches the exact owner lane quantity.
- `RecoveryPolicyContext` and `RecoveryAction` carry owner lane fields into the decision envelope so execution receives durable owner metadata.
- `PositionSafetySupervisor` can resolve stale synthetic `scoped_tradability_denied:owner_identity_incomplete` quarantines only when a single current owner lane is live, protected, and broker/core/ledger quantities agree.

Tests added/updated:

- `test_runtime_reconciler_blocks_policy_when_owner_identity_incomplete`
- `test_runtime_reconciler_blocks_aggregate_recovery_when_owner_qty_differs_from_broker_qty`
- `test_position_reconciler_adds_owner_from_single_ledger_lot`
- `test_position_safety_supervisor_resolves_owner_identity_denied_quarantine_when_owner_lane_protected`

## Runtime Evidence After Fix

After restarting paper runtime:

```text
broker_truth.go_no_go=go
position_safety.go_no_go=go
production_state.go_no_go=go
production_state.entry_go_no_go=partial
production_state.global_halt=false
```

The old synthetic quarantine was auto-resolved with evidence:

```text
reason=owner_lane_live_position_protected
owner_lane.strategy_id=nvda_minute_flip_paper_002
broker_qty=1
core_qty=1
ledger_qty=1
protected_order_count=2
```

Then paper trading resumed. The runtime submitted and filled an NVDA paper entry:

```text
strategy_id=nvda_minute_flip_paper_001
symbol=NVDA
side=BUY
qty=1
state=FILLED
broker_order_id=966
order_ref=nvda_flip:nvda_minute_flip_paper_001:NVDA:ENT
```

Protection was attached:

```text
stop_loss broker_order_id=968
take_profit broker_order_id=970
```

The order note included an allowed scoped decision:

```text
TradabilityDecision.allow=true
scope.account_id=DUO112752
scope.strategy_instance_id=nvda_minute_flip_paper_001
scope.symbol=NVDA
flow_type=ENTRY
```

## How To Diagnose This Next Time

### 1. Check global platform health

```bash
venv/bin/python - <<'PY'
import json
from pathlib import Path
for p in [
  'runtime/readiness/production_state/production_state_latest.json',
  'runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json',
  'runtime/readiness/position_safety/position_safety_latest.json',
]:
    d=json.loads(Path(p).read_text())
    print(p, {k:d.get(k) for k in ['generated_at','go_no_go','entry_go_no_go','safety_go_no_go','global_halt','blockers','active_quarantine_count','broker_position_count','broker_open_order_count']})
PY
```

If `global_halt=true`, do not bypass it. Inspect broker truth, runtime freshness, position safety, manual recovery, and order truth first.

### 2. Check strategy-instance scoped state

```bash
venv/bin/python - <<'PY'
import json
from pathlib import Path
for sid in ['nvda_minute_flip_paper_001','nvda_minute_flip_paper_002']:
    p=Path(f'runtime/readiness/strategy_tradability/heartbeats/{sid}.json')
    d=json.loads(p.read_text())
    print(sid, d.get('generated_at'), d.get('tradable'), d.get('blockers'), d.get('details',{}).get('active_symbol_kill_switches'))
PY
```

Expected meanings:

- `system_paused`: usually startup or platform pause input; check production state and runtime startup.
- `order_lifecycle_pending`: this strategy has a pending order; not an EOD/global failure.
- `entry_isolation_blocked_pending_order`: another order is still active for this account/symbol path.
- `broker_truth_mismatch`: inspect whether broker/core/ledger/protection are aligned. If orphan refs are `sl:` or `tp:` refs, verify the protection ref owner fields before treating them as true orphan orders.

### 3. Check DB owner lanes and broker orders

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d test -P pager=off \
  -c "SELECT strategy_id, symbol, pos_qty, avg_px, updated_at FROM core.core_position WHERE symbol='NVDA' ORDER BY strategy_id;" \
  -c "SELECT account_id, strategy_id, strategy_instance_id, symbol, lot_id, qty FROM core.position_ledger_current WHERE symbol='NVDA' AND ABS(qty)>1e-9 ORDER BY strategy_instance_id;" \
  -c "SELECT order_id, strategy_id, symbol, state, side, qty, broker_order_id, ib_perm_id, order_ref, order_type, limit_px, stop_px, created_at, updated_at FROM core.core_order WHERE symbol='NVDA' ORDER BY updated_at DESC LIMIT 30;"
```

A healthy protected long position should have:

- one nonzero core position for the owning strategy instance
- one matching nonzero ledger lane
- matching broker position
- stop/take-profit or flattening orders matching the same owner lane

### 4. Check active quarantine

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d test -P pager=off \
  -c "SELECT quarantine_id, symbol, reason, status, broker_order_id, ib_perm_id, order_ref, note, updated_at, resolved_at FROM core.core_order_quarantine WHERE symbol='NVDA' ORDER BY created_at DESC LIMIT 10;"
```

Do not manually resolve a quarantine unless broker truth, DB projection, owner lane, and protection evidence all agree.

## Important Safety Boundaries

Do not use these shortcuts:

- do not turn `production_state.go_no_go=go` into blanket entry permission
- do not treat `entry_go_no_go=partial` as global no-go
- do not resolve unknown-order quarantine with symbol-only evidence
- do not attribute orders by symbol alone
- do not ignore broker truth, position safety, runtime freshness, or manual recovery blockers
- do not force both same-symbol strategies to trade concurrently unless account/symbol risk policy explicitly allows it

## Current Interpretation

If one NVDA strategy has an active position or pending entry, the other strategy may be scoped-blocked by same account/symbol isolation. That is expected unless `same_symbol_multi_strategy_safe` and per-account/symbol risk caps explicitly permit parallel same-symbol entries.

The fix in this incident restored scoped tradability and paper execution. It did not globally disable account-symbol isolation.
