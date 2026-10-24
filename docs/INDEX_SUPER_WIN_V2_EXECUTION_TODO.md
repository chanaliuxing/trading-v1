# INDEX_SUPER_WIN_V2_EXECUTION_TODO

> Status: superseded by [INDEX_SUPER_WIN_STOCKS_KERNEL_REWRITE_PLAN.md](./docs/INDEX_SUPER_WIN_STOCKS_KERNEL_REWRITE_PLAN.md).
>
> This TODO list belongs to the older `replica/v2` implementation path. The active implementation plan is now the clean `stocks_kernel` rewrite.

## 1. 目标

本清单用于把 `index_super_win v2` 从设计文档落地为可上线系统。

约束不变：

1. 不影响现有 `qqq_intraday_001`
2. 默认不接入现网执行链
3. 所有新增能力先 `dry_run`
4. 所有共享层必须 `opt-in`


## 2. 分阶段 TODO

## Phase 0: 冻结现网

### P0-1 配置冻结

- [x] 停止 `index_super_win_001`
- [x] 保持 `qqq_intraday_001` 正常运行
- [x] 确认生产配置中没有任何 `index_super_win_v2` 自动加载项
- [ ] 确认新模块未被 registry 默认注册到现网运行链路

### P0-2 基线采样

- [ ] 记录现网 `qqq_intraday_001` 的启动日志基线
- [ ] 记录现网 `qqq_intraday_001` 的持仓同步异常基线
- [ ] 记录现网 `qqq_intraday_001` 的 orphan order 基线
- [ ] 固化一份“现网问题不属于 v2 改动”的对照日志样本


## Phase 1: 基础设施层

### P1-1 Runtime Store

- [x] 新增 `RuntimeLifecycle`
- [x] 新增 `RuntimeState`
- [x] 新增 `InMemoryRuntimeStore`
- [ ] 增加 `runtime_store` schema 版本字段
- [ ] 增加导出/导入接口，便于未来切换 DB/Redis
- [ ] 增加按 `strategy_id + symbol` 的冲突检测与覆盖保护

### P1-2 Ownership Service

- [x] 新增 `OwnershipState`
- [x] 新增 `OwnershipRecord`
- [x] 新增 `InMemoryPositionOwnershipService`
- [ ] 增加 ownership 审计日志字段
- [ ] 增加 claim/release idempotency token
- [ ] 增加 quarantine 升级/降级规则

### P1-3 Order Identity

- [x] 新增 `iswv2` namespace
- [x] 新增 `build_order_ref`
- [x] 新增 `parse_order_ref`
- [x] 新增 `normalize_trigger_id`
- [ ] 增加 parent-child order_ref 规范
- [ ] 增加 suffix 序列号规范
- [ ] 增加跨策略 namespace 校验工具

### P1-4 Broker Snapshot

- [x] 新增 `BrokerPositionSnapshot`
- [x] 新增 `BrokerOpenOrderSnapshot`
- [x] 新增 `BrokerSnapshotService`
- [ ] 增加 account 过滤器
- [ ] 增加 symbol 白名单过滤策略
- [ ] 增加 avg cost 聚合精度测试

### P1-5 Coordinator

- [x] 新增 `TradeIntent`
- [x] 新增 `CoordinationDecision`
- [x] 新增 `RuntimeCoordinator.preview_entry`
- [x] 新增 `RuntimeCoordinator.mark_entry_submitted`
- [x] 新增 `RuntimeCoordinator.mark_flat`
- [x] 新增 `RuntimeCoordinator.mark_live_from_broker`
- [x] 新增 `RuntimeCoordinator.mark_quarantine`
- [ ] 增加 `preview_exit`
- [ ] 增加 `reject_on_unknown_broker_position`


## Phase 2: 策略层骨架

### P2-1 Index Strategy Shell

- [x] 新增 `IndexSuperWinStrategy`（新实现）
- [x] 支持 `dry_run`
- [x] 支持 `symbols` 配置订阅
- [x] `on_features()` 只做 preview，不发真实单
- [ ] 增加 `shadow_submit` 模式
- [ ] 增加 observability 输出
- [ ] 增加运行态快照输出

### P2-2 Instrument Mapping

- [x] 新增 `InstrumentBinding`
- [x] 新增 `InstrumentMap`
- [x] 支持 `signal_symbol -> exec_symbol`
- [x] 支持多股票配置
- [ ] 支持 future/ETF/stock 统一接口

### P2-3 Signal Model

- [x] 新增 `SignalDecision`
- [x] 新增 `SignalModel` 协议
- [x] 新增默认 `FeatureDrivenSignalModel`
- [x] 新增 notional 回推 qty 逻辑
- [x] mapped binding 按 `exec_price` 做 qty sizing
- [x] 新增 side/qty/reason 标准化逻辑


## Phase 3: 协调适配层

### P3-1 Strategy -> Coordinator

- [x] 把策略入口改成 `SignalModel -> TradeIntent`
- [x] 用 `InstrumentMap` 决定执行标的
- [x] 用 `Coordinator` 做 preview，并在 submit approved 后记录 `intent_submitted`
- [x] dry-run 下不调用 `ctx.submit_intent`

### P3-2 Runtime -> Broker Truth

- [ ] 接入 `BrokerSnapshotService.read_position`
- [x] 启动时 broker 非零仓位 -> `quarantine`
- [x] pre-entry 时 broker 非零未知仓位 -> 阻止新 entry
- [ ] 接入 `BrokerSnapshotService.read_open_orders`
- [ ] 定义 `broker_unknown -> quarantine` 路径
- [ ] 定义 broker flat / runtime pending 的回收规则

