# Option Launch Readiness Detailed Plan

目的：从第一性原理出发，回答 4 个问题：

1. 我们距离目标总共完成了多少
2. `QQQ` 股票信号复刻为 `QQQ option` 信号，是否已经完全落地
3. 下周开盘前，是否可以正式跑
4. 还需要补哪些工作，才能不跑偏地推进

---

## 1. 第一性原理拆解

“可正式跑”的最小充分条件不是“代码已经存在”，而是下面 5 层同时成立：

1. `Definition Layer`
- 策略定义、模块装配、配置 contract 正确

2. `Behavior Layer`
- `QQQ option` 的入场/退出语义真实继承自 `QQQ` 股票信号家族

3. `Evidence Layer`
- replay / shadow / state acceptance 有真实样本，不是纸面 contract

4. `Operational Layer`
- runbook、监控、回滚、shared-cap、allowlist、审计链完整

5. `Release Gate Layer`
- 有明确的开盘前 go/no-go 清单，任何关键项缺失都自动判定为 `BLOCK`

只要上面任一层没闭环，就不能叫“正式可跑”。

---

## 2. 当前总进度判断

### 2.1 平台总进度

结论：

1. `平台骨架完成度`：约 `85%`
2. `active strategy 真实放行完成度`：约 `35%`
3. `下周开盘可正式跑完成度`：约 `25%`
4. `整体按最终需求的总进度`：约 `60%`

### 2.2 为什么不是更高

因为当前高完成度主要集中在：

1. definition / plugin-kernel / contract / inventory / docs
2. active strategy 聚合报告
3. 基础 runner 和 acceptance pipeline

但当前低完成度集中在最关键的放行层：

1. `QQQ` 没有拿到 post-instrumentation 多日 clean pass shadow acceptance
2. `SPY MA` 还没有 real matched replay day
3. `SPY MA` live-shadow 还没有 `matched_entries > 0`
4. 还没有一份“下周开盘 go/no-go”硬门槛文档与一键检查结果

这意味着：

1. 平台能跑
2. 结构也基本对
3. 但还不能宣称“可以正式交易”

---

## 3. QQQ 复刻落地判断

### 3.1 已完成部分

结论：`QQQ option` 的“定义层复刻”已经落地，约 `80%`

已经成立：

1. active strategy 已切到 `qqq_intraday_option_v1_001`
2. definition 已绑定 `QQQ_INTRADAY_OPTION_PARITY_V1`
3. signal module 已绑定 `qqq_intraday_stock_signal_v2`
4. direction mapping 已固定为：
- `LONG -> CALL`
- `SHORT -> PUT`
5. exit 语义已按 underlying parity 设计：
- hard stop
- logic retreat
- EOD flatten
6. rollout contract 已绿色
7. platform admission 已绿色

### 3.2 未完成部分

结论：`QQQ option` 的“交易前放行层复刻”还没有落地完成，约 `40%`

还没成立：

1. 没有足够的 post-instrumentation 多日真实 state acceptance 证据
2. 当前 cutover recommendation 仍然是 `BLOCK`
3. 还没有达到“至少 3 个 clean pass day”的最低放行样本
4. 还没有形成“下周开盘前 QQQ go/no-go”结论文件

### 3.3 最终判断

所以对这个问题必须非常明确：

1. `QQQ 股票信号作为 QQQ option 信号`：
- `设计层 / 装配层 / 单元测试层`：已基本落地
2. `是否已经完全落地到可正式跑`：
- `没有`

---

## 4. 下周开盘是否能正式跑

当前结论：

1. `不能直接判定可以正式跑`
2. 当前系统状态应视为：`有机会，但仍 blocked`

阻塞原因只有两类，而且都是真阻塞，不是文书问题。

### 4.1 QQQ 阻塞

当前 active evidence 结论：

1. `qqq_intraday_option_v1_001`
- `qqq_state_shadow_cutover_blocked`

硬原因：

1. post-instrumentation day 只有 `1`
2. pass day 为 `0`
3. fail day 为 `1`
4. 没有 state audit 真实通过样本

### 4.2 SPY 阻塞

当前 active evidence 结论：

1. `spy_1dte_ma10_reject_0324_001`
- `spy_replay_parity_no_real_matched_day`
- `spy_live_shadow_no_matched_entries`

硬原因：

1. 还没找到 `old_count > 0` 且 `new_count > 0` 且 `matched=true` 的真实日
2. 当前 live-shadow 没有 `matched_entries > 0`

---

## 5. 开盘前必须完成的 Todo

原则：只做 active path，先 `QQQ`，再 `SPY`，最后才谈第三类样本。

### P0: QQQ 开盘前放行门

#### P0.1 收集真实 state shadow 样本

