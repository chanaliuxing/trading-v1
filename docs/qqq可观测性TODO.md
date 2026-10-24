# QQQ 可观测性 TODO

目标：让 `qqq_intraday_001` 在盘中与盘后都能快速回答三件事：
1. 策略是否在正常运行。
2. 为什么没有交易。
3. 时间线上每个关键节点发生了什么。

## P0（已落地）

- [x] 补充“标准时间线（Expected Timeline）”到操作文档
  - [x] 定义节点：`09:25` 启动、`09:30` 首个 heartbeat、`09:45` 锁参、`09:46` 首次入场判定、`15:45` 强平、收盘后复盘。
  - [x] 每个节点写“缺失时检查命令 + 排查路径”。

- [x] 固化“真相源优先级”
  - [x] `runtime/state/qqq_intraday_qqq_intraday_001.json`（最终是否交易）
  - [x] `runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl`（为什么没下单）
  - [x] `runtime/state/qqq_intraday_observability_qqq_intraday_001.json`（数据健康）
  - [x] `~/grafana-stack/logs/trader.log`（系统级上下文）

- [x] 增加“5分钟诊断命令包”章节（可复制执行）
  - [x] 一条命令看 `initialized/code_version/heartbeat/entry_blocked/entered/shutdown`。
  - [x] 一条命令看 `missing_required` 与 `stale`。
  - [x] 一条命令看 `SFS-1s Redis write timed out`。

- [x] 完善 `qqq_entry_blocked` reason 字典
  - [x] 逐项解释 reason 含义（reason 已覆盖所有 return 路径）。
  - [x] 每个 reason 给“下一步动作”（见 `docs/qqq操作文档.md`）。

- [x] 增加“版本一致性检查”
  - [x] `qqq_intraday_initialized` 增加 `code_version`（git short sha，失败 `unknown`）。
  - [x] 运行中的 `code_version` 与当前 `git rev-parse --short HEAD` 对比，不一致不做结论。

## 已落地清单（代码）

- [x] 每分钟心跳：`qqq_loop_heartbeat` 写入 `runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl`
  - 字段：`strategy_id/is_traded/amp_locked/deadwater/toxic_day/last_price_add/data_age_qqq_sec/data_age_add_sec/code_version`
- [x] 每分钟入场拒单：`qqq_entry_blocked` 写入同一 `jsonl`
  - 字段：`reason/amp_locked/close_now/close_prev/vwap/long_ok/short_ok/add_now/add_ago/add_threshold/qqq_last/deviation/dev_threshold/atr/atr_period/exec_symbol`
- [x] 低噪音观测快照：每分钟写 `runtime/state/qqq_intraday_observability_qqq_intraday_001.json`
- [x] 启动时立刻写一次可观测快照；循环异常时也尝试写（避免“无文件盲区”）

## P1（本周完成）

- [ ] 增加日终复盘模板（固定结构）
  - [ ] 交易结果：是否交易、入场时间、方向、出场原因。
  - [ ] 未交易原因：Top3 `qqq_entry_blocked` reason 统计。
  - [ ] 时间线：关键事件按时间排序。
  - [ ] 异常：数据断流、Redis timeout、重启事件。

- [ ] 增加可观测 SLO/告警阈值
  - [ ] `qqq_loop_heartbeat` 超过 70 秒无更新报警。
  - [ ] observability 文件超过 70 秒无更新报警。
  - [ ] `missing_required` 连续 5 分钟报警。
  - [ ] `SFS-1s Redis write timed out` 连续出现报警。

- [ ] 补充“重启/中断识别规则”
  - [ ] 识别 `Initiating graceful shutdown` -> `System shutdown complete` -> `qqq_intraday_initialized` 的重启链路。
  - [ ] 重启前后状态文件对比（`is_traded/amp_locked/price_0945`）。

## P2（可选增强）

- [ ] 增加每日自动摘要脚本（输出 Markdown）
  - [ ] 输入：交易日。
  - [ ] 输出：关键 timeline + gate 阻断统计 + 异常摘要。

- [ ] 增加 Grafana 面板
  - [ ] heartbeat 连续性。
  - [ ] missing_required 计数。
  - [ ] entry_blocked reason 分布。
  - [ ] entered/flatten 事件时间轴。

## 验收标准（Definition of Done）

- [x] 任一交易日 5 分钟内可回答“为什么没交易”。
- [x] 任一交易日 5 分钟内可还原关键 timeline。
- [x] 不再出现“只有锁参日志，没有后续判定信息”的盲区。

## 5分钟诊断命令包（直接复制）

```bash
# 1) 进程与版本指纹
pgrep -af ./run_system.py
tail -n 50 runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl | rg "qqq_intraday_initialized|qqq_loop_heartbeat|qqq_entry_blocked|qqq_entered|qqq_flatten_submitted|shutdown"

# 2) 观察数据完整性与新鲜度
cat runtime/state/qqq_intraday_observability_qqq_intraday_001.json | jq '.ready,.missing_required,.required_values'

# 3) Redis/SFS 写入风险
rg "SFS-1s Redis write timed out|redis write timeout" ~/grafana-stack/logs/trader.log | tail -n 20
```

## 执行记录

- Owner：Codex + 用户
- 开始日期：2026-02-09
- 目标完成日期：2026-02-09（P0）
- 最近更新：2026-02-09
- 备注：P1/P2 保留为后续增强项
