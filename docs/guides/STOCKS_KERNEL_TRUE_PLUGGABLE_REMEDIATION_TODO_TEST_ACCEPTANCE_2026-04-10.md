# Stocks Kernel True Pluggable Remediation TODO / Test Plan / Acceptance

目的：把当前 `stocks_kernel` 从“重构后的模块化单体”纠偏为“真正可插拔、可注册、可按需装配”的平台内核。  
范围：只处理本次 review 暴露出的平台化偏差，不混入新的交易功能扩展。  
原则：先消除 kernel 对具体策略能力的硬编码认知，再继续扩功能。

## 1. 问题定义

当前实现已经有：

1. feature registry
2. gate registry
3. policy registry
4. runtime stages
5. `DataContextBundle`
6. action / authority / ownership contracts

但这些能力还没有真正形成“只靠注册和配置即可扩展”的闭环。  
当前更接近“拆分后的大策略”，而不是“平台内核”。

本次 review 确认了 5 个关键偏差：

1. action envelope `decision_id` 在多动作同 tick 下仍可能碰撞。
2. multi-scope gate 依赖 `strategy.py` 私有注入 runtime context，不是真正 gate contract。
3. policy 构造仍有 `qqq_intraday_policy_v1` 特判，kernel 仍知道具体策略。
4. `DataContextBundle` 无条件构建，带来不必要的延迟成本。
5. runtime stage 仍是固定 handler map，扩 stage 仍要改 kernel core。
6. `stocks` 与 `options` 之间还没有正式的数据平面共享 contract，同一标的行情/特征仍可能重复拉取、重复计算、重复缓存。

## 2. 整改目标

整改完成后，必须满足下面 5 条：

1. 新增 policy 不改 `strategy.py`
2. 新增多域 gate 不改 `strategy.py`
3. 新增 runtime stage 不改 `strategy.py`
4. 未启用多域能力时，不构建多域运行时上下文
5. 同 tick 多动作 envelope / decision / audit / idempotency 唯一且稳定

并新增 1 条跨策略架构目标：

6. `stocks` 与 `options` 在同一 underlying symbol 下共享正式 market data / feature data contract，避免重复生产同一份基础数据

## 3. TODO List

### 3.1 P0: Action Envelope Idempotency Contract

目标：消除 action 路径的 decision_id 碰撞。

TODO:

1. 重构 `StrategySubmitBridge.build_action_envelope`
2. action decision_id 纳入：
   - `trigger_id`
   - `action_type`
   - `lane_id`
   - `qty`
   - 必要时的 `position_lot_id`
3. entry / action / exit 三类 envelope 的 decision key 规则统一成同一 contract
4. 为多动作同 tick 增加唯一性测试和稳定性测试

完成标准：

1. 同 symbol / 同 tick / 同 action_type / 不同 lane 的 envelope 不碰撞
2. 同 symbol / 同 tick / 同 action_type / 不同 qty 的 envelope 不碰撞
3. 相同输入重复构造 decision_id 保持稳定

### 3.2 P0: Gate Runtime Contract 平台化

目标：让 multi-scope gate 变成真正可插拔 gate，而不是 strategy 私货。

TODO:

1. 扩展 `GatePipeline.evaluate` 正式输入 contract
2. 把 `data_context_bundle` 作为 gate runtime 的正式字段，而不是写入 `feature_context.values["__data_context_bundle__"]`
3. 通过兼容适配层升级 gate contract，已有单域 gate 不允许因签名升级而被迫同步重写
4. `multi_scope_readiness` 改为纯 gate 消费正式 runtime context
5. strategy 中删除 multi-scope gate 的特判注入逻辑
6. 为未来多域 gate 保留标准扩展接口
7. 上线方式必须支持：
   - feature flag
   - shadow path
   - fallback to legacy gate contract
   - per-strategy gradual enablement

完成标准：

1. 新增一个多域 gate 时，不需要修改 `strategy.py`
2. `multi_scope_readiness` 仅依赖 gate contract，不依赖魔法 key
3. gate registry + config 即可接入新的 runtime gate
4. 已有 gate 在未启用新 contract 时行为保持不变
5. 单域 gate 可以通过兼容适配层零改动继续运行

### 3.3 P0: Policy Factory / Registry 去特判

目标：让 kernel 不再知道 `qqq_intraday_policy_v1`。

TODO:

