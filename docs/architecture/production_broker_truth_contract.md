# Production Broker Truth Contract

## Broker-Backed Truth

`broker_truth_backed=true` is valid only when a fresh `live_broker_truth_snapshot`
comes from a broker-source, manager-owned read path. A database projection can be
included for comparison, but it is never broker truth.

Required live snapshot fields:

- `artifact_type=live_broker_truth_snapshot`
- `source=live_broker`
- `broker_truth_backed=true`
- `account`
- `source_process_pid`
- `generated_at`
- `positions`
- `open_orders`
- `lookup_failed=false`
- `stale=false`

When the snapshot is produced through the IB connection manager, it also carries
`connection_role`, `ib_client_id`, and `ib_connection_epoch`.

## Fail Closed Cases

The following states must keep production readiness at `no_go`:

- missing live broker snapshot
- stale live broker snapshot
- `lookup_failed=true`
- invalid or missing account
- `db_projection_only`

`db_projection_only` can prove that the database projection is clean, but it
cannot prove broker truth.

## Ownership Boundary

Production readiness read paths must not call `IBInsyncAdapter._connect()`
directly. Broker truth and position safety use either:

- the IB connection manager's manager-owned read handle, or
- a fresh broker truth artifact produced by that manager-owned path.

Position safety may consume broker truth, but broker truth is not allowed to
depend exclusively on a position-safety wrapper artifact.

## Position Safety Dependency Contract

Position safety treats broker truth as an upstream dependency in production
manager-owned paths. Its artifact records a `dependency_state` object with:

- `broker_truth_required`
- `broker_truth_available`
- `broker_truth_backed`
- `broker_truth_source`
- `broker_truth_snapshot_id`
- `broker_truth_generated_at`
- `broker_truth_age_sec`
- `broker_truth_lookup_failed`
- `broker_truth_stale`

Dependency states are explicit:

- `OK`: fresh broker-backed live truth; position safety may run checks and
  cleanup.
- `MISSING`: no broker truth snapshot; fail closed with
  `broker_truth_missing`.
- `STALE`: snapshot age exceeds TTL or is marked stale; fail closed with
  `broker_truth_stale`.
- `LOOKUP_FAILED`: upstream broker truth lookup failed; fail closed with
  `broker_truth_lookup_failed`.
- `NOT_BROKER_BACKED`: DB projection or non-live source; fail closed with
  `broker_truth_not_broker_backed`.
- `INVALID_ACCOUNT` / `INVALID_SCHEMA`: malformed broker truth; fail closed.

When dependency validation fails, residual cleanup is blocked with reason
`broker_truth_unavailable`; position safety must not fall back to direct adapter
reads.
