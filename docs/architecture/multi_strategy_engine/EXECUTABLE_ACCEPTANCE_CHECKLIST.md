# Executable Acceptance Checklist

## Gate 1: Schema and Field Completeness
- [ ] Run schema validation on representative payload fixtures.
- [ ] SQL check confirms 100% critical trace field coverage.

## Gate 2: Isolation and Idempotency
- [ ] Same-symbol dual-strategy test passes with zero cross-strategy mutation.
- [ ] Idempotent resubmission test shows no duplicate live broker order.

## Gate 3: Netted Invariants
- [ ] Parent-child fill accounting SQL returns 0 violations.
- [ ] Residual allocation is deterministic across replay runs.

## Gate 4: Failure Injection
- [ ] Duplicate callback scenario passes.
- [ ] Out-of-order callback scenario passes.
- [ ] DB transient failure retry scenario passes.
- [ ] Broker timeout + retry scenario passes.

## Gate 5: Rollback Readiness
- [ ] Feature-flag OFF parity regression passes.
- [ ] Rollback drill completes in < 5 minutes.
- [ ] Attribution field RPO is 0 after rollback.

## Gate 6: Pre-Integration Go/No-Go
- [ ] No open P0/P1 defects.
- [ ] 1-day paper smoke passes all checks.
- [ ] Signed approval from strategy owner + execution owner.
