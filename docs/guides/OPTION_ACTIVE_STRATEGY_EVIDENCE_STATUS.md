# Option Active Strategy Evidence Status

目的：把 active option 策略的真实证据收敛成一个统一出口，直接回答每条 active 策略当前是 `ready` 还是 `blocked`，以及 `why`。

## 一键入口

```bash
./venv/bin/python ./tool/replay/run_option_active_strategy_evidence_status.py
```

默认输出：

- `./runtime/state/option_active_strategy_evidence_status.json`

## 证据来源

平台与合同层：

1. `./runtime/state/option_platform_admission_gate.json`
2. `./runtime/state/option_strategy_rollout_evidence_report.json`

策略真实证据层：

1. `QQQ`: `./runtime/state/qqq_state_shadow_acceptance/qqq_state_shadow_cutover_recommendation.json`
2. `SPY MA replay`: `runtime/state/spy_option_ma_parity*.json`（自动取最新）
3. `SPY MA shadow`: `runtime/state/spy_option_ma_shadow_parity_*.json`（自动取最新）

## 判定规则（当前版本）

统一前提：

1. platform admission 不能 blocked
2. rollout contract 必须 pass

`QQQ`：

1. `cutover_still_blocked=false` 才能 ready

`SPY MA`：

1. replay 至少有一天同时满足：
   - `matched=true`
   - `old_count>0`
   - `new_count>0`
2. live-shadow 需要 `matched_entries>0`

## 输出字段

核心字段：

1. `recommendation`: `ACTIVE_STRATEGIES_EVIDENCE_READY` 或 `BLOCK`
2. `blocked_strategy_ids`
3. `strategies[].readiness`
4. `strategies[].reasons`
5. `strategies[].evidence`

## 使用方式

每次推进只需要这三步：

1. 先跑 platform + rollout 固定报告
2. 再跑 qqq / spy 的真实证据工具
3. 最后跑本报告，作为唯一 active readiness 结论
