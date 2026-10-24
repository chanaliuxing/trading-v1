# SystemHealthController Design / Acceptance (2026-04-21)

**Status**: Draft / Phase 0 Gate
**Owner**: Codex + operator review
**Related documents**:
- `docs/implementation/IBKR_ORDER_TRUTH_AND_RECONNECT_FINAL_PLAN_2026-04-17.md`
- `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md`
- `docs/implementation/QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md`

## 1. Decision Summary

We agree with Claude's revised review and apply the following corrections to the prior Codex plan:

1. `SystemHealthController` is a real control center, not an alert helper.
2. Every live/shadow strategy must explicitly subscribe to `system_health_events`.
3. A strategy that does not subscribe is not allowed to run. This is the "silent non-speaker is muted" rule.
4. Phase 1 is estimated at 2 weeks, not 1 week, because QQQ monolith integration and chaos gates are in scope.
5. While Phase 1 is being built, all new live promotion is frozen. Shadow-only work may continue.

## 2. Problem Statement

The system already has several pieces of resilience:

1. `IBConnectionManager` owns reconnect discipline and connection epochs.
2. `services/exec/adapters/ib_adapter.py` records disconnects and supports callbacks.
3. `run_system.py` can send `system_alert` through the notification manager.
4. `services/notifications/telegram_notifier.py` supports Telegram push.
5. Prometheus already has `BrokerDisconnected`.

The missing piece is a single authority that converts these facts into a trading state:

```text
IB disconnects
  -> Telegram alert
  -> system trading pause
  -> strategy silence
  -> broker truth resync
  -> position/order reconcile
  -> controlled resume
```

Today this behavior is distributed across connection, execution, strategy, and reconciliation code. That is not enough for the operator requirement:

> The system must keep running, alert through Telegram when IB disconnects, wait safely, and resume normal operation after IB recovers.

## 3. Non-Negotiable Invariants

### 3.1 Single Trading State

There must be one canonical state source:

```text
SystemHealthController.state
```

Allowed states:

1. `STARTING`
2. `RUNNING`
3. `BROKER_DEGRADED`
4. `SYSTEM_PAUSED`
5. `RECOVERING`
6. `RESUMED`
7. `HALTED`

No strategy, service, or execution path may independently decide that trading is enabled when the controller says it is paused or degraded.

### 3.2 Silent Non-Speaker Is Muted

Every registered strategy must subscribe to `system_health_events` during initialization.

Rules:

1. `initialize()` must call a system-provided subscription API.
2. The controller records an acknowledgement by `strategy_id`.
3. If a strategy does not acknowledge subscription before market runtime starts, the controller rejects that strategy.
4. Rejection is fail-closed:
   - live strategy: do not start
   - shadow strategy: do not promote and mark as invalid
5. QQQ monolith is not exempt. `strategies/qqq_intraday_strategy.py` must be adapted to subscribe explicitly.

This avoids the dangerous split-brain case:

```text
HealthController says: paused
QQQ monolith says: data looks fresh, can trade
```

The controller wins.

### 3.3 Pause Means No New Risk

When state is `BROKER_DEGRADED`, `SYSTEM_PAUSED`, or `RECOVERING`:

1. No new entry orders.
2. No new scale-in orders.
3. No reverse-open orders.
4. Existing broker-side protective orders may remain.
5. Manual flatten remains available through the controlled Telegram/manual path.
6. Reconciliation may inspect and quarantine, but must not create new exposure.

### 3.4 Resume Requires Broker Truth

IB connected is not sufficient for resume.

Resume requires:

```text
exec_session_connected
AND market_session_connected
AND master_truth_complete
AND unresolved_order_quarantine_count == 0 for affected symbols
AND position_reconcile_clean_or_operator_ack
```

## 4. Controller Responsibilities

`SystemHealthController` owns:

1. IB connection event ingestion.
2. Runtime service crash event ingestion.
3. Trading state transition.
4. Telegram alert push for disconnect and recovery.
5. Strategy pause/resume broadcast.
6. Subscription enforcement.
7. Recovery orchestration gates.
8. Chaos/gameday result recording.

It does not own:

1. Actual broker order placement.
2. Protective order idempotency.
3. Ledger mutation.
4. QQQ signal calculation.

Those belong to `UnifiedOrderManager`, `PositionLedger`, and `QQQSignalAuthority`.

## 5. Proposed Module Boundary

New package:

```text
services/health/
  __init__.py
  controller.py
  events.py
  state.py
  subscriptions.py
  telegram.py
  watchdog.py
```

Primary classes:

```python
class SystemHealthController:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def on_ib_disconnected(self, event: IBConnectionEvent) -> None: ...
    async def on_ib_reconnected(self, event: IBConnectionEvent) -> None: ...
    async def on_service_crashed(self, event: ServiceCrashEvent) -> None: ...
    async def subscribe_strategy(self, strategy_id: str, capabilities: dict) -> HealthSubscription: ...
    async def assert_strategy_admitted(self, strategy_id: str) -> None: ...
    async def broadcast(self, event: SystemHealthEvent) -> None: ...
    async def request_resume(self, reason: str) -> ResumeDecision: ...
```

