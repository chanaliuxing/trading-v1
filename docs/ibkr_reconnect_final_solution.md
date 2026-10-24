# IBKR 断线重连与幽灵订单治理：最终方案（已按代码落地）

## 1. 结论（采纳/修正）
- 采纳 Claude 的核心修正：`reconciler` 必须先去武器化，再收紧 clientId。
- 采纳 ChatGPT 的硬门槛：M1 是“删能力代码路径”，不是仅改配置开关。
- 采纳统一交易闸门定义：`trading_enabled = upstream_trading_enabled AND no_unresolved_quarantine_for_symbol`。
- 保留 Codex 的主线：Registry + Master Truth + Epoch + Quarantine + Report-only。

## 2. Critical Review（对原方案的关键改动）
- 原先风险点：`RuntimeReconciler` 运行路径仍可能触发自动 cancel/terminalize。
  现已修正：`run_once()` 不再调用 flat protective auto-cancel 与 flatten lifecycle auto-terminal 入口；`_cleanup_ghosts/_adopt_orphans` 明确降为禁用（仅告警返回）。
- 原先风险点：运行态仍可能依赖 fresh-probe 辅助会话。
  现已修正：`RuntimeReconciler` 的 ghost confirm 不再触发 fresh-probe；适配器侧辅助 probe 默认关闭（显式环境变量才开）。
- 原先风险点：交易可用判断在不同模块分散。
  现已修正：ExecutionService 接受上游 `trading_enabled`，并统一叠加 symbol quarantine 形成最终布尔闸门。

## 3. 五个高风险实施包（仅拆到可编码包）
1. `PKG-1 Reconciler 去武器化`
   责任文件：`services/reconciliation/runtime_reconciler.py`, `run_system.py`, `tests/unit/services/test_runtime_reconciler_*`
   完成定义：运行态无自动 cancel/terminalize/adopt；仅检测+审计+quarantine。

2. `PKG-2 Master Truth + 启动闸门`
   责任文件：`services/ibkr/ib_connection_manager.py`, `run_system.py`
   完成定义：`require_master` readiness 生效，启动日志可见 master truth 完整性。

3. `PKG-3 订单身份与隔离`
   责任文件：`db/pg/*.sql`, `services/exec/execution_service.py`, `services/exec/safety_kernel.py`
   完成定义：`core.core_order` 写入 `ib_client_id/ib_session_role/ib_connection_epoch`；存在未知 broker 单时对 symbol 拦单。

4. `PKG-4 ClientId Registry + 漂移移除`
   责任文件：`services/ibkr/client_id_registry.py`, `services/ibkr/ib_connection_manager.py`, `services/exec/adapters/ib_adapter.py`, `config.yml`
   完成定义：角色化确定分配、状态持久化、移除 reconnect 随机漂移。

5. `PKG-5 Epoch 句柄化`
   责任文件：`services/ibkr/ib_connection_manager.py` + 市场/组合长生命周期消费者
   完成定义：消费者通过 handle+epoch 获取 IB；旧 epoch 不再被静默复用。

6. `PKG-6 Daily Restart Stale-Circuit 自愈`
   责任文件：`services/ibkr/ib_connection_manager.py`, `services/production/recovery_readiness.py`, `services/market/market_data_service.py`, `services/market/aggregator/five_sec_agg.py`, `docs/guides/IBKR_RECONNECT_STORM_AND_STUCK_RECOVERY_RUNBOOK_2026-04.md`
   完成定义：IB Gateway 已恢复但持久 circuit 仍 open 时，`wait_until_ready` 允许 bounded disposable API probe 验证 broker API；probe 成功后关闭 circuit 并继续 market/exec/master 恢复。盘后/闭市 market-data tick/bar stale 不再误判为 IB recovery failure，RTH 仍硬挡。

## 4. 当前代码状态（对应五包）
- `PKG-1`: 已落地（含测试基线更新）。
- `PKG-2`: 已落地。
- `PKG-3`: 已落地。
- `PKG-4`: 已落地。
- `PKG-5`: 已落地到核心消费者（market/options/backfill/vix/order_monitor/account_service）。
- `PKG-6`: 已落地（含 stale circuit probe 自愈单测）。

## 5. 变更后运行原则
- Reconciler 是异常检测器，不是自动修复器。
- Broker 未知订单进入 quarantine，阻断同 symbol 新 intent。
- ClientId 由 Registry 决定，运行期不漂移。
- 交易放行只认统一布尔闸门。
- Persistent Gateway circuit 只能阻断真实长连接；不能永久阻断 disposable readiness proof。
- Broker recovery 和 data freshness 分层：broker 三路恢复后，盘后缺 tick/bar 不能把系统误报为 IB 未恢复；RTH 数据新鲜度仍由 recovery/data gates 硬挡。
