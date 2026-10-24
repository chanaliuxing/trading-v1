# QQQ Option First-Principles Unblock Acceptance (2026-04-09)

## A. Shared Truth Plane

- [x] A1. One `(QQQ, resolution)` underlying stream can serve multiple strategy consumers in the same process.
- [x] A2. Adding a second consumer does not suppress callbacks to the first or second consumer.
- [x] A3. Shared truth contains only reusable market state.
- [x] A4. Shared truth does not contain order ownership or broker execution state.
- [x] A5. Option chain fields do not substitute for stock truth.

## B. Signal Authority

- [x] B1. `QQQIntradayStrategy` remains the protected live signal authority during `P0`.
- [x] B2. Stock signal artifact includes normalized direction, source ids, and trigger time.
- [x] B3. `QQQ option` live mode consumes upstream stock signal directly.
- [x] B4. `LONG -> CALL` mapping is preserved.
- [x] B5. `SHORT -> PUT` mapping is preserved.

## C. Live Safety

- [x] C1. One option strategy instance has exactly one live signal writer.
- [x] C2. Shadow replay cannot submit live entries.
- [x] C3. Missing source signal does not trigger silent replay failover.
- [x] C4. Same-minute duplicate signal does not create duplicate option submit.
- [x] C5. Valid source signal can still reach contract selection and submit path.

## D. Explainability

- [x] D1. Each evaluated minute has one explicit downstream result.
- [x] D2. Result codes include `signal_consumed` when signal is accepted.
- [x] D3. Result codes include `source_signal_missing` when source is absent.
- [x] D4. Result codes include `source_signal_stale` when source is too old.
- [x] D5. Result codes include `no_contract_selection` when option selection fails.
- [x] D6. Result codes include `entry_intent_submitted` on successful submit.
- [x] D7. No silent heartbeat-only failure mode remains.

## E. Stock Baseline Protection

- [x] E1. Existing `QQQ` stock regression tests remain green.
- [x] E2. Existing `QQQ` stock observability tests remain green.
- [x] E3. Stock-side order semantics are unchanged by signal publication hooks.

## F. Process Topology

- [x] F1. Same-process stock and option sharing works correctly.
- [x] F2. Cross-process sharing works when `QQQ_STOCK_SIGNAL_BACKEND=redis`.
- [x] F3. Shared-state-layer fallback behavior (`auto -> memory`) is explicit and observable.

## F2. Signal Plane Reliability

- [x] F2.1 Redis failures use retry/backoff, not permanent disable after one error.
- [x] F2.2 Heartbeat includes `signal_plane_state` + error/retry diagnostics.
- [x] F2.3 Remote clear supports stale shared-signal cleanup.
- [x] F2.4 Opening acceptance report includes signal-plane degraded ratio.

## G. Runtime Acceptance

- [ ] G1. Stock strategy is alive at open.
- [ ] G2. Option strategy is alive at open.
- [ ] G3. Option reports `signal_source.mode=upstream_stock_signal`.
- [ ] G4. Stock signal artifacts are visible during open-session.
- [ ] G5. Option emits source-aware downstream results during open-session.
- [ ] G6. No duplicate option submit is observed.
- [ ] G7. Shared underlying stream metrics show one stream with multiple consumers.

## Final Gate

- [ ] GO only if sections A-E pass and runtime section G has no critical failure.
- [ ] NO-GO if any of these remain: dropped shared-stream consumers, silent fallback live entry, stock baseline drift, unexplained non-trade minutes.
