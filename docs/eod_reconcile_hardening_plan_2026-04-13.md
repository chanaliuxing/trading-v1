# 每日复盘对账修复方案（2026-04-13）

## 1. 背景与问题结论

基于 `2026-04-13` 复盘报告与运行审计，当前高优先级问题集中在：

1. `ibkr_input_missing`：复盘脚本 IB 输入口径不足（仅 open snapshot），收盘后无法完成逐单 system-IB 核对。
2. `missing_telegram_push`：部分订单在 Telegram 审计中缺失，核心原因包含：
   - 下单通知事件中 `broker_order_id` 可能为空；
   - 对账匹配过于依赖单一键（`broker_order_id`）。
3. 部分取消单（特别是保护单/退出单未成交取消）存在“执行审计可见但 Telegram 订单事件缺失”的观察差异。

目标：修复对账准确性，且不影响实盘交易稳定性与下单链路。

## 2. 变更范围与原则

- 优先修改离线复盘逻辑与通知字段完整性，不改交易决策和风控行为。
- 所有可能影响推送量的变更必须可开关（feature flag）。
- 先纸面/仿真验证，再实盘灰度。

## 2.1 Review 结论

- 当前方向正确，但需要把“交易路径禁改边界”明确下来，避免把报告修复扩散成交易行为改造。
- 本轮允许直接落地的内容只有两类：
  - 字段完整性修复：`PlacedOrder.broker_order_id`、通知事件 broker id 兜底。
  - 离线对账修复：Telegram 多键匹配、IB 输入有效性展示。
- 本轮不允许直接落地：
  - 修改风控审批、订单参数、保护单生成和取消时机。
  - 在实盘关键路径新增阻塞式 IB 历史查询。
  - 无 feature flag 地新增取消单 Telegram 推送。

## 2.2 本轮 No-Go 清单

- 不修改 `ExecPlan`、下单价格、数量、TIF、OCA 逻辑。
- 不修改 `risk_service`、`safety_kernel` 审批和拒绝条件。
- 不在 `place_*`、订单状态回调、成交回调中增加阻塞式网络查询。
- 不为降低报告噪音而掩盖真实异常。

## 3. TODO 清单

### P0（必须本轮完成）

- [x] `IB adapter`：在 `PlacedOrder` 写入 `broker_order_id=str(order_id)`。
  - 文件：`services/exec/adapters/ib_adapter.py`
- [x] `execution_service`：发布 `order_submitted` 通知时兜底 `broker_order_id`。
  - 规则：`broker_order_id = placed_order.broker_order_id or str(placed_order.order_id)`
  - 文件：`services/exec/execution_service.py`
- [x] 复盘脚本：Telegram 对账改为多键匹配链。
  - 匹配顺序：`broker_order_id -> perm_id -> order_ref`
  - 文件：`scripts/ai_trader/research/eod_reconcile_report.py`
- [x] 复盘脚本：明确输出“IB逐单一致性是否有效”的状态与原因（已有字段保持并强化文案）。

### P0.1（本轮明确不做）

- [x] 不默认开启 `order_cancelled` Telegram 推送（仅保留 feature flag 能力）。
- [ ] 不修改保护单/止盈止损的生成、取消、替换逻辑。
- [ ] 不把 `reqExecutions` / `reqCompletedOrders` 放进实盘下单路径。

### P1（建议本轮完成）

- [x] 复盘脚本：扩展 IB 订单输入源（当日 executions/completedOrders）用于收盘后逐单核对。
  - 文件：`scripts/ai_trader/research/eod_reconcile_report.py`
- [x] 增加针对今天问题场景的单测（broker id 缺失、order_ref 回退匹配、IB 输入空但执行存在）。
  - 文件：`tests/unit/scripts/ai_trader/research/test_eod_reconcile_report.py`
  - 文件：`tests/unit/exec/test_fill_notifications.py`

### P2（灰度后再开）

