# Trading System Audit Gate

Use this gate when you need one command that answers whether the trading system is safe to keep testing, merge, or prepare for manual canary review.

```bash
scripts/run_trading_system_audit_gate.sh quick
scripts/run_trading_system_audit_gate.sh full
scripts/run_trading_system_audit_gate.sh pre_canary
```

The gate always writes:

- `reports/audit_gate/audit_gate_latest.json`
- `docs/reports/audit_gate_YYYYMMDD.md`

The top-level verdict is one of:

- `PASS`: required checks passed for the selected mode.
- `FAIL`: a safety, static scan, command, artifact, or diagnostic blocker was found.
- `INSUFFICIENT_DATA`: required runtime evidence is missing or too stale to approve the selected mode.

## Modes

### quick

Use after normal code changes.

Checks:

- no-live IBKR unit-test guard
- static danger scan with allowlist
- `git diff --check`
- `tests/unit/exec`
- `tests/unit/services`
- IB reconnect regression `fast`

### full

Use before merge or after meaningful execution/reconciliation changes.

Checks:

- all `quick` checks
- `tests/unit/reconciliation`
- `tests/unit/reports`
- IB reconnect regression `full`
- required readiness and diagnostic artifacts are present and fresh

### pre_canary

Use before manual canary review.

Checks:

- all `full` checks
- bounded `SHADOW_ONLY` runtime
- bounded `PAPER_ONLY` runtime with fake paper broker events
- strategy execution reliability report
- approved-to-fill conversion report
- Vegas DLB edge measurement report
- paper/shadow soak gate report
- final audit report generation

Runtime duration can be shortened for local smoke checks:

```bash
AUDIT_GATE_RUNTIME_DURATION_SEC=10 \
AUDIT_GATE_RUNTIME_SAMPLE_COUNT=3 \
scripts/run_trading_system_audit_gate.sh pre_canary
```

Do not use shortened runtime settings as final canary evidence.

## Safety Behavior

The script sets `TRADING_TEST_ALLOW_LIVE_IBKR=0` for every command it runs. If the no-live guard fails, the script writes the audit report and stops without running later layers.

The static scan fails on unallowlisted appearances of risky surfaces such as direct `IB()`, `connectAsync`, broker order mutations, symbol-only ownership paths, auto reconciler mutations, broker aggregate position overwrite, and DB projection as broker truth. Known broker adapters, recovery tools, and legacy operator scripts are allowlisted in `tool/report/build_trading_system_audit_gate_report.py`.

## Interpreting Output

Start with `reports/audit_gate/audit_gate_latest.json`:

- `blockers`: exact reasons the gate did not pass.
- `warnings`: non-blocking evidence to review.
- `static_scan.findings`: unallowlisted risky code references.
- `runtime_artifacts.artifacts`: freshness and source status for readiness artifacts.
- `paper_shadow.diagnostics`: pre-canary diagnostic verdicts.
- `commands`: command summaries and return codes.

`PASS` never means live auto-launch is approved. The gate only approves the scope in `approved_scope`; pre-canary `PASS` approves manual canary review, not live trading or automatic canary launch.
