# QQQ Stock Signal Option Strategy Design

## Goal

Define the next QQQ option strategy as a plugin-kernel strategy that reuses the QQQ stock strategy logic for signal generation.

Target outcome:

- stop using the old standalone QQQ option strategy family as the primary runtime path
- keep future QQQ option work on the pluggable kernel only
- make QQQ stock and QQQ option follow one signal logic family

This document is a design standard, not a rollout-complete implementation record.

## Scope Decision

This strategy is for:

- QQQ intraday directional signals
- option execution instead of stock execution
- similar stop-loss and flatten behavior to the QQQ stock strategy

This strategy is not for:

- preserving the old QQQ option-specific entry logic as the source of truth
- introducing another legacy live class outside the plugin kernel
- maintaining two separately tuned QQQ intraday signal families without explicit reason

## Retirement Decision

Retire the previous standalone QQQ option strategy family from operational use once the replacement is ready.

Operational meaning of "retire":

1. old strategy instance `enabled: false`
2. old strategy id removed from Telegram allowlist if alerts are no longer needed
3. old strategy id removed from shared cap groups if it should no longer reserve or share capital
4. new parameter tuning happens only on the replacement plugin path

Recommended retirement target:

- primary target: the old QQQ intraday option family used as the direct predecessor of this strategy
- do not retire unrelated QQQ option families unless they are explicitly replaced by this design

## Architecture Choice

Use the existing option plugin kernel:

- Kernel: `./strategies/options_kernel/base.py`
- Plugin contract: `./strategies/options_kernel/plugin.py`
- Definition registry: `./strategies/options_kernel/modules/strategy_definitions.py`

New strategy family proposal:

- registry name: `qqq_intraday_option_live_v1`
- strategy id: `qqq_intraday_option_v1_001`
- kernel mode: `pure_plugin`

## Source Of Truth

The source of truth for market direction should be the QQQ stock strategy logic family, not the retired QQQ option entry code.

Specifically reuse these semantics:

- template family: `qqq_intraday_core` or an extracted equivalent
- stock policy intent: `./strategies/stocks_kernel/policies/qqq_intraday_policy_v1.py`
- stock-side gate/feature semantics from `./strategies/stocks_kernel/templates.py`

The option strategy should consume the same market interpretation, then translate the signal into option orders.

## Proposed Module Layout

### 1. Signal module

Add a new signal module under:

- `./strategies/options_kernel/modules/qqq_intraday_stock_signal.py`

Responsibility:

- compute the QQQ intraday feature context using the same feature family as stock
- apply the same gate semantics as the stock strategy
- emit `LONG` or `SHORT` directional signal only when the stock logic would enter

Design rule:

- do not reimplement a second opinionated QQQ intraday entry model
- adapt the stock signal path into option-kernel signal output

### 2. Strategy definition

Add a new `StrategyDefinition`:

- `definition_id`: `QQQ_INTRADAY_OPTION_PARITY_V1`

Recommended binding:

- `underlying`: `QQQ`
- `risk_symbol`: `OPT:QQQ:0DTE`
- `signal_module`: `QQQIntradayStockSignalModule`
- `contract_policy`: shared standard option contract policy
- `execution_policy`: shared standard option execution policy
- `position_policy`: new parity-oriented QQQ option position policy
- `audit_namespace`: `qqq_intraday_option`

## Signal Mapping

Map stock signal to option side as follows:

- stock `LONG` => buy `CALL`
- stock `SHORT` => buy `PUT`

No stock hedge legs should be submitted by this strategy.

The execution instrument changes, but the market decision stays tied to QQQ.

## Contract Selection

Recommended first version:

- preferred DTE: `0`
- fallback DTE max: `1`
- target delta band:
  - call: `0.35` to `0.60`
  - put: `-0.60` to `-0.35` in economic meaning
- spread guard remains mandatory
- stale quote entry remains blocked by default

Design intent:

- enough delta to preserve parity with stock direction
- not so deep ITM that capital usage behaves like stock replacement
- not so far OTM that stop logic becomes noisy

If repo-wide contract policy only supports absolute delta ranges, keep the config expressed in that form and document the call/put interpretation in the plugin snapshot.

## Exit Design

Exit behavior should be "similar to QQQ stock logic", but that needs a precise rule. The recommended rule is:

### Primary exit anchor: underlying parity

Use the QQQ stock logic family as the main driver for:

- hard stop
- logic retreat
- forced end-of-day flatten

Meaning:

- if the underlying-level stop or retreat condition would close the stock version, the option version should also submit a close intent

