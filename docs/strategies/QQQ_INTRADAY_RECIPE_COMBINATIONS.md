# QQQ Intraday Recipe Combinations

## Purpose

Define allowed and invalid module combinations for trader-configurable recipes.

Implementation source:

- `./strategies/qqq_intraday_rules/recipe.py`

## Allowed Modules

### session_module

- `baseline_rth_window`
- `ab_opening_window`
- `fast_drift_window`

### direction_module

- `baseline_direction`
- `fast_drift_direction`
- `ab_direction`
- `elite_direction`

### guards

- `amp_lock_guard`
- `stable_entry_guard`
- `restart_degrade_guard`
- `startup_anchor_guard`
- `open_orders_guard`
- `live_position_guard`
- `pending_decision_sync_guard`
- `ownership_guard`
- `qqq_freshness_guard`
- `qqew_freshness_guard`
- `continuity_180s_guard`
- `atr_ready_guard`
- `vwap_ready_guard`
- `volume_ready_guard`

### overlays

- `shadow_extreme_overlay`
- `anti_chase_overlay`
- `reentry_cooldown_overlay`
- `high_vol_confirm_veto`
- `day_shape_overlay`

### translator

- `qqq_option_translation`
- `tqqq_sqqq_translation`

## Shared Upstream Recipe Contract

Stock and option must share the same upstream modules (session/guards/direction/overlays).
Only translator differs by execution target:

- stock uses `tqqq_sqqq_translation`
- option uses `qqq_option_translation`

Helper API:

- `load_stock_entry_recipe(extras)`
- `load_option_entry_recipe(extras)`

Source:

- `./strategies/qqq_intraday_rules/recipe.py`

## Invalid Combination Rules

- Any unknown module id is invalid.
- `translator` outside the allowed set is invalid.
- `entry_recipe` must be a dictionary-like object.
- Strategy-specific execution logic must not be encoded in direction modules.
- Contract selection, quote health, and order profile tuning must stay outside recipe direction modules.

## Operational Guardrail

Recipe validation is fail-closed. Invalid config raises `ValueError` and blocks startup/deployment checks.
