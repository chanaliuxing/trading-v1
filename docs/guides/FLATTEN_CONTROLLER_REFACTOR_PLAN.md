# Flatten Controller Refactor Plan

目的：
把当前散落在：

- [base.py](./strategies/options_kernel/base.py)
- [execution_service.py](./services/exec/execution_service.py)
- [safety_kernel.py](./services/exec/safety_kernel.py)
- [fill_event_handler.py](./services/exec/fill_event_handler.py)
- [runtime_reconciler.py](./services/reconciliation/runtime_reconciler.py)

里的 `flatten` 语义，收敛成一个独立控制器。

---

## 1. 当前问题

虽然已经有：
- `flatten_action`
- lane freeze
- fill confirm
- reconciler auto-confirm
- `flatten_lifecycle`

但它仍然是“多模块协作的轻量状态机”，不是一个独立 controller。

结果：
1. 代码分散
2. 完成判定分散
3. 失败判定分散
4. 运维虽然能 trace，但开发仍难以局部收口

---

## 2. 目标

新增：
- `services/exec/flatten_controller.py`

职责只做一件事：
- 托管 lane 级 flatten 生命周期

不做：
- 产生策略信号
- 选择合约
- 解释 alpha

---

## 3. Controller 接口

建议接口：

1. `start_flatten(...)`
2. `mark_submitted(...)`
3. `mark_fill_event(...)`
4. `mark_cancelled(...)`
5. `mark_rejected(...)`
6. `reconcile_completion(...)`
7. `get_lane_state(...)`

输入最少要带：
1. `strategy_id`
2. `symbol`
3. `order_id`
4. `decision_id`
5. `source`
   - `eod`
   - `manual`
   - `risk`
   - `protective`

---

## 4. 统一完成判定

控制器内统一判定：

### `confirmed`
必须全部满足：
1. broker qty == 0
2. broker open orders == 0
3. DB position == 0
4. DB working orders == 0

### `pending`
已 submit 或 fill 发生，但上面 4 条未全满足

### `failed`
guard/broker/db 明确失败

### `quarantined`
超过 grace window 仍未收口，且不能安全 auto-heal

---

## 5. 文件级改造清单

### 5.1 新增文件

1. [flatten_controller.py](./services/exec/flatten_controller.py)
2. [test_flatten_controller.py](./tests/unit/exec/test_flatten_controller.py)

### 5.2 修改文件

1. [execution_service.py](./services/exec/execution_service.py)
   - submit `flatten_action` 后，不直接散写状态
   - 改为调用 controller

2. [safety_kernel.py](./services/exec/safety_kernel.py)
   - fill/cancel/reject 后，不直接散写 `flatten_lifecycle`
   - 改为调用 controller

3. [fill_event_handler.py](./services/exec/fill_event_handler.py)
   - flatten completion audit 改为读取 controller 状态

4. [runtime_reconciler.py](./services/reconciliation/runtime_reconciler.py)
   - `flatten_incomplete_detected`
   - auto-confirm
   - auto-unlock
   改为通过 controller API 触发，而不是直接改 note

5. [run_system.py](./run_system.py)
   - wiring `FlattenController`

---

## 6. 数据落点

短期：
- 继续使用 `core.core_order.note.flatten_lifecycle`

中期：
- 升级到独立表，例如：
  - `core.flatten_lifecycle`

建议：
1. Phase 2 先做 controller + note-backed adapter
2. 后续再切到独立表

这样风险最小。

---

## 7. 测试计划

### 单测
1. `created -> submitted`
2. `submitted -> pending`
3. `submitted/pending -> confirmed`
4. `submitted/pending -> failed/rejected/cancelled`
5. 超时 -> `quarantined`

### 集成测试
1. EOD flatten 闭环
2. manual flatten 闭环
3. fill 漏回调时 reconciler 补确认
4. broker flat 但 DB 不净时 pending/confirm 判定

### 脏状态测试
1. stale open orders
2. stale DB position
3. stale entry lock
4. stale flatten note

---

## 8. 验收标准

1. `flatten` 成为独立控制流
2. `flatten` 状态只从 controller 出口更新
3. 不再由多个模块各自发明 `flatten` 终态
4. EOD/manual flatten 共享同一完成确认规则
5. 运维看到 lane 时，能直接知道：
   - `submitted`
   - `pending`
   - `confirmed`
   - `failed`
   - `quarantined`

---

## 9. 实施顺序

1. 先保留现有 note 持久化
2. 新建 `FlattenController`
3. 先接入 `ExecutionService`
4. 再接入 `SafetyKernel`
5. 再接入 `FillEventHandler`
6. 最后把 `RuntimeReconciler` 改成只调用 controller

这样不会一次性把现有闭环打散。
