# IBKR Reconnect Storm + Stuck-Reconnect Runbook (2026-04)

## 1) Incident Summary

Initial problem:
- Off-hours and restart windows triggered repeated IB reconnect attempts.
- IBKR/Gateway eventually rejected connections or dropped sessions.

Regression introduced during hardening:
- After system boot, if IB disconnected once, market/execution paths could get stuck in long timeout loops and never recover until IB Gateway was restarted.

Impact:
- Market data and execution degraded or unavailable.
- ClientId churn and auxiliary sessions increased pacing/reject risk.

## 2) Root Causes (First Principles)

### A. Multiple connection factories broke global control
- Reconnect hardening assumed one global gate, but some paths created direct `IBInsyncAdapter` sessions outside `IBConnectionManager`.
- Result: “gated” and “ungated” reconnect attempts existed at the same time.

### B. Gatekeeper contract was violated in callers
- Denial from gatekeeper was sometimes treated as “log only” and code still attempted real connect.
- Some denial paths were incorrectly reported as real connect failures, inflating shared backoff.

### C. Readiness and handshake were conflated
- TCP port ready != API handshake healthy.
- During Gateway restart/recovery, socket could accept TCP while `connectAsync` still timed out.
- Without fallback client/session reset, system repeatedly retried stale state.

### D. Shared IB lifecycle invalidation was incomplete
- On disconnect, stale shared `IB` objects could survive too long.
- Reconnect loop reused unhealthy session/client context instead of forcing clean re-init quickly enough.

### E. Diagnostic paths amplified load
- Fresh-probe / owner-cancel diagnostics can create extra IB sessions.
- If throttling is too loose (especially in RTH), reconciler cadence can turn anomalies into request amplification.

### F. Observability gaps delayed diagnosis
- Missing chain-level logs made it hard to distinguish:
  1) gatekeeper denial,
  2) port not ready,
  3) handshake timeout,
  4) stale shared client reuse,
  5) ungated side-channel sessions.

## 3) Fix Baseline (What must stay true)

### Rule 1: Single owner for IB sessions
- `IBConnectionManager` is the only factory for long-lived market/exec sessions.
- No silent fallback that creates local adapters in services.

### Rule 2: Gatekeeper semantics are strict
- `acquire() == False` means “NO connect attempt now”.
- Denial is not a connection failure. Do not call `report_failure()` for denial.
- Backoff sleep must be outside gate lock.

### Rule 3: Separate readiness probe from connect budget
- Readiness probes are serialized but must not consume reconnect attempt budget.
- Real connection attempts are rate-limited.

### Rule 4: Startup must support degraded mode
- Startup should not hard-fail just because IB is temporarily unavailable.
- Bring process up; allow background recovery to reconnect.

### Rule 5: On disconnect, aggressively invalidate stale shared market session
- Clear/break stale shared `IB` references and event bindings before reconnect.
- On timeout, recreate client and allow bounded clientId fallback.

### Rule 6: Diagnostic paths need low-latency but non-zero backpressure
- Keep RTH throttles small, not zero.
- Add in-flight dedupe per order/client anomaly key to prevent per-second duplicate probes.

### Rule 7: Full-chain observability is mandatory
- Log start/end of each connect flow with host/port/clientId/ib_id/caller.
- Log gatekeeper decision, readiness state, and timeout fallback transitions.
- Keep benign IB warnings (e.g. 2107 HMDS inactive) at low noise level.

## 4) Fast Triage Decision Tree

When “IB is up but system cannot reconnect”:

1. Port readiness
- Check if target port accepts TCP.
- If closed/refused: infra/config issue, not clientId issue.

2. API handshake health
- If TCP open but `connectAsync` times out repeatedly, treat as handshake-layer unhealthy.
- Trigger clean disconnect/recreate flow; allow bounded clientId fallback.

3. Session factory uniqueness
- Verify no additional adapter factories are creating sessions outside manager.
- Check for multiple clientIds rapidly appearing from one process.

4. Gatekeeper behavior
- Confirm denied paths do not attempt connect.
- Confirm denial does not increment failure backoff.

5. Shared lifecycle
- Confirm disconnect event invalidates shared market session (`_market_ib=None`, events unbound).

6. Diagnostic amplification
- Confirm probe throttles and in-flight dedupe are active in RTH and off-hours.

## 5) Log Events to Watch (High Signal)

- Gatekeeper:
  - `reconnect_gatekeeper_proceed`
  - `reconnect_gatekeeper_rate_limited`
  - `reconnect_gatekeeper_backoff`
  - `reconnect_gatekeeper_probe_not_ready`
  - `reconnect_gatekeeper_probe_ready`
  - `reconnect_gatekeeper_failure`
  - `reconnect_gatekeeper_reset`
- Market connect flow:
  - `ib_market_connect_flow_begin`
  - `ib_connect_attempt_begin`
  - `ib_connect_timeout_retry`
  - `ib_connect_timeout_recreate_client`
  - `ib_market_connected`
  - `ib_market_shared_session_invalidated`
- Adapter/service:
  - `ib_market_data_adapter_request_shared_ib`
  - `market_data_service_ib_connection_lost`

## 6) Guardrails for Future AI Changes

Before merge, enforce this checklist:
- No new direct `IBInsyncAdapter(...)` creation outside approved factory/utility path.
- Any new reconnect caller must pass gatekeeper contract tests:
  - denial => no connect attempt,
  - denial => no `report_failure`,
  - failure => `report_failure`,
  - success => `report_success`.
- Startup tests include “IB unavailable at boot but process still starts”.
- Restart tests include “connected -> IB restart -> reconnect succeeds without process restart”.
- RTH anomaly tests verify diagnostic probes are bounded (no 1s infinite reconnection churn per anomaly key).

## 7) Practical Defaults (Safe + Real-time)

- Keep reconnect gate rate limit conservative in off-hours.
- Keep RTH diagnostic throttle low but non-zero (typical floor: 1-2s) plus in-flight dedupe.
- Prefer delayed-frozen market data mode off-hours.
- Never treat code 2107 HMDS inactivity as reconnect failure trigger.

## 8) 2026-04-18 Runtime Owner Fix (run_system equivalence)

Objective:
- Ensure runtime recovery is equivalent to cold start readiness path in `run_system.py`.
- Remove dependency on `market_data_service` as the recovery owner for `master/exec`.

What was implemented:
- `IBConnectionManager` now owns a single runtime recovery loop:
  - Triggered by disconnect of any role (`market/exec/master`).
  - Loop calls `wait_until_ready(require_exec=True, require_master=True)` until all are ready.