Event shape:

```python
SystemHealthEvent(
    event_id: str,
    state: str,
    previous_state: str,
    reason: str,
    severity: str,
    started_at: datetime,
    recovered_at: datetime | None,
    ib_epoch: int | None,
    affected_symbols: list[str],
    metadata: dict,
)
```

## 6. Strategy Subscription Contract

New strategy-facing contract:

```python
await ctx.system_health.subscribe(
    strategy_id=self.strategy_id,
    callbacks={
        "on_system_paused": self.on_system_paused,
        "on_system_resumed": self.on_system_resumed,
        "on_system_halted": self.on_system_halted,
    },
    capabilities={
        "can_shadow_while_paused": True,
        "can_manual_flatten_while_paused": False,
    },
)
```

Minimum required behavior:

```python
async def on_system_paused(event):
    self._system_paused = True

async def on_system_resumed(event):
    self._system_paused = False
    await self.refresh_runtime_truth()
```

Required guard:

```python
if self._system_paused:
    return  # no new signal/intent generation
```

For QQQ monolith:

1. Add `_system_paused` state.
2. Subscribe during initialization.
3. Gate all entry intent generation on `_system_paused`.
4. Keep existing data freshness logic, but only as a local additional blocker.
5. Add tests proving HealthController pause overrides local freshness.

## 7. Telegram Alert Requirements

Disconnect alert fields:

1. Event type: `system_alert`.
2. Status: `CRITICAL`.
3. Service: `IBKR`.
4. Reason: `ib_disconnected`.
5. IB role: `exec`, `market`, `master`, or `unknown`.
6. Last error code/message.
7. Current health state.
8. Open order count if available.
9. Position count if available.
10. Action taken: `system_paused`.

Recovery alert fields:

1. Status: `RECOVERED`.
2. Reason: `ib_recovered`.
3. Disconnect duration.
4. New connection epoch.
5. Reconcile summary.
6. Quarantine count.
7. Resume decision: `auto_resumed`, `manual_ack_required`, or `halted`.

Alert dedupe:

1. First disconnect: immediate.
2. Still disconnected: no more than once every 15 minutes unless severity changes.
3. Recovery: always send once.
4. Flapping: send summarized alert after threshold.

## 8. IB Watchdog

The watchdog must be able to alert even if `run_system.py` hangs.

Preferred implementation:

1. Lightweight separate process or launchd/systemd service.
2. Pings IB Gateway/TWS port and optional API current time.
3. Sends Telegram directly through the same notification config.
4. Writes heartbeat to `runtime/state/ib_watchdog.json`.

Minimum checks:

1. TCP port reachable.
2. API connect succeeds with diagnostic client id.
3. Last successful probe age.
4. `run_system.py` process heartbeat age.

The watchdog must not:

1. Place orders.
2. Modify orders.
3. Adopt broker orders.
4. Override `SystemHealthController` state.

## 9. Integration Points

### 9.1 `run_system.py`

Required changes:

1. Instantiate `SystemHealthController` before strategy engine start.
2. Route `_send_system_alert` through controller for runtime health events.
3. On service crash, call `controller.on_service_crashed(...)`.
4. Before declaring trading ready, check all strategy subscriptions.
5. During shutdown, stop controller after strategies stop broadcasting.

### 9.2 `services/exec/adapters/ib_adapter.py`

Required changes:

1. Replace or wrap `_on_disconnected_callback` with controller event publishing.
2. Emit structured `IBConnectionEvent`.
3. Include role, client id, epoch, recent IB error code, source.

### 9.3 `services/ibkr/ib_connection_manager.py`

Required changes:

1. Publish master/market/exec connection state to controller.
2. Expose current connection epoch and truth readiness.
3. Provide controller-readable snapshot.

### 9.4 Strategy Engine / Context

Required changes:

1. Add `ctx.system_health`.
2. Strategy registration must include health subscription check.
3. Startup fails if a live strategy is not admitted.

## 10. Phase 1 Work Breakdown

Recommended duration: 2 weeks.

| Work item | Estimate | Notes |
|---|---:|---|
| Controller state machine + event model | 3 days | Includes unit tests |
| Telegram disconnect/recovery wiring | 1 day | Reuse existing notification manager |
| Strategy subscription registry | 2 days | Includes fail-closed admission |
| QQQ monolith subscription patch list + tests | 2 days | Must prove pause overrides local logic |
| IB watchdog process | 2 days | Independent heartbeat and Telegram path |
| run_system integration | 2 days | Service crash and startup admission |
| Chaos/gameday integration gate | 2 days | Broker disconnect scenario |

## 11. Acceptance Tests

