# Universal Option Strategy (Plugin-based)

## Goal
Make option strategies pluggable and modular:
- Shared: market data subscription, order intent flow, broker sync, position adoption basics
- Pluggable: entry signal, contract selection, exit policy

## Core files
- `strategies/options_universal/base_strategy.py`
- `strategies/options_universal/plugin.py`
- `strategies/options_universal/types.py`
- `strategies/options_universal/template_plugin.py`

## How to create a new option strategy
1. Implement plugin class from `OptionStrategyPlugin`.
2. Create strategy class inheriting `UniversalOptionLiveStrategy` and override `build_plugin()`.
3. Register new strategy in `config.yml` with unique `strategy_id`.

## Isolation and concurrency
- Engine APIs `position/open_orders/portfolio` are strategy-scoped by `strategy_id`.
- Keep `order_ref` namespaced with `strategy_id`.
- For same-underlying multi-strategy, always keep runtime state files strategy-scoped.

## Migration suggestion
- Step 1: New strategies use universal kernel.
- Step 2: Migrate existing option strategies one-by-one by extracting signal/exits into plugins.
- Step 3: Remove duplicated strategy-specific sync/order logic after all migrations.
