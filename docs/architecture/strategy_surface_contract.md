# Strategy Surface Contract

## Purpose

The live strategy surface is not a production permission list. It is an inventory
of enabled strategies and their execution/data paths. Each row must identify the
path type, contract readiness, and the maximum permission tier allowed by the
available evidence.

## Path Types

- `OPTIONS_KERNEL_PURE_PLUGIN`: option kernel strategy running through the pure plugin path.
- `STOCKS_KERNEL_EXECUTION_PATH`: stocks kernel path with separate stock execution semantics.
- `DIRECT_STRATEGY_EXECUTION_PATH`: strategy source directly emits/submits intents or imports execution adapters.
- `LEGACY_STRATEGY_EXECUTION_PATH`: legacy strategy path without current production contracts.
- `OPTION_PRIVATE_DATA_PATH`: option strategy using private/non-normalized data path.
- `UNKNOWN_PATH`: path cannot be classified from config/source evidence.

## Permission Tiers

- `PRODUCTION_ELIGIBLE`: all production contracts are ready and explicit parity evidence exists.
- `CANARY_ONLY`: mostly contract-ready but requires limited exposure and parity/soak proof.
- `SHADOW_ONLY`: may run for observation but must not have full production order permission.
- `DISABLED_UNTIL_CONTRACT_PARITY`: blocked until data/risk/execution/fill contracts are normalized.

Current default is conservative: pure plugin and stocks-kernel paths are
`CANARY_ONLY` unless explicit production parity proof is added later.

## Contract Readiness Fields

Each row includes:

- `data_contract_ready`
- `session_contract_ready`
- `risk_contract_ready`
- `execution_contract_ready`
- `fill_attribution_ready`
- `production_contract_ready`
- `blockers`
- `warnings`

`production_contract_ready` is false unless every required contract is true and
there is explicit production parity proof. Direct, legacy, private-data, and
blockers-disabled paths are not production eligible.

## Examples

- Pure plugin option strategy: `OPTIONS_KERNEL_PURE_PLUGIN`, `CANARY_ONLY`,
  warning `production_parity_proof_required`.
- Stocks kernel strategy: `STOCKS_KERNEL_EXECUTION_PATH`, `CANARY_ONLY`, not
  equivalent to pure plugin until parity proof exists.
- QQQ direct strategy: `DIRECT_STRATEGY_EXECUTION_PATH`, `SHADOW_ONLY`.
- NVDA legacy/private strategy: `LEGACY_STRATEGY_EXECUTION_PATH` or
  `OPTION_PRIVATE_DATA_PATH`, `DISABLED_UNTIL_CONTRACT_PARITY`.
- Blockers-disabled strategy: `SHADOW_ONLY`, blocker `stock_execution_blockers_disabled`.