- `master` session now has explicit disconnect binding and invalidation:
  - On disconnect, clear master truth state and trigger manager recovery.
- `exec` adapter now reports disconnect callback to manager in managed mode:
  - Manager receives event and starts/continues recovery loop.
- `wait_until_ready` now supports `initiator`:
  - During active manager recovery, non-manager callers become passive waiters.
  - Prevents competing runtime owners.

Key runtime evidence pattern after fix:
- `ib_runtime_recovery_triggered`
- `ib_runtime_recovery_cycle_begin`
- reconnect chain for all 3 roles:
  - `ib_market_connected`
  - `ib_exec_connected`
  - `ib_master_connected`
  - `ib_master_truth_snapshot_complete`
- `ib_runtime_recovery_cycle_complete`

Operational implication:
- `run_system` startup gate (`wait_until_ready(require_exec=True, require_master=True)`) remains the canonical readiness contract.
- Runtime now follows the same contract under manager ownership, instead of relying on market-service side effects.

## 9) 2026-04-21 Regression Addendum

This incident was not caused by IB Gateway client config drift. The trading-system side had real reconnect regressions relative to the earlier hardening work.

Confirmed regressions:
- `get_master_ib()` bypassed the shared reconnect gatekeeper, so `master` could start a `client_id=0` handshake while `market` already owned the active slot.
- Bounded timeout fallback in `_connect_ib_with_retry()` had been removed, so repeated `apiStart TimeoutError()` stayed on one poisoned session path instead of recreating the client and trying the next bounded fallback.
- `ConnectionRefused` / `connect call failed` during Gateway restart was counted as a normal connect failure, which inflated shared backoff exactly when the port was still coming back.
- `run_system.py` called `wait_until_ready(require_master=True)` but its startup pass/fail check still only looked at `market/exec`; `master/master_truth` could fail and the process could still log startup success.

Current contract after remediation:
- `market`, `exec`, and `master` all obey the same gatekeeper contract.
- Port-not-ready failures increase readiness backoff only; they do not count as real handshake failures.
- Timeout retries recreate the IB client and may use bounded clientId fallback.
- `run_system` startup readiness requires all of:
  - `market_connected`
  - `exec_connected`
  - `master_connected`
  - `master_truth_complete`

Operator rule:
- Do not edit IB Gateway client config to solve this class of incident.
- Fix or validate trading-system reconnect ownership first.
- When checking whether Gateway is really back, prefer a minimal API probe over a full `run_system.py` restart loop.

High-signal logs for this regression family:
- `ib_master_gatekeeper_acquire_begin`
- `ib_master_gatekeeper_denied`
- `ib_connect_timeout_client_id_fallback`
- `reconnect_gatekeeper_port_not_ready_failure`
- `run_system_startup_ib_not_ready`
- `ib_wait_until_ready_short_circuit`

Pre-merge acceptance for future reconnect changes:
- Restart scenario must pass: `connected -> IB restart -> reconnect succeeds without process restart`.
- Startup gate must fail closed when `master_truth_complete=False`.
- No code path may start a long-lived IB session without going through `IBConnectionManager`.

## 10) 2026-04-21 Production Remediation Record

What was landed in code:
- `get_master_ib()` now goes through the same reconnect gatekeeper path as `market`.
- `_connect_ib_with_retry()` keeps the bounded timeout fallback path:
  - timeout can recreate the `IB` client,
  - bounded clientId fallback is allowed,
  - port-not-ready is treated separately from handshake failure.
- `run_system.py` startup readiness now fails closed unless all of these are true:
  - `market_ib_connected=True`
  - `exec_ib_connected=True`
  - `master_ib_connected=True`
  - `master_truth_complete=True`
- `services/market/writer/ch_writer.py` now self-heals on:
  - assignment loss beyond threshold,
  - commit-time `No partitions assigned`,
  - consume-loop heartbeat/coordinator/session-expired style failures.
- `OrderTruthReconciler` matches `ib_perm_id` first and only trusts `broker_order_id` inside the current session window.
- `SafetyKernel` writes fills to `position_ledger`, then syncs `core.core_position` from `position_ledger_current`.

Commits for this incident family:
- `1fef34fb` `ibkr: restore reconnect gate and startup truth checks`
- `a67edc2a` `kafka: harden ch writer consumer recovery`
- `ec802255e` `reconcile: harden order truth and position sync`

Runtime acceptance observed on 2026-04-21:
- New `run_system.py` process started successfully after code rollout.
- High-signal healthy logs:
  - `run_system_startup_ib_ready` with `master_ib_connected=True` and `master_truth_complete=True`
  - `ib_master_truth_snapshot_complete`
  - `ALL SERVICES STARTED - SYSTEM RUNNING`
  - steady `ib_exec_probe_ok`
  - steady `ib_market_probe_ok`
- Expected-but-benign startup behavior:
  - `ib_master_gatekeeper_denied` may appear briefly while `market` owns the slot during startup.
  - This is acceptable if it is followed by normal `master_connected` and `startup_ib_ready`.

Operator validation after any future IB restart:
1. Confirm `run_system.py` stays running; do not assume lack of immediate reconnect means Gateway config drift.
2. Check logs for this exact recovery shape:
   - `ib_market_connected`
   - `ib_exec_connected`
   - `ib_master_connected`
   - `ib_master_truth_snapshot_complete`
   - `run_system_startup_ib_ready`
   - `ALL SERVICES STARTED - SYSTEM RUNNING`
3. Confirm there is no fresh stream of:
   - `run_system_startup_ib_not_ready`
   - `Bar data fallback failed`
   - `Kafka consumer assignment lost beyond threshold`
   - repeated `ib_runtime_recovery_cycle_begin` without a matching `...complete`
4. Only after the trading-system path is validated should Gateway UI actions be considered.

## 11) 2026-04-21 Shutdown / Restart Runbook Correction

This was the other operational lesson from the same incident family: the restart method matters.

What we confirmed in production on 2026-04-21:
- `screen -X quit` is not a safe `run_system.py` restart primitive for this stack.
- It can close the owning `screen` session while the underlying `run_system.py` login shell / Python process is still alive.
- If a new instance is started immediately after that, the single-instance lock fires:
  - `run_system_single_instance_lock_failed`
- This is not an IB Gateway failure and should not trigger Gateway-side changes.

Correct controlled restart procedure:
1. Find the actual `run_system.py` process:
   - `pgrep -af 'python run_system.py'`
