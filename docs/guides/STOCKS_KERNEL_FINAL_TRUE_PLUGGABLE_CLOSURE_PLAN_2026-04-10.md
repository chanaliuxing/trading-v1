# Stocks Kernel Final True Pluggable Closure Plan

目的：收掉当前 `stocks_kernel` 从“明显模块化”到“真正可插拔”的最后关键缺口。  
范围：只处理本轮 review 暴露的 3 个剩余平台化问题，不混入新的交易功能。  
上线原则：所有整改默认向后兼容、可灰度、可回滚，且不影响当前已运行的旧策略与非 kernel 策略。

## 1. 背景

当前代码已经完成了大部分 contract-first 平台化工作：

1. feature / gate / policy / stage 已拆出独立概念
2. `DataContextBundle` 已进入 runtime contract
3. `policy_registry_specs` 已支持配置驱动 policy 注册
4. shared underlying data plane 已有基础 contract，并加了 namespace / freshness 边界
5. 安全 stage 已具备强约束顺序和不可禁用保护

但从第一性原理看，还没有完全达到“搭积木式装配”的最终要求。  
当前剩余的 3 个硬缺口：

1. runtime stage 扩展仍依赖 `strategy.py` 上的方法绑定
2. gate 类型扩展仍依赖 `default_gate_registry()` 的内建注册
3. policy 基础层仍编译期依赖 `QQQIntradayPolicyV1`

## 2. 最终目标

这轮整改完成后，必须满足下面 6 条：

1. 新增 runtime stage 不改 `strategy.py`
2. 新增 gate 类型不改 `gates/registry.py`
3. 新增 policy 类型不改 `policies/registry.py`
4. 默认未启用新注册式路径时，现有行为保持等价
5. 所有扩展点都支持配置级 rollout / fallback / rollback
6. 任何扩展都不能破坏 safety-critical stage 顺序与 flatten 特权

## 3. 非目标

这份文档明确不做下面这些事：

1. 不改具体交易策略 alpha 逻辑
2. 不改 broker / DB / execution service 总体架构
3. 不要求旧配置一次性迁移
4. 不为了纯架构整洁牺牲现网稳定性

## 4. 设计原则

### 4.1 平台层不认识具体策略实现

平台层可以认识 contract，不应该认识 `QQQ`、`SMH`、`SOXL` 这种具体实现类。

### 4.2 装配层负责把实现接到 contract

内核只定义注册入口、生命周期和安全边界。  
具体实现通过配置或装配清单接入，而不是写死在 kernel core。

### 4.3 扩展点必须分两层

1. contract layer：统一接口、运行时上下文、生命周期
2. assembly layer：动态加载、注册、配置合并、灰度开关

### 4.4 兼容优先于优雅

每个整改项都必须：

1. 先有 adapter
2. 再有新 contract
3. 再做迁移
4. 最后才允许清理 legacy path

### 4.5 现网安全优先于架构完整

只要整改会影响当前已运行实例，就必须优先保证现网安全。

硬要求：

1. 开盘时段不做破坏性切换
2. 不允许一边改 contract 一边直接切 live path
3. 任何新路径先 shadow，再 canary，再逐步放量
4. 旧路径在整个灰度期间必须保持可立即回退

### 4.6 失败必须显式，不允许静默错路

动态注册最危险的问题不是报错，而是“悄悄走错实现”。

硬要求：

1. plugin load 失败必须 fail-fast 或进入显式 fallback
2. fallback 必须写审计
3. unknown stage / unknown gate / unknown policy 不允许 silent ignore
4. 任一 registry source 冲突必须有明确优先级和审计字段

## 5. 动内核禁止项

这轮整改明确禁止下面这些实现方式：

1. 不允许直接删除 legacy path 后再补测试
2. 不允许在 `strategy.py` 增加新的策略特判来“临时过渡”
3. 不允许把 load failure 吞掉后退回默认实现而不记审计
4. 不允许通过修改线上 DB 数据完成回滚
5. 不允许要求其他旧策略同步改配置才能继续运行
6. 不允许在 market open 期间做首次 live cutover
7. 不允许让动态 stage 拿到绕过 `safety_invariants` / `session_flatten` 的执行特权

