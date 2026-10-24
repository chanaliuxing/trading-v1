# QQQ State Machine Upgrade Todo / Test / Acceptance

目的：
从第一性原理出发，为当前交易系统的状态机升级定义一份可执行清单，明确哪些状态机必须保留独立语义，哪些地方必须统一收口，并把 `QQQIntradayStrategy` 作为全程不可破坏的基线系统。

第一约束：
`QQQIntradayStrategy` 不是这次重构的实验对象，而是验收标尺。任何状态机升级都必须先保护它的实盘行为，再逐步收口其他模块。

代码参考：
- [qqq_intraday_strategy.py](./strategies/qqq_intraday_strategy.py)
- [runtime_store.py](./services/strategy_runtime_v2/runtime_store.py)
- [coordinator.py](./services/strategy_runtime_v2/coordinator.py)
- [execution_state_model.py](./services/exec/execution_state_model.py)
- [flatten_controller.py](./services/exec/flatten_controller.py)
- [order_lifecycle.py](./strategies/stocks_kernel/modules/order_lifecycle.py)

---

## 1. First-Principles Conclusion

当前系统不应该收敛成一个超大状态机。

原因：
1. 策略运行态、订单态、flatten 态、protection 态解决的是不同问题。
2. 这些状态属于不同实体：
   - strategy + symbol
   - order_ref
   - flatten workflow
   - protection lane
3. 如果强行并成一个状态机，会出现组合爆炸，最终变成不可维护的巨型枚举。

因此正确方向不是“只保留一个状态机”，而是：
1. 保留多个独立状态机
2. 增加一个统一状态内核
3. 所有迁移只允许通过统一内核发生
4. 所有状态变化都必须可审计、可回放、可恢复

---

## 2. Current Problem Statement

当前不合理的点，不是状态机数量多，而是它们彼此脱节：

1. `RuntimeLifecycle` 表达策略级交易阶段，但目前仍有策略直接 mutate state。
2. `OrderLifecycleModule` 只覆盖局部订单收敛逻辑，与统一执行状态模型未完全对齐。
3. `FlattenController` 已存在，但业务链路仍有 scattered note write 和 scattered completion 判断。
4. `execution_state_model.py` 已给出统一语义，但尚未成为唯一迁移入口。
5. QQQ 主策略稳定且真实可用，但当前系统没有把它显式定义为 upgrade baseline。

---

## 3. Non-Negotiable Guardrails

### 3.1 QQQ Behavior Freeze

在整个升级周期内，`QQQIntradayStrategy` 必须满足：

1. 不修改真实 alpha 逻辑：
   - 入场规则
   - 退出规则
   - 止损/止盈判定
   - 信号生成节奏
2. 不因为状态机重构新增：
   - 漏单
   - 重复单
   - 提前平仓
   - 延迟平仓
   - 错误 quarantine
3. 所有新状态机能力先以：
   - shadow
   - dual-write
   - audit-only
   形式接入，不先接管 QQQ 主路径

### 3.2 Rollback Safety

任何迁移必须支持：

1. feature flag 关闭
2. 回退到 legacy state writer
3. 回退后不影响：
   - 在途订单
   - 已有持仓
   - flatten 正在进行中的工作单

### 3.3 State Ownership

任何状态必须有唯一 owner：

1. `runtime` 只能由 runtime coordinator / state kernel 迁移
2. `order lifecycle` 只能由 order state adapter / state kernel 迁移
3. `flatten lifecycle` 只能由 flatten controller / state kernel 迁移
4. `protection state` 只能由 protection controller / state kernel 迁移

禁止策略、reconciler、handler 直接散写最终状态。

---

## 4. Target Architecture

### 4.1 Independent State Machines To Keep

必须保留 4 套独立状态机：

1. `Runtime State Machine`
   - 策略在某 symbol 上的运行阶段
   - 例如：`flat / preview / intent_submitted / pending_entry / live / pending_exit / quarantine`

2. `Order State Machine`
   - 单个 order_ref 的 broker 执行态
   - 例如：`created / submitted / accepted / partial / filled / rejected / cancelled`

3. `Flatten State Machine`
   - 一次 flatten workflow 是否已真正完成闭环
   - 例如：`created / submitted / pending / confirmed / failed / quarantined`

4. `Protection State Machine`
   - 仓位是否处于受保护状态、恢复状态或冻结状态
   - 例如：`protected / recovery / failed / frozen`

### 4.2 One Unified Coordination Kernel

