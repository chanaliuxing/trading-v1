# Broker Truth Convergence Acceptance Checklist - 2026-05-01

## Hard Gates
- [x] Startup aborts or remains recovering when any order orphan, ghost, state mismatch, or position mismatch exists.
- [x] `system_resumed` is never emitted with synthetic clean flags.
- [x] UOM blocks new risk-increasing intents for quarantined/mismatched symbols.

## Fill Convergence
- [x] Every broker execution has exactly one durable `core_fill` row.
- [x] Every durable fill has a corresponding position ledger event.
- [x] Unattributed fills either converge into adopted reconciliation facts or keep the symbol blocked.

## Position Convergence
- [x] Broker/ledger mismatch is resolved by append-only ledger adjustment, not table overwrite.
- [x] The adjustment includes audit evidence and idempotency key.
- [x] Manual acknowledgement is required when strategy/lot ownership is uncertain.

## Open Order Convergence
- [x] Known DB orders get broker identity synced by `orderRef`.
- [x] Unknown broker orders stay quarantined and blocked until adopted or cancelled.
- [x] Terminal DB rows never silently adopt still-open broker orders.

## Evidence
- [x] Pre-open evidence shows zero active order quarantines.
- [x] Pre-open evidence shows zero broker/ledger mismatches.
- [x] Runtime evidence includes clean order truth and clean position truth snapshots.
