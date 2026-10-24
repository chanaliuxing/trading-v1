# Scorecard 模板（run_system 与 llm_trader 统一）

> 用途：每日/每周评估两条线，支撑 Champion–Challenger 决策、回滚/晋升。
> 数据源：
> - run_system：Alpha Loop 输出（`alpha_out/*/gen_*/gen_summary.json`, `rows.csv`）
> - llm_trader：ClickHouse（`llm_trader.decisions`）与复盘导出（`llm_trader/results/decisions_YYYYMMDD.csv`）

---

## 1) 元信息
- 日期：YYYY-MM-DD（ET）
- 线别：run_system | llm_trader
- 会话：PRE | RTH | POST | OVN（可多选与切片）
- 配置/版本：策略/Prompt 版本、权重（70/30 等）、git commit（可选）

---

## 2) 关键指标（成本/滑点后净值）
- 收益与风险：
  - E（期望值）：____（说明口径：每笔/每周期/整日）
  - Sharpe：____    Sortino：____
  - Max Drawdown（MDD）：____
  - Profit Factor（PF）：____  HitRate：____  Trades：____
- 执行质量（如适用）：
  - 滑点 p95（bp）：____（门：见成本模型/历史分位）
  - 决策延迟 p95（ms）：____（DLB 约束）
  - 拒单率/异常率：____
- LLM 专属：
  - 推理一致性分：____（复盘判定：理由与数据吻合度）

参考命令（llm_trader 当日摘要）：
```
clickhouse-client --query "
SELECT countIf(executed=1) trades,
       round(avg(pnl),2) avg_pnl,
       round(sum(pnl),2) total_pnl,
       round(sum(pnl>0)*100.0/nullIf(countIf(executed=1),0),2) win_rate
FROM llm_trader.decisions WHERE toDate(ts)=today() AND executed=1 FORMAT Pretty"
```

---

## 3) 稳健性切片（必须覆盖）
- 按会话：PRE/RTH/POST/OVN（四象限）
- 按波动：高/低波（按 RVOL/ATR 分位）
- 按事件：财报/宏观/大跳空（如适用）

输出：各切片 Sharpe/Sortino/MDD/Trades；坏区间标注与原因。

---

## 4) 对比（基线 vs 候选）
- 基线（Champion）与候选（Challenger）逐项对比：Sharpe↑ / Sortino↑ / MDD↓（或不劣）/ PF↑ / 命中率↑
- LLM：一致性分≥阈值（自定：如≥0.6/0.7）

结论：
- 是否满足“晋升为 Challenger/提升权重”的门槛：是 | 否（理由）

---

## 5) 回滚判据（强制）
- 账户级：日内止损预算/滚动 MDD 触发
- 执行质量：滑点/延迟超阈或拒单异常
- 健康门：`logs/system_health.json` 转 `error`

动作：
- 立即降档/回滚至 Champion（或 Shadow），停止新仓/清理旧仓。

---

## 6) 决策与行动
- 今日 A/B 权重：Champion ____ / Challenger ____（默认 70/30）
- 改动（参数/Prompt/节奏）与预期收益-风险影响：____
- 监控要点（指标/阈值/查询）：____
- 复核时间点（盘中/日终/周终）：____