必须新增或强化一个统一状态内核，职责只有 4 类：

1. 校验合法迁移
2. 校验跨状态机一致性约束
3. 记录统一迁移事件
4. 提供幂等、乱序容忍、恢复重建支持

---

## 5. Todo List

### Phase 0: Freeze QQQ Baseline

1. 盘点 `QQQIntradayStrategy` 当前真实主路径：
   - entry
   - exit
   - stop
   - TP / flatten
   - broker callback handling
2. 固化 trader-facing baseline 文档：
   - 什么时候进
   - 为什么不进
   - 为什么减仓
   - 为什么平仓
3. 生成 replay baseline 数据集，至少覆盖：
   - 正常趋势日
   - 震荡反复日
   - 开盘强波动日
   - broker callback 非理想日
4. 明确哪些行为属于 QQQ 真值，后续只能 parity，不得“顺手优化”

### Phase 1: Freeze State Contracts

1. 冻结 4 套状态机的正式状态定义
2. 冻结每套状态机的合法迁移矩阵
3. 定义统一迁移事件契约：
   - `event_id`
   - `trace_id`
   - `entity_type`
   - `entity_id`
   - `before_state`
   - `event_type`
   - `after_state`
   - `reason`
   - `source_module`
4. 定义跨状态机一致性规则
5. 明确每种状态的唯一 owner 模块

### Phase 2: Build State Kernel

1. 实现统一 transition validator
2. 实现统一 event recorder
3. 实现 idempotency contract
4. 实现 out-of-order event tolerance contract
5. 实现 snapshot + replay 恢复接口
6. 保留 feature flag，默认只观测不接管

### Phase 3: Shadow QQQ Without Behavior Change

1. 在 QQQ 主链路旁路双写新状态事件
2. 新状态内核只记录，不参与交易决策
3. 每日输出 parity 报告：
   - entry time parity
   - exit time parity
   - block reason parity
   - flatten completion parity
4. 收集差异样本，逐项解释，不允许 silent mismatch

### Phase 4: Flatten First

1. flatten 生命周期先统一收口
2. reconciler 不再直接散写 flatten 终态
3. fill / reject / cancel 不再各自发明 flatten 完成语义
4. flatten completion rule 统一为：
   - broker qty == 0
   - broker open orders == 0
   - DB position == 0
   - DB working orders == 0
5. 对 QQQ 仍先保持 dual-write，不立即切主控

### Phase 5: Order Lifecycle Convergence

1. 把 `OrderLifecycleModule` 对齐到统一 order state contract
2. 区分：
   - created
   - submitted
   - accepted
   - partial
   - filled
   - terminal reject/cancel
3. 将 submit/ack/fill/terminal 都接入统一迁移事件
4. 引入 duplicate / stale / orphan / mismatch 测试矩阵

### Phase 6: Runtime State Convergence

1. 去掉策略壳中散落的直接 `mutate_state`
2. runtime 状态迁移全部走 coordinator + kernel
3. quarantine / flat / pending_entry / live 的切换全部可审计
4. runtime 和 broker truth 不一致时，只允许进入：
   - recoverable path
   - quarantine path
   不能 silent overwrite

### Phase 7: QQQ Cutover Last

1. 先在非核心策略切主路径
2. QQQ 最后切换
3. QQQ 切换前必须通过：
   - replay parity
   - shadow parity
   - failure injection
   - restart recovery
4. QQQ 切换时必须保留快速回滚开关

### Phase 8: QQQ Acceptance Pipeline

1. 基于 QQQ 四条 shadow state 链生成 daily readiness
2. 基于 readiness + baseline contract 生成 acceptance decision
3. acceptance 输出必须明确：
   - `PASS / WARN / FAIL`
   - `cutover_blocked`
   - `reasons`
   - `next_actions`
4. acceptance pipeline 必须可长期日更运行，不能只作为一次性脚本

---

## 6. Test Plan

### 6.1 QQQ Baseline Replay Tests

必须为 `QQQIntradayStrategy` 建立 baseline replay：

1. 校验 entry timestamp 一致
2. 校验 exit timestamp 一致
3. 校验 direction 一致
4. 校验 block reason 一致
5. 校验 flatten reason 与完成时刻一致
6. 校验 broker callback 次序变化时，交易结果仍不变

### 6.2 State Transition Unit Tests

每套状态机都要覆盖：

1. 合法迁移
2. 非法迁移
3. 重复事件幂等
4. 乱序事件保护
5. 终态后拒绝再次迁移
6. quarantine 进入与退出规则

