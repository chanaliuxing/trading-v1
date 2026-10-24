# Option Execution Test Plan

目标：覆盖期权执行链最危险的状态收敛问题，而不是只测 happy path。

执行方式：
1. 先按 [EXECUTION_INTEGRITY_PROGRAM_V1.md](./docs/guides/EXECUTION_INTEGRITY_PROGRAM_V1.md) 的阶段推进。
2. 每个阶段先补状态模型测试，再补功能测试，再补 dirty-state 测试。
3. 不允许只补 happy path 单测就算通过。

## 1. 状态机测试

### 1.1 Entry lifecycle
验证：
1. `intent_open -> pending_fill -> live`
2. `accepted` 不会直接进入 `live`
3. 未成交期间不会重复开仓
4. `pending_exit` 期间不会再触发新 entry

### 1.2 Exit lifecycle
验证：
1. 普通 exit
2. partial exit
3. EOD flatten
4. manual flatten

验证点：
- `close_only`
- `flatten_action`
- `position_qty_hint`
- exit 后状态归零

## 2. Protection / OCO 测试

### 2.1 First-fill attach
验证：
1. first fill 后及时 attach OCO
2. partial fill 时 child qty 正确
3. fill coalesce 后 child qty 最终与仓位一致

### 2.2 Child lifecycle
验证：
1. one child filled -> sibling cancelled
2. stale local pending child 会触发 rearm 或 protection_failed
3. 持仓归零后 child order 不残留
4. `protection_failed` 后同 strategy+symbol 新 entry 会被 lock 阻断

### 2.3 Restart recovery
验证：
1. broker 有 live child -> 可恢复
2. broker flat / child 不可见 -> 不恢复 stale DB child
3. stale DB position 不会在 bootstrap 后被当成 live 仓

## 3. Flatten 测试

### 3.1 EOD flatten
验证：
1. 先 cancel 相关 working orders
2. submit close-only flatten
3. 无 quote 时仍可按预期风格提交
4. submit 后同 strategy+symbol 新 entry 会被 lock 阻断
5. 完成后 broker qty == 0 / open orders == 0 / DB position == 0，并自动解锁
6. fill 后会产生 `flatten_completion_confirmed` 或 `flatten_completion_pending` 审计
7. `core.core_order.note.flatten_lifecycle` 会反映 `submitted / confirmed / pending / cancelled / rejected`
8. `runtime_reconciler` 会对超时未收口的 flatten 发出 `flatten_incomplete_detected`

### 3.2 Manual flatten
验证：
1. manual flatten 不会与普通策略 entry 竞争
2. flatten 完成后设置 intervention lock 或等效保护
3. 不会重复发多张 flatten

## 4. Reconciliation 测试

### 4.1 Ghost / orphan / mismatch
验证：
1. DB 有、IB 无 -> ghost
2. IB 有、DB 无 -> orphan
3. 双方都有、状态不同 -> mismatch

### 4.2 Position convergence
验证：
1. broker flat + no working orders -> stale DB position auto-heal
2. option internal symbol 同样适用
3. broker non-flat 时不得误归零 DB position

## 5. Traceability 测试

验证：
1. 正常 entry / exit / fill / cancel / terminal status 都能串到：
   - strategy_id
   - decision_id
   - order_ref
   - position_lot_id
   - broker_order_id
   - perm_id
2. 正常路径不出现 `unknown` 常态
3. fallback attribution 仅限历史脏数据或 adopt 场景

## 6. 实盘前演练

### 6.1 Paper 演练
至少验证：
1. 第一笔 entry 完整闭环
2. 第一笔 stop/tp 完整闭环
3. 一笔 EOD flatten 完整闭环
4. 收盘后 recon 仍然 clean

### 6.2 Dirty-state 演练
人工构造：
1. stale DB position
2. stale child order row
3. orphan open order
4. restart recovery
5. protection attach failure / rearm exhausted

目标：
- 系统进入可解释状态，不乱修正、不乱开仓。

## 7. 分阶段专项测试

### 7.1 Phase 1: 状态模型
验证：
1. 现有 entry / exit / fill / flatten / protection 路径都能映射到统一状态
2. 非法转移会被拒绝或发 correctness audit
3. `accepted -> live` 这类错误跳转不能出现

### 7.2 Phase 2: Flatten Controller
验证：
1. lane freeze 生效
2. 相关 working orders 先撤
3. flatten 成功提交
4. broker / DB / strategy 三处归零后进入 `confirmed`
5. 不能归零时进入 `pending/failed/quarantined`
6. fill 漏回调时 reconciler 能补确认，但不会误清 `manual_flatten/protection_failed` 锁

### 7.3 Phase 3: Protection Controller
验证：
1. live 仓位有保护或被冻结
2. partial fill / full fill / restart / reconnect 都不会破坏 invariant
3. stale child order 不会复活

### 7.4 Phase 4: Convergence Rules
验证：
1. broker truth 覆盖规则一致
2. auto-heal / quarantine / manual 三分流一致
3. stock / option 共用框架，不串 symbol，不串 strategy