### 11.1 Unit Tests

Required:

1. State transitions:
   - `RUNNING -> BROKER_DEGRADED`
   - `BROKER_DEGRADED -> RECOVERING`
   - `RECOVERING -> RESUMED`
   - `RECOVERING -> HALTED`
2. Alert dedupe:
   - first disconnect sends
   - repeated disconnect is deduped
   - recovery always sends
3. Strategy admission:
   - subscribed strategy admitted
   - non-subscribed live strategy rejected
   - QQQ monolith subscription required
4. Pause override:
   - strategy with fresh data still blocks when system paused

### 11.2 Integration Tests

Required:

1. Mock IB disconnect triggers controller pause.
2. Reconnect does not resume until broker truth and reconcile gates pass.
3. Service crash sends Telegram and moves state to `HALTED` or `SYSTEM_PAUSED`.
4. Startup with a non-subscribed live strategy fails closed.

### 11.3 Chaos Gate

Scenario:

1. Start paper runtime.
2. Disconnect IB Gateway/TWS for 60 seconds.
3. Confirm Telegram disconnect alert within 30 seconds.
4. Confirm no new entry orders during disconnect.
5. Reconnect IB.
6. Confirm reconcile runs before resume.
7. Confirm Telegram recovery alert.
8. Confirm QQQ can continue after resume without duplicate order or reverse-open.

Gate result must be stored under:

```text
reports/system_health/ib_disconnect_gameday/YYYYMMDD.json
```

## 12. Metrics

Required metrics:

```text
system_health_state{state}
system_health_transitions_total{from_state,to_state,reason}
system_health_strategy_subscriptions_total{strategy_id,status}
system_health_strategy_rejections_total{strategy_id,reason}
system_health_pause_duration_seconds
system_health_resume_attempts_total{result}
ib_disconnect_alerts_total{role,reason}
ib_recovery_alerts_total{result}
```

## 13. Release Gates

Phase 1 is not complete until:

1. Every registered live strategy has an explicit health subscription.
2. QQQ monolith is subscribed and pause-gated.
3. IB disconnect sends Telegram.
4. IB recovery sends Telegram.
5. Disconnect chaos test passes.
6. No new live entry is possible while state is paused/degraded/recovering.
7. Controller state is included in daily EOD report.

## 14. Decision Record

### Accepted

1. Claude's "silent non-speaker is muted" rule is accepted.
2. Phase 1 duration is revised from 1 week to 2 weeks.
3. QQQ monolith must be brought under the controller, not left as a legacy exception.
4. The controller is the trading-state authority; local data freshness checks are only additional blockers.

### Rejected / Deferred

1. A demo-only 1-week controller is rejected because it would not enforce strategy subscription or QQQ pause semantics.
2. Option/Replay data-plane work is deferred to the separate data-plane phase; it is not required for the health controller.

## 15. Resolved Questions / v1.0

The following items are now explicitly resolved so Phase 1 implementation can start without relying on chat history.

### Q1. Who is the final trading-state authority?

Decision:

1. `SystemHealthController` is the only authority for runtime trading state.
2. `run_system.py`, strategy classes, and execution paths may consume that state.
3. They may add stricter local blockers, but they may not override a pause/degraded/halted state.

### Q2. What is the admission policy for non-subscribed strategies?

Decision:

1. All enabled strategies must subscribe before admission.
2. For `mode=live`, lack of subscription is a startup failure.
3. For `mode=shadow`, lack of subscription is also a startup failure under Phase 0 freeze; shadow is not exempt.
4. This keeps operational semantics uniform and prevents "shadow today, live tomorrow" drift.

### Q3. What is the QQQ monolith migration requirement for Phase 1?

Decision:

1. `strategies/qqq_intraday_strategy.py` must subscribe to `system_health_events`.
2. Entry generation must be blocked by controller state.
3. Existing freshness and restart-degrade logic may remain as additional blockers.
4. No code path in QQQ monolith may treat broker reconnect as sufficient for self-resume.

### Q4. What authority does the watchdog have?

Decision:

1. The watchdog may detect, heartbeat, and alert.
2. The watchdog may not resume trading.
3. The watchdog may not mutate broker or DB state.
4. The watchdog may request controller attention by emitting a health event or alert.

### Q5. What is the startup policy under unresolved quarantine?

Decision:

1. Any enabled live strategy whose symbol overlaps unresolved quarantine must not be admitted to live trading.
2. If the controller cannot map quarantine scope to a symbol, it must fail system-wide closed.
3. Resume after reconnect follows the same rule.

### Q6. What changed after the Phase 0 surface baseline correction?

Decision:

1. `stocks_kernel_shadow_smh_001` and `stocks_kernel_shadow_spy_001` were discovered to be live-configured despite shadow naming.
2. Phase 0 correction changed them to true shadow mode in `config.yml`.
3. This correction is now part of the canonical baseline and must not be reverted implicitly.
