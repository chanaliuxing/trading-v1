# run_system.py Review & Hardening Plan

## Goals
- Improve runtime safety (especially around shutdown and live trading guardrails).
- Reduce “silent failure” modes (tasks exiting early, hidden warnings, partial startup).
- Keep changes low-risk: small PRs, feature flags, and shadow-mode validation first.

## Phase 1 — Correctness & Stability (must-do)
1. **Fix task lifecycle gating**
   - Problem: multiple loops use `while self.running` but `self.running` is set `True` late in startup, so early-started tasks may exit immediately.
   - Change: set `self.running = True` before spawning long-lived tasks, and prefer `while not SHUTDOWN_EVENT.is_set()` for service loops.
   - Add: ensure every long-running task is added to `self.services` for crash detection and final cancellation.

2. **Tighten warning suppression**
   - Replace global `warnings.simplefilter("ignore", ResourceWarning)` with a targeted filter (message/module) scoped to the known `ib_insync` loop warning.

3. **Make “fatal vs non-fatal” explicit**
   - Identify critical dependencies (IB connection, Postgres pool, Kafka bootstrap) and fail fast when they are required for the chosen mode.
   - For optional components (e.g., hedging, dashboards), keep “warn and continue”.

## Phase 2 — Security & Safety Guardrails
1. **Live trading confirmation**
   - Require an explicit confirmation (e.g., `CONFIRM_LIVE=1`) when `TRADING_MODE=live`.
   - Log the selected mode prominently and refuse to start execution without confirmation.

2. **Secrets and defaults**
   - Avoid weak credential defaults in non-local contexts (e.g., `CH_PASS=clickhouse`).
   - In `paper/live`, require secrets to be set (or require a “local dev” mode to allow defaults).

3. **Metrics exposure**
   - Confirm the Prometheus server bind address; prefer binding to `127.0.0.1` unless explicitly configured and firewalled.

4. **Config-driven dynamic imports**
   - Treat strategy module/class loading as trusted-only. Optionally add an allowlist prefix (e.g., `strategies.`) to prevent importing arbitrary modules from config.

## Phase 3 — Performance & Operability
1. **Gate heavy diagnostics**
   - Make `PYTHONTRACEMALLOC=1` and `faulthandler.enable()` conditional (e.g., `DEBUG_RUNTIME=1`) for production throughput.

2. **Improve run loop monitoring**
   - Replace the 1-second polling loop + list mutation with `asyncio.wait(..., return_when=FIRST_EXCEPTION)` and a clear “supervisor” task pattern.

3. **Kafka consumer restart robustness**
   - If a consumer enters a bad state after repeated failures, recreate the consumer object (not just `stop()`/`start()`), and backoff with jitter.

## Phase 4 — Refactor (structure, not behavior)
- Split `RealTradingSystem.start()` into modules with clear ownership:
  - `services/bootstrap_observability.py`
  - `services/bootstrap_market_data.py`
  - `services/bootstrap_features.py`
  - `services/bootstrap_execution.py`
  - `services/bootstrap_writers.py`
- Introduce a small “service wrapper” contract (name, start task(s), stop coroutine, critical/optional) to unify start/stop and crash handling.

## Validation Checklist
- Shadow mode: `TRADING_MODE=shadow` starts and runs for 5+ minutes with clean shutdown (`Ctrl+C` once).
- Paper mode: execution components start only when configured; `Ctrl+C` reliably disconnects shared IB sessions.
- Tests: run `make test` and at least `make -f Makefile.tests test-quick` for feature/data changes.