1. 给 policy registry 增加 factory / builder 能力
2. policy 从 “注册实例” 改为 “注册可构造对象”
3. 把 policy 参数过滤和构造逻辑迁移到 policy 自己或 factory 层
4. 删除 `strategy.initialize()` 中的 QQQ 特判分支
5. 保持现有 `qqq_intraday_policy_v1` 行为不变
6. 增加一个测试专用 dummy policy，验证无需改 kernel 即可注册运行
7. 上线方式必须支持：
   - feature flag
   - shadow construction path
   - fallback to legacy policy construction path
   - per-strategy enablement

完成标准：

1. `strategy.py` 中不出现具体 policy id 特判
2. 新 policy 仅通过 registry/factory 注册即可被加载
3. 现有 QQQ policy 回归不退化
4. 未启用新 factory 的策略实例，构造路径与当前行为等价

### 3.4 P1: DataContextBundle 按需构建

目标：只在确实需要时才付出多域构建成本。

TODO:

1. 明确多域消费声明来源：
   - scoped feature requirements
   - multi-scope gates
   - 未来需要多域的 policy/stage/module
2. 引入 `requires_data_context_bundle` 判定
3. 未声明依赖时跳过 `DataContextBundle` 构建
4. scope history/snapshot 构建做并发化，避免串行 await
5. 增加 slow-path 审计字段，记录 bundle build cost

完成标准：

1. 无多域需求的 tick 不构建 bundle
2. 有多域需求的 tick 仍可正确拿到 execution/confirm/hedge contexts
3. 非必要 tick 的执行耗时下降

### 3.5 P1: Stage Registry 化

目标：stage 扩展不再依赖固定 handler map。

TODO:

1. 引入 runtime stage registry / stage spec
2. stage 注册包含：
   - `stage_id`
   - handler
   - 默认顺序
   - enabled default
   - metadata
3. orchestrator 从固定 `DEFAULT_STAGES` 转为 registry build plan
4. strategy 不再维护硬编码 `handlers = {...}`
5. 定义保留安全 stage 与不可越过的顺序约束：
   - `runtime_recovery`
   - `broker_truth_sync`
   - `safety_invariants`
   - `session_flatten`
   - `entry_submission`
6. 明确禁止：
   - 把 entry 类 stage 排到 `session_flatten` 前面绕过 flatten 特权
   - 把自定义 stage 插到 `runtime_recovery` 之前
   - 让自定义 stage 绕过 `safety_invariants`
7. 增加 stage 插拔测试：新增测试 stage 不改 kernel 主文件即可执行
8. 上线方式必须支持：
   - feature flag
   - legacy fixed-stage fallback
   - shadow execution ordering validation
   - per-strategy gradual enablement

完成标准：

1. 新增 stage 只需注册
2. stage 可以配置排序/启停
3. 不再出现 `runtime_stage_unknown` 这种因 core 未更新造成的伪扩展失败
4. 任意扩展 stage 都不能破坏保留安全 stage 的顺序边界

### 3.6 P0: Rollout / Fallback / Rollback Gate

目标：防止平台化整改以“大切换”方式冲击现网。

TODO:

1. 对每个核心 contract 重写项定义独立 flag：
   - policy factory
   - gate runtime contract
   - stage registry
   - bundle on-demand
   - action envelope v2
2. 所有新路径必须支持 legacy fallback
3. 所有新路径必须支持 per-strategy enablement
4. 对高风险路径增加 shadow mode：
   - 运行新路径
   - 不影响真实交易决策
   - 仅比较输出与审计
5. 明确 rollback 方式必须是配置切换，不允许 DB surgery
6. 发布前输出 go/no-go 检查项与回滚点

完成标准：

1. 任一核心整改项都可以单独打开/关闭
2. 任一核心整改项都可以在不改代码的情况下快速回退
3. 未启用新路径的实例不受新整改逻辑影响

### 3.7 P0: Cross-Asset Shared Data Plane

目标：让 `stocks` 与 `options` 在同一 underlying symbol 下共享可插拔的数据与特征平面，而不是各自重复实现。

TODO:

1. 定义统一 `underlying market data contract`，至少包含：
   - underlying snapshot
   - underlying history
   - underlying readiness
   - underlying feature context
2. 区分两类数据：
   - underlying-shared data
   - instrument-specific data
3. 明确共享边界：
   - 股票策略直接消费 underlying-shared data
   - 期权策略既消费 underlying-shared data，也消费 option-specific quote/greeks/chain data