2. Send `SIGTERM` to that process, not to the screen session only.
3. Wait until `pgrep -af 'python run_system.py'` returns nothing.
4. Confirm the shutdown path has completed in logs.
5. Start a fresh detached session:
   - `screen -dmS trading_run_system zsh -lc 'cd . && exec ./venv_ml/bin/python run_system.py >> /workspace/grafana-stack/logs/trader.log 2>&1'`

Shutdown-path fixes now present in code:
- `run_system.py` shuts down shared `IBConnectionManager` before observability/network clients.
- `IBConnectionManager.shutdown()` now:
  - cancels and awaits `ib_runtime_recovery_loop`,
  - disposes the managed exec adapter cleanly,
  - gives the event loop one final cleanup slice before returning.
- `IBInsyncAdapter.stop()` now unbinds events before disconnect and supports dispose-on-shutdown.
- `services/obs/writers.py` now gives `aiohttp` sessions a small cleanup slice after `close()`.
- `tests/unit/services/test_ib_connection_manager.py` now auto-shuts down managers created inside tests, preventing false teardown noise.

Observed result after these shutdown fixes:
- A controlled `SIGTERM` restart no longer reproduced fresh:
  - `RuntimeError: Event loop is closed`
  - `Unclosed client session`
- Restarted process again reached:
  - `run_system_startup_ib_ready`
  - `ALL SERVICES STARTED - SYSTEM RUNNING`

Remaining note:
- A separate aiokafka shutdown warning (`Task was destroyed but it is pending!`) can still come from Kafka producer teardown under degraded broker timing.
- That is a Kafka producer cleanup concern, not an IB reconnect concern, and must be debugged separately from Gateway reconnect issues.

Do not do these during triage:
- Do not edit IB Gateway/TWS config files for this incident class.
- Do not treat a brief gatekeeper denial as proof the reconnect fix regressed.
- Do not clear production DB rows before first checking whether the live issue is actually market-data loss rather than order truth noise.

## 12) 2026-04-23 Gateway Lifecycle Recovery Finding

Observed production pattern:
- IB Gateway may restart, disconnect, freeze, or expose a half-open API listener at any time.
- The daily auto-restart window is only one reproducible trigger, not the boundary of the problem.
- During any restart/recovery event, the old API listener may briefly appear as TCP-open but not API-handshake-ready, or may transition through `ConnectionRefused`.
- `run_system.py` can remain alive and correctly fail closed, but still create an over-eager recovery pattern:
  - `market_disconnect_event` / `master_disconnect_event` / `exec_disconnect_event` trigger runtime recovery almost immediately.
  - `market` attempts the first connect before Gateway has finished authentication and API listener startup.
  - `ConnectionRefused` increments readiness failures and pushes the shared off-hours backoff out to tens of seconds.
  - If the Gateway API listener wedges in a half-open state, system-side retries can continue indefinitely until Gateway is restarted manually.

Concrete evidence from the 2026-04-22/23 incident:
- Gateway restart began at `2026-04-23T00:02:05Z`.
- The system attempted API reconnect at `2026-04-23T00:02:01Z` and again at `00:02:11Z`, before Gateway authentication completed at about `00:02:20Z`.
- Recovery eventually succeeded only after Gateway API became available:
  - `ib_master_connected` / `ib_master_truth_snapshot_complete`
  - `ib_market_connected`
  - `ib_exec_connected`
  - `ib_runtime_recovery_cycle_complete`
- In the earlier stuck case, a separate probe showed the API socket was half-open:
  - `lsof` showed `*:4002 (LISTEN)`
  - independent `ib_insync.connectAsync()` timed out
  - manual Gateway restart cleared the condition and the existing `run_system.py` recovered without a process restart.

Root cause classification:
- This is not a strategy, Kafka, ClickHouse, or order-management problem.
- This is not solved by restarting `run_system.py`.
- This is a missing quiet-period protocol in the broker supervisor layer:
  - the code distinguishes port-not-ready vs handshake timeout,
  - but it does not enforce a universal broker recovery quiet period after any disconnect,
  - and repeated TCP/API probes can fill the IB Gateway API accept backlog.
- Do not auto-restart IB Gateway from code. Gateway restart/login may require credentials or manual authentication.

Required policy change:
1. Add a universal broker recovery quiet period after any IB disconnect, Gateway process restart, TCP refusal, or API handshake timeout.
   - The daily auto-restart window may pre-arm this mode, but the same mode must trigger from any runtime disconnect event.
   - End only after an independent API probe succeeds, not merely when TCP 4002 listens.
   - During the quiet period, all trading paths remain fail-closed/paused.
   - Recovery loops must not run TCP probes or API probes while quiet period is active.
   - After quiet period expires, run one independent API probe; if it fails, re-enter a longer cooldown.
2. Add Gateway-level diagnosis when API handshake remains unhealthy after the quiet/grace period.
   - If TCP is open but independent `ib_insync` probe times out repeatedly, classify as `gateway_api_half_open`.
   - If TCP is refused beyond the expected startup grace period, classify as `gateway_api_not_started`.
   - If `netstat -Lan` shows the 4002 listen queue at capacity (for example `50/0/50`), classify as `gateway_api_accept_backlog_full`.
   - Do not restart `run_system.py` to fix this. Stop system probes, alert operator, and wait for manual Gateway/API recovery.
   - After the independent API probe succeeds, let `IBConnectionManager` recover market/exec/master normally.
3. Telegram alerting must distinguish:
   - `IB recovery quiet period entered`
   - `Gateway API ready, recovery starting`
   - `Gateway API half-open/backlog full, manual Gateway action required`
   - `Recovery complete: market/exec/master/master_truth all ready`
   - `Recovery failed: manual intervention required`
4. Evidence gate after every broker recovery:
   - `run_system.py` process still alive.
   - independent API probe connects with a disposable client id.
   - `ib_market_connected`
   - `ib_exec_connected`
   - `ib_master_connected`
   - `ib_master_truth_snapshot_complete`
   - `ib_runtime_recovery_cycle_complete`

Implementation backlog:
- Add `IBGatewaySupervisor` or extend `SystemHealthController` with a Gateway lifecycle diagnostic sub-controller.
- Add config for:
  - optional daily Gateway restart pre-arm window,
  - universal disconnect quiet period,
  - API-probe grace period,
  - API-probe failure cooldown,
  - manual escalation threshold.
