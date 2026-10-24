# Option Execution TODO

来源：
- [TRADING_EXECUTION_RED_BOOK](./docs/guides/TRADING_EXECUTION_RED_BOOK.md)
- [OPTION_EXECUTION_GAP_LIST.md](./docs/guides/OPTION_EXECUTION_GAP_LIST.md)

目标：把期权执行链从“可控但未完全收口”推进到“生产级可验证”。

## P0

### 1. 保护单 invariant 系统化
- 为 live option position 增加统一 protection invariant 检查。
- 定义 `protected / protection_recovery / protection_failed` 的强状态语义。
- 在 `safety kernel` 或更高一层加入 correctness alert：
  - 有仓无保护
  - stale child order
  - protection rearm exhausted
- 让执行层和策略侧都能识别 `protection_failed/recovery`，自动阻断新开仓。

验收：
- live 仓位全时段有 broker-side 保护，或处于明确 recovery/freeze 模式。

### 2. Flatten 闭环机械化
- 定义统一 flatten 状态流：
  - freeze lane
  - cancel related working orders
  - submit flatten
  - confirm broker qty == 0
  - terminalize child orders
  - zero DB position
  - release reservation
- 禁止 flatten 与 entry/TP/trailing 并发竞争同一 lane。
- 为 EOD flatten 和 manual flatten 建立同一完成确认路径。

验收：
- flatten 完成后，broker / DB / strategy 三处均归零。

当前状态：
- `FlattenController` 最小骨架已落地，并已接入 `ExecutionService / SafetyKernel / RuntimeReconciler`
- 下一步是把更多散落的 terminal/审计出口继续收到 controller

## P1

### 3. DB position 自动收敛规则独立化
- 明确 broker flat + no working orders 时的 DB position auto-heal 规则。
- 将“auto-heal / quarantine / manual intervention”三种动作做成明确分支。
- option / stock 共用同一判断框架，但按 instrument 保留细节差异。

验收：
- 不再出现审计 clean 但 `core.core_position` 非零长期残留。

### 4. Protection child order 恢复/认领重构
- child order 恢复必须先看 broker truth。
- 拆开：
  - orphan adopt
  - protection recover
  - stale child cleanup
- 不允许仅凭 DB child row 把 protection 重新当 live。

验收：
- 重启后 stale stop/tp 不会复活。

### 5. Traceability 去 fallback 常态化
- 正常路径补齐：
  - `decision_id`
  - `strategy_run_id`
  - `position_lot_id`
  - `perm_id`
- 让 `fill_context_missing_fields` 只出现在历史脏数据/外部 adopt 场景。

验收：
- 正常 live fill/cancel/terminal status 不再出现 `unknown` 常态。

## P2

### 6. Reconciler 降级为纯兜底
- 保留 ghost/orphan/mismatch 修正能力。
- 但把正常订单闭环做成不依赖 reconciler 的主路径。
- 降低高频 orphan/ghost 告警密度。

验收：
- 正常交易日内，多数订单不依赖 reconciler 收尾。

### 7. 审计 correctness/noise 分层
- 定义 correctness alert：
  - live position without protection
  - broker/db position mismatch
  - flatten incomplete
  - normal-path fill attribution missing
- 其余探针失败、单次 fallback 归类为 noise/degraded。

验收：
- 运维值班可以按告警等级直接判断是否需要人工介入。

## 已落地
- `accepted != filled`：option kernel 状态机已修。
- `close_only + flatten_action`：EOD exit 已补足。
- `close_only MKT`：translator 已尊重，不再误回 LMT。
- `cancel_open_orders`：`order_ref + symbol + strategy` 精确匹配已加强。
- OCO child persistence 冲突更新已补强。
- pending entry 不再误表现为 `POSITION_OPEN`。
- `runtime_reconciler` 已支持 option stale DB position auto-heal。
- `safety_kernel` bootstrap 已增加 broker position truth 检查，避免 stale DB 残仓重启后复活为 live plan。
- `safety_kernel` 在 `PROTECTION_FAILED` 时已写入 strategy+symbol 级 entry lock，阻止未受保护 lane 继续开新仓。
- `flatten_action` 提交后已写入 strategy+symbol 级 entry lock，并在 flat + 无 open orders 后自动解锁。
- `flatten_lifecycle` 已持久化进 `core.core_order.note`，可用于提交/完成/异常终态追踪。
- `runtime_reconciler` 已增加 `flatten_incomplete_detected` 巡检，用于发现长时间未收口的 flatten lane。
- `runtime_reconciler` 已能在 broker 已 flat 且无 IB open orders 时自动确认 `flatten_lifecycle=confirmed`，并只释放 `flatten_action` 锁。
