# Option Strategy Rollout Evidence

目的：把每个 definition-driven option strategy 的 `replay / shadow / cutover` 证据要求固化成 definition contract，而不是继续靠口头约定。

权威工具：

- [build_option_strategy_rollout_evidence_report.py](./tool/report/build_option_strategy_rollout_evidence_report.py)
- [run_option_strategy_rollout_evidence_report.py](./tool/replay/run_option_strategy_rollout_evidence_report.py)

## 核心原则

每个 option strategy definition 都必须回答：

1. 用哪套 replay harness
2. 是否必须有 `replay_strategy_id`
3. 是否必须有 `live_parity_profile_id`
4. 是否必须开启 parity snapshot
5. 是否要求 state shadow acceptance
6. cutover 前必须满足什么策略级证据

## 当前 evidence contract

1. `option_ma_rollout_v1`
2. `qqq_intraday_option_rollout_v1`
3. `generic_intraday_momentum_rollout_v1`

## 当前判断

这个 report 只回答“rollout contract 是否齐备”。  
它不回答：

1. 真实 replay 是否已经通过
2. 真实 shadow 多日证据是否已经通过
3. 是否可以直接 live cutover