- Add a standalone probe command that does not reuse in-process `IB` objects.
- Add integration/chaos tests:
  - connected -> planned Gateway daily restart -> quiet period -> API probe success -> market/exec/master/master_truth ready without restarting `run_system.py`.
  - connected -> unplanned Gateway kill -> manual restart/login -> API probe success -> full recovery.
  - connected -> TCP 4002 half-open/API handshake timeout -> quiet period/cooldown -> no accept-backlog amplification.
  - connected -> IB 1100/1102 data-center flap -> no Gateway restart unless independent API probe remains unhealthy past threshold.

---

Owner note:
- If future changes improve this policy, update this runbook and both AI memory files in the same PR.

## 13) 2026-05-08 Daily Restart Stale-Circuit Finding

Observed after the 19:45 ET IB restart:
- Independent disposable API probe succeeded:
  - host `127.0.0.1`, port `4002`, clientId `97`
  - `reqCurrentTime` returned successfully
  - managed account returned `DUO112752`
- Platform artifacts still showed startup not ready:
  - `runtime/readiness/production_state/production_state_latest.json`
  - `status=HOLD`, `go_no_go=no_go`
  - reason `waiting_for_ib_broker_readiness`
- Persistent Gateway circuit was still open:
  - `runtime/state/ib_gateway_connection_circuit.json`
  - reason `TimeoutError: ib_api_handshake_not_ready`
  - source `market`
- `run_system.py` logs showed the startup gate sleeping on the persistent circuit:
  - `ib_gateway_connection_circuit_connect_blocked`
  - `ib_wait_until_ready_gateway_circuit_open`
  - `run_system_startup_ib_not_ready_retrying`

Root cause:
- Gateway had recovered enough for a fresh API session, but the platform was still honoring a previously armed persistent circuit.
- The persistent circuit blocked both real connections and the readiness path, so `wait_until_ready` waited for the long cooldown instead of proving the API had recovered.
- This creates a false HOLD state after daily restart: broker API is reachable, but platform recovery remains delayed by stale local state.

Superseded code contract:
- This finding was correct for stale local circuit state, but the first remediation was too permissive.
- Allowing disposable probes while the persistent circuit was open turned the circuit into a low-frequency active login tester.
- The 2026-05-09/10 audit showed this still permitted multiple recovery callers to touch IB after `manual_gateway_api_recovery_required`.
- The current production contract is now defined in section 14.

Daily 19:45 restart acceptance:
1. Independent probe must pass:
   - disposable client id connects,
   - `reqCurrentTime` succeeds,
   - managed accounts are visible.
2. Persistent circuit must close after the probe:
   - `runtime/state/ib_gateway_connection_circuit.json` should show `state=closed` or `open_until=null`.
3. Startup/runtime readiness must converge:
   - `ib_market_connected`
   - `ib_exec_connected`
   - `ib_master_connected`
   - `ib_master_truth_snapshot_complete`
4. Production state must leave broker-wait HOLD:
   - no `waiting_for_ib_broker_readiness`
   - no `ib_gateway_connection_circuit_open` blocker.
5. If the independent probe passes but production state stays HOLD, treat it as a platform state/circuit bug, not an IB Gateway connectivity bug.
6. If broker sessions are ready but production state reports `market_data_*_stale` or `bar_pipeline_stale` outside RTH, treat that as a data-plane freshness/readiness policy bug, not as an IB connection failure.

Operational warning from the same validation:
- Historical stderr contained `No space left on device` from ClickHouse/logging.
- Disk pressure can prevent readiness artifacts, logs, and ClickHouse temporary files from being written.
- Before blaming IB after a restart, verify disk has enough headroom:
  - `df -h / /opt/homebrew/var/lib/clickhouse .`

## 14) 2026-05-11 Manual-Required and Lease-Based Recovery Contract

Accepted review:
- The 2026-05-09/10 outage was not a single IB connection bug.
- After `gateway_api_half_open` and `manual_gateway_api_recovery_required`, several runtime paths still performed readiness/probe/reconnect attempts.
- A halted production state must stop strategy entry, but IB API login attempts must be limited to a single explicitly authorized recovery owner.
- The 19:49 ET incident showed the opposite failure mode: a non-manual stale circuit blocked all recovery proof until cooldown expiry even after IB Gateway was ready.

Current code contract:
- `IBGatewayRecoveryCoordinator` is the policy boundary for disposable IB API recovery probes.
- Callers that want to perform a real `connectAsync` recovery probe must be authorized by `IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES`.
- `runtime/state/ib_gateway_connection_circuit.json` has a durable `manual_required` flag.
- `manual_required=true` keeps the Gateway circuit open indefinitely; `open_until` expiry is ignored until a successful authorized recovery proof clears it.
- Non-manual circuit open blocks real long-lived market/exec/master connects, but does not block authorized recovery proof.
- `IBConnectionManager.wait_until_ready()` may call `_probe_api_handshake_ready()` while the Gateway circuit is open only as `*:circuit_recovery_probe`.
- Circuit-open recovery proof must acquire the durable process-shared recovery probe lease before constructing `IB()` or calling `connectAsync`.
- Default recovery probe lease settings:
  - `IB_GATEWAY_RECOVERY_PROBE_WHILE_OPEN_SEC=30`
  - `IB_GATEWAY_RECOVERY_PROBE_LEASE_SEC=20`
- `_probe_api_handshake_ready()` itself must ask `IBGatewayRecoveryCoordinator` before constructing `IB()` or calling `connectAsync`.
- If the coordinator rejects the caller, the circuit is manual-required, or another recovery lease is active, `_probe_api_handshake_ready()` returns `False` without touching IB.
- Auxiliary broker observability probes, including fresh-order probes and owner-client cancel probes, must skip real IB API login while the Gateway circuit is open.
- Pre-connect API probe failure must call `report_aborted`, not `report_failure`, because the probe path already records the Gateway circuit failure through the coordinator.
- A repeated API handshake failure emits `ib_gateway_api_unhealthy`, records `manual_gateway_api_recovery_required`, and opens the durable manual-required circuit.
- A successful authorized probe records circuit success through the coordinator and clears `manual_required`.

Operational rule:
1. During the daily 19:45 ET restart or any unplanned IB outage, let the platform fail closed.
2. If the circuit is open but `manual_required=false`, the platform should run only lease-protected recovery proof and should not wait the full stale cooldown once IB is back.
3. If the circuit enters `manual_required`, do not restart `run_system.py` as the first action.
4. Manually verify IB Gateway/TWS is logged in and API is enabled.
5. Run exactly one independent recovery proof from the designated operator path.
6. Confirm the circuit is closed:
   - `runtime/state/ib_gateway_connection_circuit.json`
   - `state=closed`
   - `manual_required=false`
