# Memory Notes

## 2026-04-18 IB reconnect hardening (runtime owner unification)

Context:
- Symptom was: cold start could connect, but after IB restart only one role might recover.
- Root cause was runtime recovery ownership ambiguity (market path driving reconnect; master/exec had no independent runtime owner).

Applied decisions:
- `IBConnectionManager` is the single runtime recovery owner.
- Any role disconnect (`market/exec/master`) triggers manager recovery.
- Recovery loop repeatedly runs:
  - `wait_until_ready(require_exec=True, require_master=True)`
  - until market/exec/master + master truth are ready.
- `master` now has explicit disconnect event binding + state invalidation.
- `exec` managed-mode adapter now sends disconnect callback to manager.
- During active manager recovery, non-manager `wait_until_ready` callers do passive wait only.

Verification signature to look for in logs:
- `ib_runtime_recovery_triggered`
- `ib_runtime_recovery_cycle_begin`
- `ib_market_connected` + `ib_exec_connected` + `ib_master_connected`
- `ib_master_truth_snapshot_complete`
- `ib_runtime_recovery_cycle_complete`

Guardrail:
- Do not re-introduce runtime recovery owner logic inside `market_data_service`.
- Keep `run_system` startup gate and manager runtime gate semantically equivalent.
