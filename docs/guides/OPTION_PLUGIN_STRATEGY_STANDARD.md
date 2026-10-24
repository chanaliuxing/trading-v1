# Option Plugin Strategy Standard

## Purpose

Related long-range planning documents:

- `./docs/guides/OPTION_STRATEGY_FIRST_PRINCIPLES_MASTER_PLAN.md`
- `./docs/guides/OPTION_STRATEGY_TODO_TEST_ACCEPTANCE.md`

This document defines the required process for adding or modifying option strategies in this repository.

Primary goals:

- Keep strategy logic correct and traceable.
- Prevent "config looks changed, runtime still uses old logic".
- Prevent strategy isolation bugs across positions, orders, and IBKR sync.
- Make future strategy additions repeatable.

This standard applies to:

- New option strategies
- New option strategy instances on a different underlying
- Migration of legacy option strategies into the plugin kernel
- Any live change to QQQ/SPY MA option strategies or QQQ 0DTE option strategy

## Architecture

Current live option strategies are expected to run on the shared plugin kernel:

- Kernel: `./strategies/options_kernel/base.py`
- Plugin contract: `./strategies/options_kernel/plugin.py`
- Strategy definition registry: `./strategies/options_kernel/modules/strategy_definitions.py`
- Shared contract policy: `./strategies/options_kernel/modules/standard_option_contract_policy.py`
- Shared execution policy: `./strategies/options_kernel/modules/standard_option_execution_policy.py`
- Shared position policy: `./strategies/options_kernel/modules/ma_runner_position_policy.py`

The runtime chain is:

1. `config.yml` instantiates a strategy entry.
2. Strategy entrypoint creates an `OptionKernelStrategy`.
3. Plugin resolves a `definition_id`.
4. Definition chooses:
   - signal module
   - contract policy
   - execution policy
   - position policy
5. Kernel runs the strategy with strategy-scoped ownership, audit, and order intent flow.

## Supported Strategy Patterns

The plugin kernel supports two approved patterns for live option strategies:

### Pattern A: Option-native signal strategy

Signal, contract selection, and exits are all defined inside the option strategy family.

Examples:

- QQQ 1DTE MA option strategy
- SPY 1DTE MA option strategy

### Pattern B: Stock-signal, option-execution strategy

Signal generation reuses an existing stock strategy's market logic, but execution is done with options.

Required shape:

1. Reuse the stock-side feature/gate/policy semantics as the signal source of truth
2. Map stock `LONG` signal to option `CALL`, stock `SHORT` signal to option `PUT`
3. Keep contract selection, order routing, ownership, and broker sync inside the option plugin kernel
4. Keep stop-loss and flatten behavior anchored to the same underlying logic family, unless an explicit deviation is documented

This pattern is the correct path when the team wants:

- one market logic for QQQ stock and QQQ option
- fewer parallel strategy families to tune
- option execution without forking the QQQ stock decision model

## Source Of Truth

This repository has three separate configuration layers. They do not have equal authority.

### Layer 1: Strategy definition

File:

- `./strategies/options_kernel/modules/strategy_definitions.py`

This is the real logic router.

It decides:

- which signal module is used
- which position management policy is used
- which execution policy is used
- which contract selection policy is used

If the definition points to the wrong signal module, changing `config.yml` alone is not enough.

### Layer 2: Plugin

Files:

- `./strategies/options_plugins/qqq_1dte_ma10_reject.py`
- `./strategies/options_plugins/spy_1dte_ma10_reject.py`
- `./strategies/options_plugins/qqq_0dte_opt_live.py`

The plugin decides:

- which `definition_id` the strategy uses
- plugin-level default extras
- plugin-level overrides
- runtime observability payloads

Important: plugin code can override config values. Example: a plugin may force `min_open_interest = 0`. In that case, config is not the final source of truth.

### Layer 3: Strategy instance config

File:

- `./config.yml`

This defines:

- enabled/disabled state
- `strategy_id`
- capital group
- instance-specific parameters
- execution profile ids
- instance-level rollout behavior

This layer is only safe when Layer 1 and Layer 2 already point to the intended runtime path.

## Rule: Same Logic On Different Underlyings

If two strategies are meant to use the same logic, they must match at all three layers:

1. Same signal module family
2. Same replay strategy id
3. Same logic-related extras

Allowed differences:

- `underlying`
- `risk_symbol`
- `audit_namespace`
- `live_parity_profile_id`
- `shared_cap_group_id`
- `shared_cap_group_strategy_ids`

Not sufficient:

- "Just copy config"
- "Just set the same replay_strategy_id in config"

Correct approach:

1. Add or reuse a dedicated `StrategyDefinition`
2. Point the plugin `definition_id` to that definition
3. Align plugin defaults
4. Align config instance extras

## Required Files When Adding A New Strategy

For a brand-new option strategy:

1. Signal module
   - Add under `./strategies/options_kernel/modules/`
   - Implement signal building and pending-confirm flow
2. Strategy definition
   - Register in `./strategies/options_kernel/modules/strategy_definitions.py`
3. Plugin
   - Add under `./strategies/options_plugins/`
   - Must return the correct `definition_id`
4. Entrypoint strategy
   - Add under `./strategies/`
   - Inherit `OptionKernelStrategy`
5. Config instance
   - Add entry in `./config.yml`
6. Tests or verification harness
   - Add or update under `./tests/`
   - Or update `./strategies/options_kernel/harness.py` if parity testing is needed

## Required Files When Adding The Same Strategy To Another Underlying

If the logic is the same and only the underlying changes:

1. Reuse the same signal module if possible
2. Add a new `StrategyDefinition` for the new underlying
3. Add or reuse a plugin for that underlying
4. Ensure plugin `definition_id` points to the new definition
5. Add config entry
6. Verify runtime snapshot after restart

Do not duplicate logic into a second signal module unless the logic actually diverges.

## Live Strategy Isolation Rules

Every live option strategy must satisfy all of these:

### Identity

- `strategy_id` must be unique
- `underlying` must match the intended symbol
- `risk_symbol` must match the intended strategy family

### Capital isolation

- `shared_cap_group_id` must be explicit
- `shared_cap_group_strategy_ids` must contain only the strategies intended to share capital
- Different underlyings should use different cap groups unless there is an explicit reason not to

### Order and position isolation

- Use plugin kernel only
- Keep strategy-scoped ownership enabled
- Keep `strict_scope_validation: true`
- Keep `allow_same_option_symbol_cross_strategy` explicit

### Observability

- `enable_live_parity_snapshot: true`
- `live_parity_snapshot_root` must be strategy-family scoped
- `audit_namespace` must be correct

## Active Strategy Map

Current active plugin-kernel option strategies are expected to be maintained in these files:

- QQQ intraday stock-signal option:
  - Entrypoint: `./strategies/qqq_intraday_option_v1_strategy.py`
  - Plugin: `./strategies/options_plugins/qqq_intraday_option.py`
  - Definition: `./strategies/options_kernel/modules/strategy_definitions.py`
- SPY MA:
  - Entrypoint: `./strategies/spy_1dte_ma10_reject_v2_strategy.py`
  - Plugin: `./strategies/options_plugins/spy_1dte_ma10_reject.py`
  - Definition: `./strategies/options_kernel/modules/strategy_definitions.py`

Legacy files may still exist, but active live tuning should happen on the enabled plugin path, not the disabled legacy path.

### Retirement rule for old QQQ option paths

When a new QQQ option family is introduced to replace an old one:

1. The old QQQ option strategy instance must be disabled in `./config.yml`
2. The old strategy id must be removed from any allowlist, shared-cap group, and rollout checklist that should no longer include it
3. New tuning must stop on the retired plugin/config path
4. The replacement must remain on the plugin kernel only

Do not keep two QQQ option families live just because both are technically pluggable. If one is declared retired, it must stop being part of the operational path.

## Safe Change Procedure

Use this sequence every time.

### Case A: Parameter-only change

Use this when:

- signal module stays the same
- definition stays the same
- plugin `definition_id` stays the same

Steps:

1. Change plugin defaults if the strategy intentionally enforces the value there
2. Change `config.yml`
3. Restart runtime
4. Verify snapshot log for the intended values

### Case B: Logic family change

Use this when:

- changing signal module
- changing replay strategy family
- making QQQ logic equal to SPY logic
- moving a strategy from one plugin family to another

Steps:

1. Add or update the `StrategyDefinition`
2. Update plugin `definition_id`
3. Update plugin default `replay_strategy_id`
4. Update config extras
5. Restart runtime
6. Verify snapshot log
7. Verify per-minute gate logs