7. Confirm runtime readiness:
   - independent `reqCurrentTime` succeeds,
   - `production_state_latest.go_no_go=go`,
   - `safety_go_no_go=go`,
   - `entry_go_no_go=go`,
   - `broker_truth_snapshot_latest.go_no_go=go`,
   - no broker live positions or open orders unless they are expected and protected,
   - no active manual intervention locks.

2026-05-11 live validation after remediation:
- Independent IB API probe connected to `127.0.0.1:4002` with clientId `99` and `reqCurrentTime` succeeded.
- `runtime/state/ib_gateway_connection_circuit.json` showed `state=closed`, `manual_required=false`, `consecutive_failures=0`.
- `runtime/readiness/production_state/production_state_latest.json` showed `go_no_go=go`, `safety_go_no_go=go`, `entry_go_no_go=go`, `failure_count=0`.
- `runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json` showed `go_no_go=go`, no blockers, no live broker positions, no live broker open orders, no nonzero core positions, and no open core orders.
- `runtime/readiness/position_safety/position_safety_latest.json` showed `go_no_go=go`, `broker_position_count=0`, `flat_live_order_count=0`, `active_manual_lock_count=0`.
- Production strategy heartbeats for `qqq_intraday_001`, `stocks_kernel_smh_001`, `stocks_kernel_spy_001`, option intraday strategies, and configured 1DTE strategies were `tradable=true` with no blockers.

Remaining production hardening:
- Promote `IBGatewayRecoveryCoordinator` from policy boundary to an active low-frequency supervisor with explicit operator ack workflow.
- Convert all external/manual IB executions into the same fill-attribution/quarantine path as clientId 20 executions.
- Clean historical option ledger current-state rows that offset across null and deterministic lot ids, then add a constraint or migration rule preventing new active null-lot production rows.

## 15) 2026-05-14 TCP Listen but API Handshake Timeout

Observed on 2026-05-14 ET after an operator-visible Gateway login:
- `lsof -nP -iTCP:4002 -sTCP:LISTEN` showed the IB Gateway Java process listening on `*:4002`.
- IB Gateway `launcher.log` showed password authentication and `Authentication complete` at `2026-05-14 11:06:00 ET`.
- The authorized operator proof using disposable client ids `97` and `99` reached TCP `Connected`, then timed out waiting for IB API startup:
  - `API connection failed: TimeoutError()`
  - `proof.api_handshake=failed`
  - `proof.current_time=failed`
  - `manual_required_cleared=false`
- `runtime/state/ib_gateway_connection_circuit.json` remained `manual_required=true`, `state=proof_failed`.
- `production_state_latest.json` could still contain a recent `runtime_pid`, but that PID was no longer alive. Treat artifact PID liveness as evidence, not decoration.

Root cause classification:
- Paper port `4002` was correct.
- TCP listening was not sufficient evidence of broker API readiness.
- Restarting `run_system.py` would not fix this state.
- Clearing the local circuit without a successful proof would create a false-ready trading system.
- Repeated dry-run/check diagnostics must not open sockets or add pressure to the Gateway API listener.

Contract update:
- `scripts/check_paper_runtime.sh` must use passive `lsof` port observation while `manual_required=true`; it must not actively connect to IB in that state.
- `scripts/operator_ib_gateway_recovery_proof.py` must not touch IB for dry-run, missing ack, missing live opt-in, or unauthorized source.
- Authorized live proof must use one disposable IB API session as the proof. Do not add a separate TCP pre-probe before the IB API session.
- `scripts/check_ib_operator_proof_dry_run.sh` must write to a temporary artifact path so dry-run cannot overwrite production proof evidence.
- Startup diagnostics must distinguish `ib_gateway_manual_required` from `ib_api_handshake_failed_manual_required` when the port is passively listening and the last proof failed with `TimeoutError`.
- Runtime readiness checks must report whether the `production_state` runtime PID is alive.

Operator action:
1. Do not restart, kill, force-quit, or reopen IB Gateway from automation.
2. Operator inspects the existing Gateway UI for login/API settings/client-id prompts.
3. Run exactly one authorized proof after operator inspection.
4. Start or resume runtime only after proof succeeds and clears `manual_required`.

Additional 2026-05-14 finding after the operator proof:
- Disposable client id `97` succeeded at `2026-05-14T11:59:25 ET`.
- The runtime started afterward skipped its pre-connect disposable probe because the operator proof was recent.
- The real market session then failed on production `clientId=10` with `TimeoutError`, and fallback `clientId=11` also timed out.
- A targeted authorized verification of production `clientId=10` at `2026-05-14T12:08:57 ET` reached TCP `Connected`, then timed out after 20 seconds before API startup completed.
- Therefore, the old acceptance rule was incomplete: "one disposable client can connect" is not the same as "production sessions can connect."

Root cause update:
- Port `4002` was still correct.
- IB Gateway was not restarted by the system during this verification.
- The platform diagnostic was wrong to report `ib_api_ready=true` from an old disposable success while the durable circuit and production client proof showed a later failure.
- Runtime timeout fallback from production `clientId=10` to adjacent ids is unsafe for owner-scoped trading identity and is disabled by default.

Updated acceptance:
1. `check_paper_runtime.sh` must show old disposable proof as `matches_circuit=false` when a later circuit failure exists.
2. `ib_api_ready` must be false unless the durable circuit is currently `closed`, `manual_required=false`, and `last_proof_status=PROOF_SUCCEEDED`.
3. Production readiness still requires the actual market/exec/master sessions to connect; disposable proof is only a Gateway/API prerequisite.
4. If production `clientId=10` times out while no runtime process is connected, classify the issue as `production_client_api_handshake_failed`, not as a port problem and not as a reason to change `4002`.

## 16) 2026-05-11 Stale-Circuit Recovery Regression

Incident pattern:
- IB Gateway restarted at `2026-05-11 19:45 ET`.
- The first disposable API pre-connect probe failed with `TimeoutError: ib_api_handshake_not_ready`.
- The persistent Gateway circuit opened and startup retry at `19:49 ET` saw `ib_gateway_circuit_open=True`, then waited for the circuit instead of probing recovery.
- The same pre-connect failure was also counted again through gatekeeper failure reporting, inflating `consecutive_failures` and cooldown.

Required behavior after the fix:
- Persistent Gateway circuit may block real long-lived market/exec/master connection attempts.
- Non-manual circuit open must not permanently block recovery proof.
- Only authorized recovery owners may run the circuit-open disposable probe:
  - `manager_runtime_recovery:circuit_recovery_probe`
  - `run_system.start:circuit_recovery_probe`
