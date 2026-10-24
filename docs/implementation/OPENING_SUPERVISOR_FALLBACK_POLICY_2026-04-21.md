# Opening Supervisor Fallback Policy - 2026-04-21

## Goal

Opening-time failures should be handled automatically, but with hard safety boundaries:

1. Every automated repair must be revertable with a commit boundary.
2. Infrastructure and strategy logic are out of scope for opening-time fallback repairs.
3. Opening-time fallback may only repair supervisor, automation, observability, and launch wiring problems.

## Allowed Auto-Repair Scope

Allowed paths:

- `tool/ops/`
- `tests/unit/tool/`
- `docs/implementation/`
- `ops/launchd/`
- `scripts/run_system_autostart.sh`

Ignored generated artifacts:

- `runtime/state/`
- `reports/`
- `logs/`

Forbidden paths:

- `strategies/`
- `services/`
- `infra/`
- `libs/`
- `run_system.py`
- `config.yml`

## Fallback Execution Rules

When `Opening Guard Open` or `Opening Guard Acceptance` still ends in `HOLD` after bounded remediation:

1. Escalate to GPT-5.4 fallback repair.
2. Fallback repair may only change files within the allowed auto-repair scope.
3. After the fix:
   - run focused tests,
   - rerun the opening supervisor or health report,
   - run `tool/ops/check_opening_repair_scope.py`,
   - create a dedicated commit so the change is trivially revertable.

If the scope check fails, fallback repair must stop and report `ESCALATE`.

## What Fallback May Fix

- Wrong or stale opening health checks
- Broken opening supervisor control flow
- Launchd or autostart script issues
- Observability/report parsing false positives
- Missing report generation wiring

## What Fallback May Not Fix

- QQQ strategy logic
- Stock or option signal semantics
- Core execution flow
- IB broker adapters
- Market data infrastructure internals
- Runtime reconciler core behavior

Those remain manual or post-close engineering work.
