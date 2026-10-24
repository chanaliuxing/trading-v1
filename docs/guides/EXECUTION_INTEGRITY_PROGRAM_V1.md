# Execution Integrity Program v1

目的：
把当前“可运行、可兜底、但仍有结构性分散”的执行链，推进到“统一状态模型、统一控制器、统一收敛规则”的生产级体系。

适用范围：
- 股票执行链
- 期权执行链
- `services/exec/*`
- `services/reconciliation/*`
- `strategies/*_kernel/*`

---

## 1. Program Goal

这期只解决三个核心问题：

1. 统一仓位/订单生命周期状态机
2. 统一 `flatten` 与 `protection` 两个控制器
3. 统一 broker truth -> DB -> strategy 的收敛规则

这不是策略 alpha 项目。
这是一项执行完整性项目。

---

## 2. Review + Critical

### 2.1 当前系统已经具备的能力

1. `accepted != filled` 已开始在 `option kernel` 落地。
2. `PROTECTION_FAILED -> freeze` 已落地。
3. `flatten submit -> freeze` 已落地。
4. `fill confirm -> unlock` 已落地。
5. `fill` 漏回调时，`runtime_reconciler` 已能自动确认 `flatten` 完成并释放 `flatten_action` 锁。
6. stale option DB position 已支持 auto-heal。
7. restart 时 stale DB 残仓不会再轻易复活成 live plan。

### 2.2 当前仍然存在的结构性问题

1. 状态语义仍分散：
   - strategy runtime state
   - option ownership lot
   - `core.core_order.state`
   - `flatten_lifecycle`
   - lock store
   - reconciler audit
   这些状态还没有统一到一套正式状态机。

2. `flatten` 仍是“轻量状态机 + 多点配合”，不是独立控制器。

3. `protection invariant` 仍是“分层收紧”，不是单一 controller 托管。

4. broker / DB / strategy 的收敛规则已经变强，但仍分散在：
   - fill handler
   - safety kernel
   - runtime reconciler
   - strategy bootstrap

5. 这意味着系统现在已经不是“容易爆炸”，但仍未达到“可形式化证明正确”的层级。

### 2.3 关键判断

后续不应该再按“哪里漏补哪里”的方式做。
应该按专项推进：

1. 先定义统一状态模型
2. 再把 `flatten` 升级成独立控制器
3. 再把 `protection` 升级成独立控制器
4. 最后统一 convergence rules

---

## 3. Target Architecture

### 3.1 Position Lifecycle Controller

统一管理状态：

1. `intent_open`
2. `pending_fill`
3. `live`
4. `pending_exit`
5. `flattening`
6. `closed`
7. `quarantined`

要求：
- 股票和期权共用主框架
- 策略层只消费 controller 状态，不自管持仓真相

### 3.2 Protection Controller

统一管理：

1. `protected`
2. `protection_recovery`
3. `protection_failed`
4. `freeze_new_entry`

要求：
- 任何 `live` 仓位都必须满足：
  - broker-side protection 有效
  - 或处于 recovery/freeze

### 3.3 Flatten Controller

统一管理：

1. `created`
2. `submitted`
3. `pending`
4. `confirmed`
5. `failed`
6. `cancelled`
7. `quarantined`

要求：
- `flatten` 拥有 lane 控制权
- 与普通 exit / TP / trailing 不并发竞争

### 3.4 Convergence Rules

统一管理：

1. 何时 broker truth 覆盖 DB
2. 何时 auto-heal
3. 何时 quarantine
4. 何时只能人工介入

---

## 4. Delivery Phases

### Phase 1: Execution State Model

交付：
- `docs/guides/EXECUTION_STATE_MODEL.md`
- 状态枚举
- 合法转移图
- 非法转移检测

成功标准：
- 所有订单/仓位状态都能映射到统一模型
- 不再出现“同一时刻多套语义冲突”

### Phase 2: Flatten Controller

交付：
- `services/exec/flatten_controller.py`
- 统一 `flatten` 完成确认路径
- 统一 lane freeze / unlock

成功标准：
- `flatten` 成为独立控制流
- `EOD flatten` 与 `manual flatten` 共享同一完成判定

当前进度：
- `services/exec/flatten_controller.py` 已落地最小可用版本
- 已接入 `ExecutionService / SafetyKernel / RuntimeReconciler`
- 仍需继续把 `FillEventHandler` 与更多 terminal path 收到 controller 出口

### Phase 3: Protection Controller

交付：
- `services/exec/protection_controller.py`
- live position invariant checker
- recovery / failure / freeze 统一托管

成功标准：
- “有仓必有保护或冻结”变成系统硬约束

### Phase 4: Convergence Rules

交付：
- `services/reconciliation/convergence_rules.py`
- auto-heal / quarantine / manual 三分流规则

成功标准：
- broker / DB / strategy 的不一致进入可解释收敛框架

---

## 5. Testing Plan

### 5.1 Phase 1: 状态模型测试

验证：
1. 每条现有路径都能映射到统一状态
2. 非法状态转移会被拒绝或告警
3. `accepted -> live` 这种错误跳转被封死

测试类型：
- unit
- state transition matrix test
- audit snapshot regression

### 5.2 Phase 2: Flatten Controller 测试

验证：
1. freeze lane
2. cancel related working orders
3. submit flatten
4. broker qty == 0
5. broker open orders == 0
6. DB position == 0
7. DB open orders == 0
8. confirmed / pending / failed / quarantined 判定正确

测试类型：
- unit
- integration
- dirty-state replay

### 5.3 Phase 3: Protection Controller 测试

验证：
1. partial fill attach
2. full fill attach
3. sibling cancel
4. stale child cleanup
5. restart recover
6. protection failure freeze

测试类型：
- unit
- restart simulation
- option OCO replay

### 5.4 Phase 4: Convergence Rules 测试

验证：
1. broker flat + DB non-flat -> auto-heal 或 quarantine
2. broker non-flat + DB flat -> mismatch escalation
3. symbol-scope / strategy-scope 不串
4. option / stock 都遵循同一框架

测试类型：
- unit
- recon integration
- dirty-state matrix

---

## 6. Acceptance Standard

Program 结束时，必须全部满足：

1. 同一策略同一 lane 不重复开仓
2. `accepted` 永远不会被当成 `filled`
3. live 仓位永远有保护或被冻结
4. `flatten` 永远能进入：
   - `confirmed`
   - `failed`
   - `quarantined`
   之一，不能悬空
5. broker / DB / strategy 三处最终一致
6. 正常路径 fill attribution 不依赖 fallback
7. 收盘后不依赖 cleanup tool 才能收干净
8. 运维可以通过审计直接判断：
   - 哪条 lane 正常
   - 哪条 lane 待恢复
   - 哪条 lane 需要人工介入

---

## 7. Immediate Next Step

下一步不再继续碎片化补洞。

直接启动：

1. `Phase 1: Execution State Model`
2. 同时输出 `Flatten Controller` 文件级改造清单

只有把这两份先做出来，后面的改造才会稳定。
