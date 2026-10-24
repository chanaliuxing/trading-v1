# IB Connection Storm Root Cause - 2026-04-28

## Verdict

The 2026-04-28 incident did not start when IB finally disconnected at 15:46 ET. That was the terminal failure.

The IB pressure started no later than 12:40:29 ET, when market revalidation and option chain requests began failing. At 12:41:14 ET the system entered a repeated per-symbol stale-feed resubscribe loop. That loop continued until IB closed all sessions at 15:46:30 ET.

## Evidence

- 12:40:29 ET: `ib_market_revalidation_failed` begins with `QQQ_L1_MISSING`.
- 12:40:29 ET to 15:46:24 ET:
  - `ib_market_revalidation_failed`: 718
  - `IBKR returned no option chains`: 1436
  - `No option quotes fetched`: 718
- 12:41:14 ET to 15:45:33 ET:
  - `IB symbol feed stale, resubscribing`: 1850
  - `Failed to qualify contract`: 1850
- 15:45:33 ET: the last full burst resubscribed QQQ, SPY, TQQQ, SQQQ, SMH, SOXL, SOXS, RSP, SPXL, and SPXS.
- 15:46:30 ET:
  - `Peer closed connection.`
  - `ib_master_disconnected`
  - `ib_exec_disconnected`
  - `ib_market_disconnected`
- 15:46:31 ET onward: recovery short-circuit correctly blocked immediate reconnect attempts because the pre-connect API probe was not ready.

## Root Cause

The architecture had a reconnect gate, but it did not have a market-data load-shedding gate.

When IB market data was degraded or revalidation-required, two callers continued to generate IB requests:

1. `services/market/ib_market_data.py`
   - The stale subscription monitor resubscribed every stale symbol independently.
   - In the failure state, this became one qualify/cancel/resubscribe cycle per symbol per cooldown window.

2. `services/market/options_data.py` and `tools/ingest_options_quotes.py`
   - Option chain fetch continued even after underlying L1 revalidation failed.
   - If underlying qualification returned no valid `conId`, secdef requests could still be attempted or quickly retried.
   - Zero-row option ingest was treated as a normal loop result, so it did not increase backoff.

The system therefore treated "IB is degraded" as "retry more symbol and option requests", which is the opposite of the desired behavior.

## Critical Architecture Review

### What The Previous Reconnect Architecture Actually Fixed

The previous hardening correctly targeted session ownership:

- long-lived `market`, `exec`, and `master` sessions are owned by `IBConnectionManager`;
- reconnect callers go through the gatekeeper;
- failed API handshakes arm recovery short-circuit;
- recovery does not rely on restarting `run_system.py`;
- master/market/exec stale session events are fenced by epoch.

That fixed reconnect ownership and stale-session contamination. It did not fully fix IB request pressure.

### What It Did Not Architecturally Cover

The missing layer is a broker request governor. Once a valid shared `IB` object is handed to a business module, that module can still call:

- `qualifyContractsAsync`;
- `reqMktData`;
- `cancelMktData`;
- `reqSecDefOptParamsAsync`;
- `reqHistoricalDataAsync`.

Those calls do not create a new long-lived session, so they bypass the reconnect gatekeeper. But they still load IB Gateway and can still create a storm.

This is why the earlier architecture looked correct from a connection perspective while still failing operationally: it serialized `connect()`, but it did not serialize or budget IB API requests during degraded market state.

### Concrete Gaps Exposed By This Incident

1. `IBConnectionManager` owns sessions, but not all IB API traffic.
   - `services/market/ib_market_data.py` could independently resubscribe every stale symbol.
   - `services/market/options_data.py` could independently fetch option chains after L1 revalidation failed.
   - `tools/ingest_options_quotes.py` treated zero quotes as a normal loop and kept polling.

2. `market_revalidation_required` was advisory, not mandatory.
   - The flag existed.
   - Callers were not required to stop expensive IB requests while it was true.
   - There was no central `allow_market_request(kind, key)` decision API.

3. Error 300/354 was downgraded to noise too early.
   - It is reasonable not to page on a single option request rejection.
   - It is not reasonable to let thousands of 300/354 events continue without opening a circuit breaker.
   - The system had noise suppression, not storm suppression.

4. Request backoff was per caller, not global.
   - Symbol resubscribe had per-symbol cooldown.
   - Option chain fetch had partial caches/backoff.
   - There was no shared broker-wide budget such as "max N qualify/sec", "max N secdef/min", or "market degraded => only probe requests allowed".

5. Strategy/data fallbacks still contain direct IB calls.
   - Example: QQQ price-base recovery can call historical IB data through the raw shared `IB` object.
   - This may be acceptable as an emergency fallback, but it must be routed through the same broker request governor with a low budget.

6. Opening/automation checks can detect outcomes but cannot control IB load.
   - Reports tell us the system is unhealthy after the fact.
   - They do not prevent a running process from generating excessive IB traffic.

## Architecture Fix Required

Add an `IBRequestGovernor` under `IBConnectionManager`.

The governor must become the only approved route for expensive IB API calls from business modules:

- contract qualification;
- stock/index market data subscribe/resubscribe;
- option secdef chain requests;
- option quote batch requests;
- historical-data fallback requests;
- diagnostic probes.

Required policy:

1. When broker state is `READY`, allow normal traffic with rate budgets.
2. When broker state is `MARKET_DEGRADED` or `REVALIDATION_REQUIRED`, allow only one controlled revalidation/probe path; block symbol resubscribe and option chain fetch.
3. When broker state is `RECOVERY_QUIET_PERIOD`, block all business IB calls; only the supervisor probe may run.
4. When repeated 300/354/321 errors occur for the same request class, open a request-class circuit breaker.
5. Every block must be observable as a structured event, not hidden as "no data".

## Implementation TODO

- [x] Emergency patch: suppress symbol stale resubscribe while market revalidation is active.
- [x] Emergency patch: add option-chain failure backoff and require underlying `conId > 0`.
- [x] Emergency patch: treat zero option quote rows as ingest failure for backoff.
- [x] Add `services/ibkr/ib_request_governor.py`.
- [x] Add `IBConnectionManager.allow_market_request(kind, key)` and `record_market_request_result(kind, key, ok, error_code)`.
- [x] Route `ib_market_data` resubscribe through governor instead of local cooldown only.
- [x] Route `options_data` secdef/quote requests through governor instead of local backoff only.
- [x] Route QQQ historical price-base fallback through governor.
- [x] Route NBBO cache subscriptions, historical backfill, option spread conId qualification, and v3 execution guard qualification through governor.
- [x] Add a runtime-source linter that flags direct `reqMktData`, `reqSecDefOptParamsAsync`, `qualifyContractsAsync`, and `reqHistoricalDataAsync` outside approved broker-adapter modules unless the caller uses the governor.
- [x] Add chaos test: simulate market revalidation failure and assert no business IB request storm.

## Architecture Acceptance Criteria

- During `market_revalidation_required=True`, business modules emit zero `reqSecDefOptParamsAsync` calls.
- During `market_revalidation_required=True`, stale symbol monitor emits zero `qualifyContractsAsync` calls.
- A 300/354 burst opens an option-request circuit breaker within one minute.
- A single bad option chain cannot generate repeated secdef requests faster than the configured global budget.
- QQQ strategy emergency IB historical fallback is bounded and denied while broker is degraded.
- All direct IB API request call sites are either inside approved broker modules or covered by tests proving they are diagnostic-only and budgeted.

## 2026-04-28 Completion Evidence

- Governance linter: `venv/bin/python tool/ops/check_ib_request_governance.py --output reports/ib_request_governance/latest.json`
  - status: `PASS`
  - violations: `[]`
- Focused tests:
  - `tests/unit/services/test_ib_request_governor.py`
  - `tests/unit/services/test_ib_request_governance_chaos.py`
  - `tests/unit/tool/test_check_ib_request_governance.py`
  - `tests/unit/services/market/test_ib_market_data_premarket_ports.py`
  - `tests/unit/services/market/test_options_data_ibkr_backoff.py`
  - `tests/unit/tools/test_ingest_options_quotes.py`
  - `tests/unit/strategies/test_qqq_ib_request_governor.py`

## 2026-04-28 Startup Regression Found After Restart

The first restart after the request-governor work exposed a separate IB session bug:

- API readiness probe connected with diagnostic `clientId=97`.
- The probe session was cached and promoted into the long-lived market session.
- Exec readiness probe then reused `clientId=97`.
- IB rejected it with error 326, "client id is already in use".
- Startup failed with `market=True exec=False master=False`.

Root cause: a diagnostic probe was allowed to become a real broker session. That violates the connection architecture boundary. A probe is only a disposable readiness check; it must never be promoted into market, exec, or master.

Fix:

- Remove cached probe sessions from `IBConnectionManager`.
- Always disconnect probe `IB` objects in `_probe_api_handshake_ready`.
- Ensure real market/exec/master sessions always use their dedicated client IDs.
- Add unit coverage that a successful API handshake probe disconnects.

Additional startup hardening:

- `scripts/run_system_autostart.sh` now validates log-path writability with the same Python `RotatingFileHandler` that `boot_logging.py` uses. Shell `touch` is not enough on macOS because a path can look writable to the shell but fail in Python during process import.

Verification:

- `venv/bin/python -m pytest tests/unit/services/test_ib_connection_manager.py tests/unit/services/test_ib_request_governor.py tests/unit/services/test_ib_request_governance_chaos.py -q`
- `venv/bin/python -m pytest tests/unit/tool/test_run_system_autostart_script.py tests/unit/test_boot_logging.py -q`
- Manual restart reached `ALL SERVICES STARTED - SYSTEM RUNNING` with `market clientId=10` and `master clientId=0`; no `clientId 97 already in use` appeared after the fix.

## 2026-04-28 Follow-up: Probe Must Not Poison Recovery Budget

The restart verification exposed one more architecture gap relative to
`docs/guides/IBKR_RECONNECT_STORM_AND_STUCK_RECOVERY_RUNBOOK_2026-04.md`:

- API readiness probe failure was still recorded through `gatekeeper.report_failure`.
- That violated the documented rule that readiness probes are serialized but must not consume reconnect attempt budget.
- `wait_until_ready` could continue touching exec/master in the same loop after market had already armed recovery short-circuit.
- `scripts/run_system_autostart.sh` exported `TRADING_MODE=shadow` whenever the shell did not already define `TRADING_MODE`, masking `.env` values such as `TRADING_MODE=paper` and causing only the market/master subset to start when the operator expected 3 IB roles.

Fix:

- `_ensure_api_ready_before_real_connect` now arms recovery short-circuit on API probe failure without calling `gatekeeper.report_failure`.
- API readiness probe now has bounded diagnostic client-id fallback (`97`, `98`, `99` by default) so a stuck diagnostic client id cannot block recovery when Gateway is otherwise healthy.
- API readiness probe now waits briefly after disconnecting the diagnostic session before releasing real market/exec/master connects.
- `wait_until_ready` stops the current recovery iteration immediately after a short-circuit-producing market or exec failure, instead of continuing to the next role.
- `run_system_autostart.sh` now respects explicit environment first, then `.env`, and only falls back to `shadow` when neither exists.

Verification:

- `venv/bin/python -m py_compile services/ibkr/ib_connection_manager.py`
- `venv/bin/python -m pytest tests/unit/services/test_ib_connection_manager.py tests/unit/services/test_ib_request_governor.py tests/unit/services/test_ib_request_governance_chaos.py tests/unit/tool/test_run_system_autostart_script.py tests/unit/test_boot_logging.py -q`
  - `66 passed`

## 2026-04-28 Follow-up: Real Supervisor Must Be launchd, Not nohup

The foreground restart verified the IB connection fix, but the actual unattended
startup path exposed a second operational root cause:

- A plain `nohup scripts/run_system_autostart.sh &` can disappear without an
  application traceback in the Codex/automation execution context.
- The existing Opening Supervisor default remediation still used that plain
  `nohup` command.
- The LaunchAgent template also forced `TRADING_MODE=shadow`, masking
  `.env` values such as `TRADING_MODE=paper`.
- `boot_logging._path_writable` used a plain append open while real startup uses
  `RotatingFileHandler`; on macOS those can differ under background process
  permissions.

Fix:

- Opening Supervisor now uses launchd as the primary restart mechanism:
  `launchctl kickstart -k gui/$UID/com.trading.run_system.0925`, with `nohup`
  only as a developer-shell fallback when the LaunchAgent is not installed.
- `ops/launchd/com.trading.run_system.0925.plist` no longer sets
  `TRADING_MODE`; `scripts/run_system_autostart.sh` remains the single place
  that resolves explicit env, then `.env`, then `shadow` fallback.
- `boot_logging._path_writable` now validates with `RotatingFileHandler`, the
  same handler used by the real process.
- The LaunchAgent was installed to
  `~/Library/LaunchAgents/com.trading.run_system.0925.plist`, enabled, and
  kickstarted.

Verification:

- `venv/bin/python -m pytest tests/unit/services/test_ib_connection_manager.py tests/unit/services/test_ib_request_governor.py tests/unit/services/test_ib_request_governance_chaos.py tests/unit/tool/test_run_opening_supervisor.py tests/unit/tool/test_run_system_autostart_script.py tests/unit/test_boot_logging.py -q`
  - `72 passed`
- launchd state:
  - `launchctl print gui/$UID/com.trading.run_system.0925` shows `state = running`, `pid = 70800`.
- Runtime state:
  - `run_system_startup_ib_ready` at `2026-04-28T17:40:35`.
  - `ALL SERVICES STARTED - SYSTEM RUNNING` at `2026-04-28T17:40:40`.
  - `lsof -nP -iTCP:4002` shows 3 established Python connections to IB Gateway.
  - Telegram startup/recovery notification sent at `2026-04-28T17:40:29`.

## Fix Landed

1. Market stale resubscribe is now suppressed while the shared IB market session is degraded or requires revalidation.
2. Option chain fetch now returns empty with backoff when market revalidation is not ready.
3. Option chain fetch now requires a qualified underlying `conId > 0` before requesting secdef option parameters.
4. Empty secdef, empty underlying qualification, and empty option qualification now mark a chain-level backoff.
5. Option quote ingest now treats zero inserted rows as a failure for backoff purposes.

## Acceptance

- During IB degraded/revalidation state, there must be no per-symbol stale resubscribe storm.
- During underlying L1 failure, option chain fetch must not request secdef repeatedly.
- During zero option quote results, ingest must increase backoff instead of polling at the normal rate.
- IB recovery should be controlled by the connection manager probe path, not by strategy/data callers issuing direct load.

## Tests

- `tests/unit/services/market/test_ib_market_data_premarket_ports.py::test_stale_feed_resubscribe_suppressed_when_market_revalidation_required`
- `tests/unit/services/market/test_options_data_ibkr_backoff.py`
- `tests/unit/tools/test_ingest_options_quotes.py::test_run_backs_off_when_fetch_returns_zero_rows`