### 6.3 Cross-State Consistency Tests

验证多状态机之间不出现自相矛盾：

1. `runtime=live` 时，不允许 `flatten=confirmed` 且 broker qty 非零
2. `runtime=flat` 时，不允许本策略仍有 owned live order
3. `protection=failed` 时，不允许继续新开仓
4. `flatten=pending/confirmed` 时，不允许并发 normal entry
5. `order=accepted` 不等于 `runtime=live`

### 6.4 Dual-Write Parity Tests

对 legacy 与 new kernel 做逐事件比对：

1. 允许新增 audit 字段
2. 不允许交易行为变化
3. 不允许 block/submit/fill/flatten 时刻发生实质漂移
4. 每个 mismatch 都必须带原因分类：
   - contract gap
   - event ordering gap
   - stale read gap
   - real bug

### 6.5 Failure Injection Tests

必须覆盖非理想实盘条件：

1. fill 回调丢失
2. order update 乱序
3. submit 成功但 ack 延迟
4. DB update 临时失败
5. broker snapshot 短暂不可读
6. restart 发生在：
   - entry submitted 后
   - partial fill 后
   - flatten pending 中

### 6.6 Flatten Completion Tests

1. EOD flatten 闭环
2. manual flatten 闭环
3. panic flatten 闭环
4. broker flat 但 DB 未归零时保持 `pending`
5. broker flat 且 DB/working order 全收口时进入 `confirmed`
6. stale flatten 超时进入 `quarantined`

### 6.7 Runtime Recovery Tests

1. broker 有仓、runtime 丢失时能恢复
2. runtime=live 但 broker flat 时能收口
3. foreign order / foreign position 不被误认领
4. quarantine 后不会自动回到正常 live，除非显式恢复路径通过

### 6.8 Production Shadow Tests

至少连续多日 shadow：

1. 每日输出 parity 摘要
2. 差异率必须稳定下降
3. 不允许出现新增 QQQ 实盘故障
4. reconcile auto-heal 率、quarantine 率、orphan 率必须进入可接受区间

---

## 7. Acceptance Criteria

### 7.1 QQQ Protection Acceptance

以下条件必须全部满足，否则视为未通过：

1. `QQQIntradayStrategy` 入场逻辑不变
2. `QQQIntradayStrategy` 退出逻辑不变
3. `QQQIntradayStrategy` 止损/止盈触发时刻不变
4. `QQQIntradayStrategy` 不新增重复单、漏单、误平仓
5. `QQQIntradayStrategy` 在 shadow/dual-write 期间可以正常运行

### 7.2 State Kernel Acceptance

1. 所有状态迁移都有唯一入口
2. 所有迁移都有统一审计事件
3. 非法迁移会被明确拒绝并记录
4. 同一事件重复投递不会产生重复副作用
5. 乱序事件不会 silently corrupt state

### 7.3 Flatten Acceptance

1. flatten 状态不再由多个模块各自发明终态
2. flatten completion rule 全系统统一
3. EOD/manual/panic/recovery flatten 共用同一 lifecycle contract
4. flatten stale 会进入 quarantine 或人工介入路径，而不是无限 pending

### 7.4 Runtime Acceptance

1. runtime 状态不再被策略代码直接散改
2. runtime 与 broker truth 不一致时有标准恢复路径
3. runtime quarantine 有明确定义和触发原因
4. runtime flat/live/pending 的语义对 trader 和 reconciler 一致

### 7.5 Operability Acceptance

1. 运维可以看到每个实体当前状态
2. 运维可以看到最后一次迁移原因
3. 运维可以区分：
   - 正常 pending
   - 长时间未收口 pending
   - quarantine
   - auto-healed
4. 重启后系统能从 broker + DB 重建状态，而不是进入未知态

### 7.6 Rollout Acceptance

1. 新状态机能力默认可关闭
2. rollback 可在短时间内完成
3. rollback 后状态归因字段不丢失
4. QQQ 必须是最后一个切主路径的策略

---

## 8. Definition Of Done

只有在以下全部成立时，才能声明状态机升级完成：

1. QQQ replay parity 通过
2. QQQ shadow parity 通过
3. 非理想条件 failure injection 通过
4. flatten / order / runtime 都已接入统一状态内核
5. 不再存在绕过统一内核直接改最终状态的主路径代码
6. 运维、reconciler、策略、执行层对状态语义说同一种语言

如果以上任一条不满足，就只能说“在升级中”，不能说“已完成”。