### Secondary execution anchor: option marketability

The actual option order price and fill logic still depend on option quotes:

- quote freshness
- spread guard
- degraded close rules if NBBO quality drops

### Why this split

If stops are based only on option premium:

- QQQ stock and option can diverge too much
- volatility expansion can create false stop-outs

If exits ignore option quote health:

- the strategy can hold unexitable contracts during quote degradation

So the recommended design is:

- underlying decides whether to exit
- option execution layer decides how to exit safely

## Position Policy Proposal

Add a new position policy under:

- `./strategies/options_kernel/modules/qqq_intraday_option_position_policy.py`

Required responsibilities:

1. Maintain entry-side metadata:
   - stock-side signal direction
   - entry underlying price
   - entry ATR used
   - entry time
   - selected contract metadata
2. Recompute stop and retreat conditions from underlying movement
3. Trigger EOD flatten at the same operational cutoff as the stock family, unless a different option cutoff is explicitly configured
4. Emit audit reasons that remain readable in stock terms:
   - `underlying_hard_stop`
   - `underlying_logic_retreat`
   - `force_flatten_eod`

Optional extension for later:

- TP1 / break-even behavior that mirrors stock logic, but only after the base stop-and-flatten path is stable

## Plugin Proposal

Add plugin:

- `./strategies/options_plugins/qqq_intraday_option.py`

Responsibilities:

- set `definition_id = QQQ_INTRADAY_OPTION_PARITY_V1`
- apply defaults for DTE, delta, quote freshness, and parity profile
- expose runtime snapshot fields proving stock-signal parity is active

The plugin should explicitly log:

- stock signal family id
- underlying stop model id
- option contract selection band
- active `kernel_mode`

## Entrypoint Proposal

Add entrypoint:

- `./strategies/qqq_intraday_option_v1_strategy.py`

Shape:

- inherit `OptionKernelStrategy`
- `plugin_cls = QQQIntradayOptionPlugin`

No new legacy runtime shell should be introduced.

## Config Proposal

Add a new config entry in `./config.yml`:

- `id: qqq_intraday_option_v1_001`
- `registry_name: qqq_intraday_option_live_v1`
- `enabled: false` initially
- `kernel_mode: pure_plugin`

Recommended first-pass extras:

- `underlying: QQQ`
- `risk_symbol: OPT:QQQ:0DTE`
- `dte_preferred: 0`
- `dte_fallback_max: 1`
- `contracts`: small initial size
- `shared_cap_group_id`: new dedicated group for the replacement family
- `strict_scope_validation: true`
- `enable_live_parity_snapshot: true`

Do not reuse the retired strategy id.

## Rollout Plan

### Phase 1: design freeze

- lock the replacement architecture
- identify exactly which old QQQ option instance is being retired

### Phase 2: shadow validation

- run new strategy disabled for live execution or in shadow mode
- compare stock signal timestamps vs option signal timestamps
- verify direction parity and exit parity

### Phase 3: paper enablement

- enable only the new strategy instance
- keep the retired strategy disabled
- verify ownership, fills, and flatten behavior

### Phase 4: production cutover

- Telegram allowlist updated to the new strategy id only
- shared cap group cleaned up
- runbook updated to the replacement family

## Acceptance Criteria

The replacement is ready only if all of the following are true:

1. New strategy runs on `OptionKernelStrategy`
2. No new QQQ option legacy class is introduced
3. Entry direction matches the stock logic family
4. Exit reasons are auditable in underlying terms
5. Contract selection is strategy-scoped and quote-gated
6. Old QQQ option strategy is disabled for operational use
7. Runtime snapshots prove the replacement path is active

## Minimal Implementation Checklist

1. add `qqq_intraday_stock_signal.py`
2. add `qqq_intraday_option_position_policy.py`
3. register `QQQ_INTRADAY_OPTION_PARITY_V1`
4. add `qqq_intraday_option.py` plugin
5. add `qqq_intraday_option_v1_strategy.py`
6. add `config.yml` instance
7. disable the retired QQQ option instance
8. add unit tests for:
   - long/short mapping
   - underlying stop parity
   - force flatten parity
   - contract selection guardrails
   - ownership isolation

## Review Notes

The main engineering risk is not contract selection. It is silent logic drift between:

- QQQ stock signal path
- QQQ option entry path
- QQQ option exit path

This design avoids that by making stock logic the signal source of truth and keeping option-specific logic only where it actually belongs:

- contract choice
- quote gating
- execution
- option position management details
