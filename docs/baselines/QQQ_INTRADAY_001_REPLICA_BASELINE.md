# QQQ Intraday Replica Baseline

- Generated at ET: `2026-03-16 01:12:58 EDT`
- Git HEAD: `dbc37febae0aee724acf2e3315a8cd8f634290f2`
- Strategy ID: `qqq_intraday_001`

## Frozen Files

- `strategy_file`: `6cce607ffce1e9d64b3e6edaa28e9c293f1515d41e129788f91adf9b02b126b3`
- `execution_service_file`: `8041e9846eb211fccc0c65538452de8e8611bd6bc71c6bbe9632091bfb592ed2`
- `runtime_reconciler_file`: `83aed1f42097ef1e6f9497875abe0c67f009498a7a0cab1b976939342f7dcaca`
- `fill_event_handler_file`: `44752377c1f007eb8353fa86cb01e3bf6978f7de638a6b2d46f0c6d7c6d1150b`
- `config_file`: `f7df923966fa671609035d28186d313215fc1e77f56c870fd129a50a4359daf9`

## Strategy Contract

- Module: `strategies.qqq_intraday_strategy`
- Class: `QQQIntradayStrategy`
- Symbols: `QQQ,QQEW,TQQQ,SQQQ,AD-NASD`
- Loop interval sec: `1.0`
- ATR period: `14`
- Stop ATR mult: `2.5`
- Submit exchange stop intent: `True`
- Hard stop enabled: `True`
- Breakeven shield enabled: `False`
- ATR seed path: `runtime/state/qqq_intraday_atr_seed.json`
- Daily ATR path: `runtime/state/qqq_daily_atr_context.json`
- Observability snapshot path: `runtime/state/qqq_intraday_observability_qqq_intraday_001.json`
- Diag log path: `runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl`

## Shared Execution Contract

- Execution enabled: `True`
- Route: `SMART`
- TIF: `DAY`
- Entry isolation enabled: `True`
- Entry isolation symbols: ``
- Reconciliation runtime interval sec: `60`

## Frozen Parity Fields

- `gate_allow_or_block`
- `block_reason`
- `side`
- `qty`
- `entry_order_ref_kind`
- `protective_stop_submit`
- `flatten_reason`
- `runtime_transition`
- `restart_recovery_transition`

## Minimum Replay Fixtures

- `normal_entry_tp1_trailing_exit`
- `restart_live_position_missing_protection`
- `same_symbol_foreign_open_order_conflict`
