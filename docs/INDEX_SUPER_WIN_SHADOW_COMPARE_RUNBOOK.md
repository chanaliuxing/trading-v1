# Index Super Win Shadow Compare Runbook

## Goal

Use one command to compare:

- `qqq_intraday_001` observability snapshot
- `index_super_win_001` diag snapshot

The output answers:

1. Is `index_super_win` independent and entry-enabled?
2. If `qqq` is already live on the same symbol, did `index_super_win` block correctly?
3. Did `index_super_win` show a foreign-owner / broker-conflict reason instead of trying to take over?

## Tool

Command:

```bash
python tool/compare_index_super_win_shadow.py --symbol AAPL
```

Default files:

- `runtime/state/qqq_intraday_observability_qqq_intraday_001.json`
- `runtime/state/index_super_win_diag_snapshot_index_super_win_001.json`

Override example:

```bash
python tool/compare_index_super_win_shadow.py \
  --symbol TQQQ \
  --qqq-obs-path runtime/state/qqq_intraday_observability_qqq_intraday_001.json \
  --index-diag-snapshot-path runtime/state/index_super_win_diag_snapshot_index_super_win_001.json
```

JSON mode:

```bash
python tool/compare_index_super_win_shadow.py --symbol AAPL --json
```

Capture one sample:

```bash
python tool/capture_index_super_win_shadow_sample.py --symbol AAPL
```

Build session report:

```bash
python tool/build_index_super_win_shadow_session_report.py --day 2026-03-14
```

Run repeated session sampling:

```bash
python tool/run_index_super_win_shadow_session.py \
  --symbols TQQQ \
  --iterations 12 \
  --sleep-interval-sec 300
```

Run one daily same-symbol proof package:

```bash
python tool/replay/run_daily_dual_strategy_same_symbol_proof.py \
  --day 2026-03-24 \
  --replica-strategy-id index_super_win_replica_001
```

Check whether real same-symbol dated sessions are ready to prove:

```bash
python tool/replay/build_real_session_proof_readiness.py \
  --min-total-samples 1 \
  --min-blocked-samples 1
```

Run one-click readiness + batch proof pipeline:

```bash
python tool/replay/run_real_session_proof_pipeline.py \
  --replica-strategy-id index_super_win_replica_001 \
  --min-total-samples 1 \
  --min-blocked-samples 1
```

Default proof output:

- `runtime/state/index_super_win_same_symbol_proof/YYYY-MM-DD/`

Export disabled-state preflight inputs and run preflight:

```bash
bash scripts/automation/real_session_preflight_export.sh
```

Build final machine enablement checklist:

```bash
bash scripts/automation/real_session_enablement_checklist.sh
```

Run the ordered post-close bundle:

```bash
bash scripts/automation/real_session_postclose_bundle.sh
```

Output:

- `runtime/state/index_super_win_same_symbol_proof/final_enablement_checklist.json`

## Output Semantics

- `PASS`: isolation/parity guard behaved as expected
- `WARN`: not an immediate isolation failure, but state is not clean enough for cut-in
- `FAIL`: unsafe for cut-in

Exit code:

- `0`: all checks passed
- `1`: warnings only
- `2`: at least one failure

## Key Rules

### 1. Same-symbol live protection

If `qqq` is already live on the same execution symbol:

- `index_super_win` must not enter
- expected result is `blocked`
- expected reasons include:
  - `foreign_symbol_ownership_already_claimed`
  - broker/open-order conflict

If `index_super_win` still shows `entered`, treat it as `FAIL`.

### 2. Startup readiness

If `qqq` is ready but `index_super_win` is not entry-enabled:

- this is `WARN`
- check `startup_consistency.block_reason`
- common causes:
  - IB open-orders probe failed
  - broker snapshot read failed

### 3. Namespace leak

If `index` diag snapshot contains `qqqv1` namespace text:

- treat as `FAIL`
- this means isolation contract leaked into `index` runtime outputs

## Recommended Shadow Procedure

### A. Standalone

Run `index_super_win` alone and compare:

```bash
python tool/compare_index_super_win_shadow.py --symbol AAPL
python tool/compare_index_super_win_shadow.py --symbol MSFT
python tool/compare_index_super_win_shadow.py --symbol NVDA
```

Expected:

- no `FAIL`
- if no active `qqq` on those symbols, no same-symbol conflict findings

### B. Same-symbol with `qqq`

When `qqq` is live/shadow on the same execution symbol:

```bash
python tool/compare_index_super_win_shadow.py --symbol TQQQ
```

Expected:

- `PASS Same-symbol isolation held`
- no `FAIL`

### C. Cut-in Gate

Do not consider controlled live cut-in unless:

1. compare tool returns no `FAIL`
2. repeated runs during session are stable
3. multi-strategy smoke remains green
4. no foreign cancel / foreign flatten attempt is seen in logs

## Session Sampling Workflow

### 1. Capture during session

Run this every few minutes for the same symbol:

```bash
python tool/capture_index_super_win_shadow_sample.py --symbol TQQQ
```

Default output directory:

- `runtime/state/index_super_win_shadow_samples/YYYY-MM-DD/`

Each sample file contains:

- source snapshot paths
- compare findings
- exit code
- capture timestamp

### 1.1 Auto-run repeated sampling

Instead of manual capture, run:

```bash
python tool/run_index_super_win_shadow_session.py \
  --symbols TQQQ,AAPL \
  --iterations 12 \
  --sleep-interval-sec 300
```

Outputs:

- `session_manifest.json`
- `session_report.json`
- one sample file per symbol per iteration

### 2. Build end-of-session report

```bash
python tool/build_index_super_win_shadow_session_report.py --day 2026-03-14
```

Default output:

- `runtime/state/index_super_win_shadow_samples/YYYY-MM-DD/session_report.json`

### 3. Readiness rule

`session_report.json` exposes:

- `ready_for_cutin`
- per-symbol `exit_code_counter`
- per-symbol `finding_level_counter`
- top repeated warning/fail messages

Treat session as not ready if:

- any sample has `exit_code=2`
- any symbol has repeated `WARN` due to startup/broker inconsistency
- same-symbol live protection ever fails

## Daily Proof Workflow

After one shadow session day is captured, run:

```bash
python tool/replay/run_daily_dual_strategy_same_symbol_proof.py \
  --day 2026-03-24 \
  --replica-strategy-id index_super_win_replica_001
```

This produces one proof package containing:

- `qqq_actual_traces/`
- `replica_actual_traces/`
- `replica_same_symbol_proof/`
- `dual_strategy_same_symbol_proof.json`
- `dual_strategy_foreign_safety_report.json`
- `dual_strategy_enablement_attestation.json`

Interpretation:

- `qqq_expectation_ok=true`: whenever the captured sample says `qqq` was live on the same symbol, a nearby `qqq` trace was exported
- `replica_same_symbol_proof_green=true`: replica-side expected trace and actual trace matched for the captured session
- `overall_green=true`: both sides exported cleanly and replica same-symbol proof matched

## Real Session Readiness Workflow

Before trying to prove real same-symbol days, run:

```bash
python tool/replay/build_real_session_proof_readiness.py \
  --min-total-samples 1 \
  --min-blocked-samples 1
```

Output:

- `runtime/state/index_super_win_same_symbol_proof/real_session_proof_readiness.json`

Interpretation:

- `has_any_ready_day=true`: at least one dated session directory has enough inputs to run real proof
- `ready=false` with reasons means the machine can explain why proof cannot run yet:
  - `insufficient_total_samples`
  - `insufficient_blocked_samples`
  - `missing_audit_files`
  - `no_same_symbol_qqq_samples`

## One-Click Real Proof Pipeline

Once dated session samples exist, run:

```bash
python tool/replay/run_real_session_proof_pipeline.py \
  --replica-strategy-id index_super_win_replica_001 \
  --min-total-samples 1 \
  --min-blocked-samples 1
```

Behavior:

1. runs real-session readiness first
2. if no ready day exists, stops with `pipeline_status=readiness_only`
3. if ready days exist, runs batch dual proof over those days

Output files:

- `runtime/state/index_super_win_same_symbol_proof/real_session_proof_readiness.json`
- `runtime/state/index_super_win_same_symbol_proof/real_session_dual_proof_batch_report.json`
- `runtime/state/index_super_win_same_symbol_proof/real_session_proof_pipeline.json`

## Automation Wrappers

Intraday same-symbol sample capture:

```bash
bash scripts/automation/real_session_shadow_capture.sh
```

Post-close proof pipeline:

```bash
bash scripts/automation/real_session_proof_pipeline.sh
```

Post-close proof daily summary:

```bash
bash scripts/automation/real_session_proof_daily_summary.sh
```

Install launchd jobs:

```bash
bash scripts/setup_real_session_proof_launchd.sh
```

Remove launchd jobs:

```bash
bash scripts/uninstall_real_session_proof_launchd.sh
```

Default launchd schedule:

- `com.trading.real_session.shadow_capture.intraday`: `09:35`
- `com.trading.real_session.proof.postclose`: `16:12`
- `com.trading.real_session.proof.summary.postclose`: `16:15`

Default behavior:

- intraday capture is fail-open and only records shadow samples
- post-close pipeline stops quietly at `readiness_only` if real dated samples are still missing
- post-close daily summary surfaces `needs_more_real_samples=true` instead of throwing noisy failures