## 6. 实施顺序

必须按下面顺序推进，不能跳步：

1. 先补 registry contract 和 adapter
2. 再补 unit tests
3. 再补 integration tests
4. 再做 shadow path
5. 再做单策略 canary
6. 最后才允许扩大 enablement

任何一步失败，立刻停在当前阶段，不进入下一步。

## 7. TODO List

### 7.1 P0: Runtime Stage Registry 真正开放化

目标：新增 stage handler 不改 `strategy.py`。

现状问题：

1. stage plan 已可配置
2. 但 handler 仍来自 `self._runtime_stage_handlers`
3. handler 解析仍依赖 strategy 实例方法名

TODO：

1. 引入正式 `RuntimeStageRegistry`
2. stage registry 注册项至少包含：
   - `stage_id`
   - `handler`
   - `order`
   - `enabled_default`
   - `metadata`
   - `safety_class`
3. `handler` 支持注册独立 callable，而不是只支持 strategy method name
4. strategy 只负责：
   - 构造 runtime context
   - 调用 stage registry / orchestrator
   - 执行 reserved safety boundary check
5. 把当前 builtin stage 迁移为 registry 内建 spec，而不是 strategy 私有 handler map
6. 支持配置驱动 stage 注册，例如：
   - module
   - attr
   - stage_id
   - order
   - enabled_default
7. 保留并强化 reserved stage 规则：
   - `runtime_recovery`
   - `broker_truth_sync`
   - `safety_invariants`
   - `session_flatten`
   - `entry_submission`
8. 明确禁止：
   - 任意自定义 stage 插到 `runtime_recovery` 之前
   - entry 类 stage 排到 `session_flatten` 之前
   - 绕过 `safety_invariants`
9. rollout 要求：
   - `enable_runtime_stage_registry_v2`
   - legacy fixed handler fallback
   - per-strategy enablement
   - shadow order validation
10. production safety 要求：
   - 先 shadow，不接真实交易决策
   - canary 仅限单实例 / 单策略
   - 首次 live enable 必须在非开盘时段完成
   - 任一异常可通过 flag 单独回退

完成标准：

1. 新 stage 通过 registry + config 即可运行
2. `strategy.py` 不再维护内建 handler 字典
3. stage 插件可执行但不能突破 reserved safety boundary
4. 未启用 v2 的实例行为与当前一致

### 7.2 P0: Gate Registry 动态装配化

目标：新增 gate 类型不改 `gates/registry.py`。

现状问题：

1. gate pipeline 已走 registry contract
2. 但 registry 本身仍只有 `default_gate_registry()` 的内建注册
3. 新 gate 类型仍需改核心注册代码

TODO：

1. 给 `GateRegistry` 增加 `register_from_specs`
2. 支持配置驱动 gate 装配：
   - `gate_id`
   - `module`
   - `attr`
   - `default_config`
3. 支持 factory function 和 class constructor 两种形式
4. 统一 disabled gate / runtime gate / legacy gate 的 contract
5. 把 `multi_scope_readiness` 保留为默认 gate 只是“默认装配”，而不是“平台硬编码依赖”
6. `strategy.initialize()` 中改为：
   - 先加载默认 gate pack
   - 再加载 `gate_registry_specs`
   - 最后按 `gate_pipeline` 实际装配
7. rollout 要求：
   - `enable_dynamic_gate_registry`
   - legacy default registry fallback
   - per-strategy enablement
   - unknown gate fail-fast + audit
8. production safety 要求：
   - 默认 registry pack 保持不变
   - dynamic spec 只允许在显式 flag 下生效
   - unknown gate 不能自动跳过继续交易
   - config load 失败时必须回退到旧 registry 或阻断启动

完成标准：

1. 新 gate 只通过配置注册即可接入
2. `gates/registry.py` 不需要为每个新 gate 改代码
3. 已有 gate 行为不退化
4. `multi_scope_readiness` 只是一个普通 gate 实现

### 7.3 P0: Policy Registry 与具体策略实现解耦

目标：平台层不再编译期依赖 QQQ policy。

现状问题：

1. `policy_registry_specs` 已支持动态注册
2. 但 registry 顶层仍直接 import `QQQIntradayPolicyV1`
3. 默认 registry 仍内建 QQQ factory

