# Pluggable Architecture Hardening — Status Tracker

**Owner plan**: `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md`
**Update cadence**: 每周五收盘后
**Last updated**: 2026-04-20

**关联文档**:

1. 四柱硬化主计划:
   `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md`
2. QQQ 迁移主计划:
   `docs/implementation/QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md`
3. QQQ 迁移状态:
   `docs/implementation/QQQ_KERNEL_MIGRATION_STATUS.md`

**使用规则**:

1. 本文件只更新状态，不重复写实现方案。
2. 实现细节改动写回四柱硬化主计划。
3. 阶段顺序和 cutover 判定写回 QQQ 迁移主计划。

---

## 当前周

**Week 0 — 四柱 P0 落地 + 柱 D 出口校验补强**

---

## 四柱状态

### 柱 A — Kernel Unified Contracts
- [x] 列出 `options_kernel/types.py` 全部类型及字段
- [x] 设计 `libs/strategy/kernel_contracts/{types,plugin,audit}.py`
- [x] options_kernel 改为从 kernel_contracts 重导出
- [x] `BaseStockPlugin` 原型
- [x] `BaseStockPlugin` 生命周期 facade(`on_init/on_features/on_intent/on_fill/on_shutdown`)
- [x] `ComposableStocksKernelStrategy` 运行时加载 `plugin_cls` 并调用 lifecycle hook
- [x] example plugin `strategies/stocks_kernel/plugins/example_breakout.py`
- [x] Remove Alternatives 时间线写入并绑定删除 PR owner/date

### 柱 B — Shared Data Plane Mandatory
- [x] monolith publish shared plane
- [x] monolith consume shared plane(`plane_only` 默认启用)
- [x] `config.yml` 默认 `enable_shared_underlying_data_plane: true`
- [x] `QQQ` fail-closed: `plane_only` 禁止 shared plane 关闭时启动
- [x] kernel 消费侧跨策略一致性 test `test_shared_plane_consistency.py`
- [x] shared plane readback diff metric `shared_plane_readback_diff_total`
- [x] Prometheus `shared_plane_publish_latency_ms` 指标
- [x] monolith plane read path 首版落地
- [x] `SFS1sReader` 策略层 lint 禁令设计稿

### 柱 C — System-Level Flip Guard + Ghost Resolver Closure
- C.1 Flip Guard
  - [x] `services/exec/flip_guard.py` 原型
  - [x] `db/pg/migration_20260421_flip_guard.ddl`
  - [ ] SafetyKernel 注入调用点
  - [x] ExecutionService 最底层 submit 再次检查
  - [x] root_symbol 映射表(SOXL↔SOXS 等)
  - [x] contract metadata 自动推导 `root_symbol`
  - [x] same-bar reversal block 规则
  - [x] exemption `expires_at` + 告警
  - [ ] shadow 验证(fault-injection)
  - [x] 跨重启恢复测试
- C.2 Ghost Closure
  - [x] DDL 调用单点化(启动时一次)
  - [ ] Quarantine 跨 option↔underlying 传染字段 `root_symbol`
  - [x] startup broker truth pull -> DB diff -> quarantine + ACK gate
  - [ ] quarantine 双向传染矩阵文档与测试

### 柱 D — Runtime Identity Propagation
- [ ] `StrategyContext.current_ib_session()`
- [ ] `IBHandle.call(epoch)` 全系统包裹
- [x] `DecisionEnvelope.origin_ib_epoch` 新字段
- [x] `origin_ib_epoch` 改成 handle 注入，不允许策略自由填
- [x] ExecutionService ingress epoch mismatch reject
- [x] adapter egress epoch mismatch reject(place/modify/cancel)
- [ ] fill 回流校验 client_id/permId
- [ ] master truth session 反查 permId/client_id
- [x] Prometheus alert 规则: epoch mismatch / schema invalid / same-bar reversal

---

## Hard Gate 状态

