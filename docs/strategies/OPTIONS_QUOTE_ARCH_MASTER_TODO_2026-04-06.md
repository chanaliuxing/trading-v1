# Option Quote Architecture Master TODO (2026-04-06)

## Goal

Build an option quote architecture that is:

- memory-first on the hot path
- explicit about fallback policy
- replay/audit-safe
- observable enough for paper/live acceptance

## Current Status

Completed:

- Shared `QuoteStore` introduced for option quote reads.
- Entry quote hot path supports explicit `fallback_to_ch`.
- Coverage no longer undercounts because of shallow cache alone.
- Cache updates only after successful ClickHouse insert.
- Active strategy config now declares entry/mark fallback policy explicitly.
- Freshness logic now compares memory and ClickHouse latest timestamps and selects the fresher source.
- Strategy heartbeat/audit now emits `quote_source` + `quote_age_sec` on key quote-gate paths.
- Opening 30-minute paper-session acceptance script is available (`tools/check_option_quote_opening_30m.py`).
- QuoteStore hot-path contract is now explicit in code and unit-tested.
- Active definition-driven option strategies now enforce explicit fallback flags in config tests.
- Optional cross-process realtime quote state layer is implemented (`OPTIONS_QUOTE_LIVE_STATE_BACKEND=redis|memory|off`).

Still open:

- Collect opening-session live evidence and archive reports for both QQQ/SPY paper sessions.
- Turn on and burn in redis-backed live state in production-like paper runtime.
- Add per-underlying metrics dashboard for source ratio/freshness/fallback.

## Implementation TODO

## P0

- [x] Emit `quote_source` and `quote_age_sec` together in strategy heartbeat/audit logs.
- [x] Add a paper-session validation script/query to summarize:
  - quote source ratio
  - fallback rate
  - freshness failures
  - coverage failures

## P1

- [x] Define a formal `QuoteStore` runtime contract for hot-path callers:
  - entry path
  - mark path
  - health path
  - replay/audit path
- [x] Add tests for active config presence of explicit fallback flags.
- [x] Add tests for enabled definition-driven option strategies that rely on `StandardOptionContractPolicy`.

## P2

- [x] Implement reusable live quote state layer abstraction (`services/market/options_quote_live_state.py`) and wire ingest/query paths.
- [ ] Enable redis backend by environment in paper-live runtime and run burn-in verification.
- [ ] Add per-underlying observability metrics for quote freshness and fallback behavior.

## Test Plan Link

- [OPTIONS_QUOTE_ARCH_TEST_PLAN_2026-04-06.md](./docs/strategies/OPTIONS_QUOTE_ARCH_TEST_PLAN_2026-04-06.md)

## Acceptance Checklist Link

- [OPTIONS_QUOTE_ARCH_ACCEPTANCE_CHECKLIST_2026-04-06.md](./docs/strategies/OPTIONS_QUOTE_ARCH_ACCEPTANCE_CHECKLIST_2026-04-06.md)
