# QQQ Intraday Option V1 Runbook

## Strategy

- Strategy id: `qqq_intraday_option_v1_001`
- Registry name: `qqq_intraday_option_live_v1`
- Entrypoint: `./strategies/qqq_intraday_option_v1_strategy.py`
- Plugin: `./strategies/options_plugins/qqq_intraday_option.py`
- Definition: `QQQ_INTRADAY_OPTION_PARITY_V1`

## Source Of Truth

- Market logic source: `qqq_intraday_policy_v1`
- Signal module: `./strategies/options_kernel/modules/qqq_intraday_stock_signal.py`
- Exit policy: `./strategies/options_kernel/modules/qqq_intraday_option_position_policy.py`

## Behavior

- Stock `LONG` signal -> buy QQQ call
- Stock `SHORT` signal -> buy QQQ put
- Hard stop, logic retreat, and EOD flatten are driven by underlying QQQ logic
- Contract selection is handled by the option kernel contract policy

## Current Operational Decision

- New active QQQ option path: `qqq_intraday_option_v1_001`
- Retired QQQ option paths:
  - `qqq_0dte_opt_live_pure_001`
  - `qqq_1dte_ma10_reject_0324_v2_001`

## Config

Relevant config entry:

- `./config.yml`

Key extras:

- `kernel_mode: pure_plugin`
- `risk_symbol: OPT:QQQ:0DTE`
- `dte_preferred: 0`
- `dte_fallback_max: 1`
- `delta_min: 0.35`
- `delta_max: 0.60`
- `hard_stop_atr_mult: 0.75`
- `logic_retreat_min_hold_sec: 360`

## Verification

Run:

```bash
./venv/bin/pytest -q tests/unit/strategies/options_kernel/test_qqq_intraday_option_parity.py
./venv/bin/pytest -q tests/unit/strategies/options_kernel/test_base.py
```

Check runtime logs for:

- `option_ma_strategy_snapshot`
- `option_kernel_pure_subscribed`

Confirm snapshot fields show:

- `definition_id=QQQ_INTRADAY_OPTION_PARITY_V1`
- `plugin=qqq_intraday_option_plugin`
- `risk_symbol=OPT:QQQ:0DTE`

## Rollback

If the new path must be rolled back:

1. disable `qqq_intraday_option_v1_001`
2. do not re-enable retired QQQ option paths without an explicit decision
3. inspect runtime snapshot and audit logs before switching to any replacement