### P3-3 Ownership Enforcement

- [ ] broker 非零但 ownership 不明时禁止 entry
- [ ] 反向仓位时强制 quarantine
- [ ] 同 symbol 不允许两个 strategy_id 同时 claim
- [ ] 同 symbol 多 trigger 时保持单 owner


## Phase 4: 保护单与恢复

### P4-1 Protection Adapter

- [ ] 新增 protection adapter 接口
- [ ] 定义 `need_protection(symbol, qty, stop_px)` 协议
- [ ] dry-run 模式下只记录不执行
- [ ] live 模式下通过共享层提交保护单

### P4-2 Restart Recovery

- [ ] 启动时加载 runtime store
- [ ] 启动时读取 broker snapshot
- [ ] 启动时执行 ownership 恢复
- [ ] 启动时发现未知仓位进入 quarantine
- [ ] 启动时发现未知 stop order 进入 reconciler 路径


## Phase 5: Shadow / Live 验证

### P5-1 Shadow

- [ ] 单标的 shadow
- [ ] 多标的 shadow
- [ ] 与 `qqq_intraday_001` 并行 shadow
- [ ] 重启恢复 shadow

### P5-2 Live

- [ ] 单标的 live 小规模
- [ ] 多标的 live 小规模
- [ ] 与 `qqq_intraday_001` 并行 live
- [ ] live 断连恢复演练


## 3. 测试方案

## 3.1 单元测试

### 已完成

- `order_identity`
- `runtime_store`
- `ownership_service`
- `broker_snapshot_service`
- `coordinator`
- `index_super_win` shell
- `InstrumentMap`
- `SignalModel`

### 待补

1. `Coordinator.preview_exit`
2. `reject_on_unknown_broker_position`
3. `BrokerSnapshotService.read_open_orders`
4. `ownership idempotency`
5. `namespace collision checker`
6. `submit reject/exception rollback`


## 3.2 集成测试

### A. 纯 dry-run 集成

1. 新策略初始化
2. 多 symbol 订阅
3. 同时接收多个 symbol feature
4. 生成多个独立 preview
5. 不触发真实 `ctx.submit_intent`

### B. 协调层集成

1. 同 symbol 双策略 claim 冲突
2. broker 非零仓位进入 quarantine
3. runtime flat + broker unknown 不误认仓
4. `iswv2` order_ref 不被 `qqqv1` 解析逻辑误认

### C. 恢复链路集成

1. 启动前 runtime 有 pending_entry
2. 启动前 broker 有 live position
3. 启动前 broker 有 orphan order
4. 启动前 DB ledger 终态但 broker 仍有单


## 3.3 Shadow 测试

1. 与现网 `qqq_intraday_001` 同时运行
2. 同 symbol 不发真实单
3. 多 symbol 连续产生 preview
4. 重启后 preview 状态可恢复
5. 不产生新的 orphan order


## 3.4 Live 测试

只在以下条件满足后进行：

1. 单元测试全绿
2. dry-run shadow 稳定
3. 与 `qqq` 并行无串单
4. 无 ownership 误认

live 测试顺序：

1. 单策略单标的
2. 单策略双标的
3. 与 `qqq` 并行不同标的
4. 与 `qqq` 并行同类风险标的


## 4. 验收清单

## 4.1 工程验收

- [ ] 所有 v2 代码位于新模块
- [ ] 现网 `qqq` 未接入任何 v2 新依赖
- [ ] `order_ref` 仅使用 `iswv2:*`
- [ ] `qqqv1:*` 未在 v2 代码中出现
- [ ] v2 模块可以单独编译、导入、单测

## 4.2 行为验收

- [ ] dry-run 下不提交真实 intent
- [ ] submit approved 前不 claim ownership
- [ ] submit approved 前不持久化 `pending_entry`
- [ ] 多 symbol 预览互不覆盖
- [ ] 同 symbol ownership 冲突被正确拦截
- [ ] broker unknown position 被 quarantine
- [ ] runtime flat 不会自动认领 broker 非零仓位

## 4.3 并行验收

- [ ] `qqq_intraday_001` 启动日志与基线一致
- [ ] `qqq_intraday_001` 行为未因 v2 改动改变
- [ ] v2 运行时不产生 `qqqv1` namespace 订单
- [ ] v2 与 `qqq` 并行时无串单、无 ownership 误认

## 4.4 上线前最终验收

- [ ] shadow 期无新增 orphan order
- [ ] shadow 期无反向仓位
- [ ] shadow 期无重复 entry preview
- [ ] 重启恢复后无未知 live position 被直接接管
- [ ] live 灰度阶段无异常 flatten / 反复开仓 / 反向开仓


## 5. 每阶段交付物

### Phase 1 交付物

- `services/strategy_runtime_v2/*`
- `tests/unit/services/strategy_runtime_v2/*`
- `docs/INDEX_SUPER_WIN_V2_RUNTIME_SCHEMA.md`

### Phase 2 交付物

- `strategies/index_super_win_strategy.py`
- `InstrumentMap`
- `SignalModel`
- 对应单测

### Phase 3 交付物

- coordinator 扩展
- broker truth 接口
- quarantine 流程

### Phase 4 交付物

- protection adapter
- restart recovery adapter
- dry-run/recovery 集成测试

### Phase 5 交付物

- shadow report
- 并行验证报告
- 上线门禁清单


## 6. 当前执行建议

立即执行顺序：

1. 完成 `InstrumentMap`
2. 完成 `SignalModel`
3. 将 `IndexSuperWinStrategy` 接入这两层
4. 补对应单元测试
5. 保持 `dry_run`
6. 再进入 broker truth / quarantine 接入
