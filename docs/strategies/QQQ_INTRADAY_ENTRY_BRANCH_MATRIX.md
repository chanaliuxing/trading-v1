# QQQ Intraday Entry Branch Matrix

## Scope

This file freezes the current live entry branch structure in:

- `./strategies/qqq_intraday_strategy.py`

## Active Entry Models In Code

- `baseline`
- `v47_fast_drift`
- `v46_ab`
- `v44_elite`

Source:

- `./strategies/qqq_intraday_strategy.py:3351`
- `./strategies/qqq_intraday_strategy.py:3549`
- `./strategies/qqq_intraday_strategy.py:3811`
- `./strategies/qqq_intraday_strategy.py:4227`

## Production Source Of Truth (Current)

Current enabled stock instance:

- `strategy_id = qqq_intraday_001`
- `module = strategies.qqq_intraday_strategy`
- `entry_model = v47_fast_drift`

Source:

- `./config.yml:815`
- `./config.yml:848`

## Branch Summary

### baseline

- Window: `09:45-15:30`
- Direction: QQQ vs VWAP, optional QQEW resonance
- Velocity: `k_normal/k_pulse * atr_eff` with volume pulse
- High-vol protections enabled by `enable_high_vol_guard`

### v47_fast_drift

- Window: `ab_time_start -> max(ab_time_end, 14:30)`
- Opening gate: `early_override`, `atr_rank_skip`, `delayed_confirm`, `early_lock_openclose_mismatch`
- Drift and veto controls: rollback/rebound checks, cooldown and chase guard

### v46_ab

- Window: AB opening window (`ab_time_start/ab_time_end`)
- Branch states: opening hard gate and `AB_A` arm/mark/pullback flow
- Strong opening-specific velocity/volume/overextension filters

### v44_elite

- Direction from QQQ/VWAP, optional QQEW resonance
- Elite-specific overextension, momentum decay, high-vol sign confirmation
- Pullback reentry logic after overextension marks

## Stable Trader Reason Mapping Baseline

Current normalized mapping source:

- `./strategies/qqq_intraday_gate_audit_logic.py`

Important mapped reasons:

- `data_not_ready -> SIGNAL_GATE_FAIL_DATA_NOT_READY`
- `direction_mismatch -> SIGNAL_GATE_FAIL_DIRECTION_MISMATCH`
- `restart_degrade_active -> SIGNAL_GATE_FAIL_RESTART_DEGRADE_ACTIVE`
- `startup_anchor_embargo -> SIGNAL_GATE_FAIL_STARTUP_ANCHOR_EMBARGO`
- `ownership_conflict -> OWNERSHIP_CONFLICT`

Pass-through reasons are listed in:

- `SIGNAL_BLOCK_PASSTHROUGH_REASONS`

This matrix is the reference baseline for future parity replay.
