# Runtime Version Freshness Contract

## Purpose

Production go/no-go must prove that the running process has loaded the current
critical code. Fresh broker truth, position safety, and tradability artifacts are
not sufficient if `run_system` started before a critical module changed.

## Runtime Artifact

The runtime version artifact records:

- process identity: `pid`, `process_start_time`, `runtime_id`, `restart_id`, and command.
- code identity: `git_sha`, `build_id`, `source_tree_hash`, and critical module records.
- freshness: `loaded_code_current`, `critical_modules_stale_count`,
  `runtime_version_loaded_code_stale`, and `restart_required`.
- blockers and warnings.

## Critical Module Rule

For every critical module:

- `modified_at <= process_start_time` means the current process can have loaded
  that module version.
- `modified_at > process_start_time` means restart proof is missing.
- missing or uncomputable manifest state fails closed as runtime unknown.

When any critical module is stale, production state must include
`runtime_version_loaded_code_stale` in `runtime_blockers` and set platform
`go_no_go=no_go`.

## Build Identity

If expected `git_sha`, `build_id`, or `source_tree_hash` is available and does
not match the runtime identity, the runtime status is `BUILD_MISMATCH`. This is a
hard runtime blocker unless explicitly configured otherwise in a later contract.

## Production State Bucket

Runtime freshness is not a strategy entry failure. It belongs to:

- `buckets.runtime_blockers`
- `summary.runtime_failure_count`
- `summary.failure_count`
- `readiness_details.runtime_version`

It must not increment `entry_failure_count` or be duplicated per strategy.

## Examples

1. Fresh runtime:
   - process starts after all critical module mtimes
   - `loaded_code_current=true`
   - no runtime blocker

2. `safety_kernel.py` modified after process start:
   - `loaded_code_current=false`
   - `restart_required=true`
   - blocker `runtime_version_loaded_code_stale`

3. Missing manifest:
   - status `MISSING_MANIFEST`
   - blocker `runtime_version_missing_manifest`

4. Build mismatch:
   - status `BUILD_MISMATCH`
   - blocker `runtime_version_build_mismatch`

5. Runtime stale while all other artifacts are fresh:
   - production state remains `no_go`
   - runtime bucket explains the only failure
