# QQQ Option Dual-Live Runbook

## Scope

This runbook covers the isolated live strategy:

- `strategy_id`: `qqq_1dte_ma10_reject_0324_001`
- module: `strategies.qqq_1dte_ma10_reject_live_strategy`
- class: `QQQ1DTEMA10RejectLiveStrategy`

It is currently configured as `enabled: true` in `config.yml`.

## Safety

The existing live option strategy remains unchanged:

- `qqq_0dte_opt_live_001`

The new strategy is isolated by:

- separate `strategy_id`
- separate module/class
- separate signal logic
- separate risk symbol: `OPT:QQQ:1DTE`
- separate audit component: `qqq_1dte_ma10_reject_live_strategy`

## Important

The two QQQ option live strategies can run at the same time:

- `qqq_0dte_opt_live_001`
- `qqq_1dte_ma10_reject_0324_001`

They are protected at three layers:

1. Separate `strategy_id` state and open-order views
2. Separate option contract symbols and separate `risk_symbol`
3. Shared capital group cap: `QQQ_OPT_LIVE_SHARED`

For option orders, the system now also supports:

- same option `symbol` across different strategies
- same-symbol cross-strategy entry allowed only for explicit option intents
- same-strategy duplicate pending entry still blocked
- option `close_only` quantity remains strategy-scoped via `core.core_position(strategy_id, symbol)`

Shared capital group behavior:

- configured on both strategies in `config.yml`
- current combined cap: `20000 USD`
- enforcement happens before reservation
- if combined reserved + open position notional + new intent notional exceeds the cap, the new entry is denied with `shared_cap_group_block`

This means they can run together without software conflict, but they still share one account and one combined QQQ option exposure budget.

Important boundary:

- system-managed fills/orders for the same option symbol can now coexist across strategies
- manual IBKR-side intervention on the same option symbol can still create account-vs-strategy drift
- if you manually trade the same contract in IBKR outside the strategy system, treat that as an exception path and reconcile before reopening entries

## Strategy Profile

- underlying: `QQQ`
- contract target: `1DTE`, fallback `2DTE`
- delta target: `0.45-0.55`, fallback `0.40-0.60`
- fixed size: `4` contracts
- no new entry before `09:40 ET`
- no new entry after `15:00 ET`
- force flatten: `15:50 ET`
- signal engine: replay-aligned `ma10_reject_0324`
- additive branch enabled:
  - `daily_best_call_only: true`
  - `daily_best_min_dist_vwap_atr: 2.0`

## Verification

Before market open:

1. Run:
   - `./venv/bin/python -m pytest -q tests/unit/strategies/test_qqq_1dte_ma10_reject_live_strategy.py tests/unit/strategies/test_qqq_0dte_opt_live_strategy_quotes.py tests/unit/risk/test_risk_service_close_only_exempt.py`
2. Run:
   - `./venv/bin/python scripts/config_sanity_check.py --config config.yml`
3. Confirm `options_quotes_1m` is healthy for `QQQ`
4. Confirm both strategies have:
   - the same `shared_cap_group_id`
   - the same `shared_cap_group_limit_usd`
   - the same `shared_cap_group_strategy_ids`
5. Confirm Telegram allowlist contains:
   - `qqq_0dte_opt_live_001`
   - `qqq_1dte_ma10_reject_0324_001`

## Intraday Checks

Watch these signals in logs/audit:

- `qqq_1dte_setup_detected`
- `qqq_1dte_entry_confirmed`
- `shared_cap_group_block`
- `entry_isolation_blocked`
- `qqq_1dte_state_restored`
- `qqq_0dte_position_diag`

If the process restarts intraday:

1. Check for `qqq_1dte_state_restored`
2. Check the restored symbol matches the live broker position
3. Verify exits are still active for:
   - `STRUCTURE_STOP`
   - `TP1_1R`
   - `RUNNER_MA10_FAIL`
   - `SOFT_TIME_STOP`
   - `HARD_TIME_STOP`
   - `EOD_FORCE_FLATTEN`

## Operational Notes

- 0DTE strategy notifications continue unchanged
- 1DTE strategy notifications are now included in Telegram allowlist
- 1DTE runtime state file path:
  - `runtime/state/qqq_1dte_ma10_reject_live_qqq_1dte_ma10_reject_0324_001.json`