If step 1 or step 2 is skipped, the system may still run old logic even if config looks correct.

### Case C: Replace an old option strategy with a stock-signal option strategy

Use this when:

- a legacy or standalone option signal family is being retired
- the new option strategy should follow QQQ stock logic
- signal generation and execution are intentionally split

Steps:

1. Identify the stock strategy logic that becomes the signal source of truth
2. Add a dedicated option `StrategyDefinition` for the replacement family
3. Add a plugin that maps stock signal direction to option contract side
4. Reuse shared option contract/execution modules where possible
5. Add a position policy that documents whether exits are driven by:
   - underlying parity logic
   - option premium logic
   - or a hybrid of both
6. Add a new config entry with a new `strategy_id`
7. Disable the retired QQQ option instance in config
8. Remove the retired strategy from operational allowlists and shared-cap groups if it should no longer participate
9. Restart runtime and verify replacement snapshot logs

If step 7 or step 8 is skipped, the retired strategy can still interfere with ownership, alerts, or capital accounting.

## Mandatory Verification Checklist

After any live strategy addition or migration, verify all of the following:

### Static verification

- Strategy entry exists in `./config.yml`
- Plugin points to the intended `definition_id`
- Definition points to the intended signal module
- Definition uses the intended policies
- Capital group and strategy ids are correct

### Runtime verification

After restart, verify in logs:

- `option_ma_strategy_snapshot`
- `option_kernel_pure_subscribed`
- `qqq_1dte_ma_minute_heartbeat` or equivalent strategy heartbeat
- no unexpected `WARMUP_NOT_READY` after preload period
- no unexpected `POSITION_OPEN` when flat
- no unexpected cross-strategy ownership conflicts

### Behavioral verification

- Strategy can produce `SIGNAL_READY`
- Strategy can produce `SETUP_READY_WAIT_CONFIRM`
- Expected block reasons match intended logic
- Contract selection uses intended DTE and delta range
- Orders and positions remain scoped to the correct strategy
- For stock-signal option strategies: underlying-side entry, stop, and flatten reasons match the stock logic family

## Common Failure Modes

### Failure 1: Config changed, runtime logic did not change

Cause:

- plugin still points to the old `definition_id`
- definition still points to the old signal module

Fix:

- align definition and plugin, not just config

### Failure 2: Same strategy copied to another symbol, but behavior diverges

Cause:

- one underlying still uses a different signal module
- plugin default extras diverge

Fix:

- compare definition, plugin, and config together

### Failure 3: Orders or positions appear mixed across strategies

Cause:

- reused strategy ids
- incorrect cap group setup
- disabled scope validation

Fix:

- unique `strategy_id`
- correct `shared_cap_group_id`
- keep `strict_scope_validation: true`

### Failure 4: Strategy looks enabled but never trades

Cause:

- wrong signal module
- wrong time window
- quote gating
- stale entry quote policy mismatch
- warmup or preload not ready

Fix:

- inspect snapshot and minute heartbeat logs first
- do not guess from config alone

## Review Standard For Any New Strategy PR

Before merging or enabling a new strategy, review must answer these questions:

1. Which file is the source of truth for signal logic?
2. Which `definition_id` is actually used at runtime?
3. Which signal module is actually bound?
4. Which policy handles exits?
5. Which policy handles contract selection?
6. Which config values are overridden by the plugin?
7. What is the capital isolation boundary?
8. What is the order/position isolation boundary?
9. What runtime logs prove the strategy is using the intended path?

If any answer is unclear, the strategy is not ready for live enablement.

## Recommended Minimal Commands

Useful checks before enabling:

```bash
rg -n "qqq_1dte_ma10_reject|spy_1dte_ma10_reject|qqq_0dte_opt_live" ./config.yml ./strategies
```

```bash
rg -n "option_ma_strategy_snapshot|option_kernel_pure_subscribed|SIGNAL_READY|SETUP_READY_WAIT_CONFIRM" ./logs/trader.log
```

## Final Rule

For live option strategies in this repository:

- Do not treat `config.yml` as the only source of truth.
- Do not tune disabled legacy files and assume the enabled strategy changed.
- Do not add a new underlying by copy-pasting config only.
- Do not fork QQQ stock logic into another standalone option logic family unless the divergence is intentional and documented.
- Always align definition, plugin, and config together.

That is the minimum standard required to avoid silent logic drift.