- Circuit-open recovery probe is protected by a process-shared lease:
  - default minimum interval: `IB_GATEWAY_RECOVERY_PROBE_WHILE_OPEN_SEC=30`
  - default lease duration: `IB_GATEWAY_RECOVERY_PROBE_LEASE_SEC=20`
- Probe success must close the persistent circuit and clear local recovery backoff.
- Probe failure must not create a connection storm.
- `manual_required=True` still blocks recovery probes until operator acknowledgement.
- Pre-connect API probe failure must use `report_aborted`, not `report_failure`, so one failed proof is not double-counted.

High-signal logs for this regression:
- `ib_gateway_recovery_probe_allowed_circuit_open`
- `ib_gateway_recovery_probe_blocked_circuit_open`
- `ib_wait_until_ready_gateway_circuit_cleared_by_probe`
- `reconnect_gatekeeper_aborted`
- Absence of repeated `ib_connect_attempt_begin` while a circuit-open recovery lease is active.

Required regression command before merging any IB reconnect or startup-gate change:

```bash
scripts/run_ib_reconnect_regression.sh fast
scripts/run_ib_reconnect_regression.sh full
```

The regression script is intentionally no-live by default:
- It sets `TRADING_TEST_ALLOW_LIVE_IBKR=0`.
- It must not connect to local `127.0.0.1:4001/4002`.
- Live IB restart drills are separate operator drills and must require explicit opt-in.

Fast suite covers the incident class directly:
- pre-connect timeout does not double-count gatekeeper failure,
- unauthorized recovery caller cannot open a socket,
- normal disposable probe cannot clear an open circuit unless it is an authorized circuit-recovery probe,
- circuit-open `wait_until_ready()` performs bounded recovery proof and clears circuit on success,
- process-shared recovery probe lease allows only one active probe,
- manual-required circuit blocks recovery probes,
- unit tests cannot reach live IBKR.

Full suite additionally covers:
- market/exec/master connection manager behavior,
- reconnect gatekeeper semantics,
- IB adapter premarket reconnect paths,
- request governor and request-governance chaos tests,
- system health production-state transitions,
- runtime recovery drill and startup smoke.

## 16) 2026-05-11 22:24 Manual-Required Latch Regression

Incident facts:
- `run_system.py` process `14171` started at `2026-05-11 20:43:06 ET`.
- At `21:03:05 ET`, startup health reported `startup_ib_not_ready_retrying`; the Gateway circuit was open after `ConnectionRefusedError` against `127.0.0.1:4002`.
- At `21:06:35 ET`, the platform emitted `ib_gateway_api_unhealthy` with `gateway_reason=gateway_api_half_open`, `failure_streak=5`, and `action=manual_gateway_api_recovery_required`.
- `runtime/state/ib_gateway_connection_circuit.json` then latched `state=manual_required`, `manual_required=true`, `cooldown_sec=300`, `open_until=null`.
- IB Gateway process `81609` started at `2026-05-11 22:24:00 ET`.
- After Gateway was back, the platform still logged startup retry attempts at `22:24:55`, `22:25:56`, `22:26:57`, and later, with `market=False`, `exec=False`, `master=False`, `master_truth=False`, `ib_gateway_circuit_open=True`, and `manual_required=True`.
- `production_state_latest.json` stayed `go_no_go=no_go`; broker truth was `db_projection_only`, `broker_truth_backed=false`, and position safety artifacts were stale.

Critical finding:
- The current protection stopped the connection storm, but it also created a permanent recovery latch.
- A normal IB restart window produced repeated `ConnectionRefusedError`; after the threshold, the system classified it as manual-required recovery instead of a recoverable Gateway restart.
- Once latched, there was no active recovery owner, operator acknowledgement flow, or proof command integrated into startup/system health to clear the circuit.
- The platform therefore did not discover that IB Gateway was back. It only rediscovered that its own circuit was manually locked.

Architectural root causes:
1. **Restart intent is not first-class.** Planned daily restart, user-initiated restart, and unexpected Gateway outage are all collapsed into generic failed connect attempts.
2. **Connection refused and API half-open are over-merged.** `ConnectionRefusedError` while Gateway is down should enter a bounded `gateway_restarting/recovering` state. TCP-open/API-handshake-timeout after Gateway is up is the real `gateway_api_half_open` case.
3. **Manual-required has no closure workflow.** `manual_required=true` is durable and indefinite, but the runtime has no single production path for `ack -> one leased proof -> reqCurrentTime/order/position readiness -> circuit closed`.
4. **Startup retry hides the real terminal state.** Logs keep saying `startup_ib_not_ready_retrying`, even though the actionable state is `manual_gateway_api_recovery_required`.
5. **Broker truth and safety depend on the same stuck path.** Once IB API proof is blocked, broker truth cannot become live-backed and safety artifacts remain stale, so strategies cannot safely resume.
6. **Runtime version freshness matters.** If `run_system.py` was started before critical recovery modules changed, production state must remain `no_go` until the runtime is restarted on the current code hash.

Required production behavior:
- During a declared IB restart window, repeated `ConnectionRefusedError` must fail closed without escalating to `manual_required` before the planned restart grace budget is exhausted.
- Outside a declared restart window, `ConnectionRefusedError` may open a non-manual circuit with bounded, lease-protected recovery proof.
- `manual_required` should be reserved for:
  - proven Gateway API half-open after TCP is reachable but API handshake repeatedly fails,
  - prolonged unplanned outage after the configured recovery budget,
  - operator or risk policy forcing manual intervention.
- If `manual_required=true`, startup/system health must surface `manual_recovery_required` as the primary state, not generic retry.
- There must be exactly one active recovery owner. It must hold the durable recovery lease before creating `IB()` or calling `connectAsync`.
- Authorized proof success must clear `manual_required` only after:
  - independent API handshake succeeds,
  - broker current time succeeds,
  - broker positions can be fetched,
  - open orders can be fetched,
  - market/exec/master readiness can be re-established or explicitly reported unavailable,
  - production artifacts are regenerated with current process start, git SHA, config hash, generated time, and source PID.

