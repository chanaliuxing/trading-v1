# Index Super Win Observability

## Goal

Provide full-chain diagnostics for:

- Why a position was opened.
- Why an entry did not open (which gate blocked it).
- Where IBKR sync or broker reconciliation failed.
- Whether runtime state and broker truth drifted.

## Outputs

Configured via `strategy.params.extras`:

- `enable_observability_file` (default: `true`)
- `diag_log_path` (JSONL event stream)
- `diag_snapshot_path` (latest aggregate snapshot)
- `diag_flush_interval_sec` (disk snapshot write interval)

For `index_super_win_001` defaults in `config.yml`:

- `runtime/state/index_super_win_diag_index_super_win_001.jsonl`
- `runtime/state/index_super_win_diag_snapshot_index_super_win_001.json`

## Event Nodes

Each event is emitted as `index_super_win_diag_event` with:

- `node`, `status`, `symbol`, `reason`, `extras`, `ts_us`

Core nodes:

- `initialize.start|ready`: boot/startup path.
- `features.received`: signal bar received.
- `signal.observed`: parsed signal decision.
- `entry_gate.blocked`: pre-entry block reason (`signal_inactive`, `missing_side`, `non_positive_qty`, `broker:*`, `open_orders:*`, `preview:*`, `submit:*`).
- `preview.*`: runtime preview status.
- `submit.start|APPROVED|REJECTED|...|exception`: submission result.
- `runtime.INTENT_SUBMITTED`: runtime state moved to submitted.
- `ctx_position.flat|nonzero|error`: strategy context position read.
- `ibkr_positions_refresh.ok|error|disconnected|missing_ib`: IBKR pull state.
- `broker_reconcile_positions.start|done`: broker position reconciliation.
- `broker_reconcile_open_orders.start|done`: broker open-order reconciliation.
- `broker_position.*` and `broker_open_orders.*`: symbol-level broker truth path.
- `shutdown.start`: controlled shutdown point.

## Trader View Snapshot

`diag_snapshot_path` includes:

- `counters`: global node/status counters.
- `reason_counters_top`: top reasons globally.
- `symbol_counters`: node/status counters per symbol.
- `symbol_reason_counters_top`: reason counters per symbol.
- `trader_view_by_symbol`:
  - `final_state`: `entered|blocked|attempted_not_entered|no_attempt`
  - `entry_attempts`
  - `entry_blocked`
  - `entry_submitted`
  - `top_reasons`
  - `last_preview`

Use `trader_view_by_symbol` first for "why opened / why not opened today".

## Quick Triage

1. "Why no entry?"

```bash
jq '.trader_view_by_symbol' runtime/state/index_super_win_diag_snapshot_index_super_win_001.json
```

Check `final_state=blocked` and `top_reasons`.

2. "Where was it blocked in chain?"

```bash
rg '"node":"entry_gate"|"node":"preview"|"node":"submit"|"node":"runtime"' runtime/state/index_super_win_diag_index_super_win_001.jsonl
```

3. "IBKR sync health?"

```bash
rg '"node":"ibkr_positions_refresh"' runtime/state/index_super_win_diag_index_super_win_001.jsonl
```

4. "Broker truth vs runtime guard?"

```bash
rg '"node":"broker_reconcile_positions"|"node":"broker_reconcile_open_orders"|"node":"broker_position"|"node":"broker_open_orders"' runtime/state/index_super_win_diag_index_super_win_001.jsonl
```

## Interpretation Rules

- `runtime.INTENT_SUBMITTED > 0` means strategy intent reached runtime submitted state.
- `entry_gate.blocked > 0` with no submit means signal was blocked before order submission.
- Frequent `ibkr_positions_refresh.error|disconnected` indicates broker sync reliability issue.
- `broker_open_orders.quarantined` or `broker_position.nonzero` on unknown ownership indicates conflict protection is working (entry should be blocked).
