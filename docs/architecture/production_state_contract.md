# Production State Contract

## Purpose

`production_state_latest.json` is the operator-facing go/no-go artifact. It must
explain why production is blocked without mixing platform safety failures,
strategy entry blockers, stale artifacts, idle states, and runtime freshness.

## Top-Level Rule

`go_no_go` is the platform safety decision. It is `no_go` when safety or runtime
blockers are present. Entry blockers set `entry_go_no_go=no_go`, but do not by
themselves change platform safety `go_no_go`.

## Summary Fields

- `failure_count`: explainable sum of safety, entry, artifact-stale, and runtime failures.
- `safety_failure_count`: hard platform blockers such as broker truth, position safety, session/recovery, execution, or manual safety locks.
- `entry_failure_count`: real strategy entry blockers while the strategy should be entry-capable now.
- `artifact_stale_count`: stale readiness artifacts and stale strategy heartbeats.
- `runtime_failure_count`: loaded-code/runtime-version freshness blockers. This
  includes stale critical modules, unknown runtime manifest state, and build
  identity mismatches.
- `idle_count`: strategies alive but not expected to enter now.
- `stale_heartbeat_count`: stale strategy heartbeat count from the tradability snapshot.
- `tradable_entry_ready_count`: strategies that can submit new ENTRY intents now.
- `close_only_allowed_count`: strategies allowed to close/reduce while entry is blocked.

## Buckets

- `safety_blockers`: broker truth, position safety, session/recovery, execution safety, manual locks.
- `entry_blockers`: data, validator, contract selection, manifest, or execution-entry blockers.
- `artifact_stale_blockers`: stale artifacts and stale strategy heartbeats.
- `runtime_blockers`: runtime version / loaded code freshness blockers. These
  are hard platform blockers and are never counted as strategy entry failures.
- `idle_reasons`: offsession, not in entry window, no signal, waiting.
- `warnings`: non-blocking operator warnings.

Each blocker should appear in one failure bucket. Idle reasons are not failures.
Global safety blockers should not be duplicated as per-strategy entry failures.

## Not Entry Failures

- `offsession`
- `idle_not_in_entry_window`
- `not_in_entry_window`
- `no_signal`
- `waiting`
- stale heartbeat
- global `system_paused`
- broker truth / position safety global no-go duplicated inside strategy tradability

## Examples

1. Offsession all strategies alive:
   - `idle_count > 0`
   - `entry_failure_count = 0`
   - `tradable_entry_ready_count = 0`

2. Broker truth missing:
   - `safety_blockers` contains the broker truth reason
   - `go_no_go = no_go`
   - duplicated strategy risk blockers are suppressed from `entry_failure_count`

3. System paused:
   - system blocker is platform state, not a strategy bug
   - close-only may remain allowed

4. Stale heartbeat:
   - `artifact_stale_count` and `stale_heartbeat_count` increase
   - `entry_failure_count` does not increase

5. Data stream invalid during entry window:
   - `entry_blockers` contains `stream_invalid`
   - `entry_go_no_go = no_go`

6. Close-only allowed but entry blocked:
   - `tradable_entry_ready_count` excludes the strategy
   - `close_only_allowed_count` includes it

7. Critical module changed after process start:
   - `runtime_blockers` contains `runtime_version_loaded_code_stale`
   - `runtime_failure_count = 1`
   - `entry_failure_count` is unchanged
   - `readiness_details.runtime_version.restart_required = true`