Regression tests that must exist before this class is considered closed:
- Planned restart refusal test: Gateway port closed during declared restart does not enter `manual_required` before grace expiry.
- Manual latch visibility test: when `manual_required=true`, production state reports `manual_recovery_required` and does not emit only `startup_ib_not_ready_retrying`.
- Manual proof test: an authorized operator recovery proof can clear `manual_required` after mocked broker time, positions, and open orders succeed.
- Manual proof storm test: ten concurrent/manual proof callers produce one real `IB()` connection attempt and nine lease denials/skips.
- Runtime freshness test: if `run_system.py` process start predates modified critical IB recovery modules, production state stays `no_go` with a runtime-version blocker.

Operator rule for this incident class:
1. Inspect `runtime/state/ib_gateway_connection_circuit.json`.
2. If `manual_required=true` and IB Gateway is already logged in/API-enabled, do not wait for automatic startup retry to fix it.
3. Run the designated operator acknowledgement plus recovery-proof path once.
4. After proof, require `state=closed`, `manual_required=false`, live broker truth, fresh position safety, and `production_state_latest.go_no_go=go` before allowing strategy entry.
5. If critical IB recovery files changed after the `run_system.py` process start time, restart the runtime on the current git/config hash after broker positions and open orders are independently verified.

Remediation landed:
- `ConnectionRefusedError` from a closed Gateway port no longer emits `ib_gateway_api_unhealthy` or marks `manual_required`; it remains a bounded non-manual circuit/recovery condition.
- A historical `manual_required` latch caused by port refusal can be cleared by an authorized, lease-protected recovery proof after `reqCurrentTime` succeeds.
- A true API half-open latch, such as repeated API handshake timeout after TCP is reachable, still blocks automatic socket creation until manual intervention.
- `run_system.py` now exposes `manual_recovery_required` when the Gateway circuit is manually latched instead of reporting only generic `startup_ib_not_ready_retrying`.
- `scripts/run_ib_reconnect_regression.sh fast` includes the refusal/manual-latch regression tests.

## 17) Implemented P0-1 Recovery State Machine

Implemented code-level contract:
- `CLOSED`: circuit closed, `manual_required=false`.
- `RECOVERABLE_RESTARTING`: declared restart window plus `ConnectionRefusedError` within grace; no manual latch.
- `RECOVERABLE_REFUSED`: `ConnectionRefusedError` outside grace; bounded non-manual circuit, lease-protected recovery probes only.
- `RECOVERABLE_COOLDOWN`: gateway-class failure without manual latch; bounded cooldown.
- `API_HALF_OPEN`: TCP connect path is reachable but API handshake/current-time proof repeatedly times out.
- `MANUAL_REQUIRED`: durable manual latch for true half-open/manual intervention.
- `PROOF_IN_PROGRESS`: authorized operator proof owns the durable lease.
- `PROOF_SUCCEEDED`: proof passed and circuit is closed.
- `PROOF_FAILED`: proof failed and manual latch remains.

Classification rules:
- Planned restart refusal is expected during the declared grace window. It must not emit `ib_gateway_api_unhealthy` and must not set `manual_required=true`.
- Refusal outside grace is recoverable and non-manual. It may cool down and allow a single authorized recovery probe lease.
- Only repeated API handshake timeout after TCP is reachable may escalate to `gateway_api_half_open` and `manual_gateway_api_recovery_required`.
- `manual_required=true` blocks automatic probes. It permits only the operator-acknowledged proof path.
- Runtime code freshness remains a hard production gate. If critical IB recovery modules changed after the running `run_system.py` process started, production readiness stays `no_go`.

Operator proof path:
- Function/API: `services.ibkr.ib_gateway_recovery_coordinator.run_authorized_gateway_recovery_proof(...)`.
- Required source: listed in `IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES`.
- Required ack: non-empty `ack_token` or explicit `IB_GATEWAY_RECOVERY_OPERATOR_ACK=true`.
- Required lease: durable process-shared recovery proof lease must be acquired before constructing `IB()`.
- Proof checks:
  - independent disposable `connectAsync`,
  - `reqCurrentTimeAsync`,
  - `positions()`,
  - `reqOpenOrdersAsync()` plus `openTrades()` or `openOrders()`.
- Success closes circuit, clears `manual_required`, and resets recovery backoff when a gatekeeper is supplied.
- Failure preserves the manual latch and reports through `report_aborted`, not `report_failure`.
- Lease denial and unauthorized source return without constructing `IB()`.

Regression commands:

```bash
venv/bin/python -m pytest -q tests/unit/safety/test_no_live_ibkr_in_unit_tests.py
scripts/run_ib_reconnect_regression.sh fast
scripts/run_ib_reconnect_regression.sh full
```

Important: this P0-1 change is unit/fake-IB validated only. It does not claim a live production IB Gateway restart drill.

## 18) Runtime Operator Proof CLI

Implemented operator path:
- CLI: `scripts/operator_ib_gateway_recovery_proof.py`.
- Runbook: `docs/runbooks/ib_gateway_operator_recovery_proof.md`.
- Default mode is dry-run and must not construct `IB()`.
- Live mode requires `--live`, exact operator acknowledgement, `TRADING_TEST_ALLOW_LIVE_IBKR=1`, authorized source in `IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES`, and durable recovery proof lease.

Dry-run:

```bash
TRADING_TEST_ALLOW_LIVE_IBKR=0 \
venv/bin/python scripts/operator_ib_gateway_recovery_proof.py \
  --source operator_cli \
  --host 127.0.0.1 \
  --port 4002 \
  --client-id 98
```

Live proof, explicit opt-in only:

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

Proof contract:
- Missing live opt-in, missing ack, unauthorized source, and lease denial return before any IB object is constructed.
- Success requires disposable API connect, `reqCurrentTime`, positions fetch, and open orders fetch.
- Success clears `manual_required` only through the recovery coordinator.
- Failure keeps the manual latch.
- The CLI writes `runtime/recovery/ib_gateway_operator_proof/latest.json`.
- Production readiness exposes the latest proof artifact for visibility only. Stale proof artifacts do not make the platform `go`, and broker truth / position safety / runtime freshness gates remain authoritative.

Exit codes:
- `0`: dry-run or proof success.
- `2`: proof failed safely.
- `3`: missing live opt-in, missing ack, or unauthorized source.
- `4`: recovery lease denied.
- `5`: reserved for invalid runtime/circuit state.

Regression coverage:
- `scripts/run_ib_reconnect_regression.sh fast` includes operator CLI dry-run, missing ack, unauthorized source, lease denied, proof success clears fake circuit, proof failure keeps fake `manual_required`, and readiness visibility.
- `scripts/run_ib_reconnect_regression.sh full` includes the full operator CLI unit file plus the existing recovery tests.

