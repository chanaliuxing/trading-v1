# Stocks Kernel Observability TODO

## 已落地

- 统一本地审计 writer：`runtime/audit/YYYY-MM-DD/{strategy,exec,market,recon}-HH.jsonl`
- 异步 append-only writer：热路径只 enqueue，后台 flush，失败 fail-open
- 统一事件 schema：包含 `domain/component/severity/status/strategy_id/strategy_run_id/trigger_id/decision_id/trace_id/order_ref/broker_order_id/perm_id/symbol/reason/payload`
- `strategy` 链路审计：
  - `strategy_initialized`
  - `gate_allowed`
  - `gate_blocked`
  - `signal_evaluated`
  - `entry_plan_built`
  - `intent_ready`
- `exec` 链路审计：
  - `entry_lot_opened`
  - `order_acknowledged`
  - `stop_registered`
  - `flatten_requested`
  - `entry_fill_applied`
  - `exit_fill_applied`
  - `fill_quarantined`
  - `broker_submit_ack`
  - `fill_received`
  - `db_persisted`
  - `db_persist_failed`
- 查询工具支持：
  - `strategy_id`
  - `symbol`
  - `position_lot_id`
  - `order_ref`
  - `trace_id`
  - `broker_order_id`
  - `stage`
  - `domain`
  - `date`
  - `hour`
- 日报汇总工具：按 `date` 汇总 `domain/stage/reason/strategy`
- cleanup recon 审计：
  - `residual_cleanup_started`
  - `residual_cleanup_preview`
  - `residual_cleanup_completed`
- `stocks_kernel` startup isolation metrics：
  - `stocks_kernel_startup_preflight_total`
  - `stocks_kernel_startup_blocked`
  - `stocks_kernel_symbol_kill_switch_total`

## 未完成

- metrics 看板 / 告警接线
- cleanup validation bundle 自动汇总

## 下一步 TODO

1. 把 startup failure metrics 接到 Grafana / alert
2. 增加 cleanup validation bundle
