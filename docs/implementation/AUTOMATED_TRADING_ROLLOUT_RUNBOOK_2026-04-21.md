# Automated Trading Rollout Runbook - 2026-04-21

## Purpose

This runbook turns the daily trading hardening work into a closed loop:

1. Generate machine-readable evidence.
2. Evaluate whether the rollout can advance.
3. Review blockers critically.
4. Apply only bounded fixes that preserve QQQ signal parity and order safety.

The loop does not auto-promote live trading by itself. It can mark `live_cutover_allowed=true` only after the gates pass.

## Daily Evidence

Run after the close:

```bash
venv/bin/python tool/ops/build_daily_trading_evidence.py \
  --output reports/daily_trading_evidence/$(date +%Y%m%d).json
```

The evidence package writes and links:

- `reports/strategy_surface/live_strategy_surface_report_YYYYMMDD.json`
- `reports/orders/uom_caller_surface_YYYYMMDD.json`
- `reports/qqq_kernel_shadow_parity/qqq_shadow_drift_report_latest.json`
- `reports/daily_trading_evidence/YYYYMMDD.json`

Hard blockers:

- Phase 0 live/shadow freeze drift.
- Production direct execution callers outside the UOM boundary.
- Missing protection registry evidence.
- Missing lot-level position reconciler evidence.
- QQQ shadow drift `HOLD`.

## Rollout State

After evidence generation:

```bash
venv/bin/python tool/ops/evaluate_rollout_state.py \
  --evidence reports/daily_trading_evidence/$(date +%Y%m%d).json \
  --state runtime/state/trading_rollout_state.json
```

The state file tracks:

- `paper_clean_days`
- `paper_clean_day_ids`
- `qqq_shadow_observed_days`
- `last_recommendation`
- `blockers`
- `live_cutover_allowed`

The same day is idempotent; rerunning the evaluator will not double count clean paper days.

## Go / No-Go Rules

`HOLD` means no live promotion.

`CONTINUE_SHADOW` means paper/shadow can continue, but D+3 QQQ parity evidence is still incomplete.

`PASS` means the daily evidence passed. Live UOM rollout still requires:

- At least 3 clean paper days in `runtime/state/trading_rollout_state.json`.
- QQQ shadow drift recommendation `PASS`.
- Zero unresolved blockers.
- Human approval for live cutover timing and size.

## Automation Cadence

- Pre-open: refresh Phase 0 and UOM caller surfaces; do not modify code.
- Post-close: build daily evidence and update rollout state.
- Evening review: classify blockers as `BLOCKER`, `FIX_NOW`, `BACKLOG`, or `NO_ACTION`; apply only small bounded fixes with tests.

## Fix Discipline

Allowed in the evening review:

- Report correctness fixes.
- Gate false-positive fixes.
- Missing evidence wiring.
- Narrow idempotency or registry fixes with tests.

Not allowed in the same automated pass:

- QQQ signal calculation rewrites.
- Broad MarketDataPlane rewrites.
- Plugin chain refactors.
- Live cutover.

## Current Critical Path

1. Keep `run_system.py` and IB connected during paper/shadow sessions.
2. Collect 3 clean paper days and D+3 QQQ drift evidence.
3. Review every blocker within 48 hours.
4. Only when state says `live_cutover_allowed=true`, schedule a watched small-size live UOM rollout.