| Gate | 柱 | 状态 | 备注 |
|---|---|---|---|
| Kernel contracts 抽取后 options 单测通过 | A | ✅ | `options_kernel/types.py` 已重导出 |
| stocks_kernel 可加载 plugin 实例 | A | ✅ | builder/plugin blueprint + runtime plugin hook 已落地; 多 plugin fill 隔离测试已补 |
| 10 日 `strategy_entrypoint_bypass_total = 0` | A | ⏳ | |
| monolith publish 延迟 p99 < 5ms | B | ⏳ | `shared_plane_publish_latency_ms` 已补,等待生产观测 |
| shared plane 字节级一致 | B | ✅ | QQQ producer -> stocks kernel consumer test 已落地 |
| 10 日 `shared_plane_readback_diff_total = 0` | B | ⏳ | 指标和告警已落地,等待生产观测 |
| 10 日 `shared_plane_miss_fallback_total = 0` | B | ⏳ | |
| FlipGuard shadow BLOCK 验证 | C.1 | ⏳ | 还缺 fault injection |
| FlipGuard 跨重启持久化 | C.1 | ✅ | PG store + JSON fallback 已落地 |
| 5 日 0 次 flat 后立即反向 reopen 漏拦截 | C.1 | ⏳ | |
| 10 日 `flat_to_reverse_reopen_total = 0` | C.1 | ⏳ | |
| 10 日 `flip_guard_exemption_used_total = 0` | C.1 | ⏳ | 指标和告警已落地,等待生产观测 |
| DDL 单点化 | C.2 | ✅ | runtime 不再写 DDL |
| Quarantine 跨 root 传染 | C.2 | ⏳ | |
| 10 日 `ghost_order_detected_total = 0` | C.2 | ⏳ | |
| 10 日 `startup_blocked_by_unknown_broker_order_total = 0` | C.2 | ⏳ | |
| IB epoch mismatch reject | D | ✅ | ingress + adapter egress |
| `IBHandle.call(epoch)` 覆盖率 100% | D | ⏳ | 仍缺 handle 级统一包裹 |
| 正常日 10 日 `ib_epoch_mismatch_rejects_total = 0` | D | ⏳ | |
| 演练日 `ib_epoch_mismatch_rejects_total >= 1` | D | ⏳ | |

---

## 周会记录

### 2026-04-20(Week 0 启动)
- 签署主计划 `PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md`
- 关键事实实证:
  - `grep flip_cooldown\|reverse_open services/exec` = 0 命中 → 反向开单无系统级防线
  - `grep enable_shared_underlying_data_plane config.yml` = 0 命中 → 共享平面未启用
  - `KernelIntentRecord` 只在 `options_kernel/types.py`,stocks_kernel 无 → 契约分叉
- 新增终态约束:
  - Remove Alternatives 时间线必须落地
  - 每柱必须有“连续 N 日生产零事件”Hard Gate
  - startup ghost reconcile gate 必须进柱 C.2
- Week 1 目标: 柱 C.2 DDL 单点化 PR + 柱 A options_kernel types 清单 + 柱 B monolith publish 点确认

### 2026-04-20(Week 0 更新)
- 已落地:
  - `ExecutionService -> SafetyKernel -> IB adapter` 透传 `expected_epoch`
  - `ib_adapter` 出口 epoch reject(place/modify/cancel)
  - `infra/prometheus_alerts.yml` 新增 same-bar reversal / ingress epoch mismatch / adapter epoch mismatch / schema invalid 告警
- 新增待办:
  - `test_shared_plane_consistency.py` 作为 shared plane 字节级一致 CI gate
  - FlipGuard exemption 增加 `expires_at` + 告警摩擦

### 2026-04-20(Round 14 review 后更新)
- 同意 Claude review:
  - 平台级 97% 口径过高;应拆成 `QQQ 主路径 97% / 平台终态约 95%`
  - Pillar A 之前仍是元数据门面,必须补运行时 plugin lifecycle
- 已落地:
  - `BaseStockPlugin` 增加 `on_init/on_features/on_intent/on_fill/on_shutdown`
  - `ComposableStocksKernelStrategy` 支持 `plugin_cls`/`plugin_instance` 加载,并在 init/features/intent/fill/shutdown 路径调用 hook
  - 新增 `strategies/stocks_kernel/plugins/example_breakout.py` 作为可执行 plugin 模板

### 2026-04-20(继续推进)
- 已落地:
  - `shared_plane_readback_diff_total` 指标
  - `UnderlyingSharedDataPlane.assert_latest_matches(...)`
  - QQQ monolith producer -> stocks kernel consumer 的 shared plane readback 一致性测试
  - FlipGuard exemption 必须带未来 `expires_at` 才生效;永久字符串豁免不再绕过
  - `exec_flip_guard_exemption_used_total` 指标与 `FlipGuardExemptionUsed` 告警
