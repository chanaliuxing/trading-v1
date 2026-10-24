# Runtime Repair TODO, Tests, Acceptance - 2026-05-11

## Scope

Fix the live blocker that kept production state in `no_go` after IB restart and prevented strategies from trading normally.

## TODO

- [x] Remove option-chain revalidation self-lock so secdef requests can clear market revalidation.
- [x] Prevent empty ClickHouse option quote aggregates from appearing as stale 1970 quotes.
- [x] Complete broker contract identity for selected internal option symbols at the platform contract-selection boundary.
- [x] Restart `run_system.py` and verify live production state.
- [x] Verify strategies, broker truth, and option quote ingestion after restart.
- [x] Split production safety go/no-go from entry go/no-go so strategy/data/contract admission issues do not become global safety no-go.

## Tests

Command:

```bash
PYTHONPATH=. venv/bin/python -m pytest --import-mode=importlib \
  tests/unit/services/test_contract_selection_service.py \
  tests/unit/services/market/test_options_data_ibkr_backoff.py \
  tests/unit/services/market/test_options_quote_store.py -q
```

Result:

```text
23 passed in 1.82s
```

Additional architecture test command after production-state split:

```bash
PYTHONPATH=. venv/bin/python -m pytest --import-mode=importlib \
  tests/unit/services/test_production_readiness_services.py \
  tests/unit/services/test_contract_selection_service.py \
  tests/unit/services/market/test_options_data_ibkr_backoff.py \
  tests/unit/services/market/test_options_quote_store.py -q
```

Result:

```text
112 passed, 4 warnings in 4.42s
```

## Live Acceptance

- `run_system.py` restarted with PID `54446`.
- Production state: `go_no_go=go`, `failure_count=0`.
- Production state split: `go_no_go=go`, `safety_go_no_go=go`, `entry_go_no_go=go`, `failure_count=0`.
- Strategy tradability: `10/10` tradable, no blockers.
- Broker truth: live broker truth available, projection clean, open broker orders `0`, broker positions `0`, core open orders `0`, nonzero core positions `0`, nonzero ledger positions `0`.
- Contract selection: `go`, selected SPY contract identity valid, localSymbol `SPY   260511C00739000`.
- Option quote ingestion after restart:
  - QQQ rows today: `374`, last `10:51 ET`.
  - SPY rows today: `393`, last `10:51 ET`.
  - NVDA rows today: `136`, last `10:51 ET`.

## Critical Notes

- This restores normal strategy readiness and trading eligibility; it does not force a trade when no strategy signal is present.
- The platform now accepts valid internal option symbols by deriving deterministic IB/OCC identity fields, while still rejecting expiry/strike/right mismatches.
- Longer-term hardening should still propagate IB-qualified `conId` through the quote cache/schema, but the immediate production blocker is removed without bypassing identity validation.
- Top-level `go_no_go` now means safety/system run permission. Entry-specific blockers are exposed through `entry_go_no_go`, `entry_failures`, and per-strategy tradability so they can block entries without stopping broker-safety lanes or degrading the whole runtime.
