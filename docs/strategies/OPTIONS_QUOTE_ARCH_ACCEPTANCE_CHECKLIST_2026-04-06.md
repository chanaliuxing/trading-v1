# Option Quote Architecture Acceptance Checklist (2026-04-06)

## A. Architecture Invariants

- [x] A1. Strategy hot-path quote reads are routed through `QuoteStore`, not ad-hoc direct SQL.
- [x] A2. Entry quote path is memory-first and supports explicit CH fallback switch.
- [x] A3. Mark quote path supports configurable CH fallback for resilience.
- [x] A4. Cache update happens only after ClickHouse insert success.
- [x] A5. Heartbeat/audit payloads include `quote_source` + `quote_age_sec`.

## B. Data Freshness and Coverage

- [x] B1. `latest_quote_age_sec` gate works for both `SPY` and `QQQ`.
- [x] B2. `quote_minute_coverage` does not undercount due to shallow memory snapshots.
- [x] B3. Coverage fallback to ClickHouse aggregate works when memory window is incomplete.
- [ ] B4. No persistent false `QUOTE_HEARTBEAT_PAUSE` in normal data conditions.
- [x] B5. Freshness selection is explainable when memory and ClickHouse disagree.
- [x] B6. Optional live-state layer can participate in freshness selection (`source=live_state`).

## C. Strategy Behavior Consistency

- [x] C1. `SPY` option strategy remains aligned with prior signal/risk behavior.
- [x] C2. `QQQ` option strategy mark and exit logic function normally.
- [ ] C3. No new blocking reason dominates due to architecture regressions.
- [x] C4. Existing parity/binding unit tests stay green.

## D. Test Evidence

- [x] D1. `test_options_quote_memory_cache.py` pass.
- [x] D2. `test_options_quote_store.py` pass.
- [x] D3. `test_ingest_options_quotes.py` pass.
- [x] D4. `test_qqq_0dte_opt_live_strategy_quotes.py` pass.
- [x] D5. `test_spy_1dte_ma10_reject_parity.py` pass.
- [x] D6. `test_spy_1dte_ma10_reject_v2_strategy.py` pass.
- [x] D7. `test_option_plugin_definition_binding.py` pass.
- [x] D8. Freshness source-selection unit tests pass.
- [x] D9. `test_options_quote_live_state.py` pass.
- [x] D10. `test_option_quote_fallback_config_presence.py` pass.
- [x] D11. `test_check_option_quote_opening_30m.py` pass.

## E. Runtime Acceptance (Paper Trading)

- [ ] E1. Open-session 30-minute run completes with normal strategy heartbeats.
- [ ] E2. Entry/exit decisions contain valid quote fields (`bid/ask/mid/quote_age_sec`).
- [ ] E3. No architecture-level crashes or repeated data-gate flapping.
- [ ] E4. Audit path remains queryable from ClickHouse for post-trade evidence.
- [ ] E5. Opening acceptance report (`tools/check_option_quote_opening_30m.py`) returns `go=true`.

## Final Go/No-Go Rule

- [ ] GO only if all A-D pass and E has no critical failures.
- [ ] NO-GO if any P1 issue remains in architecture invariants or runtime stability.
