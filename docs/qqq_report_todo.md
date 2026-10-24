# QQQ 可观测性与日报 TODO

## 目标
做成交易员可直接看懂的日报：每分钟看到数据是否到位、是否触发下单、是否成交、未下单原因是哪个 gate。

## P0（本周必须）
- [x] `tools/qqq_generate_report.py` 增加 `--from/--to`，默认只出 `09:30-16:00 ET`。
- [x] 时间线改为“单分钟唯一行”，同分钟内合并 `heartbeat + entry_blocked + order/fill`。
- [x] 增加 `Gate Matrix`：`amp/vwap_trend/add_velocity/deviation/atr/qty` 每分钟 PASS/FAIL。
- [x] 增加 `Order Lifecycle`：`intent_submitted -> broker_ack -> partial_fill -> full_fill -> flatten`。
- [ ] 增加 `No-Trade Root Cause` 自动判定：给出主因、次因、证据时间点。

## P1（增强可用性）
- [ ] 输出 CSV：`runs/reports/*.csv`，字段可直接给 BI/Grafana 画图。
- [ ] 输出 `gate_reason` 分布图（按分钟堆叠）。
- [ ] 增加“运行分段”视图：按每次 `qqq_intraday_initialized` 切分。
- [ ] 报告里标注代码版本漂移（同一天多 `code_version` 直接报警）。

## P2（策略侧改造）
- [x] 修复重启状态恢复：`_restore_runtime_state()` 后要恢复 `self._day`，避免同日被 `_daily_reset_if_needed()` 覆盖。
- [x] `price_base` 在重启后不可再计算时，写明确诊断事件：`price_base_window_missed`。
- [ ] 在 `_maybe_enter()` 中额外写 `gate_pass_snapshot`（即便最终被后续 gate 拦截）。
- [ ] 增加 `order_id / perm_id / fill_qty / avg_fill_px` 到策略日志字段。

## 验收标准
- [ ] 给定某一天，能在 1 分钟内回答：
  - [ ] 有没有交易（下单/成交）？
  - [ ] 如果没交易，主 gate 是哪个？
  - [ ] 从几点到几点卡在该 gate？
  - [ ] 当时数据是否新鲜、是否缺字段？
- [ ] 日报不需要再逐个打开 `trader.log / diag.jsonl / state.json`。
