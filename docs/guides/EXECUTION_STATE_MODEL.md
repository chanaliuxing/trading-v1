# Execution State Model

目的：
给股票和期权执行链提供同一套正式状态语义，避免：

1. `accepted` 被误当成 `filled`
2. `flatten` 同时被多套局部状态解释
3. `protection` 失败后仍继续当正常 lane 运行
4. broker / DB / strategy 各说各话

代码入口：
- [execution_state_model.py](./services/exec/execution_state_model.py)

升级约束与验收：
- [QQQ_STATE_MACHINE_UPGRADE_TODO_TEST_ACCEPTANCE.md](./docs/guides/QQQ_STATE_MACHINE_UPGRADE_TODO_TEST_ACCEPTANCE.md)

---

## 1. Position Lane State

定义：
1. `intent_open`
2. `pending_fill`
3. `live`
4. `pending_exit`
5. `flattening`
6. `closed`
7. `quarantined`

解释：

### `intent_open`
- 策略产生 entry intent
- 还未进入 broker submit 成功态

### `pending_fill`
- broker 已接受 submit
- 但尚未形成真实 live 仓位
- 这是当前系统必须明确区分的关键态

### `live`
- broker fill 已确认
- 持仓真实存在

### `pending_exit`
- 普通退出意图已成立
- 但仍未完成最终归零

### `flattening`
- lane 已进入独占 flatten 控制流
- 不允许与 entry / TP / trailing 并发竞争

### `closed`
- 该 lane 已彻底结束

### `quarantined`
- 该 lane 进入隔离态
- 系统不再把它当正常交易路径处理

---

## 2. Protection State

定义：
1. `protected`
2. `protection_recovery`
3. `protection_failed`
4. `freeze_new_entry`

解释：

### `protected`
- live 仓位已有有效 broker-side 保护

### `protection_recovery`
- 系统正在恢复保护
- 允许有限恢复逻辑

### `protection_failed`
- 保护恢复失败
- 这是 correctness failure，不是普通 warning

### `freeze_new_entry`
- 同 strategy + symbol 新 entry 必须阻断

---

## 3. Flatten Lifecycle State

定义：
1. `created`
2. `submitted`
3. `pending`
4. `confirmed`
5. `failed`
6. `cancelled`
7. `rejected`
8. `quarantined`

解释：

### `created`
- flatten intent 已创建

### `submitted`
- flatten 单已发往 broker

### `pending`
- flatten 已触发，但 broker / DB / strategy 尚未完全归零

### `confirmed`
- 已确认：
  - broker qty == 0
  - broker open orders == 0
  - DB position == 0
  - DB open orders 收口

### `failed`
- flatten 流程已失败，需要明确处理

### `cancelled`
- flatten 被取消

### `rejected`
- flatten 被 broker 或 guard 拒绝

### `quarantined`
- flatten 长时间不收口，进入隔离/人工介入路径

---

## 4. 合法状态转移

代码里已定义并可直接单测：
- [execution_state_model.py](./services/exec/execution_state_model.py)
- [test_execution_state_model.py](./tests/unit/exec/test_execution_state_model.py)

关键规则：

### Position
1. `intent_open -> pending_fill`
2. `pending_fill -> live`
3. `live -> pending_exit`
4. `live -> flattening`
5. `pending_exit -> live/flattening/closed`
6. `flattening -> closed/quarantined`

明确禁止：
1. `intent_open -> live`
2. `pending_fill -> flattening`
3. `confirmed flatten -> pending`

### Protection
1. `protected -> recovery/failed/freeze`
2. `recovery -> protected/failed/freeze`
3. `failed -> freeze`
4. `freeze -> recovery/protected`

明确禁止：
1. `failed -> protected` 直接跳回

### Flatten
1. `created -> submitted/rejected/cancelled/failed/quarantined`
2. `submitted -> pending/confirmed/rejected/cancelled/failed/quarantined`
3. `pending -> confirmed/rejected/cancelled/failed/quarantined`

---

## 5. 为什么这套状态模型必要

如果没有统一状态模型，系统就会持续出现这类问题：

1. `accepted` 被误当成 live position
2. `flatten` 已提交，但 strategy 以为已平，reconciler 以为未平
3. `protection_failed` 只是日志 warning，没有形成系统冻结
4. DB、broker、strategy 的状态互相不能映射

统一状态模型的价值不是“更好看”，而是：
- 防止非法状态跳转
- 让审计、测试、reconciler、controller 说同一语言

---

## 6. Phase 1 落地边界

这一阶段只做两件事：

1. 把统一状态模型定义出来
2. 用单测把合法/非法转移钉死

这一阶段**不直接重写**：
- execution service
- safety kernel
- flatten controller
- protection controller

这些属于后续 phase。

---

## 7. 后续接入顺序

1. `Flatten Controller` 先接入这套模型
2. `Protection Controller` 再接入
3. `RuntimeReconciler` 和 `FillEventHandler` 最后都转成按这套模型说话

只有这样，后续的执行链才不是局部 patch，而是真正统一。
