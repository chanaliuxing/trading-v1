# Option Platform Execution Roadmap

目的：把当前 option 平台从“多个已落地能力”收口成一条统一执行路线，避免后续继续碎片化推进。  
原则：后续所有推进都必须优先回答 3 个问题：

1. 当前是在补平台 contract，还是在补策略实证
2. 当前动作是否直接服务于 cutover 证据
3. 当前动作是否会影响 `QQQ` 稳定路径

## 1. 当前判断

结论：有机会，而且现在已经不再缺平台骨架，缺的是按顺序产出真实证据。

当前已完成：

1. option platform contract 已成型
2. option platform admission gate 已绿色
3. option strategy rollout evidence contract 已绿色
4. active option 主路径只有两条：
   - `qqq_intraday_option_v1_001`
   - `spy_1dte_ma10_reject_0324_001`

当前未完成：

1. `QQQ` 的真实 multi-day state shadow acceptance 证据
2. `SPY MA` 的真实 replay parity + live-shadow parity 证据闭环
3. 第三类 archetype 的真实策略级验收，而不只是 platform sample
4. CI / release pipeline 的强门槛接入

## 2. 执行原则

后续推进必须遵守：

1. 先平台治理，后策略证据
2. 先 active strategy，后 disabled sample
3. 先 `QQQ` 与 `SPY MA` 两条主路径，后第三类样本
4. 没有真实 evidence 的策略，不能宣称 ready
5. 没有通过 replay/shadow 的策略，不能谈 cutover

## 3. 当前状态分层

### 3.1 平台层

已完成：

1. inventory
2. assembly-adapter compliance
3. platform coverage
4. active config contract
5. rollout evidence contract
6. trader-facing schema / template / checklist / risk matrix

权威报告：

1. [OPTION_PLATFORM_ADMISSION_GATE.md](./docs/guides/OPTION_PLATFORM_ADMISSION_GATE.md)
2. [OPTION_STRATEGY_ROLLOUT_EVIDENCE.md](./docs/guides/OPTION_STRATEGY_ROLLOUT_EVIDENCE.md)
3. [OPTION_ACTIVE_STRATEGY_EVIDENCE_STATUS.md](./docs/guides/OPTION_ACTIVE_STRATEGY_EVIDENCE_STATUS.md)
4. [OPTION_LAUNCH_READINESS_DETAILED_PLAN.md](./docs/guides/OPTION_LAUNCH_READINESS_DETAILED_PLAN.md)
5. [OPTION_LAUNCH_EXECUTION_TODO.md](./docs/guides/OPTION_LAUNCH_EXECUTION_TODO.md)
6. [OPTION_LAUNCH_GO_NO_GO_GATE.md](./docs/guides/OPTION_LAUNCH_GO_NO_GO_GATE.md)
7. [QQQ_LAUNCH_GO_NO_GO.md](./docs/guides/QQQ_LAUNCH_GO_NO_GO.md)

### 3.2 策略层

`QQQ`：

1. state shadow pipeline 已有
2. 需要继续积累真实多日 evidence

`SPY MA`：

1. readiness script 已有
2. replay parity runner 已有
3. live-shadow parity day runner 已有
4. 目前还缺“有真实 entry 的交易日样本”

第三类样本：

1. contract 已接入
2. 还是 disabled sample
3. 暂不进入当前主路径 cutover 关键路径

## 4. 单线执行计划

### Phase 1: 锁定当前 active 路径

目标：

1. active strategy 列表不再漂移
2. active strategy config 不再缺 `definition_id`
3. active strategy 始终保持 platform admission gate 绿色

完成定义：

1. [option_platform_admission_gate.json](./runtime/state/option_platform_admission_gate.json) 持续绿色

当前状态：

1. 已完成

### Phase 2: 跑 QQQ 真实 evidence

目标：

1. 累积 `QQQ` post-instrumentation 多日真实 state shadow evidence
2. 至少拿到可评估 cutover review 的基础样本

执行命令：

```bash
./venv/bin/python ./tool/replay/run_qqq_state_shadow_acceptance_batch.py \
  --source local \
  --auto-days \
  --state-instrumentation-start-date 2026-04-03 \
  --max-days 10
```

完成定义：

1. `QQQ` 多日 acceptance 报告不再是 `pre_instrumentation`
2. 开始出现真实 `PASS/WARN/FAIL`

### Phase 3: 跑 SPY MA replay parity

目标：

1. 找出真正有 entry 的交易日
2. 不再停留在 `matched=true but 0 trades`

执行命令：

```bash
./venv/bin/python ./scripts/run_spy_option_ma_parity_range.py \
  --start 2026-03-20 \
  --end 2026-03-31 \
  --out ./runtime/state/spy_option_ma_parity_range_2026-03-20_2026-03-31.json
```

完成定义：

1. 至少有 1 个 `old_count > 0` 且 `new_count > 0` 的匹配日

### Phase 4: 跑 SPY MA live-shadow parity

前提：

1. 已找到有 entry 的 replay day

执行命令：

```bash
./venv/bin/python ./scripts/run_spy_option_ma_shadow_parity_day.py \
  --date YYYY-MM-DD \
  --out ./runtime/state/spy_option_ma_shadow_parity_YYYY-MM-DD.json
```

完成定义：

1. 生成 trader-facing parity report
2. `matched_entries > 0`

当前状态：

1. 已产出报告，但仍 blocked
2. `2026-03-30`: `replay_expected_entries=3`, `matched_entries=0`
3. `2026-03-26`: `replay_expected_entries=0`, `live_shadow_entries=1`（口径不一致样本）

### Phase 5: 汇总 active strategy 真实证据

目标：

1. 不再只看 contract 是否存在
2. 改为看 active strategy 的真实 evidence 是否齐

需要汇总：

1. QQQ state shadow acceptance
2. SPY MA replay parity
3. SPY MA live-shadow parity
4. option platform admission gate
5. option rollout evidence report

完成定义：

1. 每条 active strategy 都有明确 `ready / blocked / why`

当前状态：

1. 已新增统一聚合入口：

```bash
./venv/bin/python ./tool/replay/run_option_active_strategy_evidence_status.py
```

2. 当前输出会直接给出 active strategy 的 `readiness/reasons/evidence`

### Phase 6: 再决定第三类样本是否进入实证阶段

目标：

1. 只在前两条 active 路径有稳定 evidence 后，才给第三类 archetype 升级到真实验收

## 5. 不允许的推进方式

1. 为了看起来快，继续加第四、第五类 sample strategy
2. 在没有真实 evidence 前讨论 cutover
3. 在 `QQQ` 没拿到多日 state shadow 样本前改主逻辑
4. 用单日 `0 trades` parity 结果宣称策略已验收

## 6. 当前立即下一步

唯一正确的下一步：

1. 批量跑 `SPY MA replay parity` 找到有真实 entry 的日子
2. 同时继续收集 `QQQ` 开盘后的真实 state shadow evidence

也就是：

1. `QQQ` 走 state-shadow-first
2. `SPY MA` 走 parity-first

两条线并行，但都服务于同一目标：active option 主路径真实 evidence 闭环。