4. 引入共享数据 provider / registry，避免 `stocks` 和 `options` 分别对同一股票代码重复拉取和重复算基础特征
5. 对共享特征定义标准命名与缓存键，避免同 underlying 重复计算：
   - price/vwap/atr/v3m 等基础特征
   - session/regime/readiness 等基础上下文
6. 明确禁止：
   - 同一 underlying 在 `stocks` 和 `options` 两套架构里各自独立维护一份基础行情 contract
   - 同一 underlying 基础特征各算各的且结果不可对齐
7. 保持 instrument-specific 隔离：
   - option greeks / NBBO / chain / expiry-specific features 不强行下沉到股票 kernel
   - stock execution lanes / position lifecycle 不强行上浮到 option kernel
8. 上线方式必须支持：
   - shared data plane feature flag
   - legacy per-strategy data path fallback
   - parity/shadow compare
   - per-strategy gradual enablement

完成标准：

1. 同一 underlying symbol 的基础行情只需要一套正式 contract
2. `stocks` 和 `options` 可消费同一份 underlying snapshot/history/readiness/features
3. option-specific 数据与股票专属执行语义仍保持隔离
4. 未启用 shared data plane 的旧路径保持可运行

## 4. 测试计划

### 4.1 Unit Tests

#### Action Envelope

1. 同 tick 同 action_type 不同 lane 生成不同 `decision_id`
2. 同 tick 同 action_type 不同 qty 生成不同 `decision_id`
3. 同 tick 同 action_type 不同 trigger 生成不同 `decision_id`
4. 相同 action 输入生成稳定 `decision_id`

#### Gate Runtime Contract

1. `GatePipeline` 可以向 gate 传递正式 `data_context_bundle`
2. `multi_scope_readiness` 在正式 contract 下通过/阻断均正确
3. 无 runtime context 且配置允许缺失时 fail-soft
4. 新增一个测试 gate，只通过 registry/config 即可运行
5. 旧 gate 通过兼容适配层零改动继续运行
6. legacy gate contract 与 new gate contract 在未启用新路径时结果一致

#### Policy Factory

1. registry 可根据 `policy_id + policy_params + extras` 构造 policy
2. `qqq_intraday_policy_v1` 在新 factory 路径下构造成功
3. dummy policy 不修改 kernel 即可注册并完成 initialize
4. legacy policy construction path 在 flag 关闭时保持等价

#### Bundle On-Demand

1. 无 scoped feature / multi-scope gate 时，不构建 bundle
2. 启用 scoped feature 时，构建 bundle
3. 启用 multi-scope gate 时，构建 bundle
4. bundle builder 在多 symbol scope 下并发加载成功

#### Cross-Asset Shared Data Plane

1. 同一 underlying symbol 下，`stocks` 与 `options` 读取到相同的 underlying snapshot contract
2. 同一 underlying symbol 下，基础特征只计算一次即可被两侧消费
3. option-specific data 缺失时，不影响股票策略基础数据消费
4. 股票执行字段不存在时，不影响 option-specific runtime
5. shared data plane flag 关闭时，两侧仍可走 legacy 数据路径

#### Stage Registry

1. registry 中注册的新 stage 能被 orchestrator 发现
2. stage 可通过配置禁用
3. stage 执行顺序符合注册/override 规则
4. 删除 core handler map 后，新 stage 仍可运行
5. 自定义 stage 无法排到 `runtime_recovery` 之前
6. 自定义 stage 无法把 entry 放到 `session_flatten` 之前绕过安全顺序
7. legacy fixed-stage path 在 flag 关闭时保持等价

#### Rollout / Fallback

1. 每个核心整改项的 feature flag 可单独开关
2. flag 关闭时行为与 legacy 路径一致
3. shadow mode 可产出比对结果且不影响真实 submit
4. rollback 仅通过配置切换即可生效

### 4.2 Integration Tests

1. 单策略单票，在无多域需求配置下正常运行且不构建 bundle
2. 单策略多票，在有 execution/confirm/hedge 需求时正确构建 bundle
3. action set 同 tick 多动作下，audit / decision / idempotency 全部唯一
4. 注册一个 dummy policy + dummy gate + dummy stage，在不改 `strategy.py` 的情况下完整跑通 runtime
5. 老的 `qqq_intraday_policy_v1` 路径保持可运行，不影响现有老策略
6. flag 关闭时，legacy `stocks_kernel` 路径行为与整改前等价
7. 未启用新路径的非-kernel / 旧策略运行不受影响
8. 同一 underlying 下，股票策略与期权策略可共享基础行情与特征，但不发生执行/持仓语义串扰