TODO：

1. 把 `PolicyRegistry` 拆成：
   - pure registry contract
   - default policy pack / builtin assembly
2. `policies/registry.py` 不再 import 任何具体策略实现
3. 把 QQQ policy 默认注册迁移到独立装配模块，例如：
   - `policies/default_pack.py`
   - 或 `policies/assemblies/qqq.py`
4. strategy 初始化改为：
   - 构造空 registry 或基础 registry
   - 加载 builtin policy pack
   - 加载 `policy_registry_specs`
   - build selected policy
5. rollout 要求：
   - `enable_policy_registry_pure_core`
   - legacy default registry fallback
   - per-strategy enablement
   - build parity audit
6. production safety 要求：
   - 默认 QQQ 装配先保持旧行为等价
   - build failure 不能静默替换成错误 policy
   - policy source / builder source 必须记录审计
   - 首次切换先做同输入 build parity compare

完成标准：

1. `policies/registry.py` 不 import 具体策略实现
2. 新 policy 通过配置或装配模块即可接入
3. 默认 QQQ policy 仍可零行为漂移运行
4. 未启用新路径时老实例行为等价

### 7.4 P0: Rollout / Fallback / Rollback 清单

目标：整改可上线，不冲击现网。

TODO：

1. 每个核心整改项单独 flag 化：
   - `enable_runtime_stage_registry_v2`
   - `enable_dynamic_gate_registry`
   - `enable_policy_registry_pure_core`
2. 每项都必须支持 legacy fallback
3. 每项都必须支持 per-strategy enablement
4. 每项都必须有审计字段：
   - registry source
   - selected implementation
   - fallback reason
   - unknown plugin / load failure reason
5. 未启用新路径的实例必须 byte-equivalent or behavior-equivalent
6. rollback 必须只是配置切换，不允许人工改 DB
7. 明确切换窗口：
   - shadow：任意时间可开
   - canary：仅限非开盘时段开启
   - 扩量：至少经过一个完整交易日 shadow/观察后再做
8. 明确 freeze 条件：
   - 出现 `runtime_stage_unknown`
   - 出现 unknown gate/policy load failure
   - 出现行为漂移但无审计解释
   - 出现策略启动失败或 fallback 风暴

完成标准：

1. 任一整改项都可独立关闭
2. 任一整改项关闭后旧路径立即恢复
3. 旧策略、老策略、非 kernel 策略不受影响

## 8. 测试方案

### 8.1 Unit Tests

#### Runtime Stage Registry

1. 注册独立 stage callable，不依赖 strategy method
2. 通过 config spec 动态加载测试 stage 并执行
3. reserved safety stage 缺失时自动补齐
4. reserved safety stage 不能被禁用
5. 非法排序会被 clamp 或 fail-fast
6. legacy fallback 路径仍执行原有 builtin stage

#### Gate Registry

1. 通过 `gate_registry_specs` 动态注册测试 gate
2. class-based gate 和 factory-based gate 都能加载
3. unknown gate fail-fast 并产生日志/审计
4. legacy gate 在未启用动态 registry 时行为不变
5. `multi_scope_readiness` 作为普通 gate 被 registry 装配成功

#### Policy Registry

1. pure core registry 不 import 具体策略实现
2. builtin policy pack 能注册 QQQ policy
3. `policy_registry_specs` 能注册 dummy policy
4. 新 policy 构造不需要改 `strategy.py`
5. legacy fallback 构造路径保持兼容

### 8.2 Integration Tests

1. 一个测试策略只通过 config 注入：
   - custom stage
   - custom gate
   - custom policy
   并完成一次完整 runtime cycle
2. stage/gate/policy 三个扩展点同时启用时，runtime cycle 不报错
3. reserved safety stage 仍按固定边界执行
4. `session_flatten` 仍高于 entry 类 stage
5. fallback flags 关闭后，运行路径回到旧实现
6. dynamic load failure 会被显式审计并进入预期 fallback / block path
7. 同时存在 default pack 与 dynamic pack 时，优先级行为符合设计且可审计

### 8.3 Regression Tests

