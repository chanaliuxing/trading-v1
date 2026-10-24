# Option Quote Architecture Test Plan (2026-04-06)

## Scope

This plan validates the option quote architecture for live strategies under the new pluggable kernel:

- `SPY` 1DTE MA option strategy family
- `QQQ` 0DTE option strategy family
- Shared quote stack: memory cache + `QuoteStore` + ClickHouse fallback

## First-Principles Goals

1. Trading decisions must read low-latency quotes from a live in-process store.
2. Persistence must remain authoritative for replay/audit, but not block hot-path decisioning.
3. Health gates (freshness/coverage) must avoid false negatives caused by shallow cache windows.
4. Strategy behavior must remain consistent with stable stock-signal logic and existing risk exits.
5. Freshness selection must be explainable when memory and ClickHouse disagree.

## Test Layers

## 1) Unit: Quote Data Plane

Target files:

- `services/market/options_quote_memory_cache.py`
- `services/market/options_quote_store.py`

Cases:

1. Cache pruning by age and snapshot cap.
2. Memory-only query path does not call ClickHouse when `fallback_to_ch=False`.
3. Coverage uses memory when window is fully covered.
4. Coverage falls back to ClickHouse aggregate when memory window is insufficient.
5. Contract quote read returns best valid quote with age filter.
6. Insert ordering guarantees cache update occurs only after successful ClickHouse insert.
7. Freshness source selection chooses the newer timestamp between memory and ClickHouse.
8. Hot-path quote payload contract requires `bid/ask/mid/age_sec/quote_source`.
9. Optional live-state layer (`options_quote_live_state`) works in `memory` and `off` modes.

Commands:

```bash
venv/bin/pytest -q \
  tests/unit/services/market/test_options_quote_live_state.py \
  tests/unit/services/market/test_options_quote_memory_cache.py \
  tests/unit/services/market/test_options_quote_store.py \
  tests/unit/tools/test_ingest_options_quotes.py
```

## 2) Unit: Strategy Integration

Target files:

- `strategies/options_plugins/ma_1dte_reject_shared.py`
- `strategies/ma_1dte_option_live_base.py`
- `strategies/qqq_0dte_opt_live_strategy.py`
- `strategies/options_plugins/qqq_0dte_opt_live.py`

Cases:

1. Entry selection path uses shared `QuoteStore` for quote rows.
2. `QQQ` mark lookup path works with memory-first and optional CH fallback.
3. Quote heartbeat and quote-health gates remain stable.
4. `SPY` option plugin binding and parity tests continue passing.
5. Heartbeat and gate-block audits contain `quote_source` and `quote_age_sec`.

Commands:

```bash
venv/bin/pytest -q \
  tests/unit/strategies/test_qqq_0dte_opt_live_strategy_quotes.py \
  tests/unit/strategies/test_spy_1dte_ma10_reject_parity.py \
  tests/unit/strategies/test_spy_1dte_ma10_reject_v2_strategy.py \
  tests/unit/strategies/test_option_plugin_definition_binding.py
```

```bash
venv/bin/pytest -q \
  tests/unit/strategies/test_option_quote_fallback_config_presence.py
```

## 3) Runtime Validation (Paper Trading)

Pre-market checks:

1. `bars_1m_current` available for `QQQ`/`SPY`.
2. `options_quotes_1m` has current-day rows for expected DTE and both rights.
3. Strategy config includes explicit fallback flags:
   - `entry_quote_allow_ch_fallback` (default false for hot-path entry)
   - `mark_quote_allow_ch_fallback` (default true for mark safety)

Open-session checks (first 30 minutes):

1. Strategy minute heartbeat emits continuously.
2. Entry blocked reasons are explainable (`NO_SIGNAL`, `NO_QUOTES`, `STALE_QUOTE`, etc.).
3. `quote_age_sec` remains within configured thresholds in normal data conditions.
4. Quote-health gate does not false-block during good market-data coverage.
5. Heartbeat/diagnostic evidence can explain which quote source was effectively used for freshness decisions.
6. Run automatic opening acceptance report:

```bash
venv/bin/python tools/check_option_quote_opening_30m.py \
  --date 2026-04-06 \
  --strategy-id qqq_0dte_opt_live_001 \
  --audit-root runtime/audit
```

## 4) Regression Gate

Before enabling next live session:

1. All unit commands above pass.
2. No direct strategy hot-path read from `trading.options_quotes_1m` remains outside `QuoteStore`.
3. Paper run produces no repeated `QUOTE_HEARTBEAT_PAUSE` from data-path regressions.
4. Opening report `go=true` for target strategy in paper run.

## Exit Criteria

This test plan is considered complete when:

1. Unit suite is green for quote stack and strategy integration.
2. Paper runtime checks confirm healthy quote freshness and gating behavior.
3. `SPY`/`QQQ` option strategies run without architecture-induced blocking.