- [x] 新增 `order_cancelled` Telegram 事件（仅保护/退出单，默认关闭）。
  - 增加 feature flag：`TELEGRAM_NOTIFY_CANCELLED_ENABLED=0`
  - 文件：`services/exec/fill_event_handler.py`、`services/notifications/*`

## 4. 测试计划

## 4.1 单元测试

1. `IB adapter` 返回对象测试：
   - 断言 `PlacedOrder.broker_order_id` 非空且等于 `order_id` 字符串。
2. `execution_service` 通知事件测试：
   - 当 `placed_order.broker_order_id=None` 时，`NotificationEvent.broker_order_id` 仍有值。
3. 复盘匹配测试：
   - case A：broker id 匹配成功；
   - case B：broker id 缺失但 `order_ref` 匹配成功；
   - case C：broker id/order_ref 都缺失时才判定 `missing_telegram_push`。
4. IB 输入有效性测试：
   - 收盘后 open orders=0 但 executions/completedOrders 非空时，`broker_order_consistency_enabled=True`。

## 4.2 集成测试（回放/离线）

1. 使用 `2026-04-13` 数据回放生成复盘报告：
   - 对比修复前后 `missing_telegram_push` 数量变化；
   - 确认仅消除假阳性，不掩盖真实缺失。
2. 验证报告高光问题排序仍正确（严重度、可读性、可追溯字段完整）。

## 4.3 纸面盘/实盘灰度验证

1. 灰度仅开启 P0/P1（不启用取消单通知）。
2. 观察 1-2 个交易日：
   - 下单成功率、成交率、拒单率；
   - `notification_queue_full_drop`、`telegram_notification_failed`；
   - 复盘中 `ibkr_input_missing` 是否下降；
   - `missing_telegram_push` 是否与审计一致。

## 4.4 回归门禁

代码合入前必须满足：

1. 所有新增单测通过。
2. 现有执行通知相关测试通过。
3. 使用 `2026-04-13` 数据重跑复盘后：
   - `missing_telegram_push` 只保留真实缺失；
   - 不新增新的 `critical`；
   - 不改变订单成交结果和持仓结果。

## 5. 验收标准清单（Checklist）

## 5.1 功能正确性

- [x] 复盘报告可稳定输出。
- [x] `order_submitted` 审计中 broker id 不再无故为空。
- [x] Telegram 对账支持多键匹配，明显减少假阳性缺失。
- [x] 收盘后脚本已尝试获取 IB 当日订单维度数据（`executions/completedOrders/open_orders` 合并）。
- [ ] 当前生产 Gateway 会话需实际返回当日历史订单；若仍为空，则需接入 Flex/历史导出兜底。

## 5.2 交易安全性（强制）

- [ ] 未修改下单决策逻辑、风控阈值、下单参数。
- [ ] 未增加阻塞式通知调用到交易关键路径。
- [ ] 未引入新的重试风暴或消息洪泛。
- [ ] 实盘核心指标无回归（下单/成交/风控/延迟）。

## 5.3 可观测性与可追溯

- [ ] 复盘报告中每个 high/critical 项都可回溯到原始审计字段。
- [ ] 报告明确标识“IB逐单一致性检查是否生效”。
- [ ] 对账失败项包含足够上下文字段（strategy_id、order_key、symbol、details）。

## 5.4 回滚标准

任一条件触发立即回滚至仅离线修复：

- [ ] 出现交易链路异常（下单失败率显著上升）。
- [ ] 通知队列持续满载或推送失败激增。
- [ ] 实盘监控出现新 P0/P1 告警。

## 6. 实施顺序（建议）

1. 先改 P0 字段完整性：`ib_adapter` + `execution_service`。
2. 再改 P0 离线对账：复盘脚本 Telegram 多键匹配。
3. 跑单测和 `2026-04-13` 复盘回归。
4. 若 Gateway 仍不给历史订单，则进入下一提交：接入 Flex/历史导出兜底。

## 7. 交付物

- 代码变更 PR（P0/P1）。
- 测试结果（单测 + 回放对比）。
- 当日与次日复盘报告对照（修复前后）。
- 灰度观察记录与结论。