Validation note:
- This section is code/fake-IB validated only. It does not claim a live operator drill. A live drill must be explicitly run by an operator using the command above.

## 19) Live Operator Drill Prep Only

Preparation artifacts:
- Plan: `docs/runbooks/live_ib_gateway_operator_drill_plan.md`.
- Evidence template: `docs/reports/templates/live_ib_gateway_operator_drill_evidence_template.md`.
- Dry-run smoke: `scripts/check_ib_operator_proof_dry_run.sh`.

Dry-run smoke:

```bash
TRADING_TEST_ALLOW_LIVE_IBKR=0 scripts/check_ib_operator_proof_dry_run.sh
```

Operator-only rule:
- The live drill is not part of regression defaults.
- The live drill must never run unattended.
- The live drill must not trade, cancel, modify, flatten, or clean up unknown broker orders.
- The live drill may only be run after the pre-flight checklist in the live drill plan is complete.
- This preparation step does not claim the live drill has been completed.

## 20) 2026-05-13 Manual Gateway Restart Slow-Recovery Regression

Incident pattern:
- `run_system.py` was running current code at commit `38758cc06`.
- IB Gateway was externally restarted by the operator at about `20:07 ET`.
- Gateway logged in successfully and exposed the paper API port.
- The platform did recover, but only after the existing non-manual Gateway circuit cooldown expired.
- During the cooldown, production state stayed in broker-wait/no-go even though the operator had restarted Gateway.

Critical finding:
- This was not the old connection storm. The durable circuit and recovery lease limited IB API attempts correctly.
- The bug was the opposite failure mode: slow recovery after an external Gateway restart.
- `IBConnectionManager.wait_until_ready()` attempted one authorized circuit-open recovery probe, but after a failed probe it slept for the full circuit `retry_after_sec`.
- The durable recovery probe lease was already responsible for preventing storms. Sleeping the full circuit cooldown duplicated that policy and delayed discovery that the operator had restarted Gateway.

Required behavior:
- Long-lived market/exec/master connects must remain blocked while the Gateway circuit is open.
- Non-manual circuit-open startup must keep polling at the short recovery cadence.
- Real disposable IB API probes are still gated by `IBGatewayRecoveryCoordinator` and the durable recovery lease.
- A successful authorized probe closes the circuit and lets canonical sessions reconnect.
- `manual_required=true` remains a hard latch and still requires the explicit operator proof path.

Remediation:
- `wait_until_ready()` now sleeps only `_gateway_circuit_recovery_probe_sleep_sec` between non-manual circuit-open recovery checks.
- The coordinator/lease remains the only authority allowed to construct a disposable recovery `IB()` object while the circuit is open.
- This makes external Gateway restarts converge promptly without reintroducing a connection storm.

Regression coverage:
- `test_wait_until_ready_uses_short_probe_cadence_when_circuit_recoverable`
- `test_wait_until_ready_probes_gateway_circuit_open_for_recovery`
- `scripts/run_ib_reconnect_regression.sh fast`

## 21) 2026-05-14/15 Pre-Connect Probe + Runtime Hard-Exit Hotfix

Incident pattern:
- IB Gateway was logged in and listening on paper port `4002`, but `run_system.py` did not maintain production IB sessions.
- The durable circuit file contained stale state from `market:pre_connect_api_probe`, including `manual_required=true` and `last_proof_status=PROOF_FAILED`.
- Startup repeatedly treated that diagnostic proof failure as an operator/manual recovery latch and did not reach the canonical market/exec/master connection path.
- After the first remediation allowed the runtime to connect, `ProductionStateEmitter` later killed the entire `run_system.py` process with `os._exit(78)` after repeated production-state lifecycle failures such as `position_safety_artifact_stale`.
- The result looked like "IB still not connected" because the process that owned the three live IB sockets had exited.

Root causes:
- A diagnostic pre-connect API probe had become a hard startup gate before real production sessions. That violated the core IB architecture: `IBConnectionManager` must own canonical long-lived market, exec, and master sessions.
- Stale pre-connect proof fields were interpreted by `scripts/start_paper_runtime.sh` as current evidence that broker API recovery was still required.
- Production-state close-only/degraded lifecycle handling was allowed to terminate the main runtime process. That converted a safety/no-go condition into a broker disconnection.

Hotfix behavior:
- `IB_PRE_CONNECT_API_PROBE_ENABLED` is now opt-in. By default, startup skips the disposable pre-connect probe and proceeds to canonical `IBConnectionManager` market/exec/master connections.
- Stale manual circuits whose source is `*:pre_connect_api_probe` are cleared by `IBConnectionManager` during startup. True operator/manual circuits remain blocking.
- `scripts/start_paper_runtime.sh` no longer blocks startup on stale pre-connect probe proof fields.
- `PRODUCTION_STATE_EXIT_ON_CORE_STOPPED` is now opt-in. By default, production-state failures may keep the platform `no_go`, `RUNNING_PARTIAL_ENTRY`, or close-only, but they must not kill `run_system.py` and drop IB sockets.

Required runtime evidence after this fix:
- `run_system.py` process is alive.
- `lsof -nP -iTCP:4002 -sTCP:ESTABLISHED,LISTEN` shows three Python connections to Gateway paper port `4002`.
- Startup log contains all of:
  - `market_ib_connected=true`
  - `exec_ib_connected=true`
  - `master_ib_connected=true`
  - `master_truth_complete=true`
- Background probes such as `ib_exec_probe_ok` continue after the next production-state emission cycle.

Operational distinction:
- Broker connected does not mean entries are enabled.
- Strategy tradability residuals, stale position-safety artifacts, platform EOD state, and recovery-readiness gates may still put the system into `no_go` or partial-entry mode.
- Those gates are allowed to block trading. They are not allowed to terminate the broker connection owner process unless an operator explicitly enables `PRODUCTION_STATE_EXIT_ON_CORE_STOPPED=1`.

Regression coverage:
- `tests/unit/services/test_ib_connection_manager.py`
- `tests/unit/tool/test_start_paper_runtime_script.py`
- `tests/unit/services/test_production_readiness_services.py::test_production_state_emitter_hard_exits_after_core_lifecycle_failures`
- `scripts/start_paper_runtime.sh --check-only`

Current operator rule:
- Treat this as a hotfix for a production connectivity regression.
- Do not re-enable pre-connect probes or production-state hard-exit by default.
- If either opt-in is needed for a specific drill, document the drill, set the environment variable explicitly, and verify that the runtime still reaches the canonical market/exec/master readiness contract afterward.