目标：

1. 取得至少 `3` 个 post-instrumentation 交易日
2. 至少 `3` 个 clean `PASS`

执行：

1. 开盘后跑 `run_qqq_state_shadow_acceptance_batch.py`
2. 优先用真实线上或 ClickHouse 数据，不再只看 local 空样本
3. 每天收盘后更新 cutover recommendation

完成定义：

1. `post_instrumentation_day_count >= 3`
2. `post_instrumentation_pass_days >= 3`
3. `post_instrumentation_fail_days == 0`
4. `cutover_still_blocked == false`

#### P0.2 固化 QQQ go/no-go 清单

目标：

1. 下周开盘前，只看一份 QQQ 放行结论

必须检查：

1. state acceptance 通过
2. strategy enabled 状态正确
3. live parity snapshot 开启
4. Telegram / audit / logs / snapshot path 正常
5. 回滚策略明确

完成定义：

1. 新增 `QQQ_LAUNCH_GO_NO_GO.md`
2. 给出明确 `GO` 或 `BLOCK`

### P1: SPY parity 闭环

#### P1.1 找到 legacy/new 行为差异根因

目标：

1. 把 `2026-03-30 old_count=0 new_count=7` 收敛到具体 gate 或 signal 差异

检查顺序：

1. minute-level signal 生成差异
2. entry window 差异
3. data freshness / guard 差异
4. option selector / quote gating 差异
5. runtime state / ownership / open-order guard 差异

完成定义：

1. 产出一份 root-cause 文档
2. 每个差异都有文件级定位
3. 每个差异有修复归属和测试

#### P1.2 跑出至少一个 real matched day

目标：

1. 至少一个交易日满足：
- `old_count > 0`
- `new_count > 0`
- `matched = true`

完成定义：

1. 该日 replay report 固化到 runtime/state
2. active evidence status 不再报 `spy_replay_parity_no_real_matched_day`

#### P1.3 跑出 live-shadow matched entries

目标：

1. 对已经 replay matched 的交易日，跑 live-shadow day report

完成定义：

1. `matched_entries > 0`
2. active evidence status 不再报 `spy_live_shadow_no_matched_entries`

### P2: 统一开盘前放行门

#### P2.1 新增 launch gate 文档和一键入口

目标：

1. 不再人工拼接多个报告判断

必须汇总：

1. platform admission
2. rollout evidence
3. QQQ cutover recommendation
4. SPY parity status
5. active evidence summary
6. go/no-go checklist

完成定义：

1. 新增 `OPTION_LAUNCH_GO_NO_GO_GATE.md`
2. 新增一键 runner 输出 `GO/BLOCK`

#### P2.2 定义开盘前绝对阻塞项

任何一项为真，直接 `BLOCK`：

1. active evidence recommendation != ready
2. QQQ cutover blocked
3. SPY replay 未闭环
4. SPY shadow 未闭环
5. active config contract 非绿
6. rollback 路径未验证

---

## 6. 更深一层的任务拆解

### 6.1 QQQ 任务拆解

1. 确认 state audit 在真实交易日是否持续写入
2. 如果没有写入，先修 instrumentation，不准继续谈 cutover
3. 如果有写入但缺状态链，定位缺的是 runtime/order/flatten/protection 哪一条
4. 如果有状态链但 FAIL，定位失败原因是否来自：
- no state records
- missing runtime live
- missing order progression
- missing protected state
- flatten missing terminal
5. 修复后重新跑 batch
6. 连续累计到 3 个 clean PASS day

### 6.2 SPY 任务拆解

1. 产出 minute-by-minute legacy/new signal diff
2. 先对 `2026-03-30` 做 full diff
3. 再对 `2026-03-26` 做 full diff
4. 把差异按层分桶：
- signal not emitted
- signal emitted but blocked
- contract selected differently
- envelope extras only
5. 先修“会影响入场数量”的差异
6. 再修“不会影响数量但会影响 parity compare”的差异
7. 修复后重跑 replay
8. replay 通过后再跑 shadow

### 6.3 发布任务拆解

1. 写 go/no-go 文档
2. 写 launch runner
3. 写回滚清单
4. 明确开盘前谁看哪份报告
5. 明确开盘后第一小时怎么观测

---

## 7. 本周最终目标

本周不追求“看起来所有东西都完成”，只追求一个明确结果：

1. 如果 `QQQ` 和 `SPY` 证据都闭环，就给出 `GO`
2. 如果任一主路径没闭环，就明确给出 `BLOCK`

也就是说，本周真正的交付物不是更多策略，而是：

1. 更清晰的放行标准
2. 更细的执行 todo
3. 能被一键验证的 go/no-go 结论