- 已落地:
  - contract metadata 自动推导 root symbol
  - Remove Alternatives 删除 PR owner/date

### 2026-04-20(删除替代路径绑定)
- Remove Alternatives owner/date:

| Deadline | Date | Owner | Scope | Required output |
|---|---:|---|---|---|
| D+0 | 2026-04-20 | Codex primary / Claude review | 禁止新增策略直接 import `SFS1sReader`; 禁止新增股票策略绕过 `BaseStockPlugin` | 本状态文件登记; 后续 PR review 按此执行 |
| D+30 | 2026-05-20 | Codex primary / Claude review | `strategies/` 下 `SFS1sReader` lint 禁令进入 CI | lint test 或 CI job 合入 |
| D+45 | 2026-06-04 | Codex primary / Claude review | `stocks_kernel` 旧 DSL 标记 deprecated; 禁止新增旧 DSL 实例 | deprecation warning + 文档更新 |
| D+60 | 2026-06-19 | Codex primary / Claude review | QQQ monolith plane read path 完成生产观测 | 10 个交易日 `shared_plane_readback_diff_total = 0` |
| D+90 | 2026-07-19 | Codex primary / Claude review | 删除策略层 `SFS1sReader` 暴露; 移除旧 stocks DSL 公共入口 | 删除 PR 提交并通过回归 |
| D+120 | 2026-08-18 | Codex primary / Claude review | `StrategyAlgorithm` 降级为 legacy compatibility only; 新增策略直接继承 lint fail | CI lint gate 合入 |

### 2026-04-20(FlipGuard root symbol authority)
- 已落地:
  - `risk_symbol=OPT:QQQ:...` 归一到 underlying root,避免期权与股票落入不同 cooldown bucket
  - `contract_details.underSymbol` / `contract_details.under_symbol` / object attribute `underSymbol` 自动推导 root
  - 显式 root 字段优先,IB metadata 次之,leveraged ETF override 兜底

### 2026-04-20(SFS1sReader lint guard)
- 已落地:
  - `tools/lint_strategy_sfs_reader_imports.py` 固定当前 legacy direct reader baseline
  - `tests/unit/tools/test_lint_strategy_sfs_reader_imports.py` 阻止新增 strategy 直接引用 `SFS1sReader`
  - baseline 只允许下降,不允许新增; D+30 前可直接接入 CI

### 2026-04-20(Shared plane publish latency)
- 已落地:
  - `shared_plane_publish_latency_ms{namespace,symbol}` 记录 SFS 1s -> shared plane publish 耗时
  - 单测验证 flush 发布 shared plane 时同步 observe latency

### 2026-04-20(Round 15 review 后更新)
- 同意 Claude review:
  - QQQ 主路径约 98%; 可插拔平台约 96%
  - `on_init` fail-fast / `on_fill`、`on_shutdown` fail-safe 是合理取舍,但必须写明
  - `on_intent` 阻挡需要插件名、原因和 Prometheus 指标,不能只依赖 audit
  - 可插拔平台继续提高分数需要至少两只不同 `BaseStockPlugin` 子类并发路径测试
- 已落地:
  - `exec_stock_plugin_intent_blocks_total{strategy_id,plugin_component,symbol,reason}`
  - `stock_plugin_intent_blocked` audit payload 补 `plugin_component` / `trace_id` / `side` / block reason
  - `BaseStockPlugin` 与 `ExampleBreakoutStockPlugin` 文档写明 init fail-fast、runtime fail-safe
  - 两个不同 stock plugin 实例的 fill attribution 隔离测试

### 2026-04-20(FlipGuard PG 持久化)
- 已落地:
  - `db/pg/migration_20260421_flip_guard.ddl`
  - `core.core_flip_guard` 作为生产持久化表
  - `FlipGuardRegistry.hydrate_from_pg(...)`
  - `FlipGuardRegistry.persist_submission_to_pg(...)`
  - `ExecutionService` 在检查前 hydrate,提交后 upsert
  - JSON state 保留为无 PG / 本地测试 fallback
