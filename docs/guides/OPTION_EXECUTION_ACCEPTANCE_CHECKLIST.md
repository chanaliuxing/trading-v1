# Option Execution Acceptance Checklist

使用方式：
- 先对照 [EXECUTION_INTEGRITY_PROGRAM_V1.md](./docs/guides/EXECUTION_INTEGRITY_PROGRAM_V1.md) 看当前处于哪个阶段。
- 每个阶段的交付必须先过本清单对应部分，再进入下一阶段。

## A. 下单链
- [ ] 同一策略同一 lane 不会重复开仓
- [ ] `accepted` 不会直接变成 `live`
- [ ] `pending_fill` 期间不会再次发 entry
- [ ] `pending_exit` 期间不会再次发 entry

## B. 持仓链
- [ ] broker / DB / strategy 三处仓位最终一致
- [ ] broker 已 flat 时，DB position 会自动归零或进入 quarantine
- [ ] 不会出现“strategy 认为有仓、broker 无仓”长期残留
- [ ] 不会出现“broker 有仓、strategy 正常继续开新仓”

## C. 保护单链
- [ ] live 仓位始终有 broker-side protection 或 recovery/freeze 状态
- [ ] `PROTECTION_FAILED` 后同 strategy+symbol 新 entry 会被自动阻断
- [ ] partial fill 后 child qty 正确
- [ ] child 成交后 sibling 正确撤销
- [ ] 持仓归零后不残留 stale stop/tp
- [ ] 重启后 stale DB child 不会复活

## D. Flatten 链
- [ ] EOD flatten 带 `close_only=true`
- [ ] EOD flatten 带 `flatten_action=true`
- [ ] flatten submit 后同 strategy+symbol 新 entry 会被自动阻断
- [ ] flatten 前先 cancel 相关 working orders
- [ ] flatten 完成后 broker qty == 0
- [ ] flatten 完成后 broker open orders == 0
- [ ] flatten 完成后 DB order terminal
- [ ] flatten 完成后 DB position == 0
- [ ] flatten 完成后对应 entry lock 自动释放
- [ ] flatten fill 后存在明确的 `flatten_completion_confirmed/pending` 审计
- [ ] `core.core_order.note.flatten_lifecycle` 能反映 `submitted / confirmed / pending / cancelled / rejected`
- [ ] 长时间未收口的 flatten 会触发 `flatten_incomplete_detected`

## E. Reconcile 链
- [ ] ghost / orphan / mismatch 都能被正确分类
- [ ] reconciler 不会把活单误 terminalize
- [ ] reconciler 不会把 account-level broker position 直接乱写进 strategy position
- [ ] 正常订单闭环不依赖 reconciler 收尾

## F. 审计链
- [ ] 每笔 entry / exit / fill / cancel / OCO 都能反查完整链路
- [ ] 正常路径不出现 `decision_id=unknown` 常态
- [ ] 正常路径不出现 `strategy_run_id=unknown` 常态
- [ ] fallback attribution 仅限异常/历史脏数据场景
- [ ] correctness alert 与 noise alert 已区分

## G. 运维链
- [ ] 开盘前能核对 broker positions / broker open orders / DB positions / DB open orders
- [ ] 第一笔 live/paper 单可以完整 trace
- [ ] 收盘后可确认系统 clean，无残仓、残单、残 reservation
- [ ] dirty-state cleanup 有标准顺序，不会再通过 bootstrap 激活 stale child orders

## H. Phase Gate
- [ ] Phase 1：统一状态模型已定义，且现有链路都能映射
- [ ] Phase 2：`flatten` 已形成独立控制流，不再只是散落状态
- [ ] Phase 3：`protection invariant` 已成为系统硬约束
- [ ] Phase 4：broker / DB / strategy 收敛规则已统一

## I. Program Exit Criteria
- [ ] 股票和期权都使用同一套执行完整性框架
- [ ] 正常交易日不依赖 cleanup tool 才能收干净
- [ ] correctness alert 可直接驱动人工介入决策
- [ ] 盘中、收盘、重启三种场景都可解释、可追踪、可收敛
