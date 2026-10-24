# IB Long-Run Stability TODO - 2026-04-30

## Goal

Make `run_system` survive daily IB Gateway restarts, especially the 7:45 restart window, without getting stuck in a misleading partial-recovery state or creating restart storms.

## P1 - Runtime Recovery Must Resume System Health

Problem:
- `IBConnectionManager._runtime_recovery_loop()` can reconnect market, exec, master, and broker truth, but only logs completion.
- `SystemHealthController` may remain `recovering`.
- `services/orders/manager.py` blocks non-cancel orders while state is `recovering`.

Fix:
- Emit `system_resumed` after runtime recovery verifies:
  - market connected
  - exec connected
  - master connected
  - master truth complete
- Ensure `SystemHealthController.on_ib_connection_event()` accepts and publishes `system_resumed`.

Tests:
- `tests/unit/services/test_ib_connection_manager.py::test_runtime_recovery_loop_emits_system_resumed_when_fully_ready`
- Add/keep assertions for:
  - `event_type == system_resumed`
  - `resume_ready is True`
  - broker truth and ledger metadata present

Acceptance:
- A simulated runtime disconnect followed by successful recovery moves health state back to `running`.
- UOM no longer blocks because of stale `recovering` after IB is fully ready.

## P1 - Market/Master Disconnects Must Pause System Health

Problem:
- Exec disconnect emits `ib_disconnected`.
- Market/master disconnects invalidate sessions and trigger recovery, but do not notify the health controller.
- If market/master break first, order flow can remain `running` while broker truth is not available.

Fix:
- Emit `ib_disconnected` from market disconnect handler.
- Emit `ib_disconnected` from master disconnect handler.
- Keep the existing runtime recovery trigger.

Tests:
- Add unit tests for market disconnect health event.
- Add unit tests for master disconnect health event.

Acceptance:
- Market or master disconnect causes health controller to enter paused/recovering path before reconnect attempts complete.
- Non-cancel order placement is blocked while broker truth is unavailable.

## P1 - Half-Open Gateway Must Escalate

Problem:
- Gateway can listen on `4002` while IB API handshake times out.
- Current code backs off and retries, but does not emit the existing `ib_gateway_api_unhealthy` event.
- Operators see repeated timeouts instead of a clear manual Gateway recovery signal.

Fix:
- Track consecutive API readiness probe failures.
- After a configurable threshold, emit `ib_gateway_api_unhealthy`.
- Include `action=manual_gateway_api_recovery_required`.
- Use a cooldown so alerts do not spam during a long Gateway outage.

Tests:
- Add unit test that repeated API readiness failures emit `ib_gateway_api_unhealthy`.
- Existing `SystemHealthController` test must still halt on that event.

Acceptance:
- When TCP is open but API handshake fails repeatedly, the system halts/alerts clearly instead of silently retrying forever.
- A successful API probe resets the failure streak.

## P2 - Slow Startup Must Not Create launchd Restart Loops

Problem:
- Cold startup waits `RUN_SYSTEM_IB_STARTUP_GATE_SEC` and raises if IB is not fully ready.
- launchd has `KeepAlive=true` and `ThrottleInterval=30`, so slow Gateway startup can rebuild services repeatedly.
- In-process reconnect backoff is lost on every process restart.

Fix:
- Keep `run_system` alive and retry the broker startup gate in-process.
- Log and alert each failed broker startup gate.
- Sleep `RUN_SYSTEM_IB_STARTUP_RETRY_SEC` between attempts.

Tests:
- Add focused unit coverage if a lightweight startup-gate helper is introduced.
- At minimum, run existing IB connection manager and health controller tests.

Acceptance:
- If Gateway takes longer than 90 seconds to become API-ready, `run_system` waits and retries instead of exiting into a launchd loop.
- Once IB is ready, startup proceeds to strategy/runtime services.

## Manual Verification

1. Stop `run_system`.
2. Start IB Gateway and verify an independent `ib_insync` probe succeeds.
3. Start `run_system`.
4. Confirm logs include:
   - `run_system_startup_ib_ready`
   - `system_resumed` after runtime reconciler startup
5. During a controlled IB restart:
   - health enters paused/recovering on disconnect
   - no order submission proceeds while broker truth is unavailable
   - recovery emits `system_resumed` after market/exec/master/truth are ready
6. During a simulated half-open Gateway:
   - repeated `ib_api_handshake_probe_not_ready`
   - then `ib_gateway_api_unhealthy`
   - health controller state becomes `halted`
