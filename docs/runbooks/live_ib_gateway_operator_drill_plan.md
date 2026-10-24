# Live IB Gateway Operator Drill Plan

## Purpose

- Validate the operator proof path under controlled explicit opt-in.
- Confirm `manual_required` can be cleared only by authorized proof.
- Confirm proof does not trade, cancel, modify, flatten, or assign unknown orders.

## Not a Trading Test

- This drill does not approve live trading.
- This drill does not approve unattended recovery.
- This drill does not approve auto-cancel, auto-flatten, or unknown-order cleanup.
- This drill only validates IB Gateway API proof and circuit closure behavior.

## Preconditions

- Operator is present and watching the drill.
- IB Gateway is logged in.
- IB Gateway API is enabled.
- Correct account is visible in IB Gateway.
- IB Gateway must not be restarted, killed, force-quit, or reopened by an agent/script. A Gateway restart requires manual password entry in this environment and is not automatically recoverable.
- No unexpected live positions or open orders exist, or every existing position/order is explicitly documented before the drill.
- Runtime freshness is current.
- `IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES=operator_cli`.
- Circuit state is inspected before the drill.
- `manual_required=true`, or a test environment has a safe fake/manual drill latch explicitly created.
- Disk space is checked.
- Logs and runtime artifact directories are writable.
- No other operator is running the proof.
- Paper/shadow strategy entry is disabled during the drill unless explicitly approved.

## Dry-Run Command

Dry-run is mandatory before any live proof. It must not construct an IB client:

```bash
TRADING_TEST_ALLOW_LIVE_IBKR=0 \
venv/bin/python scripts/operator_ib_gateway_recovery_proof.py \
  --source operator_cli \
  --host 127.0.0.1 \
  --port 4002 \
  --client-id 98
```

Expected dry-run result:
- `result=DRY_RUN`
- `live_mode=false`
- `proof.current_time=not_run`
- `proof.positions=not_run`
- `proof.open_orders=not_run`

## Live Proof Command

Do not run until the operator confirms every preflight item.

The live proof must use a disposable client id that is not used by market, exec, or master. Do not use any strategy client id.

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

## Success Criteria

- CLI result is `SUCCESS`.
- `proof.current_time=ok`.
- `proof.positions=ok`.
- `proof.open_orders=ok`.
- Circuit `manual_required=false`.
- Operator proof artifact is generated.
- Broker truth is live-backed after runtime resumes.
- Position safety is fresh.
- Production state is explainable.
- No unexpected orders, cancels, or modifies occurred.

## Failure Criteria

- Missing ack, unauthorized source, lease denied, or proof failed.
- Circuit remains `manual_required=true`.
- Production state remains `no_go`.
- No retry spam is performed.
- Gateway login/API state is inspected before another attempt.

## Post-Proof Verification Checklist

- Inspect `runtime/recovery/ib_gateway_operator_proof/latest.json`.
- Inspect `runtime/state/ib_gateway_connection_circuit.json`.
- Confirm no unexpected orders/cancels/modifies in broker UI and logs.
- Confirm live broker truth artifact is fresh and live-backed after runtime resumes.
- Confirm position safety artifact is fresh.
- Confirm production state is explainable before enabling any entry path.

## Rollback and Failure Handling

- If proof fails, leave `manual_required=true`.
- Do not rerun proof repeatedly.
- Do not restart, kill, force-quit, or reopen IB Gateway from automation. The operator must inspect the existing Gateway UI/login/API state.
- Do not restart `run_system.py` as the first recovery action unless runtime freshness requires it after external broker verification.
- Confirm IB Gateway login, API settings, trusted IP/API client settings, and client id conflicts.
- If unexpected broker state is found, stop the drill and move to manual incident handling.

## Post-Drill Artifacts

Capture:
- `runtime/recovery/ib_gateway_operator_proof/latest.json`
- `runtime/state/ib_gateway_connection_circuit.json`
- `runtime/readiness/production_state/production_state_latest.json`
- `runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json`
- `runtime/readiness/position_safety/position_safety_latest.json`
- relevant trader logs around proof time

## Final Go/No-Go Criteria

Operator may run live proof manually only when:
- baseline no-live regression is green,
- dry-run result is `DRY_RUN`,
- every precondition is checked,
- operator source is authorized,
- disposable client id is confirmed unused,
- expected broker positions and open orders are documented,
- strategy entry is disabled or explicitly approved for the drill window,
- the operator accepts that this proof performs no trading action and only validates IB API readiness plus circuit closure.
