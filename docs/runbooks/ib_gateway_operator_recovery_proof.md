# IB Gateway Operator Recovery Proof

This runbook is for the manual-required IB Gateway recovery latch. It is not a live drill script and it must not be used as an unattended reconnect loop.

For live drill preparation, use `docs/runbooks/live_ib_gateway_operator_drill_plan.md`.
For evidence capture, use `docs/reports/templates/live_ib_gateway_operator_drill_evidence_template.md`.
For a no-live smoke check, run `scripts/check_ib_operator_proof_dry_run.sh`.

## When to Use

- `runtime/state/ib_gateway_connection_circuit.json` has `manual_required=true`.
- IB Gateway is externally confirmed logged in and API enabled.
- Production state reports `manual_recovery_required` / `manual_gateway_api_recovery_required`.
- The operator intends to run one disposable broker API proof, not strategy trading.

## Before Running

- Verify no unexpected live positions or open orders outside the system.
- Verify runtime freshness; stale runtime modules remain production `no_go`.
- Verify the intended source, usually `operator_cli`, is present in `IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES`.
- Verify no other operator is running the proof; the durable lease will deny concurrent attempts.
- Do not restart `run_system.py` as the first action when the circuit is manually latched. First identify whether this is a Gateway/login/API state issue or a proof failure.
- Do not restart, kill, or reopen IB Gateway from an agent or script. IB Gateway restart requires manual password entry and is not automatically recoverable in this environment. Preserve the existing Gateway process, have the operator verify login/API state in the UI, then run the authorized proof once.

## Dry Run

Dry-run is the default and must not construct an IB client:

```bash
TRADING_TEST_ALLOW_LIVE_IBKR=0 \
venv/bin/python scripts/operator_ib_gateway_recovery_proof.py \
  --source operator_cli \
  --host 127.0.0.1 \
  --port 4002 \
  --client-id 98
```

Expected result: JSON with `result=DRY_RUN`, `live_mode=false`, `proof.*=not_run`.

Smoke check:

```bash
TRADING_TEST_ALLOW_LIVE_IBKR=0 scripts/check_ib_operator_proof_dry_run.sh
```

## Live Proof

Live proof requires all of the following:

- `--live`
- exact operator acknowledgement
- `TRADING_TEST_ALLOW_LIVE_IBKR=1`
- authorized source
- durable recovery lease

Live proof is manual operator-only. It is not part of unit, integration, fast, or full regression defaults.

```bash
export IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES=operator_cli
TRADING_TEST_ALLOW_LIVE_IBKR=1 \
venv/bin/python scripts/operator_ib_gateway_recovery_proof.py \
  --live \
  --ack "I understand this will perform a disposable IB API proof" \
  --source operator_cli \
  --host 127.0.0.1 \
  --port 4002 \
  --client-id 98 \
  --timeout-sec 10
```

The CLI writes JSON to stdout and to:

```text
runtime/recovery/ib_gateway_operator_proof/latest.json
```

## Success Criteria

- `result=SUCCESS`.
- `proof.current_time=ok`.
- `proof.positions=ok`.
- `proof.open_orders=ok`.
- `circuit_after.manual_required=false`.
- `manual_required_cleared=true`.
- Broker truth and position safety are live-backed and fresh after the runtime resumes.
- Production state is explainable; stale runtime freshness still keeps the platform `no_go`.

## Failure Behavior

- `result=FAILED` exits `2` and keeps `manual_required=true`.
- `result=MISSING_ACK`, `UNAUTHORIZED`, or `LIVE_OPT_IN_MISSING` exits `3` and does not construct IB.
- `result=LEASE_DENIED` exits `4` and does not construct IB.
- Inspect `runtime/recovery/ib_gateway_operator_proof/latest.json` and the circuit state before retrying.
- If the proof times out while TCP is open, treat it as a Gateway UI/login/API-state problem for the operator to inspect. Do not kill or restart IB Gateway as an automated recovery step.
- `4002 LISTEN` is only passive port evidence. It is not proof that IB API is ready.
- A proof that logs TCP `Connected` and then fails with `TimeoutError()` means IB accepted the socket but did not complete API startup. Leave `manual_required=true`.
- If `production_state_latest.json` contains a runtime PID, verify that PID is alive before treating the artifact as current runtime evidence.

## 2026-05-14 Incident Note

Observed state:

- Gateway process was listening on paper port `4002`.
- Gateway launcher log showed `Authentication complete`.
- Authorized operator proof with disposable client id `97` later succeeded, but runtime still failed to connect production market `clientId=10`.
- Targeted authorized verification of `clientId=10` reached TCP `Connected`, then timed out during API startup after 20 seconds.
- `check_paper_runtime.sh` previously reported `ib_api_ready=true` from the old disposable proof even while the Gateway circuit and production client proof showed `TimeoutError`; that was a false-ready diagnostic bug.

Operational conclusion:

- Do not change the port.
- Do not restart Gateway from automation.
- Do not clear the circuit manually.
- A disposable proof is Gateway/API evidence only; it is not sufficient proof that production `market=10`, `exec=20`, and `master=0` sessions can start.
- Runtime can be called healthy only after the durable circuit is closed and the production sessions connect successfully.
- Timeout fallback to adjacent production client ids is disabled by default for market/master sessions; owner lanes require stable client ids.

## Forbidden

- No repeated proof spam.
- No direct ad hoc connect scripts outside this CLI.
- No automatic clear of `manual_required`.
- No agent/script-initiated IB Gateway restart, kill, force quit, or reopen.
- No order, cancel, modify, auto-flatten, or unknown-order cleanup from this proof path.
- No treating DB projection as broker truth after proof.
