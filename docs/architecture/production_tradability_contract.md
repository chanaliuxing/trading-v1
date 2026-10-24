# Production Tradability Contract

## Purpose

`tradable` was historically overloaded across heartbeat liveness, off-session idle,
data health, system pause, risk readiness, and true entry readiness. Production
readiness now treats entry readiness as a derived state with explicit components.

## Fields

- `alive`: the strategy heartbeat exists and is fresh enough to prove the strategy process is alive.
- `configured_live`: the strategy is enabled in the live production surface/config.
- `session_open`: the strategy's market/session is currently open.
- `entry_window_open`: the strategy is inside a window where new entries are allowed.
- `data_ready`: market data, features, and streams are fresh and valid.
- `system_ready`: platform/system state is not paused or critically stale.
- `risk_ready`: broker truth, position safety, and risk gates allow new entries.
- `execution_ready`: the execution path is available and contract-compatible.
- `tradable_entry_ready`: true only when all readiness fields above are true.
- `close_only_allowed`: close/reduce/cleanup may be allowed even when entry is blocked.
- `idle_reason`: non-failure idle state such as `offsession`, `not_in_entry_window`, or `no_signal`.
- `block_reasons`: real blockers grouped into data/system/risk/execution categories.

The legacy `tradable` field is retained for compatibility, but it is now an alias
for `tradable_entry_ready`. It must not mean alive, off-session OK, or idle OK.

## Derived Rule

```text
tradable_entry_ready =
  alive
  and configured_live
  and session_open
  and entry_window_open
  and data_ready
  and system_ready
  and risk_ready
  and execution_ready
```

## Non-Failure Idle States

`offsession` and `idle_not_in_entry_window` are not strategy entry failures. They
set `tradable_entry_ready=false` and increment idle counts, but they should not
inflate `entry_failure_count`.

## Real Blockers

- `stream_invalid`, stale market data, missing required values, and feature gaps set `data_ready=false`.
- `system_paused` sets `system_ready=false`; it is a platform state, not a strategy bug.
- `broker_truth_missing`, `position_safety_no_go`, and risk/quarantine failures set `risk_ready=false`.
- execution degradation or incompatible execution paths set `execution_ready=false`.

## Production State Counts

Production state must report:

- `entry_failure_count`: real entry blockers only.
- `idle_count`: alive strategies not expected to enter now.
- `stale_heartbeat_count`: stale heartbeat/artifact cases.
- `tradable_entry_ready_count`: strategies truly ready to submit entry intents.
- `close_only_allowed_count`: strategies allowed to close/reduce while entries are blocked.
