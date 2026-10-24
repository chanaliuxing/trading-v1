# Ghost Order / Ghost Position 落地 TODO（可打勾）

目标：  
- 所有期权组合单（Bull Put Spread）统一走 `IBInsyncAdapter` 执行通路；  
- 引入明确的订单/仓位状态机，只在真实成交后才建立 position；  
- 所有状态变化都可审计，支持重启恢复与日终对账；  
- 彻底消除“订单被拒但策略以为有仓位”的幽灵问题。

---

## A. 执行通道统一：IBKR Options → IBInsyncAdapter
- [x] **A1. 复查当前期权执行路径**
  - [x] 确认 `IBInsyncAdapter.place_combo_order(...)` 已存在且可用于下 BAG 组合单。
- [x] **A2. 提炼统一的 Vertical Spread 下单接口**
  - [x] 在 `IBInsyncAdapter` 中新增 `place_vertical_spread(...)`：
    - 组装两个 `Option` → 构建 `ComboLeg` → 调用 `place_combo_order(...)`。  
    - 固化 expiry 格式转换、共用 `PlacedOrder`。
- [x] **A3. 改造 IBKROptionsDataProvider 复用 IBInsyncAdapter**
  - [x] 复用 `services.exec.adapters.ib_adapter.get_ib_adapter()` 全局连接（减少多 IB 连接导致的状态漂移）。  
  - [x] `place_vertical_spread` 返回统一 schema（`status/order_id/order_ref/oca_group/...`）。
  - [x] Stub backend 也返回同一 schema（`status="Filled"`/`order_id` 为虚拟值）。

- [x] **B1. 明确订单状态模型**
  - [x] `OptionsDataProvider._normalize_status()` 统一映射到 `OrderState`（`NEW/SUBMITTED/PARTIAL/FILLED/CANCELLED/REJECTED`），策略/监控都使用同一套值。
- [x] **B2. 扩展 OptionTradeRepository & 数据库**
  - [x] 增加迁移 SQL：`sql/option_spread_trades_v2.sql`（添加 `order_id/order_status/filled_* /reason`）。  
  - [x] `OptionTradeRepository` 支持写入 v2 字段，并提供 `update_order_status(...)`（best-effort）。  
  - [x] `scripts/apply_option_spread_schema.py` + `make apply-option-schema` 已执行，PG 表结构现含全部 v2 列。

## C. OrderMonitor 服务：监听 & 等待填单
- [x] **C1. 新建 `services/portfolio/order_monitor.py`**
  - [x] in-memory 映射：`order_id -> {status, filled_qty, avg_price, reason}`（status cache）。  
  - [x] 提供 `wait_for_final(order_id)`、`get_current_status(order_id)`。
- [x] **C2. 连接 IB 回调与 OrderMonitor**
  - [x] 订阅 `ib.orderStatusEvent`，在状态变化时可唤醒 `wait_for_fill`。  
  - [x] 在状态变化时 best-effort 调用 `OptionTradeRepository.update_order_status`（需要 v2 schema）。  
  - [x] 订阅 `ib.execDetailsEvent`（由 `IBInsyncAdapter` 管理），为未来扩展成交/手续费信息做准备。

## D. 重构 BullPutSpreadStrategy：Pending Entry vs Open Position
- [x] **D1. 引入策略内部状态字段**
  - [x] `pending_position`/`position`（预留 `pending_exit`），含 state/订单号。
- [x] **D2. 改写 `_try_enter`**
  - [x] 下单后只记录 pending（不立即建仓、不立即挂 TP/SL）；  
  - [x] 只在 transmit=true 时进入 pending 跟踪，transmit=false 仅落库+审计（避免幽灵仓位）。
- [x] **D3. 实现 Entry Fill 确认（避免幽灵仓位）**
  - [x] 事件驱动：`OrderMonitor.wait_for_fill` 触发 `Filled` 后才升级为真实仓位并挂 TP/SL。  
  - [x] 轮询兜底：仍保留 `get_order_status` 查询作为 fallback。  
  - [x] `Cancelled/Rejected/Inactive/ApiCancelled/TIMEOUT` 立即清除 pending 并落库/审计。
- [x] **D4. `_manage_position` 避免重复平仓/重复下单**
  - [x] 若已挂出 TP/SL（OCA）订单，则优先监控其 `Filled` 状态，避免再发“手动 close”。  
  - [x] `_close` 修正为 `side="BUY"` 并携带 OCA group（减少行为不一致）。  
  - [x] Exit/close 路径也接入 OrderMonitor（TP/SL/close 提交后等待 Filled/NotFilled，再落库+审计）；pending_exit 清理。  
  - [x] 显式记录 `state_change`（OPEN→CLOSING→CLOSED）到 Postgres + audit log，方便追溯 ghost state。

## E. 审计与日志增强
- [x] **E1. JSONL & ClickHouse 事件扩展**
  - [x] 审计事件通过 `_audit_event` 统一写入 `order_id/order_status/state/filled_price/reason` 打底字段，后续可直接 JSONExtract。  
- [x] **E2. 监控**
  - [x] `scripts/check_bull_put_health.py`（`make check-bull-put-health`）提供 CLI 健康检查并纳入 `recon-all`。  
  - [x] `dashboards/bull_put_spread_dashboard.json` 提供 Entry Fill Ratio/State Distribution/Skip 事件面板，可直接导入 Grafana；`doc/bull_put_spread_monitoring.md` 补充 SQL 示例与面板说明。

## F. IB vs 本地仓位对账
- [x] **F1. `scripts/check_positions_consistency.py`**
  - [x] 比对 IB 实际持仓 vs 本地 `option_spread_trades` 推导仓位。  
  - [x] 输出 diff + 非零 exit code。
- [ ] **F2. 集成到 nightly job**
  - [x] 提供 `make check-positions`（手动/定时调用均可）。  
  - [x] 已加入 `make recon-all`（夜间自动跑一次 ghost-position 对账）。

## G. 测试计划
- [ ] **G1. 单元测试**
  - [ ] `BullPutSpreadStrategy`：拒单不建仓、填单后建仓、取消后清理。  
  - [ ] `OrderMonitor`：模拟 `orderStatus` 更新确保 `wait_for_final` 正常。
- [ ] **G2. 集成测试（stub backend）**
  - [ ] 模拟 entry 填单/取消、exit 流程，验证 JSONL + DB 记录。
- [ ] **G3. IBKR Paper 实测**
  - [ ] transmit=false & transmit=true 场景验证，无幽灵仓位；  
  - [ ] 对账脚本始终一致。

---

后续按此 TODO 逐项打勾，并在 `Git` 里跟踪每一阶段的代码修改与测试结果。