1. 现有 `stocks_kernel` unit 全量通过
2. 现有 `stocks_kernel` integration 全量通过
3. `options_kernel` 相关 shared data 既有测试继续通过
4. 旧策略配置在未启用新 flag 时结果不变
5. 非 kernel 老策略 smoke test 通过
6. 启用新 flag 的测试实例，不影响未启用实例的 runtime output

### 8.4 Shadow / Canary 验证

1. shadow 阶段比较：
   - selected stage order
   - gate decisions
   - winning actions
   - policy build result identity
2. canary 阶段比较：
   - 启动成功率
   - fallback 触发率
   - slow execution 警告是否异常升高
   - 审计字段是否完整
3. 任一差异超阈值即停止扩量

## 9. 回归测试执行清单

建议按下面顺序执行：

1. `venv/bin/pytest -q tests/unit/strategies/test_stocks_kernel_*.py`
2. `venv/bin/pytest -q tests/integration/strategies/test_stocks_kernel_runtime_cycles.py`
3. `venv/bin/pytest -q tests/unit/strategies/options_kernel/test_base.py tests/unit/strategies/test_shared_underlying_data_plane.py tests/unit/strategies/test_stocks_kernel_shared_underlying_plane.py`
4. 运行新增专项测试：
   - dynamic stage registry
   - dynamic gate registry
   - pure-core policy registry
5. 对未启用新 flag 的实例做 smoke parity：
   - same config
   - same input
   - same winning actions / entry decisions / gate decisions
6. 做 shadow parity：
   - old path vs new path stage order
   - old path vs new path selected gate set
   - old path vs new path selected policy identity
7. 做 canary checklist：
   - 启动正常
   - fallback 受控
   - 无异常 slow warning 激增
   - 无其他策略告警外溢

## 10. 验收标准

### 10.1 功能验收

必须全部满足：

1. 新增 stage 不改 `strategy.py`
2. 新增 gate 不改 `gates/registry.py`
3. 新增 policy 不改 `policies/registry.py`
4. stage/gate/policy 都能通过 config spec 完成装配
5. reserved safety boundary 仍然有效

### 10.2 稳定性验收

必须全部满足：

1. 未启用新 flag 的实例行为不变
2. 老策略和非 kernel 策略不受影响
3. fallback 路径可用
4. load failure 会 fail-fast 或显式审计，不允许 silent wrong path
5. 开盘期间不需要做首次 live cutover
6. 任一异常都能通过配置回退

### 10.3 可运维验收

必须全部满足：

1. 审计中能看出 stage/gate/policy 来自哪个 registry source
2. 审计中能看出是否走了 fallback
3. 任一新路径都能通过配置快速回滚
4. 能从审计直接定位 load failure / registry conflict / fallback reason

### 10.4 开盘准入验收

必须全部满足：

1. shadow 已跑过至少一个完整交易日或等价 replay 周期
2. canary 已通过
3. 未观察到其他策略行为异常
4. rollback 预案已验证
5. 本次变更不要求同步重启其他无关策略

## 11. Go / No-Go

### 11.1 Go

允许进入灰度的条件：

1. Unit / integration / regression 全绿
2. 新路径仅在显式 flag 下启用
3. fallback 已验证
4. 审计字段完整
5. shadow / canary 已通过

### 11.2 No-Go

任一项出现即禁止上线：

1. 新增扩展点仍需改 kernel core
2. reserved safety stage 可被绕过
3. 未启用新路径时老策略行为漂移
4. load failure 被静默吞掉
5. rollback 不能只靠配置完成
6. 首次切换需要在开盘时直接 live 验证
7. 影响到其他未启用实例

## 12. Commit / Rollback 要求

1. 每个整改项单独 commit，禁止把 stage/gate/policy 三项混成一个大提交
2. 每个 commit 必须附带对应测试通过记录
3. 任何 live enable 改动必须和纯重构改动分开提交
4. rollback 以 revert commit 或配置关闭为主，不允许现场手工 patch

## 13. 完成定义

这份计划完成时，`stocks_kernel` 才能说“真正接近完全可插拔”：

1. kernel core 只维护 contract 和 safety boundary
2. 扩展实现进入 registry / assembly layer
3. 交易员通过模块 + 配置组合策略，而不是继续改 `strategy.py`