### 4.3 Regression Tests

1. 现有 `stocks_kernel` unit 全量回归
2. 现有 integration 回归
3. 重点回归：
   - action set
   - authority
   - protection lifecycle
   - reconcile/adopt
   - multi-strategy same-symbol isolation
4. 运行一次 certification / matrix，确认 stage 覆盖和 readiness 不回退
5. 回归现有非-kernel 策略冒烟用例，确认本次整改未外溢影响系统其他策略
6. 回归 option 策略基础路径，确认 shared data plane 引入后未破坏 option 专属数据语义

### 4.4 Runtime / Performance Checks

1. 记录 bundle build 耗时分布
2. 对比整改前后 `strategy execution slow` 告警频率
3. 核对非交易时间是否减少重路径执行

## 5. 验收标准清单

### 5.1 平台化验收

1. `strategy.py` 中不存在具体 `policy_id` 特判构造
2. `strategy.py` 中不存在对 `multi_scope_readiness` 的私有注入逻辑
3. 新 gate / 新 policy / 新 stage 均可只靠注册与配置接入
4. kernel core 不需要知道具体 QQQ 策略细节
5. stage 扩展不能突破保留安全顺序边界
6. `stocks` 与 `options` 的共享能力建立在正式 shared data contract 上，而不是相互读取对方私有 runtime 状态

### 5.2 正确性验收

1. 多动作同 tick 不出现 decision_id 碰撞
2. action / audit / submit / counting 语义一致
3. 多域 gate 阻断行为与配置一致
4. 不启用多域能力时行为与旧逻辑等价
5. legacy gate / policy / stage 路径在 flag 关闭时与整改前等价
6. 同一 underlying 的 shared 基础数据在 `stocks` 与 `options` 两侧语义一致

### 5.3 性能验收

1. 无多域需求配置下，不发生 bundle 构建
2. 有多域需求配置下，bundle 构建延迟可解释且受控
3. `strategy execution slow` 不因本次整改恶化
4. shared data plane 引入后，不因重复拉取/重复算特征导致额外慢执行

### 5.4 兼容性验收

1. 老策略不需要立即迁移配置
2. 默认值不破坏已有 QQQ 路径
3. feature flag / fallback 可用
4. 回滚不需要 DB surgery
5. 未启用新路径的 `stocks_kernel` 实例行为不变
6. 非-kernel 和 legacy 策略运行不受本次整改影响
7. 未触及的策略配置加载语义保持等价
8. option 策略在未启用 shared data plane 时行为保持不变

## 6. 不通过条件

出现以下任一项，视为本次整改未完成：

1. 新增 policy 仍需改 `strategy.py`
2. 新增 multi-domain gate 仍需改 `strategy.py`
3. 新增 stage 仍需改固定 handler map
4. action decision_id 仍可在同 tick 冲突
5. 无多域需求时仍默认构建 `DataContextBundle`
6. 老策略运行被本次整改破坏
7. 新路径上线不支持 flag / fallback / rollback
8. stage 扩展可绕过保留安全顺序
9. 旧 gate 因 contract 升级被迫同步重写
10. `stocks` 与 `options` 共享数据通过私有 runtime 互相耦合，而不是正式 contract
11. shared data plane 引入后破坏 option-specific 或 stock-specific 数据语义

## 7. 实施顺序

建议按下面顺序推进，避免边改边继续固化错误架构：

1. `P0` action envelope id contract
2. `P0` rollout / fallback / rollback gate
3. `P0` policy factory / registry 去特判
4. `P0` gate runtime contract 平台化
5. `P1` bundle 按需构建
6. `P0` cross-asset shared data plane
7. `P1` stage registry 化
8. 全量回归、matrix、性能对比、准入结论

## 8. 最终定义

本次整改只有在下面这句话成立时，才算真正完成：

“交易员新增一个股票策略，只需要声明 template / policy / gates / features / stages / lanes / contracts，而不需要修改 `stocks_kernel` 主骨架代码。”

并且：

“当股票策略和期权策略针对同一 underlying symbol 运行时，它们共享同一份正式基础行情与特征 contract，而不是各自复制一套基础数据链路。”
