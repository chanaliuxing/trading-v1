# QQQ Option Plugability Guide (2026-04-09)

## Goal

Build option strategies like Lego blocks while keeping stock baseline stable.

Core separation:

1. truth fabric
2. signal authority
3. execution expression

## Module Contract

## 1) Truth Fabric

Source:

- `strategies/qqq_intraday_rules/truth_fabric.py`

Contract:

- `QQQUnderlyingTruthFabric`
- single accessor: `resolve_qqq_truth_fabric(runtime, extras)`

Rules:

1. Truth contains market state only.
2. No broker order IDs, position lot IDs, or ownership state.
3. Option chain can shape contract selection, not stock truth.

## 2) Signal Authority

Source:

- `strategies/qqq_stock_signal_artifact.py`

Contract:

- publish: `publish_stock_entry_signal(...)`
- consume: `get_latest_stock_entry_signal(...)`
- diagnostics: `signal_plane_health()`

Runtime backend:

- `QQQ_STOCK_SIGNAL_BACKEND=auto|redis|memory|off`
- TTL: `QQQ_STOCK_SIGNAL_TTL_SEC`
- health states: `healthy_redis|healthy_memory|degraded_no_redis|degraded_fallback_memory|off`

Rules:

1. Live stock strategy is authoritative writer in `upstream_stock_signal` mode.
2. Option strategy is consumer only in upstream mode.
3. In replay mode, option can generate local shadow signal but cannot submit live entry when `shadow_only=true`.
4. Redis failure must not freeze permanently: use retry/backoff and observable degraded states.

## 3) Execution Expression

Source:

- `strategies/options_plugins/qqq_intraday_option.py`

Signal source switch:

- `signal_source_mode=upstream_stock_signal|independent_rule_replay`

Mapping:

1. `LONG -> CALL`
2. `SHORT -> PUT`

Rules:

1. Entry authority must be single-path per strategy instance.
2. Replay must never be silent failover for missing upstream signal.
3. Every evaluated minute must emit explicit downstream result code.

## New Strategy Checklist

1. Reuse `resolve_qqq_truth_fabric` for market truth.
2. Choose one signal authority mode.
3. Keep contract policy and risk/exit logic independent from signal-source implementation.
4. Add tests:
   - authority rules
   - stale/missing upstream handling
   - no duplicate submit per minute
   - explicit downstream result coverage
5. Add runtime acceptance evidence for opening 30 minutes.
